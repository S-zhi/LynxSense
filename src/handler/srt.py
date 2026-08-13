"""SRT 相关配置接口。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from src.service.srt.replicate_schema import (
    ReplicateSchemaError,
    get_video_language_options,
    get_whisper_model_weight_options,
)

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
    """返回 Replicate Whisper 支持的模型权重列表。"""
    try:
        return get_whisper_model_weight_options()
    except ReplicateSchemaError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/target-languages", response_model=list[str])
def list_target_languages() -> list[str]:
    """返回支持的翻译目标语言列表。"""
    from src.config import settings
    return list(settings.target_languages)
