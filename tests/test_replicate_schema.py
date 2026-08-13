"""Replicate schema 缓存与兜底机制测试。"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch
import pytest
import httpx

from src.service.srt.replicate_schema import (
    TTLCache,
    _schema_cache,
    fetch_replicate_version_schema,
    get_video_language_options,
    get_whisper_model_weight_options,
    FALLBACK_LANGUAGES,
    FALLBACK_MODELS,
    ReplicateSchemaError,
)


@pytest.fixture(autouse=True)
def clear_global_cache():
    _schema_cache.clear()
    yield
    _schema_cache.clear()


def test_ttl_cache_basic():
    cache = TTLCache(ttl_seconds=10.0)
    cache.set("key", "value")
    assert cache.get("key") == "value"
    assert cache.get("non-existent") is None


def test_ttl_cache_expiration():
    cache = TTLCache(ttl_seconds=5.0)
    with patch("time.time", return_value=100.0):
        cache.set("key", "value")
        assert cache.get("key") == "value"

    with patch("time.time", return_value=106.0):
        assert cache.get("key") is None


def test_ttl_cache_clear():
    cache = TTLCache(ttl_seconds=10.0)
    cache.set("key", "value")
    cache.clear()
    assert cache.get("key") is None


def test_fetch_schema_uses_cache():
    mock_schema = {
        "openapi_schema": {
            "components": {
                "schemas": {
                    "Input": {
                        "properties": {
                            "language": {"enum": ["en", "zh"]},
                            "model_name": {"enum": ["tiny", "base"]},
                        },
                        "required": [],
                    }
                }
            }
        }
    }

    with patch("httpx.get") as mock_get, patch("os.getenv", return_value="fake-token"):
        mock_response = MagicMock()
        mock_response.json.return_value = mock_schema
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        # 第一次获取，请求外部 API
        schema1 = fetch_replicate_version_schema()
        assert schema1 == mock_schema["openapi_schema"]
        assert mock_get.call_count == 1

        # 第二次获取，使用缓存，无额外外部请求
        schema2 = fetch_replicate_version_schema()
        assert schema2 == mock_schema["openapi_schema"]
        assert mock_get.call_count == 1


def test_shared_cache_for_languages_and_models():
    mock_schema = {
        "openapi_schema": {
            "components": {
                "schemas": {
                    "Input": {
                        "properties": {
                            "language": {"enum": ["en", "zh", "fr"]},
                            "model_name": {"enum": ["tiny", "base", "small"]},
                        },
                        "required": [],
                    }
                }
            }
        }
    }

    with patch("httpx.get") as mock_get, patch("os.getenv", return_value="fake-token"):
        mock_response = MagicMock()
        mock_response.json.return_value = mock_schema
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        # 获取语言列表
        langs = get_video_language_options()
        assert langs == ["en", "zh", "fr"]
        assert mock_get.call_count == 1

        # 获取模型权重列表（应共享相同的 schema 缓存）
        models = get_whisper_model_weight_options()
        assert models == ["tiny", "base", "small"]
        assert mock_get.call_count == 1


def test_fallback_when_token_missing():
    # 当 REPLICATE_API_TOKEN 缺失时，应返回内置兜底，而不是抛错
    with patch("os.getenv", return_value=""):
        langs = get_video_language_options()
        assert langs == FALLBACK_LANGUAGES

        models = get_whisper_model_weight_options()
        assert models == FALLBACK_MODELS


def test_fallback_on_network_error():
    # 当网络异常时，应返回内置兜底，而不是抛错
    with patch("httpx.get", side_effect=httpx.HTTPError("Connection failed")), patch("os.getenv", return_value="fake-token"):
        langs = get_video_language_options()
        assert langs == FALLBACK_LANGUAGES

        models = get_whisper_model_weight_options()
        assert models == FALLBACK_MODELS


def test_fallback_on_invalid_response_format():
    # 当返回不合法结构时，应返回内置兜底，而不是抛错
    with patch("httpx.get") as mock_get, patch("os.getenv", return_value="fake-token"):
        mock_response = MagicMock()
        mock_response.json.return_value = {"invalid_structure": True}
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        langs = get_video_language_options()
        assert langs == FALLBACK_LANGUAGES

        models = get_whisper_model_weight_options()
        assert models == FALLBACK_MODELS
