"""资源治理（storage）API 单测。

- stats：在临时目录构造样本后断言统计正确
- cleanup_preview + cleanup：清理跳过 RUNNING 任务；执行后状态正确同步
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from src.config import (
    AUDIO_FILENAME,
    OUTPUT_VIDEO,
    ORIGINAL_SRT,
    SOURCE_VIDEO_STEM,
    TRANSLATED_SRT,
    task_dir,
)
from src.handler import storage as storage_routes
from src.handler import tasks as tasks_routes
from src.handler.app import app
from src.handler.deps import get_store
from src.store import TaskStore


# ---------- 工具 ----------

@pytest.fixture
def client(tmp_path, monkeypatch):
    """API 测试夹具：临时 DB + 临时产物根目录。"""
    store = TaskStore(tmp_path / "test.db")
    app.dependency_overrides[get_store] = lambda: store

    # 把 task_dir 重定向到 tmp_path（与现有 tasks 测试一致）
    def fake_dir(tid: str):
        return tmp_path / tid
    monkeypatch.setattr(tasks_routes, "task_dir", fake_dir)
    # storage 路由自己 import 了 task_dir，也需要 patch
    monkeypatch.setattr(storage_routes, "task_dir", fake_dir)
    # 保留策略文件落点重定向到 tmp_path（settings 是 frozen dataclass，
    # 不能直接 setattr，所以覆盖 _retention_path 即可）
    monkeypatch.setattr(storage_routes, "_retention_path", lambda: tmp_path / ".retention.json")
    # 不真跑流水线
    monkeypatch.setattr(tasks_routes, "enqueue_pipeline", lambda task_id: None)

    with TestClient(app) as c:
        c._store = store
        c._tmp = tmp_path
        yield c
    app.dependency_overrides.clear()


def _seed_task(
    client, store: TaskStore, *,
    title: str = "Sample",
    status: str = "SUCCESS",
    progress: int = 100,
    source_bytes: int = 1024,
    audio_bytes: int = 512,
    srt_bytes: int = 128,
    output_bytes: int = 4096,
    age_days: float = 0.0,
) -> str:
    """创建一个任务并按指定大小落盘产物，便于 stats 断言。"""
    rec = store.create(
        url="https://example.com/v",
        source_lang="auto", target_lang="zh-CN",
        mode="mono", burn="hard", model="small", engine="deepseek",
        title=title,
    )
    # 可选：把 created_at 倒推，模拟"老任务"
    if age_days > 0:
        past = int((time.time() - age_days * 86400) * 1000)
        store.update(rec.id, created_at=past, updated_at=past)
    if status:
        store.update(rec.id, status=status, progress=progress)
    d = client._tmp / rec.id
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{SOURCE_VIDEO_STEM}.mp4").write_bytes(b"\0" * source_bytes)
    (d / AUDIO_FILENAME).write_bytes(b"\0" * audio_bytes)
    (d / ORIGINAL_SRT).write_bytes(b"\0" * srt_bytes)
    if status == "SUCCESS":
        (d / TRANSLATED_SRT).write_bytes(b"\0" * srt_bytes)
        (d / OUTPUT_VIDEO).write_bytes(b"\0" * output_bytes)
    return rec.id


def _post_cleanup(client, **payload) -> dict:
    return client.post("/api/storage/cleanup", json=payload).json()


def test_cleanup_requires_api_token_when_configured(client, monkeypatch):
    import dataclasses
    from src.handler import deps as deps_mod
    monkeypatch.setattr(
        deps_mod,
        "settings",
        dataclasses.replace(deps_mod.settings, api_token="secret123"),
    )

    r1 = client.post("/api/storage/cleanup", json={})
    assert r1.status_code == 401

    r2 = client.post("/api/storage/cleanup", json={}, headers={"X-API-Token": "secret123"})
    assert r2.status_code == 200


def test_cleanup_rate_limiting(client, monkeypatch):
    from src.handler import deps as deps_mod
    deps_mod._cleanup_limiter.reset()

    # Trigger 10 requests -> success
    for i in range(10):
        r = client.post("/api/storage/cleanup", json={})
        assert r.status_code == 200

    # 11th request -> 429
    r11 = client.post("/api/storage/cleanup", json={})
    assert r11.status_code == 429
    assert "请求过于频繁" in r11.json()["detail"]

    deps_mod._cleanup_limiter.reset()


# ---------- stats ----------

def test_stats_aggregates_total_and_kinds(client):
    store = client._store
    # 任务 A：完整流水线产物
    _seed_task(client, store, title="A",
               source_bytes=1000, audio_bytes=200, srt_bytes=50, output_bytes=4000)
    # 任务 B：只有 source（早期/失败）
    _seed_task(client, store, title="B", status="FAILED", progress=40,
               source_bytes=500, audio_bytes=0, srt_bytes=0, output_bytes=0)
    # 注意：上面 _seed_task 会强制写 source.mp4；为模拟"没有 audio / srt"，
    # 我们直接删除那两个文件
    # 先看 B 的 id
    recs = store.list()
    rec_b = [r for r in recs if r.title == "B"][0]
    d = client._tmp / rec_b.id
    (d / AUDIO_FILENAME).unlink(missing_ok=True)
    (d / ORIGINAL_SRT).unlink(missing_ok=True)
    # TRANSLATED_SRT / OUTPUT_VIDEO 在 FAILED 时本来就不写

    r = client.get("/api/storage/stats")
    assert r.status_code == 200
    data = r.json()

    expected_total = 1000 + 200 + 50 + 50 + 4000 + 500
    assert data["totalBytes"] == expected_total
    assert data["totalTasks"] == 2
    # 都不在 RUNNING 集合里
    assert data["runnableTaskCount"] == 2

    by_kind = data["byKind"]
    # source：1000 + 500 = 1500
    assert by_kind["source"] == 1500
    # audio：200
    assert by_kind["audio"] == 200
    # original_srt：50（只有 A 有）
    assert by_kind["original_srt"] == 50
    # translated_srt：50（只有 A 有）
    assert by_kind["translated_srt"] == 50
    # output：4000（只有 A 有）
    assert by_kind["output"] == 4000

    # byTask 按时占用降序：A 5300 > B 500
    by_task = data["byTask"]
    assert by_task[0]["taskId"] != by_task[1]["taskId"]
    sizes = [t["size"] for t in by_task]
    assert sizes == sorted(sizes, reverse=True)
    # 任务 A 状态可读
    a_row = next(t for t in by_task if t["title"] == "A")
    assert a_row["status"] == "SUCCESS"
    assert a_row["artifactCount"] >= 4  # source/audio/srt/output


# ---------- cleanup_preview + cleanup ----------

def test_cleanup_preview_and_execute_skip_running(client):
    store = client._store
    # 三种状态：成功（可清）、运行中（必须跳过）、失败（可清）
    success_id = _seed_task(
        client, store, title="ok",
        status="SUCCESS", source_bytes=2000, audio_bytes=300, srt_bytes=40, output_bytes=800,
    )
    running_id = _seed_task(
        client, store, title="busy",
        status="DOWNLOADING", progress=10, source_bytes=1500, audio_bytes=0, srt_bytes=0, output_bytes=0,
    )
    failed_id = _seed_task(
        client, store, title="err",
        status="FAILED", source_bytes=900, audio_bytes=0, srt_bytes=0, output_bytes=0,
    )

    # 预览：2 个可清，1 个跳过
    pre = client.post("/api/storage/cleanup_preview", json={}).json()
    assert pre["matchedTasks"] == 2
    assert pre["matchedBytes"] > 0
    assert {t["taskId"] for t in pre["skippedTasks"]} == {running_id}
    target_ids = {t["taskId"] for t in pre["targets"]}
    assert target_ids == {success_id, failed_id}

    # 执行：清掉 2 个；运行中保留
    res = _post_cleanup(client, taskIds=[success_id, running_id, failed_id])
    assert res["deletedTasks"] == 2
    assert res["deletedBytes"] > 0
    assert {t["taskId"] for t in res["skippedTasks"]} == {running_id}

    # 验证：成功 / 失败任务被彻底清掉；运行中任务记录 + 目录都在
    assert store.get(success_id) is None
    assert not (client._tmp / success_id).exists()
    assert store.get(failed_id) is None
    assert not (client._tmp / failed_id).exists()

    running_rec = store.get(running_id)
    assert running_rec is not None
    assert running_rec.status == "DOWNLOADING"  # 状态原样保留
    assert (client._tmp / running_id).is_dir()  # 目录原样保留
    assert (client._tmp / running_id / f"{SOURCE_VIDEO_STEM}.mp4").exists()

    # 再调一次预览，应为空（无目标）
    pre2 = client.post("/api/storage/cleanup_preview", json={}).json()
    assert pre2["matchedTasks"] == 0


def test_cleanup_respects_kind_filter_and_keeps_task(client):
    """按 kind 过滤的清理：只删指定类别产物，任务记录保留。"""
    store = client._store
    cid = _seed_task(
        client, store, title="keepme",
        status="SUCCESS", source_bytes=200, audio_bytes=300, srt_bytes=40, output_bytes=800,
    )
    d = client._tmp / cid

    # 只清 audio
    res = _post_cleanup(client, kinds=["audio"])
    assert res["deletedTasks"] == 0  # 不是整任务清理
    assert res["deletedBytes"] == 300
    assert len(res["partial"]) == 1
    assert res["partial"][0]["taskId"] == cid

    # 任务记录还在；audio.wav 没了；其它产物仍在
    assert store.get(cid) is not None
    assert not (d / AUDIO_FILENAME).exists()
    assert (d / f"{SOURCE_VIDEO_STEM}.mp4").exists()
    assert (d / TRANSLATED_SRT).exists()
    assert (d / OUTPUT_VIDEO).exists()

    # 再按 olderThanDays=0 全部清：这次会走整任务清理路径
    res2 = _post_cleanup(client)
    assert res2["deletedTasks"] == 1
    assert store.get(cid) is None
    assert not d.exists()


def test_cleanup_re_validates_status_before_delete(client, monkeypatch):
    """预览与执行之间状态可能变化；执行时再校验一次 RUNNING。"""
    store = client._store
    cid = _seed_task(
        client, store, title="racy",
        status="SUCCESS", source_bytes=100, audio_bytes=0, srt_bytes=0, output_bytes=200,
    )

    # 预览时是 SUCCESS
    pre = client.post("/api/storage/cleanup_preview", json={"taskIds": [cid]}).json()
    assert pre["matchedTasks"] == 1

    # 在预览和执行之间，状态被改成 RUNNING
    store.update(cid, status="EXTRACTING", progress=15, current_step="EXTRACTING")

    res = _post_cleanup(client, taskIds=[cid])
    # 应被识别为"中途进入 RUNNING"，归到 skipped
    assert res["deletedTasks"] == 0
    assert {t["taskId"] for t in res["skippedTasks"]} == {cid}
    # 任务记录与目录都还在
    assert store.get(cid) is not None
    assert (client._tmp / cid).is_dir()


def test_retention_roundtrip(client):
    """保留策略的 get / put 闭环。"""
    assert client.get("/api/storage/retention").json() == {"days": None, "updatedAt": None}

    r = client.put("/api/storage/retention", json={"days": 7})
    assert r.status_code == 200
    out = r.json()
    assert out["days"] == 7
    assert out["updatedAt"] > 0

    got = client.get("/api/storage/retention").json()
    assert got["days"] == 7
    assert got["updatedAt"] == out["updatedAt"]


def test_cleanup_preview_older_than_days_filter(client):
    store = client._store
    fresh_id = _seed_task(client, store, title="new",
                          status="FAILED", source_bytes=10, age_days=0)
    old_id = _seed_task(client, store, title="old",
                        status="FAILED", source_bytes=10, age_days=10)

    pre = client.post("/api/storage/cleanup_preview", json={"olderThanDays": 3}).json()
    matched = {t["taskId"] for t in pre["targets"]}
    assert matched == {old_id}
    assert fresh_id not in matched
