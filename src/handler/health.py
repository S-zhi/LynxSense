"""健康检查路由。"""

from __future__ import annotations

from fastapi import APIRouter

from src.service.runtime_check import build_readiness

router = APIRouter(tags=["health"])


@router.get("/api/health")
def health() -> dict:
    return {"ok": True}


@router.get("/api/health/ready")
def readiness() -> dict:
    """返回脱敏的运行时能力状态，供 MCP 和运维检查使用。"""
    return build_readiness()
