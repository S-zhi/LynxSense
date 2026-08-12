"""配置层：全局设置与存储路径策略。"""

from .config import settings
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
    "artifacts_present",
    "ensure_task_dir",
    "task_dir",
    "SOURCE_VIDEO_STEM",
    "AUDIO_FILENAME",
    "ORIGINAL_SRT",
    "TRANSLATED_SRT",
    "OUTPUT_VIDEO",
]
