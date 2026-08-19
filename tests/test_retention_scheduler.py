"""Retention scheduler unit tests.
"""

from __future__ import annotations

import time
import pytest

from src.config import AUDIO_FILENAME, SOURCE_VIDEO_STEM, TRANSLATED_SRT
from src.handler import storage as storage_routes
from src.service.retention_scheduler import (
    execute_retention_cleanup,
    start_retention_scheduler,
    _scheduler_thread,
)
from src.store import ProbeStore, TaskStore


def test_execute_retention_cleanup_when_none_or_zero(tmp_path, monkeypatch):
    db_p = tmp_path / "test.db"
    store = TaskStore(db_p)
    probe_store = ProbeStore(db_p)

    monkeypatch.setattr(storage_routes, "_retention_path", lambda: tmp_path / ".retention.json")

    # Retention days is None -> no cleanup performed
    res = execute_retention_cleanup(store, probe_store)
    assert res is None


def test_execute_retention_cleanup_deletes_expired_and_updates_last_run(tmp_path, monkeypatch):
    db_p = tmp_path / "test.db"
    store = TaskStore(db_p)
    probe_store = ProbeStore(db_p)

    monkeypatch.setattr(storage_routes, "_retention_path", lambda: tmp_path / ".retention.json")
    def fake_dir(tid: str):
        return tmp_path / tid
    monkeypatch.setattr(storage_routes, "task_dir", fake_dir)

    # Set retention days = 7
    storage_routes._save_retention(storage_routes.RetentionOut(days=7, updatedAt=int(time.time() * 1000)))

    # Create old expired task (10 days old)
    rec_old = store.create(
        url="https://example.com/v1",
        source_lang="en", target_lang="zh-CN",
        mode="mono", burn="hard", model="small", engine="deepseek", title="old_task",
    )
    past_ms = int((time.time() - 10 * 86400) * 1000)
    store.update(rec_old.id, created_at=past_ms, updated_at=past_ms, status="SUCCESS")
    d_old = tmp_path / rec_old.id
    d_old.mkdir(parents=True, exist_ok=True)
    (d_old / f"{SOURCE_VIDEO_STEM}.mp4").write_bytes(b"\0" * 1024)

    # Create fresh task (1 day old)
    rec_new = store.create(
        url="https://example.com/v2",
        source_lang="en", target_lang="zh-CN",
        mode="mono", burn="hard", model="small", engine="deepseek", title="new_task",
    )
    store.update(rec_new.id, status="SUCCESS")
    d_new = tmp_path / rec_new.id
    d_new.mkdir(parents=True, exist_ok=True)
    (d_new / f"{SOURCE_VIDEO_STEM}.mp4").write_bytes(b"\0" * 512)

    # Create running task (10 days old) - should be skipped
    rec_run = store.create(
        url="https://example.com/v3",
        source_lang="en", target_lang="zh-CN",
        mode="mono", burn="hard", model="small", engine="deepseek", title="running_task",
    )
    store.update(rec_run.id, created_at=past_ms, updated_at=past_ms, status="TRANSCRIBING")
    d_run = tmp_path / rec_run.id
    d_run.mkdir(parents=True, exist_ok=True)
    (d_run / f"{SOURCE_VIDEO_STEM}.mp4").write_bytes(b"\0" * 2048)

    res = execute_retention_cleanup(store, probe_store)

    assert res is not None
    assert res.deletedTasks == 1
    assert store.get(rec_old.id) is None
    assert not d_old.exists()

    assert store.get(rec_new.id) is not None
    assert d_new.exists()

    assert store.get(rec_run.id) is not None
    assert d_run.exists()

    # Check retention json updated with lastRunAt
    ret = storage_routes._load_retention()
    assert ret.days == 7
    assert ret.lastRunAt is not None
    assert ret.lastRunAt > 0


def test_start_retention_scheduler(tmp_path):
    start_retention_scheduler(check_interval_sec=3600.0)
    from src.service.retention_scheduler import _scheduler_thread
    assert _scheduler_thread is not None
    assert _scheduler_thread.is_alive()
