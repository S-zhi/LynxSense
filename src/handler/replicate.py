"""Replicate 账户状态 API。"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from src.service.replicate_account import query_replicate_balance


router = APIRouter(prefix="/api/replicate", tags=["replicate"])


class ReplicateAccountOut(BaseModel):
    type: Optional[str] = None
    username: Optional[str] = None
    name: Optional[str] = None


class ReplicateBalanceOut(BaseModel):
    status: str
    authenticated: bool
    account: Optional[ReplicateAccountOut] = None
    balance: Optional[float] = None
    currency: str = "USD"
    balanceSupported: bool = False
    source: str
    billingUrl: str
    checkedAt: int
    errorCode: Optional[str] = None
    message: str


@router.get("/balance", response_model=ReplicateBalanceOut)
def get_balance() -> ReplicateBalanceOut:
    """查询 Replicate Token 状态；余额由官方响应提供时才展示。"""
    return ReplicateBalanceOut(**query_replicate_balance())
