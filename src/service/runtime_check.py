"""运行时就绪检查。

该模块只返回脱敏后的能力状态，供健康检查和 MCP 适配层使用；不会返回任何
API Key 的实际内容。
"""

from __future__ import annotations

import importlib.util
import os
import shutil
from pathlib import Path
from typing import Any

from src.config import settings
from src.core.ffmpeg_utils import has_subtitles_filter


def _check_binary(command: str) -> str:
    """返回可执行文件状态，不暴露命令行参数或环境变量。"""
    return "available" if shutil.which(command) else "missing"


def _check_writable_directory(path: Path) -> str:
    """确保目录存在并检查当前进程是否可写。"""
    try:
        path.mkdir(parents=True, exist_ok=True)
        return "writable" if os.access(path, os.W_OK) else "not_writable"
    except OSError:
        return "unavailable"


def _has_value(value: str | None) -> bool:
    return bool(value and value.strip())


def build_readiness() -> dict[str, Any]:
    """构造业务服务的脱敏 readiness 响应。

    ``ok`` 表示默认的完整流水线（含硬字幕）所需的基础环境是否可用。
    下载能力和硬字幕能力单独暴露，便于 MCP 给出精确的下一步提示。
    """
    env_file = settings.backend_dir / ".env"

    replicate_ready = _has_value(os.getenv("REPLICATE_API_TOKEN"))
    deepseek_ready = _has_value(
        os.getenv("SUBTRANS_DEEPSEEK_API_KEY")
        or os.getenv("DEEPSEEK_API_KEY")
        or settings.deepseek_api_key
    )
    ffmpeg_status = _check_binary(settings.ffmpeg_bin)
    ffprobe_status = _check_binary(settings.ffprobe_bin)
    yt_dlp_status = "available" if importlib.util.find_spec("yt_dlp") else "missing"
    data_dir_status = _check_writable_directory(settings.data_dir)
    db_dir_status = _check_writable_directory(settings.db_path.parent)

    hard_burn_ready = (
        ffmpeg_status == "available"
        and has_subtitles_filter(settings.ffmpeg_bin)
    )

    download_ready = (
        ffmpeg_status == "available"
        and ffprobe_status == "available"
        and yt_dlp_status == "available"
        and data_dir_status == "writable"
        and db_dir_status == "writable"
    )
    full_pipeline_ready = download_ready and replicate_ready and deepseek_ready
    hard_pipeline_ready = full_pipeline_ready and hard_burn_ready

    missing: list[str] = []
    if not replicate_ready:
        missing.append("REPLICATE_API_TOKEN")
    if not deepseek_ready:
        missing.append("SUBTRANS_DEEPSEEK_API_KEY 或 DEEPSEEK_API_KEY")
    if ffmpeg_status != "available":
        missing.append("ffmpeg")
    if ffprobe_status != "available":
        missing.append("ffprobe")
    if yt_dlp_status != "available":
        missing.append("yt-dlp")
    if data_dir_status != "writable":
        missing.append("SUBTRANS_DATA_DIR 可写权限")
    if db_dir_status != "writable":
        missing.append("SUBTRANS_DB 所在目录可写权限")

    if not full_pipeline_ready:
        message = (
            "业务服务尚未完成初始化。请在项目根目录的 .env 中配置必要参数，"
            "并确认 FFmpeg、FFprobe 和 yt-dlp 可用，然后重启业务服务。"
        )
    elif not hard_burn_ready:
        message = (
            "基础流水线已就绪，但当前 FFmpeg 不支持硬字幕滤镜；"
            "请安装带 libass 的 ffmpeg-full，或将 burn 设置为 soft。"
        )
    else:
        message = "业务服务已就绪，可以运行完整字幕流水线。"

    return {
        "ok": hard_pipeline_ready,
        "initialized": full_pipeline_ready,
        "config_file": str(env_file),
        "config_file_present": env_file.is_file(),
        "required_environment": [
            "REPLICATE_API_TOKEN",
            "SUBTRANS_DEEPSEEK_API_KEY 或 DEEPSEEK_API_KEY",
        ],
        "checks": {
            "replicate_api_token": "available" if replicate_ready else "missing",
            "deepseek_api_key": "available" if deepseek_ready else "missing",
            "ffmpeg": ffmpeg_status,
            "ffprobe": ffprobe_status,
            "yt_dlp": yt_dlp_status,
            "data_directory": data_dir_status,
            "database_directory": db_dir_status,
            "subtitle_filter": "available" if hard_burn_ready else "missing",
        },
        "capabilities": {
            "download": download_ready,
            "full_pipeline": full_pipeline_ready,
            "hard_burn": hard_burn_ready,
            "soft_burn": full_pipeline_ready,
        },
        "missing": missing,
        "message": message,
    }
