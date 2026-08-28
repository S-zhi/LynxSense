import subprocess

from src.core import ffmpeg_utils


def test_has_subtitles_filter_does_not_share_results_between_binaries(monkeypatch):
    ffmpeg_utils._subtitles_filter_cache.clear()
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd[0])
        output = " subtitles " if cmd[0] == "with-libass" else ""
        return subprocess.CompletedProcess(cmd, 0, output, "")

    monkeypatch.setattr(ffmpeg_utils.shutil, "which", lambda binary: f"/bin/{binary}")
    monkeypatch.setattr(ffmpeg_utils.Path, "is_file", lambda self: False)
    monkeypatch.setattr(ffmpeg_utils.Path, "stat", lambda self: type("Stat", (), {"st_dev": 1, "st_ino": hash(str(self)), "st_size": 1, "st_mtime_ns": 1})())
    monkeypatch.setattr(ffmpeg_utils.subprocess, "run", fake_run)

    assert ffmpeg_utils.has_subtitles_filter("with-libass") is True
    assert ffmpeg_utils.has_subtitles_filter("without-libass") is False
    assert calls == ["with-libass", "without-libass"]


def test_has_subtitles_filter_rechecks_when_binary_metadata_changes(monkeypatch):
    ffmpeg_utils._subtitles_filter_cache.clear()
    mtime = [1]
    calls = []

    class Stat:
        st_dev = 1
        st_ino = 1
        st_size = 1

        @property
        def st_mtime_ns(self):
            return mtime[0]

    monkeypatch.setattr(ffmpeg_utils.shutil, "which", lambda _: "/bin/ffmpeg")
    monkeypatch.setattr(ffmpeg_utils.Path, "is_file", lambda self: False)
    monkeypatch.setattr(ffmpeg_utils.Path, "stat", lambda self: Stat())
    monkeypatch.setattr(ffmpeg_utils.subprocess, "run", lambda cmd, **kwargs: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, " subtitles ", ""))

    assert ffmpeg_utils.has_subtitles_filter("ffmpeg") is True
    assert ffmpeg_utils.has_subtitles_filter("ffmpeg") is True
    mtime[0] = 2
    assert ffmpeg_utils.has_subtitles_filter("ffmpeg") is True
    assert len(calls) == 2


def test_has_subtitles_filter_caches_missing_binary_result(monkeypatch):
    ffmpeg_utils._subtitles_filter_cache.clear()
    calls = []
    monkeypatch.setattr(ffmpeg_utils.shutil, "which", lambda _: None)
    monkeypatch.setattr(ffmpeg_utils.subprocess, "run", lambda *args, **kwargs: calls.append(args) or (_ for _ in ()).throw(FileNotFoundError))

    assert ffmpeg_utils.has_subtitles_filter("missing-ffmpeg") is False
    assert ffmpeg_utils.has_subtitles_filter("missing-ffmpeg") is False
    assert len(calls) == 1
