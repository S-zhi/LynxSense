"""③ 语音识别（Replicate）单测。

mock replicate.run：验证 SRT 解析（标准/紧凑格式）、语言参数传递、错误包装、进度回调。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from src.core import transcriber
from src.core.transcriber import (
    TranscribeError,
    transcribe,
    _extract_segments,
    _parse_srt_segments,
)


@pytest.fixture(autouse=True)
def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(transcriber, "ensure_task_dir", lambda tid: tmp_path)
    monkeypatch.setattr(transcriber, "_load_env", lambda: None)
    monkeypatch.setenv("REPLICATE_API_TOKEN", "fake-token")


def _mock_replicate(monkeypatch, output):
    """mock replicate.Client，使 Client(...).run(ref, input=...) 返回指定 output。

    返回一个 captured dict，captured["input"] 是最后一次传入的 input。
    """
    captured: dict = {"input": None}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, ref, input):
            captured["input"] = input
            return output

    monkeypatch.setattr(transcriber.replicate, "Client", FakeClient)
    return captured


def make_fake_audio(tmp_path):
    p = tmp_path / "test.wav"
    p.write_bytes(b"fake audio")
    return p


# ---------- _parse_srt_segments ----------

def test_parse_standard_srt():
    srt = (
        "1\n00:00:01,000 --> 00:00:02,500\nHello world\n\n"
        "2\n00:00:03,000 --> 00:00:05,000\nSecond line\n"
    )
    segs = _parse_srt_segments(srt)
    assert len(segs) == 2 and segs[0]["text"] == "Hello world"


def test_parse_compact_replicate_srt():
    srt = (
        "1\n0:00:00.060 --> 0:00:05.220\nHello world\n"
        "2\n0:00:05.220 --> 0:00:10.500\nSecond line\n"
    )
    segs = _parse_srt_segments(srt)
    assert len(segs) == 2 and segs[1]["text"] == "Second line"


# ---------- _extract_segments ----------

def test_extract_from_file_output_url(monkeypatch):
    fake_srt = "1\n0:00:00.060 --> 0:00:01.000\nTest\n2\n0:00:02.000 --> 0:00:03.000\nLine2\n"
    monkeypatch.setattr(transcriber.httpx, "get", lambda url, timeout: SimpleNamespace(
        text=fake_srt, raise_for_status=lambda: None))
    segs = _extract_segments({"srt_file": "https://x/sub.srt"})
    assert len(segs) == 2


def test_extract_from_segments_list():
    assert len(_extract_segments([{"text": "a", "start": 0.0, "end": 1.0}])) == 1


def test_extract_from_empty():
    assert _extract_segments(None) == []
    assert _extract_segments({}) == []


# ---------- transcribe ----------

def test_transcribe_missing_audio():
    with pytest.raises(TranscribeError, match="不存在"):
        transcribe(Path("/nonexistent/audio.wav"), "t1")


def test_transcribe_with_remote_url(monkeypatch, tmp_path):
    fake_srt = "1\n0:00:00.060 --> 0:00:01.000\nHello\n"
    _mock_replicate(monkeypatch, {"srt_file": "https://x/sub.srt"})
    monkeypatch.setattr(transcriber.httpx, "get", lambda url, timeout: SimpleNamespace(
        text=fake_srt, raise_for_status=lambda: None))
    res = transcribe("https://example.com/audio.wav", "t1", language="en")
    assert res.segment_count == 1


def test_transcribe_with_local_file(monkeypatch, tmp_path):
    audio = make_fake_audio(tmp_path)
    fake_srt = "1\n0:00:00.060 --> 0:00:01.000\nHello\n"
    cap = _mock_replicate(monkeypatch, {"srt_file": "https://x/sub.srt"})
    monkeypatch.setattr(transcriber.httpx, "get", lambda url, timeout: SimpleNamespace(
        text=fake_srt, raise_for_status=lambda: None))
    res = transcribe(audio, "t1")
    assert res.segment_count == 1
    assert cap["input"]["audio_path"].closed is True


def test_transcribe_passes_language_and_model(monkeypatch, tmp_path):
    audio = make_fake_audio(tmp_path)
    fake_srt = "1\n0:00:00.060 --> 0:00:01.000\nHello\n"
    cap = _mock_replicate(monkeypatch, {"srt_file": "https://x/sub.srt"})
    monkeypatch.setattr(transcriber.httpx, "get", lambda url, timeout: SimpleNamespace(
        text=fake_srt, raise_for_status=lambda: None))
    transcribe(audio, "t1", language="ja", model_name="large-v3")
    assert cap["input"]["language"] == "ja" and cap["input"]["model_name"] == "large-v3"


def test_transcribe_auto_language_omitted(monkeypatch, tmp_path):
    audio = make_fake_audio(tmp_path)
    fake_srt = "1\n0:00:00.060 --> 0:00:01.000\nHello\n"
    cap = _mock_replicate(monkeypatch, {"srt_file": "https://x/sub.srt"})
    monkeypatch.setattr(transcriber.httpx, "get", lambda url, timeout: SimpleNamespace(
        text=fake_srt, raise_for_status=lambda: None))
    transcribe(audio, "t1", language="auto")
    assert "language" not in cap["input"]
    transcribe(audio, "t1", language=None)
    assert "language" not in cap["input"]


def test_transcribe_progress_called(monkeypatch, tmp_path):
    audio = make_fake_audio(tmp_path)
    fake_srt = "1\n0:00:00.060 --> 0:00:01.000\nHello\n"
    _mock_replicate(monkeypatch, {"srt_file": "https://x/sub.srt"})
    monkeypatch.setattr(transcriber.httpx, "get", lambda url, timeout: SimpleNamespace(
        text=fake_srt, raise_for_status=lambda: None))
    progress = []
    transcribe(audio, "t1", on_progress=progress.append)
    assert len(progress) >= 2


def test_transcribe_error_wrapped(monkeypatch, tmp_path):
    audio = make_fake_audio(tmp_path)

    class FailClient:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, ref, input):
            raise RuntimeError("API down")

    monkeypatch.setattr(transcriber.replicate, "Client", FailClient)
    with pytest.raises(TranscribeError, match="API down"):
        transcribe(audio, "t1")


def test_transcribe_no_api_token(monkeypatch, tmp_path):
    audio = make_fake_audio(tmp_path)
    monkeypatch.delenv("REPLICATE_API_TOKEN", raising=False)
    with pytest.raises(TranscribeError, match="REPLICATE_API_TOKEN"):
        transcribe(audio, "t1")


# ---------- 冷启动重试 ----------

def test_transcribe_retries_on_timeout_then_succeeds(monkeypatch, tmp_path):
    """第一次读超时（冷启动），重试后成功。"""
    audio = make_fake_audio(tmp_path)
    fake_srt = "1\n0:00:00.060 --> 0:00:01.000\nHello\n"
    calls = {"n": 0}
    opened_files = []

    class FlakyClient:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, ref, input):
            opened_files.append(input["audio_path"])
            assert input["audio_path"].closed is False
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.ReadTimeout("read timed out")
            return {"srt_file": "https://x/sub.srt"}

    monkeypatch.setattr(transcriber.replicate, "Client", FlakyClient)
    monkeypatch.setattr(transcriber, "settings", SimpleNamespace(
        replicate_whisper_model="fake-model",
        replicate_timeout=30,
        replicate_retries=2,
    ))
    monkeypatch.setattr(transcriber.httpx, "get", lambda url, timeout: SimpleNamespace(
        text=fake_srt, raise_for_status=lambda: None))
    monkeypatch.setattr(transcriber.time, "sleep", lambda s: None)  # 跳过退避等待

    res = transcribe(audio, "t1")
    assert res.segment_count == 1
    assert calls["n"] == 2  # 第一次超时 + 第二次成功
    assert len(opened_files) == 2
    assert opened_files[0] is not opened_files[1]
    assert all(f.closed for f in opened_files)


def test_transcribe_retries_exhausted(monkeypatch, tmp_path):
    """每次都超时，重试用尽后抛出清晰错误。"""
    audio = make_fake_audio(tmp_path)
    opened_files = []

    class AlwaysTimeout:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, ref, input):
            opened_files.append(input["audio_path"])
            assert input["audio_path"].closed is False
            raise httpx.ReadTimeout("read timed out")

    monkeypatch.setattr(transcriber.replicate, "Client", AlwaysTimeout)
    monkeypatch.setattr(transcriber, "settings", SimpleNamespace(
        replicate_whisper_model="fake-model",
        replicate_timeout=30,
        replicate_retries=2,
    ))
    monkeypatch.setattr(transcriber.time, "sleep", lambda s: None)

    with pytest.raises(TranscribeError, match="多次超时"):
        transcribe(audio, "t1")
    assert len(opened_files) == 2
    assert opened_files[0] is not opened_files[1]
    assert all(f.closed for f in opened_files)
