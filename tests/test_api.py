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
from src.handler.schemas import to_out
from src.core.downloader import ProbeResult
from src.store import TaskStore, TaskRecord


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


def test_health(client):
    assert client.get("/api/health").json() == {"ok": True}


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


# ---------- /api/tasks/probe 探针端点 ----------

def test_probe_returns_probe_result_shape(client, monkeypatch):
    """POST /api/tasks/probe 应把 probe_video 的 ProbeResult 翻成 TaskProbeOut。"""
    fake = ProbeResult(
        ok=True,
        title="P",
        extractor="Generic",
        duration=9.0,
        formats_count=2,
        webpage_url="https://x/v",
    )
    monkeypatch.setattr(tasks_routes, "probe_video", lambda url, **kw: fake)

    r = client.post("/api/tasks/probe", json={"url": "https://x/v"})
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["title"] == "P"
    assert data["extractor"] == "Generic"
    assert data["duration"] == 9.0
    assert data["formatsCount"] == 2
    assert data["webpageUrl"] == "https://x/v"
    assert data["reason"] is None and data["detail"] is None


def test_probe_failure_response(client, monkeypatch):
    """失败时透传 reason / detail。"""
    fake = ProbeResult(ok=False, reason="yt-dlp 暂不支持这个网站或链接", detail="Unsupported URL")
    monkeypatch.setattr(tasks_routes, "probe_video", lambda url, **kw: fake)

    r = client.post("/api/tasks/probe", json={"url": "ftp://x/"})
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is False
    assert data["reason"] == "yt-dlp 暂不支持这个网站或链接"
    assert data["detail"] == "Unsupported URL"
    assert data["formatsCount"] == 0


def test_probe_rejects_empty_url_422(client):
    """url 字段 min_length=1：空串应被 422 拒绝。"""
    assert client.post("/api/tasks/probe", json={"url": ""}).status_code == 422


def test_probe_rejects_missing_url_422(client):
    assert client.post("/api/tasks/probe", json={}).status_code == 422


# ---------- /api/tasks/upload 上传端点边界 ----------

def _upload(client, **fields):
    """构造上传 multipart 请求；name 字段走默认值。"""
    defaults = {
        "sourceLang": "en", "targetLang": "ja", "mode": "mono",
        "burn": "hard", "model": "small", "engine": "deepseek",
        "needSubtitle": "true",
    }
    defaults.update(fields)
    return client.post(
        "/api/tasks/upload",
        data=defaults,
        files={"file": (fields["_filename"], fields.get("_content", b"VIDEO"), "video/mp4")},
    )


def test_upload_rejects_unsupported_extension_400(client, monkeypatch):
    """非视频扩展名应在创建记录前返回 400，且不入队、不落盘。"""
    enqueued = []
    monkeypatch.setattr(tasks_routes, "enqueue_pipeline", enqueued.append)

    r = _upload(client, _filename="clip.txt", _content=b"x", mode="mono")
    assert r.status_code == 400
    assert "不支持的视频格式" in r.json()["detail"]
    assert enqueued == []


def test_upload_rejects_no_extension_400(client, monkeypatch):
    """无扩展名视为未知格式，返回 400。"""
    enqueued = []
    monkeypatch.setattr(tasks_routes, "enqueue_pipeline", enqueued.append)

    r = _upload(client, _filename="clip", _content=b"x")
    assert r.status_code == 400
    assert "未知" in r.json()["detail"]
    assert enqueued == []


def test_upload_normalizes_uppercase_extension(client, monkeypatch):
    """扩展名大小写不敏感：.MP4 与 .Mp4 都应被接受。"""
    enqueued = []
    monkeypatch.setattr(tasks_routes, "enqueue_pipeline", enqueued.append)

    r = _upload(client, _filename="A.MP4", _content=b"VIDEO")
    assert r.status_code == 201
    data = r.json()
    rec = client._store.get(data["id"])
    assert (client._tmp / data["id"] / f"source.MP4").exists()
    assert rec.title == "A"


def _count_task_dirs(base: Path) -> int:
    """统计 base 下形如 task_* 的目录数量，用于验证清理是否彻底。"""
    return sum(1 for p in base.iterdir() if p.is_dir() and p.name.startswith("task_"))


def test_upload_empty_file_400_and_cleanup(client, monkeypatch):
    """0 字节文件应被拒为 400，且任务记录与产物目录应被清理。"""
    enqueued = []
    monkeypatch.setattr(tasks_routes, "enqueue_pipeline", enqueued.append)

    before = _count_task_dirs(client._tmp)
    r = _upload(client, _filename="clip.mp4", _content=b"")
    assert r.status_code == 400
    assert "空" in r.json()["detail"]

    # 不能留任何孤儿任务或任务目录
    assert client.get("/api/tasks").json() == []
    assert _count_task_dirs(client._tmp) == before
    assert enqueued == []


def test_upload_save_failure_500_and_cleanup(client, monkeypatch):
    """落盘失败时记为 500，任务记录与任务目录应被清掉，避免孤儿。"""
    enqueued = []
    monkeypatch.setattr(tasks_routes, "enqueue_pipeline", enqueued.append)

    def boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(tasks_routes.shutil, "copyfileobj", boom)

    before = _count_task_dirs(client._tmp)
    r = _upload(client, _filename="clip.mp4", _content=b"VIDEO")
    assert r.status_code == 500
    assert "保存上传文件失败" in r.json()["detail"]

    assert client.get("/api/tasks").json() == []
    assert _count_task_dirs(client._tmp) == before
    assert enqueued == []


def test_upload_uses_filename_stem_as_title(client, monkeypatch):
    """title 取自文件名 stem（去后缀）。"""
    monkeypatch.setattr(tasks_routes, "enqueue_pipeline", lambda tid: None)

    r = _upload(client, _filename="my-clip.mp4", _content=b"VIDEO")
    assert r.status_code == 201
    rec = client._store.get(r.json()["id"])
    assert rec.title == "my-clip"


def test_upload_title_uses_stem_for_dotted_name(client, monkeypatch):
    """以点开头的文件名（Path 中 stem 非空但带点）也应按 stem 入库。"""
    monkeypatch.setattr(tasks_routes, "enqueue_pipeline", lambda tid: None)

    # '..mp4' -> Path('..mp4').suffix == '.mp4'、stem == '.' （不会被 "" 兜底）
    r = _upload(client, _filename="..mp4", _content=b"VIDEO")
    assert r.status_code == 201
    rec = client._store.get(r.json()["id"])
    assert rec.title == "."


def test_upload_persists_need_subtitle_false(client, monkeypatch):
    """needSubtitle=false 应入库为 0，并能在响应里读到。"""
    monkeypatch.setattr(tasks_routes, "enqueue_pipeline", lambda tid: None)

    r = _upload(client, _filename="clip.mp4", _content=b"VIDEO", needSubtitle="false")
    assert r.status_code == 201
    data = r.json()
    assert data["sourceType"] == "upload"
    assert data["needSubtitle"] is False

    rec = client._store.get(data["id"])
    assert rec.source_type == "upload"
    assert rec.need_subtitle == 0


def test_upload_invalid_enum_param_422(client, monkeypatch):
    """上传端点也对 mode/burn/engine 枚举做校验，非法值应 422。"""
    enqueued = []
    monkeypatch.setattr(tasks_routes, "enqueue_pipeline", enqueued.append)

    for bad in [{"mode": "mixed"}, {"burn": "weird"}, {"engine": "other"}]:
        r = _upload(client, _filename="clip.mp4", _content=b"VIDEO", **bad)
        assert r.status_code == 422, f"应被 422 拒绝: {bad}"
    assert enqueued == []


def test_upload_calls_enqueue_with_task_id(client, monkeypatch):
    """上传成功时 enqueue_pipeline 收到新建的 task_id。"""
    enqueued = []
    monkeypatch.setattr(tasks_routes, "enqueue_pipeline", enqueued.append)

    r = _upload(client, _filename="clip.mp4", _content=b"VIDEO")
    data = r.json()
    assert r.status_code == 201
    assert enqueued == [data["id"]]


# ---------- to_out() 链路新增字段透出 ----------

def test_to_out_exposes_source_type_and_need_subtitle():
    """TaskOut 应携带 sourceType / needSubtitle，与前端契约对齐。"""
    rec = TaskRecord(
        id="task_abc", url="u", source_lang="en", target_lang="zh-CN",
        mode="mono", burn="hard", model="small", engine="deepseek",
        source_type="upload", need_subtitle=0,
    )
    out = to_out(rec)
    assert out.sourceType == "upload"
    assert out.needSubtitle is False


def test_to_out_need_subtitle_field_defaults_to_true():
    """need_subtitle 默认 1（True）应翻为 needSubtitle=true。"""
    rec = TaskRecord(
        id="t", url="u", source_lang="auto", target_lang="zh-CN",
        mode="mono", burn="hard", model="small", engine="deepseek",
    )
    out = to_out(rec)
    assert out.sourceType == "url"
    assert out.needSubtitle is True


def test_to_out_success_with_subtitle_when_need_subtitle_true():
    """SUCCESS + need_subtitle=True：outputs 应同时含 video / subtitle。"""
    rec = TaskRecord(
        id="task_x", url="u", source_lang="auto", target_lang="zh-CN",
        mode="mono", burn="hard", model="small", engine="deepseek",
        status="SUCCESS", need_subtitle=1,
    )
    out = to_out(rec)
    assert out.outputs == {
        "video": "/api/tasks/task_x/download",
        "subtitle": "/api/tasks/task_x/subtitle",
    }


def test_to_out_success_without_subtitle_when_need_subtitle_false():
    """SUCCESS + need_subtitle=False：outputs 只含 video（仅下载场景无字幕）。"""
    rec = TaskRecord(
        id="task_y", url="u", source_lang="auto", target_lang="zh-CN",
        mode="mono", burn="hard", model="small", engine="deepseek",
        status="SUCCESS", need_subtitle=0,
    )
    out = to_out(rec)
    assert out.outputs == {"video": "/api/tasks/task_y/download"}
    assert "subtitle" not in out.outputs


def test_to_out_non_success_no_outputs():
    """非 SUCCESS 状态不应有 outputs，避免把未烧录路径暴露给前端。"""
    rec = TaskRecord(
        id="task_z", url="u", source_lang="auto", target_lang="zh-CN",
        mode="mono", burn="hard", model="small", engine="deepseek",
        status="BURNING", progress=90, current_step="BURNING",
        need_subtitle=1,
    )
    out = to_out(rec)
    assert out.outputs is None


# ---------- _resolve_video 下载回退 ----------

def test_resolve_video_prefers_output_mp4(client, monkeypatch, tmp_path):
    """output.mp4 存在时优先返回它（烧录成品）。"""
    cid = client.post("/api/tasks", json=_payload()).json()["id"]
    d = client._tmp / cid
    d.mkdir(parents=True, exist_ok=True)
    (d / "source.mp4").write_bytes(b"SRC")
    (d / "output.mp4").write_bytes(b"OUT")

    assert tasks_routes._resolve_video(cid) == d / "output.mp4"


def test_resolve_video_falls_back_to_source_when_no_output(client, tmp_path):
    """output.mp4 缺失但 source.* 存在 -> 回退到 source.*。"""
    cid = client.post("/api/tasks", json=_payload()).json()["id"]
    d = client._tmp / cid
    d.mkdir(parents=True, exist_ok=True)
    (d / "source.mp4").write_bytes(b"SRC")

    assert tasks_routes._resolve_video(cid) == d / "source.mp4"


def test_resolve_video_supports_other_source_extension(client, tmp_path):
    """source.* 兼容任意扩展名（mkv / mov / webm 等）。"""
    cid = client.post("/api/tasks", json=_payload()).json()["id"]
    d = client._tmp / cid
    d.mkdir(parents=True, exist_ok=True)
    (d / "source.mkv").write_bytes(b"SRC")

    assert tasks_routes._resolve_video(cid) == d / "source.mkv"


def test_resolve_video_returns_none_when_missing(client, tmp_path):
    """目录或文件都不存在时返回 None。"""
    cid = client.post("/api/tasks", json=_payload()).json()["id"]
    assert tasks_routes._resolve_video(cid) is None


def test_download_409_when_only_source_no_output(client, tmp_path):
    """仅下载模式（无 output.mp4）下走 /download 不应 409：可回退到 source.*。"""
    cid = client.post("/api/tasks", json=_payload()).json()["id"]
    d = client._tmp / cid
    d.mkdir(parents=True, exist_ok=True)
    (d / "source.mp4").write_bytes(b"SRC")

    r = client.get(f"/api/tasks/{cid}/download")
    assert r.status_code == 200
    assert r.content == b"SRC"


def test_download_subtitle_409_when_need_subtitle_false(client, tmp_path):
    """need_subtitle=False 的任务不应能下载字幕（链路不会生成）。"""
    cid = client.post("/api/tasks", json=_payload(needSubtitle=False)).json()["id"]
    client._store.update(cid, status="SUCCESS", progress=100, need_subtitle=0)
    # 即便前端误调，物理文件也不应被读到
    assert client.get(f"/api/tasks/{cid}/subtitle").status_code == 409


def test_success_task_outputs_skip_subtitle_when_need_subtitle_false(client):
    """SUCCESS 且 needSubtitle=False 时 GET 任务的 outputs 不应含 subtitle 键。"""
    cid = client.post("/api/tasks", json=_payload(needSubtitle=False)).json()["id"]
    client._store.update(cid, status="SUCCESS", progress=100, need_subtitle=0)
    data = client.get(f"/api/tasks/{cid}").json()
    assert data["outputs"] == {"video": f"/api/tasks/{cid}/download"}
    assert "subtitle" not in data["outputs"]
