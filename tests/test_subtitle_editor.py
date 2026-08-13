"""字幕编辑路由（parse / save / re-burn）单测。

不真跑 ffmpeg：re-burn 通过 monkeypatch 替换 burn_subtitles。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.handler import subtitle_editor as editor_routes
from src.handler.app import app
from src.handler.deps import get_store
from src.store import TaskStore


@pytest.fixture
def client(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / "test.db")
    app.dependency_overrides[get_store] = lambda: store
    # 把 task_dir 指向临时根，避免污染真实 data/
    monkeypatch.setattr(editor_routes, "task_dir", lambda tid: tmp_path / tid)

    # 创建一条带字幕的任务
    cid = store.create(
        url="https://example.com/v",
        source_lang="en",
        target_lang="zh-CN",
        mode="mono",
        burn="hard",
        model="small",
        engine="deepseek",
    ).id
    d = tmp_path / cid
    d.mkdir(parents=True, exist_ok=True)
    (d / "original.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,500\nHello\n\n"
        "2\n00:00:01,500 --> 00:00:03,000\nWorld\n",
        encoding="utf-8",
    )
    (d / "translated.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,500\n你好\n\n"
        "2\n00:00:01,500 --> 00:00:03,000\n世界\n",
        encoding="utf-8",
    )

    with TestClient(app) as c:
        c._store = store
        c._tid = cid
        c._tmp = tmp_path
        yield c
    app.dependency_overrides.clear()


# ---------- GET /api/tasks/{id}/subtitles ----------

def test_get_subtitles_returns_both_locales(client):
    r = client.get(f"/api/tasks/{client._tid}/subtitles")
    assert r.status_code == 200
    data = r.json()
    assert data["taskId"] == client._tid
    assert data["hasOriginal"] is True
    assert data["hasTranslated"] is True
    assert len(data["original"]) == 2
    assert len(data["translated"]) == 2
    # 字段命名对齐前端
    assert data["original"][0]["text"] == "Hello"
    assert data["original"][0]["start"] == 0.0
    assert data["original"][0]["end"] == 1.5
    # 每条都应有 id 字段供前端稳定引用
    assert all("id" in e and e["id"].startswith("sub_") for e in data["original"])


def test_get_subtitles_409_when_neither_exists(tmp_path):
    """两个 SRT 都不存在时返回 409，提示用户先跑流水线。"""
    store = TaskStore(tmp_path / "test.db")
    app.dependency_overrides[get_store] = lambda: store

    cid = store.create(
        url="https://example.com/v",
        source_lang="en",
        target_lang="zh-CN",
        mode="mono",
        burn="hard",
        model="small",
        engine="deepseek",
    ).id
    (tmp_path / cid).mkdir(parents=True, exist_ok=True)
    # 不写任何 srt

    with TestClient(app) as c:
        r = c.get(f"/api/tasks/{cid}/subtitles")
    assert r.status_code == 409
    app.dependency_overrides.clear()


def test_get_subtitles_only_original(client, tmp_path):
    """只生成 original 时，translated 数组为空但 hasTranslated=False。"""
    (tmp_path / client._tid / "translated.srt").unlink()
    r = client.get(f"/api/tasks/{client._tid}/subtitles")
    assert r.status_code == 200
    data = r.json()
    assert data["hasOriginal"] is True
    assert data["hasTranslated"] is False
    assert data["translated"] == []


def test_get_subtitles_404(client):
    assert client.get("/api/tasks/nope/subtitles").status_code == 404


# ---------- PUT /api/tasks/{id}/subtitles ----------

def test_save_subtitles_overwrites_default(client):
    """不传 version 时，覆盖 translated.srt。"""
    body = {
        "locale": "translated",
        "entries": [
            {"id": "a1", "index": 1, "start": 0.0, "end": 1.0, "text": "早上好"},
            {"id": "a2", "index": 2, "start": 1.0, "end": 2.0, "text": "再见"},
        ],
    }
    r = client.put(f"/api/tasks/{client._tid}/subtitles", json=body)
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["path"] == "translated.srt"
    assert data["count"] == 2

    # 落盘文件被覆盖
    content = (client._tmp / client._tid / "translated.srt").read_text(encoding="utf-8")
    assert "早上好" in content
    assert "再见" in content
    # 旧内容已不再出现（"你好" 是 fixture 里的旧文本）
    assert "你好" not in content

    # 再次 GET 应当读到新内容
    r2 = client.get(f"/api/tasks/{client._tid}/subtitles")
    texts = [e["text"] for e in r2.json()["translated"]]
    assert texts == ["早上好", "再见"]


def test_save_subtitles_version_writes_sidecar(client):
    """传 version=v2 时，写入 translated.v2.srt，原文件保留。"""
    body = {
        "locale": "translated",
        "version": "v2",
        "entries": [
            {"id": "x", "index": 1, "start": 0.0, "end": 1.0, "text": "新版本"},
        ],
    }
    r = client.put(f"/api/tasks/{client._tid}/subtitles", json=body)
    assert r.status_code == 200
    assert r.json()["path"] == "translated.v2.srt"

    d = client._tmp / client._tid
    assert (d / "translated.v2.srt").exists()
    # 原文件保留
    assert (d / "translated.srt").exists()
    assert "世界" in (d / "translated.srt").read_text(encoding="utf-8")


def test_save_subtitles_sorts_by_start(client):
    """前端发乱序条目，磁盘上也应按 start 升序写回。"""
    body = {
        "locale": "translated",
        "entries": [
            {"id": "2", "index": 99, "start": 5.0, "end": 6.0, "text": "later"},
            {"id": "1", "index": 1, "start": 0.5, "end": 1.0, "text": "earlier"},
        ],
    }
    r = client.put(f"/api/tasks/{client._tid}/subtitles", json=body)
    assert r.status_code == 200

    content = (client._tmp / client._tid / "translated.srt").read_text(encoding="utf-8")
    assert content.index("earlier") < content.index("later")


def test_save_subtitles_rejects_invalid_locale(client):
    body = {
        "locale": "junk",
        "entries": [{"id": "1", "index": 1, "start": 0.0, "end": 1.0, "text": "x"}],
    }
    r = client.put(f"/api/tasks/{client._tid}/subtitles", json=body)
    assert r.status_code == 422


def test_save_subtitles_rejects_end_before_start(client):
    body = {
        "locale": "translated",
        "entries": [
            {"id": "1", "index": 1, "start": 5.0, "end": 1.0, "text": "x"},
        ],
    }
    r = client.put(f"/api/tasks/{client._tid}/subtitles", json=body)
    assert r.status_code == 422


def test_save_subtitles_rejects_unsafe_version(client):
    body = {
        "locale": "translated",
        "version": "../../etc/passwd",
        "entries": [{"id": "1", "index": 1, "start": 0.0, "end": 1.0, "text": "x"}],
    }
    r = client.put(f"/api/tasks/{client._tid}/subtitles", json=body)
    assert r.status_code == 422


def test_save_subtitles_404(client):
    r = client.put(
        "/api/tasks/nope/subtitles",
        json={"locale": "translated", "entries": []},
    )
    assert r.status_code == 404


# ---------- POST /api/tasks/{id}/subtitles/burn ----------

def test_reburn_calls_burn_subtitles(client, tmp_path, monkeypatch):
    """re-burn 应基于 translated.srt + source.* 调用 burn_subtitles。"""
    called = {}

    def fake_burn(video, srt, task_id, on_progress=None, *, mode="hard"):
        called["video"] = str(video)
        called["srt"] = str(srt)
        called["task_id"] = task_id
        called["mode"] = mode
        # 模拟生成产物
        out = tmp_path / task_id / "output.mp4"
        out.write_bytes(b"NEW-VIDEO")
        from src.core.subtitle_burner import BurnResult
        return BurnResult(output_path=out, mode=mode, filesize=out.stat().st_size)

    monkeypatch.setattr(editor_routes, "burn_subtitles", fake_burn)

    # 准备 source.mp4，否则路由会 409
    (tmp_path / client._tid / "source.mp4").write_bytes(b"OLD")

    r = client.post(f"/api/tasks/{client._tid}/subtitles/burn", json={})
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["outputPath"] == "output.mp4"
    assert called["video"].endswith("source.mp4")
    assert called["srt"].endswith("translated.srt")
    # 不传 mode 时按任务的 burn 设置（默认 hard）
    assert called["mode"] == "hard"

    # DB 里 output_video 字段也被刷新
    rec = client._store.get(client._tid)
    assert rec.output_video is not None
    assert rec.output_video.endswith("output.mp4")


def test_reburn_accepts_mode_override(client, tmp_path, monkeypatch):
    """请求体里的 mode 覆盖任务默认 burn 设置。"""
    called = {}

    def fake_burn(video, srt, task_id, on_progress=None, *, mode="hard"):
        called["mode"] = mode
        from src.core.subtitle_burner import BurnResult
        out = tmp_path / task_id / "output.mp4"
        out.write_bytes(b"x")
        return BurnResult(output_path=out, mode=mode, filesize=1)

    monkeypatch.setattr(editor_routes, "burn_subtitles", fake_burn)
    (tmp_path / client._tid / "source.mp4").write_bytes(b"OLD")

    r = client.post(f"/api/tasks/{client._tid}/subtitles/burn", json={"mode": "soft"})
    assert r.status_code == 200
    assert called["mode"] == "soft"


def test_reburn_409_when_no_srt(client, tmp_path):
    (client._tmp / client._tid / "translated.srt").unlink()
    (tmp_path / client._tid / "source.mp4").write_bytes(b"OLD")
    r = client.post(f"/api/tasks/{client._tid}/subtitles/burn", json={})
    assert r.status_code == 409


def test_reburn_409_when_no_source(client):
    r = client.post(f"/api/tasks/{client._tid}/subtitles/burn", json={})
    assert r.status_code == 409


def test_reburn_400_on_burn_error(client, tmp_path, monkeypatch):
    """burn_subtitles 抛 BurnError 时路由应返回 400。"""
    from src.core.subtitle_burner import BurnError

    def fake_burn(video, srt, task_id, on_progress=None, *, mode="hard"):
        raise BurnError("测试失败")

    monkeypatch.setattr(editor_routes, "burn_subtitles", fake_burn)
    (tmp_path / client._tid / "source.mp4").write_bytes(b"OLD")

    r = client.post(f"/api/tasks/{client._tid}/subtitles/burn", json={})
    assert r.status_code == 400
    assert "测试失败" in r.json()["detail"]


def test_write_locks_leak_cleanup(client, monkeypatch):
    """测试创建任务并保存字幕后触发锁生成，然后删除任务或清理任务，验证 _write_locks 是否成功回收。"""
    from src.handler.subtitle_editor import _write_locks, _lock_for
    from src.handler import storage as storage_routes

    # 重写 task_dir 为 fake_dir 以便 storage.py 能正确定位到 tmp_path 目录下的任务
    def fake_dir(tid: str):
        return client._tmp / tid
    monkeypatch.setattr(storage_routes, "task_dir", fake_dir)

    # 1. 直接触发锁创建
    task_id = client._tid
    lock = _lock_for(task_id)
    assert task_id in _write_locks

    # 2. 调用 DELETE 接口删除任务，验证锁已被移除
    r = client.delete(f"/api/tasks/{task_id}")
    assert r.status_code == 204
    assert task_id not in _write_locks

    # 3. 再创建一个新任务，测试 storage/cleanup 清理是否也回收锁
    new_tid = client._store.create(
        url="https://example.com/v2",
        source_lang="en",
        target_lang="zh-CN",
        mode="mono",
        burn="hard",
        model="small",
        engine="deepseek",
        title="another_task"
    ).id
    client._store.update(new_tid, status="SUCCESS", progress=100)

    # 建立对应目录和产物
    d = client._tmp / new_tid
    d.mkdir(parents=True, exist_ok=True)
    from src.config import SOURCE_VIDEO_STEM
    (d / f"{SOURCE_VIDEO_STEM}.mp4").write_bytes(b"VIDEO")

    # 触发锁创建
    _lock_for(new_tid)
    assert new_tid in _write_locks

    # 调用 POST /api/storage/cleanup
    cleanup_payload = {"taskIds": [new_tid]}
    r_cleanup = client.post("/api/storage/cleanup", json=cleanup_payload)
    assert r_cleanup.status_code == 200
    assert r_cleanup.json()["deletedTasks"] == 1

    # 验证锁字典已经被清除了该任务的锁，防止内存泄漏
    assert new_tid not in _write_locks
