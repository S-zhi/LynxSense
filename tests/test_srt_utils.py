"""SRT 读写工具单测（含多编码解析）。"""

from __future__ import annotations

import pytest

from src.core.srt_utils import (
    Subtitle,
    decode_srt_bytes,
    format_timestamp,
    parse_srt,
    parse_timestamp,
    read_srt_content,
    write_srt,
)


def test_format_timestamp_basic():
    assert format_timestamp(0) == "00:00:00,000"
    assert format_timestamp(1.5) == "00:00:01,500"
    assert format_timestamp(3661.123) == "01:01:01,123"


def test_format_timestamp_negative_clamped():
    assert format_timestamp(-5) == "00:00:00,000"


def test_parse_timestamp_comma_and_dot():
    assert parse_timestamp("00:00:01,500") == 1.5
    assert parse_timestamp("00:00:01.500") == 1.5
    assert parse_timestamp("01:01:01,123") == 3661.123


def test_roundtrip(tmp_path):
    subs = [
        Subtitle(1, 0.0, 1.5, "Hello"),
        Subtitle(2, 1.5, 3.0, "World"),
    ]
    p = tmp_path / "x.srt"
    write_srt(subs, p)
    parsed = parse_srt(p)
    assert len(parsed) == 2
    assert parsed[0].text == "Hello"
    assert parsed[1].start == 1.5
    assert parsed[1].end == 3.0


def test_write_reindexes(tmp_path):
    # 传入乱序 index，写出后应重排为 1,2
    subs = [Subtitle(99, 0, 1, "a"), Subtitle(7, 1, 2, "b")]
    p = tmp_path / "x.srt"
    write_srt(subs, p)
    content = p.read_text(encoding="utf-8-sig")
    assert content.startswith("1\n")
    assert "\n2\n" in content




def test_write_rejects_non_positive_duration(tmp_path):
    p = tmp_path / "invalid.srt"
    with pytest.raises(ValueError, match="结束时间必须大于开始时间"):
        write_srt([Subtitle(1, 2.0, 2.0, "invalid")], p)


def test_parse_multiline_text(tmp_path):
    p = tmp_path / "x.srt"
    p.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\n原文\n译文\n",
        encoding="utf-8",
    )
    subs = parse_srt(p)
    assert subs[0].text == "原文\n译文"


def test_parse_missing_index(tmp_path):
    # 没有序号行，只有时间行 + 文本
    p = tmp_path / "x.srt"
    p.write_text("00:00:00,000 --> 00:00:01,000\nhi\n", encoding="utf-8")
    subs = parse_srt(p)
    assert len(subs) == 1
    assert subs[0].text == "hi"


def test_parse_empty(tmp_path):
    p = tmp_path / "x.srt"
    p.write_text("", encoding="utf-8")
    assert parse_srt(p) == []


def test_parse_gbk_encoding(tmp_path):
    p = tmp_path / "gbk.srt"
    content_str = "1\n00:00:00,000 --> 00:00:01,500\n测试字幕\n"
    p.write_bytes(content_str.encode("gbk"))

    subs = parse_srt(p)
    assert len(subs) == 1
    assert subs[0].text == "测试字幕"


def test_parse_utf16le_with_and_without_bom(tmp_path):
    content_str = "1\n00:00:00,000 --> 00:00:01,500\n测试字幕\n"

    # 带 BOM 的 UTF-16
    p1 = tmp_path / "utf16_bom.srt"
    p1.write_bytes(content_str.encode("utf-16"))
    subs1 = parse_srt(p1)
    assert len(subs1) == 1
    assert subs1[0].text == "测试字幕"

    # 不带 BOM 的 UTF-16 LE
    p2 = tmp_path / "utf16_nobom.srt"
    p2.write_bytes(content_str.encode("utf-16-le"))
    subs2 = parse_srt(p2)
    assert len(subs2) == 1
    assert subs2[0].text == "测试字幕"


def test_parse_utf16be(tmp_path):
    content_str = "1\n00:00:00,000 --> 00:00:01,500\n测试字幕\n"
    p = tmp_path / "utf16be.srt"
    p.write_bytes(content_str.encode("utf-16-be"))
    subs = parse_srt(p)
    assert len(subs) == 1
    assert subs[0].text == "测试字幕"


def test_parse_crlf_line_endings(tmp_path):
    p = tmp_path / "crlf.srt"
    p.write_bytes("1\r\n00:00:00,000 --> 00:00:01,000\r\n测试字幕\r\n".encode("gbk"))
    subs = parse_srt(p)
    assert len(subs) == 1
    assert subs[0].text == "测试字幕"


def test_write_srt_custom_encoding(tmp_path):
    subs = [Subtitle(1, 0.0, 1.0, "GBK测试")]
    p = tmp_path / "out_gbk.srt"
    write_srt(subs, p, encoding="gbk")

    # 验证磁盘数据为合法 GBK
    raw = p.read_bytes()
    text, enc = decode_srt_bytes(raw)
    assert enc in ("gbk", "gb18030")
    assert "GBK测试" in text


def test_decode_srt_bytes_empty():
    text, enc = decode_srt_bytes(b"")
    assert text == ""
    assert enc == "utf-8"


def test_read_srt_content_raw_text():
    srt_str = "1\n00:00:00,000 --> 00:00:01,000\nhello"
    subs = parse_srt(srt_str)
    assert len(subs) == 1
    assert subs[0].text == "hello"
