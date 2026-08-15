from __future__ import annotations

import pytest
from pathlib import Path
from types import SimpleNamespace

from src.service.asset_resolver import AssetResolver, ResourceState, ResourceError
from src.service import orchestrator
from src.service.orchestrator import PipelineParams, run_pipeline


def test_asset_resolver_check_file_state(tmp_path):
    # 1. Non-existent file
    non_existent = tmp_path / "ghost.mp4"
    assert AssetResolver.check_file_state(non_existent) == ResourceState.DELETED

    # 2. Directory instead of file
    directory = tmp_path / "sub_dir"
    directory.mkdir()
    assert AssetResolver.check_file_state(directory) == ResourceState.UNREADABLE

    # 3. Empty file (0 bytes)
    empty_file = tmp_path / "empty.mp4"
    empty_file.touch()
    assert AssetResolver.check_file_state(empty_file) == ResourceState.UNREADABLE

    # 4. Valid file (has content)
    valid_file = tmp_path / "valid.mp4"
    valid_file.write_bytes(b"content")
    assert AssetResolver.check_file_state(valid_file) == ResourceState.AVAILABLE


def test_asset_resolver_resolve_source(tmp_path, monkeypatch):
    # Mock task_dir to return a specific tmp_path for the task_id
    monkeypatch.setattr("src.service.asset_resolver.task_dir", lambda tid: tmp_path)

    # 1. Directory doesn't exist
    fake_dir = tmp_path / "fake_task"
    monkeypatch.setattr("src.service.asset_resolver.task_dir", lambda tid: fake_dir)
    state, path, msg = AssetResolver.resolve_source("task_1")
    assert state == ResourceState.DELETED
    assert path is None
    assert "源视频文件" in msg

    # Restore task_dir
    monkeypatch.setattr("src.service.asset_resolver.task_dir", lambda tid: tmp_path)

    # 2. Source file missing
    state, path, msg = AssetResolver.resolve_source("task_1")
    assert state == ResourceState.DELETED
    assert path is None

    # 3. Source file unreadable (empty)
    src_file = tmp_path / "source.mp4"
    src_file.touch()
    state, path, msg = AssetResolver.resolve_source("task_1")
    assert state == ResourceState.UNREADABLE
    assert path == src_file

    # 4. Source file available
    src_file.write_bytes(b"video data")
    state, path, msg = AssetResolver.resolve_source("task_1")
    assert state == ResourceState.AVAILABLE
    assert path == src_file


def test_asset_resolver_ignores_partial_download(tmp_path, monkeypatch):
    monkeypatch.setattr("src.service.asset_resolver.task_dir", lambda tid: tmp_path)
    partial = tmp_path / "source.mp4.part"
    partial.write_bytes(b"partial video")

    state, path, msg = AssetResolver.resolve_source("task_1")

    assert state == ResourceState.DELETED
    assert path is None


def test_asset_resolver_require_methods(tmp_path, monkeypatch):
    monkeypatch.setattr("src.service.asset_resolver.task_dir", lambda tid: tmp_path)

    # Test require_source raises ResourceError when deleted
    with pytest.raises(ResourceError) as exc_info:
        AssetResolver.require_source("task_1")
    assert exc_info.value.state == ResourceState.DELETED

    # Test require_source returns path when available
    src_file = tmp_path / "source.mp4"
    src_file.write_bytes(b"video data")
    assert AssetResolver.require_source("task_1") == src_file

    # Test require_audio raises ResourceError when unreadable (empty)
    audio_file = tmp_path / "audio.wav"
    audio_file.touch()
    with pytest.raises(ResourceError) as exc_info:
        AssetResolver.require_audio("task_1")
    assert exc_info.value.state == ResourceState.UNREADABLE


def test_pipeline_interruption_at_boundary(tmp_path, monkeypatch):
    # This tests that run_pipeline fails immediately at the correct step when a dependency is missing.
    # Let's say download succeeded, but the downloaded source video was deleted / not created.
    # It should fail at DOWNLOADING or EXTRACTING.

    # We will mock download_video to succeed and return a dummy path.
    # But when extract_audio boundary checks require_source, we let require_source raise ResourceError.

    monkeypatch.setattr(orchestrator, "download_video",
                        lambda *a, **kw: SimpleNamespace(video_path=Path("/d/source.mp4"), title="T"))

    # Raise ResourceError on require_source (representing deleted/missing source)
    monkeypatch.setattr(orchestrator.AssetResolver, "require_source",
                        lambda tid: (_ for _ in ()).throw(ResourceError("源视频已删除", ResourceState.DELETED)))

    params = PipelineParams(
        task_id="t1",
        url="http://x/v",
        source_lang="auto",
        target_lang="zh-CN",
    )
    events = []
    with pytest.raises(ResourceError) as exc_info:
        run_pipeline(params, events.append)

    assert exc_info.value.state == ResourceState.DELETED
    assert events[-1].status == "FAILED"
    assert "源视频已删除" in events[-1].error


def test_pipeline_resumes_from_existing_artifacts(monkeypatch, tmp_path):
    """重启后从已有的最近阶段产物继续，而不是从头执行。"""
    monkeypatch.setattr(orchestrator, "task_dir", lambda tid: tmp_path)
    monkeypatch.setattr("src.service.asset_resolver.task_dir", lambda tid: tmp_path)
    for name, content in {
        "source.mp4": b"video",
        "audio.wav": b"audio",
        "original.srt": b"original",
    }.items():
        (tmp_path / name).write_bytes(content)

    calls = []

    def should_not_run(name):
        def fn(*args, **kwargs):
            calls.append(name)
            raise AssertionError(f"已完成的阶段不应重复执行: {name}")
        return fn

    def fake_translate(*args, **kwargs):
        calls.append("translate_srt")
        (tmp_path / "translated.srt").write_bytes(b"translated")
        return SimpleNamespace(srt_path=tmp_path / "translated.srt")

    def fake_burn(*args, **kwargs):
        calls.append("burn_subtitles")
        (tmp_path / "output.mp4").write_bytes(b"output")
        return SimpleNamespace(output_path=tmp_path / "output.mp4")

    monkeypatch.setattr(orchestrator, "download_video", should_not_run("download_video"))
    monkeypatch.setattr(orchestrator, "extract_audio", should_not_run("extract_audio"))
    monkeypatch.setattr(orchestrator, "transcribe", should_not_run("transcribe"))
    monkeypatch.setattr(orchestrator, "translate_srt", fake_translate)
    monkeypatch.setattr(orchestrator, "burn_subtitles", fake_burn)

    params = PipelineParams(
        task_id="t1", url="https://x/v", source_lang="auto", target_lang="zh-CN",
        title="Recovered video",
    )
    result = run_pipeline(params, lambda event: None)

    assert calls == ["translate_srt", "burn_subtitles"]
    assert result.status == "SUCCESS"
    assert result.title == "Recovered video"
