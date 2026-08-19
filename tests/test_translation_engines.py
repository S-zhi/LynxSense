"""可配置翻译引擎的存储与协议适配测试。"""

from __future__ import annotations

from src.core.translation_engines import EngineClient
from src.store.translation_engine_store import TranslationEngineStore


def test_translation_engine_store_seeds_deepseek_once(tmp_path):
    db = tmp_path / "engines.db"
    store = TranslationEngineStore(db)

    seeded = store.ensure_default_deepseek(
        api_key="env-key",
        base_url="https://api.deepseek.com/",
        model="deepseek-chat",
    )
    assert seeded.id == "deepseek"
    assert seeded.name == "DeepSeek"
    assert seeded.api_type == "openai_compatible"
    assert seeded.base_url == "https://api.deepseek.com"
    assert seeded.model == "deepseek-chat"
    assert seeded.has_api_key is True
    assert seeded.availability == "UNKNOWN"

    # 启动流程可重复执行，且不覆盖用户后续修改。
    store.update("deepseek", name="我的 DeepSeek")
    again = store.ensure_default_deepseek(api_key="another-key")
    assert again.id == "deepseek"
    assert again.name == "我的 DeepSeek"
    assert again.api_key == "env-key"


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


def test_make_engine_client_custom_timeout():
    from src.core.translation_engines import make_engine_client
    config = {
        "api_type": "openai_compatible",
        "base_url": "https://example.test",
        "model": "demo",
        "api_key": "key",
    }
    client = make_engine_client(config, timeout=10)
    assert client.timeout == 10

    default_client = make_engine_client(config)
    assert default_client.timeout == 60


def test_reset_dangling_checking(tmp_path):
    db = tmp_path / "engines.db"
    store = TranslationEngineStore(db)

    # 1. 配置有 API key，被置为 CHECKING (如进程崩溃残留)
    c1 = store.create(
        name="Engine 1", api_type="openai_compatible",
        base_url="https://e1.test", model="m1", api_key="k1",
    )
    store.update(c1.id, availability="CHECKING")

    # 2. 未配置 API key，被置为 CHECKING
    c2 = store.create(
        name="Engine 2", api_type="openai_compatible",
        base_url="https://e2.test", model="m2", api_key=None,
    )
    # 强制设置空 key 和 CHECKING
    with store._connect() as conn:
        conn.execute("UPDATE translation_engines SET availability = 'CHECKING', api_key = '' WHERE id = ?", (c2.id,))

    # 重置悬空 CHECKING
    reset_count = store.reset_dangling_checking()
    assert reset_count == 2

    e1 = store.get(c1.id)
    assert e1.availability == "UNKNOWN"

    e2 = store.get(c2.id)
    assert e2.availability == "UNCONFIGURED"


def test_validate_engine_uses_shorter_timeout(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from src.handler.app import app
    from src.handler.deps import get_translation_engine_store, reset_singletons
    from src.handler.translation_engines import VALIDATE_TIMEOUT_SEC

    assert VALIDATE_TIMEOUT_SEC == 10

    db = tmp_path / "engines.db"
    store = TranslationEngineStore(db)
    rec = store.create(
        name="Gateway", api_type="openai_compatible",
        base_url="https://gateway.test", model="m1", api_key="sk-test",
    )

    reset_singletons()
    app.dependency_overrides[get_translation_engine_store] = lambda: store

    seen_timeout = []

    def fake_make_engine_client(config, timeout=None):
        seen_timeout.append(timeout)
        class FakeClient:
            def complete(self, sys, user, max_tokens=4):
                return "OK"
        return FakeClient()

    monkeypatch.setattr("src.handler.translation_engines.make_engine_client", fake_make_engine_client)

    with TestClient(app) as client:
        r = client.post(f"/api/settings/translation-engines/{rec.id}/validate")
        assert r.status_code == 200
        data = r.json()
        assert data["available"] is True
        assert data["availability"] == "AVAILABLE"
        assert seen_timeout == [10]

    updated = store.get(rec.id)
    assert updated.availability == "AVAILABLE"

    app.dependency_overrides.clear()
    reset_singletons()
