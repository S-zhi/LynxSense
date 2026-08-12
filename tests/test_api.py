"""API 层单测。用 FastAPI TestClient + 隔离的临时 DB（依赖覆盖）。

执行（pipeline）在第 1 步是占位，所以这里只验证 CRUD / 文件下载 / 校验，
不涉及真实跑流水线。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.handler import tasks as tasks_routes
from src.handler.app import app
from src.handler.deps import get_store
from src.store import RESOURCE_STATUS_AVAILABLE, RESOURCE_STATUS_MISSING, TaskStore


@pytest.fixture
def client(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / "test.db")
    # 覆盖 store 依赖 -> 临时库
    app.dependency_overrides[get_store] = lambda: store
    # 下载端点用 task_dir 定位文件 -> 指向临时目录
    monkeypatch.setattr(tasks_routes, "task_dir", lambda tid: tmp_path / tid)
    # 不在 API 测试里真跑流水线（执行器单独测）
    monkeypatch.setattr(tasks_routes, "enqueue_pipeline", lambda task_id: None)
    with TestClient(app) as c:
        c._store = store
        c._tmp = tmp_path
        yield c
    app.dependency_overrides.clear()


def _payload(**over):
    body = {
        "url": "https://example.com/v",
        "sourceLang": "auto",
        "targetLang": "zh-CN",
        "mode": "mono",
        "burn": "hard",
        "model": "small",
        "engine": "deepseek",
    }
    body.update(over)
    return body


# ---------- 创建 ----------

def test_create_task(client):
    r = client.post("/api/tasks", json=_payload())
    assert r.status_code == 201
    data = r.json()
    assert data["id"].startswith("task_")
    assert data["status"] == "PENDING"
    assert data["progress"] == 0
    # camelCase 字段对齐前端
    assert data["sourceLang"] == "auto"
    assert data["targetLang"] == "zh-CN"
    assert data["outputs"] is None
    assert data["createdAt"] > 0


def test_create_defaults_when_minimal(client):
    r = client.post("/api/tasks", json={"url": "https://x/y"})
    assert r.status_code == 201
    data = r.json()
    assert data["targetLang"] == "zh-CN" and data["mode"] == "mono"


def test_create_missing_url_422(client):
    r = client.post("/api/tasks", json={"targetLang": "zh-CN"})
    assert r.status_code == 422


def test_create_rejects_invalid_enum_params(client, monkeypatch):
    """非法枚举参数应在创建前返回 422，且不能入队。"""
    enqueued = []
    monkeypatch.setattr(tasks_routes, "enqueue_pipeline", enqueued.append)

    assert client.post("/api/tasks", json=_payload(mode="mixed")).status_code == 422
    assert client.post("/api/tasks", json=_payload(burn="weird")).status_code == 422
    assert client.post("/api/tasks", json=_payload(engine="other")).status_code == 422
    assert enqueued == []


def test_create_rejects_empty_model_and_languages(client, monkeypatch):
    """模型和语言字段为空时应在创建前返回 422，且不能入队。"""
    enqueued = []
    monkeypatch.setattr(tasks_routes, "enqueue_pipeline", enqueued.append)

    assert client.post("/api/tasks", json=_payload(model="")).status_code == 422
    assert client.post("/api/tasks", json=_payload(sourceLang="")).status_code == 422
    assert client.post("/api/tasks", json=_payload(targetLang="")).status_code == 422
    assert enqueued == []


def test_create_upload_task_persists_options_and_file(client, monkeypatch):
    """上传视频创建任务时，字幕模式与烧录方式应和链接任务一样入库透传。"""
    enqueued = []
    monkeypatch.setattr(tasks_routes, "enqueue_pipeline", enqueued.append)

    r = client.post(
        "/api/tasks/upload",
        data={
            "sourceLang": "en",
            "targetLang": "ja",
            "mode": "bilingual",
            "burn": "soft",
            "model": "medium",
            "engine": "deepseek",
            "needSubtitle": "true",
        },
        files={"file": ("clip.mp4", b"VIDEO", "video/mp4")},
    )

    assert r.status_code == 201
    data = r.json()
    assert data["sourceType"] == "upload"
    assert data["mode"] == "bilingual"
    assert data["burn"] == "soft"
    assert enqueued == [data["id"]]
    assert (client._tmp / data["id"] / "source.mp4").read_bytes() == b"VIDEO"


# ---------- 查询 ----------

def test_list_tasks(client):
    client.post("/api/tasks", json=_payload())
    client.post("/api/tasks", json=_payload(url="https://x/2"))
    r = client.get("/api/tasks")
    assert r.status_code == 200
    assert len(r.json()) == 2


def test_get_task(client):
    cid = client.post("/api/tasks", json=_payload()).json()["id"]
    r = client.get(f"/api/tasks/{cid}")
    assert r.status_code == 200 and r.json()["id"] == cid


def test_get_missing_404(client):
    assert client.get("/api/tasks/nope").status_code == 404


# ---------- 删除 ----------

def test_delete_task(client):
    cid = client.post("/api/tasks", json=_payload()).json()["id"]
    # 造个产物目录，验证会被清理
    d = client._tmp / cid
    d.mkdir(parents=True, exist_ok=True)
    (d / "output.mp4").write_bytes(b"x")

    assert client.delete(f"/api/tasks/{cid}").status_code == 204
    assert client.get(f"/api/tasks/{cid}").status_code == 404
    assert not d.exists()


def test_delete_missing_404(client):
    assert client.delete("/api/tasks/nope").status_code == 404


# ---------- 重试 ----------

def test_retry_resets_status(client, monkeypatch):
    """失败任务重试时应重置状态并重新入队。"""
    cid = client.post("/api/tasks", json=_payload()).json()["id"]
    client._store.update(cid, status="FAILED", progress=40, error="boom")
    enqueued = []
    monkeypatch.setattr(tasks_routes, "enqueue_pipeline", enqueued.append)

    r = client.post(f"/api/tasks/{cid}/retry")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "PENDING"
    assert data["progress"] == 0
    assert data["error"] is None
    assert enqueued == [cid]


def test_retry_running_task_returns_409(client, monkeypatch):
    """运行中任务重试应返回 409 且不能重复入队。"""
    cid = client.post("/api/tasks", json=_payload()).json()["id"]
    client._store.update(cid, status="DOWNLOADING", progress=10, current_step="DOWNLOADING")
    enqueued = []
    monkeypatch.setattr(tasks_routes, "enqueue_pipeline", enqueued.append)

    r = client.post(f"/api/tasks/{cid}/retry")

    assert r.status_code == 409
    assert enqueued == []
    rec = client._store.get(cid)
    assert rec.status == "DOWNLOADING"
    assert rec.progress == 10


# ---------- 文件下载 ----------

def test_download_409_when_not_ready(client):
    cid = client.post("/api/tasks", json=_payload()).json()["id"]
    assert client.get(f"/api/tasks/{cid}/download").status_code == 409
    assert client.get(f"/api/tasks/{cid}/subtitle").status_code == 409


def test_download_serves_file(client):
    cid = client.post("/api/tasks", json=_payload()).json()["id"]
    d = client._tmp / cid
    d.mkdir(parents=True, exist_ok=True)
    (d / "output.mp4").write_bytes(b"VIDEO")
    (d / "translated.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n", encoding="utf-8")

    r = client.get(f"/api/tasks/{cid}/download")
    assert r.status_code == 200 and r.content == b"VIDEO"
    r2 = client.get(f"/api/tasks/{cid}/subtitle")
    assert r2.status_code == 200 and "hi" in r2.text


def test_success_task_exposes_outputs(client):
    cid = client.post("/api/tasks", json=_payload()).json()["id"]
    client._store.update(cid, status="SUCCESS", progress=100)
    data = client.get(f"/api/tasks/{cid}").json()
    assert data["outputs"]["video"] == f"/api/tasks/{cid}/download"
    assert data["outputs"]["subtitle"] == f"/api/tasks/{cid}/subtitle"
    # 资源可用时显式标注 AVAILABLE
    assert data["resourceStatus"] == RESOURCE_STATUS_AVAILABLE


def test_health(client):
    assert client.get("/api/health").json() == {"ok": True}


# ---------- issue #22：终态任务产物丢失 ----------


def test_success_task_with_missing_video_hides_outputs(client):
    """SUCCESS 任务但 output.mp4 不在 → outputs 必须为空，resourceStatus=MISSING。"""
    cid = client.post("/api/tasks", json=_payload()).json()["id"]
    client._store.update(cid, status="SUCCESS", progress=100)

    # 模拟服务重启：创建一个全新的 TaskStore 读同一个 db
    fresh = TaskStore(client._store.db_path)
    # 故意不创建任何产物文件
    marked = tasks_routes.scan_missing_terminal(fresh, data_dir=client._tmp)
    assert marked == [cid]

    data = client.get(f"/api/tasks/{cid}").json()
    assert data["status"] == "SUCCESS"  # 任务历史状态保留
    assert data["resourceStatus"] == RESOURCE_STATUS_MISSING
    assert data["outputs"] is None
    assert data["error"] == "资源已删除"


def test_success_download_only_task_with_missing_source_hides_outputs(client):
    """needSubtitle=False 的"仅下载"任务丢失 source.* 也算资源已丢失。"""
    cid = client.post("/api/tasks", json=_payload(needSubtitle=False)).json()["id"]
    client._store.update(cid, status="SUCCESS", progress=100)

    # 不落 source.* 文件，直接扫
    marked = tasks_routes.scan_missing_terminal(client._store, data_dir=client._tmp)
    assert marked == [cid]

    data = client.get(f"/api/tasks/{cid}").json()
    assert data["resourceStatus"] == RESOURCE_STATUS_MISSING
    assert data["outputs"] is None


def test_scan_missing_terminal_is_idempotent(client):
    """重复跑扫描不会再次写库，状态保持稳定。"""
    cid = client.post("/api/tasks", json=_payload()).json()["id"]
    client._store.update(cid, status="SUCCESS", progress=100)

    assert tasks_routes.scan_missing_terminal(client._store, data_dir=client._tmp) == [cid]
    # 第二次扫描，状态已经是 MISSING，应该返回空列表并不修改 updated_at 之外的字段
    rec_before = client._store.get(cid)
    assert tasks_routes.scan_missing_terminal(client._store, data_dir=client._tmp) == []
    rec_after = client._store.get(cid)
    # updated_at 在第二次扫描不应该被刷新（因为没有写）
    assert rec_before.updated_at == rec_after.updated_at
    assert rec_after.resource_status == RESOURCE_STATUS_MISSING


def test_scan_missing_terminal_skips_non_success(client):
    """非 SUCCESS 终态（如 FAILED）即使文件不存在也不应被降级。"""
    cid = client.post("/api/tasks", json=_payload()).json()["id"]
    client._store.update(cid, status="FAILED", progress=40, error="boom")

    assert tasks_routes.scan_missing_terminal(client._store, data_dir=client._tmp) == []
    rec = client._store.get(cid)
    assert rec.status == "FAILED"
    assert rec.resource_status == RESOURCE_STATUS_AVAILABLE
    assert rec.error == "boom"


def test_scan_missing_terminal_keeps_success_when_artifacts_present(client):
    """SUCCESS + 文件齐备时不应被误判为 MISSING。"""
    cid = client.post("/api/tasks", json=_payload()).json()["id"]
    client._store.update(cid, status="SUCCESS", progress=100)

    d = client._tmp / cid
    d.mkdir(parents=True, exist_ok=True)
    (d / "output.mp4").write_bytes(b"VIDEO")
    (d / "translated.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n", encoding="utf-8")

    assert tasks_routes.scan_missing_terminal(client._store, data_dir=client._tmp) == []
    data = client.get(f"/api/tasks/{cid}").json()
    assert data["resourceStatus"] == RESOURCE_STATUS_AVAILABLE
    assert data["outputs"]["video"] == f"/api/tasks/{cid}/download"


def test_download_missing_video_marks_resource_missing(client):
    """下载端点遇到 SUCCESS 任务但文件不在，应降级为 MISSING 并返回'资源已删除'。"""
    cid = client.post("/api/tasks", json=_payload()).json()["id"]
    client._store.update(cid, status="SUCCESS", progress=100)

    r = client.get(f"/api/tasks/{cid}/download")
    assert r.status_code == 409
    assert r.json()["detail"] == "资源已删除"

    rec = client._store.get(cid)
    assert rec.resource_status == RESOURCE_STATUS_MISSING
    # 详情接口同步反映新状态
    data = client.get(f"/api/tasks/{cid}").json()
    assert data["resourceStatus"] == RESOURCE_STATUS_MISSING
    assert data["outputs"] is None


def test_download_missing_subtitle_marks_resource_missing(client):
    """仅字幕缺失：下载视频成功，下载字幕时降级。"""
    cid = client.post("/api/tasks", json=_payload()).json()["id"]
    d = client._tmp / cid
    d.mkdir(parents=True, exist_ok=True)
    (d / "output.mp4").write_bytes(b"VIDEO")
    # 不写 translated.srt
    client._store.update(cid, status="SUCCESS", progress=100)

    r = client.get(f"/api/tasks/{cid}/subtitle")
    assert r.status_code == 409
    assert r.json()["detail"] == "资源已删除"

    rec = client._store.get(cid)
    assert rec.resource_status == RESOURCE_STATUS_MISSING


def test_download_keeps_not_generated_message_for_running_task(client):
    """非 SUCCESS 任务（流水线还在跑）下载应仍返回'尚未生成'，不动 resource_status。"""
    cid = client.post("/api/tasks", json=_payload()).json()["id"]
    client._store.update(cid, status="TRANSLATING", progress=50, current_step="TRANSLATING")

    r = client.get(f"/api/tasks/{cid}/download")
    assert r.status_code == 409
    assert r.json()["detail"] == "成品视频尚未生成"

    rec = client._store.get(cid)
    # 运行中任务不应当被降级
    assert rec.resource_status == RESOURCE_STATUS_AVAILABLE
    assert rec.status == "TRANSLATING"


def test_list_tasks_reflects_resource_missing(client):
    """列表接口也要反映 resourceStatus，MISSING 任务的 outputs 为 None。"""
    a = client.post("/api/tasks", json=_payload()).json()["id"]
    b = client.post("/api/tasks", json=_payload(url="https://x/2")).json()["id"]

    # a 完整，b 缺文件
    d = client._tmp / a
    d.mkdir(parents=True, exist_ok=True)
    (d / "output.mp4").write_bytes(b"VIDEO")
    (d / "translated.srt").write_text("x", encoding="utf-8")
    client._store.update(a, status="SUCCESS", progress=100)
    client._store.update(b, status="SUCCESS", progress=100)

    marked = tasks_routes.scan_missing_terminal(client._store, data_dir=client._tmp)
    assert sorted(marked) == [b]

    listed = {item["id"]: item for item in client.get("/api/tasks").json()}
    assert listed[a]["resourceStatus"] == RESOURCE_STATUS_AVAILABLE
    assert listed[a]["outputs"]["video"] == f"/api/tasks/{a}/download"
    assert listed[b]["resourceStatus"] == RESOURCE_STATUS_MISSING
    assert listed[b]["outputs"] is None


# ---------- CORS ----------

def test_cors_allows_local_workbench_origin(client):
    r = client.options(
        "/api/tasks",
        headers={
            "Origin": "http://localhost:5273",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "http://localhost:5273"


def test_cors_rejects_untrusted_origin(client):
    r = client.options(
        "/api/tasks",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert r.status_code == 400
    assert "access-control-allow-origin" not in r.headers
