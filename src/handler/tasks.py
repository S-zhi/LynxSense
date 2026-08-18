"""任务相关路由（业务域：tasks）。

新增其它业务时，仿照本文件建一个 APIRouter，再在 app.py 里 include 即可。
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse

from src.config import (
    OUTPUT_VIDEO,
    SOURCE_VIDEO_STEM,
    TRANSLATED_SRT,
    artifacts_present,
    settings,
    task_dir,
)
from src.core.downloader import probe_video
from src.handler.subtitle_editor import release_lock
from src.handler.deps import get_probe_store, get_store, get_translation_engine_store
from src.handler.schemas import (
    ProbeRecordOut,
    ProbeRecordsClearOut,
    TaskCreate,
    TaskOut,
    TaskProbeIn,
    TaskProbeOut,
    _probe_record_to_out,
    to_out,
)
from src.service.runner import enqueue_pipeline
from src.service.asset_resolver import AssetResolver, ResourceState
from src.store import (
    RESOURCE_STATUS_AVAILABLE,
    RESOURCE_STATUS_MISSING,
    ProbeStore,
    TaskStore,
    TranslationEngineStore,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tasks", tags=["tasks"])

_TERMINAL = {"SUCCESS", "FAILED"}

# 资源已丢失时给用户的简短、稳定错误文案，避免把文件系统异常 / 堆栈漏到 UI
_DELETED_MESSAGE = "资源已删除"

# 允许上传的本地视频扩展名（小写，含点）
_UPLOAD_VIDEO_EXTS = {
    ".mp4", ".mov", ".mkv", ".webm", ".avi",
    ".m4v", ".flv", ".ts", ".mpeg", ".mpg", ".wmv",
}


def _require(store: TaskStore, task_id: str):
    rec = store.get(task_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return rec


def _mark_resource_missing(store: TaskStore, task_id: str, reason: str) -> None:
    """把一个任务的 resource_status 幂等地置为 MISSING。"""
    rec = store.get(task_id)
    if rec is None or rec.resource_status == RESOURCE_STATUS_MISSING:
        return
    store.update(
        task_id,
        resource_status=RESOURCE_STATUS_MISSING,
        error=reason,
    )


def _ensure_translation_engine(
    engine: str,
    need_subtitle: bool,
    engines: TranslationEngineStore,
) -> None:
    if not need_subtitle or engine == "deepseek":
        return
    rec = engines.get(engine)
    if rec is None:
        raise HTTPException(status_code=422, detail="翻译引擎配置不存在")
    if not rec.enabled:
        raise HTTPException(status_code=422, detail="翻译引擎已停用")
    if not rec.api_key:
        raise HTTPException(status_code=422, detail="翻译引擎尚未配置 API Key")
    if rec.availability != "AVAILABLE":
        raise HTTPException(status_code=422, detail="翻译引擎尚未通过可用性检测")


def scan_missing_terminal(store: TaskStore, *, data_dir=None) -> List[str]:
    """扫描终态 SUCCESS 任务，把磁盘产物已丢失的降级为 MISSING。

    幂等：已为 MISSING 的不再处理；运行中任务（status != SUCCESS）忽略。
    返回被降级的 task_id 列表，便于启动日志 / 测试断言。
    """
    data_dir = data_dir if data_dir is not None else settings.data_dir
    downgraded: List[str] = []
    for rec in store.list():
        if rec.status != "SUCCESS":
            continue
        if rec.resource_status == RESOURCE_STATUS_MISSING:
            continue

        need_sub = bool(rec.need_subtitle)
        if need_sub:
            state_out, _, _ = AssetResolver.resolve_output_video(rec.id)
            state_srt, _, _ = AssetResolver.resolve_translated_srt(rec.id)
            if state_out == ResourceState.AVAILABLE and state_srt == ResourceState.AVAILABLE:
                continue
            error_msg = "资源不可读" if (state_out == ResourceState.UNREADABLE or state_srt == ResourceState.UNREADABLE) else _DELETED_MESSAGE
        else:
            state_src, _, _ = AssetResolver.resolve_source(rec.id)
            if state_src == ResourceState.AVAILABLE:
                continue
            error_msg = "资源不可读" if state_src == ResourceState.UNREADABLE else _DELETED_MESSAGE

        store.update(
            rec.id,
            resource_status=RESOURCE_STATUS_MISSING,
            error=error_msg,
        )
        downgraded.append(rec.id)
    return downgraded


# ---------- CRUD ----------

@router.post("", response_model=TaskOut, status_code=201)
def create_task(
    body: TaskCreate,
    store: TaskStore = Depends(get_store),
    engines: TranslationEngineStore = Depends(get_translation_engine_store),
) -> TaskOut:
    _ensure_translation_engine(body.engine, body.needSubtitle, engines)
    rec = store.create(
        url=body.url,
        source_lang=body.sourceLang,
        target_lang=body.targetLang,
        mode=body.mode,
        burn=body.burn,
        model=body.model,
        engine=body.engine,
        need_subtitle=body.needSubtitle,
    )
    enqueue_pipeline(rec.id)  # 第 2 步接入真正执行
    return to_out(rec)


@router.post("/upload", response_model=TaskOut, status_code=201)
def create_upload_task(
    file: UploadFile = File(..., description="本地视频文件"),
    sourceLang: str = Form("auto", min_length=1),
    targetLang: str = Form("zh-CN", min_length=1),
    mode: Literal["mono", "bilingual"] = Form("mono"),
    burn: Literal["hard", "soft"] = Form("hard"),
    model: str = Form("small", min_length=1),
    engine: str = Form("deepseek", min_length=1),
    needSubtitle: bool = Form(True),
    store: TaskStore = Depends(get_store),
    engines: TranslationEngineStore = Depends(get_translation_engine_store),
) -> TaskOut:
    """上传本地视频并创建任务：源文件直接落盘，跳过下载，走后续识别 / 翻译 / 烧录。

    字幕模式（mode）与烧录方式（burn）与链接任务同样透传到下层流水线。
    """
    filename = (file.filename or "").strip()
    _ensure_translation_engine(engine, needSubtitle, engines)
    ext = Path(filename).suffix.lower()
    if ext not in _UPLOAD_VIDEO_EXTS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的视频格式：{ext or '未知'}（支持 {', '.join(sorted(_UPLOAD_VIDEO_EXTS))}）",
        )

    # 先建记录拿到 task_id，再把源文件落盘到该任务目录
    rec = store.create(
        url=filename,
        source_lang=sourceLang,
        target_lang=targetLang,
        mode=mode,
        burn=burn,
        model=model,
        engine=engine,
        source_type="upload",
        need_subtitle=needSubtitle,
        title=Path(filename).stem or "上传的视频",
    )

    d = task_dir(rec.id)
    d.mkdir(parents=True, exist_ok=True)
    dest = d / f"{SOURCE_VIDEO_STEM}{ext}"
    try:
        with dest.open("wb") as out:
            shutil.copyfileobj(file.file, out)  # 流式写盘，避免整文件入内存
    except Exception as e:
        store.delete(rec.id)
        shutil.rmtree(d, ignore_errors=True)
        release_lock(rec.id)
        raise HTTPException(status_code=500, detail="保存上传文件失败") from e
    finally:
        file.file.close()

    if not dest.exists() or dest.stat().st_size == 0:
        store.delete(rec.id)
        shutil.rmtree(d, ignore_errors=True)
        release_lock(rec.id)
        raise HTTPException(status_code=400, detail="上传的视频文件为空")

    enqueue_pipeline(rec.id)
    return to_out(store.get(rec.id) or rec)


@router.get("", response_model=List[TaskOut])
def list_tasks(store: TaskStore = Depends(get_store)) -> List[TaskOut]:
    return [to_out(r) for r in store.list()]


@router.get("/{task_id}", response_model=TaskOut)
def get_task(task_id: str, store: TaskStore = Depends(get_store)) -> TaskOut:
    return to_out(_require(store, task_id))


@router.post("/probe", response_model=TaskProbeOut)
def probe_task(
    body: TaskProbeIn,
    probes: ProbeStore = Depends(get_probe_store),
) -> TaskProbeOut:
    """探测视频链接是否能被 yt-dlp 解析并找到可下载格式。

    每次探测都持久化一行到 probe_records，便于用户回看历史、
    排查"链接换格式还是不可下载"等问题。失败也会记录（含 reason/detail），
    错误信息不会因为页面刷新而丢失。
    """
    result = probe_video(body.url)
    # 探测本身失败不影响响应；同时把 ok=False 的记录也存下来，方便回看错误
    probes.record(
        url=body.url,
        ok=result.ok,
        title=result.title,
        extractor=result.extractor,
        duration=result.duration,
        formats_count=result.formats_count,
        webpage_url=result.webpage_url,
        reason=result.reason,
        detail=result.detail,
    )
    return TaskProbeOut(
        ok=result.ok,
        title=result.title,
        extractor=result.extractor,
        duration=result.duration,
        formatsCount=result.formats_count,
        webpageUrl=result.webpage_url,
        reason=result.reason,
        detail=result.detail,
    )


@router.get("/probe/records", response_model=List[ProbeRecordOut])
def list_probe_records(
    limit: int = Query(50, ge=1, le=500),
    probes: ProbeStore = Depends(get_probe_store),
) -> List[ProbeRecordOut]:
    """按时间倒序返回最近的下载测试记录。limit 默认 50，上限 500。"""
    return [_probe_record_to_out(r) for r in probes.list(limit=limit)]


@router.delete("/probe/records", response_model=ProbeRecordsClearOut)
def clear_probe_records(
    probes: ProbeStore = Depends(get_probe_store),
) -> ProbeRecordsClearOut:
    """一键清空所有下载测试历史。"""
    deleted = probes.clear()
    return ProbeRecordsClearOut(deleted=deleted)


@router.delete("/probe/records/{record_id}", status_code=204)
def delete_probe_record(
    record_id: str,
    probes: ProbeStore = Depends(get_probe_store),
) -> None:
    """删除单条下载测试历史；不存在返回 404。"""
    if not probes.delete(record_id):
        raise HTTPException(status_code=404, detail="测试记录不存在")


@router.delete("/{task_id}", status_code=204)
def delete_task(task_id: str, store: TaskStore = Depends(get_store)) -> None:
    _require(store, task_id)
    store.delete(task_id)
    shutil.rmtree(task_dir(task_id), ignore_errors=True)  # 连产物目录一起清
    release_lock(task_id)


@router.post("/{task_id}/retry", response_model=TaskOut)
def retry_task(task_id: str, store: TaskStore = Depends(get_store)) -> TaskOut:
    """仅允许失败任务重新入队，避免运行中任务重复执行。"""
    rec = _require(store, task_id)
    if rec.status != "FAILED":
        raise HTTPException(status_code=409, detail="只有失败任务可以重试")
    updated = store.update(
        task_id,
        status="PENDING",
        progress=0,
        current_step=None,
        error=None,
    )
    enqueue_pipeline(task_id)
    return to_out(updated)


# ---------- 文件下载 ----------

@router.head("/{task_id}/source", status_code=204)
def check_source_video(task_id: str, store: TaskStore = Depends(get_store)):
    """轻量确认源视频是否可用，避免前端先展示原生播放器加载态。"""
    download_source_video(task_id, store)
    return Response(status_code=204)


@router.get("/{task_id}/source")
def download_source_video(task_id: str, store: TaskStore = Depends(get_store)):
    """返回未烧录字幕的源视频，供预览页在两个视频轨道之间切换。"""
    _require(store, task_id)
    state, path, message = AssetResolver.resolve_source(task_id)
    if state == ResourceState.AVAILABLE and path is not None:
        return FileResponse(path, filename=f"{task_id}-source{path.suffix}")
    raise HTTPException(status_code=409, detail=message)


@router.head("/{task_id}/download", status_code=204)
def check_download_video(task_id: str, store: TaskStore = Depends(get_store)):
    """轻量确认成品视频是否可用，避免前端误显示播放器转圈。"""
    download_video(task_id, store)
    return Response(status_code=204)


@router.get("/{task_id}/download")
def download_video(task_id: str, store: TaskStore = Depends(get_store)):
    rec = _require(store, task_id)
    path = _resolve_video(task_id)
    if path is not None:
        state = AssetResolver.check_file_state(path)
        if state == ResourceState.AVAILABLE:
            return FileResponse(path, media_type="video/mp4", filename=f"{task_id}.mp4")
        elif state == ResourceState.UNREADABLE:
            if rec.status == "SUCCESS" and rec.resource_status == RESOURCE_STATUS_AVAILABLE:
                _mark_resource_missing(store, task_id, "资源不可读")
            raise HTTPException(status_code=409, detail="资源不可读")

    # 兜底：成功任务的产物被清掉时，要把状态降级为 MISSING，
    # 避免下次列表 / 详情接口继续暴露已失效的下载链接。
    if rec.status == "SUCCESS" and rec.resource_status == RESOURCE_STATUS_AVAILABLE:
        _mark_resource_missing(store, task_id, _DELETED_MESSAGE)
    raise HTTPException(
        status_code=409,
        detail=_DELETED_MESSAGE if rec.status == "SUCCESS" else "成品视频尚未生成",
    )


def _resolve_video(task_id: str):
    """定位可下载的视频：优先烧录成品 output.mp4，仅下载模式回退到 source.*。"""
    d = task_dir(task_id)
    out = d / OUTPUT_VIDEO
    if out.exists():
        return out
    for p in sorted(d.glob(f"{SOURCE_VIDEO_STEM}.*")):
        return p
    return None


@router.get("/{task_id}/subtitle")
def download_subtitle(task_id: str, store: TaskStore = Depends(get_store)):
    rec = _require(store, task_id)
    path = task_dir(task_id) / TRANSLATED_SRT
    state = AssetResolver.check_file_state(path)
    if state == ResourceState.AVAILABLE:
        return FileResponse(path, media_type="application/x-subrip", filename=f"{task_id}.srt")
    elif state == ResourceState.UNREADABLE:
        if rec.status == "SUCCESS" and rec.resource_status == RESOURCE_STATUS_AVAILABLE:
            _mark_resource_missing(store, task_id, "资源不可读")
        raise HTTPException(status_code=409, detail="资源不可读")

    if rec.status == "SUCCESS" and rec.resource_status == RESOURCE_STATUS_AVAILABLE:
        _mark_resource_missing(store, task_id, _DELETED_MESSAGE)
    raise HTTPException(
        status_code=409,
        detail=_DELETED_MESSAGE if rec.status == "SUCCESS" else "译文字幕尚未生成",
    )


@router.post("/{task_id}/folder", summary="打开任务文件夹")
def open_task_folder(task_id: str, store: TaskStore = Depends(get_store)) -> dict:
    """用系统文件管理器打开任务产物目录。"""
    _require(store, task_id)
    path = task_dir(task_id)
    if not path.exists():
        raise HTTPException(status_code=409, detail="任务目录尚未生成")
    _open_folder(path)
    return {"ok": True}


def _open_folder(path) -> None:
    """按当前系统选择文件管理器打开目录。"""
    if sys.platform == "darwin":
        cmd = ["open", str(path)]
    elif sys.platform.startswith("win"):
        cmd = ["explorer", str(path)]
    else:
        cmd = ["xdg-open", str(path)]
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError as e:
        raise HTTPException(status_code=500, detail="当前系统不支持打开文件夹") from e


# ---------- SSE 进度 ----------

def _sse_payload(rec) -> str:
    data = {
        "id": rec.id,
        "status": rec.status,
        "progress": rec.progress,
        "currentStep": rec.current_step,
        "title": rec.title,
        "error": rec.error,
        "resourceStatus": to_out(rec).resourceStatus,
        "outputs": to_out(rec).outputs,
    }
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.get("/{task_id}/stream")
def stream_progress(task_id: str, store: TaskStore = Depends(get_store)):
    """轮询库表并以 SSE 推送进度（含心跳保活、超时断流与终态事件）。"""
    _require(store, task_id)

    def gen():
        last = None
        start_time = time.time()
        last_sent = time.time()
        timeout_sec = max(1, settings.stream_timeout_sec)

        while True:
            rec = store.get(task_id)
            if rec is None:
                yield 'data: {"error":"任务不存在"}\n\n'
                return

            snapshot = (rec.status, rec.progress)
            now = time.time()

            if snapshot != last:
                if rec.status in _TERMINAL:
                    yield f"event: end\n{_sse_payload(rec)}"
                    return
                yield _sse_payload(rec)
                last = snapshot
                last_sent = now
            else:
                if rec.status in _TERMINAL:
                    yield f"event: end\n{_sse_payload(rec)}"
                    return
                elif now - last_sent >= 15:
                    yield ":keepalive\n\n"
                    last_sent = now

            if now - start_time >= timeout_sec:
                yield 'event: timeout\ndata: {"error":"stream timeout"}\n\n'
                return

            time.sleep(1)

    return StreamingResponse(gen(), media_type="text/event-stream")
