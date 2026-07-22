"""TTSManagerから呼ぶ共通テキストプリフライト。"""

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

from .adapters import VoicevoxCompatibleDictionaryAdapter
from .repository import YomiRepository
from .runtime import YomiLinterRuntime, contains_japanese
from .types import PreflightResult, TTSYomiPolicy

logger = logging.getLogger(__name__)

DEFAULT_MODEL_ID = "ayousanz/yomi-linter-modernbert-ja-130m"
DEFAULT_POLICIES = {
    "voicevox": TTSYomiPolicy.DICTIONARY,
    "aivisspeech": TTSYomiPolicy.DICTIONARY,
    "irodori_tts": TTSYomiPolicy.DETECT_ONLY,
    "miotts": TTSYomiPolicy.DETECT_ONLY,
    "voiceroid": TTSYomiPolicy.DETECT_ONLY,
    "aivoice": TTSYomiPolicy.DETECT_ONLY,
    "cevio": TTSYomiPolicy.DETECT_ONLY,
    "nijivoice": TTSYomiPolicy.DETECT_ONLY,
}


def normalize_settings(config: Dict[str, Any]) -> Dict[str, Any]:
    raw = (config.get("tts", {}) or {}).get("yomi_linter", {}) or {}
    return {
        "enabled": bool(raw.get("enabled", False)),
        "model_id": str(raw.get("model_id") or DEFAULT_MODEL_ID),
        "device": str(raw.get("device") or "cpu"),
        "quantization": str(raw.get("quantization") or "int8"),
        "confidence_threshold": max(0.0, min(1.0, float(raw.get("confidence_threshold", 0.5)))),
        "log_detections": bool(raw.get("log_detections", True)),
        "cache_dir": raw.get("cache_dir"),
        "revision": raw.get("revision") or "main",
        "local_files_only": bool(raw.get("local_files_only", False)),
        "policies": raw.get("policies", {}) or {},
    }


class YomiPreflightService:
    def __init__(
        self,
        runtime: Optional[YomiLinterRuntime] = None,
        repository: Optional[YomiRepository] = None,
    ) -> None:
        self.runtime = runtime or YomiLinterRuntime()
        self.repository = repository or YomiRepository()
        self.dictionary_adapter = VoicevoxCompatibleDictionaryAdapter(self.repository)

    def status(self, config: Dict[str, Any]) -> Dict[str, Any]:
        settings = normalize_settings(config)
        return {
            "enabled": settings["enabled"],
            "model_id": settings["model_id"],
            "device": settings["device"],
            "quantization": settings["quantization"],
            "effective_quantization": self.runtime.effective_quantization,
            "model_loaded": self.runtime.state == "ready",
            "download_status": self.runtime.state,
            "error": self.runtime.last_error,
            "restart_required": False,
        }

    async def process(
        self,
        text: str,
        *,
        engine_name: str,
        engine: Any,
        config: Dict[str, Any],
    ) -> PreflightResult:
        settings = normalize_settings(config)
        policy = self._policy(engine_name, settings)
        empty = PreflightResult(
            original_text=text,
            detections=[],
            model_id=settings["model_id"],
            tts_engine=engine_name,
            dictionary_applied=False,
            final_text=text,
            policy=policy,
        )
        if not settings["enabled"] or policy == TTSYomiPolicy.DISABLED:
            if engine_name in {"voicevox", "aivisspeech"}:
                try:
                    await self.dictionary_adapter.cleanup_owned(
                        engine, engine_name=engine_name
                    )
                except Exception as exc:
                    logger.warning("無効化したTTS辞書の後片付けに失敗しました: %s", exc)
            return empty
        if not contains_japanese(text):
            return empty
        detections = []
        try:
            detections = await self.runtime.detect(text, settings)
        except Exception as exc:
            logger.warning(
                "Yomi Linter検出に失敗したため原文でTTSを継続します: engine=%s error=%s",
                engine_name,
                exc,
            )
        try:
            entries = await asyncio.to_thread(self.repository.list_dictionary, True)
        except Exception as exc:
            logger.warning("共通読み辞書を取得できないため検出のみ継続します: %s", exc)
            entries = []
        target_entries = [
            entry for entry in entries
            if self._targets_engine(entry.get("target_tts"), engine_name)
        ]
        applicable = [entry for entry in target_entries if entry["surface"] in text]
        dictionary_applied = False
        if policy == TTSYomiPolicy.DICTIONARY:
            try:
                dictionary_applied = await self.dictionary_adapter.apply(
                    engine,
                    target_entries,
                    engine_name=engine_name,
                    applicable_entry_ids={str(entry["id"]) for entry in applicable},
                )
            except Exception as exc:
                logger.warning(
                    "TTSユーザー辞書へ反映できないため原文で継続します: engine=%s error=%s",
                    engine_name,
                    exc,
                )
        result = PreflightResult(
            original_text=text,
            detections=detections,
            model_id=settings["model_id"],
            tts_engine=engine_name,
            dictionary_applied=dictionary_applied,
            final_text=text,
            policy=policy,
        )
        if detections:
            surfaces = {entry["surface"] for entry in applicable}
            try:
                await asyncio.to_thread(
                    self.repository.record_candidates,
                    original_text=text,
                    detections=detections,
                    model_id=settings["model_id"],
                    tts_engine=engine_name,
                    final_text=text,
                    dictionary_surfaces=surfaces,
                    dictionary_applied=dictionary_applied,
                )
            except Exception as exc:
                logger.warning("未解決の誤読候補を保存できませんでした: %s", exc)
            if settings["log_detections"]:
                logger.info(
                    "tts_yomi_detection %s",
                    json.dumps(result.to_dict(), ensure_ascii=False),
                )
        return result

    @staticmethod
    def _targets_engine(targets: Any, engine_name: str) -> bool:
        values = [str(value).lower() for value in (targets or [])]
        return not values or "all" in values or engine_name.lower() in values

    @staticmethod
    def _policy(engine_name: str, settings: Dict[str, Any]) -> TTSYomiPolicy:
        raw = settings.get("policies", {}).get(engine_name)
        if raw:
            try:
                policy = TTSYomiPolicy(raw)
                # 自動テキスト書換えは初期実装で許可しない。
                return TTSYomiPolicy.DETECT_ONLY if policy == TTSYomiPolicy.TEXT_REWRITE else policy
            except ValueError:
                pass
        return DEFAULT_POLICIES.get(engine_name, TTSYomiPolicy.DETECT_ONLY)


_service = YomiPreflightService()


def get_yomi_preflight_service() -> YomiPreflightService:
    return _service
