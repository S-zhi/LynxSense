"""SRT 相关配置接口。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from src.handler.deps import get_store, require_api_token

from src.service.srt.replicate_schema import (
    ReplicateSchemaError,
    get_video_language_options,
    get_whisper_model_weight_options,
)
from src.service.model_manager import MODEL_CATALOG, MODEL_NAMES, model_manager
from src.store import TaskStore

router = APIRouter(prefix="/api/srt", tags=["srt"])


@router.get("/languages", response_model=list[str])
def list_video_languages() -> list[str]:
    """返回 Replicate Whisper 支持的视频源语言列表。"""
    try:
        return get_video_language_options()
    except ReplicateSchemaError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/model-weights", response_model=list[str])
def list_model_weights() -> list[str]:
    """返回 Replicate Whisper 模型权重（兼容旧客户端）。"""
    try:
        return get_whisper_model_weight_options()
    except ReplicateSchemaError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/model-options", response_model=list[str])
def list_model_options() -> list[str]:
    """返回带识别后端标识的 Whisper 模型选项。"""
    try:
        replicate_models = get_whisper_model_weight_options()
        local_models = [f"local:{item['name']}" for item in MODEL_CATALOG if item["name"] != "tiny"]
        return ["local:tiny"] + local_models + [f"replicate:{model}" for model in replicate_models]
    except ReplicateSchemaError as exc:
        # Local recognition is the default and must remain usable without a
        # Replicate token or network access. Keep the compatibility option when
        # Replicate is available, but degrade this listing to local:tiny.
        return ["local:tiny"]


@router.get("/local-models")
def list_local_models() -> list[dict]:
    """返回本地 faster-whisper 模型目录及下载状态。"""
    return model_manager.list_models()


@router.post("/local-models/{model_name}/download")
def download_local_model(model_name: str) -> dict:
    """启动本地模型下载并返回可轮询的状态。"""
    if model_name not in MODEL_NAMES:
        raise HTTPException(status_code=404, detail=f"不支持的本地 Whisper 模型: {model_name}")
    return model_manager.download(model_name)


@router.delete("/local-models/{model_name}", dependencies=[Depends(require_api_token)])
def delete_local_model(model_name: str, store: TaskStore = Depends(get_store)) -> dict:
    """删除已下载的本地模型，避免清理正在使用的模型文件。"""
    if model_name not in MODEL_NAMES:
        raise HTTPException(status_code=404, detail=f"不支持的本地 Whisper 模型: {model_name}")

    local_backends = {"local", "local_whisper", "whisper", "faster_whisper"}
    for task in store.list():
        if task.status in {"SUCCESS", "FAILED", "CANCELLED"} or not task.need_subtitle:
            continue
        backend, separator, selected_model = str(task.model or "").partition(":")
        if separator and backend.strip().lower() == "replicate":
            continue
        if separator and backend.strip().lower() not in local_backends:
            continue
        if (selected_model if separator else task.model).strip() == model_name:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "MODEL_IN_USE",
                    "message": "有未结束的任务选用了这个模型，暂时无法删除",
                },
            )

    try:
        return model_manager.delete(model_name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/target-languages", response_model=list[str])
def list_target_languages() -> list[str]:
    """返回支持的翻译目标语言列表。"""
    from src.config import settings
    return list(settings.target_languages)
