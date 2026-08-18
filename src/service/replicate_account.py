"""Replicate 账户状态查询。

Replicate 的公开 HTTP API 目前没有余额 / credit balance endpoint。这里调用
官方的 ``GET /v1/account`` 作为安全的 Token 校验和账户识别入口，并兼容未来
官方在账户响应中增加余额字段的情况；不会调用网页内部接口，也不会把 Token
或上游原始响应返回给前端。
"""

from __future__ import annotations

import os
import time
from typing import Any, Optional

import httpx


REPLICATE_ACCOUNT_URL = "https://api.replicate.com/v1/account"
REPLICATE_BILLING_URL = "https://replicate.com/account/billing"
REPLICATE_REQUEST_TIMEOUT = 10.0


def _unconfigured() -> dict[str, Any]:
    return {
        "status": "unconfigured",
        "authenticated": False,
        "account": None,
        "balance": None,
        "currency": "USD",
        "balanceSupported": False,
        "source": "official_account_api",
        "billingUrl": REPLICATE_BILLING_URL,
        "checkedAt": int(time.time() * 1000),
        "errorCode": "missing_api_token",
        "message": "未设置 REPLICATE_API_TOKEN，暂时无法查询 Replicate 账户状态",
    }


def _extract_balance(payload: dict[str, Any]) -> tuple[Optional[float], str]:
    """只提取明确的数值余额，不猜测 token / cents 等其他计量单位。"""
    for key in ("balance", "credit_balance", "creditBalance", "remaining_credits", "remainingCredits"):
        value = payload.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return float(value), str(payload.get("currency") or "USD")
        if isinstance(value, dict):
            amount = value.get("amount", value.get("value"))
            if isinstance(amount, bool):
                continue
            if isinstance(amount, (int, float)):
                return float(amount), str(value.get("currency") or payload.get("currency") or "USD")
    return None, "USD"


def _account_summary(payload: dict[str, Any]) -> dict[str, Optional[str]]:
    """只保留官方账户接口中的非敏感识别字段。"""
    return {
        "type": payload.get("type") if isinstance(payload.get("type"), str) else None,
        "username": payload.get("username") if isinstance(payload.get("username"), str) else None,
        "name": payload.get("name") if isinstance(payload.get("name"), str) else None,
    }


def query_replicate_balance(
    *,
    api_token: Optional[str] = None,
    timeout: float = REPLICATE_REQUEST_TIMEOUT,
) -> dict[str, Any]:
    """查询 Replicate 账户状态，并在官方响应支持时返回余额。

    当前官方 ``/v1/account`` 通常只返回账户身份信息，因此成功但没有余额
    字段时返回 ``status=unsupported``，而不是伪造或估算一个金额。
    """
    token = api_token or os.getenv("REPLICATE_API_TOKEN")
    if not token or not token.strip():
        return _unconfigured()

    checked_at = int(time.time() * 1000)
    try:
        response = httpx.get(
            REPLICATE_ACCOUNT_URL,
            headers={"Authorization": f"Bearer {token.strip()}"},
            timeout=httpx.Timeout(timeout, connect=min(10.0, timeout)),
        )
    except httpx.RequestError:
        return {
            "status": "unavailable",
            "authenticated": False,
            "account": None,
            "balance": None,
            "currency": "USD",
            "balanceSupported": False,
            "source": "official_account_api",
            "billingUrl": REPLICATE_BILLING_URL,
            "checkedAt": checked_at,
            "errorCode": "network_error",
            "message": "无法连接 Replicate 官方 API，请检查网络后重试",
        }

    if response.status_code in (401, 403):
        return {
            "status": "error",
            "authenticated": False,
            "account": None,
            "balance": None,
            "currency": "USD",
            "balanceSupported": False,
            "source": "official_account_api",
            "billingUrl": REPLICATE_BILLING_URL,
            "checkedAt": checked_at,
            "errorCode": "invalid_api_token",
            "message": "Replicate API Token 无效、已过期或没有访问权限",
        }

    if response.status_code >= 400:
        return {
            "status": "unavailable",
            "authenticated": False,
            "account": None,
            "balance": None,
            "currency": "USD",
            "balanceSupported": False,
            "source": "official_account_api",
            "billingUrl": REPLICATE_BILLING_URL,
            "checkedAt": checked_at,
            "errorCode": f"http_{response.status_code}",
            "message": f"Replicate 官方 API 返回 HTTP {response.status_code}",
        }

    try:
        payload = response.json()
    except ValueError:
        return {
            "status": "unavailable",
            "authenticated": False,
            "account": None,
            "balance": None,
            "currency": "USD",
            "balanceSupported": False,
            "source": "official_account_api",
            "billingUrl": REPLICATE_BILLING_URL,
            "checkedAt": checked_at,
            "errorCode": "invalid_response",
            "message": "Replicate 官方 API 返回了无法解析的响应",
        }

    if not isinstance(payload, dict):
        payload = {}
    balance, currency = _extract_balance(payload)
    supported = balance is not None
    return {
        "status": "available" if supported else "unsupported",
        "authenticated": True,
        "account": _account_summary(payload),
        "balance": balance,
        "currency": currency,
        "balanceSupported": supported,
        "source": "official_account_api",
        "billingUrl": REPLICATE_BILLING_URL,
        "checkedAt": checked_at,
        "errorCode": None,
        "message": (
            "已从 Replicate 官方 API 获取余额"
            if supported
            else "Token 有效，但 Replicate 官方公开 API 未返回余额字段；请打开账单页查看"
        ),
    }
