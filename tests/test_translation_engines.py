"""可配置翻译引擎的存储与协议适配测试。"""

from __future__ import annotations

from src.core.translation_engines import EngineClient
from src.store.translation_engine_store import TranslationEngineStore


def test_translation_engine_store_persists_without_exposing_model(tmp_path):
    db = tmp_path / "engines.db"
    store = TranslationEngineStore(db)
    created = store.create(
        name="本地 OpenAI 网关", api_type="openai_compatible",
        base_url="http://localhost:9000/v1/", model="qwen-plus", api_key="secret",
    )
    loaded = TranslationEngineStore(db).get(created.id)
    assert loaded is not None
    assert loaded.name == "本地 OpenAI 网关"
    assert loaded.base_url == "http://localhost:9000/v1"
    assert loaded.has_api_key is True


def test_openai_compatible_payload(monkeypatch):
    seen = {}

    class Response:
        def raise_for_status(self): pass
        def json(self): return {"choices": [{"message": {"content": "OK"}}]}

    def fake_post(url, **kwargs):
        seen["url"] = url
        seen.update(kwargs)
        return Response()

    monkeypatch.setattr("httpx.post", fake_post)
    result = EngineClient("openai_compatible", "https://example.test/v1", "demo", "key").complete("sys", "user")
    assert result == "OK"
    assert seen["url"].endswith("/v1/chat/completions")
    assert seen["headers"]["Authorization"] == "Bearer key"
    assert seen["json"]["model"] == "demo"


def test_anthropic_compatible_payload(monkeypatch):
    seen = {}

    class Response:
        def raise_for_status(self): pass
        def json(self): return {"content": [{"type": "text", "text": "OK"}]}

    def fake_post(url, **kwargs):
        seen["url"] = url
        seen.update(kwargs)
        return Response()

    monkeypatch.setattr("httpx.post", fake_post)
    result = EngineClient("anthropic_compatible", "https://example.test", "claude-demo", "key").complete("sys", "user")
    assert result == "OK"
    assert seen["url"].endswith("/v1/messages")
    assert seen["headers"]["x-api-key"] == "key"
    assert seen["json"]["system"] == "sys"
