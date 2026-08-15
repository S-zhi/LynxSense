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
