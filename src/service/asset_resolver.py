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

        # yt-dlp 下载中会留下 source.<ext>.part；它不是可恢复流水线的完整源视频。
        source_paths = sorted(
            p for p in d.glob(f"{SOURCE_VIDEO_STEM}.*") if not p.name.endswith(".part")
        )
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
    def cleanup_download_temp_files(cls, task_id: str) -> None:
        """删除下载器临时文件，防止恢复任务复用不兼容的半截下载。

        参数:
            task_id: 任务标识，用于定位任务目录。

        返回:
            None。目录不存在或单个文件删除失败时记录日志并继续。

        副作用:
            删除任务目录中由 yt-dlp 产生的 ``.part`` 和 ``.ytdl`` 文件。
            仅供 URL 下载任务调用，不会删除正式 source 文件。
        """
        d = task_dir(task_id)
        if not d.is_dir():
            return

        for pattern in ("*.part", "*.ytdl"):
            for path in d.glob(pattern):
                try:
                    path.unlink()
                    logger.info("已清理恢复任务的下载临时文件: task=%s, file=%s", task_id, path.name)
                except OSError as e:
                    logger.warning("清理恢复任务下载临时文件失败: task=%s, file=%s, err=%s", task_id, path.name, e)

    @classmethod
    def cleanup_cancelled_artifacts(
        cls, task_id: str, current_step: Optional[str] = None, source_type: Optional[str] = None
    ) -> None:
        """清理取消任务的半截/不完整产物，防止下次重试时复用损坏或残缺的文件。"""
        d = task_dir(task_id)
        if not d.exists():
            return

        step_artifacts_map = {
            "BURNING": [OUTPUT_VIDEO],
            "TRANSLATING": [TRANSLATED_SRT, OUTPUT_VIDEO],
            "TRANSCRIBING": [ORIGINAL_SRT, TRANSLATED_SRT, OUTPUT_VIDEO],
            "EXTRACTING": [AUDIO_FILENAME, ORIGINAL_SRT, TRANSLATED_SRT, OUTPUT_VIDEO],
            "DOWNLOADING": [AUDIO_FILENAME, ORIGINAL_SRT, TRANSLATED_SRT, OUTPUT_VIDEO],
        }

        to_remove_names = set(step_artifacts_map.get(current_step, [
            AUDIO_FILENAME, ORIGINAL_SRT, TRANSLATED_SRT, OUTPUT_VIDEO
        ]))

        to_remove_names.add("tmp_burn.srt")

        for name in to_remove_names:
            p = d / name
            if p.exists():
                try:
                    p.unlink()
                    logger.info("已清理取消任务的产物: task=%s, file=%s", task_id, name)
                except OSError as e:
                    logger.warning("清理取消任务产物失败: task=%s, file=%s, err=%s", task_id, name, e)

        cls.cleanup_download_temp_files(task_id)

        if (current_step in (None, "DOWNLOADING")) and source_type != "upload":
            for p in d.glob(f"{SOURCE_VIDEO_STEM}.*"):
                if not p.name.endswith(".part"):
                    try:
                        p.unlink()
                        logger.info("已清理取消任务的 source 文件: task=%s, file=%s", task_id, p.name)
                    except OSError as e:
                        logger.warning("清理 source 文件失败: task=%s, file=%s, err=%s", task_id, p.name, e)

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
