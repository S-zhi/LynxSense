"""管理用户提供的本地 Whisper 模型，绝不从 Hub 下载。"""

from __future__ import annotations

import json
import math
import re
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any, BinaryIO

from src.config import settings

MODEL_CATALOG: tuple[dict[str, Any], ...] = (
    {"name": "tiny", "label": "Whisper Tiny", "size": "~75 MB"},
    {"name": "base", "label": "Whisper Base", "size": "~145 MB"},
    {"name": "small", "label": "Whisper Small", "size": "~465 MB"},
    {"name": "medium", "label": "Whisper Medium", "size": "~1.5 GB"},
    {"name": "large-v3", "label": "Whisper Large V3", "size": "~3 GB"},
    {"name": "large-v3-turbo", "label": "Whisper Large V3 Turbo", "size": "~1.6 GB"},
)
MODEL_NAMES = frozenset(item["name"] for item in MODEL_CATALOG)
_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
_WEIGHT = re.compile(r"^(pytorch_model(?:-\d{5}-of-\d{5})?\.bin|model(?:-\d{5}-of-\d{5})?\.safetensors)$")
_SIDECARS = frozenset({
    "config.json", "generation_config.json", "preprocessor_config.json",
    "tokenizer_config.json", "tokenizer.json", "vocab.json", "merges.txt",
    "normalizer.json", "special_tokens_map.json", "added_tokens.json",
    "pytorch_model.bin.index.json", "model.safetensors.index.json",
})
_HF_MARKER = ".subtrans-hf.json"


class ModelValidationError(ValueError):
    """上传文件或模型行为不符合本地 Whisper 协议。"""


class ModelDependencyError(RuntimeError):
    """检查环境缺少可选的 Hugging Face 推理依赖。"""


def _safe_name(name: str) -> str:
    if not _NAME.fullmatch(name) or name.startswith(".") or name in MODEL_NAMES:
        raise ModelValidationError("模型名称只能由字母、数字、下划线和连字符组成，且不能与内置模型重名")
    return name


def _safe_filename(filename: str) -> str:
    if not filename or filename != Path(filename).name or "\\" in filename or filename.startswith("."):
        raise ModelValidationError("模型文件必须位于目录根部，且不能包含路径")
    if not (_WEIGHT.fullmatch(filename) or filename in _SIDECARS):
        raise ModelValidationError(f"不支持的模型文件: {filename}")
    return filename


def _weight_files(path: Path) -> list[Path]:
    weights = sorted(p for p in path.iterdir() if p.is_file() and _WEIGHT.fullmatch(p.name))
    if not weights:
        raise ModelValidationError("缺少 .bin 或 .safetensors 权重")
    suffixes = {p.suffix for p in weights}
    if len(suffixes) != 1:
        raise ModelValidationError("不能混用 .bin 和 .safetensors 权重")
    index_name = "pytorch_model.bin.index.json" if ".bin" in suffixes else "model.safetensors.index.json"
    index = path / index_name
    if len(weights) > 1 or any("-of-" in p.name for p in weights):
        if not index.is_file():
            raise ModelValidationError("分片权重缺少索引文件")
        try:
            mapping = json.loads(index.read_text(encoding="utf-8"))["weight_map"]
            declared = set(mapping.values())
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ModelValidationError("权重索引格式无效") from exc
        if declared != {p.name for p in weights}:
            raise ModelValidationError("权重分片与索引不一致")
    elif index.exists():
        raise ModelValidationError("单文件权重不应带分片索引")
    return weights


def validate_hf_model(path: Path) -> None:
    """离线验证官方 Whisper 文件、权重和 ASR 输入输出契约。"""
    if path.is_symlink() or not path.is_dir():
        raise ModelValidationError("模型目录无效")
    for entry in path.iterdir():
        if entry.name == _HF_MARKER and entry.is_file() and not entry.is_symlink():
            continue
        if entry.is_symlink() or not entry.is_file() or (entry.name not in _SIDECARS and not _WEIGHT.fullmatch(entry.name)):
            raise ModelValidationError(f"模型目录包含不支持的文件: {entry.name}")
        if entry.stat().st_size == 0:
            raise ModelValidationError(f"模型文件为空: {entry.name}")
    try:
        config = json.loads((path / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ModelValidationError("缺少有效的 config.json") from exc
    if not isinstance(config, dict) or config.get("model_type") != "whisper":
        raise ModelValidationError("只支持 Hugging Face Whisper 语音识别模型")
    if not (path / "preprocessor_config.json").is_file():
        raise ModelValidationError("缺少 preprocessor_config.json")
    if not ((path / "tokenizer.json").is_file() or (path / "vocab.json").is_file() and (path / "merges.txt").is_file()):
        raise ModelValidationError("缺少 Whisper tokenizer 文件")
    weights = _weight_files(path)

    try:
        if weights[0].suffix == ".safetensors":
            from safetensors import safe_open
            for weight in weights:
                with safe_open(weight, framework="pt", device="cpu") as handle:
                    if not handle.keys():
                        raise ModelValidationError(f"权重文件为空: {weight.name}")
        else:
            import torch
            for weight in weights:
                state = torch.load(weight, map_location="cpu", weights_only=True)
                if not isinstance(state, dict) or not state:
                    raise ModelValidationError(f"权重结构无效: {weight.name}")
                del state
        from transformers import WhisperForConditionalGeneration, WhisperProcessor, pipeline

        processor = WhisperProcessor.from_pretrained(str(path), local_files_only=True, trust_remote_code=False)
        model, loading = WhisperForConditionalGeneration.from_pretrained(
            str(path), local_files_only=True, trust_remote_code=False,
            use_safetensors=weights[0].suffix == ".safetensors", output_loading_info=True,
        )
        if loading.get("missing_keys") or loading.get("mismatched_keys"):
            raise ModelValidationError("权重缺少 Whisper 所需参数或参数形状不匹配")
        recognizer = pipeline(
            "automatic-speech-recognition", model=model,
            tokenizer=processor.tokenizer, feature_extractor=processor.feature_extractor,
            device="cpu",
        )
        import numpy as np
        result = recognizer(
            {"array": np.zeros(16000, dtype=np.float32), "sampling_rate": 16000},
            return_timestamps=True, generate_kwargs={"task": "transcribe"},
        )
        if not isinstance(result, dict) or not isinstance(result.get("text"), str):
            raise ModelValidationError("模型输出不符合 Whisper ASR 协议")
        chunks = result.get("chunks")
        if not isinstance(chunks, list) or (result["text"].strip() and not chunks):
            raise ModelValidationError("模型输出缺少时间戳片段")
        for chunk in chunks:
            if not isinstance(chunk.get("text"), str) or not isinstance(chunk.get("timestamp"), (tuple, list)) or len(chunk["timestamp"]) != 2:
                raise ModelValidationError("模型时间戳输出无效")
            start, end = chunk["timestamp"]
            if start is None or not isinstance(start, (float, int)) or not math.isfinite(start) or start < 0 or end is not None and (not isinstance(end, (float, int)) or not math.isfinite(end) or end < start):
                raise ModelValidationError("模型时间戳输出无效")
    except ModelValidationError:
        raise
    except ImportError as exc:
        raise ModelDependencyError("请先启用 Hugging Face 模型依赖：uv sync --extra hf-models") from exc
    except Exception as exc:
        raise ModelValidationError(f"模型离线加载或推理检查失败: {exc}") from exc


class LocalModelManager:
    """只管理本应用目录中的副本，用户原文件不会被清理。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()

    @property
    def root(self) -> Path:
        configured = getattr(settings, "local_whisper_download_root", None)
        root = Path(configured).expanduser() if configured else Path(settings.data_dir) / "models"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _disk_ready(self, name: str) -> bool:
        path = self.root / name
        return path.is_dir() and not path.is_symlink() and (
            (path / _HF_MARKER).is_file() or
            (name in MODEL_NAMES and (path / ".subtrans-ready").is_file())
        )

    def is_ready(self, name: str) -> bool:
        return bool(_NAME.fullmatch(name)) and self._disk_ready(name)

    def is_hf(self, name: str) -> bool:
        return self.is_ready(name) and (self.root / name / _HF_MARKER).is_file()

    def resolve_path(self, name: str) -> str:
        if not self.is_ready(name):
            raise RuntimeError(f"本地 Whisper 模型尚未就绪: {name}")
        return str(self.root / name)

    def list_models(self) -> list[dict[str, Any]]:
        with self._lock:
            result = [{**item, "status": "READY" if self._disk_ready(item["name"]) else "NOT_INSTALLED", "format": "ctranslate2"} for item in MODEL_CATALOG]
            for path in sorted(self.root.iterdir()):
                if path.name in MODEL_NAMES or not path.is_dir() or path.is_symlink() or not (path / _HF_MARKER).is_file():
                    continue
                try:
                    meta = json.loads((path / _HF_MARKER).read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                result.append({"name": path.name, "label": meta.get("label", path.name), "size": sum(p.stat().st_size for p in path.iterdir() if p.is_file()), "status": "READY", "format": "huggingface"})
            return result

    def import_files(self, name: str, label: str, files: list[tuple[str, BinaryIO]]) -> dict[str, Any]:
        _safe_name(name)
        if not files:
            raise ModelValidationError("请选择模型目录中的文件")
        with self._lock:
            destination = self.root / name
            if destination.exists() or destination.is_symlink():
                raise FileExistsError(f"模型名称已存在: {name}")
            stage = Path(tempfile.mkdtemp(prefix=".import-", dir=self.root))
            try:
                seen: set[str] = set()
                for filename, source in files:
                    filename = _safe_filename(filename)
                    if filename in seen:
                        raise ModelValidationError(f"重复的模型文件: {filename}")
                    seen.add(filename)
                    with (stage / filename).open("xb") as target:
                        shutil.copyfileobj(source, target, length=1024 * 1024)
                validate_hf_model(stage)
                (stage / _HF_MARKER).write_text(json.dumps({"label": label[:100] or name}), encoding="utf-8")
                stage.rename(destination)
            finally:
                if stage.exists():
                    shutil.rmtree(stage)
            return next(item for item in self.list_models() if item["name"] == name)

    def check(self, name: str) -> dict[str, Any]:
        _safe_name(name)
        with self._lock:
            path = self.root / name
            if not self.is_hf(name):
                raise FileNotFoundError(f"未找到受管 Hugging Face 模型: {name}")
            try:
                validate_hf_model(path)
            except ModelValidationError:
                shutil.rmtree(path)
                raise
            return next(item for item in self.list_models() if item["name"] == name)

    def delete(self, name: str) -> dict[str, Any]:
        with self._lock:
            if not _NAME.fullmatch(name) or not self.is_ready(name):
                raise FileNotFoundError(f"本地模型不存在: {name}")
            shutil.rmtree(self.root / name)
        return {"name": name, "status": "NOT_INSTALLED"}


model_manager = LocalModelManager()
