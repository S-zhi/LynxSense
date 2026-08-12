"""存储路径策略。

每个任务一个目录：data/{task_id}/，所有中间产物与成品都落在里面。
好处：断点续跑只需检查文件是否存在；清理任务只需删一个目录；
DB 只存相对信息，不依赖绝对路径历史。
"""

from __future__ import annotations

from pathlib import Path

from .config import settings

# 流水线各阶段的标准产物文件名（stem，不含扩展名的固定基名）
SOURCE_VIDEO_STEM = "source"      # 下载的原始视频 source.mp4
AUDIO_FILENAME = "audio.wav"      # 提取的音频
ORIGINAL_SRT = "original.srt"     # 识别出的原文字幕
TRANSLATED_SRT = "translated.srt"  # 翻译后的字幕
OUTPUT_VIDEO = "output.mp4"       # 烧录后的成品


def task_dir(task_id: str) -> Path:
    """返回任务目录路径（不保证存在）。"""
    return settings.data_dir / task_id


def ensure_task_dir(task_id: str) -> Path:
    """返回任务目录路径，并确保已创建。"""
    d = task_dir(task_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def artifacts_present(task_id: str, *, data_dir: Path, need_subtitle: bool) -> bool:
    """判断一个成功任务的产物是否还都在磁盘上。

    - 完整流水线（need_subtitle=True）：output.mp4 与 translated.srt 都必须在。
    - 仅下载模式（need_subtitle=False）：source.* 至少存在一个。

    与 task_dir 一样不抛异常；目录不存在视为资源丢失。
    该函数用于服务启动 / 下载兜底场景：发现资源缺失就把状态降级为 MISSING，
    避免给用户暴露已经失效的下载链接。
    """
    d = data_dir / task_id
    if not d.is_dir():
        return False
    if need_subtitle:
        return (d / OUTPUT_VIDEO).exists() and (d / TRANSLATED_SRT).exists()
    return any(d.glob(f"{SOURCE_VIDEO_STEM}.*"))
