"""任务相关路由（业务域：tasks）。

新增其它业务时，仿照本文件建一个 APIRouter，再在 app.py 里 include 即可。
"""

from __future__ import annotations

import inspect
import json
import logging
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from src.config import (
    OUTPUT_VIDEO,
    SOURCE_VIDEO_STEM,
    TRANSLATED_SRT,

    artifacts_present,
    settings,

    task_dir,
)
from src.core.downloader import probe_video
from src.handler.deps import get_store
from src.handler.schemas import (
    ResumeOption,
    ResumeOptionsOut,
    TaskCreate,
    TaskOut,
    TaskProbeIn,
    TaskProbeOut,
    to_out,
)
from src.service.orchestrator import (
    PIPELINE_STEPS,
    PipelineError,
    list_resume_options,
    validate_start_from,
)
from src.service.runner import enqueue_pipeline
from src.store import RESOURCE_STATUS_AVAILABLE, RESOURCE_STATUS_MISSING, TaskStore

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

# 前端显示用的阶段名（与 PIPELINE_STEPS 一一对应）
_STEP_LABELS = {
    "DOWNLOADING": "下载视频",
    "EXTRACTING": "提取音频",
    "TRANSCRIBING": "语音转写",
    "TRANSLATING": "字幕翻译",
    "BURNING": "烧录字幕",
}


def _enqueue(task_id: str, start_from: Optional[str] = None) -> None:
    """兼容新旧 enqueue_pipeline 签名：检测是否支持 start_from 关键字。"""
    try:
        sig = inspect.signature(enqueue_pipeline)
    except (TypeError, ValueError):
        sig = None
    accepts_kw = (
        sig is not None
        and any(
            p.kind in (p.KEYWORD_ONLY, p.POSITIONAL_OR_KEYWORD)
            and p.name == "start_from"
            for p in sig.parameters.values()
        )
    )
    if accepts_kw:
        enqueue_pipeline(task_id, start_from=start_from)
    else:
        enqueue_pipeline(task_id)


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
        if artifacts_present(
            rec.id, data_dir=data_dir, need_subtitle=bool(rec.need_subtitle)
        ):
            continue
        store.update(
            rec.id,
            resource_status=RESOURCE_STATUS_MISSING,
            error=_DELETED_MESSAGE,
        )
        downgraded.append(rec.id)
    return downgraded


# ---------- CRUD ----------


@router.post("", response_model=TaskOut, status_code=201)
def create_task(body: TaskCreate, store: TaskStore = Depends(get_store)) -> TaskOut:
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
    enqueue_pipeline(rec.id)
    return to_out(rec)


@router.post("/upload", response_model=TaskOut, status_code=201)
def create_upload_task(
    file: UploadFile = File(..., description="本地视频文件"),
    sourceLang: str = Form("auto", min_length=1),
    targetLang: str = Form("zh-CN", min_length=1),
    mode: Literal["mono", "bilingual"] = Form("mono"),
    burn: Literal["hard", "soft"] = Form("hard"),
    model: str = Form("small", min_length=1),
    engine: Literal["deepseek"] = Form("deepseek"),
    needSubtitle: bool = Form(True),
    store: TaskStore = Depends(get_store),
) -> TaskOut:
    """上传本地视频并创建任务：源文件直接落盘，跳过下载，走后续识别 / 翻译 / 烧录。"""
    filename = (file.filename or "").strip()
    ext = Path(filename).suffix.lower()
    if ext not in _UPLOAD_VIDEO_EXTS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的视频格式：{ext or '未知'}（支持 {', '.join(sorted(_UPLOAD_VIDEO_EXTS))}）",
        )

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
            shutil.copyfileobj(file.file, out)
    except Exception as e:
        store.delete(rec.id)
        shutil.rmtree(d, ignore_errors=True)
        raise HTTPException(status_code=500, detail="保存上传文件失败") from e
    finally:
        file.file.close()

    if not dest.exists() or dest.stat().st_size == 0:
        store.delete(rec.id)
        shutil.rmtree(d, ignore_errors=True)
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
def probe_task(body: TaskProbeIn) -> TaskProbeOut:
    """探测视频链接是否能被 yt-dlp 解析并找到可下载格式。"""
    result = probe_video(body.url)
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


@router.delete("/{task_id}", status_code=204)
def delete_task(task_id: str, store: TaskStore = Depends(get_store)) -> None:
    _require(store, task_id)
    store.delete(task_id)
    shutil.rmtree(task_dir(task_id), ignore_errors=True)


@router.post("/{task_id}/retry", response_model=TaskOut)
def retry_task(
    task_id: str,
    start_from: Optional[str] = Query(
        default=None,
        description=(
            "断点续跑：从指定阶段开始。可选值：" + ", ".join(PIPELINE_STEPS) + "。"
            "留空则从头重跑。"
        ),
    ),
    store: TaskStore = Depends(get_store),
) -> TaskOut:
    """仅允许失败任务重新入队，避免运行中任务重复执行。

    支持断点续跑（Issue #30）：通过 ``start_from`` 指定从哪一步开始；
    之前阶段的产物被复用，跳过重跑。
    """
    rec = _require(store, task_id)
    if rec.status != "FAILED":
        raise HTTPException(status_code=409, detail="只有失败任务可以重试")

    if start_from is not None and start_from not in PIPELINE_STEPS:
        raise HTTPException(
            status_code=400,
            detail=f"未知的 start_from：{start_from!r}（合法值：{', '.join(PIPELINE_STEPS)}）",
        )

    # 校验前置产物完整性（产物缺失直接 409，明确告诉客户端不能从这步开始）
    if start_from is not None:
        try:
            validate_start_from(start_from, task_id, bool(rec.need_subtitle))
        except PipelineError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e

    updated = store.update(
        task_id,
        status="PENDING",
        progress=0,
        current_step=None,
        error=None,
    )
    # 注意：completed_steps / last_error_step 由 orchestrator 在新一次执行中重写，
    # 这里只清状态，不动历史元数据，让前端/审计能追到最近一次失败的位置。
    _enqueue(task_id, start_from=start_from)
    return to_out(updated)


# ---------- 断点续跑：可恢复选项 ----------


@router.get("/{task_id}/resume_options", response_model=ResumeOptionsOut)
def resume_options(
    task_id: str,
    store: TaskStore = Depends(get_store),
) -> ResumeOptionsOut:
    """返回该任务的可恢复选项列表（用于前端"从此步骤继续"菜单）。

    对所有任务都有意义：按当前磁盘上的产物推断每一步是否可作为安全的 resume 起点。
    """
    rec = _require(store, task_id)
    need_subtitle = bool(rec.need_subtitle)
    raw = list_resume_options(task_id, need_subtitle)
    options: List[ResumeOption] = [
        ResumeOption(
            step=name,
            label=_STEP_LABELS.get(name, name),
            available=available,
            reason=reason,
        )
        for name, available, reason in raw
    ]
    return ResumeOptionsOut(
        taskId=rec.id,
        status=rec.status,
        completedSteps=list(rec.completed_steps or []),
        lastErrorStep=rec.last_error_step,
        options=options,
    )


# ---------- 文件下载 ----------


@router.get("/{task_id}/download")
def download_video(task_id: str, store: TaskStore = Depends(get_store)):
    rec = _require(store, task_id)
    path = _resolve_video(task_id)
    if path is not None:
        return FileResponse(path, media_type="video/mp4", filename=f"{task_id}.mp4")
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
    if path.exists():
        return FileResponse(path, media_type="application/x-subrip", filename=f"{task_id}.srt")
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
    """轮询库表并以 SSE 推送进度（方案 A 足够；将来可换事件驱动）。"""
    _require(store, task_id)

    def gen():
        last = None
        for _ in range(3600):  # 上限 ~1 小时
            rec = store.get(task_id)
            if rec is None:
                yield 'data: {"error":"任务不存在"}\n\n'
                return
            snapshot = (rec.status, rec.progress)
            if snapshot != last:
                yield _sse_payload(rec)
                last = snapshot
            if rec.status in _TERMINAL:
                return
            time.sleep(1)

    return StreamingResponse(gen(), media_type="text/event-stream")
