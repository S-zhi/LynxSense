"""③ 语音识别（Replicate）单测。

mock prediction create/get：验证 SRT 解析、状态轮询、恢复、错误包装和进度回调。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from src.core import transcriber
from src.core.transcriber import (
    TranscribeCancelledError,
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


def _prediction(*, status="succeeded", output=None, prediction_id="pred_1", error=None):
    return SimpleNamespace(id=prediction_id, status=status, output=output, error=error)


def _mock_replicate(monkeypatch, output):
    """mock replicate.Client，使 predictions.create 返回成功结果。

    返回一个 captured dict，captured["input"] 是最后一次传入的 input。
    """
    captured: dict = {"input": None, "create_calls": 0, "get_calls": []}

    class FakePredictions:
        def create(self, *, version, input, wait):
            captured["input"] = input
            captured["create_calls"] += 1
            assert wait is False
            return _prediction(output=output)

        def get(self, prediction_id):
            captured["get_calls"].append(prediction_id)
            return _prediction(output=output, prediction_id=prediction_id)

        def cancel(self, prediction_id):
            captured["canceled"] = prediction_id

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.predictions = FakePredictions()

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


def test_extract_rejects_unstructured_list():
    with pytest.raises(TranscribeError, match="结构无法解析") as exc_info:
        _extract_segments(["a", "b"])
    assert exc_info.value.code == "invalid_response"


def test_extract_rejects_invalid_segment_shape():
    with pytest.raises(TranscribeError, match="结构无法解析") as exc_info:
        _extract_segments({"segments": [{"text": "a"}]})
    assert exc_info.value.code == "invalid_response"


def test_extract_from_empty():
    assert _extract_segments([]) == []


# ---------- transcribe ----------

def test_transcribe_missing_audio():
    with pytest.raises(TranscribeError, match="不存在"):
        transcribe(Path("/nonexistent/audio.wav"), "t1")


def test_transcribe_rejects_empty_response(monkeypatch, tmp_path):
    _mock_replicate(monkeypatch, [])
    with pytest.raises(TranscribeError, match="空字幕列表") as exc_info:
        transcribe("https://example.com/audio.wav", "t1")
    assert exc_info.value.code == "empty_response"


def test_transcribe_rejects_zero_duration_segment(monkeypatch, tmp_path):
    _mock_replicate(monkeypatch, [{"text": "Hello", "start": 1.0, "end": 1.0}])
    with pytest.raises(TranscribeError, match="结束时间必须大于开始时间") as exc_info:
        transcribe("https://example.com/audio.wav", "t1")
    assert exc_info.value.code == "invalid_response"


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

    class FailPredictions:
        def create(self, **kwargs):
            raise RuntimeError("API down")

    class FailClient:
        def __init__(self, *args, **kwargs):
            self.predictions = FailPredictions()

    monkeypatch.setattr(transcriber.replicate, "Client", FailClient)
    with pytest.raises(TranscribeError, match="API down"):
        transcribe(audio, "t1")


def test_transcribe_no_api_token(monkeypatch, tmp_path):
    audio = make_fake_audio(tmp_path)
    monkeypatch.delenv("REPLICATE_API_TOKEN", raising=False)
    with pytest.raises(TranscribeError, match="REPLICATE_API_TOKEN") as exc_info:
        transcribe(audio, "t1")
    assert exc_info.value.code == "missing_api_key"


@pytest.mark.parametrize("status_code, expected_code", [
    (401, "unauthorized"),
    (403, "unauthorized"),
    (429, "rate_limited"),
    (500, "upstream_error"),
    (502, "upstream_error"),
    (400, "model_error"),
    (422, "model_error"),
])
def test_transcribe_http_status_error_codes(monkeypatch, tmp_path, status_code, expected_code):
    audio = make_fake_audio(tmp_path)

    class StatusErrorPredictions:
        def create(self, **kwargs):
            request = httpx.Request("POST", "https://api.replicate.com/v1/predictions")
            response = httpx.Response(status_code, request=request)
            raise httpx.HTTPStatusError("HTTP error", request=request, response=response)

    class StatusErrorClient:
        def __init__(self, *args, **kwargs):
            self.predictions = StatusErrorPredictions()

    monkeypatch.setattr(transcriber.replicate, "Client", StatusErrorClient)
    with pytest.raises(TranscribeError) as exc_info:
        transcribe(audio, "t1")
    assert exc_info.value.code == expected_code


def test_transcribe_replicate_error_code(monkeypatch, tmp_path):
    audio = make_fake_audio(tmp_path)

    class ReplicateErrorPredictions:
        def create(self, **kwargs):
            raise transcriber.replicate.exceptions.ReplicateError(
                title="Unauthorized", status=401, detail="Invalid token"
            )

    class ReplicateErrorClient:
        def __init__(self, *args, **kwargs):
            self.predictions = ReplicateErrorPredictions()

    monkeypatch.setattr(transcriber.replicate, "Client", ReplicateErrorClient)
    with pytest.raises(TranscribeError) as exc_info:
        transcribe(audio, "t1")
    assert exc_info.value.code == "unauthorized"


def test_transcribe_model_error_code(monkeypatch, tmp_path):
    audio = make_fake_audio(tmp_path)

    class ModelErrorPredictions:
        def create(self, **kwargs):
            return _prediction(status="failed", error="Model failed processing")

    class ModelErrorClient:
        def __init__(self, *args, **kwargs):
            self.predictions = ModelErrorPredictions()

    monkeypatch.setattr(transcriber.replicate, "Client", ModelErrorClient)
    with pytest.raises(TranscribeError) as exc_info:
        transcribe(audio, "t1")
    assert exc_info.value.code == "model_error"


# ---------- 冷启动重试 ----------

def test_transcribe_retries_on_timeout_then_succeeds(monkeypatch, tmp_path):
    """创建请求第一次超时，一小时策略等待后才再次创建。"""
    audio = make_fake_audio(tmp_path)
    fake_srt = "1\n0:00:00.060 --> 0:00:01.000\nHello\n"
    calls = {"n": 0}
    opened_files = []
    progress_events = []

    class FlakyPredictions:
        def create(self, *, version, input, wait):
            opened_files.append(input["audio_path"])
            assert input["audio_path"].closed is False
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.ReadTimeout("read timed out")
            return _prediction(output={"srt_file": "https://x/sub.srt"})

    class FlakyClient:
        def __init__(self, *args, **kwargs):
            self.predictions = FlakyPredictions()

    monkeypatch.setattr(transcriber.replicate, "Client", FlakyClient)
    monkeypatch.setattr(transcriber, "settings", SimpleNamespace(
        replicate_whisper_model="fake-model",
        replicate_timeout=30,
        replicate_retries=2,
        replicate_retry_interval=3600,
        replicate_poll_interval=30,
    ))
    monkeypatch.setattr(transcriber.httpx, "get", lambda url, timeout: SimpleNamespace(
        text=fake_srt, raise_for_status=lambda: None))
    monkeypatch.setattr(transcriber.time, "sleep", lambda s: None)  # 跳过退避等待

    res = transcribe(audio, "t1", on_progress=progress_events.append)
    assert res.segment_count == 1
    assert calls["n"] == 2
    assert any(event.status == "retrying" for event in progress_events)
    assert len(opened_files) == 2
    assert opened_files[0] is not opened_files[1]
    assert all(f.closed for f in opened_files)


def test_transcribe_cancel_check_raised_before_call(monkeypatch, tmp_path):
    audio = make_fake_audio(tmp_path)

    def cancel_check():
        raise TranscribeCancelledError("cancellation requested")

    with pytest.raises(TranscribeCancelledError, match="cancellation requested"):
        transcribe(audio, "t1", cancel_check=cancel_check)


def test_transcribe_cancel_check_raised_during_retry_sleep(monkeypatch, tmp_path):
    audio = make_fake_audio(tmp_path)
    check_calls = {"n": 0}

    class FlakyPredictions:
        def create(self, **kwargs):
            raise httpx.ReadTimeout("read timed out")

    class FlakyClient:
        def __init__(self, *args, **kwargs):
            self.predictions = FlakyPredictions()

    monkeypatch.setattr(transcriber.replicate, "Client", FlakyClient)
    monkeypatch.setattr(transcriber, "settings", SimpleNamespace(
        replicate_whisper_model="fake-model",
        replicate_timeout=30,
        replicate_retries=3,
        replicate_retry_interval=3600,
        replicate_poll_interval=30,
    ))
    monkeypatch.setattr(transcriber.time, "sleep", lambda s: None)

    def cancel_check():
        check_calls["n"] += 1
        # 在重试循环中抛出取消
        if check_calls["n"] >= 3:
            raise TranscribeCancelledError("cancelled during retry")

    with pytest.raises(TranscribeCancelledError, match="cancelled during retry"):
        transcribe(audio, "t1", cancel_check=cancel_check)


def test_transcribe_retries_exhausted(monkeypatch, tmp_path):
    """每次都超时，重试用尽后抛出清晰错误。"""
    audio = make_fake_audio(tmp_path)
    opened_files = []

    class AlwaysTimeoutPredictions:
        def create(self, **kwargs):
            input = kwargs["input"]
            opened_files.append(input["audio_path"])
            assert input["audio_path"].closed is False
            raise httpx.ReadTimeout("read timed out")

    class AlwaysTimeout:
        def __init__(self, *args, **kwargs):
            self.predictions = AlwaysTimeoutPredictions()

    monkeypatch.setattr(transcriber.replicate, "Client", AlwaysTimeout)
    monkeypatch.setattr(transcriber, "settings", SimpleNamespace(
        replicate_whisper_model="fake-model",
        replicate_timeout=30,
        replicate_retries=2,
        replicate_retry_interval=3600,
        replicate_poll_interval=30,
    ))
    monkeypatch.setattr(transcriber.time, "sleep", lambda s: None)

    with pytest.raises(TranscribeError, match="多次网络失败") as exc_info:
        transcribe(audio, "t1")
    assert exc_info.value.code == "network_error"
    assert len(opened_files) == 2
    assert opened_files[0] is not opened_files[1]
    assert all(f.closed for f in opened_files)


def test_transcribe_waiting_prediction_is_polled_without_recreate(monkeypatch, tmp_path):
    """starting/processing 只轮询同一个 prediction，不提交第二个任务。"""
    audio = make_fake_audio(tmp_path)
    fake_srt = "1\n0:00:00.060 --> 0:00:01.000\nHello\n"
    create_calls = []
    get_calls = []
    queued = [
        _prediction(status="starting", prediction_id="pred_waiting"),
        _prediction(status="processing", prediction_id="pred_waiting"),
        _prediction(
            status="succeeded",
            prediction_id="pred_waiting",
            output={"srt_file": "https://x/sub.srt"},
        ),
    ]

    class WaitingPredictions:
        def create(self, **kwargs):
            create_calls.append(kwargs)
            return queued.pop(0)

        def get(self, prediction_id):
            get_calls.append(prediction_id)
            return queued.pop(0)

    class WaitingClient:
        def __init__(self, *args, **kwargs):
            self.predictions = WaitingPredictions()

    monkeypatch.setattr(transcriber.replicate, "Client", WaitingClient)
    monkeypatch.setattr(transcriber.httpx, "get", lambda url, timeout: SimpleNamespace(
        text=fake_srt, raise_for_status=lambda: None))
    monkeypatch.setattr(transcriber.time, "sleep", lambda seconds: None)

    result = transcribe(audio, "t1")

    assert result.segment_count == 1
    assert len(create_calls) == 1
    assert get_calls == ["pred_waiting", "pred_waiting"]
    assert not (tmp_path / "replicate_prediction.json").exists()


def test_transcribe_get_timeout_retries_same_prediction(monkeypatch, tmp_path):
    """状态查询超时后只重查已有 ID，绝不能重新 create。"""
    audio = make_fake_audio(tmp_path)
    fake_srt = "1\n0:00:00.060 --> 0:00:01.000\nHello\n"
    calls = {"create": 0, "get": []}

    class FlakyGetPredictions:
        def create(self, **kwargs):
            calls["create"] += 1
            return _prediction(status="starting", prediction_id="pred_same")

        def get(self, prediction_id):
            calls["get"].append(prediction_id)
            if len(calls["get"]) == 1:
                raise httpx.ReadTimeout("get timed out")
            return _prediction(
                prediction_id=prediction_id,
                output={"srt_file": "https://x/sub.srt"},
            )

    class FlakyGetClient:
        def __init__(self, *args, **kwargs):
            self.predictions = FlakyGetPredictions()

    monkeypatch.setattr(transcriber.replicate, "Client", FlakyGetClient)
    monkeypatch.setattr(transcriber.httpx, "get", lambda url, timeout: SimpleNamespace(
        text=fake_srt, raise_for_status=lambda: None))
    monkeypatch.setattr(transcriber.time, "sleep", lambda seconds: None)

    result = transcribe(audio, "t1")

    assert result.segment_count == 1
    assert calls == {"create": 1, "get": ["pred_same", "pred_same"]}


def test_transcribe_cancel_active_prediction(monkeypatch, tmp_path):
    """本地取消时同步取消远端 prediction，并清理恢复状态。"""
    audio = make_fake_audio(tmp_path)
    canceled = []
    checks = {"count": 0}

    class ActivePredictions:
        def create(self, **kwargs):
            return _prediction(status="starting", prediction_id="pred_cancel")

        def cancel(self, prediction_id):
            canceled.append(prediction_id)

    class ActiveClient:
        def __init__(self, *args, **kwargs):
            self.predictions = ActivePredictions()

    def cancel_check():
        checks["count"] += 1
        if checks["count"] >= 4:
            raise TranscribeCancelledError("cancel active")

    monkeypatch.setattr(transcriber.replicate, "Client", ActiveClient)

    with pytest.raises(TranscribeCancelledError, match="cancel active"):
        transcribe(audio, "t1", cancel_check=cancel_check)

    assert canceled == ["pred_cancel"]
    assert not (tmp_path / "replicate_prediction.json").exists()


def test_transcribe_resumes_persisted_prediction_after_restart(monkeypatch, tmp_path):
    """任务恢复时读取已保存 ID，直接查询而不重新创建 prediction。"""
    audio = make_fake_audio(tmp_path)
    fake_srt = "1\n0:00:00.060 --> 0:00:01.000\nHello\n"
    model_ref = transcriber.settings.replicate_whisper_model
    (tmp_path / "replicate_prediction.json").write_text(
        '{"prediction_id":"pred_saved","model_ref":"' + model_ref + '","status":"starting"}',
        encoding="utf-8",
    )
    calls = {"create": 0, "get": []}

    class ResumePredictions:
        def create(self, **kwargs):
            calls["create"] += 1
            raise AssertionError("已有 prediction ID 时不应重新创建")

        def get(self, prediction_id):
            calls["get"].append(prediction_id)
            return _prediction(
                prediction_id=prediction_id,
                output={"srt_file": "https://x/sub.srt"},
            )

    class ResumeClient:
        def __init__(self, *args, **kwargs):
            self.predictions = ResumePredictions()

    monkeypatch.setattr(transcriber.replicate, "Client", ResumeClient)
    monkeypatch.setattr(transcriber.httpx, "get", lambda url, timeout: SimpleNamespace(
        text=fake_srt, raise_for_status=lambda: None))

    result = transcribe(audio, "t1")

    assert result.segment_count == 1
    assert calls == {"create": 0, "get": ["pred_saved"]}


def test_cancel_failure_preserves_prediction_for_next_run(monkeypatch, tmp_path):
    """远端取消失败时保留 ID，并在下一次运行启动时重试清理。"""
    cancel_calls = []
    checks = {"count": 0}

    class FirstPredictions:
        def create(self, **kwargs):
            return _prediction(status="starting", prediction_id="pred_pending")

        def cancel(self, prediction_id):
            cancel_calls.append(prediction_id)
            raise RuntimeError("temporary cancel failure")

    class FirstClient:
        def __init__(self, *args, **kwargs):
            self.predictions = FirstPredictions()

    monkeypatch.setattr(transcriber.replicate, "Client", FirstClient)
    monkeypatch.setattr(transcriber.time, "sleep", lambda seconds: None)

    def cancel_check():
        checks["count"] += 1
        if checks["count"] >= 4:
            raise TranscribeCancelledError("cancelled")

    with pytest.raises(TranscribeCancelledError):
        transcriber._run_replicate_with_retry(
            "model", lambda: {}, timeout=30, retries=1, retry_interval=0,
            poll_interval=0, state_dir=tmp_path, cancel_check=cancel_check,
        )

    state = (tmp_path / "replicate_prediction.json").read_text(encoding="utf-8")
    assert '"prediction_id": "pred_pending"' in state
    assert '"status": "cancel_pending"' in state

    class RetryPredictions:
        def cancel(self, prediction_id):
            cancel_calls.append(prediction_id)

        def create(self, **kwargs):
            return _prediction(output="result", prediction_id="pred_new")

    class RetryClient:
        def __init__(self, *args, **kwargs):
            self.predictions = RetryPredictions()

    monkeypatch.setattr(transcriber.replicate, "Client", RetryClient)
    result = transcriber._run_replicate_with_retry(
        "model", lambda: {}, timeout=30, retries=1, retry_interval=0,
        poll_interval=0, state_dir=tmp_path,
    )

    assert result == "result"
    assert cancel_calls == ["pred_pending", "pred_pending"]
    state = (tmp_path / "replicate_prediction.json").read_text(encoding="utf-8")
    assert '"prediction_id": "pred_new"' in state
    assert '"status": "succeeded"' in state
