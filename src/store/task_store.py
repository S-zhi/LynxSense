"""任务记录的 SQLite 存储。

单文件数据库，零服务进程；开启 WAL 以支持 API 进程读、Worker 进程写并发。
字段与前端契约对齐（status / progress / current_step / outputs / 时间戳）。
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional

# 与流水线状态机一致
STATUSES = (
    "PENDING",
    "DOWNLOADING",
    "EXTRACTING",
    "TRANSCRIBING",
    "TRANSLATING",
    "BURNING",
    "SUCCESS",
    "FAILED",
)

# 资源可用性：与流水线 status 解耦，专门描述"任务已经成功 / 失败，
# 但磁盘上的产物是否还在"。服务重启或运行中下载时若发现资源缺失，
# 会把 resource_status 置为 MISSING，避免继续暴露已失效的下载链接。
RESOURCE_STATUS_AVAILABLE = "AVAILABLE"
RESOURCE_STATUS_MISSING = "MISSING"
RESOURCE_STATUSES = (RESOURCE_STATUS_AVAILABLE, RESOURCE_STATUS_MISSING)


@dataclass
class TaskRecord:
    id: str
    url: str
    source_lang: str
    target_lang: str
    mode: str          # mono | bilingual
    burn: str          # hard | soft
    model: str         # whisper 模型
    engine: str        # 翻译引擎，目前 deepseek
    source_type: str = "url"  # url=在线链接下载 upload=本地上传视频
    need_subtitle: int = 1  # 1=需要字幕(完整流水线) 0=仅下载视频
    status: str = "PENDING"
    progress: int = 0
    current_step: Optional[str] = None
    title: Optional[str] = None
    error: Optional[str] = None
    output_video: Optional[str] = None
    output_subtitle: Optional[str] = None
    created_at: int = 0   # epoch 毫秒
    updated_at: int = 0
    resource_status: str = RESOURCE_STATUS_AVAILABLE  # 产物文件是否在盘

    def to_dict(self) -> dict:
        return asdict(self)


_COLUMNS = list(TaskRecord.__dataclass_fields__.keys())


def _now_ms() -> int:
    return int(time.time() * 1000)


class TaskStore:
    """任务表的增删改查。每次操作开一个短连接，交给 SQLite 处理文件锁。"""

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
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    url TEXT NOT NULL,
                    source_lang TEXT NOT NULL,
                    target_lang TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    burn TEXT NOT NULL,
                    model TEXT NOT NULL,
                    engine TEXT NOT NULL,
                    source_type TEXT NOT NULL DEFAULT 'url',
                    need_subtitle INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL,
                    progress INTEGER NOT NULL,
                    current_step TEXT,
                    title TEXT,
                    error TEXT,
                    output_video TEXT,
                    output_subtitle TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    resource_status TEXT NOT NULL DEFAULT 'AVAILABLE'
                )
                """
            )
            # 轻量迁移：给旧库补上后加的列
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
            if "need_subtitle" not in cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN need_subtitle INTEGER NOT NULL DEFAULT 1")
            if "source_type" not in cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN source_type TEXT NOT NULL DEFAULT 'url'")
            if "resource_status" not in cols:
                conn.execute(
                    "ALTER TABLE tasks ADD COLUMN resource_status TEXT NOT NULL DEFAULT 'AVAILABLE'"
                )

    # ---------- 增 ----------
    def create(
        self,
        *,
        url: str,
        source_lang: str,
        target_lang: str,
        mode: str,
        burn: str,
        model: str,
        engine: str,
        source_type: str = "url",
        need_subtitle: bool = True,
        title: Optional[str] = None,
    ) -> TaskRecord:
        now = _now_ms()
        rec = TaskRecord(
            id="task_" + uuid.uuid4().hex[:8],
            url=url,
            source_lang=source_lang,
            target_lang=target_lang,
            mode=mode,
            burn=burn,
            model=model,
            engine=engine,
            source_type=source_type,
            need_subtitle=int(need_subtitle),
            title=title,
            status="PENDING",
            progress=0,
            created_at=now,
            updated_at=now,
        )
        placeholders = ", ".join(["?"] * len(_COLUMNS))
        values = [getattr(rec, c) for c in _COLUMNS]
        with self._connect() as conn:
            conn.execute(
                f"INSERT INTO tasks ({', '.join(_COLUMNS)}) VALUES ({placeholders})",
                values,
            )
        return rec

    # ---------- 查 ----------
    def get(self, task_id: str) -> Optional[TaskRecord]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return _row_to_record(row) if row else None

    def list(self) -> List[TaskRecord]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM tasks ORDER BY created_at DESC").fetchall()
        return [_row_to_record(r) for r in rows]

    # ---------- 改 ----------
    def update(self, task_id: str, **fields) -> Optional[TaskRecord]:
        allowed = {k: v for k, v in fields.items() if k in _COLUMNS and k != "id"}
        if not allowed:
            return self.get(task_id)
        allowed["updated_at"] = _now_ms()
        assignments = ", ".join(f"{k} = ?" for k in allowed)
        values = list(allowed.values()) + [task_id]
        with self._connect() as conn:
            cur = conn.execute(f"UPDATE tasks SET {assignments} WHERE id = ?", values)
            if cur.rowcount == 0:
                return None
        return self.get(task_id)

    # ---------- 删 ----------
    def delete(self, task_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            return cur.rowcount > 0


def _row_to_record(row: sqlite3.Row) -> TaskRecord:
    return TaskRecord(**{c: row[c] for c in _COLUMNS})
