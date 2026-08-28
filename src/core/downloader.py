"""流水线第①步：下载视频。

输入：视频页面 URL + 任务 ID
输出：DownloadResult（其中 video_path 指向 data/{task_id}/source.mp4）

基于 yt-dlp，合并最佳音视频流为单个 mp4。通过 on_progress 回调把下载进度
往外透传，供编排层（orchestrator）转成 SSE 推给前端。

依赖：yt-dlp（Python 包）、ffmpeg（合并音视频流，需在 PATH 中）。
"""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError as YtDlpDownloadError

from src.config import settings, ensure_task_dir, SOURCE_VIDEO_STEM

logger = logging.getLogger(__name__)


class DownloadError(RuntimeError):
    """下载阶段失败。包装底层异常，向上层提供统一的错误类型。"""


@dataclass
class DownloadProgress:
    """单次进度回调的数据。percent 为 0-100；总大小未知时 total_bytes 为 None。"""

    status: str  # "downloading" | "finished"
    percent: float
    downloaded_bytes: int
    total_bytes: Optional[int]
    speed: Optional[float]  # 字节/秒
    eta: Optional[int]      # 预计剩余秒数


@dataclass
class DownloadResult:
    """下载结果。"""

    video_path: Path
    title: str
    duration: Optional[float]   # 秒
    ext: str
    filesize: Optional[int]     # 字节
    width: Optional[int]
    height: Optional[int]
    source_url: str


@dataclass
class ProbeResult:
    """视频链接探测结果，不下载媒体文件。"""

    ok: bool
    title: Optional[str] = None
    extractor: Optional[str] = None
    duration: Optional[float] = None
    formats_count: int = 0
    webpage_url: Optional[str] = None
    reason: Optional[str] = None
    detail: Optional[str] = None
    cached: bool = False
    language: Optional[str] = None

_PROBE_CACHE_MAX_SIZE = 1000
_probe_cache_lock = threading.Lock()
_probe_cache: OrderedDict[tuple[str, str, str], tuple[float, ProbeResult]] = OrderedDict()


def clear_probe_cache() -> None:
    """清空视频探测结果缓存。"""
    with _probe_cache_lock:
        _probe_cache.clear()

# 视频下载回调函数 回调函数逻辑 提供修改进度的展示函数 -> 放入执行流程中的hook内
ProgressHook = Callable[[DownloadProgress], None]


class _DownloadConcurrencyLimiter:
    """只限制媒体下载阶段的并发，不占用后续字幕处理的下载名额。

    使用条件变量而不是固定大小的 ``Semaphore``，这样运行中修改
    ``SUBTRANS_DOWNLOAD_WORKERS`` 后，新任务会使用最新配置；已经运行中的下载
    不会被强行打断。每次 ``slot`` 退出都会释放名额，即使 yt-dlp 抛出异常。
    """

    def __init__(self, limit_getter: Callable[[], int]):
        self._limit_getter = limit_getter
        self._condition = threading.Condition()
        self._active = 0

    @property
    def active(self) -> int:
        with self._condition:
            return self._active

    def _limit(self) -> int:
        try:
            return max(1, int(self._limit_getter()))
        except (TypeError, ValueError):
            return 2

    @contextmanager
    def slot(self):
        wait_started = time.monotonic()
        with self._condition:
            while self._active >= self._limit():
                # 周期性重读配置，支持运行中把并发上限调高。
                self._condition.wait(timeout=0.5)
            self._active += 1
        waited = time.monotonic() - wait_started

        try:
            yield waited
        finally:
            with self._condition:
                self._active -= 1
                self._condition.notify()


def _configured_download_workers() -> int:
    """读取下载阶段并发配置，并兼容测试中的简化 settings 对象。"""
    try:
        return max(1, int(getattr(settings, "download_workers", 2)))
    except (TypeError, ValueError):
        return 2


def _configured_fragment_workers() -> int:
    """读取单个 HLS/DASH 媒体任务的分片并发配置。"""
    try:
        return max(1, int(getattr(settings, "download_concurrent_fragments", 4)))
    except (TypeError, ValueError):
        return 4


_download_limiter = _DownloadConcurrencyLimiter(_configured_download_workers)

# 对钩子函数进行封装
def _make_progress_adapter(on_progress: ProgressHook):
    """把 yt-dlp 的 hook 字典转换成统一的 DownloadProgress 回调。"""

    def hook(d: dict) -> None:
        status = d.get("status")
        if status not in ("downloading", "finished"):
            return

        downloaded = d.get("downloaded_bytes") or 0
        total = d.get("total_bytes") or d.get("total_bytes_estimate")

        if status == "finished":
            percent = 100.0
        elif total:
            percent = max(0.0, min(100.0, downloaded / total * 100.0))
        else:
            percent = 0.0

        try:
            on_progress(
                DownloadProgress(
                    status=status,
                    percent=round(percent, 1),
                    downloaded_bytes=downloaded,
                    total_bytes=total,
                    speed=d.get("speed"),
                    eta=d.get("eta"),
                )
            )
        except Exception:  # 回调里的异常不应中断下载
            logger.exception("下载进度回调异常，已忽略")

    return hook


def download_video(
    url: str,
    task_id: str,
    on_progress: Optional[ProgressHook] = None,
    *,
    cookies_file: Optional[Path] = None,
    format_selector: Optional[str] = None,
) -> DownloadResult:
    """下载单个视频到 data/{task_id}/source.mp4。

    Args:
        url: 视频页面地址。
        task_id: 任务 ID，决定输出目录。
        on_progress: 可选进度回调。
        cookies_file: 可选 cookies 文件（部分站点需年龄校验 / 登录）。
        format_selector: 可选覆盖 yt-dlp 的 format 选择串。

    Returns:
        DownloadResult

    Raises:
        DownloadError: 下载或解析失败、或找不到产物文件。
    """
    out_dir = ensure_task_dir(task_id)
    # 固定基名，扩展名交给 yt-dlp / 合并器决定，最终合并为 mp4
    outtmpl = str(out_dir / f"{SOURCE_VIDEO_STEM}.%(ext)s")

    ydl_opts: dict = {
        "format": format_selector or settings.download_format,
        "merge_output_format": settings.merge_output_format,
        "outtmpl": outtmpl,
        "noplaylist": True,          # 只下单个视频，忽略播放列表
        "retries": settings.download_retries,
        # yt-dlp 的 HLS/DASH 分片默认串行；适度并发可避免单个视频被单连接吞吐限制。
        "concurrent_fragment_downloads": _configured_fragment_workers(),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,          # 关闭 yt-dlp 自带进度条，进度走我们的 hook
        "overwrites": True,          # 重跑时覆盖旧文件
    }

    cookies = cookies_file or settings.cookies_file
    if cookies:
        ydl_opts["cookiefile"] = str(cookies)

    if on_progress is not None:
        ydl_opts["progress_hooks"] = [_make_progress_adapter(on_progress)]

    logger.info("开始下载: task=%s url=%s", task_id, url)

    try:
        with _download_limiter.slot() as wait_seconds:
            started = time.monotonic()
            logger.info(
                "获得下载槽位: task=%s wait=%.2fs active=%d limit=%d",
                task_id,
                wait_seconds,
                _download_limiter.active,
                _configured_download_workers(),
            )
            try:
                with YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
            finally:
                logger.info(
                    "释放下载槽位: task=%s elapsed=%.2fs",
                    task_id,
                    time.monotonic() - started,
                )
    except YtDlpDownloadError as e:
        raise DownloadError(f"视频下载失败: {e}") from e
    except Exception as e:  # 解析 / 网络等其它错误
        raise DownloadError(f"下载过程中出错: {e}") from e

    video_path = _resolve_output_path(info, out_dir)
    if video_path is None or not video_path.exists():
        raise DownloadError("下载完成但未找到产物文件")

    result = DownloadResult(
        video_path=video_path,
        title=info.get("title") or video_path.stem,
        duration=info.get("duration"),
        ext=video_path.suffix.lstrip("."),
        filesize=_safe_filesize(video_path),
        width=info.get("width"),
        height=info.get("height"),
        source_url=url,
    )
    logger.info(
        "下载完成: task=%s file=%s (%.1f MB)",
        task_id,
        video_path.name,
        (result.filesize or 0) / 1024 / 1024,
    )
    return result


def probe_video(
    url: str,
    *,
    cookies_file: Optional[Path] = None,
    format_selector: Optional[str] = None,
    ttl_sec: Optional[float] = None,
    force_refresh: bool = False,
) -> ProbeResult:
    """探测 URL 是否能被 yt-dlp 解析并找到可下载格式，不落盘下载。

    支持 TTL 网络级缓存：在 ttl_sec 时间内对相同 URL 的重复探测将直接返回缓存，
    命中时结果带 cached=True，避免频繁撞上游风控/429。
    """
    clean_url = url.strip()
    if not _is_probe_url(clean_url):
        return ProbeResult(ok=False, reason="请输入有效的视频链接", cached=False)

    effective_ttl = ttl_sec if ttl_sec is not None else float(settings.probe_cache_ttl_sec)
    effective_cookies = cookies_file or settings.cookies_file
    effective_format = format_selector or settings.download_format
    cache_key = (clean_url, str(effective_cookies or ""), str(effective_format))
    now = time.time()

    if not force_refresh and effective_ttl > 0:
        with _probe_cache_lock:
            cached = _probe_cache.get(cache_key)
            if cached is not None:
                ts, cached_res = cached
                if now - ts < effective_ttl:
                    _probe_cache.move_to_end(cache_key)
                    return dataclasses.replace(cached_res, cached=True)
                # 删除当前过期项，避免它继续占用有界缓存空间。
                del _probe_cache[cache_key]

    ydl_opts: dict = {
        "format": effective_format,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "simulate": True,
        # 不开 check_formats：它会逐个向格式 URL 发探测请求，
        # 而部分站点（如 pornhub）签名 CDN 会拒绝这类校验请求，
        # 导致明明能下载的视频被误判为“无可用格式”。探测只需能解析出格式即可。
    }

    if effective_cookies:
        ydl_opts["cookiefile"] = str(effective_cookies)

    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(clean_url, download=False)
    except YtDlpDownloadError as e:
        res = ProbeResult(
            ok=False,
            reason=_probe_failure_reason(str(e)),
            detail=_clip_error(str(e)),
            cached=False,
        )
    except Exception as e:
        res = ProbeResult(
            ok=False,
            reason="链接探测失败",
            detail=_clip_error(str(e)),
            cached=False,
        )
    else:
        language = _sniff_language(info)
        formats_count = _count_formats(info)
        if formats_count == 0 and not info.get("url"):
            res = ProbeResult(
                ok=False,
                title=info.get("title"),
                extractor=info.get("extractor_key") or info.get("extractor"),
                duration=info.get("duration"),
                webpage_url=info.get("webpage_url") or clean_url,
                reason="未找到可下载的视频格式",
                language=language,
                cached=False,
            )
        else:
            res = ProbeResult(
                ok=True,
                title=info.get("title"),
                extractor=info.get("extractor_key") or info.get("extractor"),
                duration=info.get("duration"),
                formats_count=formats_count,
                webpage_url=info.get("webpage_url") or clean_url,
                language=language,
                cached=False,
            )

    if effective_ttl > 0:
        with _probe_cache_lock:
            _probe_cache[cache_key] = (now, res)
            _probe_cache.move_to_end(cache_key)
            while len(_probe_cache) > _PROBE_CACHE_MAX_SIZE:
                _probe_cache.popitem(last=False)

    return res


def _sniff_language(info: dict) -> Optional[str]:
    """从 yt-dlp 元数据中嗅探视频语言线索（best-effort）。"""
    lang = info.get("language")
    if isinstance(lang, str) and lang.strip():
        return _normalize_language_code(lang)
    for key in ("subtitles", "automatic_captions"):
        clues = info.get(key)
        if isinstance(clues, dict):
            for code in clues:
                if isinstance(code, str) and code.strip():
                    return _normalize_language_code(code)
    return None


def _normalize_language_code(code: str) -> str:
    c = code.replace("_", "-").strip()
    low = c.lower()
    if low in ("zh-hans", "zh-cn", "zh-sg", "chs"):
        return "zh-CN"
    if low in ("zh-hant", "zh-tw", "zh-hk", "cht"):
        return "zh-TW"
    if low.startswith("zh"):
        return "zh"
    if "-" in c:
        base = c.split("-")[0].lower()
        if len(base) == 2:
            return base
    return c.lower()


def _resolve_output_path(info: dict, out_dir: Path) -> Optional[Path]:
    """从 yt-dlp 的 info 中解析最终产物路径，带多重回退。"""
    # 1) 最可靠：合并后的 requested_downloads[].filepath
    downloads = info.get("requested_downloads")
    if downloads:
        fp = downloads[0].get("filepath")
        if fp:
            return Path(fp)

    # 2) info 顶层可能直接带 filepath
    fp = info.get("filepath")
    if fp:
        return Path(fp)

    # 3) 兜底：按固定基名在目录里找
    candidates = sorted(out_dir.glob(f"{SOURCE_VIDEO_STEM}.*"))
    # 优先合并目标容器
    target = out_dir / f"{SOURCE_VIDEO_STEM}.{settings.merge_output_format}"
    if target.exists():
        return target
    return candidates[0] if candidates else None


def _safe_filesize(path: Path) -> Optional[int]:
    """安全读取文件大小，读取失败时返回 None。"""
    try:
        return path.stat().st_size
    except OSError:
        return None


def _count_formats(info: dict) -> int:
    """统计 yt-dlp 解析结果中可见的格式数量。"""
    formats = info.get("formats")
    if isinstance(formats, list):
        return len(formats)
    requested = info.get("requested_formats")
    if isinstance(requested, list):
        return len(requested)
    return 1 if info.get("url") else 0


def _is_probe_url(url: str) -> bool:
    """检查探针输入是否是 http(s) URL。"""
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _probe_failure_reason(message: str) -> str:
    """把 yt-dlp 异常摘要归类成用户可读的失败原因。"""
    text = message.lower()
    if "unsupported url" in text:
        return "yt-dlp 暂不支持这个网站或链接"
    if "private" in text or "login" in text or "cookie" in text:
        return "该链接可能需要登录态或 cookies"
    if "no video formats" in text or "requested format is not available" in text:
        return "未找到匹配当前配置的可下载格式"
    if "not available" in text or "404" in text:
        return "视频不可用或链接已失效"
    return "yt-dlp 无法解析这个链接"


def _clip_error(message: str, limit: int = 500) -> str:
    """截断底层错误，避免接口返回过长日志。"""
    return message[:limit]
