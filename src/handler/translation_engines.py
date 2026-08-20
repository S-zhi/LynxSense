"""翻译引擎配置与连通性检测 API。"""

from __future__ import annotations

import time
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.core.translation_engines import TranslationEngineError, make_engine_client
from src.handler.deps import get_translation_engine_store, require_api_token
from src.store import AVAILABILITY, ENGINE_TYPES, TranslationEngine, TranslationEngineStore


router = APIRouter(prefix="/api/settings/translation-engines", tags=["translation-engines"])

VALIDATE_TIMEOUT_SEC = 10


class EngineIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    apiType: str = Field(min_length=1)
    baseUrl: str = Field(min_length=1, max_length=500)
    model: str = Field(min_length=1, max_length=200)
    apiKey: Optional[str] = Field(default=None, max_length=500)
    enabled: bool = True


class EngineOut(BaseModel):
    id: str
    name: str
    apiType: str
    baseUrl: str
    model: str
    enabled: bool
    hasApiKey: bool
    availability: str
    lastCheckedAt: Optional[int] = None
    lastError: Optional[str] = None
    apiKeyRotatedAt: Optional[int] = None


class EngineCheckOut(BaseModel):
    id: str
    availability: str
    available: bool
    checkedAt: int
    errorCode: Optional[str] = None
    message: Optional[str] = None


def _validate_type(api_type: str) -> None:
    if api_type not in ENGINE_TYPES:
        raise HTTPException(status_code=422, detail="不支持的 API 接入类型")


def _out(rec: TranslationEngine) -> EngineOut:
    return EngineOut(
        id=rec.id, name=rec.name, apiType=rec.api_type, baseUrl=rec.base_url,
        model=rec.model, enabled=bool(rec.enabled), hasApiKey=rec.has_api_key,
        availability=rec.availability, lastCheckedAt=rec.last_checked_at,
        lastError=rec.last_error,
        apiKeyRotatedAt=rec.api_key_rotated_at,
    )


def _require(store: TranslationEngineStore, engine_id: str) -> TranslationEngine:
    rec = store.get(engine_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="翻译引擎配置不存在")
    return rec


@router.get("", response_model=List[EngineOut])
def list_engines(store: TranslationEngineStore = Depends(get_translation_engine_store)) -> List[EngineOut]:
    return [_out(rec) for rec in store.list()]


@router.post("", response_model=EngineOut, status_code=201, dependencies=[Depends(require_api_token)])
def create_engine(body: EngineIn, store: TranslationEngineStore = Depends(get_translation_engine_store)) -> EngineOut:
    _validate_type(body.apiType)
    key = body.apiKey.strip() if body.apiKey and body.apiKey.strip() else None
    return _out(store.create(
        name=body.name, api_type=body.apiType, base_url=body.baseUrl,
        model=body.model, api_key=key, enabled=body.enabled,
    ))


@router.put("/{engine_id}", response_model=EngineOut, dependencies=[Depends(require_api_token)])
def update_engine(
    engine_id: str,
    body: EngineIn,
    store: TranslationEngineStore = Depends(get_translation_engine_store),
) -> EngineOut:
    _validate_type(body.apiType)
    rec = _require(store, engine_id)
    fields = dict(name=body.name, api_type=body.apiType, base_url=body.baseUrl, model=body.model, enabled=body.enabled)
    # 空值表示保持原密钥不变；创建配置时则自然表示未配置。
    if body.apiKey is not None:
        fields["api_key"] = body.apiKey.strip() if body.apiKey.strip() else ""
    updated = store.update(engine_id, **fields)
    return _out(updated or rec)


@router.delete("/{engine_id}", status_code=204, dependencies=[Depends(require_api_token)])
def delete_engine(engine_id: str, store: TranslationEngineStore = Depends(get_translation_engine_store)) -> None:
    _require(store, engine_id)
    store.delete(engine_id)


@router.post("/{engine_id}/validate", response_model=EngineCheckOut, dependencies=[Depends(require_api_token)])
def validate_engine(
    engine_id: str,
    store: TranslationEngineStore = Depends(get_translation_engine_store),
) -> EngineCheckOut:
    rec = _require(store, engine_id)
    checked_at = int(time.time() * 1000)
    if not (rec.api_key and rec.api_key.strip()):
        store.update(engine_id, availability="UNCONFIGURED", last_checked_at=checked_at, last_error="未配置 API Key")
        return EngineCheckOut(id=engine_id, availability="UNCONFIGURED", available=False, checkedAt=checked_at, errorCode="missing_api_key", message="未配置 API Key")

    store.update(engine_id, availability="CHECKING", last_checked_at=checked_at, last_error=None)
    try:
        client = make_engine_client(rec, timeout=VALIDATE_TIMEOUT_SEC)
        client.complete("Reply with OK only.", "OK", max_tokens=4)
    except TranslationEngineError as exc:
        store.update(engine_id, availability="UNAVAILABLE", last_checked_at=checked_at, last_error=str(exc))
        return EngineCheckOut(id=engine_id, availability="UNAVAILABLE", available=False, checkedAt=checked_at, errorCode=exc.code, message=str(exc))

    store.update(engine_id, availability="AVAILABLE", last_checked_at=checked_at, last_error=None)
    return EngineCheckOut(id=engine_id, availability="AVAILABLE", available=True, checkedAt=checked_at)
