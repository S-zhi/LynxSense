"""Replicate 账户状态查询测试。"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from src.handler import replicate as replicate_handler
from src.handler.app import app
from src.service import replicate_account


@pytest.fixture(autouse=True)
def _clear_cache():
    replicate_account.clear_cache()
    yield
    replicate_account.clear_cache()


def _response(payload, status_code=200):
    return httpx.Response(status_code, json=payload)


def test_missing_token_is_unconfigured(monkeypatch):
    monkeypatch.delenv("REPLICATE_API_TOKEN", raising=False)

    result = replicate_account.query_replicate_balance()

    assert result["status"] == "unconfigured"
    assert result["balance"] is None
    assert result["errorCode"] == "missing_api_token"


def test_official_account_api_without_balance_is_explicitly_unsupported(monkeypatch):
    calls = []
    monkeypatch.setattr(
        replicate_account.httpx,
        "get",
        lambda url, **kwargs: (calls.append((url, kwargs)) or _response({
            "type": "user", "username": "alice", "name": "Alice",
        })),
    )

    result = replicate_account.query_replicate_balance(api_token="r8_secret")

    assert result["status"] == "unsupported"
    assert result["authenticated"] is True
    assert result["account"] == {"type": "user", "username": "alice", "name": "Alice"}
    assert result["balance"] is None
    assert result["balanceSupported"] is False
    assert calls[0][0] == replicate_account.REPLICATE_ACCOUNT_URL
    assert calls[0][1]["headers"]["Authorization"] == "Bearer r8_secret"


def test_future_numeric_balance_field_is_forward_compatible(monkeypatch):
    monkeypatch.setattr(
        replicate_account.httpx,
        "get",
        lambda *_args, **_kwargs: _response({
            "type": "organization", "username": "acme", "balance": 12.5,
            "currency": "USD",
        }),
    )

    result = replicate_account.query_replicate_balance(api_token="r8_secret")

    assert result["status"] == "available"
    assert result["balance"] == 12.5
    assert result["currency"] == "USD"


def test_invalid_token_does_not_expose_token(monkeypatch):
    monkeypatch.setattr(
        replicate_account.httpx,
        "get",
        lambda *_args, **_kwargs: _response({"detail": "unauthorized"}, status_code=401),
    )

    result = replicate_account.query_replicate_balance(api_token="r8_secret")

    assert result["status"] == "error"
    assert result["errorCode"] == "invalid_api_token"
    assert "r8_secret" not in str(result)


def test_balance_route_returns_safe_shape(monkeypatch):
    monkeypatch.setattr(
        replicate_handler,
        "query_replicate_balance",
        lambda: {
            "status": "unsupported",
            "authenticated": True,
            "account": {"type": "user", "username": "alice", "name": "Alice"},
            "balance": None,
            "currency": "USD",
            "balanceSupported": False,
            "source": "official_account_api",
            "billingUrl": "https://replicate.com/account/billing",
            "checkedAt": 123,
            "errorCode": None,
            "message": "官方未返回余额字段",
        },
    )

    response = TestClient(app).get("/api/replicate/balance")

    assert response.status_code == 200
    assert response.json()["status"] == "unsupported"
    assert response.json()["account"]["username"] == "alice"


def test_ttl_caching_and_force_refresh(monkeypatch):
    calls = []
    monkeypatch.setattr(
        replicate_account.httpx,
        "get",
        lambda url, **kwargs: (calls.append((url, kwargs)) or _response({
            "type": "user", "username": "alice",
        })),
    )

    res1 = replicate_account.query_replicate_balance(api_token="r8_cached", ttl_sec=60)
    assert len(calls) == 1
    assert res1["cached"] is False
    assert res1["account"]["username"] == "alice"

    res2 = replicate_account.query_replicate_balance(api_token="r8_cached", ttl_sec=60)
    assert len(calls) == 1
    assert res2["cached"] is True
    assert res2["account"]["username"] == "alice"

    res3 = replicate_account.query_replicate_balance(
        api_token="r8_cached", ttl_sec=60, force_refresh=True
    )
    assert len(calls) == 2
    assert res3["cached"] is False


def test_rate_limit_and_server_error_caching(monkeypatch):
    calls = []
    monkeypatch.setattr(
        replicate_account.httpx,
        "get",
        lambda url, **kwargs: (calls.append((url, kwargs)) or _response(
            {"detail": "rate limited"}, status_code=429
        )),
    )

    res1 = replicate_account.query_replicate_balance(api_token="r8_ratelimited", ttl_sec=60)
    assert len(calls) == 1
    assert res1["status"] == "unavailable"
    assert res1["errorCode"] == "http_429"
    assert res1["cached"] is False

    res2 = replicate_account.query_replicate_balance(api_token="r8_ratelimited", ttl_sec=60)
    assert len(calls) == 1
    assert res2["status"] == "unavailable"
    assert res2["errorCode"] == "http_429"
    assert res2["cached"] is True
