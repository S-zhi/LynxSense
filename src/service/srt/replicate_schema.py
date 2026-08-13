"""获取 Replicate 模型入参 schema 的独立工具。

直接运行：
    python src/service/srt/replicate_schema.py

可选：
    python src/service/srt/replicate_schema.py --full
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_MODEL_REF = (
    "stayallive/whisper-subtitles:"
    "b97ba81004e7132181864c885a76cae0e56bc61caa4190a395f6d8ba45b7a969"
)

FALLBACK_LANGUAGES = ["en", "zh", "de", "es", "ru", "ko", "fr", "ja"]
FALLBACK_MODELS = [
    "tiny.en", "tiny", "base.en", "base", "small.en",
    "small", "medium.en", "medium", "large-v1", "large-v2",
]


class TTLCache:
    """一个简单的线程安全的 TTL 缓存。"""
    def __init__(self, ttl_seconds: float = 3600.0):
        self.ttl = ttl_seconds
        self._cache: dict[str, tuple[Any, float]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Any | None:
        with self._lock:
            if key in self._cache:
                val, expires_at = self._cache[key]
                if time.time() < expires_at:
                    return val
                else:
                    del self._cache[key]
            return None

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._cache[key] = (value, time.time() + self.ttl)

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()


_schema_cache = TTLCache(ttl_seconds=3600.0)
_fetch_lock = threading.Lock()
REPLICATE_API_BASE = "https://api.replicate.com/v1"


class ReplicateSchemaError(RuntimeError):
    """获取 Replicate schema 失败。"""


def _project_root() -> Path:
    """返回当前仓库根目录。"""
    return Path(__file__).resolve().parents[3]


def _load_env_file(env_path: Path | None = None) -> None:
    """加载 .env 到环境变量，但不覆盖外部已设置的变量。"""
    path = env_path or (_project_root() / ".env")
    if not path.exists():
        return

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and value:
            os.environ.setdefault(key, value)


def _parse_model_ref(model_ref: str) -> tuple[str, str, str]:
    """把 owner/model:version 解析为 owner、model、version。"""
    model_part, separator, version = model_ref.partition(":")
    if not separator or not version:
        raise ReplicateSchemaError(
            "模型引用必须包含版本号，格式为 owner/model:version"
        )

    parts = model_part.split("/")
    if len(parts) != 2 or not all(parts):
        raise ReplicateSchemaError(
            "模型引用必须包含 owner 和 model，格式为 owner/model:version"
        )

    owner, model = parts
    return owner, model, version


def _auth_headers(api_token: str) -> dict[str, str]:
    """构造 Replicate API 鉴权请求头。"""
    return {
        "Authorization": f"Bearer {api_token}",
        "Accept": "application/json",
    }


def fetch_replicate_version_schema(
    model_ref: str = DEFAULT_MODEL_REF,
    *,
    api_token: str | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """获取指定 Replicate 模型版本的完整 OpenAPI schema。"""
    cached = _schema_cache.get(model_ref)
    if cached is not None:
        return cached

    with _fetch_lock:
        # 双重检查锁 (Double-checked locking) 确保并发下同一 model_ref 只被拉取一次
        cached = _schema_cache.get(model_ref)
        if cached is not None:
            return cached

        _load_env_file()
        token = api_token or os.getenv("REPLICATE_API_TOKEN")
        if not token:
            raise ReplicateSchemaError(
                "未设置 REPLICATE_API_TOKEN，请通过环境变量或项目根 .env 提供"
            )

        owner, model, version = _parse_model_ref(model_ref)
        url = f"{REPLICATE_API_BASE}/models/{owner}/{model}/versions/{version}"

        try:
            response = httpx.get(url, headers=_auth_headers(token), timeout=timeout)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            body = exc.response.text[:300]
            raise ReplicateSchemaError(
                f"Replicate API 返回错误 {exc.response.status_code}: {body}"
            ) from exc
        except httpx.HTTPError as exc:
            raise ReplicateSchemaError(f"请求 Replicate API 失败: {exc}") from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise ReplicateSchemaError("Replicate API 返回的不是合法 JSON") from exc

        schema = data.get("openapi_schema")
        if not isinstance(schema, dict):
            raise ReplicateSchemaError("Replicate 响应中缺少 openapi_schema")

        _schema_cache.set(model_ref, schema)
        return schema


def get_replicate_input_schema(
    model_ref: str = DEFAULT_MODEL_REF,
    *,
    api_token: str | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """获取指定 Replicate 模型版本的入参 Input schema。"""
    schema = fetch_replicate_version_schema(
        model_ref,
        api_token=api_token,
        timeout=timeout,
    )
    input_schema = (
        schema.get("components", {})
        .get("schemas", {})
        .get("Input")
    )
    if not isinstance(input_schema, dict):
        raise ReplicateSchemaError("OpenAPI schema 中缺少 components.schemas.Input")
    return input_schema


def get_replicate_optional_inputs(
    model_ref: str = DEFAULT_MODEL_REF,
    *,
    api_token: str | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """获取指定 Replicate 模型版本的可选入参定义。"""
    input_schema = get_replicate_input_schema(
        model_ref,
        api_token=api_token,
        timeout=timeout,
    )
    required = set(input_schema.get("required") or [])
    properties = input_schema.get("properties") or {}
    if not isinstance(properties, dict):
        raise ReplicateSchemaError("Input schema 中 properties 格式异常")
    return {
        name: schema
        for name, schema in properties.items()
        if name not in required
    }


def _get_enum_values(input_schema: dict[str, Any], field_name: str) -> list[str]:
    """从 Input schema 的指定字段里提取 enum 字符串列表。"""
    properties = input_schema.get("properties") or {}
    if not isinstance(properties, dict):
        raise ReplicateSchemaError("Input schema 中 properties 格式异常")

    field_schema = properties.get(field_name) or {}
    if not isinstance(field_schema, dict):
        raise ReplicateSchemaError(f"Input schema 中 {field_name} 字段格式异常")

    enum_values = field_schema.get("enum") or []
    if not isinstance(enum_values, list) or not all(
        isinstance(item, str) for item in enum_values
    ):
        raise ReplicateSchemaError(f"Input schema 中 {field_name}.enum 格式异常")
    return enum_values


def get_video_language_options(
    model_ref: str = DEFAULT_MODEL_REF,
    *,
    api_token: str | None = None,
    timeout: float = 30.0,
) -> list[str]:
    """获取视频源语言可选值列表。"""
    try:
        input_schema = get_replicate_input_schema(
            model_ref,
            api_token=api_token,
            timeout=timeout,
        )
        return _get_enum_values(input_schema, "language")
    except Exception as exc:
        logger.warning(
            f"获取 Replicate 语言选项失败，使用内置兜底列表: {exc}"
        )
        return FALLBACK_LANGUAGES


def get_whisper_model_weight_options(
    model_ref: str = DEFAULT_MODEL_REF,
    *,
    api_token: str | None = None,
    timeout: float = 30.0,
) -> list[str]:
    """获取 Whisper 模型权重可选值列表。"""
    try:
        input_schema = get_replicate_input_schema(
            model_ref,
            api_token=api_token,
            timeout=timeout,
        )
        return _get_enum_values(input_schema, "model_name")
    except Exception as exc:
        logger.warning(
            f"获取 Replicate 模型权重选项失败，使用内置兜底列表: {exc}"
        )
        return FALLBACK_MODELS


def _build_parser() -> argparse.ArgumentParser:
    """构造命令行参数解析器。"""
    parser = argparse.ArgumentParser(
        description="获取 Replicate 模型版本的入参 schema"
    )
    parser.add_argument(
        "--model-ref",
        default=DEFAULT_MODEL_REF,
        help="Replicate 模型引用，格式 owner/model:version",
    )
    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument(
        "--full",
        action="store_true",
        help="输出完整 OpenAPI schema，而不是 Input schema",
    )
    output_group.add_argument(
        "--optional-only",
        action="store_true",
        help="只输出可选入参定义",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="HTTP 请求超时时间，单位秒",
    )
    return parser


def main() -> int:
    """命令行入口：打印模型 schema JSON。"""
    parser = _build_parser()
    args = parser.parse_args()

    try:
        if args.full:
            result = fetch_replicate_version_schema(
                args.model_ref,
                timeout=args.timeout,
            )
        elif args.optional_only:
            result = get_replicate_optional_inputs(
                args.model_ref,
                timeout=args.timeout,
            )
        else:
            result = get_replicate_input_schema(
                args.model_ref,
                timeout=args.timeout,
            )
    except ReplicateSchemaError as exc:
        parser.exit(1, f"{exc}\n")

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
