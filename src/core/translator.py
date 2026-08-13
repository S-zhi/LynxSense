"""流水线第④步：翻译字幕（DeepSeek）。

输入：original.srt（识别阶段产物） + 任务 ID + 源/目标语言 + 模式
输出：TranslateResult（其中 srt_path 指向 data/{task_id}/translated.srt）

时间轴严格保留：只翻译每条字幕的文本，时间戳原样复制。
mode="bilingual" 时每条字幕保留原文 + 译文两行。

翻译走 DeepSeek 的 OpenAI 兼容 /chat/completions 接口，按批发送以减少请求数。
网络调用集中在 _call_deepseek（懒加载 httpx），便于单测 mock。

当前仅支持 DeepSeek，后续可在此扩展更多引擎。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from src.config import settings, ensure_task_dir, TRANSLATED_SRT
from src.core.srt_utils import Subtitle, parse_srt, write_srt

logger = logging.getLogger(__name__)


class TranslateError(RuntimeError):
    """翻译阶段失败。"""


@dataclass
class TranslateProgress:
    percent: float
    done: int
    total: int


@dataclass
class TranslateResult:
    srt_path: Path
    count: int
    bilingual: bool


ProgressHook = Callable[[TranslateProgress], None]
BatchHook = Callable[[int, int], None]

_LANG_NAMES = {
    "auto": "the source language",
    "zh-CN": "Simplified Chinese",
    "zh-TW": "Traditional Chinese",
    "zh": "Chinese",
    "en": "English",
    "ja": "Japanese",
    "ko": "Korean",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
    "ru": "Russian",
    "it": "Italian",
    "pt": "Portuguese",
    "vi": "Vietnamese",
    "th": "Thai",
    "ar": "Arabic",
}


def _lang_name(code: str) -> str:
    return _LANG_NAMES.get(code, code)


def _call_deepseek(messages: list, *, api_key: str, base_url: str, model: str, timeout: int) -> str:
    """调用 DeepSeek /chat/completions，返回模型回复文本。懒加载 httpx。"""
    import httpx

    url = base_url.rstrip("/") + "/chat/completions"
    resp = httpx.post(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": messages,
            "temperature": 0.3,
            "stream": False,
            "max_tokens": 4096,
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"]


def translate_texts(
    texts: List[str],
    source_lang: str,
    target_lang: str,
    *,
    api_key: Optional[str] = None,
    on_batch: Optional[BatchHook] = None,
) -> List[str]:
    """翻译一批文本，保持顺序与数量一致。

    数量不匹配时自动减半批量重试（模型可能截断长 JSON）。
    """
    if not texts:
        return []
    key = api_key or settings.deepseek_api_key
    if not key:
        raise TranslateError("缺少 DeepSeek API Key（设置 SUBTRANS_DEEPSEEK_API_KEY）")

    batch_size = max(1, settings.translate_batch_size)
    out: List[str] = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start:start + batch_size]
        translated = _translate_with_fallback(batch, source_lang, target_lang, key)
        out.extend(translated)
        if on_batch is not None:
            on_batch(len(out), len(texts))
    return out


def _translate_with_fallback(
    batch: List[str], source_lang: str, target_lang: str, api_key: str
) -> List[str]:
    """翻译一个批次，数量不匹配时自动减半拆分重试。"""
    size = len(batch)
    try:
        translated = _translate_batch(batch, source_lang, target_lang, api_key)
        if len(translated) == size:
            return translated
    except Exception:
        pass

    if size <= 1:
        raise TranslateError(
            f"翻译失败：即使单条也无法获得匹配结果"
        )
    # 减半拆分：递归处理两个子批
    half = max(1, size // 2)
    logger.warning("批量 %d 翻译结果不匹配，拆分为 %d + %d 重试", size, half, size - half)
    left = _translate_with_fallback(batch[:half], source_lang, target_lang, api_key)
    right = _translate_with_fallback(batch[half:], source_lang, target_lang, api_key)
    return left + right


def _translate_batch(batch: List[str], source_lang: str, target_lang: str, api_key: str) -> List[str]:
    tgt = _lang_name(target_lang)
    src = _lang_name(source_lang)
    numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(batch))
    system = (
        "You are a professional subtitle translator. "
        f"Translate each line from {src} to {tgt}. "
        "Keep the translation natural and concise, suitable for on-screen subtitles. "
        "Do not merge or split lines, do not add explanations."
    )
    user = (
        f"Translate these {len(batch)} subtitle lines to {tgt}. "
        f"Return ONLY a JSON array of exactly {len(batch)} strings, in the same order, "
        "with no extra text:\n\n"
        f"{numbered}"
    )
    content = _call_deepseek(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        api_key=api_key,
        base_url=settings.deepseek_base_url,
        model=settings.deepseek_model,
        timeout=settings.translate_timeout,
    )
    return _parse_translation_response(content, len(batch))


def _parse_translation_response(content: str, expected: int) -> List[str]:
    """解析模型回复：优先 JSON 数组，回退到按行去编号。"""
    text = content.strip()
    # 去掉 ```json ... ``` 代码围栏
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()

    try:
        arr = json.loads(text)
        if isinstance(arr, list):
            return [str(x) for x in arr]
    except json.JSONDecodeError:
        pass

    # 回退：按非空行拆，去掉行首 "1. " / "1) " 编号
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return [re.sub(r"^\s*\d+[\.\)]\s*", "", ln).strip() for ln in lines]


def translate_srt(
    srt_path: Path | str,
    task_id: str,
    source_lang: str,
    target_lang: str,
    *,
    mode: str = "mono",
    on_progress: Optional[ProgressHook] = None,
    api_key: Optional[str] = None,
) -> TranslateResult:
    """翻译 SRT 文件到 data/{task_id}/translated.srt，时间轴保持不变。

    Args:
        mode: "mono" 仅译文；"bilingual" 原文 + 译文两行。

    Raises:
        TranslateError: 输入不存在、缺 Key、或翻译失败。
    """
    srt_path = Path(srt_path)
    if not srt_path.exists():
        raise TranslateError(f"输入字幕不存在: {srt_path}")

    bilingual = mode == "bilingual"
    out_dir = ensure_task_dir(task_id)
    out_path = out_dir / TRANSLATED_SRT

    subs = parse_srt(srt_path)
    if not subs:
        write_srt([], out_path)
        return TranslateResult(srt_path=out_path, count=0, bilingual=bilingual)

    def _on_batch(done: int, total: int) -> None:
        if on_progress is not None:
            on_progress(TranslateProgress(
                percent=round(done / total * 100, 1),
                done=done,
                total=total,
            ))

    logger.info("开始翻译: task=%s 条数=%d -> %s", task_id, len(subs), target_lang)
    translated = translate_texts(
        [s.text for s in subs], source_lang, target_lang,
        api_key=api_key, on_batch=_on_batch,
    )

    out_subs = [
        Subtitle(
            index=sub.index,
            start=sub.start,
            end=sub.end,
            text=f"{sub.text}\n{tr}" if bilingual else tr,
        )
        for sub, tr in zip(subs, translated)
    ]
    write_srt(out_subs, out_path)
    logger.info("翻译完成: task=%s -> %s", task_id, out_path.name)
    return TranslateResult(srt_path=out_path, count=len(out_subs), bilingual=bilingual)


if __name__ == "__main__":
    # 独立测试入口：python -m src.core.translator <srt> [task_id] [target_lang] [mode]
    import os
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    # 加载项目根 .env（settings 导入时已读过环境变量，这里取 key 显式传入）
    _env = Path(__file__).resolve().parents[2] / ".env"
    if _env.exists():
        for _line in _env.read_text(encoding="utf-8").splitlines():
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _, _v = _line.partition("=")
                os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

    if len(sys.argv) < 2:
        print("用法: python -m src.core.translator <srt_path> [task_id] [target_lang] [mode]")
        raise SystemExit(1)

    srt = sys.argv[1]
    task = sys.argv[2] if len(sys.argv) > 2 else "manual_test"
    target = sys.argv[3] if len(sys.argv) > 3 else "zh-CN"
    m = sys.argv[4] if len(sys.argv) > 4 else "mono"
    api_key = os.getenv("SUBTRANS_DEEPSEEK_API_KEY") or os.getenv("DEEPSEEK_API_KEY")

    def _p(p: TranslateProgress) -> None:
        print(f"\r翻译中 {p.percent:5.1f}% ({p.done}/{p.total})", end="", flush=True)

    res = translate_srt(srt, task, "auto", target, mode=m, on_progress=_p, api_key=api_key)
    print(f"\n翻译完成: {res.srt_path}（{res.count} 条，双语={res.bilingual}）")
