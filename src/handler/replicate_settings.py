"""Replicate 运行时配置 API。

页面保存的配置写入运行时设置文件，并在配置层覆盖同名环境变量。Token 永远不
会原样返回给浏览器；请求体中的 ``apiToken=null`` 表示保持已有 Token 不变。
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.config import settings
from src.config.config import update_runtime_settings
from src.handler.deps import require_api_token


router = APIRouter(prefix="/api/settings/replicate", tags=["replicate-settings"])


class ReplicateSettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # null means "leave the current token unchanged"; an empty string clears the
    # page override so the environment value becomes effective again.
    apiToken: Optional[str] = Field(default=None, max_length=500)
    whisperModel: str = Field(min_length=1, max_length=500)
    timeout: int = Field(ge=1, le=86400)
    retries: int = Field(ge=1, le=10)
    retryInterval: float = Field(ge=0, le=86400)
    pollInterval: float = Field(ge=1, le=3600)

    @field_validator("whisperModel")
    @classmethod
    def validate_model(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("模型版本不能为空")
        return value


class ReplicateSettingsOut(BaseModel):
    hasApiToken: bool
    whisperModel: str
    timeout: int
    retries: int
    retryInterval: float
    pollInterval: float


def _out() -> ReplicateSettingsOut:
    return ReplicateSettingsOut(
        hasApiToken=bool(settings.replicate_api_token),
        whisperModel=settings.replicate_whisper_model,
        timeout=settings.replicate_timeout,
        retries=settings.replicate_retries,
        retryInterval=settings.replicate_retry_interval,
        pollInterval=settings.replicate_poll_interval,
    )


@router.get("", response_model=ReplicateSettingsOut)
def get_replicate_settings() -> ReplicateSettingsOut:
    return _out()


@router.put("", response_model=ReplicateSettingsOut, dependencies=[Depends(require_api_token)])
def put_replicate_settings(body: ReplicateSettingsIn) -> ReplicateSettingsOut:
    values = {
        "replicate_whisper_model": body.whisperModel.strip(),
        "replicate_timeout": body.timeout,
        "replicate_retries": body.retries,
        "replicate_retry_interval": body.retryInterval,
        "replicate_poll_interval": body.pollInterval,
    }
    if body.apiToken is not None:
        values["replicate_api_token"] = body.apiToken.strip() or None
    update_runtime_settings(values)
    return _out()
