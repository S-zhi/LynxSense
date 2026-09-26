"""CPU 人声分离封装的单元测试。

Demucs 和 ffmpeg 都是外部依赖，因此测试只验证命令拼接、错误包装和产物
校验，不下载模型也不启动真实推理。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.core import vocal_separator as vs


def _settings(**overrides):
    values = {
        "vocal_separation_backend": "demucs",
        "vocal_separation_command": "python -m demucs.separate",
        "vocal_separation_timeout": 900,
        "ffmpeg_bin": "ffmpeg",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_separate_vocals_builds_cpu_demucs_command_and_normalizes_output(tmp_path, monkeypatch):
    source = tmp_path / "audio.wav"
    source.write_bytes(b"audio")
    task_dir = tmp_path / "task"
    demucs_output = task_dir / "vocal-separation" / "htdemucs" / "audio" / "vocals.wav"
    calls = []

    monkeypatch.setattr(vs, "settings", _settings())
    monkeypatch.setattr(vs, "ensure_task_dir", lambda task_id: task_dir)

    def fake_external(cmd, *, timeout, task_id, cancel_check=None):
        calls.append((cmd, {"timeout": timeout, "task_id": task_id}))
        if len(calls) == 1:
            demucs_output.parent.mkdir(parents=True, exist_ok=True)
            demucs_output.write_bytes(b"vocals")
        else:
            (task_dir / ".vocal.wav.tmp").write_bytes(b"normalized")
        return 0, "", ""

    monkeypatch.setattr(vs, "_run_external", fake_external)

    result = vs.separate_vocals(source, "task-1")

    assert result == task_dir / "vocal.wav"
    demucs_cmd, demucs_kwargs = calls[0]
    assert demucs_cmd[:3] == ["python", "-m", "demucs.separate"]
    assert "--two-stems=vocals" in demucs_cmd
    assert demucs_cmd[demucs_cmd.index("--device") + 1] == "cpu"
    assert demucs_cmd[demucs_cmd.index("--jobs") + 1] == "1"
    assert demucs_cmd[demucs_cmd.index("--shifts") + 1] == "0"
    assert demucs_kwargs["timeout"] == 900
    normalize_cmd, _ = calls[1]
    assert normalize_cmd[0] == "ffmpeg"
    assert normalize_cmd[-1] == str(task_dir / ".vocal.wav.tmp")
    assert "-ac" in normalize_cmd and normalize_cmd[normalize_cmd.index("-ac") + 1] == "1"
    assert "-ar" in normalize_cmd and normalize_cmd[normalize_cmd.index("-ar") + 1] == "16000"


def test_separate_vocals_rejects_missing_source(tmp_path, monkeypatch):
    monkeypatch.setattr(vs, "settings", _settings())

    with pytest.raises(vs.VocalSeparationError, match="输入音频不存在"):
        vs.separate_vocals(tmp_path / "missing.wav", "task-1")


def test_separate_vocals_wraps_demucs_timeout(tmp_path, monkeypatch):
    source = tmp_path / "audio.wav"
    source.write_bytes(b"audio")
    monkeypatch.setattr(vs, "settings", _settings())
    monkeypatch.setattr(vs, "ensure_task_dir", lambda task_id: tmp_path / "task")

    def timeout(*args, **kwargs):
        raise vs.VocalSeparationError("人声分离超时")

    monkeypatch.setattr(vs, "_run_external", timeout)

    with pytest.raises(vs.VocalSeparationError, match="人声分离超时"):
        vs.separate_vocals(source, "task-1")


def test_separate_vocals_rejects_unsupported_backend(tmp_path, monkeypatch):
    source = tmp_path / "audio.wav"
    source.write_bytes(b"audio")
    monkeypatch.setattr(vs, "settings", _settings(vocal_separation_backend="spleeter"))

    with pytest.raises(vs.VocalSeparationError, match="不支持的人声分离 backend"):
        vs.separate_vocals(source, "task-1")


def test_separate_vocals_requires_vocals_artifact(tmp_path, monkeypatch):
    source = tmp_path / "audio.wav"
    source.write_bytes(b"audio")
    task_dir = tmp_path / "task"
    monkeypatch.setattr(vs, "settings", _settings())
    monkeypatch.setattr(vs, "ensure_task_dir", lambda task_id: task_dir)
    monkeypatch.setattr(vs, "_run_external", lambda *args, **kwargs: (0, "", ""))

    with pytest.raises(vs.VocalSeparationError, match="未生成 vocals.wav"):
        vs.separate_vocals(source, "task-1")


def test_cached_vocals_rejects_cache_without_metadata(tmp_path, monkeypatch):
    source = tmp_path / "audio.wav"
    source.write_bytes(b"audio")
    (tmp_path / "vocal.wav").write_bytes(b"vocals")
    monkeypatch.setattr(vs, "ensure_task_dir", lambda task_id: tmp_path)

    assert vs.cached_vocals_are_current(source, "task-1") is False


def test_cached_vocals_rejects_stale_metadata(tmp_path, monkeypatch):
    source = tmp_path / "audio.wav"
    source.write_bytes(b"audio")
    (tmp_path / "vocal.wav").write_bytes(b"vocals")
    (tmp_path / "vocal.meta.json").write_text(
        json.dumps({
            "source": str((tmp_path / "other.wav").resolve()),
            "source_size": source.stat().st_size,
            "source_mtime_ns": source.stat().st_mtime_ns,
            "backend": "demucs",
            "command": "python -m demucs.separate",
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(vs, "ensure_task_dir", lambda task_id: tmp_path)
    monkeypatch.setattr(vs, "settings", _settings())

    assert vs.cached_vocals_are_current(source, "task-1") is False


def test_cached_vocals_accepts_matching_metadata(tmp_path, monkeypatch):
    source = tmp_path / "audio.wav"
    source.write_bytes(b"audio")
    (tmp_path / "vocal.wav").write_bytes(b"vocals")
    monkeypatch.setattr(vs, "settings", _settings())
    (tmp_path / "vocal.meta.json").write_text(
        json.dumps({
            "source": str(source.resolve()),
            "source_size": source.stat().st_size,
            "source_mtime_ns": source.stat().st_mtime_ns,
            "backend": "demucs",
            "command": "python -m demucs.separate",
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(vs, "ensure_task_dir", lambda task_id: tmp_path)

    assert vs.cached_vocals_are_current(source, "task-1") is True
