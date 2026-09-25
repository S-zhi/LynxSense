"""流水线编排：把 ①~⑤ 串成一条任务，逐步上报进度。

设计为纯逻辑：不碰数据库 / Redis，只通过 on_event 回调把状态与进度往外抛。
Worker 层把 on_event 接到「写 SQLite + 发 SSE」即可。

各步的内部百分比按权重映射到整体 0-100：
  下载 0-20 · 提取 20-35 · 识别 35-65 · 翻译 65-85 · 烧录 85-100
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from src.config import task_dir
from src.core.audio_extractor import extract_audio
from src.core.downloader import download_video
from src.core.subtitle_burner import burn_subtitles
from src.core.transcriber import TranscribeCancelledError, transcribe
from src.core.translator import translate_srt
from src.service.asset_resolver import AssetResolver, ResourceError, ResourceState

logger = logging.getLogger(__name__)

_cancel_events: dict[str, threading.Event] = {}
_cancel_events_lock = threading.Lock()


def register_cancellation_signal(task_id: str, is_cancelled: bool = False) -> threading.Event:
    with _cancel_events_lock:
        ev = _cancel_events.setdefault(task_id, threading.Event())
        if is_cancelled:
            ev.set()
        else:
            ev.clear()
        return ev


def unregister_cancellation_signal(task_id: str) -> None:
    with _cancel_events_lock:
        _cancel_events.pop(task_id, None)


def set_cancelled_signal(task_id: str) -> None:
    with _cancel_events_lock:
        ev = _cancel_events.setdefault(task_id, threading.Event())
        ev.set()


def is_cancelled_signal(task_id: str) -> bool:
    with _cancel_events_lock:
        ev = _cancel_events.get(task_id)
        return ev.is_set() if ev is not None else False


class PipelineError(RuntimeError):
    """流水线编排阶段的错误（如上传源缺失）。"""


class PipelineCancelledError(RuntimeError):
    """流水线已被用户取消。"""


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
    error_code: Optional[str] = None
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

_EXCEPTION_CODE_MAP: tuple[tuple[type[Exception], str], ...] = (
    (KeyError, "invalid_input"),
    (ValueError, "invalid_input"),
    (AttributeError, "internal_error"),
    (TypeError, "internal_error"),
    (OSError, "io_error"),
)


def _error_code_for_exception(exc: Exception) -> str:
    """返回异常的稳定错误码，自定义非空 code 优先于类型映射。"""
    custom_code = getattr(exc, "code", None)
    if custom_code:
        return custom_code
    for exception_type, code in _EXCEPTION_CODE_MAP:
        if isinstance(exc, exception_type):
            return code
    return "internal_error"


def _scale(lo: int, hi: int, pct: Optional[float]) -> int:
    if pct is None:
        return lo
    return int(lo + max(0.0, min(100.0, pct)) / 100.0 * (hi - lo))


def _check_cancelled(task_id: str) -> None:
    if is_cancelled_signal(task_id):
        raise PipelineCancelledError("任务已被用户取消")


@dataclass
class PipelineResources:
    """责任链阶段之间传递的任务产物。

    当前资源由本地路径表示；将来可以把字段替换为带状态、元数据或远程
    引用的资源对象，而不必继续扩张 ``PipelineContext``。
    """

    video_path: Optional[Path] = None
    audio_path: Optional[Path] = None
    original_srt_path: Optional[Path] = None
    translated_srt_path: Optional[Path] = None
    output_video_path: Optional[Path] = None


@dataclass
class PipelineContext:
    """责任链各节点共享的任务参数、资源、进度与执行结果。"""

    params: PipelineParams
    on_event: EventHook
    api_key: Optional[str] = None
    engine_config: object = None
    resources: PipelineResources = field(default_factory=PipelineResources)
    progress: int = 0
    current_step: Optional[str] = None
    title: Optional[str] = None
    terminal_event: Optional[PipelineEvent] = None

    @property
    def task_id(self) -> str:
        return self.params.task_id

    def emit(self, status: str, progress: int, **extra) -> None:
        _check_cancelled(self.task_id)
        self.progress = max(self.progress, progress)
        self.current_step = status if status in _BANDS else None
        self.on_event(PipelineEvent(
            status=status,
            progress=self.progress,
            current_step=self.current_step,
            **extra,
        ))

    def step_callback(self, status: str):
        if status == "DOWNLOADING" and not self.params.need_subtitle:
            lo, hi = (0, 100)
        else:
            lo, hi = _BANDS[status]

        def callback(progress) -> None:
            self.emit(status, _scale(lo, hi, getattr(progress, "percent", None)))

        return callback

    def emit_smooth(self, status: str, target: int) -> None:
        start = self.progress
        if target > start:
            step_size = max(1, (target - start) // 4)
            current = start + step_size
            while current < target:
                self.emit(status, current)
                current += step_size
        self.emit(status, target)

    def artifact_available(self, resolver) -> bool:
        resource_state, path, _ = resolver(self.task_id)
        return resource_state == ResourceState.AVAILABLE and path is not None

    def complete(self, *, outputs: dict, title: Optional[str] = None) -> PipelineEvent:
        event = PipelineEvent("SUCCESS", 100, None, title=title, outputs=outputs)
        self.terminal_event = event
        self.on_event(event)
        return event


class PipelineHandler:
    """责任链节点：处理当前阶段，然后将共享上下文交给下一节点。"""

    def __init__(self) -> None:
        self._next_handler: Optional[PipelineHandler] = None

    def set_next(self, handler: "PipelineHandler") -> "PipelineHandler":
        self._next_handler = handler
        return handler

    def handle(self, context: PipelineContext) -> PipelineEvent:
        self.process(context)
        if context.terminal_event is not None:
            return context.terminal_event
        if self._next_handler is None:
            raise PipelineError("责任链未产生任务完成结果")
        return self._next_handler.handle(context)

    def process(self, context: PipelineContext) -> None:
        raise NotImplementedError


class DownloadHandler(PipelineHandler):
    """准备源视频；上传、断点续跑和仅下载分支均在本节点处理。"""

    def process(self, context: PipelineContext) -> None:
        params = context.params
        tid = context.task_id
        context.emit("DOWNLOADING", 0)
        target_progress = 100 if not params.need_subtitle else 20

        if params.source_type == "upload":
            context.resources.video_path = _locate_uploaded_source(tid)
            context.title = params.title or context.resources.video_path.stem
            context.emit_smooth("DOWNLOADING", target_progress)
        elif context.artifact_available(AssetResolver.resolve_source):
            context.resources.video_path = AssetResolver.require_source(tid)
            context.title = params.title
            context.emit_smooth("DOWNLOADING", target_progress)
        else:
            download = download_video(
                params.url,
                tid,
                on_progress=context.step_callback("DOWNLOADING"),
            )
            context.resources.video_path = AssetResolver.require_source(tid)
            context.title = download.title
            if not params.need_subtitle:
                context.emit("DOWNLOADING", 100)

        if not params.need_subtitle:
            context.complete(
                outputs={"video": str(context.resources.video_path)},
                title=context.title,
            )
            logger.info("仅获取视频完成: task=%s source=%s", tid, params.source_type)


class AudioExtractionHandler(PipelineHandler):
    """获取可识别音频，优先复用已完成的音频产物。"""

    def process(self, context: PipelineContext) -> None:
        tid = context.task_id
        context.emit("EXTRACTING", 20)
        context.resources.video_path = AssetResolver.require_source(tid)
        if context.artifact_available(AssetResolver.resolve_audio):
            context.resources.audio_path = AssetResolver.require_audio(tid)
            context.emit("EXTRACTING", 35)
        else:
            extract_audio(
                context.resources.video_path,
                tid,
                on_progress=context.step_callback("EXTRACTING"),
            )
            context.resources.audio_path = AssetResolver.require_audio(tid)


class TranscriptionHandler(PipelineHandler):
    """把音频转成原文 SRT，优先复用已有字幕产物。"""

    def process(self, context: PipelineContext) -> None:
        params = context.params
        tid = context.task_id
        context.emit("TRANSCRIBING", 35)
        if context.artifact_available(AssetResolver.resolve_original_srt):
            context.resources.original_srt_path = AssetResolver.require_original_srt(tid)
            context.emit("TRANSCRIBING", 65)
            return
        try:
            transcribe(
                context.resources.audio_path,
                tid,
                language=params.source_lang,
                model_name=params.model,
                on_progress=context.step_callback("TRANSCRIBING"),
                cancel_check=lambda: _check_cancelled(tid),
            )
        except TranscribeCancelledError as exc:
            raise PipelineCancelledError("任务已被用户取消") from exc
        context.resources.original_srt_path = AssetResolver.require_original_srt(tid)


class TranslationHandler(PipelineHandler):
    """翻译原文字幕，并在恢复时复用现有翻译产物。"""

    def process(self, context: PipelineContext) -> None:
        params = context.params
        tid = context.task_id
        context.emit("TRANSLATING", 65)
        if context.artifact_available(AssetResolver.resolve_translated_srt):
            context.resources.translated_srt_path = AssetResolver.require_translated_srt(tid)
            context.emit("TRANSLATING", 85)
            return
        translate_srt(
            context.resources.original_srt_path,
            tid,
            params.source_lang,
            params.target_lang,
            mode=params.mode,
            on_progress=context.step_callback("TRANSLATING"),
            api_key=context.api_key,
            engine_config=context.engine_config,
            cancel_check=lambda: _check_cancelled(tid),
        )
        context.resources.translated_srt_path = AssetResolver.require_translated_srt(tid)


class SubtitleBurningHandler(PipelineHandler):
    """将翻译字幕烧录到源视频并生成最终结果事件。"""

    def process(self, context: PipelineContext) -> None:
        params = context.params
        tid = context.task_id
        context.emit("BURNING", 85)
        context.resources.video_path = AssetResolver.require_source(tid)
        if context.artifact_available(AssetResolver.resolve_output_video):
            context.resources.output_video_path = AssetResolver.require_output_video(tid)
            context.emit("BURNING", 100)
        else:
            burn_subtitles(
                context.resources.video_path,
                context.resources.translated_srt_path,
                tid,
                mode=params.burn,
                on_progress=context.step_callback("BURNING"),
            )
            context.resources.output_video_path = AssetResolver.require_output_video(tid)

        outputs = {
            "video": str(context.resources.output_video_path),
            "subtitle": str(context.resources.translated_srt_path),
        }
        context.complete(outputs=outputs, title=context.title)
        logger.info("责任链完成: task=%s", tid)


def build_pipeline_chain(*handlers: PipelineHandler) -> PipelineHandler:
    """按给定顺序连接 handler，返回责任链入口。"""
    if not handlers:
        raise ValueError("责任链至少需要一个 handler")
    for current, next_handler in zip(handlers, handlers[1:]):
        current.set_next(next_handler)
    handlers[-1]._next_handler = None
    return handlers[0]


def build_default_pipeline_chain() -> PipelineHandler:
    """创建下载 → 提取 → 转写 → 翻译 → 烧录的标准任务责任链。"""
    return build_pipeline_chain(
        DownloadHandler(),
        AudioExtractionHandler(),
        TranscriptionHandler(),
        TranslationHandler(),
        SubtitleBurningHandler(),
    )


def run_pipeline(
    params: PipelineParams,
    on_event: EventHook,
    *,
    api_key: Optional[str] = None,
    engine_config=None,
    handler_chain: Optional[PipelineHandler] = None,
) -> PipelineEvent:
    """执行任务责任链，并按已有产物从最近完成的阶段继续。"""
    context = PipelineContext(
        params=params,
        on_event=on_event,
        api_key=api_key,
        engine_config=engine_config,
    )
    _check_cancelled(params.task_id)

    try:
        chain = handler_chain if handler_chain is not None else build_default_pipeline_chain()
        return chain.handle(context)

    except PipelineCancelledError:
        logger.info("责任链已被用户取消: task=%s", params.task_id)
        AssetResolver.cleanup_cancelled_artifacts(
            params.task_id,
            current_step=context.current_step,
            source_type=params.source_type,
        )
        raise
    except ResourceError as exc:
        if is_cancelled_signal(params.task_id):
            logger.info("责任链已被用户取消: task=%s", params.task_id)
            AssetResolver.cleanup_cancelled_artifacts(
                params.task_id,
                current_step=context.current_step,
                source_type=params.source_type,
            )
            raise PipelineCancelledError("任务已被用户取消") from exc
        logger.error(
            "责任链因资源异常中断: task=%s step=%s, msg=%s",
            params.task_id,
            context.current_step,
            str(exc),
        )
        on_event(PipelineEvent(
            status="FAILED",
            progress=context.progress,
            current_step=context.current_step,
            error=str(exc),
            error_code=getattr(exc, "code", "resource_error"),
        ))
        raise
    except Exception as exc:
        if is_cancelled_signal(params.task_id):
            logger.info("责任链已被用户取消: task=%s", params.task_id)
            AssetResolver.cleanup_cancelled_artifacts(
                params.task_id,
                current_step=context.current_step,
                source_type=params.source_type,
            )
            raise PipelineCancelledError("任务已被用户取消") from exc
        logger.exception("责任链失败: task=%s step=%s", params.task_id, context.current_step)
        on_event(PipelineEvent(
            status="FAILED",
            progress=context.progress,
            current_step=context.current_step,
            error=str(exc),
            error_code=_error_code_for_exception(exc),
        ))
        raise


def _locate_uploaded_source(task_id: str) -> Path:
    """定位上传模式下预先落盘的源视频 data/{task_id}/source.*。"""
    try:
        return AssetResolver.require_source(task_id)
    except ResourceError as e:
        raise PipelineError(str(e)) from e
