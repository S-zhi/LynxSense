"""① 下载视频 的单元测试。

全程 mock yt_dlp，不依赖网络：验证产物路径解析、结果构造、错误包装、
进度回调适配等纯逻辑。
"""

from __future__ import annotations

import pytest
from yt_dlp.utils import DownloadError as YtDlpDownloadError

from src.core import downloader
from src.core.downloader import (
    DownloadError,
    DownloadProgress,
    download_video,
    _make_progress_adapter,
    _resolve_output_path,
)


def make_fake_ydl(on_extract):
    """构造一个可作为上下文管理器使用的假 YoutubeDL，extract_info 行为由回调决定。"""

    class _FakeYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=False):
            return on_extract(url, download, self.opts)

    return _FakeYDL


@pytest.fixture
def task_path(tmp_path, monkeypatch):
    """隔离任务目录：patch downloader.ensure_task_dir 指向临时目录。"""
    d = tmp_path / "task1"
    d.mkdir()
    monkeypatch.setattr(downloader, "ensure_task_dir", lambda task_id: d)
    return d


# ---------- download_video 主流程 ----------

def test_download_success(task_path, monkeypatch):
    src = task_path / "source.mp4"

    def on_extract(url, download, opts):
        assert download is True
        src.write_bytes(b"fake video data")
        return {
            "title": "My Video",
            "duration": 12.5,
            "width": 640,
            "height": 360,
            "requested_downloads": [{"filepath": str(src)}],
        }

    monkeypatch.setattr(downloader, "YoutubeDL", make_fake_ydl(on_extract))

    res = download_video("http://example.com/v", "task1")

    assert res.video_path == src
    assert res.title == "My Video"
    assert res.duration == 12.5
    assert res.ext == "mp4"
    assert res.width == 640 and res.height == 360
    assert res.filesize == len(b"fake video data")
    assert res.source_url == "http://example.com/v"


def test_download_title_falls_back_to_stem(task_path, monkeypatch):
    src = task_path / "source.mp4"

    def on_extract(url, download, opts):
        src.write_bytes(b"x")
        return {"requested_downloads": [{"filepath": str(src)}]}  # 无 title

    monkeypatch.setattr(downloader, "YoutubeDL", make_fake_ydl(on_extract))

    res = download_video("http://x", "task1")
    assert res.title == "source"


def test_download_wraps_ytdlp_error(task_path, monkeypatch):
    def on_extract(url, download, opts):
        raise YtDlpDownloadError("boom")

    monkeypatch.setattr(downloader, "YoutubeDL", make_fake_ydl(on_extract))

    with pytest.raises(DownloadError, match="下载失败"):
        download_video("http://x", "task1")


def test_download_wraps_generic_error(task_path, monkeypatch):
    def on_extract(url, download, opts):
        raise ValueError("unexpected")

    monkeypatch.setattr(downloader, "YoutubeDL", make_fake_ydl(on_extract))

    with pytest.raises(DownloadError, match="出错"):
        download_video("http://x", "task1")


def test_download_missing_output_file(task_path, monkeypatch):
    def on_extract(url, download, opts):
        # 返回一个不存在的路径，模拟"下载完成但产物缺失"
        return {"requested_downloads": [{"filepath": str(task_path / "nope.mp4")}]}

    monkeypatch.setattr(downloader, "YoutubeDL", make_fake_ydl(on_extract))

    with pytest.raises(DownloadError, match="未找到产物"):
        download_video("http://x", "task1")


def test_download_passes_progress_hook(task_path, monkeypatch):
    """传入 on_progress 时，opts 里应注册 progress_hooks。"""
    src = task_path / "source.mp4"
    captured = {}

    def on_extract(url, download, opts):
        captured["opts"] = opts
        src.write_bytes(b"x")
        return {"requested_downloads": [{"filepath": str(src)}]}

    monkeypatch.setattr(downloader, "YoutubeDL", make_fake_ydl(on_extract))

    download_video("http://x", "task1", on_progress=lambda p: None)
    assert "progress_hooks" in captured["opts"]
    assert len(captured["opts"]["progress_hooks"]) == 1


# ---------- _resolve_output_path 回退逻辑 ----------

def test_resolve_uses_requested_downloads(tmp_path):
    target = tmp_path / "a.mp4"
    info = {"requested_downloads": [{"filepath": str(target)}]}
    assert _resolve_output_path(info, tmp_path) == target


def test_resolve_uses_top_level_filepath(tmp_path):
    target = tmp_path / "b.mp4"
    info = {"filepath": str(target)}
    assert _resolve_output_path(info, tmp_path) == target


def test_resolve_glob_prefers_merge_container(tmp_path):
    (tmp_path / "source.mp4").write_bytes(b"x")
    assert _resolve_output_path({}, tmp_path) == tmp_path / "source.mp4"


def test_resolve_glob_other_ext(tmp_path):
    (tmp_path / "source.mkv").write_bytes(b"x")
    assert _resolve_output_path({}, tmp_path) == tmp_path / "source.mkv"


def test_resolve_returns_none_when_nothing(tmp_path):
    assert _resolve_output_path({}, tmp_path) is None


# ---------- _make_progress_adapter 进度映射 ----------

def test_progress_downloading_with_total():
    seen = []
    hook = _make_progress_adapter(seen.append)
    hook({"status": "downloading", "downloaded_bytes": 50, "total_bytes": 200})
    assert len(seen) == 1
    p = seen[0]
    assert isinstance(p, DownloadProgress)
    assert p.percent == 25.0
    assert p.total_bytes == 200
    assert p.downloaded_bytes == 50


def test_progress_uses_estimate_when_no_total():
    seen = []
    hook = _make_progress_adapter(seen.append)
    hook({"status": "downloading", "downloaded_bytes": 50, "total_bytes_estimate": 100})
    assert seen[0].percent == 50.0


def test_progress_finished_is_100():
    seen = []
    hook = _make_progress_adapter(seen.append)
    hook({"status": "finished", "downloaded_bytes": 200, "total_bytes": 200})
    assert seen[0].percent == 100.0
    assert seen[0].status == "finished"


def test_progress_ignores_unknown_status():
    seen = []
    hook = _make_progress_adapter(seen.append)
    hook({"status": "error"})
    assert seen == []


def test_progress_swallows_callback_exception():
    def bad(_p):
        raise RuntimeError("callback blew up")

    hook = _make_progress_adapter(bad)
    # 不应抛出
    hook({"status": "finished"})


# ---------- probe_video / 探针辅助函数 ----------

from src.core.downloader import (
    ProbeResult,
    _clip_error,
    _count_formats,
    _is_probe_url,
    _probe_failure_reason,
    probe_video,
)


# ---------- _is_probe_url URL 校验 ----------

def test_is_probe_url_accepts_http():
    assert _is_probe_url("http://example.com/v") is True


def test_is_probe_url_accepts_https():
    assert _is_probe_url("https://www.youtube.com/watch?v=abc") is True


def test_is_probe_url_rejects_empty_string():
    assert _is_probe_url("") is False


def test_is_probe_url_rejects_ftp():
    assert _is_probe_url("ftp://example.com/v") is False


def test_is_probe_url_rejects_file_scheme():
    assert _is_probe_url("file:///etc/passwd") is False


def test_is_probe_url_rejects_scheme_without_netloc():
    assert _is_probe_url("http://") is False


def test_is_probe_url_rejects_garbage_string():
    assert _is_probe_url("not a url") is False


# ---------- _count_formats 格式数统计 ----------

def test_count_formats_uses_formats_list():
    assert _count_formats({"formats": [{}, {}, {}, {}]}) == 4


def test_count_formats_uses_requested_formats_when_no_formats():
    assert _count_formats({"requested_formats": [{}, {}]}) == 2


def test_count_formats_falls_back_to_url_only():
    # 没有 formats/requested_formats 时，存在 url 也算 1
    assert _count_formats({"url": "https://cdn/stream"}) == 1


def test_count_formats_returns_zero_when_nothing():
    assert _count_formats({}) == 0
    assert _count_formats({"formats": "not a list", "url": None}) == 0


def test_count_formats_empty_lists():
    """formats 与 requested_formats 都是空列表、且无 url 时视为 0。"""
    assert _count_formats({"formats": [], "requested_formats": []}) == 0


# ---------- _probe_failure_reason 错误归类 ----------

def test_probe_failure_reason_unsupported_url():
    assert _probe_failure_reason("Unsupported URL: https://x/") == "yt-dlp 暂不支持这个网站或链接"


def test_probe_failure_reason_private_login_cookie():
    assert _probe_failure_reason("Private video. Sign in if you've access") == "该链接可能需要登录态或 cookies"


def test_probe_failure_reason_login_keyword():
    assert _probe_failure_reason("Login required") == "该链接可能需要登录态或 cookies"


def test_probe_failure_reason_no_video_formats():
    msg = "No video formats found"
    assert _probe_failure_reason(msg) == "未找到匹配当前配置的可下载格式"


def test_probe_failure_reason_requested_format_not_available():
    msg = "The requested format is not available"
    assert _probe_failure_reason(msg) == "未找到匹配当前配置的可下载格式"


def test_probe_failure_reason_404():
    assert _probe_failure_reason("HTTP Error 404: Not Found") == "视频不可用或链接已失效"


def test_probe_failure_reason_not_available_keyword():
    assert _probe_failure_reason("Video is not available in your region") == "视频不可用或链接已失效"


def test_probe_failure_reason_default_fallback():
    assert _probe_failure_reason("Some other random failure") == "yt-dlp 无法解析这个链接"


# ---------- _clip_error 截断 ----------

def test_clip_error_short_passes_through():
    assert _clip_error("boom") == "boom"


def test_clip_error_truncates_to_500():
    msg = "x" * 800
    out = _clip_error(msg)
    assert len(out) == 500
    assert out == "x" * 500


def test_clip_error_custom_limit():
    out = _clip_error("abcdefghij", limit=3)
    assert out == "abc"


# ---------- probe_video 主流程 ----------

def test_probe_rejects_invalid_url_without_calling_ydl(monkeypatch):
    """非法 URL 应在调用 yt-dlp 之前直接返回 ok=False。"""
    called = []

    class _ShouldNotUse:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def extract_info(self, *a, **k):
            called.append((a, k))
            return {}

    monkeypatch.setattr(downloader, "YoutubeDL", _ShouldNotUse)

    res = probe_video("not a url")
    assert res.ok is False
    assert res.reason == "请输入有效的视频链接"
    assert called == []


def test_probe_success_with_formats(monkeypatch):
    """解析出 formats 列表 -> ok=True 且 formats_count 与元信息透传。"""

    def on_extract(url, download, opts):
        # 探针模式必须传 download=False
        assert download is False
        # 探测场景应不开 check_formats（见 downloader.py 注释）
        assert "check_formats" not in opts
        return {
            "title": "Probe Title",
            "extractor_key": "Youtube",
            "duration": 123.4,
            "webpage_url": "https://example.com/v",
            "formats": [{}, {}, {}],
        }

    monkeypatch.setattr(downloader, "YoutubeDL", make_fake_ydl(on_extract))

    res = probe_video("https://example.com/v")
    assert res.ok is True
    assert res.title == "Probe Title"
    assert res.extractor == "Youtube"
    assert res.duration == 123.4
    assert res.formats_count == 3
    assert res.webpage_url == "https://example.com/v"
    assert res.reason is None and res.detail is None


def test_probe_success_falls_back_to_extractor(monkeypatch):
    """无 extractor_key 时回退到 extractor。"""

    def on_extract(url, download, opts):
        return {"title": "T", "extractor": "Generic", "url": "https://x"}

    monkeypatch.setattr(downloader, "YoutubeDL", make_fake_ydl(on_extract))
    res = probe_video("https://x/v")
    assert res.ok is True
    assert res.extractor == "Generic"
    assert res.formats_count == 1


def test_probe_no_formats_returns_failure_with_metadata(monkeypatch):
    """formats_count=0 且无 url -> ok=False，并把元信息回填。"""

    def on_extract(url, download, opts):
        return {"title": "T", "extractor_key": "X", "duration": 10, "webpage_url": "https://x/v"}

    monkeypatch.setattr(downloader, "YoutubeDL", make_fake_ydl(on_extract))
    res = probe_video("https://x/v")
    assert res.ok is False
    assert res.reason == "未找到可下载的视频格式"
    # 元信息回填：title / extractor / duration / webpage_url
    assert res.title == "T" and res.extractor == "X" and res.duration == 10
    assert res.webpage_url == "https://x/v"


def test_probe_no_formats_falls_back_webpage_url_to_input(monkeypatch):
    """解析结果无 webpage_url 时回填到入参 URL。"""

    def on_extract(url, download, opts):
        return {"title": "T"}  # 无 formats / 无 url / 无 webpage_url

    monkeypatch.setattr(downloader, "YoutubeDL", make_fake_ydl(on_extract))
    res = probe_video("https://x/v")
    assert res.ok is False
    assert res.webpage_url == "https://x/v"


def test_probe_wraps_ytdlp_download_error(monkeypatch):
    """yt-dlp DownloadError 归类失败原因并把原文裁剪后放到 detail。"""

    def on_extract(url, download, opts):
        raise YtDlpDownloadError("Unsupported URL: https://x/")

    monkeypatch.setattr(downloader, "YoutubeDL", make_fake_ydl(on_extract))
    res = probe_video("https://x/")
    assert res.ok is False
    assert res.reason == "yt-dlp 暂不支持这个网站或链接"
    assert res.detail == "Unsupported URL: https://x/"


def test_probe_wraps_generic_exception(monkeypatch):
    """非 yt-dlp 异常走通用兜底：reason=链接探测失败，detail=裁剪后原文。"""

    def on_extract(url, download, opts):
        raise ValueError("network gone")

    monkeypatch.setattr(downloader, "YoutubeDL", make_fake_ydl(on_extract))
    res = probe_video("https://x/")
    assert res.ok is False
    assert res.reason == "链接探测失败"
    assert res.detail == "network gone"


def test_probe_truncates_long_error_detail(monkeypatch):
    """底层长错误应被裁剪到 500 字符以内。"""
    long_msg = "y" * 1000

    def on_extract(url, download, opts):
        raise YtDlpDownloadError(long_msg)

    monkeypatch.setattr(downloader, "YoutubeDL", make_fake_ydl(on_extract))
    res = probe_video("https://x/")
    assert res.ok is False
    assert len(res.detail) == 500


def test_probe_passes_cookies_file(monkeypatch, tmp_path):
    """显式 cookies_file 应写入 cookiefile；settings.cookies_file 优先取本次参数。"""
    captured = {}
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape")

    def on_extract(url, download, opts):
        captured["opts"] = opts
        return {"title": "T", "url": "https://x"}

    monkeypatch.setattr(downloader, "YoutubeDL", make_fake_ydl(on_extract))
    probe_video("https://x/v", cookies_file=cookies)
    assert captured["opts"]["cookiefile"] == str(cookies)


def test_probe_passes_format_selector(monkeypatch):
    """format_selector 覆盖 settings.download_format。"""
    captured = {}

    def on_extract(url, download, opts):
        captured["opts"] = opts
        return {"title": "T", "url": "https://x"}

    monkeypatch.setattr(downloader, "YoutubeDL", make_fake_ydl(on_extract))
    probe_video("https://x/v", format_selector="best[height<=480]")
    assert captured["opts"]["format"] == "best[height<=480]"


def test_probe_uses_settings_format_when_no_override(monkeypatch):
    """未传 format_selector 时退到 settings.download_format。"""
    captured = {}

    def on_extract(url, download, opts):
        captured["opts"] = opts
        return {"title": "T", "url": "https://x"}

    monkeypatch.setattr(downloader, "YoutubeDL", make_fake_ydl(on_extract))
    probe_video("https://x/v")
    assert captured["opts"]["format"] == downloader.settings.download_format


def test_probe_dataclass_defaults():
    """ProbeResult 仅 ok 必填，其它字段均有默认值。"""
    r = ProbeResult(ok=True)
    assert r.title is None
    assert r.extractor is None
    assert r.duration is None
    assert r.formats_count == 0
    assert r.webpage_url is None
    assert r.reason is None
    assert r.detail is None
    assert r.language is None


def test_sniff_language_from_direct_language():
    from src.core.downloader import _sniff_language
    assert _sniff_language({"language": "en-US"}) == "en"
    assert _sniff_language({"language": "ja-JP"}) == "ja"
    assert _sniff_language({"language": "zh-Hans"}) == "zh-CN"
    assert _sniff_language({"language": "zh-Hant"}) == "zh-TW"


def test_sniff_language_from_subtitles_and_auto_captions():
    from src.core.downloader import _sniff_language
    assert _sniff_language({"subtitles": {"ja": [{"ext": "vtt"}]}}) == "ja"
    assert _sniff_language({"automatic_captions": {"es": [{"ext": "vtt"}]}}) == "es"


def test_probe_extracts_sniffed_language(monkeypatch):
    def on_extract(url, download, opts):
        return {
            "title": "Japanese Video",
            "language": "ja-JP",
            "url": "https://x/stream",
        }

    monkeypatch.setattr(downloader, "YoutubeDL", make_fake_ydl(on_extract))
    res = probe_video("https://x/v")
    assert res.ok is True
    assert res.language == "ja"
