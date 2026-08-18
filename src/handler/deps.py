"""路由共享依赖。"""

from __future__ import annotations

import time
from threading import Lock

from fastapi import Depends, HTTPException, Request

from src.config import settings
from src.store import ProbeStore, TaskStore, TranslationEngineStore

# 存储单例（懒加载，便于测试用依赖覆盖替换）
_store: TaskStore | None = None
_probe_store: ProbeStore | None = None
_translation_engine_store: TranslationEngineStore | None = None


class TokenBucketRateLimiter:
    """按 IP 的内存 Token Bucket 限流器。"""

    def __init__(self, capacity: float = 10.0, refill_rate: float = 10.0 / 60.0) -> None:
        self.capacity = capacity
        self.refill_rate = refill_rate
        self.buckets: dict[str, tuple[float, float]] = {}
        self.lock = Lock()

    def acquire(self, client_ip: str, tokens: float = 1.0) -> bool:
        now = time.time()
        with self.lock:
            if len(self.buckets) > 10000:
                self.buckets = {
                    ip: (tok, t) for ip, (tok, t) in self.buckets.items() if now - t < 3600
                }
            curr_tokens, last_time = self.buckets.get(client_ip, (self.capacity, now))
            elapsed = max(0.0, now - last_time)
            curr_tokens = min(self.capacity, curr_tokens + elapsed * self.refill_rate)
            if curr_tokens >= tokens:
                self.buckets[client_ip] = (curr_tokens - tokens, now)
                return True
            else:
                self.buckets[client_ip] = (curr_tokens, now)
                return False

    def reset(self) -> None:
        with self.lock:
            self.buckets.clear()


_cleanup_limiter = TokenBucketRateLimiter(capacity=10.0, refill_rate=10.0 / 60.0)


def require_api_token(request: Request) -> None:
    """校验 API Token（如 SUBTRANS_API_TOKEN 已配置）。"""
    expected = settings.api_token
    if not expected:
        return

    auth_header = request.headers.get("Authorization", "").strip()
    token = None
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
    elif auth_header:
        token = auth_header

    if not token:
        token = request.headers.get("X-API-Token", "").strip()

    if token != expected:
        raise HTTPException(status_code=401, detail="无效或缺失 API Token")


def rate_limit_cleanup(request: Request) -> None:
    """POST /api/storage/cleanup 的 Token Bucket 限流（同 IP 10 次/分钟）。"""
    client_ip = request.client.host if request.client else "127.0.0.1"
    if not _cleanup_limiter.acquire(client_ip):
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")


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
