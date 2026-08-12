"""断点续跑与按步骤重试（Issue #30）单测。

覆盖：
  · orchestrator.start_from 跳过前面的步骤（mock 各步函数，断言调用集合）
  · 续跑时前置产物缺失 -> PipelineError
  · TaskStore 持久化 completed_steps / last_error_step
  · retry_task 接受 start_from 并透传给 runner
  · resume_options API 按当前 task_dir 产物推断每步可用性
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.handler import tasks as tasks_routes
from src.handler.app import app
from src.handler.deps import get_store
from src.service import orchestrator
from src.service.orchestrator import (
    PIPELINE_STEPS,
    PipelineError,
    PipelineParams,
    list_resume_options,
    run_pipeline,
    validate_start_from,
)
from src.store import TaskStore


# ---------------------------------------------------------------------------
# 工具：装上五步假实现
# ---------------------------------------------------------------------------


def _params(**over) -> PipelineParams:
    base = dict(
        task_id="t1",
        url="http://x/v",
        source_lang="auto",
        target_lang="zh-CN",
        mode="mono",
        burn="hard",
        model="small",
    )
    base.update(over)
    return PipelineParams(**base)


def _install_step_fakes(monkeypatch, *, calls):
    """装上五步假实现：仅记录调用名 + 返回带必要属性的结果。"""

    def make(name, result):
        def fn(*args, **kwargs):
            calls.append(name)
            return result
        return fn

    monkeypatch.setattr(orchestrator, "download_video",
                        make("DOWNLOADING", SimpleNamespace(video_path=Path("/d/source.mp4"), title="T")))
    monkeypatch.setattr(orchestrator, "extract_audio",
                        make("EXTRACTING", SimpleNamespace(audio_path=Path("/d/audio.wav"))))
    monkeypatch.setattr(orchestrator, "transcribe",
                        make("TRANSCRIBING", SimpleNamespace(srt_path=Path("/d/original.srt"))))
    monkeypatch.setattr(orchestrator, "translate_srt",
                        make("TRANSLATING", SimpleNamespace(srt_path=Path("/d/translated.srt"))))
    monkeypatch.setattr(orchestrator, "burn_subtitles",
                        make("BURNING", SimpleNamespace(output_path=Path("/d/output.mp4"))))


# ---------------------------------------------------------------------------
# orchestrator 层：start_from 行为
# ---------------------------------------------------------------------------


def test_start_from_translate_skips_previous_steps(monkeypatch, tmp_path):
    """start_from=TRANSLATING：download/extract/transcribe 跳过，只跑 translate+burn。"""
    (tmp_path / "source.mp4").write_bytes(b"VID")
    (tmp_path / "audio.wav").write_bytes(b"WAV")
    (tmp_path / "original.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n")
    monkeypatch.setattr(orchestrator, "task_dir", lambda tid: tmp_path)

    calls = []
    _install_step_fakes(monkeypatch, calls=calls)

    events = []
    run_pipeline(_params(), events.append, start_from="TRANSLATING")

    assert "DOWNLOADING" not in calls
    assert "EXTRACTING" not in calls
    assert "TRANSCRIBING" not in calls
    assert calls == ["TRANSLATING", "BURNING"]

    # 终态事件带 completed_steps
    final = events[-1]
    assert final.status == "SUCCESS"
    assert final.completed_steps == ["DOWNLOADING", "EXTRACTING", "TRANSCRIBING", "TRANSLATING", "BURNING"]


def test_start_from_each_stage_resumes_correctly(monkeypatch, tmp_path):
    """每个 start_from 都应只跑它之后的步骤（前置阶段跳过 + 调用列表对得上）。"""
    stage_order = list(PIPELINE_STEPS)
    artifacts_per_stage = {
        "DOWNLOADING": [],
        "EXTRACTING": ["source.mp4"],
        "TRANSCRIBING": ["source.mp4", "audio.wav"],
        "TRANSLATING": ["source.mp4", "audio.wav", "original.srt"],
        "BURNING": ["source.mp4", "audio.wav", "original.srt", "translated.srt"],
    }
    monkeypatch.setattr(orchestrator, "task_dir", lambda tid: tmp_path)

    for start in stage_order:
        # 清空后只放 start 所需的"前置产物"
        for p in tmp_path.iterdir():
            p.unlink()
        for name in artifacts_per_stage[start]:
            (tmp_path / name).write_bytes(b"X")

        calls = []
        _install_step_fakes(monkeypatch, calls=calls)
        events = []
        run_pipeline(_params(), events.append, start_from=start)

        expected_called = stage_order[stage_order.index(start):]
        assert calls == expected_called, f"start_from={start}: expected {expected_called}, got {calls}"


def test_start_from_invalid_step_raises(monkeypatch):
    calls = []
    _install_step_fakes(monkeypatch, calls=calls)

    with pytest.raises(PipelineError):
        run_pipeline(_params(), lambda e: None, start_from="NOT_A_STEP")
    assert calls == []


def test_start_from_needs_artifact_missing_raises(monkeypatch, tmp_path):
    """start_from=TRANSLATING 但 original.srt 缺失 -> PipelineError，明确说不能继续。"""
    (tmp_path / "source.mp4").write_bytes(b"VID")
    (tmp_path / "audio.wav").write_bytes(b"WAV")
    # 缺 original.srt
    monkeypatch.setattr(orchestrator, "task_dir", lambda tid: tmp_path)

    calls = []
    _install_step_fakes(monkeypatch, calls=calls)

    events = []
    with pytest.raises(PipelineError) as exc_info:
        run_pipeline(_params(), events.append, start_from="TRANSLATING")

    assert "original.srt" in str(exc_info.value)
    assert calls == []  # 任何步骤都不应执行

    # 失败事件也发出，让 runner 写入 FAILED 状态
    last = events[-1]
    assert last.status == "FAILED"
    assert "original.srt" in (last.error or "")


def test_failed_event_records_error_step(monkeypatch, tmp_path):
    """不在续跑模式下：失败时把倒下阶段填到 error_step。"""
    (tmp_path / "source.mp4").write_bytes(b"VID")
    monkeypatch.setattr(orchestrator, "task_dir", lambda tid: tmp_path)

    monkeypatch.setattr(orchestrator, "download_video",
                        lambda *a, **k: SimpleNamespace(video_path=tmp_path / "source.mp4", title="T"))
    monkeypatch.setattr(orchestrator, "extract_audio",
                        lambda *a, **k: SimpleNamespace(audio_path=tmp_path / "audio.wav"))

    def boom_transcribe(*a, **k):
        raise RuntimeError("transcribe crash")

    monkeypatch.setattr(orchestrator, "transcribe", boom_transcribe)

    events = []
    with pytest.raises(RuntimeError):
        run_pipeline(_params(), events.append)

    last = events[-1]
    assert last.status == "FAILED"
    assert last.error_step == "TRANSCRIBING"
    # 之前完成的步骤也应该记录
    assert last.completed_steps == ["DOWNLOADING", "EXTRACTING"]


def test_completed_steps_accumulate_in_success_events(monkeypatch, tmp_path):
    """每一步的成功事件都应反映累计 completed_steps（前端可实时展示进度）。"""
    (tmp_path / "source.mp4").write_bytes(b"VID")
    monkeypatch.setattr(orchestrator, "task_dir", lambda tid: tmp_path)

    _install_step_fakes(monkeypatch, calls=[])

    events = []
    run_pipeline(_params(), events.append)

    success_events = [e for e in events if e.completed_steps is not None]
    # 至少看到 5 次事件携带 completed_steps（每个步骤发 1 次）
    assert len(success_events) >= 5
    # 最后一次的 completed_steps 应包含全部 5 个阶段
    assert success_events[-1].completed_steps == list(PIPELINE_STEPS)


# ---------------------------------------------------------------------------
# validate_start_from / list_resume_options 工具
# ---------------------------------------------------------------------------


def test_validate_start_from_returns_completed_before(tmp_path, monkeypatch):
    (tmp_path / "source.mp4").write_bytes(b"VID")
    (tmp_path / "audio.wav").write_bytes(b"WAV")
    (tmp_path / "original.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n")
    monkeypatch.setattr(orchestrator, "task_dir", lambda tid: tmp_path)

    completed = validate_start_from("TRANSLATING", "t1", need_subtitle=True)
    assert completed == ["DOWNLOADING", "EXTRACTING", "TRANSCRIBING"]


def test_validate_start_from_raises_when_artifact_missing(tmp_path, monkeypatch):
    (tmp_path / "source.mp4").write_bytes(b"VID")
    # 缺 audio.wav / original.srt
    monkeypatch.setattr(orchestrator, "task_dir", lambda tid: tmp_path)
    with pytest.raises(PipelineError) as exc:
        validate_start_from("TRANSLATING", "t1", need_subtitle=True)
    assert "EXTRACTING" in str(exc.value) or "audio.wav" in str(exc.value)


def test_list_resume_options_no_artifacts(tmp_path, monkeypatch):
    """无任何产物：仅 DOWNLOADING 可作为起点（前序为空）。"""
    monkeypatch.setattr(orchestrator, "task_dir", lambda tid: tmp_path)
    options = list_resume_options("t1", need_subtitle=True)
    by_name = {n: (ok, r) for n, ok, r in options}
    assert by_name["DOWNLOADING"] == (True, None)
    for n in ("EXTRACTING", "TRANSCRIBING", "TRANSLATING", "BURNING"):
        ok, reason = by_name[n]
        assert ok is False
        assert reason  # 有原因


def test_list_resume_options_with_artifacts(tmp_path, monkeypatch):
    """source.mp4 + audio.wav + original.srt 在：TRANSLATING 与 BURNING 的可恢复性视其前置。"""
    (tmp_path / "source.mp4").write_bytes(b"VID")
    (tmp_path / "audio.wav").write_bytes(b"WAV")
    (tmp_path / "original.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n")
    # translated.srt 缺失
    monkeypatch.setattr(orchestrator, "task_dir", lambda tid: tmp_path)

    options = list_resume_options("t1", need_subtitle=True)
    by_name = {n: (ok, r) for n, ok, r in options}
    # 前三步产物齐，可作为 resume 起点（前序已就绪）
    assert by_name["DOWNLOADING"] == (True, None)
    assert by_name["EXTRACTING"] == (True, None)
    assert by_name["TRANSCRIBING"] == (True, None)
    assert by_name["TRANSLATING"] == (True, None)
    # 缺 translated.srt，BURNING 不可
    assert by_name["BURNING"][0] is False
    assert "translated.srt" in by_name["BURNING"][1]


def test_list_resume_options_only_download_when_need_subtitle_false(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "task_dir", lambda tid: tmp_path)
    options = list_resume_options("t1", need_subtitle=False)
    assert [n for n, _, _ in options] == ["DOWNLOADING"]


# ---------------------------------------------------------------------------
# TaskStore 持久化
# ---------------------------------------------------------------------------


def test_store_roundtrip_completed_steps(tmp_path):
    store = TaskStore(tmp_path / "app.db")
    rec = store.create(
        url="http://x/v", source_lang="auto", target_lang="zh-CN",
        mode="mono", burn="hard", model="small", engine="deepseek",
    )
    # 新建时为 []
    assert rec.completed_steps == []
    assert rec.last_error_step is None

    store.update(rec.id, completed_steps=["DOWNLOADING", "EXTRACTING", "TRANSCRIBING"])
    store.update(rec.id, last_error_step="TRANSLATING")

    got = store.get(rec.id)
    assert got is not None
    assert got.completed_steps == ["DOWNLOADING", "EXTRACTING", "TRANSCRIBING"]
    assert got.last_error_step == "TRANSLATING"

    # 清零：retry 时把 last_error_step 置空
    store.update(rec.id, last_error_step=None)
    got2 = store.get(rec.id)
    assert got2 is not None
    assert got2.last_error_step is None
    assert got2.completed_steps == ["DOWNLOADING", "EXTRACTING", "TRANSCRIBING"]  # 不被清掉


# ---------------------------------------------------------------------------
# API 层：retry + resume_options
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / "test.db")
    app.dependency_overrides[get_store] = lambda: store
    # tasks_routes 与 orchestrator 都引用了 task_dir，resume_options / validate_start_from
    # 走 orchestrator 那个绑定，因此也要在 orchestrator 上打 patch。
    monkeypatch.setattr(tasks_routes, "task_dir", lambda tid: tmp_path / tid)
    monkeypatch.setattr(orchestrator, "task_dir", lambda tid: tmp_path / tid)
    enqueued: list = []

    def fake_enqueue(task_id, start_from=None):
        enqueued.append((task_id, start_from))

    monkeypatch.setattr(tasks_routes, "enqueue_pipeline", fake_enqueue)
    with TestClient(app) as c:
        c._store = store
        c._tmp = tmp_path
        c._enqueued = enqueued
        yield c
    app.dependency_overrides.clear()


def test_retry_with_start_from_resets_and_enqueues(client):
    rec = client._store.create(
        url="http://x/v", source_lang="auto", target_lang="zh-CN",
        mode="mono", burn="hard", model="small", engine="deepseek",
    )
    client._store.update(rec.id, status="FAILED", error="oops",
                         completed_steps=["DOWNLOADING", "EXTRACTING"],
                         last_error_step="TRANSCRIBING")
    # 给产物做齐，TRANSLATING 可以安全开始
    d = client._tmp / rec.id
    d.mkdir(parents=True, exist_ok=True)
    (d / "source.mp4").write_bytes(b"VID")
    (d / "audio.wav").write_bytes(b"WAV")
    (d / "original.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n")

    r = client.post(f"/api/tasks/{rec.id}/retry?start_from=TRANSLATING")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "PENDING"
    assert body["error"] is None
    # start_from 传给 runner
    assert client._enqueued == [(rec.id, "TRANSLATING")]


def test_retry_without_start_from_keeps_old_behavior(client):
    rec = client._store.create(
        url="http://x/v", source_lang="auto", target_lang="zh-CN",
        mode="mono", burn="hard", model="small", engine="deepseek",
    )
    client._store.update(rec.id, status="FAILED", error="oops")

    r = client.post(f"/api/tasks/{rec.id}/retry")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "PENDING"
    # 旧行为：start_from 为 None
    assert client._enqueued == [(rec.id, None)]


def test_retry_with_invalid_start_from_returns_400(client):
    rec = client._store.create(
        url="http://x/v", source_lang="auto", target_lang="zh-CN",
        mode="mono", burn="hard", model="small", engine="deepseek",
    )
    client._store.update(rec.id, status="FAILED", error="oops")

    r = client.post(f"/api/tasks/{rec.id}/retry?start_from=NOPE")
    assert r.status_code == 400
    assert client._enqueued == []


def test_retry_with_missing_artifact_returns_409(client):
    """start_from 合法但前置产物缺失 -> 409，明确告诉客户端缺啥。"""
    rec = client._store.create(
        url="http://x/v", source_lang="auto", target_lang="zh-CN",
        mode="mono", burn="hard", model="small", engine="deepseek",
    )
    client._store.update(rec.id, status="FAILED", error="oops")
    # 缺所有产物

    r = client.post(f"/api/tasks/{rec.id}/retry?start_from=TRANSLATING")
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert "TRANSLATING" in detail
    assert client._enqueued == []


def test_resume_options_no_artifacts(client):
    """无任何产物：仅 DOWNLOADING 可用，其它都不可用 + 有 reason。"""
    rec = client._store.create(
        url="http://x/v", source_lang="auto", target_lang="zh-CN",
        mode="mono", burn="hard", model="small", engine="deepseek",
    )
    client._store.update(rec.id, status="FAILED", last_error_step="TRANSLATING",
                         completed_steps=["DOWNLOADING", "EXTRACTING"])

    r = client.get(f"/api/tasks/{rec.id}/resume_options")
    assert r.status_code == 200
    body = r.json()
    assert body["taskId"] == rec.id
    assert body["status"] == "FAILED"
    assert body["lastErrorStep"] == "TRANSLATING"
    assert body["completedSteps"] == ["DOWNLOADING", "EXTRACTING"]

    options = {o["step"]: o for o in body["options"]}
    assert options["DOWNLOADING"]["available"] is True
    assert options["DOWNLOADING"]["reason"] is None
    for step in ("EXTRACTING", "TRANSCRIBING", "TRANSLATING", "BURNING"):
        assert options[step]["available"] is False
        assert options[step]["reason"]


def test_resume_options_with_artifacts(client):
    """source.mp4 + audio.wav + original.srt 在：TRANSLATING 可用；BURNING 因缺 translated.srt 不可。"""
    rec = client._store.create(
        url="http://x/v", source_lang="auto", target_lang="zh-CN",
        mode="mono", burn="hard", model="small", engine="deepseek",
    )
    d = client._tmp / rec.id
    d.mkdir(parents=True, exist_ok=True)
    (d / "source.mp4").write_bytes(b"VID")
    (d / "audio.wav").write_bytes(b"WAV")
    (d / "original.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n")
    client._store.update(rec.id, status="FAILED", last_error_step="TRANSLATING")

    r = client.get(f"/api/tasks/{rec.id}/resume_options")
    body = r.json()
    options = {o["step"]: o for o in body["options"]}
    assert options["DOWNLOADING"]["available"] is True
    assert options["EXTRACTING"]["available"] is True
    assert options["TRANSCRIBING"]["available"] is True
    assert options["TRANSLATING"]["available"] is True
    assert options["BURNING"]["available"] is False
    assert "translated.srt" in options["BURNING"]["reason"]


def test_resume_options_includes_all_steps(client):
    """options 列表必须覆盖全部 PIPELINE_STEPS（前端按它渲染菜单）。"""
    rec = client._store.create(
        url="http://x/v", source_lang="auto", target_lang="zh-CN",
        mode="mono", burn="hard", model="small", engine="deepseek",
    )
    r = client.get(f"/api/tasks/{rec.id}/resume_options")
    body = r.json()
    step_names = [o["step"] for o in body["options"]]
    assert step_names == list(PIPELINE_STEPS)


def test_resume_options_need_subtitle_false(client):
    """仅下载任务：只返回 DOWNLOADING 一个选项。"""
    rec = client._store.create(
        url="http://x/v", source_lang="auto", target_lang="zh-CN",
        mode="mono", burn="hard", model="small", engine="deepseek",
        need_subtitle=False,
    )
    r = client.get(f"/api/tasks/{rec.id}/resume_options")
    body = r.json()
    assert [o["step"] for o in body["options"]] == ["DOWNLOADING"]


def test_resume_options_missing_task_returns_404(client):
    r = client.get("/api/tasks/nope/resume_options")
    assert r.status_code == 404
