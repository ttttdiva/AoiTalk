"""Deterministic pre-processing for attached images and audio."""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import inspect
import json
import logging
import mimetypes
import os
import subprocess
import tempfile
import threading
import time
import wave
from collections import OrderedDict
from dataclasses import asdict, dataclass, field, replace
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

import httpx
from openai import AsyncOpenAI

from src.llm.multimodal import data_url_to_bytes, normalize_image_payloads, openai_content_parts
from src.llm.conversation_context import normalize_usage
from src.llm.sglang_url import resolve_sglang_base_url
from src.services.agent_team_service import config_get
from src.services.llm_model_catalog import model_supports_vision
from src.services.mage_vl_service import (
    MageVLRecognition,
    ProgressCallback,
    get_mage_vl_service,
)
from src.services.outbound_privacy_service import (
    ExternalProviderBlocked,
    OutboundPrivacyGateway,
    RawMediaBlocked,
    effective_privacy_mode,
    get_privacy_policy_context,
    privacy_config,
)

logger = logging.getLogger(__name__)


def persist_usage_sync(*args: Any, **kwargs: Any) -> bool:
    """Lazy usage persistence keeps this optional media path import-safe."""

    from src.llm.conversation_context import persist_usage_sync as _persist

    return bool(_persist(*args, **kwargs))


MEDIA_RECOGNITION_SYSTEM_PROMPT_VERSION = "2026-08-01-v2"

MEDIA_RECOGNITION_SYSTEM_PROMPT = """あなたは添付メディアの解析専門モデルです。ユーザーの発言と添付ファイルを受け取ります。\n- ユーザーの依頼が添付の処理内容を指定している場合（文字起こし、OCR、要約、構成の説明など）はそれに正確に従ってください。\n- 指定がない・添付と無関係な場合: 画像は詳細な客観的記述と可視テキストの完全な転記、音声は完全な文字起こし（話者の区別付き）、動画は時系列に沿った場面・動作・可視テキストの客観的な要約を返してください。\n- 解析結果のみを返し、ユーザーへの回答・意見・挨拶は書かないでください。回答は別のモデルが行います。"""


@dataclass
class RecognitionResult:
    name: str
    sha256: str
    provider: str
    model: str
    engine: str
    result: str = ""
    duration_ms: int = 0
    error: str = ""
    media_type: str = "image"
    cache_hit: bool = False
    options: dict[str, Any] = field(default_factory=dict)
    load_wait_ms: int = 0
    # Explicit ClipIngest recognition uses fail-soft statuses while the
    # historical callers continue to receive the default ``success`` value.
    status: str = "success"

    def to_metadata(self) -> dict[str, Any]:
        return asdict(self)


class MediaRecognitionService:
    _cache: "OrderedDict[str, RecognitionResult]" = OrderedDict()
    _cache_limit = 64
    _cache_lock = threading.RLock()

    def __init__(self, config: Any, usage_context: Any = None):
        self.config = config
        # Media recognition is invoked before the normal LLM client in some
        # request paths.  Keep an optional client/context so direct provider
        # calls can still carry session/user/project identity into TokenUsage.
        self.usage_context = usage_context
        self._recorded_usage_responses: list[Any] = []
        self._privacy_gateway = OutboundPrivacyGateway(
            config,
        )

    def _sync_privacy_gateway(self, context: Any = None) -> None:
        """Refresh request identity/policy on the long-lived media service.

        ``MediaRecognitionService`` is reused by clip-ingest and websocket
        callers.  The gateway therefore must not keep the identity from the
        first request, otherwise aliases and effective privacy mode could be
        applied to the wrong turn.
        """

        candidate = context if context is not None else self.usage_context
        if candidate is None:
            candidate = self._usage_client()
        if context is None:
            try:
                from src.services.turn_context import get_turn_context

                turn = get_turn_context()
            except Exception:
                turn = None
            if turn is not None and any(
                getattr(turn, field, None)
                for field in ("user_id", "session_id", "project_id")
            ):
                candidate = turn

        def _value(name: str, *aliases: str) -> Any:
            keys = (name, *aliases)
            if isinstance(candidate, Mapping):
                for key in keys:
                    value = candidate.get(key)
                    if value is not None:
                        return value
            for key in keys:
                value = getattr(candidate, key, None)
                if value is not None:
                    return value
            return None

        user_id = _value("user_id", "session_user_id")
        if user_id is None:
            getter = getattr(candidate, "_get_session_user_id", None)
            if callable(getter):
                try:
                    user_id = getter()
                except Exception:
                    user_id = None
        session_id = _value("current_session_id", "session_id")
        previous_identity = (
            self._privacy_gateway.user_id,
            self._privacy_gateway.session_id,
        )
        self._privacy_gateway.user_id = str(user_id or "")
        self._privacy_gateway.session_id = str(session_id or "")
        if previous_identity != (
            self._privacy_gateway.user_id,
            self._privacy_gateway.session_id,
        ):
            # A process-shared service must never restore aliases generated by
            # another user's turn.
            self._privacy_gateway._raw_to_alias.clear()
            self._privacy_gateway._alias_to_raw.clear()

        inherited = get_privacy_policy_context()
        session_policy = _value("session_context", "privacy_context")
        project_metadata = _value("project_metadata", "project_metadata_context")
        if not isinstance(session_policy, Mapping):
            session_policy = inherited.session_context
        if not isinstance(project_metadata, Mapping):
            project_metadata = inherited.project_metadata
        if _value("privacy_mode") is not None:
            session_policy = dict(session_policy or {})
            session_policy["privacy_mode"] = str(_value("privacy_mode") or "")
        self._privacy_gateway.update_policy_context(
            session_context=dict(session_policy or {}),
            project_metadata=dict(project_metadata or {}),
        )
        # ``update_policy_context`` only changes the mode.  Re-read the
        # mutable settings so a test/request that changed raw-media policy or
        # review policy takes effect without reconstructing the service.
        settings = privacy_config(self.config)
        self._privacy_gateway.settings = settings.__class__(
            **{
                **settings.__dict__,
                "mode": effective_privacy_mode(
                    self.config,
                    session_context=self._privacy_gateway.session_context,
                    project_metadata=self._privacy_gateway.project_metadata,
                ),
            }
        )

    def _privacy_gateway_for_request(self, context: Any = None) -> OutboundPrivacyGateway:
        """Snapshot identity/policy for one media request.

        The service instance is shared by websocket/clip-ingest callers.  A
        request-local copy keeps aliases and effective policy stable across
        awaits while another user refreshes the shared baseline gateway.
        """

        self._sync_privacy_gateway(context)
        gateway = copy.copy(self._privacy_gateway)
        gateway._raw_to_alias = dict(self._privacy_gateway._raw_to_alias)
        gateway._alias_to_raw = dict(self._privacy_gateway._alias_to_raw)
        gateway._counters = dict(self._privacy_gateway._counters)
        gateway.audit = []
        return gateway

    def _check_media_privacy(
        self,
        route: Mapping[str, Any] | None,
        *,
        provider_default: str = "",
        gateway: OutboundPrivacyGateway | None = None,
    ) -> None:
        active_gateway = gateway or self._privacy_gateway_for_request()
        route_map = route if isinstance(route, Mapping) else {}
        provider = str(route_map.get("provider") or provider_default or "").strip().lower()
        base_url = self._route_base_url(route_map)
        if (
            active_gateway.mode in {"protected", "local_only"}
            and provider in {"codex-cli", "claude-cli", "antigravity-cli", "grok-cli"}
        ):
            raise ExternalProviderBlocked(
                f"CLI media recognition is blocked in {active_gateway.mode} privacy mode"
            )
        active_gateway.ensure_provider_allowed(provider, base_url=base_url)
        if (
            active_gateway.mode in {"protected", "local_only"}
            and active_gateway.provider_class(provider, base_url) != "local"
            and active_gateway.settings.raw_media_policy == "block"
        ):
            raise RawMediaBlocked("raw media is blocked in protected privacy mode")

    def _media_cache_allowed(self, gateway: OutboundPrivacyGateway) -> bool:
        """Media recognition cache is direct-mode only.

        Protected/local-only results may depend on review decisions or local
        redaction context; reusing them would bypass the current policy.
        ``confirm`` and ``always`` explicitly require a fresh review too.
        """

        return bool(
            gateway.mode == "direct"
            and gateway.settings.raw_media_policy != "confirm"
            and gateway.settings.review_policy != "always"
            and gateway.settings.cache_enabled
        )

    def _protect_media_prompt(
        self,
        user_text: str,
        media: Mapping[str, Any],
        route: Mapping[str, Any] | None,
        *,
        source_kind: str,
        gateway: OutboundPrivacyGateway | None = None,
    ) -> str:
        """Apply the outbound gate immediately before a media provider call.

        Binary content is never redacted into a pretend-safe marker.  The
        gateway blocks it by default and only an injected review callback can
        approve ``raw_media_policy=confirm``.  Text context still receives the
        normal deterministic/semantic redaction and aliases are restored only
        in the local result path.
        """
        route_map = route if isinstance(route, Mapping) else {}
        provider = str(route_map.get("provider") or "").strip().lower()
        base_url = self._route_base_url(route_map)
        active_gateway = gateway or self._privacy_gateway_for_request()
        protected = active_gateway.protect_sync(
            {"text": user_text, "media": media},
            provider=provider,
            base_url=base_url,
            source_kind=source_kind,
        )
        payload = protected.payload
        if isinstance(payload, Mapping):
            return str(payload.get("text") or "")
        return user_text

    def _usage_client(self, context: Any = None) -> Any:
        """Return a persist_usage_sync-compatible context for this request."""

        candidate = context if context is not None else self.usage_context
        if context is not None and candidate is not None and (
            hasattr(candidate, "current_session_id")
            or hasattr(candidate, "current_project_id")
            or callable(getattr(candidate, "_get_session_user_id", None))
        ):
            return candidate

        # Tool/HTTP turns expose identity through the task-local turn context.
        # Use a small proxy instead of attaching ad-hoc fields to Config.
        try:
            from src.services.turn_context import get_turn_context

            turn = get_turn_context()
        except Exception:
            turn = None
        if context is None and turn is not None and any(
            getattr(turn, field, None)
            for field in ("user_id", "session_id", "project_id")
        ):
            candidate = turn
        elif context is None and candidate is not None and (
            hasattr(candidate, "current_session_id")
            or hasattr(candidate, "current_project_id")
            or callable(getattr(candidate, "_get_session_user_id", None))
        ):
            return candidate

        def _value(name: str, default: Any = None) -> Any:
            if isinstance(candidate, Mapping):
                value = candidate.get(name)
                if value is not None:
                    return value
            value = getattr(candidate, name, None)
            if value is not None:
                return value
            return getattr(turn, name, default) if turn is not None else default

        user_id = _value("user_id")
        return SimpleNamespace(
            current_session_id=_value("current_session_id", _value("session_id")),
            current_project_id=_value("current_project_id", _value("project_id")),
            character_name=_value("character_name"),
            _get_session_user_id=lambda: user_id,
        )

    def _mark_usage_recorded(self, response: Any) -> bool:
        """Avoid recording one provider response more than once."""

        try:
            if getattr(response, "_aoitalk_usage_recorded", False):
                return True
            object.__setattr__(response, "_aoitalk_usage_recorded", True)
            return False
        except Exception:
            if any(item is response for item in self._recorded_usage_responses):
                return True
            self._recorded_usage_responses.append(response)
            del self._recorded_usage_responses[:-16]
            return False

    @staticmethod
    def _response_usage(response: Any) -> Any:
        if isinstance(response, Mapping):
            return response.get("usage")
        return getattr(response, "usage", None)

    @staticmethod
    def _response_model(response: Any) -> str | None:
        if isinstance(response, Mapping):
            value = response.get("model")
        else:
            value = getattr(response, "model", None)
        return str(value).strip() if value else None

    def _record_provider_usage(
        self,
        response: Any,
        *,
        provider: str = "",
        model: str = "",
        request_type: str = "vision",
        started: float | None = None,
        latency_ms: int | None = None,
        usage_context: Any = None,
    ) -> bool:
        """Persist provider-confirmed usage, without inventing missing metrics."""

        try:
            raw_usage = self._response_usage(response)
            if raw_usage is None:
                return False
            usage = normalize_usage(
                raw_usage,
                provider=provider,
                resolved_model=self._response_model(response),
            )
            if usage.get("input_tokens") is None and usage.get("output_tokens") is None:
                return False
            if self._mark_usage_recorded(response):
                return False
            persist_usage_sync(
                self._usage_client(usage_context),
                provider=provider,
                model=model or self._response_model(response) or provider,
                usage=usage,
                request_type=request_type,
                latency_ms=(
                    max(0, int(latency_ms))
                    if latency_ms is not None
                    else (
                        max(0, int((time.monotonic() - started) * 1000))
                        if started is not None
                        else 0
                    )
                ),
                is_streaming=False,
            )
            return True
        except Exception:
            # Usage accounting must never make an otherwise successful media
            # recognition fail.
            logger.debug("メディア認識APIのusage記録に失敗しました", exc_info=True)
            return False

    def _record_gemini_usage(
        self,
        response: Any,
        *,
        model: str = "gemini-2.0-flash",
        request_type: str = "vision",
        started: float | None = None,
        latency_ms: int | None = None,
        usage_context: Any = None,
    ) -> bool:
        """Persist Gemini ``usage_metadata`` when the SDK exposes it."""

        metadata = getattr(response, "usage_metadata", None)
        if metadata is None and isinstance(response, Mapping):
            metadata = response.get("usage_metadata")
        if metadata is None:
            return False

        def _field(name: str) -> Any:
            if isinstance(metadata, Mapping):
                return metadata.get(name)
            return getattr(metadata, name, None)

        def _count(name: str) -> int | None:
            value = _field(name)
            try:
                return max(0, int(value)) if value is not None else None
            except (TypeError, ValueError):
                return None

        input_tokens = _count("prompt_token_count")
        output_tokens = _count("candidates_token_count")
        if input_tokens is None and output_tokens is None:
            return False
        if self._mark_usage_recorded(response):
            return False
        cached_tokens = _count("cached_content_token_count") or 0
        reasoning_tokens = _count("thoughts_token_count") or 0
        payload: dict[str, Any] = {
            "input_tokens": input_tokens or 0,
            "output_tokens": output_tokens or 0,
            "cached_tokens": cached_tokens,
            "cache_read_tokens": cached_tokens,
            "reasoning_tokens": reasoning_tokens,
            "cache_provider": "gemini",
            "metrics_source": "gemini.usage_metadata",
        }
        resolved_model = getattr(response, "model_version", None)
        if resolved_model is None and isinstance(response, Mapping):
            resolved_model = response.get("model_version")
        if resolved_model:
            payload["resolved_model"] = str(resolved_model)
        try:
            persist_usage_sync(
                self._usage_client(usage_context),
                provider="gemini",
                model=model or str(resolved_model or "gemini"),
                usage=payload,
                request_type=request_type,
                latency_ms=(
                    max(0, int(latency_ms))
                    if latency_ms is not None
                    else (
                        max(0, int((time.monotonic() - started) * 1000))
                        if started is not None
                        else 0
                    )
                ),
                is_streaming=False,
            )
            return True
        except Exception:
            logger.debug("Geminiメディア認識APIのusage記録に失敗しました", exc_info=True)
            return False

    def _record_cli_usage(
        self,
        backend: Any,
        *,
        provider: str,
        model: str,
        request_type: str,
        started: float | None = None,
        usage_context: Any = None,
    ) -> bool:
        """Persist one direct CLI backend invocation's confirmed usage.\n\n        ``CLIBackendBase.execute_prompt`` accumulates provider usage in its\n        backend and exposes it through ``consume_last_usage``.  Media\n        recognition bypasses ``CLILLMClient._execute_prompt_tracked`` so it\n        must consume and persist that ledger explicitly.  The method is safe\n        for backends without usage support and never estimates tokens.\n        """

        consume = getattr(backend, "consume_last_usage", None)
        if not callable(consume):
            return False
        try:
            raw_usage = consume()
        except Exception:
            logger.debug("CLIメディア認識usageの取得に失敗しました", exc_info=True)
            return False
        if not raw_usage:
            return False
        try:
            usage = normalize_usage(raw_usage, provider=provider)
            if usage.get("input_tokens") is None and usage.get("output_tokens") is None:
                return False
            persist_usage_sync(
                self._usage_client(usage_context),
                provider=provider,
                model=model,
                usage=usage,
                request_type=request_type,
                latency_ms=(
                    max(0, int((time.monotonic() - started) * 1000))
                    if started is not None
                    else 0
                ),
                is_streaming=False,
            )
            return True
        except Exception:
            # Usage telemetry must not change CLI recognition success/failure.
            logger.debug("CLIメディア認識usageの記録に失敗しました", exc_info=True)
            return False

    def _main_route(self) -> dict[str, Any]:
        provider = str(config_get(self.config, "llm_provider", "") or "").strip().lower()
        model = str(config_get(self.config, "llm_model", "") or "").strip()
        return {"provider": provider, "model": model}

    def _class_route(self, route_class: str) -> dict[str, Any]:
        route = dict(config_get(self.config, f"model_routing.classes.{route_class}", {}) or {})
        # Older configurations represented image inheritance as empty provider/model.
        if route.get("inherit") or (
            route_class == "vision"
            and not str(route.get("provider") or "").strip()
            and not str(route.get("model") or "").strip()
        ):
            return {**route, **self._main_route()}
        return route

    def _speech_model(self) -> str:
        engine = str(config_get(self.config, "speech_recognition.current_engine", "whisper") or "whisper")
        return str(config_get(self.config, f"speech_recognition.engines.{engine}.model", engine) or engine)

    def _route_base_url(self, route: Mapping[str, Any] | None) -> str:
        """Resolve a route endpoint for privacy classification.

        ``provider_classification`` intentionally requires an explicit URL for
        OpenAI-compatible local adapters.  Reusing the same defaults as the
        transport prevents local-only mode from treating a loopback endpoint
        as unknown/external merely because ``base_url`` was omitted in the
        route override.
        """

        route_map = route if isinstance(route, Mapping) else {}
        provider = str(route_map.get("provider") or "").strip().lower()
        configured = str(route_map.get("base_url") or "").strip()
        if configured:
            return configured
        if provider == "ollama":
            return str(config_get(self.config, "ollama.base_url", "http://127.0.0.1:11434/v1"))
        if provider == "sglang":
            return resolve_sglang_base_url(self.config)
        if provider == "openai_compatible_local":
            return str(
                config_get(
                    self.config,
                    "openai_compatible_local.base_url",
                    "http://127.0.0.1:8080/v1",
                )
            )
        if provider == "openrouter":
            return str(
                config_get(
                    self.config,
                    "openrouter.base_url",
                    "https://openrouter.ai/api/v1",
                )
            )
        if provider == "deepinfra":
            return str(
                config_get(
                    self.config,
                    "deepinfra.base_url",
                    "https://api.deepinfra.com/v1/openai",
                )
            )
        if provider == "kimi":
            return str(
                config_get(
                    self.config,
                    "kimi.base_url",
                    "https://api.moonshot.ai/v1",
                )
            )
        if provider == "grok":
            return "https://api.x.ai/v1"
        return ""

    async def recognize_images(
        self,
        user_text: str,
        images: list[dict[str, Any]],
    ) -> list[RecognitionResult]:
        gateway = self._privacy_gateway_for_request()
        results: list[RecognitionResult] = []
        for image in normalize_image_payloads(images):
            results.append(
                await self._recognize_one_image(
                    user_text,
                    image,
                    privacy_gateway=gateway,
                )
            )
        return results

    async def recognize_images_with_route(
        self,
        user_text: str,
        images: list[dict[str, Any]],
        route: Mapping[str, Any] | None,
        *,
        skip: bool = False,
        client: Any = None,
    ) -> list[RecognitionResult]:
        """Use an explicitly resolved request-scoped provider/model.\n\n        Clip ingest must not rewrite the process-wide ``vision`` settings.\n        The route is copied and an optional provider client can be supplied by\n        the caller; all capability/provider failures are represented in a\n        per-file result so the attachment workflow remains fail-soft.\n        """

        resolved_route = dict(route or {})
        if client is not None:
            resolved_route["_client"] = client
        provider = str(resolved_route.get("provider") or "").strip().lower()
        model = str(resolved_route.get("model") or "").strip()
        capability = model_supports_vision(provider, model)
        try:
            normalized = normalize_image_payloads(images)
        except Exception as exc:
            return [
                RecognitionResult(
                    name=str(item.get("name") or "image") if isinstance(item, dict) else "image",
                    sha256="",
                    provider=provider,
                    model=model,
                    engine="vision",
                    media_type="image",
                    error=str(exc),
                    status="error",
                )
                for item in (images or [])
            ] or [
                RecognitionResult(
                    name="image",
                    sha256="",
                    provider=provider,
                    model=model,
                    engine="vision",
                    media_type="image",
                    error=str(exc),
                    status="error",
                )
            ]

        results: list[RecognitionResult] = []
        gateway = self._privacy_gateway_for_request()
        for image in normalized:
            name = str(image.get("name") or "image")
            sha = self._media_sha256(image, "image")
            if skip:
                results.append(
                    RecognitionResult(
                        name=name,
                        sha256=sha,
                        provider=provider,
                        model=model,
                        engine="vision",
                        media_type="image",
                        status="skipped_by_user",
                    )
                )
                continue
            if capability is False or not provider or not model:
                results.append(
                    RecognitionResult(
                        name=name,
                        sha256=sha,
                        provider=provider,
                        model=model,
                        engine="vision",
                        media_type="image",
                        error=(
                            "画像入力に対応していないモデルです"
                            if capability is False
                            else "画像認識モデルが未設定です"
                        ),
                        status="unsupported",
                    )
                )
                continue
            try:
                results.append(
                    await self._recognize_one_image(
                        user_text,
                        image,
                        route=resolved_route,
                        client=client,
                        privacy_gateway=gateway,
                    )
                )
            except Exception as exc:  # defensive adapter boundary
                results.append(
                    RecognitionResult(
                        name=name,
                        sha256=sha,
                        provider=provider,
                        model=model,
                        engine="vision",
                        media_type="image",
                        error=str(exc),
                        status="error",
                    )
                )
        return results

    async def recognize_audio(
        self,
        user_text: str,
        audio: dict[str, Any],
    ) -> RecognitionResult:
        started = time.monotonic()
        gateway = self._privacy_gateway_for_request()
        data_url = self._data_url_for_media(audio, "audio")
        name = str(audio.get("name") or "audio")
        audio_class = self._class_route("audio")
        engine = str(audio_class.get("engine") or "speech_recognition").strip()
        provider = str(audio_class.get("provider") or "").strip().lower()
        model = str(audio_class.get("model") or "").strip()
        privacy_error: Exception | None = None
        speech_route: dict[str, Any] = {}
        if engine == "speech_recognition":
            speech_config = config_get(self.config, "speech_recognition", {}) or {}
            speech_engine = str(speech_config.get("current_engine") or "whisper").strip().lower()
            speech_provider = {"gemini": "gemini", "google": "google_speech", "whisper": "speech_recognition", "parakeet": "speech_recognition"}.get(speech_engine, speech_engine or "speech_recognition")
            speech_route = {"provider": speech_provider, "model": str(speech_config.get("model") or ""), "base_url": str(speech_config.get("base_url") or "")}
            if speech_provider != "speech_recognition":
                try:
                    self._check_media_privacy(speech_route, gateway=gateway)
                except Exception as exc:
                    privacy_error = exc
        elif engine != "off":
            try:
                self._check_media_privacy(audio_class, gateway=gateway)
            except Exception as exc:
                privacy_error = exc
        sha = self._media_sha256(audio, "audio")
        cache_key = self._cache_key(
            sha,
            "audio",
            user_text,
            audio_class,
            engine=engine,
            privacy_gateway=gateway,
        )
        cached = (
            self._cache_get(cache_key)
            if privacy_error is None and self._media_cache_allowed(gateway)
            else None
        )
        if cached:
            return replace(
                cached,
                name=name,
                sha256=sha,
                cache_hit=True,
                duration_ms=self._elapsed_ms(started),
            )
        try:
            if privacy_error is not None:
                raise privacy_error
            if engine == "off":
                raise RuntimeError("音声認識枠が無効です")
            if engine == "speech_recognition":
                # ``speech_recognition`` is a router, not a trust boundary:
                # its configured engine may be local Whisper/Parakeet or an
                # external Google/Gemini recognizer.  Resolve that engine
                # before handing raw PCM to the manager so protected and
                # local-only modes cannot accidentally upload media through a
                # cloud STT adapter.
                text = await asyncio.wait_for(
                    asyncio.to_thread(
                        self._recognize_audio_with_stt,
                        data_url,
                        user_text,
                        gateway,
                    )
                    if len(inspect.signature(self._recognize_audio_with_stt).parameters) >= 3
                    else asyncio.to_thread(self._recognize_audio_with_stt, data_url, user_text),
                    timeout=180,
                )
                text = gateway.restore(text or "")
                result = RecognitionResult(
                    name=name,
                    sha256=sha,
                    provider="speech_recognition",
                    model=self._speech_model(),
                    engine=engine,
                    result=text or "",
                    duration_ms=self._elapsed_ms(started),
                    media_type="audio",
                )
            else:
                if provider in {"claude", "grok", "kimi", "codex-cli", "claude-cli", "grok-cli"}:
                    raise RuntimeError(f"音声入力に非対応のプロバイダです: {provider}")
                if provider == "antigravity-cli":
                    helper = self._recognize_cli_media
                    helper_kwargs = (
                        {"privacy_gateway": gateway}
                        if "privacy_gateway" in inspect.signature(helper).parameters
                        else {}
                    )
                    awaitable = helper("audio", user_text, audio, audio_class, **helper_kwargs)
                else:
                    helper = self._recognize_audio_with_llm
                    helper_kwargs = (
                        {"privacy_gateway": gateway}
                        if "privacy_gateway" in inspect.signature(helper).parameters
                        else {}
                    )
                    awaitable = helper(user_text, audio, audio_class, **helper_kwargs)
                text = await asyncio.wait_for(awaitable, timeout=180)
                text = gateway.restore(text or "")
                result = RecognitionResult(
                    name=name,
                    sha256=sha,
                    provider=provider,
                    model=model,
                    engine=engine,
                    result=text or "",
                    duration_ms=self._elapsed_ms(started),
                    media_type="audio",
                )
        except Exception as exc:
            result = RecognitionResult(
                name=name,
                sha256=sha,
                provider=provider or engine,
                model=model,
                engine=engine,
                duration_ms=self._elapsed_ms(started),
                error=str(exc),
                media_type="audio",
            )
        if not result.error and self._media_cache_allowed(gateway):
            self._cache_put(cache_key, result)
        return result

    async def recognize_video(
        self,
        user_text: str,
        video: dict[str, Any],
        progress_callback: ProgressCallback | None = None,
    ) -> RecognitionResult:
        """Recognize one uploaded video through the official Mage-VL adapter."""

        started = time.monotonic()
        gateway = self._privacy_gateway_for_request()
        name = str(video.get("name") or "video")
        route = self._class_route("video")
        mage_settings = self._mage_settings()
        provider = str(route.get("provider") or "mage_vl").strip().lower()
        model = str(route.get("model") or mage_settings.get("model") or "microsoft/Mage-VL").strip()
        engine = "mage_vl"
        sha = self._sha256(f"video:{name}")
        options: dict[str, Any] = {
            "video_backend": str(mage_settings.get("video_backend") or "frames"),
            "codec_engine": str(mage_settings.get("codec_engine") or "traditional"),
            "num_frames": self._int_setting(mage_settings, "num_frames", 32, minimum=1),
            "max_pixels": self._int_setting(mage_settings, "max_pixels", 150_000, minimum=0),
            "max_new_tokens": self._int_setting(
                mage_settings,
                "max_new_tokens",
                256,
                minimum=1,
            ),
        }
        path: Path | None = None
        cleanup: Any = None
        cache_key = ""
        try:
            if str(config_get(self.config, "model_routing.media.video_mode", "auto") or "auto") == "off":
                raise RuntimeError("動画認識枠が無効です")
            self._check_media_privacy(route, provider_default=provider, gateway=gateway)
            if provider != "mage_vl":
                raise RuntimeError(f"動画認識に非対応のプロバイダです: {provider}")
            usage_context = self._usage_client()
            mage_service = get_mage_vl_service(self.config)
            if not mage_service.enabled():
                raise RuntimeError("Mage-VL動画認識が無効です")

            max_bytes = self._int_setting(
                mage_settings,
                "max_video_bytes",
                50 * 1024 * 1024,
                minimum=1,
            )
            path, cleanup = self._materialize_video(video, max_bytes=max_bytes)
            size = path.stat().st_size
            if size > max_bytes:
                raise RuntimeError(
                    f"動画サイズが上限を超えています（{max_bytes} bytes）"
                )
            sha = self._sha256_file(path)
            probe = await asyncio.to_thread(
                mage_service.probe_video,
                path,
            )
            duration = probe.get("duration_seconds")
            if isinstance(duration, (int, float)):
                options["duration_seconds"] = round(float(duration), 3)
            max_duration = self._int_setting(
                mage_settings,
                "max_video_duration_seconds",
                300,
                minimum=0,
            )
            if max_duration and isinstance(duration, (int, float)) and duration > max_duration:
                raise RuntimeError(
                    f"動画の長さが上限を超えています（{max_duration}秒）"
                )

            cache_key = self._cache_key(
                sha,
                "video",
                user_text,
                {
                    **route,
                    "provider": provider,
                    "model": model,
                    "base_url": route.get("base_url") or mage_settings.get("base_url"),
                },
                engine=engine,
                options=options,
                privacy_gateway=gateway,
            )
            cached = self._cache_get(cache_key) if self._media_cache_allowed(gateway) else None
            if cached:
                return replace(
                    cached,
                    name=name,
                    sha256=sha,
                    cache_hit=True,
                    duration_ms=self._elapsed_ms(started),
                )

            mage = get_mage_vl_service(self.config)
            response = await mage.recognize_video(
                path=path,
                question=(
                    f"{MEDIA_RECOGNITION_SYSTEM_PROMPT}\n\n"
                    f"{user_text or 'この動画を時系列に沿って詳しく説明してください。'}"
                ),
                num_frames=int(options["num_frames"]),
                max_pixels=int(options["max_pixels"]),
                max_new_tokens=int(options["max_new_tokens"]),
                video_backend=str(options["video_backend"]),
                progress_callback=progress_callback,
                usage_context=usage_context,
            )
            if isinstance(response, MageVLRecognition):
                text = response.result
                load_wait_ms = response.load_wait_ms
                if response.duration_seconds is not None:
                    options["duration_seconds"] = round(response.duration_seconds, 3)
                options["sampled_frames"] = response.num_frames
            else:
                text = str(getattr(response, "result", response) or "")
                load_wait_ms = int(getattr(response, "load_wait_ms", 0) or 0)
            text = gateway.restore(text or "")
            result = RecognitionResult(
                name=name,
                sha256=sha,
                provider=provider,
                model=model,
                engine=engine,
                result=text.strip(),
                duration_ms=self._elapsed_ms(started),
                media_type="video",
                options=options,
                load_wait_ms=load_wait_ms,
            )
        except Exception as exc:
            result = RecognitionResult(
                name=name,
                sha256=sha,
                provider=provider,
                model=model,
                engine=engine,
                duration_ms=self._elapsed_ms(started),
                error=str(exc),
                media_type="video",
                options=options,
            )
        finally:
            if callable(cleanup):
                cleanup()
        if not result.error and cache_key and self._media_cache_allowed(gateway):
            self._cache_put(cache_key, result)
        return result

    async def _recognize_one_image(
        self,
        user_text: str,
        image: dict[str, Any],
        *,
        route: Mapping[str, Any] | None = None,
        client: Any = None,
        privacy_gateway: OutboundPrivacyGateway | None = None,
    ) -> RecognitionResult:
        started = time.monotonic()
        gateway = privacy_gateway or self._privacy_gateway_for_request()
        name = str(image.get("name") or "image")
        vision = dict(route or self._class_route("vision"))
        explicit_client = client if client is not None else vision.get("_client")
        provider = str(vision.get("provider") or "").strip().lower()
        model = str(vision.get("model") or "").strip()
        sha = self._media_sha256(image, "image")
        # Privacy/provider policy must be evaluated before any cache lookup;
        # a stale result must never bypass a newly restrictive request mode.
        privacy_error: Exception | None = None
        try:
            self._check_media_privacy(vision, gateway=gateway)
        except Exception as exc:
            privacy_error = exc
        cache_key = self._cache_key(
            sha,
            "image",
            user_text,
            vision,
            engine="vision",
            privacy_gateway=gateway,
        )
        cached = (
            self._cache_get(cache_key)
            if privacy_error is None and self._media_cache_allowed(gateway)
            else None
        )
        if cached:
            return replace(
                cached,
                name=name,
                sha256=sha,
                cache_hit=True,
                duration_ms=self._elapsed_ms(started),
            )
        try:
            if privacy_error is not None:
                raise privacy_error
            if not provider or not model:
                raise RuntimeError("画像認識モデルが未設定です")
            if provider in {"codex-cli", "antigravity-cli"}:
                helper = self._recognize_cli_media
                helper_kwargs = (
                    {"privacy_gateway": gateway}
                    if "privacy_gateway" in inspect.signature(helper).parameters
                    else {}
                )
                text = await asyncio.wait_for(
                    helper("image", user_text, image, vision, **helper_kwargs),
                    timeout=60,
                )
            elif provider in {"openai", "openrouter", "deepinfra", "kimi", "grok", "sglang", "openai_compatible_local", "ollama"}:
                text = await asyncio.wait_for(
                    self._recognize_openai_compatible_image(
                        user_text,
                        image,
                        vision,
                        client=explicit_client,
                        privacy_gateway=gateway,
                    ),
                    timeout=60,
                )
            elif provider == "gemini":
                helper = self._recognize_gemini_image
                helper_args = (user_text, image, vision, gateway) if len(inspect.signature(helper).parameters) >= 4 else (user_text, image, vision)
                text = await asyncio.wait_for(
                    asyncio.to_thread(helper, *helper_args),
                    timeout=60,
                )
            elif provider == "claude":
                helper = self._recognize_claude_image
                helper_kwargs = (
                    {"privacy_gateway": gateway}
                    if "privacy_gateway" in inspect.signature(helper).parameters
                    else {}
                )
                text = await asyncio.wait_for(
                    helper(user_text, image, vision, **helper_kwargs),
                    timeout=60,
                )
            else:
                raise RuntimeError(f"画像認識に非対応のプロバイダです: {provider}")
            text = gateway.restore(text or "")
            result = RecognitionResult(
                name=name,
                sha256=sha,
                provider=provider,
                model=model,
                engine="vision",
                result=text or "",
                duration_ms=self._elapsed_ms(started),
                media_type="image",
                status="success",
            )
        except Exception as exc:
            result = RecognitionResult(
                name=name,
                sha256=sha,
                provider=provider,
                model=model,
                engine="vision",
                duration_ms=self._elapsed_ms(started),
                error=str(exc),
                media_type="image",
                status="error",
            )
        if not result.error and self._media_cache_allowed(gateway):
            self._cache_put(cache_key, result)
        return result

    async def _recognize_cli_media(
        self,
        kind: str,
        user_text: str,
        media: dict[str, Any],
        route: dict[str, Any],
        *,
        privacy_gateway: OutboundPrivacyGateway | None = None,
    ) -> str:
        provider = str(route.get("provider") or "").strip().lower()
        model = str(route.get("model") or "").strip()
        # CLI media adapters are never permitted in protected/local-only
        # turns.  Keep this check on the adapter itself as well as on the
        # public routing method so direct/internal callers cannot bypass the
        # privacy boundary by invoking this helper directly.
        gateway = privacy_gateway or self._privacy_gateway_for_request()
        self._check_media_privacy(route, gateway=gateway)
        if provider == "codex-cli":
            from src.llm.cli_backends.codex import CodexCLIBackend
            backend = CodexCLIBackend(model=model)
        elif provider == "antigravity-cli":
            from src.llm.cli_backends.antigravity import AntigravityCLIBackend
            backend = AntigravityCLIBackend(model=model)
        else:
            raise RuntimeError(f"{kind}入力に非対応のCLIプロバイダです: {provider}")
        attachment = (
            backend.prepare_image_attachment(media)
            if kind == "image"
            else getattr(backend, "prepare_audio_attachment", lambda _media: None)(media)
        )
        if not attachment:
            raise RuntimeError(f"{backend.get_provider_name()} は{kind}入力に対応していません")
        suffix, cleanup = attachment
        started = time.monotonic()
        request_type = "vision" if kind == "image" else "stt"
        # The configured model is the requested model.  When routing leaves it
        # blank, recover the backend's explicit/env-selected model if exposed;
        # never infer a model from token counts.
        effective_model = model or str(getattr(backend, "_model", "") or "").strip()
        if not effective_model:
            effective_model = str(
                os.getenv("CODEX_MODEL" if provider == "codex-cli" else "AGY_MODEL")
                or ""
            ).strip()
        try:
            safe_user_text = self._protect_media_prompt(
                user_text,
                media,
                route,
                source_kind=f"media_{kind}_cli",
                gateway=gateway,
            )
            prompt = (
                f"{MEDIA_RECOGNITION_SYSTEM_PROMPT}\n\n"
                f"{safe_user_text or ('添付画像を解析してください。' if kind == 'image' else '添付音声を文字起こししてください。')}"
                f"{suffix}"
            )
            success, output = await asyncio.to_thread(backend.execute_prompt, prompt)
        finally:
            try:
                cleanup()
            finally:
                self._record_cli_usage(
                    backend,
                    provider=provider,
                    model=effective_model,
                    request_type=request_type,
                    started=started,
                    usage_context=self._usage_client(),
                )
        if not success or not str(output or "").strip():
            raise RuntimeError(str(output or f"{backend.get_provider_name()} returned no output"))
        return str(output).strip()

    def _build_openai_image_messages(self, user_text: str, image: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {"role": "system", "content": MEDIA_RECOGNITION_SYSTEM_PROMPT},
            {"role": "user", "content": openai_content_parts(user_text or "添付画像を解析してください。", [image])},
        ]

    def _build_openai_audio_messages(self, user_text: str, audio: dict[str, Any]) -> list[dict[str, Any]]:
        data_url = str(audio.get("data") or audio.get("dataUrl") or "")
        mime_type, audio_bytes = data_url_to_bytes(data_url)
        encoded = data_url.split(",", 1)[1]
        return [
            {"role": "system", "content": MEDIA_RECOGNITION_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text or "添付音声を文字起こししてください。"},
                    {
                        "type": "input_audio",
                        "input_audio": {
                            "data": encoded,
                            "format": self._audio_format(mime_type, audio_bytes),
                        },
                    },
                ],
            },
        ]

    async def _recognize_openai_compatible_image(
        self,
        user_text: str,
        image: dict[str, Any],
        route: dict[str, Any],
        *,
        client: Any = None,
        privacy_gateway: OutboundPrivacyGateway | None = None,
    ) -> str:
        started = time.monotonic()
        gateway = privacy_gateway or self._privacy_gateway_for_request()
        client = self._resolve_async_chat_client(
            client or route.get("_client")
        ) or self._openai_client_for_route(route)
        kwargs: dict[str, Any] = dict(
            model=str(route.get("model")),
            messages=self._build_openai_image_messages(user_text, image),
        )
        route_provider = str(route.get("provider") or "").lower()
        if route_provider == "kimi" and str(route.get("model")) == "kimi-k3":
            kwargs["reasoning_effort"] = "max"
            kwargs["max_completion_tokens"] = 1600
        elif route_provider == "deepinfra":
            effort = str(
                route.get("reasoning_effort")
                or config_get(self.config, "deepinfra.reasoning_effort", "high")
                or "high"
            ).strip().lower()
            if effort not in {"none", "low", "medium", "high"}:
                effort = "high"
            kwargs["extra_body"] = {"reasoning_effort": effort}
            kwargs["max_tokens"] = 1600
        elif route_provider == "openrouter":
            # OpenRouter only guarantees detailed accounting when explicitly
            # requested.  Preserve any caller-provided extra body fields.
            extra_body = kwargs.setdefault("extra_body", {})
            if isinstance(extra_body, dict):
                extra_body.setdefault("usage", {"include": True})
            kwargs["temperature"] = 0
            kwargs["max_tokens"] = 1600
        else:
            kwargs["temperature"] = 0
            kwargs["max_tokens"] = 1600
        protected = await gateway.protect(
            kwargs,
            provider=route_provider,
            base_url=self._route_base_url(route),
            source_kind="media_image",
        )
        if isinstance(protected.payload, Mapping):
            kwargs = dict(protected.payload)
        response = await self._create_chat_completion_with_fallback(client, kwargs)
        self._record_provider_usage(
            response,
            provider=route_provider,
            model=str(route.get("model")),
            request_type="vision",
            started=started,
        )
        return str(response.choices[0].message.content or "")

    @staticmethod
    def _resolve_async_chat_client(candidate: Any) -> Any:
        """Extract an async OpenAI-compatible client from runtime wrappers.\n\n        The request-scoped clip client is often an ``AgentLLMClient`` (which\n        exposes a synchronous ``chat`` convenience method) rather than the\n        low-level ``AsyncOpenAI`` object used by media recognition.  Reusing\n        that wrapper as-is would produce ``chat.completions`` attribute errors\n        and silently turn every otherwise vision-capable ingest into an error.\n        Prefer its private async transport when available; reject sync\n        transports and let the route factory create the proper async client.\n        """

        def is_async_transport(value: Any) -> bool:
            if isinstance(value, AsyncOpenAI):
                return True
            try:
                create = value.chat.completions.create
            except Exception:
                return False
            if inspect.iscoroutinefunction(create):
                return True
            # The OpenAI SDK's generated ``AsyncCompletions.create`` method
            # is a regular wrapper (it returns an awaitable), so
            # ``inspect.iscoroutinefunction`` is false despite async I/O.
            class_name = type(value).__name__.lower()
            module_name = type(value).__module__.lower()
            return "async" in class_name and "openai" in module_name

        if candidate is None:
            return None
        if is_async_transport(candidate):
            return candidate
        for attr in ("_openai_client", "_async_client", "async_client"):
            nested = getattr(candidate, attr, None)
            if is_async_transport(nested):
                return nested
        return None

    @staticmethod
    async def _create_chat_completion_with_fallback(client: AsyncOpenAI, kwargs: dict[str, Any]):
        """chat.completions を叩き、reasoning 系モデルが拒否する sampling パラメータを\n        段階的に外して再試行する。\n\n        gpt-5 系などの reasoning モデルは ``max_tokens`` / ``temperature`` を拒否し\n        ``max_completion_tokens`` を要求する。呼び出し側が旧パラメータで組んでいても\n        認識が丸ごと失敗しないよう、既存の generate 経路と同じ「拒否されたら外して\n        再試行」戦略でフォールバックする。\n        """
        try:
            return await client.chat.completions.create(**kwargs)
        except Exception:
            # 1) max_tokens -> max_completion_tokens へ振り替え、temperature と
            #    extra_body(reasoning_effort) を外す。extra_body を残すと、
            #    reasoning 非対応モデルでは1回目と同じ理由で必ず失敗し、
            #    毎回3リクエスト消費してから成功することになる。
            retry = {
                key: value
                for key, value in kwargs.items()
                if key not in ("max_tokens", "temperature", "extra_body")
            }
            if "max_tokens" in kwargs and "max_completion_tokens" not in retry:
                retry["max_completion_tokens"] = kwargs["max_tokens"]
            # OpenRouter's usage.include is independent from provider-specific
            # reasoning options; keep it on a retry so accounting is not lost
            # when a model rejects temperature/max_tokens.
            extra_body = kwargs.get("extra_body")
            if isinstance(extra_body, dict) and "usage" in extra_body:
                retry["extra_body"] = {"usage": extra_body["usage"]}
            try:
                return await client.chat.completions.create(**retry)
            except Exception:
                # 2) それでも駄目なら sampling / 出力上限を全て外して素で叩く。
                bare = {"model": kwargs.get("model"), "messages": kwargs.get("messages")}
                if isinstance(extra_body, dict) and "usage" in extra_body:
                    bare["extra_body"] = {"usage": extra_body["usage"]}
                return await client.chat.completions.create(**bare)

    async def _recognize_audio_with_llm(
        self,
        user_text: str,
        audio: dict[str, Any],
        route: dict[str, Any],
        *,
        privacy_gateway: OutboundPrivacyGateway | None = None,
    ) -> str:
        started = time.monotonic()
        gateway = privacy_gateway or self._privacy_gateway_for_request()
        provider = str(route.get("provider") or "").strip().lower()
        self._check_media_privacy(route, gateway=gateway)
        if provider == "gemini":
            helper = self._recognize_gemini_audio
            helper_args = (user_text, audio, route, gateway) if len(inspect.signature(helper).parameters) >= 4 else (user_text, audio, route)
            return await asyncio.to_thread(helper, *helper_args)
        client = self._openai_client_for_route(route)
        kwargs: dict[str, Any] = dict(
            model=str(route.get("model")),
            messages=self._build_openai_audio_messages(user_text, audio),
            temperature=0,
            max_tokens=2400,
        )
        protected = await gateway.protect(
            kwargs,
            provider=provider,
            base_url=self._route_base_url(route),
            source_kind="media_audio",
        )
        if isinstance(protected.payload, Mapping):
            kwargs = dict(protected.payload)
        response = await self._create_chat_completion_with_fallback(
            client,
            kwargs,
        )
        self._record_provider_usage(
            response,
            provider=provider,
            model=str(route.get("model")),
            request_type="stt",
            started=started,
        )
        return str(response.choices[0].message.content or "")

    def _recognize_gemini_image(self, user_text: str, image: dict[str, Any], route: dict[str, Any], privacy_gateway: OutboundPrivacyGateway | None = None) -> str:
        started = time.monotonic()
        gateway = privacy_gateway or self._privacy_gateway_for_request()
        self._check_media_privacy(route, gateway=gateway)
        import google.generativeai as genai
        from google.generativeai import protos

        api_key = str(route.get("api_key") or config_get(self.config, "gemini_api_key", "") or "")
        if api_key:
            genai.configure(api_key=api_key)
        mime_type, image_bytes = data_url_to_bytes(str(image.get("data") or ""))
        model = genai.GenerativeModel(str(route.get("model")))
        protected = gateway.protect_sync(
            {"text": user_text, "media": image_bytes},
            provider="gemini",
            source_kind="media_image",
        )
        safe_user_text = user_text
        if isinstance(protected.payload, Mapping):
            safe_user_text = str(protected.payload.get("text") or user_text)
        response = model.generate_content(
            [
                MEDIA_RECOGNITION_SYSTEM_PROMPT,
                safe_user_text or "添付画像を解析してください。",
                protos.Part(inline_data=protos.Blob(mime_type=mime_type, data=image_bytes)),
            ]
        )
        self._record_gemini_usage(
            response,
            model=str(route.get("model")),
            request_type="vision",
            started=started,
        )
        return str(getattr(response, "text", "") or "").strip()

    def _recognize_gemini_audio(self, user_text: str, audio: dict[str, Any], route: dict[str, Any], privacy_gateway: OutboundPrivacyGateway | None = None) -> str:
        started = time.monotonic()
        gateway = privacy_gateway or self._privacy_gateway_for_request()
        self._check_media_privacy(route, gateway=gateway)
        import tempfile
        import google.generativeai as genai

        api_key = str(route.get("api_key") or config_get(self.config, "gemini_api_key", "") or "")
        if api_key:
            genai.configure(api_key=api_key)
        mime_type, audio_bytes = data_url_to_bytes(str(audio.get("data") or ""))
        suffix = "." + self._audio_format(mime_type, audio_bytes)
        protected = gateway.protect_sync(
            {"text": user_text, "media": audio_bytes},
            provider="gemini",
            source_kind="media_audio",
        )
        safe_user_text = user_text
        if isinstance(protected.payload, Mapping):
            safe_user_text = str(protected.payload.get("text") or user_text)
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=True) as fp:
            fp.write(audio_bytes)
            fp.flush()
            uploaded = genai.upload_file(path=fp.name)
            model = genai.GenerativeModel(str(route.get("model")))
            response = model.generate_content(
                [MEDIA_RECOGNITION_SYSTEM_PROMPT, safe_user_text or "添付音声を文字起こししてください。", uploaded]
            )
        self._record_gemini_usage(
            response,
            model=str(route.get("model")),
            request_type="stt",
            started=started,
        )
        return str(getattr(response, "text", "") or "").strip()

    async def _recognize_claude_image(self, user_text: str, image: dict[str, Any], route: dict[str, Any], *, privacy_gateway: OutboundPrivacyGateway | None = None) -> str:
        started = time.monotonic()
        gateway = privacy_gateway or self._privacy_gateway_for_request()
        self._check_media_privacy(route, gateway=gateway)
        api_key = str(route.get("api_key") or config_get(self.config, "anthropic_api_key", "") or "")
        if not api_key:
            raise RuntimeError("Anthropic API key is not configured")
        mime_type, image_bytes = data_url_to_bytes(str(image.get("data") or ""))
        import base64

        protected = gateway.protect_sync(
            {"text": user_text, "media": image_bytes},
            provider="claude",
            source_kind="media_image",
        )
        safe_user_text = user_text
        if isinstance(protected.payload, Mapping):
            safe_user_text = str(protected.payload.get("text") or user_text)

        payload = {
            "model": str(route.get("model")),
            "max_tokens": 1600,
            "system": MEDIA_RECOGNITION_SYSTEM_PROMPT,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": safe_user_text or "添付画像を解析してください。"},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": mime_type,
                                "data": base64.b64encode(image_bytes).decode("ascii"),
                            },
                        },
                    ],
                }
            ],
        }
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
        self._record_provider_usage(
            data,
            provider="claude",
            model=str(route.get("model")),
            request_type="vision",
            started=started,
        )
        return "\n".join(
            str(part.get("text") or "")
            for part in data.get("content", [])
            if isinstance(part, dict)
        ).strip()

    def _recognize_audio_with_stt(self, data_url: str, user_text: str, privacy_gateway: OutboundPrivacyGateway | None = None) -> str:
        gateway = privacy_gateway or self._privacy_gateway_for_request()
        mime_type, audio_bytes = data_url_to_bytes(data_url)
        frames, sample_rate, channels, sample_width = self._decode_audio_for_stt(
            mime_type,
            audio_bytes,
        )
        from src.audio.manager import SpeechRecognitionManager

        speech_config = config_get(self.config, "speech_recognition", {}) or {}
        engine_name = str(speech_config.get("current_engine") or "whisper")
        engine_settings = speech_config.get("engines", {}).get(engine_name, {})
        if not isinstance(engine_settings, Mapping):
            engine_settings = {}
        normalized_engine = engine_name.strip().lower()
        provider = (
            "gemini"
            if normalized_engine == "gemini"
            else "google"
            if normalized_engine == "google"
            else "speech_recognition"
        )
        safe_user_text = user_text
        if provider != "speech_recognition":
            route = {
                "provider": provider,
                "model": str(
                    engine_settings.get("model")
                    or speech_config.get("model")
                    or ""
                ),
                "base_url": str(engine_settings.get("base_url") or speech_config.get("base_url") or ""),
            }
            self._check_media_privacy(route, gateway=gateway)
            safe_user_text = self._protect_media_prompt(
                user_text,
                {"data": data_url},
                route,
                source_kind="media_audio_stt",
                gateway=gateway,
            )
        manager = SpeechRecognitionManager(engine_name, speech_config)
        recognize_kwargs = {
            "sample_rate": sample_rate,
            "channels": channels,
            "sample_width": sample_width,
            "prompt": safe_user_text or None,
            "usage_client": self._usage_client(),
        }
        try:
            return manager.recognize(frames, **recognize_kwargs) or ""
        except TypeError as exc:
            # Keep compatibility with third-party/test managers that predate
            # the optional usage_client argument; the in-tree manager accepts
            # it and records provider-confirmed Gemini STT usage.
            if "usage_client" not in str(exc):
                raise
            recognize_kwargs.pop("usage_client", None)
            return manager.recognize(frames, **recognize_kwargs) or ""

    def _decode_audio_for_stt(self, mime_type: str, audio_bytes: bytes) -> tuple[bytes, int, int, int]:
        """Decode uploaded audio to 16 kHz mono PCM for existing STT engines."""
        suffix = "." + self._audio_format(mime_type, audio_bytes)
        try:
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as fp:
                fp.write(audio_bytes)
                temp_path = fp.name
            try:
                completed = subprocess.run(
                    [
                        "ffmpeg",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-i",
                        temp_path,
                        "-f",
                        "s16le",
                        "-acodec",
                        "pcm_s16le",
                        "-ac",
                        "1",
                        "-ar",
                        "16000",
                        "pipe:1",
                    ],
                    check=True,
                    capture_output=True,
                )
                if completed.stdout:
                    return completed.stdout, 16000, 1, 2
            finally:
                import os

                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
        except (FileNotFoundError, subprocess.CalledProcessError):
            if "wav" not in str(mime_type or "").lower():
                raise RuntimeError("ffmpegで添付音声をデコードできませんでした")

        with wave.open(BytesIO(audio_bytes), "rb") as wav:
            return (
                wav.readframes(wav.getnframes()),
                wav.getframerate(),
                wav.getnchannels(),
                wav.getsampwidth(),
            )

    def _openai_client_for_route(self, route: dict[str, Any]) -> AsyncOpenAI:
        provider = str(route.get("provider") or "").strip().lower()
        base_url = str(route.get("base_url") or "").strip()
        api_key = str(route.get("api_key") or "").strip()
        if provider == "openrouter":
            base_url = base_url or str(config_get(self.config, "openrouter.base_url", "https://openrouter.ai/api/v1"))
            api_key = api_key or str(config_get(self.config, "openrouter_api_key", "") or "")
        elif provider == "deepinfra":
            base_url = base_url or str(
                config_get(self.config, "deepinfra.base_url", "")
                or config_get(self.config, "deepinfra_base_url", "")
                or os.getenv("DEEPINFRA_BASE_URL")
                or "https://api.deepinfra.com/v1/openai"
            )
            api_key = api_key or str(
                config_get(self.config, "deepinfra_api_key", "")
                or os.getenv("DEEPINFRA_TOKEN", "")
            )
        elif provider == "kimi":
            base_url = base_url or str(
                config_get(self.config, "kimi_base_url", "")
                or os.getenv("MOONSHOT_BASE_URL")
                or config_get(self.config, "kimi.base_url", "https://api.moonshot.ai/v1")
            )
            api_key = api_key or str(config_get(self.config, "kimi_api_key", "") or os.getenv("MOONSHOT_API_KEY", ""))
        elif provider == "grok":
            base_url = base_url or "https://api.x.ai/v1"
            api_key = api_key or str(config_get(self.config, "xai_api_key", "") or "")
        elif provider == "ollama":
            base_url = base_url or str(config_get(self.config, "ollama.base_url", "http://127.0.0.1:11434/v1"))
            api_key = api_key or str(config_get(self.config, "ollama.api_key", "ollama"))
        elif provider == "sglang":
            base_url = base_url or resolve_sglang_base_url(self.config)
            api_key = api_key or "dummy"
        elif provider == "openai_compatible_local":
            base_url = base_url or str(config_get(self.config, "openai_compatible_local.base_url", "http://127.0.0.1:8080/v1"))
            api_key = api_key or str(config_get(self.config, "openai_compatible_local.api_key", "dummy"))
        else:
            api_key = api_key or str(config_get(self.config, "openai_api_key", "") or "")
        return AsyncOpenAI(api_key=api_key or "dummy", base_url=base_url or None)

    def _mage_settings(self) -> dict[str, Any]:
        raw = config_get(self.config, "mage_vl", {}) or {}
        return dict(raw) if isinstance(raw, dict) else {}

    @staticmethod
    def _int_setting(
        settings: dict[str, Any],
        key: str,
        default: int,
        *,
        minimum: int,
    ) -> int:
        try:
            value = int(settings.get(key, default))
        except (TypeError, ValueError):
            value = default
        return max(minimum, value)

    def _data_url_for_media(self, media: dict[str, Any], media_type: str) -> str:
        data_url = str(media.get("data") or media.get("dataUrl") or "")
        if data_url:
            return data_url
        raw_path = str(media.get("path") or "").strip()
        if not raw_path:
            return ""
        path = self._resolve_workspace_path(raw_path)
        payload = path.read_bytes()
        mime_type = str(media.get("mimeType") or media.get("mime_type") or "").strip()
        mime_type = mime_type or mimetypes.guess_type(path.name)[0] or f"{media_type}/octet-stream"
        return f"data:{mime_type};base64,{base64.b64encode(payload).decode('ascii')}"

    def _media_sha256(self, media: dict[str, Any], media_type: str) -> str:
        data_url = str(media.get("data") or media.get("dataUrl") or "")
        if data_url:
            try:
                _mime_type, payload = data_url_to_bytes(data_url)
                return self._sha256_bytes(payload)
            except Exception:
                return self._sha256(data_url)
        raw_path = str(media.get("path") or "").strip()
        if raw_path:
            try:
                return self._sha256_bytes(self._resolve_workspace_path(raw_path).read_bytes())
            except Exception:
                return self._sha256(raw_path)
        return self._sha256(f"{media_type}:{media.get('name') or ''}")

    @staticmethod
    def _resolve_workspace_path(raw_path: str) -> Path:
        from src.tools.file_explorer import get_root_dir

        root = get_root_dir().resolve()
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = root / candidate
        resolved = candidate.resolve()
        if resolved != root and root not in resolved.parents:
            raise ValueError("添付ファイルのパスがワークスペース外です")
        return resolved

    def _materialize_video(
        self,
        video: dict[str, Any],
        *,
        max_bytes: int,
    ) -> tuple[Path, Any]:
        raw_path = str(video.get("path") or "").strip()
        if raw_path:
            path = self._resolve_workspace_path(raw_path)
            if not path.is_file():
                raise FileNotFoundError(f"動画ファイルが見つかりません: {raw_path}")
            return path, None

        data_url = str(video.get("data") or video.get("dataUrl") or "")
        if not data_url:
            raise ValueError("動画ファイルのパスまたはデータがありません")
        header, encoded = data_url.split(",", 1) if "," in data_url else ("", "")
        if not encoded:
            raise ValueError("動画のdata URLが不正です")
        estimated_size = (
            (len(encoded.rstrip("=")) * 3) // 4
            if ";base64" in header.lower()
            else len(encoded.encode("utf-8"))
        )
        if estimated_size > max_bytes:
            raise RuntimeError(
                f"動画サイズが上限を超えています（{max_bytes} bytes）"
            )
        mime_type, payload = data_url_to_bytes(data_url)
        if len(payload) > max_bytes:
            raise RuntimeError(
                f"動画サイズが上限を超えています（{max_bytes} bytes）"
            )
        name = str(video.get("name") or "video")
        suffix = Path(name).suffix or mimetypes.guess_extension(mime_type) or ".mp4"
        with tempfile.NamedTemporaryFile(prefix="aoitalk-mage-vl-", suffix=suffix, delete=False) as fp:
            fp.write(payload)
            path = Path(fp.name)

        def cleanup() -> None:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass

        return path, cleanup

    def _cache_key(
        self,
        sha: str,
        media_type: str,
        user_text: str,
        route: dict[str, Any],
        *,
        engine: str,
        options: dict[str, Any] | None = None,
        privacy_gateway: OutboundPrivacyGateway | None = None,
    ) -> str:
        route_fingerprint = {
            str(key): value
            for key, value in route.items()
            if str(key).lower() not in {"api_key", "apikey", "token", "secret"}
        }
        payload = {
            "prompt_version": MEDIA_RECOGNITION_SYSTEM_PROMPT_VERSION,
            "sha256": sha,
            "media_type": media_type,
            "instruction": user_text or "",
            "engine": engine,
            "route": route_fingerprint,
            "options": options or {},
        }
        if privacy_gateway is not None:
            payload["privacy"] = {
                "user_id": str(privacy_gateway.user_id or ""),
                "session_id": str(privacy_gateway.session_id or ""),
                "mode": str(privacy_gateway.mode or ""),
                "raw_media_policy": str(privacy_gateway.settings.raw_media_policy or ""),
                "review_policy": str(privacy_gateway.settings.review_policy or ""),
            }
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return self._sha256(serialized)

    @classmethod
    def _cache_get(cls, key: str) -> RecognitionResult | None:
        with cls._cache_lock:
            result = cls._cache.get(key)
            if result:
                cls._cache.move_to_end(key)
            return result

    @classmethod
    def _cache_put(cls, key: str, result: RecognitionResult) -> None:
        with cls._cache_lock:
            cls._cache[key] = result
            cls._cache.move_to_end(key)
            while len(cls._cache) > cls._cache_limit:
                cls._cache.popitem(last=False)

    @staticmethod
    def _sha256(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _sha256_bytes(value: bytes) -> str:
        return hashlib.sha256(value).hexdigest()

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return int((time.monotonic() - started) * 1000)

    @staticmethod
    def _audio_format(mime_type: str, _audio_bytes: bytes) -> str:
        lowered = str(mime_type or "").lower()
        if "mpeg" in lowered or "mp3" in lowered:
            return "mp3"
        if "wav" in lowered:
            return "wav"
        if "webm" in lowered:
            return "webm"
        if "ogg" in lowered:
            return "ogg"
        if "flac" in lowered:
            return "flac"
        if "m4a" in lowered or "mp4" in lowered:
            return "m4a"
        return "wav"
