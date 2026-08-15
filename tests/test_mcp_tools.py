import asyncio

import pytest

from src.mcp_server.tools import SubtitleMcpTools


class FakeBusinessApi:
    def __init__(self):
        self.created_payload = None

    async def readiness(self):
        return {
            "ok": True,
            "capabilities": {
                "download": True,
                "full_pipeline": True,
                "hard_burn": True,
            },
        }

    async def probe_video(self, url):
        return {"ok": True, "title": "Demo", "webpageUrl": url}

    async def create_task(self, payload):
        self.created_payload = payload
        return {
            "id": "task_1",
            "status": "PENDING",
            "progress": 0,
            "currentStep": None,
            "resourceStatus": "AVAILABLE",
        }

    async def get_task(self, task_id):
        return {
            "id": task_id,
            "status": "SUCCESS",
            "progress": 100,
            "resourceStatus": "AVAILABLE",
            "outputs": {
                "video": "/api/tasks/task_1/download",
                "subtitle": "/api/tasks/task_1/subtitle",
            },
        }

    async def list_tasks(self):
        return []

    async def retry_task(self, task_id):
        return {"id": task_id, "status": "PENDING"}

    def absolute_url(self, path):
        return f"http://business.test/{path.lstrip('/')}"


def _run(coro):
    return asyncio.run(coro)


def test_start_pipeline_returns_task_id_and_uses_business_contract():
    api = FakeBusinessApi()
    tools = SubtitleMcpTools(api)

    result = _run(tools.start_subtitle_pipeline(
        "https://example.test/video",
        target_lang="ja",
        mode="bilingual",
        burn="soft",
    ))

    assert result["ok"] is True
    assert result["task_id"] == "task_1"
    assert api.created_payload == {
        "url": "https://example.test/video",
        "sourceLang": "auto",
        "targetLang": "ja",
        "mode": "bilingual",
        "burn": "soft",
        "model": "small",
        "engine": "deepseek",
        "needSubtitle": True,
    }


def test_artifacts_are_returned_as_absolute_urls():
    tools = SubtitleMcpTools(FakeBusinessApi())

    result = _run(tools.get_task_artifacts("task_1"))

    assert result == {
        "ok": True,
        "task_id": "task_1",
        "artifacts": [
            {
                "type": "video",
                "filename": "task_1.mp4",
                "download_url": "http://business.test/api/tasks/task_1/download",
            },
            {
                "type": "subtitle",
                "filename": "task_1.srt",
                "download_url": "http://business.test/api/tasks/task_1/subtitle",
            },
        ],
    }


def test_start_pipeline_reports_initialization_failure():
    class NotReadyApi(FakeBusinessApi):
        async def readiness(self):
            return {
                "ok": False,
                "capabilities": {"download": False, "full_pipeline": False},
                "config_file": "/project/.env",
                "missing": ["REPLICATE_API_TOKEN"],
            }

    result = _run(SubtitleMcpTools(NotReadyApi()).start_subtitle_pipeline(
        "https://example.test/video"
    ))

    assert result["ok"] is False
    assert result["error_code"] == "NOT_INITIALIZED"
    assert result["missing"] == ["REPLICATE_API_TOKEN"]
