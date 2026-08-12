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
    # start_from 默认 None：与旧行为完全一致
    assert submitted[0][1] == ("task_x", None)


def test_run_writes_progress_and_success(store, monkeypatch):
    tid = _make_task(store)

    def fake_pipeline(params, on_event, *, api_key=None, start_from=None):
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

    def fake_pipeline(params, on_event, *, api_key=None, start_from=None):
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

    def boom(params, on_event, *, api_key=None, start_from=None):
        on_event(PipelineEvent("DOWNLOADING", 5, "DOWNLOADING"))
        on_event(PipelineEvent("FAILED", 5, "DOWNLOADING", error="下载失败"))
        raise RuntimeError("下载失败")

    monkeypatch.setattr(runner, "run_pipeline", boom)
    runner._run(tid)  # 不应抛出

    rec = store.get(tid)
    assert rec.status == "FAILED"
    assert "下载失败" in rec.error


def test_run_failure_fallback_when_no_event(store, monkeypatch):
    """run_pipeline 直接抛异常、没发 FAILED 事件时，兜底也要写 FAILED。"""
    tid = _make_task(store)

    def boom(params, on_event, *, api_key=None, start_from=None):
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
