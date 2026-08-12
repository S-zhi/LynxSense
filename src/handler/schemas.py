"""API 请求 / 响应模型。

字段用 camelCase，直接对齐前端契约（web/app.js 的 RealApi），
这样前端切真实后端时无需改字段。
"""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field

from src.store import TaskRecord


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


class ResumeOption(BaseModel):
    """断点续跑选项：表示可以"从这一步开始重跑"。"""

    step: str
    label: str
    available: bool
    reason: Optional[str] = None


class ResumeOptionsOut(BaseModel):
    """GET /api/tasks/{id}/resume_options 的响应。"""

    taskId: str
    status: str
    # 已成功完成的阶段名（按顺序）；可能为空（旧数据或新任务）
    completedSteps: List[str] = Field(default_factory=list)
    # 上次失败倒下的阶段（FAILED 任务才有）
    lastErrorStep: Optional[str] = None
    options: List[ResumeOption]


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
    completedSteps: List[str] = Field(default_factory=list)
    lastErrorStep: Optional[str] = None
    createdAt: int
    updatedAt: int


def to_out(rec: TaskRecord) -> TaskOut:
    """TaskRecord(snake_case) -> TaskOut(camelCase)。"""
    need_subtitle = bool(rec.need_subtitle)
    outputs = None
    if rec.status == "SUCCESS":
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
        completedSteps=list(rec.completed_steps or []),
        lastErrorStep=rec.last_error_step,
        createdAt=rec.created_at,
        updatedAt=rec.updated_at,
    )
