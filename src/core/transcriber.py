"""流水线第③步：语音识别（Replicate-hosted Whisper）。

输入：audio.wav（提取阶段产物） + 任务 ID
输出：TranscribeResult（其中 srt_path 指向 data/{task_id}/original.srt）

通过 Replicate 云端 API 调用 Whisper，无需本地 GPU / 模型下载。
支持本地上传音频文件。进度通过轮询 prediction status 回调透传。

依赖：replicate（Python SDK）、REPLICATE_API_TOKEN（环境变量或 .env）。
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

import httpx
import replicate

from src.config import settings, ensure_task_dir, ORIGINAL_SRT
from src.core.srt_utils import Subtitle, decode_srt_bytes, write_srt

logger = logging.getLogger(__name__)


class TranscribeError(RuntimeError):
    """语音识别阶段失败。"""

    def __init__(self, message: str, *, code: str = "transcribe_error"):
        super().__init__(message)
        self.code = code


class TranscribeCancelledError(RuntimeError):
    """语音识别已被取消。"""


def _do_cancel_check(cancel_check: Optional[Callable[[], None]]) -> None:
    if cancel_check is not None:
        cancel_check()


@dataclass
class TranscribeProgress:
    percent: Optional[float]
    status: str   # "starting" | "processing" | "retrying" | "succeeded"


@dataclass
class TranscribeResult:
    srt_path: Path
    language: str
    language_probability: Optional[float]
    segment_count: int
    duration: Optional[float]


ProgressHook = Callable[[TranscribeProgress], None]

_PREDICTION_STATE_FILE = "replicate_prediction.json"
_ACTIVE_PREDICTION_STATUSES = {"starting", "processing"}
_TERMINAL_PREDICTION_STATUSES = {"succeeded", "failed", "canceled", "aborted"}


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


def _prediction_state_path(state_dir: Path) -> Path:
    return state_dir / _PREDICTION_STATE_FILE


def _load_prediction_state(state_dir: Path, model_ref: str) -> Optional[dict]:
    """读取匹配模型的 prediction 状态；损坏或不匹配时返回 None。

    Args:
        state_dir: 保存任务运行状态的目录。
        model_ref: 当前 Replicate 模型版本引用。
    Returns:
        包含 prediction_id 等字段的状态字典，或不可用时的 None。
    Side effects:
        对不可读或模型不匹配的状态文件记录 warning，但不删除文件。
    """
    path = _prediction_state_path(state_dir)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        logger.warning("Replicate prediction 状态文件不可读，忽略并重新创建: %s", path)
        return None
    if not isinstance(payload, dict):
        logger.warning("Replicate prediction 状态格式无效，忽略并重新创建: %s", path)
        return None
    prediction_id = payload.get("prediction_id")
    if payload.get("model_ref") != model_ref or not isinstance(prediction_id, str):
        logger.warning("Replicate prediction 状态与当前模型不匹配，忽略: %s", path)
        return None
    return payload


def _load_prediction_id(state_dir: Path, model_ref: str) -> Optional[str]:
    """读取可恢复的 prediction ID；无有效状态时返回 None。

    Args:
        state_dir: 保存任务运行状态的目录。
        model_ref: 当前 Replicate 模型版本引用。
    Returns:
        匹配状态中的 prediction ID，或 None。
    Side effects:
        复用状态读取逻辑并保留其日志行为。
    """
    payload = _load_prediction_state(state_dir, model_ref)
    return payload["prediction_id"] if payload is not None else None


def _save_prediction_state(
    state_dir: Path,
    *,
    prediction_id: str,
    model_ref: str,
    status: str,
) -> None:
    path = _prediction_state_path(state_dir)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    payload = {
        "prediction_id": prediction_id,
        "model_ref": model_ref,
        "status": status,
        "updated_at": int(time.time()),
    }
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp_path.replace(path)


def _clear_prediction_state(state_dir: Path) -> None:
    path = _prediction_state_path(state_dir)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("清理 Replicate prediction 状态文件失败: %s", path, exc_info=True)


def _sleep_with_cancel(seconds: float, cancel_check: Optional[Callable[[], None]]) -> None:
    elapsed = 0.0
    while elapsed < seconds:
        _do_cancel_check(cancel_check)
        sleep_time = min(0.5, seconds - elapsed)
        time.sleep(sleep_time)
        elapsed += sleep_time
    _do_cancel_check(cancel_check)


def _error_code_for_status(status: Optional[int]) -> str:
    if status in (401, 403):
        return "unauthorized"
    if status == 429:
        return "rate_limited"
    if status and status >= 500:
        return "upstream_error"
    if status and 400 <= status < 500:
        return "model_error"
    return "upstream_error"


def _run_replicate_with_retry(
    model_ref: str,
    build_input: Callable[[], dict],
    *,
    timeout: int,
    retries: int,
    retry_interval: float,
    poll_interval: float,
    state_dir: Path,
    on_progress: Optional[ProgressHook] = None,
    last_pct: Optional[float] = 1.0,
    cancel_check: Optional[Callable[[], None]] = None,
):
    """创建并轮询 Replicate prediction，网络失败时延迟重试同一任务。

    prediction ID 会持久化到任务目录。只要远端状态仍是 starting/processing，
    就始终查询同一个 prediction，不会再次提交。仅在创建请求本身没有取得 ID
    且发生网络错误时，才可能在 retry_interval 后重新创建。
    """
    _do_cancel_check(cancel_check)
    client = replicate.Client(
        api_token=os.getenv("REPLICATE_API_TOKEN"),
        timeout=httpx.Timeout(float(timeout), connect=30.0),
    )
    state = _load_prediction_state(state_dir, model_ref)
    prediction_id = state["prediction_id"] if state is not None else None
    if state is not None and state.get("status") == "cancel_pending":
        try:
            client.predictions.cancel(prediction_id)
        except Exception:
            logger.warning("重试取消 Replicate prediction 失败: id=%s", prediction_id, exc_info=True)
        else:
            _clear_prediction_state(state_dir)
            prediction_id = None
    network_failures = 0
    last_exc: Exception | None = None

    while True:
        try:
            _do_cancel_check(cancel_check)
            if prediction_id is None:
                input_payload = build_input()
                try:
                    prediction = client.predictions.create(
                        version=model_ref,
                        input=input_payload,
                        wait=False,
                    )
                finally:
                    audio_file = input_payload.get("audio_path")
                    close = getattr(audio_file, "close", None)
                    if callable(close):
                        close()
                prediction_id = prediction.id
                logger.info("Replicate prediction 已创建: id=%s", prediction_id)
            else:
                prediction = client.predictions.get(prediction_id)

            network_failures = 0
            status = str(getattr(prediction, "status", "")).lower()
            _save_prediction_state(
                state_dir,
                prediction_id=prediction_id,
                model_ref=model_ref,
                status=status,
            )

            if status in _ACTIVE_PREDICTION_STATUSES:
                if on_progress is not None:
                    _safe_callback(on_progress, last_pct, status)
                logger.info("Replicate prediction 等待中: id=%s status=%s", prediction_id, status)
                _sleep_with_cancel(poll_interval, cancel_check)
                continue

            if status == "succeeded":
                if on_progress is not None:
                    _safe_callback(on_progress, 100.0, "succeeded")
                return prediction.output

            if status in _TERMINAL_PREDICTION_STATUSES:
                error = getattr(prediction, "error", None) or f"prediction 状态为 {status}"
                _clear_prediction_state(state_dir)
                raise TranscribeError(
                    f"Replicate 语音识别失败: {error}",
                    code="model_error",
                )

            raise TranscribeError(
                f"Replicate 返回未知 prediction 状态: {status or 'empty'}",
                code="upstream_error",
            )
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last_exc = e
            network_failures += 1
            logger.warning(
                "Replicate 第 %d/%d 次网络失败，%s 秒后重试%s: %s",
                network_failures,
                retries,
                retry_interval,
                "查询同一 prediction" if prediction_id else "创建",
                e,
            )
            if network_failures >= retries:
                break
            if on_progress is not None:
                _safe_callback(on_progress, last_pct, "retrying")
            _sleep_with_cancel(retry_interval, cancel_check)
        except (httpx.HTTPStatusError, replicate.exceptions.ReplicateError) as e:
            status = getattr(e, "status", None) or getattr(getattr(e, "response", None), "status_code", None)
            raise TranscribeError(
                f"Replicate 语音识别失败: {e}",
                code=_error_code_for_status(status),
            ) from e
        except Exception as e:  # 模型报错 / 鉴权等非瞬时错误，不重试
            if isinstance(e, TranscribeCancelledError) or type(e).__name__ == "PipelineCancelledError":
                if prediction_id is not None:
                    try:
                        client.predictions.cancel(prediction_id)
                    except Exception:
                        _save_prediction_state(
                            state_dir,
                            prediction_id=prediction_id,
                            model_ref=model_ref,
                            status="cancel_pending",
                        )
                        path = _prediction_state_path(state_dir)
                        logger.warning(
                            "取消 Replicate prediction 失败，远端可能仍在运行；将于下次任务启动时重试: id=%s state=%s",
                            prediction_id,
                            path,
                            exc_info=True,
                        )
                    else:
                        _clear_prediction_state(state_dir)
                raise
            if isinstance(e, TranscribeError):
                raise
            raise TranscribeError(f"Replicate 语音识别失败: {e}") from e

    _do_cancel_check(cancel_check)
    raise TranscribeError(
        f"Replicate 语音识别多次网络失败（共尝试 {retries} 次；"
        f"每次间隔 {retry_interval} 秒）: {last_exc}",
        code="network_error",
    )


def transcribe(
    audio_path: Path | str,
    task_id: str,
    on_progress: Optional[ProgressHook] = None,
    *,
    language: Optional[str] = None,
    model_name: Optional[str] = None,
    cancel_check: Optional[Callable[[], None]] = None,
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
    # 进度提示：Replicate 简单 API 调用等待模型冷启动完成并返回结果；等待期间保持 starting/retrying 状态
    _load_env()
    if not os.getenv("REPLICATE_API_TOKEN"):
        raise TranscribeError("未设置 REPLICATE_API_TOKEN（请在 .env 中配置）", code="missing_api_key")

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

    _do_cancel_check(cancel_check)
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
        retry_interval=settings.replicate_retry_interval,
        poll_interval=settings.replicate_poll_interval,
        state_dir=out_dir,
        on_progress=on_progress,
        cancel_check=cancel_check,
    )
    _do_cancel_check(cancel_check)

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
    _clear_prediction_state(out_dir)
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
    try:
        resp = httpx.get(url, timeout=30)
        resp.raise_for_status()
        raw = getattr(resp, "content", None)
        if raw is None:
            raw = getattr(resp, "text", "").encode("utf-8")
        text, _ = decode_srt_bytes(raw)
        return text
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        code = "unauthorized" if status in (401, 403) else "rate_limited" if status == 429 else "upstream_error"
        raise TranscribeError(f"下载 Replicate 字幕文件失败 HTTP {status}", code=code) from exc
    except httpx.RequestError as exc:
        raise TranscribeError("下载 Replicate 字幕文件超时或网络异常", code="network_error") from exc


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
