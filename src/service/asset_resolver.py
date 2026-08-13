from __future__ import annotations

import logging
import os
from enum import Enum
from pathlib import Path
from typing import Optional, Tuple

from src.config import (
    AUDIO_FILENAME,
    OUTPUT_VIDEO,
    SOURCE_VIDEO_STEM,
    TRANSLATED_SRT,
    ORIGINAL_SRT,
    task_dir,
)

logger = logging.getLogger(__name__)


class ResourceState(str, Enum):
    AVAILABLE = "AVAILABLE"
    DELETED = "DELETED"
    UNREADABLE = "UNREADABLE"


class ResourceError(RuntimeError):
    """资源完整性校验或依赖中断异常"""
    def __init__(self, message: str, state: ResourceState):
        super().__init__(message)
        self.state = state


class AssetResolver:
    @staticmethod
    def check_file_state(path: Path) -> ResourceState:
        """检查物理文件的完整性状态。"""
        if not path.exists():
            return ResourceState.DELETED
        if not path.is_file():
            return ResourceState.UNREADABLE
        try:
            if path.stat().st_size == 0:
                return ResourceState.UNREADABLE
        except OSError:
            return ResourceState.UNREADABLE

        # Read permission check
        if not os.access(path, os.R_OK):
            return ResourceState.UNREADABLE

        return ResourceState.AVAILABLE

    @classmethod
    def resolve_source(cls, task_id: str) -> Tuple[ResourceState, Optional[Path], str]:
        """解析 source.*，返回 (状态, 路径, 友好错误消息)"""
        d = task_dir(task_id)
        if not d.is_dir():
            return ResourceState.DELETED, None, "源视频文件缺失，资源已删除"

        source_paths = sorted(d.glob(f"{SOURCE_VIDEO_STEM}.*"))
        if not source_paths:
            return ResourceState.DELETED, None, "源视频文件缺失，资源已删除"

        p = source_paths[0]
        state = cls.check_file_state(p)
        if state == ResourceState.DELETED:
            return ResourceState.DELETED, None, "源视频文件缺失，资源已删除"
        elif state == ResourceState.UNREADABLE:
            return ResourceState.UNREADABLE, p, f"源视频文件损坏或不可读: {p.name}"

        return ResourceState.AVAILABLE, p, ""

    @classmethod
    def resolve_audio(cls, task_id: str) -> Tuple[ResourceState, Optional[Path], str]:
        """解析 audio.wav"""
        d = task_dir(task_id)
        p = d / AUDIO_FILENAME
        state = cls.check_file_state(p)
        if state == ResourceState.DELETED:
            return ResourceState.DELETED, None, "音频文件缺失，资源已删除"
        elif state == ResourceState.UNREADABLE:
            return ResourceState.UNREADABLE, p, f"音频文件损坏或不可读: {AUDIO_FILENAME}"
        return ResourceState.AVAILABLE, p, ""

    @classmethod
    def resolve_original_srt(cls, task_id: str) -> Tuple[ResourceState, Optional[Path], str]:
        """解析 original.srt"""
        d = task_dir(task_id)
        p = d / ORIGINAL_SRT
        state = cls.check_file_state(p)
        if state == ResourceState.DELETED:
            return ResourceState.DELETED, None, "原文字幕文件缺失，资源已删除"
        elif state == ResourceState.UNREADABLE:
            return ResourceState.UNREADABLE, p, f"原文字幕文件损坏或不可读: {ORIGINAL_SRT}"
        return ResourceState.AVAILABLE, p, ""

    @classmethod
    def resolve_translated_srt(cls, task_id: str) -> Tuple[ResourceState, Optional[Path], str]:
        """解析 translated.srt"""
        d = task_dir(task_id)
        p = d / TRANSLATED_SRT
        state = cls.check_file_state(p)
        if state == ResourceState.DELETED:
            return ResourceState.DELETED, None, "译文字幕文件缺失，资源已删除"
        elif state == ResourceState.UNREADABLE:
            return ResourceState.UNREADABLE, p, f"译文字幕文件损坏或不可读: {TRANSLATED_SRT}"
        return ResourceState.AVAILABLE, p, ""

    @classmethod
    def resolve_output_video(cls, task_id: str) -> Tuple[ResourceState, Optional[Path], str]:
        """解析 output.mp4"""
        d = task_dir(task_id)
        p = d / OUTPUT_VIDEO
        state = cls.check_file_state(p)
        if state == ResourceState.DELETED:
            return ResourceState.DELETED, None, "成品视频文件缺失，资源已删除"
        elif state == ResourceState.UNREADABLE:
            return ResourceState.UNREADABLE, p, f"成品视频文件损坏或不可读: {OUTPUT_VIDEO}"
        return ResourceState.AVAILABLE, p, ""

    @classmethod
    def require_source(cls, task_id: str) -> Path:
        state, path, msg = cls.resolve_source(task_id)
        if state != ResourceState.AVAILABLE or path is None:
            raise ResourceError(msg, state)
        return path

    @classmethod
    def require_audio(cls, task_id: str) -> Path:
        state, path, msg = cls.resolve_audio(task_id)
        if state != ResourceState.AVAILABLE or path is None:
            raise ResourceError(msg, state)
        return path

    @classmethod
    def require_original_srt(cls, task_id: str) -> Path:
        state, path, msg = cls.resolve_original_srt(task_id)
        if state != ResourceState.AVAILABLE or path is None:
            raise ResourceError(msg, state)
        return path

    @classmethod
    def require_translated_srt(cls, task_id: str) -> Path:
        state, path, msg = cls.resolve_translated_srt(task_id)
        if state != ResourceState.AVAILABLE or path is None:
            raise ResourceError(msg, state)
        return path

    @classmethod
    def require_output_video(cls, task_id: str) -> Path:
        state, path, msg = cls.resolve_output_video(task_id)
        if state != ResourceState.AVAILABLE or path is None:
            raise ResourceError(msg, state)
        return path
