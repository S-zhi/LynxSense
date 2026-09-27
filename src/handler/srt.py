"""SRT 相关配置接口。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from src.service.srt.replicate_schema import (
    ReplicateSchemaError,
    get_video_language_options,
    get_whisper_model_weight_options,
)
from src.service.model_manager import MODEL_CATALOG, MODEL_NAMES, model_manager

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
        return [f"replicate:{model}" for model in replicate_models] + [f"local:{item['name']}" for item in MODEL_CATALOG]
    except ReplicateSchemaError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


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


@router.get("/target-languages", response_model=list[str])
def list_target_languages() -> list[str]:
    """返回支持的翻译目标语言列表。"""
    from src.config import settings
    return list(settings.target_languages)
