"""配置层并发与动态环境变量测试。"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from src.config import config


def test_download_format_defaults_to_480p_cap():
    assert (
        config.Settings().download_format
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


def test_dynamic_settings_read_from_env(monkeypatch):
    """验证翻译、目标语言、限制、ffmpeg 等配置项能从环境变量动态读取。"""
    monkeypatch.setenv("SUBTRANS_DEEPSEEK_API_KEY", "sk-dynamic-key")
    monkeypatch.setenv("SUBTRANS_DEEPSEEK_BASE_URL", "https://proxy.example.com")
    monkeypatch.setenv("SUBTRANS_DEEPSEEK_MODEL", "deepseek-coder")
    monkeypatch.setenv("SUBTRANS_TRANSLATE_BATCH", "16")
    monkeypatch.setenv("SUBTRANS_TRANSLATE_TIMEOUT", "120")
    monkeypatch.setenv("SUBTRANS_TARGET_LANGUAGES", "zh-CN,en,th,vi,ar")
    monkeypatch.setenv("SUBTRANS_MAX_UPLOAD_MB", "4096")
    monkeypatch.setenv("SUBTRANS_MAX_VIDEO_MINUTES", "300")
    monkeypatch.setenv("SUBTRANS_AUDIO_SR", "24000")
    monkeypatch.setenv("SUBTRANS_AUDIO_CH", "2")
    monkeypatch.setenv("SUBTRANS_DL_RETRIES", "5")
    monkeypatch.setenv("SUBTRANS_DL_CONTAINER", "mkv")
    monkeypatch.setenv("SUBTRANS_FFMPEG", "/custom/ffmpeg")
    monkeypatch.setenv("SUBTRANS_FFPROBE", "/custom/ffprobe")
    monkeypatch.setenv("SUBTRANS_STREAM_TIMEOUT_SEC", "3600")
    monkeypatch.setenv("SUBTRANS_READINESS_TTL_SEC", "120")
    monkeypatch.setenv("SUBTRANS_PROBE_CACHE_TTL_SEC", "600")
    monkeypatch.setattr(config, "_sync_env_file", lambda: None)

    settings = config.Settings()

    assert settings.deepseek_api_key == "sk-dynamic-key"
    assert settings.deepseek_base_url == "https://proxy.example.com"
    assert settings.deepseek_model == "deepseek-coder"
    assert settings.translate_batch_size == 16
    assert settings.translate_timeout == 120
    assert settings.target_languages == ("zh-CN", "en", "th", "vi", "ar")
    assert settings.max_upload_mb == 4096
    assert settings.max_video_minutes == 300
    assert settings.audio_sample_rate == 24000
    assert settings.audio_channels == 2
    assert settings.download_retries == 5
    assert settings.merge_output_format == "mkv"
    assert settings.ffmpeg_bin == "/custom/ffmpeg"
    assert settings.ffprobe_bin == "/custom/ffprobe"
    assert settings.stream_timeout_sec == 3600
    assert settings.readiness_ttl_sec == 120
    assert settings.probe_cache_ttl_sec == 600


def test_dataclasses_replace_compatibility(monkeypatch):
    """验证 dataclasses.replace 对所有字段仍然成立（如单元测试使用）。"""
    monkeypatch.setattr(config, "_sync_env_file", lambda: None)

    settings = config.Settings()
    replaced = dataclasses.replace(
        settings,
        _deepseek_api_key="sk-override-key",
        _max_upload_mb=1024,
        _target_languages=("zh-CN", "ja"),
        _data_dir=Path("/custom/data"),
    )

    assert replaced.deepseek_api_key == "sk-override-key"
    assert replaced.max_upload_mb == 1024
    assert replaced.target_languages == ("zh-CN", "ja")
    assert replaced.data_dir == Path("/custom/data")

    # 原始 settings 实例不受影响
    assert settings.max_upload_mb == 2048
