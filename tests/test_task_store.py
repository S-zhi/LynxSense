"""任务存储（SQLite）单测。"""

from __future__ import annotations

import time

import pytest

from src.store import (
    RESOURCE_STATUS_AVAILABLE,
    RESOURCE_STATUS_MISSING,
    TaskRecord,
    TaskStore,
)


@pytest.fixture
def store(tmp_path):
    return TaskStore(tmp_path / "app.db")


def _create(store, **over):
    fields = dict(
        url="http://x/v",
        source_lang="auto",
        target_lang="zh-CN",
        mode="mono",
        burn="hard",
        model="small",
        engine="deepseek",
    )
    fields.update(over)
    return store.create(**fields)


def test_create_defaults(store):
    rec = _create(store)
    assert rec.id.startswith("task_")
    assert rec.status == "PENDING"
    assert rec.progress == 0
    assert rec.created_at > 0
    assert rec.created_at == rec.updated_at


def test_get_roundtrip(store):
    rec = _create(store)
    got = store.get(rec.id)
    assert got is not None
    assert got == rec  # dataclass 相等


def test_create_defaults_source_type_url(store):
    rec = _create(store)
    assert rec.source_type == "url"


def test_create_upload_persists_source_type_and_title(store):
    rec = _create(store, source_type="upload", title="my clip")
    got = store.get(rec.id)
    assert got.source_type == "upload"
    assert got.title == "my clip"


def test_get_missing(store):
    assert store.get("nope") is None


def test_list_newest_first(store):
    a = _create(store)
    time.sleep(0.002)
    b = _create(store)
    ids = [r.id for r in store.list()]
    assert ids[0] == b.id and ids[1] == a.id


def test_update_partial(store):
    rec = _create(store)
    before = rec.updated_at
    time.sleep(0.002)
    updated = store.update(rec.id, status="TRANSCRIBING", progress=42, current_step="TRANSCRIBING")
    assert updated.status == "TRANSCRIBING"
    assert updated.progress == 42
    assert updated.current_step == "TRANSCRIBING"
    assert updated.updated_at > before
    # url 等未传字段保持不变
    assert updated.url == rec.url


def test_update_outputs_and_title(store):
    rec = _create(store)
    updated = store.update(
        rec.id, status="SUCCESS", progress=100,
        title="My Video", output_video="/d/output.mp4", output_subtitle="/d/translated.srt",
    )
    assert updated.title == "My Video"
    assert updated.output_video == "/d/output.mp4"
    assert updated.output_subtitle == "/d/translated.srt"


def test_update_ignores_unknown_field(store):
    rec = _create(store)
    updated = store.update(rec.id, bogus="x", progress=5)
    assert updated.progress == 5
    assert not hasattr(updated, "bogus")


def test_update_missing_returns_none(store):
    assert store.update("nope", progress=1) is None


def test_delete(store):
    rec = _create(store)
    assert store.delete(rec.id) is True
    assert store.get(rec.id) is None
    assert store.delete(rec.id) is False


def test_persistence_across_instances(tmp_path):
    path = tmp_path / "app.db"
    s1 = TaskStore(path)
    rec = _create(s1)
    s2 = TaskStore(path)  # 新实例读同一文件
    assert s2.get(rec.id) == rec


# ---------- resource_status 字段（issue #22） ----------

def test_resource_status_default_available(store):
    """新创建的任务默认是 AVAILABLE（资源还在）。"""
    rec = _create(store)
    assert rec.resource_status == RESOURCE_STATUS_AVAILABLE


def test_resource_status_round_trip(store):
    """resource_status 写入后可读回，模拟服务重启场景。"""
    rec = _create(store)
    updated = store.update(rec.id, resource_status=RESOURCE_STATUS_MISSING, error="资源已删除")
    assert updated.resource_status == RESOURCE_STATUS_MISSING
    assert updated.error == "资源已删除"

    # 重新打开 store 模拟重启
    s2 = TaskStore(store.db_path)
    got = s2.get(rec.id)
    assert got is not None
    assert got.resource_status == RESOURCE_STATUS_MISSING
    assert got.error == "资源已删除"


def test_resource_status_migration_adds_column(tmp_path):
    """旧库没有 resource_status 列时，初始化应补上并默认 AVAILABLE。"""
    import sqlite3

    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    # 旧版表结构（没有 resource_status、source_type、need_subtitle 之外最新列的子集）
    conn.execute(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            url TEXT NOT NULL,
            source_lang TEXT NOT NULL,
            target_lang TEXT NOT NULL,
            mode TEXT NOT NULL,
            burn TEXT NOT NULL,
            model TEXT NOT NULL,
            engine TEXT NOT NULL,
            source_type TEXT NOT NULL DEFAULT 'url',
            need_subtitle INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL,
            progress INTEGER NOT NULL,
            current_step TEXT,
            title TEXT,
            error TEXT,
            output_video TEXT,
            output_subtitle TEXT,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO tasks (id, url, source_lang, target_lang, mode, burn, model, engine, "
        "status, progress, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "task_legacy1", "http://x/v", "auto", "zh-CN", "mono", "hard", "small", "deepseek",
            "SUCCESS", 100, 0, 0,
        ),
    )
    conn.commit()
    conn.close()

    # 触发迁移
    s = TaskStore(db_path)
    rec = s.get("task_legacy1")
    assert rec is not None
    assert rec.resource_status == RESOURCE_STATUS_AVAILABLE
    assert rec.status == "SUCCESS"  # 其它字段保持不变

