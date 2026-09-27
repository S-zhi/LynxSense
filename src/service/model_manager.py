"""管理 faster-whisper 本地模型的目录、状态和异步下载。"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from src.config import settings

MODEL_CATALOG: tuple[dict[str, Any], ...] = (
    {"name": "tiny", "label": "Whisper Tiny", "size": "~75 MB"},
    {"name": "base", "label": "Whisper Base", "size": "~145 MB"},
    {"name": "small", "label": "Whisper Small", "size": "~465 MB"},
    {"name": "medium", "label": "Whisper Medium", "size": "~1.5 GB"},
    {"name": "large-v3", "label": "Whisper Large V3", "size": "~3 GB"},
    {"name": "large-v3-turbo", "label": "Whisper Large V3 Turbo", "size": "~1.6 GB"},
)
MODEL_NAMES = frozenset(item["name"] for item in MODEL_CATALOG)


class LocalModelManager:
    """Small process-local coordinator; downloads are serialized by model name."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._states: dict[str, dict[str, Any]] = {}
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="whisper-model")

    @property
    def root(self) -> Path:
        configured = getattr(settings, "local_whisper_download_root", None)
        root = Path(configured).expanduser() if configured else Path(settings.data_dir) / "models"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _entry(self, name: str) -> dict[str, Any]:
        for item in MODEL_CATALOG:
            if item["name"] == name:
                return dict(item)
        raise ValueError(f"不支持的本地 Whisper 模型: {name}")

    def _disk_ready(self, name: str) -> bool:
        # faster-whisper accepts a model directory directly. A marker is written
        # after a successful constructor call, avoiding fragile cache layout checks.
        return (self.root / name / ".subtrans-ready").exists()

    def list_models(self) -> list[dict[str, Any]]:
        result = []
        with self._lock:
            for item in MODEL_CATALOG:
                name = item["name"]
                state = dict(self._states.get(name, {}))
                status = "READY" if self._disk_ready(name) else state.get("status", "NOT_INSTALLED")
                result.append({**item, "status": status, "phase": state.get("phase", "ready" if status == "READY" else "idle"), "progress": state.get("progress", 100 if status == "READY" else 0), "downloadedBytes": state.get("downloadedBytes", 0), "totalBytes": state.get("totalBytes", 0), "error": state.get("error")})
        return result

    def is_ready(self, name: str) -> bool:
        return name in MODEL_NAMES and self._disk_ready(name)

    def resolve_path(self, name: str) -> str:
        if not self.is_ready(name):
            raise RuntimeError(f"本地 Whisper 模型尚未就绪: {name}")
        return str(self.root / name)

    def download(self, name: str) -> dict[str, Any]:
        self._entry(name)
        with self._lock:
            if self._disk_ready(name):
                return next(item for item in self.list_models() if item["name"] == name)
            current = self._states.get(name, {})
            if current.get("status") == "DOWNLOADING":
                return next(item for item in self.list_models() if item["name"] == name)
            self._states[name] = {"status": "DOWNLOADING", "phase": "downloading", "progress": 1, "downloadedBytes": 0, "totalBytes": 0, "error": None}
            self._executor.submit(self._download, name)
        return next(item for item in self.list_models() if item["name"] == name)

    def _download(self, name: str) -> None:
        try:
            destination = self.root / name
            destination.mkdir(parents=True, exist_ok=True)
            with self._lock:
                self._states[name]["progress"] = 10
            from huggingface_hub import snapshot_download
            repo = f"Systran/faster-whisper-{name}"
            snapshot_download(repo_id=repo, local_dir=str(destination))
            (destination / ".subtrans-ready").touch()
            with self._lock:
                total = sum(p.stat().st_size for p in destination.rglob("*") if p.is_file())
                self._states[name] = {"status": "READY", "phase": "ready", "progress": 100, "downloadedBytes": total, "totalBytes": total, "error": None}
        except Exception as exc:
            with self._lock:
                self._states[name] = {"status": "ERROR", "phase": "error", "progress": 0, "downloadedBytes": 0, "totalBytes": 0, "error": str(exc)}

    def preload_tiny(self) -> None:
        if not self.is_ready("tiny"):
            self.download("tiny")


model_manager = LocalModelManager()
