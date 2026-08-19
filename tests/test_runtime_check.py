from types import SimpleNamespace

import src.service.runtime_check as runtime_check


def _settings(tmp_path, *, deepseek_api_key=None, api_token=None):
    return SimpleNamespace(
        backend_dir=tmp_path,
        data_dir=tmp_path / "data",
        db_path=tmp_path / "state" / "app.db",
        ffmpeg_bin="ffmpeg",
        ffprobe_bin="ffprobe",
        deepseek_api_key=deepseek_api_key,
        api_token=api_token,
        max_upload_mb=2048,
        max_video_minutes=180,
        pipeline_workers=2,
    )


def test_readiness_reports_fixed_config_location_and_missing_values(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime_check, "settings", _settings(tmp_path))
    monkeypatch.delenv("REPLICATE_API_TOKEN", raising=False)
    monkeypatch.delenv("SUBTRANS_DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(runtime_check.shutil, "which", lambda _: None)
    monkeypatch.setattr(runtime_check.importlib.util, "find_spec", lambda _: None)
    monkeypatch.setattr(runtime_check, "has_subtitles_filter", lambda _: False)

    result = runtime_check.build_readiness()

    assert result["ok"] is False
    assert result["initialized"] is False
    assert result["config_file"] == str(tmp_path / ".env")
    assert "REPLICATE_API_TOKEN" in result["missing"]
    assert "SUBTRANS_DEEPSEEK_API_KEY 或 DEEPSEEK_API_KEY" in result["missing"]
    assert result["capabilities"]["download"] is False
    assert result["agent_action"] == "ask_user_to_configure"
    assert result["restart_required"] is True


def test_readiness_reports_capabilities_without_exposing_keys(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime_check, "settings", _settings(tmp_path))
    monkeypatch.setenv("REPLICATE_API_TOKEN", "replicate-secret")
    monkeypatch.setenv("SUBTRANS_DEEPSEEK_API_KEY", "deepseek-secret")
    monkeypatch.setattr(
        runtime_check.shutil,
        "which",
        lambda command: f"/usr/bin/{command}",
    )
    monkeypatch.setattr(runtime_check.importlib.util, "find_spec", lambda _: object())
    monkeypatch.setattr(runtime_check, "has_subtitles_filter", lambda _: True)
    monkeypatch.setattr(
        runtime_check,
        "query_replicate_balance",
        lambda: {"status": "unsupported", "authenticated": True},
    )

    result = runtime_check.build_readiness()

    assert result["ok"] is True
    assert result["initialized"] is True
    assert result["capabilities"] == {
        "download": True,
        "ffprobe_available": True,
        "full_pipeline": True,
        "hard_burn": True,
        "soft_burn": True,
        "max_concurrent_tasks": 2,
        "max_concurrent_downloads": 2,
    }
    assert result["limits"] == {
        "max_upload_mb": 2048,
        "max_video_minutes": 180,
    }
    assert result["agent_action"] == "continue"
    assert result["restart_required"] is False
    assert "replicate-secret" not in str(result)
    assert "deepseek-secret" not in str(result)


def test_readiness_guides_agent_to_soft_burn_when_libass_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime_check, "settings", _settings(tmp_path))
    monkeypatch.setenv("REPLICATE_API_TOKEN", "replicate-secret")
    monkeypatch.setenv("SUBTRANS_DEEPSEEK_API_KEY", "deepseek-secret")
    monkeypatch.setattr(
        runtime_check.shutil,
        "which",
        lambda command: f"/usr/bin/{command}",
    )
    monkeypatch.setattr(runtime_check.importlib.util, "find_spec", lambda _: object())
    monkeypatch.setattr(runtime_check, "has_subtitles_filter", lambda _: False)
    monkeypatch.setattr(
        runtime_check,
        "query_replicate_balance",
        lambda: {"status": "unsupported", "authenticated": True},
    )

    result = runtime_check.build_readiness()

    assert result["ok"] is False
    assert result["initialized"] is True
    assert result["agent_action"] == "use_soft_burn_or_install_libass"
    assert result["restart_required"] is False
    assert any("FFmpeg subtitles 滤镜" in item for item in result["missing"])


def test_readiness_reports_invalid_replicate_token(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime_check, "settings", _settings(tmp_path))
    monkeypatch.setenv("REPLICATE_API_TOKEN", "invalid-token")
    monkeypatch.setenv("SUBTRANS_DEEPSEEK_API_KEY", "deepseek-secret")
    monkeypatch.setattr(
        runtime_check.shutil,
        "which",
        lambda command: f"/usr/bin/{command}",
    )
    monkeypatch.setattr(runtime_check.importlib.util, "find_spec", lambda _: object())
    monkeypatch.setattr(runtime_check, "has_subtitles_filter", lambda _: True)
    monkeypatch.setattr(
        runtime_check,
        "query_replicate_balance",
        lambda: {
            "status": "error",
            "errorCode": "invalid_api_token",
            "message": "Replicate API Token 无效、已过期或没有访问权限",
        },
    )

    result = runtime_check.build_readiness()

    assert result["ok"] is False
    assert result["initialized"] is False
    assert result["checks"]["replicate_api_token"] == "invalid"
    assert "REPLICATE_API_TOKEN（Token 无效或已过期）" in result["missing"]


def test_readiness_reports_cached_replicate_check_metadata(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime_check, "settings", _settings(tmp_path))
    monkeypatch.setenv("REPLICATE_API_TOKEN", "replicate-secret")
    monkeypatch.setenv("SUBTRANS_DEEPSEEK_API_KEY", "deepseek-secret")
    monkeypatch.setattr(
        runtime_check.shutil,
        "which",
        lambda command: f"/usr/bin/{command}",
    )
    monkeypatch.setattr(runtime_check.importlib.util, "find_spec", lambda _: object())
    monkeypatch.setattr(runtime_check, "has_subtitles_filter", lambda _: True)
    monkeypatch.setattr(
        runtime_check,
        "query_replicate_balance",
        lambda: {
            "status": "unsupported",
            "authenticated": True,
            "checkedAt": 1700000000000,
            "cached": True,
        },
    )

    result = runtime_check.build_readiness()

    assert result["ok"] is True
    assert result["replicate_checked_at"] == 1700000000000
    assert result["replicate_cached"] is True
    assert result["checks"]["replicate_checked_at"] == 1700000000000
    assert result["checks"]["replicate_cached"] is True


def test_readiness_reports_api_token_required(monkeypatch, tmp_path):
    s = _settings(tmp_path, api_token="secret-token")
    monkeypatch.setattr(runtime_check, "settings", s)
    result = runtime_check.build_readiness()
    assert result["checks"]["api_token_required"] is True

    s_empty = _settings(tmp_path, api_token=None)
    monkeypatch.setattr(runtime_check, "settings", s_empty)
    result_empty = runtime_check.build_readiness()
    assert result_empty["checks"]["api_token_required"] is False


def test_readiness_handles_unavailable_replicate_check_gracefully(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime_check, "settings", _settings(tmp_path))
    monkeypatch.setenv("REPLICATE_API_TOKEN", "replicate-secret")
    monkeypatch.setenv("SUBTRANS_DEEPSEEK_API_KEY", "deepseek-secret")
    monkeypatch.setattr(
        runtime_check.shutil,
        "which",
        lambda command: f"/usr/bin/{command}",
    )
    monkeypatch.setattr(runtime_check.importlib.util, "find_spec", lambda _: object())
    monkeypatch.setattr(runtime_check, "has_subtitles_filter", lambda _: True)
    monkeypatch.setattr(
        runtime_check,
        "query_replicate_balance",
        lambda: {
            "status": "unavailable",
            "errorCode": "http_429",
            "checkedAt": 1700000000000,
            "cached": False,
        },
    )

    result = runtime_check.build_readiness()

    assert result["ok"] is True
    assert result["initialized"] is True
    assert result["checks"]["replicate_api_token"] == "unavailable"
    assert result["agent_action"] == "continue"
