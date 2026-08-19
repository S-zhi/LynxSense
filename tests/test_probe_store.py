"""下载测试记录（probe_records）单测。

覆盖：CRUD 完整性、limit 行为、单条/清空删除、与 TaskStore 共库不冲突。
"""

from __future__ import annotations

import time

import pytest

from src.store import ProbeStore, TaskStore


@pytest.fixture
def store(tmp_path):
    return ProbeStore(tmp_path / "app.db")


def _record(store: ProbeStore, **over):
    fields = dict(url="https://example.com/v", ok=True, title="T", formats_count=3)
    fields.update(over)
    return store.record(**fields)


def test_empty(store):
    assert store.list() == []
    assert store.list(limit=10) == []


def test_record_roundtrip(store):
    rec = _record(
        store,
        url="https://x.com/abc",
        ok=False,
        reason="login required",
        detail="Need cookies",
    )
    assert rec.id.startswith("probe_")
    assert rec.ok == 0
    assert rec.url == "https://x.com/abc"
    assert rec.reason == "login required"
    assert rec.detail == "Need cookies"
    assert rec.created_at > 0
    got = store.get(rec.id)
    assert got == rec


def test_list_orders_by_created_desc(store):
    # created_at 截到毫秒，连续插入易同毫秒导致 SQL 排序顺序未定义；用 sleep 拉开
    a = _record(store, url="https://a")
    time.sleep(0.005)
    b = _record(store, url="https://b")
    time.sleep(0.005)
    c = _record(store, url="https://c")
    # 三条按时间倒序
    assert [r.id for r in store.list()] == [c.id, b.id, a.id]


def test_list_respects_limit(store):
    for i in range(5):
        _record(store, url=f"https://x/{i}")
    assert len(store.list(limit=2)) == 2
    # limit<=0 视为不限制
    assert len(store.list(limit=0)) == 5
    assert len(store.list(limit=-1)) == 5


def test_delete_returns_bool(store):
    r = _record(store)
    assert store.delete(r.id) is True
    # 二次删除应返回 False（不存在）
    assert store.delete(r.id) is False
    assert store.list() == []


def test_clear_returns_count(store):
    for i in range(3):
        _record(store, url=f"https://x/{i}")
    assert store.clear() == 3
    assert store.list() == []
    # 空表清空返回 0
    assert store.clear() == 0


def test_record_deduplication_within_same_hour(store, monkeypatch):
    """同一 URL 在同一小时内连续探测会覆盖更新现有记录，而不增加总行数。"""
    url = "https://example.com/same_video"
    rec1 = store.record(url=url, ok=False, reason="error 1")
    assert len(store.list()) == 1
    assert rec1.reason == "error 1"

    rec2 = store.record(url=url, ok=True, title="Fixed Video Title", formats_count=5)
    records = store.list()
    assert len(records) == 1
    assert records[0].id == rec1.id
    assert records[0].ok == 1
    assert records[0].title == "Fixed Video Title"
    assert records[0].formats_count == 5


def test_record_inserts_new_row_across_different_hours(store, monkeypatch):
    """同一 URL 在不同小时调用，会新增记录。"""
    url = "https://example.com/diff_hour_video"
    now_ms = 1700000000000

    monkeypatch.setattr("src.store.probe_store._now_ms", lambda: now_ms)
    rec1 = store.record(url=url, ok=True, title="Hour 1")

    # 1.5 小时后 (5400000 ms)
    monkeypatch.setattr("src.store.probe_store._now_ms", lambda: now_ms + 5400000)
    rec2 = store.record(url=url, ok=True, title="Hour 2")

    records = store.list()
    assert len(records) == 2
    assert rec1.id != rec2.id


def test_cleanup_older_than_days(store, monkeypatch):
    url = "https://example.com/old"
    now_ms = int(time.time() * 1000)

    # 10 天前
    monkeypatch.setattr("src.store.probe_store._now_ms", lambda: now_ms - 10 * 86400 * 1000)
    r1 = store.record(url=url + "/1", ok=True)

    # 2 天前
    monkeypatch.setattr("src.store.probe_store._now_ms", lambda: now_ms - 2 * 86400 * 1000)
    r2 = store.record(url=url + "/2", ok=True)

    # 当前
    monkeypatch.setattr("src.store.probe_store._now_ms", lambda: now_ms)

    # 清理 5 天前的记录，应清掉 10 天前的 r1
    deleted = store.cleanup_older_than_days(5)
    assert deleted == 1

    remaining = store.list()
    assert len(remaining) == 1
    assert remaining[0].id == r2.id


def test_coexists_with_task_store(tmp_path):
    """probe_records 与 tasks 共用 db 文件：两个 store 同时存在不应互相影响。"""
    p = tmp_path / "shared.db"
    probe = ProbeStore(p)
    task = TaskStore(p)
    probe.record(url="https://probe/x", ok=True)
    task.create(
        url="https://task/x",
        source_lang="en",
        target_lang="zh-CN",
        mode="mono",
        burn="hard",
        model="small",
        engine="deepseek",
    )
    assert len(probe.list()) == 1
    assert len(task.list()) == 1
    # 清 probe 不应影响 task
    probe.clear()
    assert probe.list() == []
    assert len(task.list()) == 1
