"""Hugging Face モデルの遅延ロードと検出結果キャッシュ。"""

import asyncio
import re
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

from .types import Detection

_JAPANESE_RE = re.compile(r"[ぁ-んァ-ヶ一-龯々〆ヵヶ]")
_CACHE_VERSION = "yomi-linter-v1"


class YomiModelLoading(RuntimeError):
    """初回ロード中。現在の発話は待たずにfail-openさせる。"""


def contains_japanese(text: str) -> bool:
    return bool(_JAPANESE_RE.search(text or ""))


class YomiLinterRuntime:
    """One model instance per process; loading and inference run off the event loop."""

    def __init__(self, cache_size: int = 256, cache_ttl_seconds: float = 300.0):
        self._pipeline: Any = None
        self._load_lock = asyncio.Lock()
        self._load_task: Optional[asyncio.Task] = None
        self._infer_lock = asyncio.Lock()
        self._cache: "OrderedDict[Tuple[str, str, float, str], Tuple[float, List[Detection]]]" = OrderedDict()
        self._cache_size = cache_size
        self._cache_ttl = cache_ttl_seconds
        self._signature: Optional[Tuple[str, str, str, Optional[str]]] = None
        self.state = "not_started"
        self.last_error: Optional[str] = None
        self.effective_quantization: Optional[str] = None

    async def reset(self) -> None:
        async with self._load_lock:
            self._pipeline = None
            self._load_task = None
            self._signature = None
            self._cache.clear()
            self.state = "not_started"
            self.last_error = None
            self.effective_quantization = None

    async def detect(self, text: str, settings: Dict[str, Any]) -> List[Detection]:
        if not text.strip() or not contains_japanese(text):
            return []
        model_id = str(settings["model_id"])
        threshold = float(settings["confidence_threshold"])
        key = (text, model_id, threshold, _CACHE_VERSION)
        cached = self._cache.get(key)
        if cached and time.monotonic() - cached[0] <= self._cache_ttl:
            self._cache.move_to_end(key)
            return list(cached[1])

        await self._ensure_loaded(settings)
        async with self._infer_lock:
            raw = await asyncio.to_thread(self._pipeline, text)
        detections = self._adapt_output(text, raw, threshold)
        self._cache[key] = (time.monotonic(), detections)
        self._cache.move_to_end(key)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return list(detections)

    async def _ensure_loaded(self, settings: Dict[str, Any]) -> None:
        signature = (
            str(settings["model_id"]),
            str(settings.get("device", "cpu")),
            str(settings.get("quantization", "int8")),
        )
        if self._pipeline is not None and self._signature == signature:
            return
        if self._load_task is not None:
            if not self._load_task.done():
                raise YomiModelLoading("Yomi Linterモデルをバックグラウンドで準備中です")
            try:
                await asyncio.shield(self._load_task)
            finally:
                self._load_task = None
            if self._pipeline is not None and self._signature == signature:
                return
        async with self._load_lock:
            if self._pipeline is not None and self._signature == signature:
                return
            if self._load_task is None:
                self.state = "downloading_or_loading"
                self.last_error = None
                self._load_task = asyncio.create_task(
                    self._load_and_store(dict(settings), signature)
                )
        # 約500MBの初回取得をTTS timeoutへ巻き込まない。taskはprocess内で一つだけ継続する。
        if self._load_task and not self._load_task.done():
            raise YomiModelLoading("Yomi Linterモデルをバックグラウンドで準備中です")
        if self._load_task:
            await asyncio.shield(self._load_task)

    async def _load_and_store(
        self,
        settings: Dict[str, Any],
        signature: Tuple[str, str, str, Optional[str]],
    ) -> None:
        try:
            pipeline_value, quantization = await asyncio.to_thread(
                self._load_pipeline, settings
            )
            self._pipeline = pipeline_value
            self.effective_quantization = quantization
            self._signature = signature
            self.state = "ready"
        except Exception as exc:
            self.state = "error"
            self.last_error = (
                f"repo={settings['model_id']} "
                f"cause={type(exc).__name__}: {exc}"
            )
            raise RuntimeError(self.last_error) from exc

    @staticmethod
    def _load_pipeline(settings: Dict[str, Any]):
        try:
            import torch
            from transformers import (
                AutoModelForTokenClassification,
                PreTrainedTokenizerFast,
                pipeline,
            )
        except ImportError as exc:
            raise RuntimeError(
                "Yomi Linter依存関係がありません。`pip install -e .[yomi-linter]`を実行してください"
            ) from exc

        model_id = str(settings["model_id"])
        common = {
            "revision": settings.get("revision") or "main",
            "local_files_only": bool(settings.get("local_files_only", False)),
        }
        tokenizer = PreTrainedTokenizerFast.from_pretrained(model_id, **common)
        model = AutoModelForTokenClassification.from_pretrained(
            model_id,
            use_safetensors=True,
            trust_remote_code=False,
            **common,
        )
        tokenizer.model_max_length = min(
            int(getattr(model.config, "max_position_embeddings", 8192)), 8192
        )
        device_name = str(settings.get("device", "cpu")).lower()
        if device_name == "auto":
            device_name = "cuda" if torch.cuda.is_available() else "cpu"
        effective_quantization = "none"
        if device_name == "cpu":
            model = model.eval().cpu()
            if str(settings.get("quantization", "int8")).lower() == "int8":
                try:
                    model = torch.ao.quantization.quantize_dynamic(
                        model, {torch.nn.Linear}, dtype=torch.qint8
                    )
                    effective_quantization = "int8"
                except Exception:
                    # INT8非対応環境でも検出機能自体はfloat CPUで継続する。
                    effective_quantization = "float32_fallback"
            pipeline_device = -1
        elif device_name == "cuda":
            if not torch.cuda.is_available():
                raise RuntimeError("device=cudaですがCUDAを利用できません")
            model = model.eval().to("cuda")
            pipeline_device = 0
        else:
            raise ValueError(f"未対応のdeviceです: {device_name}")
        return (
            pipeline(
                "token-classification",
                model=model,
                tokenizer=tokenizer,
                aggregation_strategy="simple",
                device=pipeline_device,
            ),
            effective_quantization,
        )

    @staticmethod
    def _adapt_output(text: str, raw: Any, threshold: float) -> List[Detection]:
        detections: List[Detection] = []
        for item in raw or []:
            if str(item.get("entity_group", "")).upper() != "RISK":
                continue
            start, end = int(item.get("start", -1)), int(item.get("end", -1))
            score = float(item.get("score", 0.0))
            if score < threshold or start < 0 or end > len(text) or start >= end:
                continue
            detections.append(Detection(text[start:end], start, end, score))
        return sorted(detections, key=lambda value: (value.start, value.end))
