"""配置层并发参数测试。"""

from __future__ import annotations

from src.config import config


def test_download_format_defaults_to_480p_cap():
    assert (
        config.Settings.__dataclass_fields__["download_format"].default
        == "bv*[height<=480]+ba/b[height<=480]"
    )


def test_concurrency_defaults(monkeypatch):
    monkeypatch.delenv("SUBTRANS_WORKERS", raising=False)
    monkeypatch.delenv("SUBTRANS_DOWNLOAD_WORKERS", raising=False)
    monkeypatch.delenv("SUBTRANS_DL_CONCURRENT_FRAGMENTS", raising=False)
    monkeypatch.setattr(config, "_sync_env_file", lambda: None)

    settings = config.Settings()

    assert settings.pipeline_workers == 8
    assert settings.download_workers == 2
    assert settings.download_concurrent_fragments == 4


def test_concurrency_values_are_read_dynamically(monkeypatch):
    monkeypatch.setenv("SUBTRANS_WORKERS", "5")
    monkeypatch.setenv("SUBTRANS_DOWNLOAD_WORKERS", "3")
    monkeypatch.setenv("SUBTRANS_DL_CONCURRENT_FRAGMENTS", "6")
    monkeypatch.setattr(config, "_sync_env_file", lambda: None)

    settings = config.Settings()

    assert settings.pipeline_workers == 5
    assert settings.download_workers == 3
    assert settings.download_concurrent_fragments == 6
