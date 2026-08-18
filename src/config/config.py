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


def _env_path(key: str, default: Path) -> Path:
    val = os.getenv(key)
    return Path(val).expanduser() if val else default


def _opt_env_path(key: str) -> Optional[Path]:
    val = os.getenv(key)
    return Path(val).expanduser() if val else None


def _env_list(key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    """读取逗号分隔的环境变量列表，空值回退到默认列表。"""
    val = os.getenv(key)
    if not val:
        return default
    items = tuple(item.strip() for item in val.split(",") if item.strip())
    return items or default


def _env_json_dict(key: str, default: dict[str, str]) -> dict[str, str]:
    """读取 JSON 字典格式的环境变量，合并到默认字典。非合法 JSON 则使用默认字典。"""
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
    # 后端根目录
    backend_dir: Path = _BACKEND_DIR

    # API 鉴权 Token（可选，配置后请求需带 Authorization: Bearer <token> 或 X-API-Token）
    api_token: Optional[str] = os.getenv("SUBTRANS_API_TOKEN") or None

    # 所有任务产物的根目录，按 data/{task_id}/ 组织
    data_dir: Path = _env_path("SUBTRANS_DATA_DIR", _BACKEND_DIR / "data")

    # SQLite 任务库文件
    db_path: Path = _env_path("SUBTRANS_DB", _BACKEND_DIR / "app.db")

    # 后台流水线并发数（方案 A：线程池）
    pipeline_workers: int = int(os.getenv("SUBTRANS_WORKERS", "2"))

    # 允许访问本地 API 的前端来源，逗号分隔覆盖
    cors_allow_origins: tuple[str, ...] = _env_list(
        "SUBTRANS_CORS_ORIGINS",
        _DEFAULT_CORS_ORIGINS,
    )

    # yt-dlp 格式选择：优先最佳视频+音频，回退到单一最佳流
    download_format: str = os.getenv("SUBTRANS_DL_FORMAT", "bv*+ba/b")

    # 合并后的容器格式
    merge_output_format: str = os.getenv("SUBTRANS_DL_CONTAINER", "mp4")

    # 部分站点需要 cookies 通过年龄校验 / 登录，可选
    cookies_file: Optional[Path] = _opt_env_path("SUBTRANS_COOKIES")

    # 下载失败重试次数
    download_retries: int = int(os.getenv("SUBTRANS_DL_RETRIES", "3"))

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
    # Replicate 推理超时（含冷启动，最长可能几分钟）
    replicate_timeout: int = int(os.getenv("SUBTRANS_REPLICATE_TIMEOUT", "1800"))
    # Replicate 超时/网络错误的重试次数（冷启动常见）
    replicate_retries: int = int(os.getenv("SUBTRANS_REPLICATE_RETRIES", "3"))

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
