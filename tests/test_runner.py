"""执行器单测：验证 enqueue 提交、事件写库、失败兜底。mock run_pipeline，不真跑流水线。"""

from __future__ import annotations

import pytest

from src.service import runner
from src.service.orchestrator import PipelineEvent
from src.store import TaskStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    s = TaskStore(tmp_path / "t.db")
    monkeypatch.setattr(runner, "_store", s)  # 执行器用临时库
    return s


def _make_task(store) -> str:
    rec = store.create(
        url="http://x/v", source_lang="auto", target_lang="zh-CN",
        mode="mono", burn="hard", model="small", engine="deepseek",
    )
    return rec.id


def test_enqueue_submits_to_executor(monkeypatch):
    submitted = []

    class FakeExecutor:
        def submit(self, fn, *args):
            submitted.append((fn, args))

    monkeypatch.setattr(runner, "_executor", FakeExecutor())
    runner.enqueue_pipeline("task_x")
    assert submitted[0][0] is runner._run
    assert submitted[0][1] == ("task_x",)


def test_run_writes_progress_and_success(store, monkeypatch):
    tid = _make_task(store)

    def fake_pipeline(params, on_event, *, api_key=None):
        on_event(PipelineEvent("DOWNLOADING", 10, "DOWNLOADING"))
        on_event(PipelineEvent("TRANSCRIBING", 50, "TRANSCRIBING"))
        on_event(PipelineEvent(
            "SUCCESS", 100, None,
            title="My Video",
            outputs={"video": "/d/output.mp4", "subtitle": "/d/translated.srt"},
        ))

    monkeypatch.setattr(runner, "run_pipeline", fake_pipeline)
    runner._run(tid)

    rec = store.get(tid)
    assert rec.status == "SUCCESS"
    assert rec.progress == 100
    assert rec.title == "My Video"
    assert rec.output_video == "/d/output.mp4"
    assert rec.output_subtitle == "/d/translated.srt"


def test_run_passes_params_from_record(store, monkeypatch):
    rec = store.create(
        url="http://x/v", source_lang="en", target_lang="ja",
        mode="bilingual", burn="soft", model="medium", engine="deepseek",
    )
    seen = {}

    def fake_pipeline(params, on_event, *, api_key=None):
        seen["url"] = params.url
        seen["target"] = params.target_lang
        seen["mode"] = params.mode
        seen["burn"] = params.burn
        seen["model"] = params.model
        on_event(PipelineEvent("SUCCESS", 100, None, outputs={}))

    monkeypatch.setattr(runner, "run_pipeline", fake_pipeline)
    runner._run(rec.id)

    assert seen == {"url": "http://x/v", "target": "ja", "mode": "bilingual",
                    "burn": "soft", "model": "medium"}


def test_run_failure_persists_failed(store, monkeypatch):
    tid = _make_task(store)

    def boom(params, on_event, *, api_key=None):
        on_event(PipelineEvent("DOWNLOADING", 5, "DOWNLOADING"))
        on_event(PipelineEvent("FAILED", 5, "DOWNLOADING", error="下载失败", error_code="download_failed"))
        raise RuntimeError("下载失败")

    monkeypatch.setattr(runner, "run_pipeline", boom)
    runner._run(tid)  # 不应抛出

    rec = store.get(tid)
    assert rec.status == "FAILED"
    assert "下载失败" in rec.error
    assert rec.error_code == "download_failed"


def test_run_failure_fallback_when_no_event(store, monkeypatch):
    """run_pipeline 直接抛异常、没发 FAILED 事件时，兜底也要写 FAILED。"""
    tid = _make_task(store)

    def boom(params, on_event, *, api_key=None):
        raise RuntimeError("意外崩溃")

    monkeypatch.setattr(runner, "run_pipeline", boom)
    runner._run(tid)

    rec = store.get(tid)
    assert rec.status == "FAILED"


def test_run_missing_task_skips(store, monkeypatch):
    called = []
    monkeypatch.setattr(runner, "run_pipeline", lambda *a, **k: called.append(1))
    runner._run("nonexistent")
    assert not called  # 任务不存在时不应调用 run_pipeline


def test_cancel_pipeline_terminates_procs_and_updates_status(store):
    import subprocess
    tid = _make_task(store)
    proc = subprocess.Popen(["sleep", "10"])
    runner.register_process(tid, proc)

    ok = runner.cancel_pipeline(tid)
    assert ok is True
    rec = store.get(tid)
    assert rec.status == "CANCELLED"
    assert rec.error == "用户取消"
    proc.wait(timeout=2)
    assert proc.poll() is not None


def test_run_does_not_overwrite_cancelled_status(store, monkeypatch):
    tid = _make_task(store)
    store.update(tid, status="CANCELLED", error="用户取消")

    def boom(params, on_event, *, api_key=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(runner, "run_pipeline", boom)
    runner._run(tid)

    rec = store.get(tid)
    assert rec.status == "CANCELLED"
    assert rec.error == "用户取消"


def test_recover_interrupted_tasks_requeues_only_non_terminal(store, monkeypatch):
    running = _make_task(store)
    pending = _make_task(store)
    success = _make_task(store)
    failed = _make_task(store)
    store.update(running, status="TRANSLATING", progress=70, current_step="TRANSLATING")
    store.update(pending, status="PENDING")
    store.update(success, status="SUCCESS", progress=100)
    store.update(failed, status="FAILED", error="boom")

    submitted = []

    class FakeExecutor:
        def submit(self, fn, *args):
            submitted.append((fn, args))

    monkeypatch.setattr(runner, "_executor", FakeExecutor())

    recovered = runner.recover_interrupted_tasks()

    assert set(recovered) == {running, pending}
    assert {args for _, args in submitted} == {(running,), (pending,)}
