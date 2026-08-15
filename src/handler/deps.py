"""路由共享依赖。"""

from __future__ import annotations

from fastapi import Depends

from src.config import settings
from src.store import ProbeStore, TaskStore, TranslationEngineStore

# 存储单例（懒加载，便于测试用依赖覆盖替换）
_store: TaskStore | None = None
_probe_store: ProbeStore | None = None
_translation_engine_store: TranslationEngineStore | None = None


def get_store() -> TaskStore:
    global _store
    if _store is None:
        _store = TaskStore(settings.db_path)
    return _store


def get_probe_store() -> ProbeStore:
    global _probe_store
    if _probe_store is None:
        _probe_store = ProbeStore(settings.db_path)
    return _probe_store


def get_translation_engine_store() -> TranslationEngineStore:
    global _translation_engine_store
    if _translation_engine_store is None:
        _translation_engine_store = TranslationEngineStore(settings.db_path)
        _translation_engine_store.ensure_default_deepseek(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            model=settings.deepseek_model,
        )
    return _translation_engine_store


def reset_singletons() -> None:
    """供测试在替换 monkeypatch 后重置单例。"""
    global _store, _probe_store, _translation_engine_store
    _store = None
    _probe_store = None
    _translation_engine_store = None
