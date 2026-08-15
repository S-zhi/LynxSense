"""流水线编排：把 ①~⑤ 串成一条任务，逐步上报进度。

设计为纯逻辑：不碰数据库 / Redis，只通过 on_event 回调把状态与进度往外抛。
Worker 层把 on_event 接到「写 SQLite + 发 SSE」即可。

各步的内部百分比按权重映射到整体 0-100：
  下载 0-20 · 提取 20-35 · 识别 35-65 · 翻译 65-85 · 烧录 85-100
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from src.config import SOURCE_VIDEO_STEM, task_dir
from src.core.audio_extractor import extract_audio
from src.core.downloader import download_video
from src.core.subtitle_burner import burn_subtitles
from src.core.transcriber import transcribe
from src.core.translator import translate_srt
from src.service.asset_resolver import AssetResolver, ResourceError, ResourceState

logger = logging.getLogger(__name__)


class PipelineError(RuntimeError):
    """流水线编排阶段的错误（如上传源缺失）。"""


@dataclass
class PipelineParams:
    task_id: str
    url: str
    source_lang: str
    target_lang: str
    mode: str = "mono"     # mono | bilingual
    burn: str = "hard"     # hard | soft
    model: str = "small"
    engine: str = "deepseek"
    source_type: str = "url"    # url=在线链接下载 upload=本地上传视频
    need_subtitle: bool = True  # False = 仅下载视频，跳过识别/翻译/烧录
    title: Optional[str] = None  # 上传模式下用原始文件名作为展示标题


@dataclass
class PipelineEvent:
    status: str
    progress: int
    current_step: Optional[str]
    title: Optional[str] = None
    error: Optional[str] = None
    outputs: Optional[dict] = None


EventHook = Callable[[PipelineEvent], None]

# (status, 整体进度下界, 上界)
_BANDS = {
    "DOWNLOADING": (0, 20),
    "EXTRACTING": (20, 35),
    "TRANSCRIBING": (35, 65),
    "TRANSLATING": (65, 85),
    "BURNING": (85, 100),
}


def _scale(lo: int, hi: int, pct: Optional[float]) -> int:
    if pct is None:
        return lo
    return int(lo + max(0.0, min(100.0, pct)) / 100.0 * (hi - lo))


def run_pipeline(
    params: PipelineParams,
    on_event: EventHook,
    *,
    api_key: Optional[str] = None,
) -> PipelineEvent:
    """顺序执行五步，并按已有产物从最近完成的阶段继续。"""
    tid = params.task_id
    state = {"progress": 0, "step": None}

    def emit(status: str, progress: int, **extra) -> None:
        state["progress"] = max(state["progress"], progress)
        state["step"] = status if status in _BANDS else None
        on_event(PipelineEvent(
            status=status,
            progress=state["progress"],
            current_step=state["step"],
            **extra,
        ))

    def step_cb(status: str):
        lo, hi = _BANDS[status]

        def cb(p) -> None:
            prog = _scale(lo, hi, getattr(p, "percent", None))
            emit(status, prog)

        return cb

    def artifact_available(resolver) -> bool:
        resource_state, path, _ = resolver(tid)
        return resource_state == ResourceState.AVAILABLE and path is not None

    try:
        emit("DOWNLOADING", 0)

        # 第①步获取源视频：已有完整源文件时跳过下载；上传模式始终复用本地文件。
        if params.source_type == "upload":
            video_path = _locate_uploaded_source(tid)
            title = params.title or video_path.stem
            emit("DOWNLOADING", 20)  # 本地视频已就位，"下载/载入"阶段直接完成
        elif artifact_available(AssetResolver.resolve_source):
            video_path = AssetResolver.require_source(tid)
            title = params.title
            emit("DOWNLOADING", 20)  # 服务重启后复用已完成的下载
        elif not params.need_subtitle:
            # 仅下载模式：下载占满整条进度，跳过识别/翻译/烧录
            dl = download_video(
                params.url, tid,
                on_progress=lambda p: emit("DOWNLOADING", _scale(0, 100, getattr(p, "percent", None))),
            )
            video_path = AssetResolver.require_source(tid)
            title = dl.title
        else:
            dl = download_video(params.url, tid, on_progress=step_cb("DOWNLOADING"))
            video_path = AssetResolver.require_source(tid)
            title = dl.title

        # 仅下载 / 仅载入本地视频：不做字幕处理，直接以源视频完成
        if not params.need_subtitle:
            outputs = {"video": str(video_path)}
            final = PipelineEvent("SUCCESS", 100, None, title=title, outputs=outputs)
            on_event(final)
            logger.info("仅获取视频完成: task=%s source=%s", tid, params.source_type)
            return final

        emit("EXTRACTING", 20)
        video_path = AssetResolver.require_source(tid)
        if artifact_available(AssetResolver.resolve_audio):
            audio_path = AssetResolver.require_audio(tid)
            emit("EXTRACTING", 35)  # 服务重启后复用已完成的音频提取
        else:
            extract_audio(video_path, tid, on_progress=step_cb("EXTRACTING"))
            audio_path = AssetResolver.require_audio(tid)

        emit("TRANSCRIBING", 35)
        if artifact_available(AssetResolver.resolve_original_srt):
            original_srt_path = AssetResolver.require_original_srt(tid)
            emit("TRANSCRIBING", 65)  # 服务重启后复用已完成的识别结果
        else:
            tr = transcribe(
                audio_path, tid,
                language=params.source_lang,
                model_name=params.model,
                on_progress=step_cb("TRANSCRIBING"),
            )
            original_srt_path = AssetResolver.require_original_srt(tid)

        emit("TRANSLATING", 65)
        if artifact_available(AssetResolver.resolve_translated_srt):
            translated_srt_path = AssetResolver.require_translated_srt(tid)
            emit("TRANSLATING", 85)  # 服务重启后复用已完成的翻译结果
        else:
            tl = translate_srt(
                original_srt_path, tid,
                params.source_lang, params.target_lang,
                mode=params.mode,
                on_progress=step_cb("TRANSLATING"),
                api_key=api_key,
            )
            translated_srt_path = AssetResolver.require_translated_srt(tid)

        emit("BURNING", 85)
        video_path = AssetResolver.require_source(tid)
        if artifact_available(AssetResolver.resolve_output_video):
            output_video_path = AssetResolver.require_output_video(tid)
            emit("BURNING", 100)  # 服务重启后复用已完成的烧录结果
        else:
            burn_subtitles(
                video_path, translated_srt_path, tid,
                mode=params.burn,
                on_progress=step_cb("BURNING"),
            )
            output_video_path = AssetResolver.require_output_video(tid)

        outputs = {"video": str(output_video_path), "subtitle": str(translated_srt_path)}
        final = PipelineEvent("SUCCESS", 100, None, title=title, outputs=outputs)
        on_event(final)
        logger.info("流水线完成: task=%s", tid)
        return final

    except ResourceError as e:
        logger.error("流水线因资源异常中断: task=%s step=%s, msg=%s", tid, state["step"], str(e))
        on_event(PipelineEvent(
            status="FAILED",
            progress=state["progress"],
            current_step=state["step"],
            error=str(e),
        ))
        raise
    except Exception as e:
        logger.exception("流水线失败: task=%s step=%s", tid, state["step"])
        on_event(PipelineEvent(
            status="FAILED",
            progress=state["progress"],
            current_step=state["step"],
            error=str(e),
        ))
        raise


def _locate_uploaded_source(task_id: str) -> Path:
    """定位上传模式下预先落盘的源视频 data/{task_id}/source.*。"""
    try:
        return AssetResolver.require_source(task_id)
    except ResourceError as e:
        raise PipelineError(str(e)) from e
