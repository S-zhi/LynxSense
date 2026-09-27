"""管理 faster-whisper 本地模型的目录、状态和异步下载。"""

from __future__ import annotations

import shutil
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


class _SilentProgressStream:
    """Discard tqdm output while keeping its byte counter active."""

    def write(self, value: str) -> int:
        return len(value)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False


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

    def _record_download_progress(self, name: str, downloaded_bytes: int) -> None:
        with self._lock:
            state = self._states.get(name)
            if not state or state.get("status") != "DOWNLOADING":
                return
            downloaded = max(0, int(downloaded_bytes))
            total = int(state.get("totalBytes", 0) or 0)
            progress = min(99, int(downloaded * 100 / total)) if total > 0 else 0
            state.update(
                phase="downloading",
                progress=progress,
                downloadedBytes=downloaded,
            )

    def delete(self, name: str) -> dict[str, Any]:
        self._entry(name)
        with self._lock:
            state = self._states.get(name, {})
            if state.get("status") == "DOWNLOADING":
                raise RuntimeError("模型正在下载，暂时无法删除")
            destination = self.root / name
            if not self._disk_ready(name):
                raise FileNotFoundError(f"本地模型尚未下载完成: {name}")
            shutil.rmtree(destination)
            self._states.pop(name, None)
        return next(item for item in self.list_models() if item["name"] == name)

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
            self._states[name] = {"status": "DOWNLOADING", "phase": "checking", "progress": 0, "downloadedBytes": 0, "totalBytes": 0, "error": None}
            self._executor.submit(self._download, name)
        return next(item for item in self.list_models() if item["name"] == name)

    def _download(self, name: str) -> None:
        try:
            destination = self.root / name
            destination.mkdir(parents=True, exist_ok=True)
            with self._lock:
                self._states[name]["phase"] = "checking"
            repo = f"Systran/faster-whisper-{name}"
            from huggingface_hub import HfApi, snapshot_download
            from tqdm.auto import tqdm as BaseTqdm

            total = 0
            downloaded = 0
            try:
                files = list(HfApi().list_repo_tree(repo_id=repo, recursive=True, revision="main"))
                for item in files:
                    size = getattr(item, "size", None)
                    relative = getattr(item, "path", "")
                    if size is None or not relative:
                        continue
                    total += int(size)
                    local_file = destination / relative
                    try:
                        if local_file.is_file() and local_file.stat().st_size == int(size):
                            downloaded += int(size)
                    except OSError:
                        continue
            except Exception:
                # A cached Hub snapshot may still be usable while the metadata
                # endpoint is offline. Keep byte counts, but leave the bar
                # indeterminate instead of inventing a percentage.
                total = 0
                downloaded = 0

            with self._lock:
                state = self._states[name]
                state.update(phase="downloading", downloadedBytes=downloaded, totalBytes=total)
                if total > 0:
                    state["progress"] = min(99, int(downloaded * 100 / total))

            manager = self

            class DownloadProgress(BaseTqdm):
                def __init__(self, *args, **kwargs):
                    bar_name = kwargs.pop("name", None)
                    self._track_model_bytes = bar_name == "huggingface_hub.snapshot_download"
                    kwargs["disable"] = not self._track_model_bytes
                    if self._track_model_bytes:
                        kwargs["file"] = _SilentProgressStream()
                    super().__init__(*args, **kwargs)

                def update(self, n=1):
                    result = super().update(n)
                    if self._track_model_bytes:
                        manager._record_download_progress(name, downloaded + int(self.n))
                    return result

            snapshot_download(repo_id=repo, revision="main", local_dir=str(destination), tqdm_class=DownloadProgress)
            (destination / ".subtrans-ready").touch()
            with self._lock:
                total = sum(p.stat().st_size for p in destination.rglob("*") if p.is_file())
                self._states[name] = {"status": "READY", "phase": "ready", "progress": 100, "downloadedBytes": total, "totalBytes": total, "error": None}
        except Exception as exc:
            with self._lock:
                previous = self._states.get(name, {})
                self._states[name] = {"status": "ERROR", "phase": "error", "progress": previous.get("progress", 0), "downloadedBytes": previous.get("downloadedBytes", 0), "totalBytes": previous.get("totalBytes", 0), "error": str(exc)}

    def preload_tiny(self) -> None:
        if not self.is_ready("tiny"):
            self.download("tiny")


model_manager = LocalModelManager()
