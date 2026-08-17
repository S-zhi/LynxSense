"""SRT 字幕读写工具。

被 ③识别（写 original.srt）和 ④翻译（读 original.srt、写 translated.srt）共用。
自己实现一份轻量解析/生成，避免引入额外依赖，也方便单测。
支持多编码自动探测（UTF-8, GBK, GB18030, UTF-16, Latin-1 等）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple, Union


@dataclass
class Subtitle:
    """一条字幕。start / end 为秒；text 可含换行（双语时原文/译文两行）。"""

    index: int
    start: float
    end: float
    text: str


def format_timestamp(seconds: float) -> str:
    """秒 -> SRT 时间戳 HH:MM:SS,mmm。"""
    if seconds < 0:
        seconds = 0.0
    total_ms = int(round(seconds * 1000))
    hours, total_ms = divmod(total_ms, 3_600_000)
    minutes, total_ms = divmod(total_ms, 60_000)
    secs, millis = divmod(total_ms, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def parse_timestamp(ts: str) -> float:
    """SRT 时间戳 -> 秒。兼容逗号或点作为毫秒分隔符。"""
    ts = ts.strip().replace(".", ",")
    hms, _, millis = ts.partition(",")
    hours, minutes, secs = hms.split(":")
    return int(hours) * 3600 + int(minutes) * 60 + int(secs) + int(millis or 0) / 1000


def decode_srt_bytes(content: bytes) -> Tuple[str, str]:
    """探测并解码 SRT 字节数据。

    按 utf-8-sig -> utf-16 -> utf-8 -> gbk -> gb18030 -> utf-16-le -> utf-16-be -> latin-1 顺序回退。
    返回 (decoded_text, detected_encoding)。
    """
    if not content:
        return "", "utf-8"

    # 1. 显式 BOM 检查
    if content.startswith(b"\xef\xbb\xbf"):
        return content[3:].decode("utf-8", errors="replace"), "utf-8-sig"
    if content.startswith(b"\xff\xfe") or content.startswith(b"\xfe\xff"):
        return content.decode("utf-16", errors="replace"), "utf-16"

    # 2. 候选编码测试
    candidates = ["utf-8", "gbk", "gb18030", "utf-16-le", "utf-16-be", "latin-1"]
    for enc in candidates:
        try:
            text = content.decode(enc)
            # 如果 utf-8 或 gbk 得到包含 NUL (\x00) 的文本，说明大概率是 UTF-16，跳过
            if "\x00" in text and enc not in ("utf-16-le", "utf-16-be"):
                continue
            # 对于无 BOM 的 UTF-16，如果字节含 --> 标志但解码文本无 -->，说明字节序(endian)选错，跳过
            if enc in ("utf-16-le", "utf-16-be") and "-->" not in text:
                if b"-->" in content or b"\x00-\x00-\x00>" in content or b"-\x00-\x00>\x00" in content:
                    continue
            return text, enc
        except (UnicodeDecodeError, ValueError):
            continue

    return content.decode("latin-1", errors="replace"), "latin-1"


def read_srt_content(input_data: Union[Path, str, bytes]) -> Tuple[str, str]:
    """读取 SRT 输入内容（文件路径、字节或文本），自动探测编码并返回 (text, encoding)。"""
    if isinstance(input_data, bytes):
        return decode_srt_bytes(input_data)

    if isinstance(input_data, (Path, str)):
        try:
            p = Path(input_data)
            if p.is_file():
                return decode_srt_bytes(p.read_bytes())
        except OSError:
            pass
        if isinstance(input_data, str):
            return input_data, "utf-8"

    raise ValueError(f"无法读取的 SRT 输入: {type(input_data)}")


def write_srt(
    subs: List[Subtitle], path: Union[Path, str], encoding: str = "utf-8-sig"
) -> None:
    """把字幕列表写成 SRT 文件（默认 UTF-8 with BOM 兼容 Windows）。序号按顺序重排。"""
    path = Path(path)
    blocks: List[str] = []
    for i, sub in enumerate(subs, start=1):
        blocks.append(
            f"{i}\n"
            f"{format_timestamp(sub.start)} --> {format_timestamp(sub.end)}\n"
            f"{sub.text}".rstrip()
        )
    content = "\n\n".join(blocks)
    if content:
        content += "\n"
    path.write_text(content, encoding=encoding)


def parse_srt(path_or_input: Union[Path, str, bytes]) -> List[Subtitle]:
    """解析 SRT 文件/文本/字节为字幕列表。自动探测编码，容忍 CRLF/缺序号/多行文本/点逗号毫秒。"""
    raw, _ = read_srt_content(path_or_input)
    raw = raw.replace("\r\n", "\n")
    subs: List[Subtitle] = []
    for block in re.split(r"\n\s*\n", raw.strip()):
        lines = [ln for ln in block.splitlines()]
        if not lines:
            continue
        # 找到包含 --> 的时间行
        time_idx = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if time_idx is None:
            continue
        start_s, _, end_s = lines[time_idx].partition("-->")
        try:
            start = parse_timestamp(start_s)
            end = parse_timestamp(end_s)
        except (ValueError, IndexError):
            continue
        text = "\n".join(lines[time_idx + 1 :]).strip()
        index = len(subs) + 1
        if time_idx == 1:  # 时间行前一行通常是序号
            try:
                index = int(lines[0].strip())
            except ValueError:
                pass
        subs.append(Subtitle(index=index, start=start, end=end, text=text))
    return subs
