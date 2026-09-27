from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
from tqdm.auto import tqdm as FakeTqdm

from src.service.model_manager import LocalModelManager


class TempModelManager(LocalModelManager):
    def __init__(self, root: Path) -> None:
        self._test_root = root
        super().__init__()

    @property
    def root(self) -> Path:
        self._test_root.mkdir(parents=True, exist_ok=True)
        return self._test_root


def test_download_progress_reports_bytes_from_huggingface(monkeypatch, tmp_path):
    manager = TempModelManager(tmp_path / "models")
    manager._states["small"] = {
        "status": "DOWNLOADING",
        "phase": "checking",
        "progress": 0,
        "downloadedBytes": 0,
        "totalBytes": 0,
        "error": None,
    }
    snapshots = []

    class FakeHfApi:
        def list_repo_tree(self, **kwargs):
            return [SimpleNamespace(path="weights.bin", size=100)]

    def fake_snapshot_download(*, repo_id, revision, local_dir, tqdm_class):
        progress = tqdm_class(
            total=100,
            initial=0,
            unit="B",
            name="huggingface_hub.snapshot_download",
        )
        progress.update(40)
        snapshots.append(manager.list_models()[2])
        progress.update(60)
        (Path(local_dir) / "weights.bin").write_bytes(b"x" * 100)

    huggingface_hub = types.ModuleType("huggingface_hub")
    huggingface_hub.HfApi = FakeHfApi
    huggingface_hub.snapshot_download = fake_snapshot_download
    monkeypatch.setitem(sys.modules, "huggingface_hub", huggingface_hub)

    manager._download("small")

    assert snapshots[0]["status"] == "DOWNLOADING"
    assert snapshots[0]["progress"] == 40
    assert snapshots[0]["downloadedBytes"] == 40
    assert snapshots[0]["totalBytes"] == 100
    completed = manager.list_models()[2]
    assert completed["status"] == "READY"
    assert completed["progress"] == 100
    assert completed["downloadedBytes"] == completed["totalBytes"]


def test_delete_ready_model_and_refuse_while_downloading(tmp_path):
    manager = TempModelManager(tmp_path / "models")
    model_dir = manager.root / "small"
    model_dir.mkdir()
    marker = model_dir / ".subtrans-ready"
    marker.touch()
    (model_dir / "weights.bin").write_bytes(b"weights")

    manager._states["small"] = {"status": "DOWNLOADING"}
    with pytest.raises(RuntimeError, match="正在下载"):
        manager.delete("small")
    assert marker.exists()

    manager._states["small"] = {"status": "READY"}
    result = manager.delete("small")
    assert result["status"] == "NOT_INSTALLED"
    assert not model_dir.exists()
