"""流水线第③步：语音识别（Replicate-hosted Whisper）。

输入：audio.wav（提取阶段产物） + 任务 ID
输出：TranscribeResult（其中 srt_path 指向 data/{task_id}/original.srt）

通过 Replicate 云端 API 调用 Whisper，无需本地 GPU / 模型下载。
支持本地上传音频文件。进度通过轮询 prediction status 回调透传。

依赖：replicate（Python SDK）、REPLICATE_API_TOKEN（环境变量或 .env）。
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

import httpx
import replicate

from src.config import settings, ensure_task_dir, ORIGINAL_SRT
from src.core.srt_utils import Subtitle, write_srt

logger = logging.getLogger(__name__)


class TranscribeError(RuntimeError):
    """语音识别阶段失败。"""


@dataclass
class TranscribeProgress:
    percent: Optional[float]
    status: str   # "starting" | "processing" | "succeeded"


@dataclass
class TranscribeResult:
    srt_path: Path
    language: str
    language_probability: Optional[float]
    segment_count: int
    duration: Optional[float]

# TODO replicate
ProgressHook = Callable[[TranscribeProgress], None]


def _load_env():
    """手动加载项目根 .env（不依赖 python-dotenv）。"""
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and val and key not in os.environ:
            os.environ[key] = val


def _safe_callback(hook: ProgressHook, pct: Optional[float], status: str) -> None:
    try:
        hook(TranscribeProgress(percent=pct, status=status))
    except Exception:
        logger.exception("进度回调异常，已忽略")


def _run_replicate_with_retry(
    model_ref: str,
    build_input: Callable[[], dict],
    *,
    timeout: int,
    retries: int,
    on_progress: Optional[ProgressHook] = None,
):
    """调用 Replicate，超时/网络错误时按退避重试（应对模型冷启动）。

    - 用带长读超时的 Client，让冷启动的等待请求不至于过早 read-timeout。
    - 仅对超时/网络类异常重试；模型报错 / 鉴权失败直接抛出，不浪费重试。
    - build_input 每次调用返回全新 input（本地文件 handle 用过即废，必须重开）。
    """
    client = replicate.Client(
        api_token=os.getenv("REPLICATE_API_TOKEN"),
        timeout=httpx.Timeout(float(timeout), connect=30.0),
    )
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            input_payload = build_input()
            try:
                return client.run(model_ref, input=input_payload)
            finally:
                audio_file = input_payload.get("audio_path")
                close = getattr(audio_file, "close", None)
                if callable(close):
                    close()
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last_exc = e
            logger.warning(
                "Replicate 第 %d/%d 次失败(超时/网络): %s", attempt, retries, e
            )
            if attempt < retries:
                backoff = min(10 * 2 ** (attempt - 1), 60)  # 10s, 20s, 40s, 上限 60s
                if on_progress is not None:
                    _safe_callback(on_progress, 1.0, "starting")  # 冷启动重试中
                time.sleep(backoff)
        except Exception as e:  # 模型报错 / 鉴权等非瞬时错误，不重试
            raise TranscribeError(f"Replicate 语音识别失败: {e}") from e

    raise TranscribeError(
        f"Replicate 语音识别多次超时失败（已重试 {retries} 次；"
        f"冷启动可调大 SUBTRANS_REPLICATE_TIMEOUT）: {last_exc}"
    )


def transcribe(
    audio_path: Path | str,
    task_id: str,
    on_progress: Optional[ProgressHook] = None,
    *,
    language: Optional[str] = None,
    model_name: Optional[str] = None,
) -> TranscribeResult:
    """把音频识别为原文字幕 data/{task_id}/original.srt。

    通过 Replicate API（stayallive/whisper-subtitles）调用 Whisper，
    返回带时间戳的 segments，然后转写为 SRT。

    Args:
        audio_path: 输入音频（通常是 audio.wav）；支持本地路径和 HTTP(S) URL。
        task_id: 任务 ID，决定输出目录。
        on_progress: 可选进度回调。
        language: 源语言代码；None / "" / "auto" 表示自动检测。
        model_name: Whisper 模型名，如 "tiny.en" / "small"。
    """
    # TODO 回调函数整体伪造
    _load_env()
    if not os.getenv("REPLICATE_API_TOKEN"):
        raise TranscribeError("未设置 REPLICATE_API_TOKEN（请在 .env 中配置）")

    lang = None if language in (None, "", "auto") else language
    model = model_name or "small"

    out_dir = ensure_task_dir(task_id)
    srt_path = out_dir / ORIGINAL_SRT

    # 区分远程 URL vs 本地文件
    audio_str = str(audio_path)
    is_remote = audio_str.startswith(("http://", "https://"))
    if not is_remote:
        p = Path(audio_path)
        if not p.exists():
            raise TranscribeError(f"输入音频不存在: {p}")
        audio_str = str(p)

    logger.info("开始识别(Replicate): task=%s model=%s lang=%s", task_id, model, lang)

    def build_input() -> dict:
        # 每次尝试都重建：本地文件 handle 用过一次就废，重试必须重新打开
        inp: dict = {"model_name": model, "vad_filter": True}
        inp["audio_path"] = audio_str if is_remote else open(audio_str, "rb")
        if lang:
            inp["language"] = lang
        return inp

    if on_progress is not None:
        _safe_callback(on_progress, 1.0, "starting")

    output = _run_replicate_with_retry(
        settings.replicate_whisper_model,
        build_input,
        timeout=settings.replicate_timeout,
        retries=settings.replicate_retries,
        on_progress=on_progress,
    )

    if on_progress is not None:
        _safe_callback(on_progress, 95.0, "succeeded")

    # 解析 segments → Subtitle 列表
    segments = _extract_segments(output)
    subs: List[Subtitle] = []
    for i, seg in enumerate(segments, start=1):
        text = (seg.get("text") or "").strip()
        if text:
            subs.append(Subtitle(
                index=i,
                start=float(seg.get("start", 0.0)),
                end=float(seg.get("end", 0.0)),
                text=text,
            ))

    write_srt(subs, srt_path)
    if on_progress is not None:
        _safe_callback(on_progress, 100.0, "succeeded")

    detected = lang or "unknown"
    duration = subs[-1].end if subs else None
    result = TranscribeResult(
        srt_path=srt_path,
        language=detected,
        language_probability=None,
        segment_count=len(subs),
        duration=duration,
    )
    logger.info("识别完成: task=%s lang=%s segments=%d", task_id, detected, len(subs))
    return result


def _extract_segments(output) -> List[dict]:
    """从 Replicate 返回中提取 segments 列表，兼容多种输出格式。"""
    # 格式 1: dict 包含 srt_file（Replicate FileOutput URL），下载后解析
    if isinstance(output, dict):
        if "srt_file" in output:
            srt_url = str(output["srt_file"])
            logger.info("下载 Replicate SRT: %s", srt_url[:80])
            srt_text = _download_text(srt_url)
            return _parse_srt_segments(srt_text)
        if "segments" in output:
            return output["segments"]
        if "srt" in output:
            return _parse_srt_segments(output["srt"])
        if "text" in output:
            return [output]

    # 格式 2: output 直接是 segments 列表
    if isinstance(output, list):
        if output and isinstance(output[0], dict) and "text" in output[0]:
            return output
        return [{"text": str(x), "start": 0.0, "end": 0.0} for x in output if x]

    return []


def _download_text(url: str) -> str:
    """下载远程文本（SRT）。"""
    resp = httpx.get(url, timeout=30)
    resp.raise_for_status()
    return resp.text


def _parse_srt_segments(srt_text: str) -> List[dict]:
    """解析 SRT 文本为 segment dict 列表。

    兼容两种格式：
    - 标准 SRT：每个 segment 之间用空行分隔。
    - Replicate 输出的紧凑 SRT：无空行，按「序号 + 时间轴 + 文本」模式逐行拆分。
    """
    import re
    text = srt_text.strip()

    # 策略 1：标准 SRT，有空行分隔
    if "\n\n" in text:
        return _parse_standard_srt(text)

    # 策略 2：逐行扫描，按序号行模式拆分
    return _parse_compact_srt(text)


def _parse_standard_srt(text: str) -> List[dict]:
    import re
    segments = []
    for block in re.split(r"\n\s*\n", text):
        lines = [ln for ln in block.splitlines() if ln.strip()]
        time_idx = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if time_idx is None:
            continue
        start_s, _, end_s = lines[time_idx].partition("-->")
        try:
            start = _parse_srt_ts(start_s)
            end = _parse_srt_ts(end_s)
        except (ValueError, IndexError):
            continue
        txt = " ".join(lines[time_idx + 1:]).strip()
        if txt:
            segments.append({"text": txt, "start": start, "end": end})
    return segments


def _parse_compact_srt(text: str) -> List[dict]:
    """解析无空行的紧凑 SRT：行首为纯数字即新 segment 开始。"""
    lines = text.splitlines()
    segments = []
    buf: List[str] = []
    for ln in lines:
        stripped = ln.strip()
        if not stripped:
            continue
        # 纯数字行 = 新 segment 开始
        if stripped.isdigit() and buf:
            seg = _block_to_segment(buf)
            if seg:
                segments.append(seg)
            buf = [stripped]
        else:
            buf.append(stripped)
    if buf:
        seg = _block_to_segment(buf)
        if seg:
            segments.append(seg)
    return segments


def _block_to_segment(lines: List[str]) -> Optional[dict]:
    """把一段 SRT 行（序号 + 时间轴 + 文本）转成 segment dict。"""
    time_idx = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
    if time_idx is None:
        return None
    # 跳过序号行（纯数字）
    text_lines = [ln for i, ln in enumerate(lines) if i > time_idx and not ln.strip().isdigit()]
    if not text_lines:
        return None
    start_s, _, end_s = lines[time_idx].partition("-->")
    try:
        start = _parse_srt_ts(start_s)
        end = _parse_srt_ts(end_s)
    except (ValueError, IndexError):
        return None
    return {"text": " ".join(text_lines).strip(), "start": start, "end": end}


def _parse_srt_ts(ts: str) -> float:
    ts = ts.strip().replace(".", ",")
    hms, _, millis = ts.partition(",")
    h, m, s = hms.split(":")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(millis or 0) / 1000


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if len(sys.argv) < 2:
        print("用法: python -m src.core.transcriber <audio_path_or_url> [task_id] [language] [model]")
        raise SystemExit(1)

    audio = sys.argv[1]
    task = sys.argv[2] if len(sys.argv) > 2 else "manual_test"
    lang = sys.argv[3] if len(sys.argv) > 3 else None
    mod = sys.argv[4] if len(sys.argv) > 4 else None

    def _p(p: TranscribeProgress) -> None:
        print(f"\r识别中 [{p.status}] {p.percent or '?'}%", end="", flush=True)

    res = transcribe(audio, task, on_progress=_p, language=lang, model_name=mod)
    print(f"\n识别结果: lang={res.language} segments={res.segment_count} dur={res.duration}")
    print(f"  字幕: {res.srt_path}")
