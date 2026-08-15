import asyncio

import httpx
import pytest

from src.mcp_server.business_client import (
    BusinessApiClient,
    BusinessApiConfig,
    BusinessApiError,
)


def _run(coro):
    return asyncio.run(coro)


def test_business_client_posts_task_payload():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/tasks"
        assert request.headers["accept"] == "application/json"
        assert request.read()  # request body is present
        return httpx.Response(201, json={"id": "task_1", "status": "PENDING"})

    client = BusinessApiClient(
        BusinessApiConfig(base_url="http://business.test"),
        transport=httpx.MockTransport(handler),
    )

    result = _run(client.create_task({"url": "https://example.test/video"}))

    assert result == {"id": "task_1", "status": "PENDING"}


def test_business_client_maps_missing_task():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "任务不存在"})

    client = BusinessApiClient(
        BusinessApiConfig(base_url="http://business.test"),
        transport=httpx.MockTransport(handler),
    )

    async def call():
        with pytest.raises(BusinessApiError) as exc_info:
            await client.get_task("missing")
        return exc_info

    exc_info = _run(call())

    assert exc_info.value.code == "TASK_NOT_FOUND"
    assert exc_info.value.message == "任务不存在"
