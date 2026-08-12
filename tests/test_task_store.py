"""任务存储（SQLite）单测。"""

from __future__ import annotations

import time

import pytest

from src.store import TaskRecord, TaskStore


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


# ---------- 链路新增字段：source_type / need_subtitle / title ----------

def test_create_persists_need_subtitle_false(store):
    """need_subtitle=False 入库应为 0；回读后 bool() 为 False。"""
    rec = _create(store, need_subtitle=False)
    got = store.get(rec.id)
    assert got.need_subtitle == 0
    assert bool(got.need_subtitle) is False


def test_create_persists_need_subtitle_true_by_default(store):
    """默认值应为 1（True）。"""
    rec = _create(store)
    got = store.get(rec.id)
    assert got.need_subtitle == 1
    assert bool(got.need_subtitle) is True


def test_create_persists_source_type_url_by_default(store):
    rec = _create(store)
    assert rec.source_type == "url"
    assert store.get(rec.id).source_type == "url"


def test_create_persists_source_type_upload(store):
    rec = _create(store, source_type="upload")
    got = store.get(rec.id)
    assert got.source_type == "upload"


def test_create_persists_title(store):
    rec = _create(store, title="My Clip")
    assert store.get(rec.id).title == "My Clip"


def test_create_title_none_by_default(store):
    rec = _create(store)
    assert rec.title is None


def test_update_need_subtitle(store):
    """可单独更新 need_subtitle 字段。"""
    rec = _create(store)
    updated = store.update(rec.id, need_subtitle=0)
    assert updated.need_subtitle == 0
    assert store.get(rec.id).need_subtitle == 0


def test_update_source_type(store):
    rec = _create(store)
    updated = store.update(rec.id, source_type="upload")
    assert updated.source_type == "upload"


def test_update_title(store):
    rec = _create(store)
    updated = store.update(rec.id, title="New Title")
    assert updated.title == "New Title"


def test_to_dict_roundtrip_fields(store):
    """TaskRecord.to_dict 字段集合应包含链路新增的 source_type / need_subtitle / title。"""
    rec = _create(store, source_type="upload", need_subtitle=False, title="X")
    d = rec.to_dict()
    for f in ("source_type", "need_subtitle", "title"):
        assert f in d
    assert d["source_type"] == "upload" and d["need_subtitle"] == 0 and d["title"] == "X"


# ---------- 旧库 schema 迁移 ----------

def test_migration_adds_new_columns_to_legacy_schema(tmp_path):
    """模拟不带 need_subtitle / source_type 列的旧库，初始化时自动 ALTER 补列。"""
    import sqlite3

    db = tmp_path / "legacy.db"
    # 用原生 sqlite3 复刻旧 schema（不带链路新增的两列）
    conn = sqlite3.connect(db)
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
        "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("task_legacy", "u", "auto", "zh-CN", "mono", "hard", "small", "deepseek",
         "PENDING", 0, None, "legacy", None, None, None, 1, 1),
    )
    conn.commit()
    conn.close()

    # 初始化时自动 ALTER 补列
    s = TaskStore(db)
    cols = {r["name"] for r in s._connect().execute("PRAGMA table_info(tasks)")}
    assert {"need_subtitle", "source_type"} <= cols

    # 旧记录能读出来，且新列拿到默认值
    rec = s.get("task_legacy")
    assert rec is not None
    assert rec.need_subtitle == 1
    assert rec.source_type == "url"
    assert rec.title == "legacy"


def test_migration_is_idempotent(tmp_path):
    """重复初始化同一 DB 不应报错。"""
    db = tmp_path / "app.db"
    TaskStore(db)
    TaskStore(db)  # 不应抛
    s = TaskStore(db)
    cols = {r["name"] for r in s._connect().execute("PRAGMA table_info(tasks)")}
    # 新列保留、且只有一份
    assert "need_subtitle" in cols and "source_type" in cols
    count = sum(
        1 for r in s._connect().execute("PRAGMA table_info(tasks)")
        if r["name"] in ("need_subtitle", "source_type")
    )
    assert count == 2


def test_create_with_legacy_url_source_type_persists_through_migration(tmp_path):
    """迁移后再 create upload 任务，原 url 行不应被影响。"""
    import sqlite3

    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY, url TEXT NOT NULL, source_lang TEXT NOT NULL,
            target_lang TEXT NOT NULL, mode TEXT NOT NULL, burn TEXT NOT NULL,
            model TEXT NOT NULL, engine TEXT NOT NULL, status TEXT NOT NULL,
            progress INTEGER NOT NULL, current_step TEXT, title TEXT, error TEXT,
            output_video TEXT, output_subtitle TEXT, created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("task_old", "https://x", "auto", "zh-CN", "mono", "hard", "small",
         "deepseek", "PENDING", 0, None, None, None, None, None, 1, 1),
    )
    conn.commit()
    conn.close()

    s = TaskStore(db)
    new_rec = s.create(
        url="clip.mp4", source_lang="auto", target_lang="zh-CN",
        mode="mono", burn="hard", model="small", engine="deepseek",
        source_type="upload", need_subtitle=False, title="clip",
    )
    # 新行能落表
    assert s.get(new_rec.id).source_type == "upload"
    # 旧行仍可读
    assert s.get("task_old").url == "https://x"
