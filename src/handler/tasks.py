"""任务相关路由（业务域：tasks）。

新增其它业务时，仿照本文件建一个 APIRouter，再在 app.py 里 include 即可。
"""

from __future__ import annotations

import errno
import json
import logging
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
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
from src.core.ffmpeg_utils import probe_duration
from src.handler.subtitle_editor import release_lock
from src.handler.deps import (
    get_probe_store,
    get_store,
    get_translation_engine_store,
    require_api_token,
)
from src.handler.schemas import (
    ErrorDetail,
    ProbeRecordOut,
    ProbeRecordsClearOut,
    TaskCreate,
    TaskOut,
    TaskProbeIn,
    TaskProbeOut,
    _probe_record_to_out,
    to_out,
)
from src.service.runner import cancel_pipeline, enqueue_pipeline
from src.service.asset_resolver import AssetResolver, ResourceState
from src.store import (
    DOWNGRADE_REASON_DISK_FAILURE,
    DOWNGRADE_REASON_UNKNOWN,
    DOWNGRADE_REASON_USER_CLEANED,
    DOWNGRADE_REASON_VOLUME_MIGRATED,
    RESOURCE_STATUS_AVAILABLE,
    RESOURCE_STATUS_MISSING,
    ProbeStore,
    TaskStore,
    TranslationEngineStore,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tasks", tags=["tasks"])

_TERMINAL = {"SUCCESS", "FAILED", "CANCELLED"}

# 资源已丢失时给用户的简短、稳定错误文案，避免把文件系统异常 / 堆栈漏到 UI
_DELETED_MESSAGE = "资源已删除"

# 允许上传的本地视频扩展名（小写，含点）
_UPLOAD_VIDEO_EXTS = {
    ".mp4", ".mov", ".mkv", ".webm", ".avi",
    ".m4v", ".flv", ".ts", ".mpeg", ".mpg", ".wmv",
}


def _upload_error(
    status_code: int,
    *,
    code: str,
    message: str,
    limits: Optional[dict] = None,
    suggestion: Optional[str] = None,
) -> HTTPException:
    detail = ErrorDetail(
        code=code,
        message=message,
        limits=limits,
        suggestion=suggestion,
    ).model_dump(exclude_none=True)
    return HTTPException(status_code=status_code, detail=detail)


def _require(store: TaskStore, task_id: str):
    rec = store.get(task_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return rec


def _mark_resource_missing(
    store: TaskStore,
    task_id: str,
    reason: str,
    downgrade_reason: str = DOWNGRADE_REASON_UNKNOWN,
    downgrade_errno: Optional[int] = None,
) -> None:
    """把一个任务的 resource_status 幂等地置为 MISSING。"""
    rec = store.get(task_id)
    if rec is None or rec.resource_status == RESOURCE_STATUS_MISSING:
        return
    store.update(
        task_id,
        resource_status=RESOURCE_STATUS_MISSING,
        error=reason,
        downgrade_reason=downgrade_reason,
        downgrade_errno=downgrade_errno,
        downgraded_at=int(time.time() * 1000),
    )


def _ensure_translation_engine(
    engine: str,
    need_subtitle: bool,
    engines: TranslationEngineStore,
) -> None:
    if not need_subtitle:
        return
    if engine == "deepseek":
        if not (settings.deepseek_api_key and settings.deepseek_api_key.strip()):
            raise HTTPException(
                status_code=422,
                detail="缺少 DeepSeek API Key，请在 .env 配置 SUBTRANS_DEEPSEEK_API_KEY",
            )
        return
    rec = engines.get(engine)
    if rec is None:
        raise HTTPException(status_code=422, detail="翻译引擎配置不存在")
    if not rec.enabled:
        raise HTTPException(status_code=422, detail="翻译引擎已停用")
    if not (rec.api_key and rec.api_key.strip()):
        raise HTTPException(status_code=422, detail="翻译引擎尚未配置 API Key")
    if rec.availability != "AVAILABLE":
        raise HTTPException(status_code=422, detail="翻译引擎尚未通过可用性检测")


def scan_missing_terminal(
    store: TaskStore,
    *,
    task_id: Optional[str] = None,
    data_dir=None,
    downgrade_reason: Optional[str] = None,
) -> List[str]:
    """扫描终态 SUCCESS 任务，把磁盘产物已丢失的降级为 MISSING。

    幂等：已为 MISSING 的不再处理；运行中任务（status != SUCCESS）忽略。
    若指定 task_id，则仅针对该任务执行单任务降级检测。
    返回被降级的 task_id 列表，便于启动日志 / 测试断言 / 资源清理。
    """
    data_path = Path(data_dir if data_dir is not None else settings.data_dir)
    downgraded: List[str] = []
    if task_id is not None:
        target = store.get(task_id)
        recs = [target] if target is not None else []
    else:
        recs = store.list()

    success_count = sum(1 for rec in recs if rec.status == "SUCCESS")
    data_root_unavailable = False
    detected_reason: Optional[str] = None
    detected_errno: Optional[int] = None

    try:
        data_path.stat()
        list(data_path.iterdir())
    except FileNotFoundError:
        data_root_unavailable = True
    except NotADirectoryError as e:
        logger.warning("scan_missing_terminal data_dir 异常: errno=%s msg=%s", e.errno, e)
        data_root_unavailable = True
        detected_reason = DOWNGRADE_REASON_USER_CLEANED
        detected_errno = e.errno
    except PermissionError as e:
        logger.warning("scan_missing_terminal data_dir 异常: errno=%s msg=%s", e.errno, e)
        data_root_unavailable = True
        detected_reason = DOWNGRADE_REASON_DISK_FAILURE
        detected_errno = e.errno
    except OSError as e:
        logger.warning("scan_missing_terminal data_dir 异常: errno=%s msg=%s", e.errno, e)
        data_root_unavailable = True
        detected_errno = e.errno
        if e.errno in (errno.EIO, errno.ENXIO, errno.ESTALE):
            detected_reason = DOWNGRADE_REASON_DISK_FAILURE
        else:
            detected_reason = DOWNGRADE_REASON_UNKNOWN

    for rec in recs:
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
            unreadable = state_out == ResourceState.UNREADABLE or state_srt == ResourceState.UNREADABLE
        else:
            state_src, _, _ = AssetResolver.resolve_source(rec.id)
            if state_src == ResourceState.AVAILABLE:
                continue
            unreadable = state_src == ResourceState.UNREADABLE

        error_msg = "资源不可读" if unreadable else _DELETED_MESSAGE
        audit_errno: Optional[int] = None
        if downgrade_reason is not None:
            audit_reason = downgrade_reason
        elif unreadable:
            audit_reason = DOWNGRADE_REASON_DISK_FAILURE
        elif data_root_unavailable:
            if detected_reason is not None:
                audit_reason = detected_reason
                audit_errno = detected_errno
            elif success_count > 1:
                audit_reason = DOWNGRADE_REASON_VOLUME_MIGRATED
            else:
                audit_reason = DOWNGRADE_REASON_UNKNOWN
        else:
            audit_reason = DOWNGRADE_REASON_UNKNOWN

        store.update(
            rec.id,
            resource_status=RESOURCE_STATUS_MISSING,
            error=error_msg,
            downgrade_reason=audit_reason,
            downgrade_errno=audit_errno,
            downgraded_at=int(time.time() * 1000),
        )
        downgraded.append(rec.id)
    return downgraded


# ---------- CRUD ----------

@router.post("", response_model=TaskOut, status_code=201, dependencies=[Depends(require_api_token)])
def create_task(
    body: TaskCreate,
    store: TaskStore = Depends(get_store),
    engines: TranslationEngineStore = Depends(get_translation_engine_store),
) -> TaskOut:
    _ensure_translation_engine(body.engine, body.needSubtitle, engines)
    rec, created = store.create_if_no_recent_active(
        url=body.url,
        source_lang=body.sourceLang,
        target_lang=body.targetLang,
        mode=body.mode,
        burn=body.burn,
        model=body.model,
        engine=body.engine,
        need_subtitle=body.needSubtitle,
    )
    if not created:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "TASK_ALREADY_RUNNING",
                "message": "该 URL 已有任务正在处理，请复用现有 task_id",
                "taskId": rec.id,
            },
        )
    enqueue_pipeline(rec.id)  # 第 2 步接入真正执行
    return to_out(rec)


@router.post("/upload", response_model=TaskOut, status_code=201, dependencies=[Depends(require_api_token)])
def create_upload_task(
    request: Request,
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
    max_upload_bytes = settings.max_upload_mb * 1024 * 1024
    content_length_hdr = request.headers.get("content-length")
    if content_length_hdr:
        try:
            if int(content_length_hdr) > max_upload_bytes:
                raise _upload_error(
                    413,
                    code="UPLOAD_TOO_LARGE",
                    message=f"上传文件大小超过最大限制 ({settings.max_upload_mb} MB)",
                    limits={"maxMb": settings.max_upload_mb},
                    suggestion="请压缩或切分视频，也可以改用 URL 任务模式。",
                )
        except ValueError:
            pass

    filename = (file.filename or "").strip()
    _ensure_translation_engine(engine, needSubtitle, engines)
    ext = Path(filename).suffix.lower()
    if ext not in _UPLOAD_VIDEO_EXTS:
        supported_formats = sorted(_UPLOAD_VIDEO_EXTS)
        raise _upload_error(
            400,
            code="UNSUPPORTED_FORMAT",
            message=f"不支持的视频格式：{ext or '未知'}（支持 {', '.join(supported_formats)}）",
            limits={"supportedFormats": supported_formats},
            suggestion="请将视频转换为受支持的格式后重新上传。",
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
    dest_part = d / f"{SOURCE_VIDEO_STEM}{ext}.part"
    dest = d / f"{SOURCE_VIDEO_STEM}{ext}"
    written_bytes = 0
    chunk_size = 1024 * 1024  # 1MB
    try:
        with dest_part.open("wb") as out:
            while True:
                chunk = file.file.read(chunk_size)
                if not chunk:
                    break
                written_bytes += len(chunk)
                if written_bytes > max_upload_bytes:
                    raise _upload_error(
                        413,
                        code="UPLOAD_TOO_LARGE",
                        message=f"上传文件大小超过最大限制 ({settings.max_upload_mb} MB)",
                        limits={"maxMb": settings.max_upload_mb},
                        suggestion="请压缩或切分视频，也可以改用 URL 任务模式。",
                    )
                out.write(chunk)
    except HTTPException:
        store.delete(rec.id)
        shutil.rmtree(d, ignore_errors=True)
        raise
    except Exception as e:
        store.delete(rec.id)
        shutil.rmtree(d, ignore_errors=True)
        raise HTTPException(status_code=500, detail="保存上传文件失败") from e
    finally:
        file.file.close()

    if not dest_part.exists() or dest_part.stat().st_size == 0:
        store.delete(rec.id)
        shutil.rmtree(d, ignore_errors=True)
        raise HTTPException(status_code=400, detail="上传的视频文件为空")

    duration_sec = probe_duration(dest_part, settings.ffprobe_bin)
    if duration_sec is None:
        store.delete(rec.id)
        shutil.rmtree(d, ignore_errors=True)
        raise HTTPException(
            status_code=400,
            detail="无法解析视频时长，请确认文件格式正确且 ffprobe 可用",
        )

    max_video_seconds = settings.max_video_minutes * 60
    if duration_sec > max_video_seconds:
        store.delete(rec.id)
        shutil.rmtree(d, ignore_errors=True)
        release_lock(rec.id)
        raise _upload_error(
            400,
            code="UPLOAD_DURATION_EXCEEDED",
            message=f"视频时长 ({duration_sec / 60:.1f} 分钟) 超过最大限制 ({settings.max_video_minutes} 分钟)",
            limits={
                "maxMinutes": settings.max_video_minutes,
                "durationMinutes": round(duration_sec / 60, 1),
            },
            suggestion="请裁剪或切分视频后重新上传。",
        )

    dest_part.replace(dest)

    enqueue_pipeline(rec.id)
    return to_out(store.get(rec.id) or rec)


@router.get("", response_model=List[TaskOut], dependencies=[Depends(require_api_token)])
def list_tasks(
    offset: int = Query(0, ge=0, description="跳过前 N 条记录"),
    limit: int = Query(50, ge=1, le=200, description="单页最大记录数，取值范围 1 到 200，默认 50"),
    before_id: Optional[str] = Query(None, description="游标：仅返回 ID 早于该任务的记录"),
    after_id: Optional[str] = Query(None, description="游标：仅返回 ID 晚于该任务的记录"),
    store: TaskStore = Depends(get_store),
) -> List[TaskOut]:
    return [
        to_out(r)
        for r in store.list(
            limit=limit,
            offset=offset,
            before_id=before_id,
            after_id=after_id,
        )
    ]


@router.get("/{task_id}", response_model=TaskOut, dependencies=[Depends(require_api_token)])
def get_task(task_id: str, store: TaskStore = Depends(get_store)) -> TaskOut:
    return to_out(_require(store, task_id))


@router.post("/probe", response_model=TaskProbeOut, dependencies=[Depends(require_api_token)])
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
        language=result.language,
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
        cached=result.cached,
        language=result.language,
    )


@router.get("/probe/records", response_model=List[ProbeRecordOut], dependencies=[Depends(require_api_token)])
def list_probe_records(
    limit: int = Query(50, ge=1, le=500),
    probes: ProbeStore = Depends(get_probe_store),
) -> List[ProbeRecordOut]:
    """按时间倒序返回最近的下载测试记录。limit 默认 50，上限 500。"""
    return [_probe_record_to_out(r) for r in probes.list(limit=limit)]


@router.delete("/probe/records", response_model=ProbeRecordsClearOut, dependencies=[Depends(require_api_token)])
def clear_probe_records(
    probes: ProbeStore = Depends(get_probe_store),
) -> ProbeRecordsClearOut:
    """一键清空所有下载测试历史。"""
    deleted = probes.clear()
    return ProbeRecordsClearOut(deleted=deleted)


@router.delete("/probe/records/{record_id}", status_code=204, dependencies=[Depends(require_api_token)])
def delete_probe_record(
    record_id: str,
    probes: ProbeStore = Depends(get_probe_store),
) -> None:
    """删除单条下载测试历史；不存在返回 404。"""
    if not probes.delete(record_id):
        raise HTTPException(status_code=404, detail="测试记录不存在")


@router.delete("/{task_id}", status_code=204, dependencies=[Depends(require_api_token)])
def delete_task(task_id: str, store: TaskStore = Depends(get_store)) -> None:
    rec = _require(store, task_id)
    if rec.status not in _TERMINAL:
        raise HTTPException(
            status_code=409,
            detail="任务运行中，请先等待或调用取消接口",
        )
    store.delete(task_id)
    shutil.rmtree(task_dir(task_id), ignore_errors=True)  # 连产物目录一起清
    release_lock(task_id)


@router.post("/{task_id}/cancel", response_model=TaskOut, dependencies=[Depends(require_api_token)])
def cancel_task(task_id: str, store: TaskStore = Depends(get_store)) -> TaskOut:
    """取消正在运行的任务。"""
    rec = _require(store, task_id)
    if rec.status in _TERMINAL:
        raise HTTPException(status_code=409, detail="任务非运行状态，无法取消")
    cancel_pipeline(task_id)
    updated = store.get(task_id) or rec
    return to_out(updated)


@router.post("/{task_id}/retry", response_model=TaskOut, dependencies=[Depends(require_api_token)])
def retry_task(task_id: str, store: TaskStore = Depends(get_store)) -> TaskOut:
    """仅允许失败或已取消任务重新入队，避免运行中任务重复执行。"""
    rec = _require(store, task_id)
    if rec.status not in ("FAILED", "CANCELLED"):
        raise HTTPException(status_code=409, detail="只有失败或已取消任务可以重试")
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

@router.head("/{task_id}/source", status_code=204, dependencies=[Depends(require_api_token)])
def check_source_video(task_id: str, store: TaskStore = Depends(get_store)):
    """轻量确认源视频是否可用，避免前端先展示原生播放器加载态。"""
    download_source_video(task_id, store)
    return Response(status_code=204)


@router.get("/{task_id}/source", dependencies=[Depends(require_api_token)])
def download_source_video(task_id: str, store: TaskStore = Depends(get_store)):
    """返回未烧录字幕的源视频，供预览页在两个视频轨道之间切换。"""
    _require(store, task_id)
    state, path, message = AssetResolver.resolve_source(task_id)
    if state == ResourceState.AVAILABLE and path is not None:
        return FileResponse(path, filename=f"{task_id}-source{path.suffix}")
    raise HTTPException(status_code=409, detail=message)


@router.head("/{task_id}/download", status_code=204, dependencies=[Depends(require_api_token)])
def check_download_video(task_id: str, store: TaskStore = Depends(get_store)):
    """轻量确认成品视频是否可用，避免前端误显示播放器转圈。"""
    download_video(task_id, store)
    return Response(status_code=204)


@router.get("/{task_id}/download", dependencies=[Depends(require_api_token)])
def download_video(task_id: str, store: TaskStore = Depends(get_store)):
    rec = _require(store, task_id)
    if rec.status != "SUCCESS":
        raise HTTPException(status_code=409, detail="成品视频尚未生成")

    path = _resolve_video(task_id)
    if path is not None:
        state = AssetResolver.check_file_state(path)
        if state == ResourceState.AVAILABLE:
            return FileResponse(path, media_type="video/mp4", filename=f"{task_id}.mp4")
        elif state == ResourceState.UNREADABLE:
            if rec.resource_status == RESOURCE_STATUS_AVAILABLE:
                _mark_resource_missing(
                    store, task_id, "资源不可读", DOWNGRADE_REASON_DISK_FAILURE
                )
            raise HTTPException(status_code=409, detail="资源不可读")

    # 兜底：成功任务的产物被清掉时，要把状态降级为 MISSING，
    # 避免下次列表 / 详情接口继续暴露已失效的下载链接。
    if rec.resource_status == RESOURCE_STATUS_AVAILABLE:
        _mark_resource_missing(store, task_id, _DELETED_MESSAGE)
    raise HTTPException(
        status_code=409,
        detail=_DELETED_MESSAGE,
    )


def _resolve_video(task_id: str):
    """定位可下载的视频：优先烧录成品 output.mp4，仅下载模式回退到 source.*（排除 .part 临时文件）。"""
    d = task_dir(task_id)
    out = d / OUTPUT_VIDEO
    if out.exists():
        return out
    state, source_path, _ = AssetResolver.resolve_source(task_id)
    if state == ResourceState.AVAILABLE and source_path is not None:
        return source_path
    return None


@router.get("/{task_id}/subtitle", dependencies=[Depends(require_api_token)])
def download_subtitle(task_id: str, store: TaskStore = Depends(get_store)):
    rec = _require(store, task_id)
    path = task_dir(task_id) / TRANSLATED_SRT
    state = AssetResolver.check_file_state(path)
    if state == ResourceState.AVAILABLE:
        return FileResponse(path, media_type="application/x-subrip", filename=f"{task_id}.srt")
    elif state == ResourceState.UNREADABLE:
        if rec.status == "SUCCESS" and rec.resource_status == RESOURCE_STATUS_AVAILABLE:
            _mark_resource_missing(
                store, task_id, "资源不可读", DOWNGRADE_REASON_DISK_FAILURE
            )
        raise HTTPException(status_code=409, detail="资源不可读")

    if rec.status == "SUCCESS" and rec.resource_status == RESOURCE_STATUS_AVAILABLE:
        _mark_resource_missing(store, task_id, _DELETED_MESSAGE)
    raise HTTPException(
        status_code=409,
        detail=_DELETED_MESSAGE if rec.status == "SUCCESS" else "译文字幕尚未生成",
    )


@router.post("/{task_id}/folder", summary="打开任务文件夹", dependencies=[Depends(require_api_token)])
def open_task_folder(task_id: str, store: TaskStore = Depends(get_store)) -> dict:
    """用系统文件管理器打开任务产物目录。"""
    if not task_id or not re.match(r"^task_[A-Za-z0-9_-]+$", task_id):
        raise HTTPException(status_code=400, detail="task_id 格式不符合规范")

    _require(store, task_id)
    path = task_dir(task_id).resolve()
    data_dir = settings.data_dir.resolve()
    try:
        if not path.is_relative_to(data_dir):
            raise HTTPException(status_code=400, detail="任务目录路径非法")
    except ValueError:
        raise HTTPException(status_code=400, detail="任务目录路径非法")

    if not path.exists():
        raise HTTPException(status_code=409, detail="任务目录尚未生成")
    _open_folder(path)
    return {"ok": True}


def _open_folder(path: Path | str) -> None:
    """按当前系统选择文件管理器打开目录。"""
    abs_path = Path(path).resolve()
    if sys.platform == "darwin":
        cmd = ["open", str(abs_path)]
    elif sys.platform.startswith("win"):
        cmd = ["explorer", str(abs_path)]
    else:
        cmd = ["xdg-open", str(abs_path)]
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
        "errorCode": rec.error_code,
        "resourceStatus": to_out(rec).resourceStatus,
        "outputs": to_out(rec).outputs,
    }
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.get("/{task_id}/stream", dependencies=[Depends(require_api_token)])
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
