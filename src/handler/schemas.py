"""API 请求 / 响应模型。

字段用 camelCase，直接对齐前端契约（web/app.js 的 RealApi），
这样前端切真实后端时无需改字段。
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from src.store import RESOURCE_STATUS_AVAILABLE, RESOURCE_STATUS_MISSING, TaskRecord


class TaskCreate(BaseModel):
    """POST /api/tasks 的请求体。"""

    url: str
    sourceLang: str = Field(default="auto", min_length=1)
    targetLang: str = Field(default="zh-CN", min_length=1)
    mode: Literal["mono", "bilingual"] = "mono"
    burn: Literal["hard", "soft"] = "hard"
    model: str = Field(default="small", min_length=1)
    engine: Literal["deepseek"] = "deepseek"
    needSubtitle: bool = True  # False = 仅下载视频，跳过识别/翻译/烧录


class TaskProbeIn(BaseModel):
    """POST /api/tasks/probe 的请求体。"""

    url: str = Field(min_length=1)


class TaskProbeOut(BaseModel):
    """视频链接探针的响应体。"""

    ok: bool
    title: Optional[str] = None
    extractor: Optional[str] = None
    duration: Optional[float] = None
    formatsCount: int = 0
    webpageUrl: Optional[str] = None
    reason: Optional[str] = None
    detail: Optional[str] = None


class TaskOut(BaseModel):
    """任务对象的响应形态。"""

    id: str
    url: str
    title: Optional[str]
    sourceLang: str
    targetLang: str
    mode: str
    burn: str
    model: str
    engine: str
    sourceType: str
    needSubtitle: bool
    status: str
    progress: int
    currentStep: Optional[str]
    error: Optional[str]
    outputs: Optional[dict]
    resourceStatus: str  # AVAILABLE | MISSING — 任务产物文件是否在盘
    createdAt: int
    updatedAt: int


def to_out(rec: TaskRecord) -> TaskOut:
    """TaskRecord(snake_case) -> TaskOut(camelCase)。

    outputs 暴露规则：
    - 任务不是 SUCCESS：None（流水线还在跑 / 失败了都不会给下载链接）
    - SUCCESS 但 resource_status == MISSING：None（产物已被清理，不再暴露失效链接）
    - SUCCESS + AVAILABLE：按 need_subtitle 拼出 video / subtitle 链接
    """
    need_subtitle = bool(rec.need_subtitle)
    resource_status = rec.resource_status or RESOURCE_STATUS_AVAILABLE
    outputs = None
    if rec.status == "SUCCESS" and resource_status == RESOURCE_STATUS_AVAILABLE:
        outputs = {"video": f"/api/tasks/{rec.id}/download"}
        if need_subtitle:
            outputs["subtitle"] = f"/api/tasks/{rec.id}/subtitle"
    return TaskOut(
        id=rec.id,
        url=rec.url,
        title=rec.title,
        sourceLang=rec.source_lang,
        targetLang=rec.target_lang,
        mode=rec.mode,
        burn=rec.burn,
        model=rec.model,
        engine=rec.engine,
        sourceType=rec.source_type,
        needSubtitle=need_subtitle,
        status=rec.status,
        progress=rec.progress,
        currentStep=rec.current_step,
        error=rec.error,
        outputs=outputs,
        resourceStatus=resource_status,
        createdAt=rec.created_at,
        updatedAt=rec.updated_at,
    )


# 暴露给前端 / 文档的状态字面量，避免散落字符串
RESOURCE_STATUS_VALUES = (RESOURCE_STATUS_AVAILABLE, RESOURCE_STATUS_MISSING)
