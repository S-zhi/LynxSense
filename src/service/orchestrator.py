"""流水线编排：把 ①~⑤ 串成一条任务，逐步上报进度。

设计为纯逻辑：不碰数据库 / Redis，只通过 on_event 回调把状态与进度往外抛。
Worker 层把 on_event 接到「写 SQLite + 发 SSE」即可。

各步的内部百分比按权重映射到整体 0-100：
  下载 0-20 · 提取 20-35 · 识别 35-65 · 翻译 65-85 · 烧录 85-100

断点续跑（Issue #30）：
  · 每个阶段用 ``Step`` 描述（name / band / 产物判定方式）。
  · ``run_pipeline(..., start_from="TRANSLATING")`` 可以从指定阶段开始；之前阶段
    视为已成功，产物由 ``validate_start_from`` 提前校验。
  · ``list_resume_options`` 扫产物给出当前可恢复起点；用于前端菜单与 /resume_options API。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Tuple

from src.config import (
    AUDIO_FILENAME,
    ORIGINAL_SRT,
    OUTPUT_VIDEO,
    SOURCE_VIDEO_STEM,
    TRANSLATED_SRT,
    task_dir,
)
from src.core.audio_extractor import extract_audio
from src.core.downloader import download_video
from src.core.subtitle_burner import burn_subtitles
from src.core.transcriber import transcribe
from src.core.translator import translate_srt

logger = logging.getLogger(__name__)


class PipelineError(RuntimeError):
    """流水线编排阶段的错误（如上传源缺失、start_from 产物不完整）。"""


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
    # 已成功完成的阶段名（按执行顺序追加）。前端可据此判断"从哪里继续"。
    completed_steps: Optional[List[str]] = None
    # 失败时倒下的阶段名（FAILED 事件携带）。None = 未失败。
    error_step: Optional[str] = None


EventHook = Callable[[PipelineEvent], None]


# ----------------------------------------------------------------------------
# 阶段描述
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Step:
    """流水线的一个阶段：描述其名、进度带、产物判定方式。"""

    name: str                              # 大写：DOWNLOADING / EXTRACTING / ...
    band: Tuple[int, int]                  # (lo, hi) 整体进度区间

    def artifact_candidates(self, task_id: str) -> List[Path]:
        """该阶段成功后会写出的产物路径列表。任一存在即视为"已成功"。"""
        d = task_dir(task_id)
        if self.name == "DOWNLOADING":
            # 源视频：扩展名由 yt-dlp 决定 / 上传可能是任意支持格式
            return sorted(d.glob(f"{SOURCE_VIDEO_STEM}.*"))
        mapping = {
            "EXTRACTING": [d / AUDIO_FILENAME],
            "TRANSCRIBING": [d / ORIGINAL_SRT],
            "TRANSLATING": [d / TRANSLATED_SRT],
            "BURNING": [d / OUTPUT_VIDEO],
        }
        return mapping[self.name]

    def is_artifact_ready(self, task_id: str) -> bool:
        """产物文件存在且非空 = 该阶段可视为已成功完成。"""
        for p in self.artifact_candidates(task_id):
            try:
                if p.is_file() and p.stat().st_size > 0:
                    return True
            except OSError:
                continue
        return False


# 完整流水线（含字幕）—— 顺序与 _BANDS 必须保持一致
PIPELINE_STEPS: Tuple[str, ...] = (
    "DOWNLOADING",
    "EXTRACTING",
    "TRANSCRIBING",
    "TRANSLATING",
    "BURNING",
)

# 各阶段的进度区间（与 PIPELINE_STEPS 一一对应）
_BANDS: dict = {
    "DOWNLOADING": (0, 20),
    "EXTRACTING": (20, 35),
    "TRANSCRIBING": (35, 65),
    "TRANSLATING": (65, 85),
    "BURNING": (85, 100),
}

DOWNLOAD = Step("DOWNLOADING", _BANDS["DOWNLOADING"])
EXTRACT = Step("EXTRACTING", _BANDS["EXTRACTING"])
TRANSCRIBE = Step("TRANSCRIBING", _BANDS["TRANSCRIBING"])
TRANSLATE = Step("TRANSLATING", _BANDS["TRANSLATING"])
BURN = Step("BURNING", _BANDS["BURNING"])

# name -> Step（用于按名查找）
_STEP_BY_NAME: dict = {s.name: s for s in (DOWNLOAD, EXTRACT, TRANSCRIBE, TRANSLATE, BURN)}


def pipeline_steps(need_subtitle: bool) -> Tuple[Step, ...]:
    """按 need_subtitle 给出实际会跑的步骤（need_subtitle=False 只跑下载）。"""
    return PIPELINE_STEPS if need_subtitle else ("DOWNLOADING",)


def pipeline_step_names(need_subtitle: bool) -> Tuple[str, ...]:
    return pipeline_steps(need_subtitle)


def _scale(lo: int, hi: int, pct: Optional[float]) -> int:
    if pct is None:
        return lo
    return int(lo + max(0.0, min(100.0, pct)) / 100.0 * (hi - lo))


# ----------------------------------------------------------------------------
# resume 工具
# ----------------------------------------------------------------------------


def is_valid_step_name(name: str) -> bool:
    return name in _STEP_BY_NAME


def validate_start_from(
    start_from: Optional[str],
    task_id: str,
    need_subtitle: bool,
) -> List[str]:
    """校验从 ``start_from`` 开始是否安全。

    - 阶段名必须合法且属于当前流水线（need_subtitle=False 时只能是 "DOWNLOADING"）
    - 所有前置阶段的产物必须存在且非空
    - start_from=None 时直接返回空列表（=从头跑，不做产物校验）

    Returns:
        start_from 之前视为已成功的阶段名列表（写库用）。

    Raises:
        PipelineError: 阶段名非法 / 产物缺失。
    """
    if start_from is None:
        return []

    if not is_valid_step_name(start_from):
        raise PipelineError(
            f"未知阶段: {start_from!r}（合法值: {', '.join(PIPELINE_STEPS)}）"
        )

    valid = pipeline_step_names(need_subtitle)
    if start_from not in valid:
        raise PipelineError(
            f"当前流水线不支持从 {start_from} 继续（仅下载任务只能从 DOWNLOADING 开始）"
        )

    start_idx = valid.index(start_from)
    completed_before: List[str] = []
    for name in valid[:start_idx]:
        step = _STEP_BY_NAME[name]
        if not step.is_artifact_ready(task_id):
            missing = step.artifact_candidates(task_id)
            miss_desc = "、".join(p.name for p in missing) or "产物"
            raise PipelineError(
                f"无法从 {start_from} 继续：阶段 {name} 的产物缺失（需要 {miss_desc}）"
            )
        completed_before.append(name)
    return completed_before


def list_resume_options(task_id: str, need_subtitle: bool) -> List[Tuple[str, bool, Optional[str]]]:
    """列出当前所有步骤的"是否可作为 resume 起点"。

    "可作为 resume 起点" = "前一步的产物都齐"。例如 TRANSLATING 可用 ⇔
    它的前置产物 original.srt 存在。

    Returns:
        [(step_name, available, reason), ...] 按 ``PIPELINE_STEPS`` 顺序。
        available=True 表示：从该步开始可以安全续跑（前序产物完整）。
        reason 不可用时填原因（哪类产物缺失）；可用时为 None。
    """
    out: List[Tuple[str, bool, Optional[str]]] = []
    names = pipeline_step_names(need_subtitle)
    for i, name in enumerate(names):
        if name == "DOWNLOADING":
            # 入口阶段：没有前置依赖，始终可作为起点
            out.append((name, True, None))
            continue
        # 取"前一步"的产物判断
        prev = _STEP_BY_NAME[names[i - 1]]
        if prev.is_artifact_ready(task_id):
            out.append((name, True, None))
        else:
            missing = prev.artifact_candidates(task_id)
            miss_desc = "、".join(p.name for p in missing) or "前置产物"
            out.append((name, False, f"缺少 {miss_desc}"))
    return out


# ----------------------------------------------------------------------------
# 主入口
# ----------------------------------------------------------------------------


def run_pipeline(
    params: PipelineParams,
    on_event: EventHook,
    *,
    api_key: Optional[str] = None,
    start_from: Optional[str] = None,
) -> PipelineEvent:
    """顺序执行流水线步骤。成功返回最终 SUCCESS 事件；失败发 FAILED 事件并抛出。

    Args:
        start_from: 从指定阶段开始；之前阶段视为已成功（产物已校验）。
                    None = 从头跑（与旧行为一致）。
    """
    tid = params.task_id
    steps = pipeline_steps(params.need_subtitle)
    state: dict = {
        "progress": 0,
        "step": None,
        "completed": [],
    }

    def emit(status: str, progress: int, **extra) -> None:
        state["progress"] = max(state["progress"], progress)
        if status in _BANDS:
            state["step"] = status
        on_event(PipelineEvent(
            status=status,
            progress=state["progress"],
            current_step=state["step"] if status in _BANDS else None,
            completed_steps=list(state["completed"]),
            error_step=None,
            **extra,
        ))

    def step_cb(status: str):
        lo, hi = _BANDS[status]

        def cb(p) -> None:
            prog = _scale(lo, hi, getattr(p, "percent", None))
            emit(status, prog)

        return cb

    def mark_completed(name: str) -> None:
        if name not in state["completed"]:
            state["completed"].append(name)

    # start_from 校验：失败时也要发 FAILED 事件（让 runner 知道这是真失败、可以重试）
    try:
        completed_before = validate_start_from(start_from, tid, params.need_subtitle)
    except PipelineError as e:
        logger.warning("start_from 校验失败: task=%s start_from=%s err=%s", tid, start_from, e)
        on_event(PipelineEvent(
            status="FAILED",
            progress=0,
            current_step=None,
            error=str(e),
            completed_steps=[],
            error_step=None,
        ))
        raise
    completed_set = set(completed_before)
    state["completed"] = list(completed_before)

    try:
        # ---------- 第①步：下载 / 载入源视频 ----------
        if "DOWNLOADING" in steps and "DOWNLOADING" not in completed_set:
            emit("DOWNLOADING", 0)
            if params.source_type == "upload":
                video_path = _locate_uploaded_source(tid)
                title = params.title or video_path.stem
                emit("DOWNLOADING", 20)
            elif not params.need_subtitle:
                # 仅下载模式：下载占满整条进度，跳过识别/翻译/烧录
                dl = download_video(
                    params.url, tid,
                    on_progress=lambda p: emit("DOWNLOADING", _scale(0, 100, getattr(p, "percent", None))),
                )
                video_path, title = dl.video_path, dl.title
            else:
                dl = download_video(params.url, tid, on_progress=step_cb("DOWNLOADING"))
                video_path, title = dl.video_path, dl.title
            mark_completed("DOWNLOADING")
        else:
            # 跳过：复用已下载/上传的源视频
            video_path = _locate_uploaded_source(tid)
            title = params.title or video_path.stem

        # 仅下载 / 仅载入本地视频：不做字幕处理
        if not params.need_subtitle:
            outputs = {"video": str(video_path)}
            final = PipelineEvent("SUCCESS", 100, None, title=title, outputs=outputs,
                                  completed_steps=list(state["completed"]))
            on_event(final)
            logger.info("仅获取视频完成: task=%s source=%s", tid, params.source_type)
            return final

        # ---------- 第②步：提取音频 ----------
        if "EXTRACTING" not in completed_set:
            emit("EXTRACTING", 20)
            au = extract_audio(video_path, tid, on_progress=step_cb("EXTRACTING"))
            mark_completed("EXTRACTING")
        else:
            au = _reuse_artifact(tid, AUDIO_FILENAME, "audio_path")

        # ---------- 第③步：语音识别 ----------
        if "TRANSCRIBING" not in completed_set:
            emit("TRANSCRIBING", 35)
            tr = transcribe(
                au.audio_path, tid,
                language=params.source_lang,
                model_name=params.model,
                on_progress=step_cb("TRANSCRIBING"),
            )
            mark_completed("TRANSCRIBING")
        else:
            tr = _reuse_artifact(tid, ORIGINAL_SRT, "srt_path")

        # ---------- 第④步：翻译 ----------
        if "TRANSLATING" not in completed_set:
            emit("TRANSLATING", 65)
            tl = translate_srt(
                tr.srt_path, tid,
                params.source_lang, params.target_lang,
                mode=params.mode,
                on_progress=step_cb("TRANSLATING"),
                api_key=api_key,
            )
            mark_completed("TRANSLATING")
        else:
            tl = _reuse_artifact(tid, TRANSLATED_SRT, "srt_path")

        # ---------- 第⑤步：烧录 ----------
        if "BURNING" not in completed_set:
            emit("BURNING", 85)
            bn = burn_subtitles(
                video_path, tl.srt_path, tid,
                mode=params.burn,
                on_progress=step_cb("BURNING"),
            )
            mark_completed("BURNING")
        else:
            bn = _reuse_artifact(tid, OUTPUT_VIDEO, "output_path")

        outputs = {"video": str(bn.output_path), "subtitle": str(tl.srt_path)}
        final = PipelineEvent("SUCCESS", 100, None, title=title, outputs=outputs,
                              completed_steps=list(state["completed"]))
        on_event(final)
        logger.info("流水线完成: task=%s", tid)
        return final

    except Exception as e:
        logger.exception("流水线失败: task=%s step=%s", tid, state["step"])
        on_event(PipelineEvent(
            status="FAILED",
            progress=state["progress"],
            current_step=state["step"],
            error=str(e),
            completed_steps=list(state["completed"]),
            error_step=state["step"],
        ))
        raise


def _locate_uploaded_source(task_id: str) -> Path:
    """定位 data/{task_id}/source.* —— 下载或上传的源视频都落在这里。"""
    d = task_dir(task_id)
    for p in sorted(d.glob(f"{SOURCE_VIDEO_STEM}.*")):
        if p.is_file():
            return p
    raise PipelineError(f"源视频缺失: {d}/{SOURCE_VIDEO_STEM}.*")


def _reuse_artifact(task_id: str, filename: str, attr_name: str):
    """构造一个 stub，让后续步骤以为刚跑完一样。用于被跳过的阶段。"""
    path = task_dir(task_id) / filename
    if not path.exists() or path.stat().st_size == 0:
        raise PipelineError(f"内部错误：阶段被标记跳过，但产物 {path} 缺失或为空")

    class _Stub:
        pass

    stub = _Stub()
    setattr(stub, attr_name, path)
    return stub
