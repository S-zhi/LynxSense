"""全局配置。

支持通过环境变量覆盖，方便在不同机器 / 部署环境调整，
本机开发直接用默认值即可。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

DEFAULT_LANG_NAMES: dict[str, str] = {
    "auto": "the source language",
    "zh-CN": "Simplified Chinese (简体中文)",
    "zh-TW": "Traditional Chinese (繁體中文)",
    "zh": "Chinese (中文)",
    "en": "English",
    "ja": "Japanese (日本語)",
    "ko": "Korean (한국어)",
    "es": "Spanish (Español)",
    "fr": "French (Français)",
    "de": "German (Deutsch)",
    "ru": "Russian (Русский)",
    "it": "Italian (Italiano)",
    "pt": "Portuguese (Português)",
    "vi": "Vietnamese (Tiếng Việt)",
    "th": "Thai (ไทย)",
    "ar": "Arabic (العربية)",
    "id": "Indonesian (Bahasa Indonesia)",
    "hi": "Hindi (हिन्दी)",
    "nl": "Dutch (Nederlands)",
    "pl": "Polish (Polski)",
    "tr": "Turkish (Türkçe)",
    "sv": "Swedish (Svenska)",
    "uk": "Ukrainian (Українська)",
    "cs": "Czech (Čeština)",
    "da": "Danish (Dansk)",
    "fi": "Finnish (Suomi)",
    "el": "Greek (Ελληνικά)",
    "he": "Hebrew (עבריت)",
    "hu": "Hungarian (Magyar)",
    "no": "Norwegian (Norsk)",
    "ro": "Romanian (Română)",
    "sk": "Slovak (Slovenčina)",
    "af": "Afrikaans",
    "ca": "Catalan (Català)",
    "bg": "Bulgarian (Български)",
    "hr": "Croatian (Hrvatski)",
    "ms": "Malay (Bahasa Melayu)",
    "fa": "Persian (فارسی)",
    "ur": "Urdu (اردو)",
    "bn": "Bengali (বাংলা)",
    "ta": "Tamil (தமிழ்)",
    "sw": "Swahili (Kiswahili)",
}

# 项目根目录（本文件位于 src/config/config.py，向上两级）
_BACKEND_DIR = Path(__file__).resolve().parents[2]
_DEFAULT_CORS_ORIGINS = (
    "http://localhost:5273", "http://127.0.0.1:5273",
    "http://localhost:8000", "http://127.0.0.1:8000",
)
_DEFAULT_TARGET_LANGUAGES = (
    "zh-CN", "zh-TW", "en", "ja", "ko", "es", "fr", "de", "ru", "it",
    "pt", "vi", "th", "ar", "id", "hi", "nl", "pl", "tr", "sv",
    "uk", "cs", "da", "fi", "el", "he", "hu", "no", "ro", "sk",
    "af", "ca", "bg", "hr", "ms", "fa", "ur", "bn", "ta", "sw",
)


def _bootstrap_env() -> None:
    """在读取任何环境变量前，先把项目根 .env 加载进来。

    必须在 class Settings 定义之前调用：dataclass 的字段默认值
    （os.getenv(...)）是在类定义期求值的。这样 uvicorn / pytest / CLI
    各入口都能拿到 .env 里的 key，无需各自再 load。
    setdefault 语义：不覆盖外部已设置的环境变量。
    """
    env_path = _BACKEND_DIR / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and val:
            os.environ.setdefault(key, val)


_bootstrap_env()

_last_env_mtime: float = -1.0


def _env_int(key: str, default: int) -> int:
    """读取可热更新的整数配置，非法值回退默认值。"""
    _sync_env_file()
    try:
        return int(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(key: str, default: float) -> float:
    """读取可热更新的浮点配置，非法值回退默认值。"""
    _sync_env_file()
    try:
        return float(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        return default


def _env_str(key: str, default: str) -> str:
    _sync_env_file()
    return os.getenv(key, default)


def _read_dynamic_setting(name: str):
    """按字段名读取支持运行期更新的环境配置。"""
    if name == "download_format":
        return _env_str("SUBTRANS_DL_FORMAT", "bv*[height<=480]+ba/b[height<=480]")
    if name == "merge_output_format":
        return _env_str("SUBTRANS_DL_CONTAINER", "mp4")
    if name == "cookies_file":
        return _opt_env_path("SUBTRANS_COOKIES")
    if name == "download_retries":
        return max(0, _env_int("SUBTRANS_DL_RETRIES", 3))
    if name == "max_upload_mb":
        return max(1, _env_int("SUBTRANS_MAX_UPLOAD_MB", 2048))
    if name == "max_video_minutes":
        return max(1, _env_int("SUBTRANS_MAX_VIDEO_MINUTES", 180))
    if name == "ffmpeg_bin":
        return _env_str("SUBTRANS_FFMPEG", "ffmpeg")
    if name == "ffprobe_bin":
        return _env_str("SUBTRANS_FFPROBE", "ffprobe")
    if name == "audio_sample_rate":
        return max(1, _env_int("SUBTRANS_AUDIO_SR", 16000))
    if name == "audio_channels":
        return max(1, _env_int("SUBTRANS_AUDIO_CH", 1))
    if name == "replicate_whisper_model":
        return _env_str(
            "SUBTRANS_WHISPER_MODEL",
            "stayallive/whisper-subtitles:b97ba81004e7132181864c885a76cae0e56bc61caa4190a395f6d8ba45b7a969",
        )
    if name == "replicate_timeout":
        return max(1, _env_int("SUBTRANS_REPLICATE_TIMEOUT", 1800))
    if name == "replicate_retries":
        return max(1, _env_int("SUBTRANS_REPLICATE_RETRIES", 3))
    if name == "replicate_retry_interval":
        return max(0.0, _env_float("SUBTRANS_REPLICATE_RETRY_INTERVAL", 3600.0))
    if name == "replicate_poll_interval":
        return max(0.0, _env_float("SUBTRANS_REPLICATE_POLL_INTERVAL", 30.0))
    if name == "deepseek_api_key":
        _sync_env_file()
        return os.getenv("SUBTRANS_DEEPSEEK_API_KEY") or os.getenv("DEEPSEEK_API_KEY")
    if name == "deepseek_base_url":
        return _env_str("SUBTRANS_DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    if name == "deepseek_model":
        return _env_str("SUBTRANS_DEEPSEEK_MODEL", "deepseek-chat")
    if name == "translate_batch_size":
        return max(1, _env_int("SUBTRANS_TRANSLATE_BATCH", 8))
    if name == "translate_timeout":
        return max(1, _env_int("SUBTRANS_TRANSLATE_TIMEOUT", 60))
    if name == "target_languages":
        return _env_list("SUBTRANS_TARGET_LANGUAGES", _DEFAULT_TARGET_LANGUAGES)
    if name == "lang_names":
        return _env_json_dict("SUBTRANS_LANG_NAMES", DEFAULT_LANG_NAMES)
    raise AttributeError(name)


def _sync_env_file() -> None:
    """如果 .env 文件存在且被修改，重新同步环境变量到 os.environ。"""
    global _last_env_mtime
    env_path = _BACKEND_DIR / ".env"
    if not env_path.exists():
        return
    try:
        mtime = env_path.stat().st_mtime
        if mtime != _last_env_mtime:
            _last_env_mtime = mtime
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key, val = key.strip(), val.strip().strip('"').strip("'")
                if key and val:
                    os.environ[key] = val
    except Exception:
        pass


def _env_path(key: str, default: Path) -> Path:
    _sync_env_file()
    val = os.getenv(key)
    return Path(val).expanduser() if val else default


def _opt_env_path(key: str) -> Optional[Path]:
    _sync_env_file()
    val = os.getenv(key)
    return Path(val).expanduser() if val else None


def _env_list(key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    """读取逗号分隔的环境变量列表，空值回退到默认列表。"""
    _sync_env_file()
    val = os.getenv(key)
    if not val:
        return default
    items = tuple(item.strip() for item in val.split(",") if item.strip())
    return items or default


def _env_json_dict(key: str, default: dict[str, str]) -> dict[str, str]:
    """读取 JSON 字典格式的环境变量，合并到默认字典。非合法 JSON 则使用默认字典。"""
    _sync_env_file()
    val = os.getenv(key)
    if not val:
        return default
    try:
        data = json.loads(val)
        if isinstance(data, dict):
            merged = dict(default)
            merged.update({str(k): str(v) for k, v in data.items()})
            return merged
    except Exception:
        pass
    return default


@dataclass(frozen=True)
class Settings:
    # 这些字段保留 dataclass 构造参数，兼容测试/调用方通过 dataclasses.replace
    # 注入临时配置；正常实例读取时由 __getattribute__ 重新读取环境变量。
    _DYNAMIC_ENV_FIELDS = frozenset({
        "download_format", "merge_output_format", "cookies_file", "download_retries",
        "max_upload_mb", "max_video_minutes", "ffmpeg_bin", "ffprobe_bin",
        "audio_sample_rate", "audio_channels", "replicate_whisper_model",
        "replicate_timeout", "replicate_retries", "replicate_retry_interval",
        "replicate_poll_interval", "deepseek_api_key", "deepseek_base_url",
        "deepseek_model", "translate_batch_size", "translate_timeout",
        "target_languages", "lang_names",
    })
    _dynamic_env_overrides: frozenset[str] = field(
        init=False, default_factory=frozenset, repr=False, compare=False
    )

    def __post_init__(self):
        # dataclasses.replace() 可能显式注入临时值；记录它们，避免被环境
        # 动态读取覆盖。默认实例（包括启动时 .env 已有值）仍保持热更新。
        overrides = set()
        for name in self._DYNAMIC_ENV_FIELDS:
            stored = object.__getattribute__(self, name)
            if stored != _read_dynamic_setting(name):
                overrides.add(name)
        object.__setattr__(self, "_dynamic_env_overrides", frozenset(overrides))

    def __getattribute__(self, name: str):
        """让尚未显式覆盖的环境配置在每次访问时生效。

        dataclass 字段不能直接改成 property，否则会破坏现有的
        dataclasses.replace(Settings(...), ...) 调用。这里保留字段 API，
        仅对默认字段做动态解析；显式传入的字段仍作为实例覆盖值。
        """
        if name in type(self)._DYNAMIC_ENV_FIELDS:
            stored = object.__getattribute__(self, name)
            if name in object.__getattribute__(self, "_dynamic_env_overrides"):
                return stored
            return _read_dynamic_setting(name)
        return object.__getattribute__(self, name)

    # 后端根目录
    backend_dir: Path = _BACKEND_DIR

    # 所有任务产物的根目录，按 data/{task_id}/ 组织
    data_dir: Path = _env_path("SUBTRANS_DATA_DIR", _BACKEND_DIR / "data")

    # SQLite 任务库文件
    db_path: Path = _env_path("SUBTRANS_DB", _BACKEND_DIR / "app.db")

    # 后台流水线并发数覆盖（None 表示动态读取 SUBTRANS_WORKERS 或 .env）
    _pipeline_workers_override: Optional[int] = field(default=None, repr=False)

    # 下载阶段并发数覆盖（None 表示动态读取 SUBTRANS_DOWNLOAD_WORKERS 或 .env）
    _download_workers_override: Optional[int] = field(default=None, repr=False)

    # API 鉴权 Token（动态读取 SUBTRANS_API_TOKEN 或编辑 .env）
    @property
    def api_token(self) -> Optional[str]:
        _sync_env_file()
        val = (os.getenv("SUBTRANS_API_TOKEN") or "").strip()
        return val if val else None

    # 后台流水线并发数（方案 A：线程池，动态读取，支持运行期调整 SUBTRANS_WORKERS 或编辑 .env）
    @property
    def pipeline_workers(self) -> int:
        if self._pipeline_workers_override is not None:
            return max(1, self._pipeline_workers_override)
        _sync_env_file()
        val = os.getenv("SUBTRANS_WORKERS", "8")
        try:
            return max(1, int(val))
        except (ValueError, TypeError):
            return 8

    # 下载阶段并发数：只限制 yt-dlp 真正下载媒体的阶段。
    # 流水线线程池可以大于这个值，使下载结束后的 SRT/烧录阶段不占下载名额。
    @property
    def download_workers(self) -> int:
        if self._download_workers_override is not None:
            return max(1, self._download_workers_override)
        _sync_env_file()
        val = os.getenv("SUBTRANS_DOWNLOAD_WORKERS", "2")
        try:
            return max(1, int(val))
        except (ValueError, TypeError):
            return 2

    # HLS/DASH 单个媒体任务的分片并发数；yt-dlp 默认值为 1。
    @property
    def download_concurrent_fragments(self) -> int:
        _sync_env_file()
        val = os.getenv("SUBTRANS_DL_CONCURRENT_FRAGMENTS", "4")
        try:
            return max(1, int(val))
        except (ValueError, TypeError):
            return 4

    # SSE 进度流连接超时上限（秒），默认 7200 秒（2 小时）
    stream_timeout_sec: int = int(os.getenv("SUBTRANS_STREAM_TIMEOUT_SEC", "7200"))

    # Readiness / Replicate 余额检查 TTL 缓存时长（秒），默认 60 秒
    readiness_ttl_sec: int = int(os.getenv("SUBTRANS_READINESS_TTL_SEC", "60"))

    # 视频探测 network TTL 缓存时长（秒），默认 300 秒（5 分钟）
    probe_cache_ttl_sec: int = int(os.getenv("SUBTRANS_PROBE_CACHE_TTL_SEC", "300"))

    # 允许访问本地 API 的前端来源，逗号分隔覆盖
    cors_allow_origins: tuple[str, ...] = _env_list(
        "SUBTRANS_CORS_ORIGINS",
        _DEFAULT_CORS_ORIGINS,
    )

    # yt-dlp 格式选择：最高 480P，优先最佳视频+音频，回退到单一最佳流
    download_format: str = os.getenv(
        "SUBTRANS_DL_FORMAT",
        "bv*[height<=480]+ba/b[height<=480]",
    )

    # 合并后的容器格式
    merge_output_format: str = os.getenv("SUBTRANS_DL_CONTAINER", "mp4")

    # 部分站点需要 cookies 通过年龄校验 / 登录，可选
    cookies_file: Optional[Path] = _opt_env_path("SUBTRANS_COOKIES")

    # 下载失败重试次数
    download_retries: int = int(os.getenv("SUBTRANS_DL_RETRIES", "3"))

    # 上传限制配置
    max_upload_mb: int = int(os.getenv("SUBTRANS_MAX_UPLOAD_MB", "2048"))
    max_video_minutes: int = int(os.getenv("SUBTRANS_MAX_VIDEO_MINUTES", "180"))

    # ffmpeg / ffprobe 可执行文件（默认走 PATH）
    ffmpeg_bin: str = os.getenv("SUBTRANS_FFMPEG", "ffmpeg")
    ffprobe_bin: str = os.getenv("SUBTRANS_FFPROBE", "ffprobe")

    # 提取音频的采样率与声道：16kHz 单声道是 Whisper 的标准输入
    audio_sample_rate: int = int(os.getenv("SUBTRANS_AUDIO_SR", "16000"))
    audio_channels: int = int(os.getenv("SUBTRANS_AUDIO_CH", "1"))

    # --- ③ 语音识别（Replicate-hosted Whisper）---
    # Replicate 模型标识（版本锁定）
    replicate_whisper_model: str = os.getenv(
        "SUBTRANS_WHISPER_MODEL",
        "stayallive/whisper-subtitles:b97ba81004e7132181864c885a76cae0e56bc61caa4190a395f6d8ba45b7a969",
    )
    # Replicate 单次 HTTP 请求超时；prediction 的排队/运行通过短轮询跟踪
    replicate_timeout: int = int(os.getenv("SUBTRANS_REPLICATE_TIMEOUT", "1800"))
    # Replicate 创建/状态查询发生网络错误时的总尝试次数
    replicate_retries: int = int(os.getenv("SUBTRANS_REPLICATE_RETRIES", "3"))
    # 网络错误后再次请求的间隔；默认 1 小时，避免重复创建长时间排队的任务
    replicate_retry_interval: float = float(
        os.getenv("SUBTRANS_REPLICATE_RETRY_INTERVAL", "3600")
    )
    # 已取得 prediction ID 后的状态轮询间隔；轮询不会创建新任务
    replicate_poll_interval: float = float(
        os.getenv("SUBTRANS_REPLICATE_POLL_INTERVAL", "30")
    )

    # --- ④ 翻译（旧版 DeepSeek 兼容配置；新配置位于 SQLite）---
    deepseek_api_key: Optional[str] = (
        os.getenv("SUBTRANS_DEEPSEEK_API_KEY") or os.getenv("DEEPSEEK_API_KEY")
    )
    deepseek_base_url: str = os.getenv("SUBTRANS_DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    deepseek_model: str = os.getenv("SUBTRANS_DEEPSEEK_MODEL", "deepseek-chat")
    # 每批翻译多少条字幕（太长模型可能截断 JSON，自动减半重试）
    translate_batch_size: int = int(os.getenv("SUBTRANS_TRANSLATE_BATCH", "8"))
    translate_timeout: int = int(os.getenv("SUBTRANS_TRANSLATE_TIMEOUT", "60"))

    # 支持的翻译目标语言列表（逗号分隔）
    target_languages: tuple[str, ...] = _env_list(
        "SUBTRANS_TARGET_LANGUAGES",
        (
            "zh-CN", "zh-TW", "en", "ja", "ko", "es", "fr", "de", "ru", "it",
            "pt", "vi", "th", "ar", "id", "hi", "nl", "pl", "tr", "sv",
            "uk", "cs", "da", "fi", "el", "he", "hu", "no", "ro", "sk",
            "af", "ca", "bg", "hr", "ms", "fa", "ur", "bn", "ta", "sw",
        ),
    )

    # 语言代码到名称的映射字典，可由 SUBTRANS_LANG_NAMES 环境变量（JSON 字符串）覆盖/追加
    lang_names: dict[str, str] = field(
        default_factory=lambda: _env_json_dict(
            "SUBTRANS_LANG_NAMES",
            DEFAULT_LANG_NAMES,
        )
    )


settings = Settings()
