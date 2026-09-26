"""配置层：全局设置与存储路径策略。"""

from .config import get_runtime_settings, settings, update_runtime_settings
from .storage import (
    artifacts_present,
    ensure_task_dir,
    task_dir,
    SOURCE_VIDEO_STEM,
    AUDIO_FILENAME,
    ORIGINAL_SRT,
    TRANSLATED_SRT,
    OUTPUT_VIDEO,
)

__all__ = [
    "settings",
    "get_runtime_settings",
    "update_runtime_settings",
    "artifacts_present",
    "ensure_task_dir",
    "task_dir",
    "SOURCE_VIDEO_STEM",
    "AUDIO_FILENAME",
    "ORIGINAL_SRT",
    "TRANSLATED_SRT",
    "OUTPUT_VIDEO",
]
