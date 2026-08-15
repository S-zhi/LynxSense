"""持久化翻译引擎配置。

引擎配置保存的是协议、地址、模型和密钥；API 层只返回脱敏后的视图。
当前应用是本地工作台，因此密钥与 SQLite 位于同一受限本地数据目录，业务代码
不会把密钥写入日志、任务记录或前端响应。
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


ENGINE_TYPES = ("openai_compatible", "anthropic_compatible")
AVAILABILITY = ("UNCONFIGURED", "UNKNOWN", "CHECKING", "AVAILABLE", "UNAVAILABLE")


@dataclass
class TranslationEngine:
    id: str
    name: str
    api_type: str
    base_url: str
    model: str
    api_key: Optional[str] = None
    enabled: int = 1
    availability: str = "UNCONFIGURED"
    last_checked_at: Optional[int] = None
    last_error: Optional[str] = None
    created_at: int = 0
    updated_at: int = 0

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_key)


def _now_ms() -> int:
    return int(time.time() * 1000)


class TranslationEngineStore:
    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS translation_engines (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    api_type TEXT NOT NULL,
                    base_url TEXT NOT NULL,
                    model TEXT NOT NULL,
                    api_key TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    availability TEXT NOT NULL DEFAULT 'UNCONFIGURED',
                    last_checked_at INTEGER,
                    last_error TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )

    def list(self) -> List[TranslationEngine]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM translation_engines ORDER BY created_at ASC").fetchall()
        return [_row_to_engine(row) for row in rows]

    def get(self, engine_id: str) -> Optional[TranslationEngine]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM translation_engines WHERE id = ?", (engine_id,)).fetchone()
        return _row_to_engine(row) if row else None

    def create(
        self,
        *,
        name: str,
        api_type: str,
        base_url: str,
        model: str,
        api_key: Optional[str] = None,
        enabled: bool = True,
    ) -> TranslationEngine:
        now = _now_ms()
        rec = TranslationEngine(
            id="engine_" + uuid.uuid4().hex[:10],
            name=name.strip(), api_type=api_type, base_url=base_url.strip().rstrip("/"),
            model=model.strip(), api_key=api_key or None, enabled=int(enabled),
            availability="UNKNOWN" if api_key else "UNCONFIGURED",
            created_at=now, updated_at=now,
        )
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO translation_engines
                (id, name, api_type, base_url, model, api_key, enabled, availability,
                 last_checked_at, last_error, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (rec.id, rec.name, rec.api_type, rec.base_url, rec.model, rec.api_key,
                 rec.enabled, rec.availability, None, None, rec.created_at, rec.updated_at),
            )
        return rec

    def update(self, engine_id: str, **fields) -> Optional[TranslationEngine]:
        rec = self.get(engine_id)
        if rec is None:
            return None
        allowed = {k: v for k, v in fields.items() if k in {
            "name", "api_type", "base_url", "model", "api_key", "enabled",
            "availability", "last_checked_at", "last_error",
        }}
        if "base_url" in allowed:
            allowed["base_url"] = str(allowed["base_url"]).strip().rstrip("/")
        if "api_key" in allowed and allowed["api_key"] == "":
            allowed["api_key"] = rec.api_key
        if "api_key" in allowed and allowed["api_key"]:
            allowed.setdefault("availability", "UNKNOWN")
        elif "api_key" in allowed and not allowed["api_key"]:
            allowed["availability"] = "UNCONFIGURED"
        if not allowed:
            return rec
        allowed["updated_at"] = _now_ms()
        assignments = ", ".join(f"{key} = ?" for key in allowed)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE translation_engines SET {assignments} WHERE id = ?",
                (*allowed.values(), engine_id),
            )
        return self.get(engine_id)

    def delete(self, engine_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM translation_engines WHERE id = ?", (engine_id,))
        return cur.rowcount > 0


def _row_to_engine(row: sqlite3.Row) -> TranslationEngine:
    return TranslationEngine(**dict(row))
