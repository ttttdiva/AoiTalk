"""Utility helpers for Nanobanana Pro slash command"""
from __future__ import annotations

import base64
import logging
import os
import re
import textwrap
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

from openai import OpenAI
from ...services.outbound_privacy_service import (
    OutboundPrivacyGateway,
    PrivacyError,
    get_privacy_policy_context,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NanobananaUsageContext:
    """Immutable identity for one Nanobanana slash-command request."""

    user_id: Optional[str]
    current_session_id: Optional[str]
    current_project_id: Any = None
    character_name: Optional[str] = None
    guild_id: Optional[str] = None
    # Durable ConversationSession identity is kept as metadata for callers;
    # ``current_session_id`` remains the runtime/usage UUID used by token
    # accounting so it cannot be confused with the DB conversation row.
    conversation_id: Optional[str] = None
    # Provider policy is request-scoped just like the usage identity.  Discord
    # callers may leave these unset when the surrounding turn has already
    # bound the effective policy through the privacy ContextVar.
    config: Any = None
    session_context: Optional[Mapping[str, Any]] = None
    project_metadata: Optional[Mapping[str, Any]] = None

    def _get_session_user_id(self) -> Optional[str]:
        return self.user_id

    @classmethod
    def from_discord(
        cls,
        user_id: Any,
        guild_id: Any,
        session_id: Any = None,
        *,
        character_name: Optional[str] = None,
        conversation_id: Any = None,
        config: Any = None,
        session_context: Optional[Mapping[str, Any]] = None,
        project_metadata: Optional[Mapping[str, Any]] = None,
    ) -> "NanobananaUsageContext":
        user = str(user_id) if user_id is not None else None
        guild = str(guild_id) if guild_id is not None else None
        canonical_user = None
        if user:
            canonical_user = f"discord:{guild}:{user}" if guild else f"discord:{user}"
        resolved_session = session_id
        if resolved_session is None and user:
            resolved_session = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"aoitalk:discord:{user}:{guild if guild else 'dm'}",
                )
            )
        return cls(
            user_id=canonical_user,
            current_session_id=(str(resolved_session) if resolved_session else None),
            character_name=character_name,
            guild_id=guild,
            conversation_id=(str(conversation_id) if conversation_id else None),
            config=config,
            session_context=(
                dict(session_context) if isinstance(session_context, Mapping) else None
            ),
            project_metadata=(
                dict(project_metadata)
                if isinstance(project_metadata, Mapping)
                else None
            ),
        )

def normalize_usage(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Lazy import keeps bot utility imports safe before the LLM package loads."""
    from ...llm.conversation_context import normalize_usage as _normalize

    return _normalize(*args, **kwargs)


def persist_usage_sync(*args: Any, **kwargs: Any) -> bool:
    """Lazy import for the optional token usage database integration."""
    from ...llm.conversation_context import persist_usage_sync as _persist

    return bool(_persist(*args, **kwargs))


def _turn_usage_client() -> Any:
    """Build a lightweight usage context from the active assistant turn."""
    try:
        from types import SimpleNamespace

        from ...services.turn_context import get_turn_context

        turn = get_turn_context()
        return SimpleNamespace(
            current_session_id=turn.session_id,
            current_project_id=turn.project_id,
            character_name=None,
            _get_session_user_id=lambda: turn.user_id,
        )
    except Exception:
        return None


class NanobananaProService:
    """Search and image generation helper"""

    SEARCH_QUERY = "Nanobanana Pro 最新情報 仕様 機能 2025"

    def __init__(self, config: Any = None) -> None:
        # Keep the service stateless per request, but retain the application
        # config as an immutable default.  A provider request must never build
        # ``OutboundPrivacyGateway(None)``: that would silently select the
        # process default (currently ``direct``) and bypass project/session
        # policy when a Discord interaction forgot to thread its context.
        self.config = config
        self._client: Optional[OpenAI] = None
        self._recorded_usage_responses: list[Any] = []

    @staticmethod
    def _coerce_usage_context(context: Any) -> Any:
        """Normalize dict/interaction context without mutating shared state."""
        if context is None:
            return None
        if (
            hasattr(context, "current_session_id")
            or hasattr(context, "current_project_id")
            or callable(getattr(context, "_get_session_user_id", None))
        ):
            return context

        def value(name: str, *aliases: str) -> Any:
            if isinstance(context, Mapping):
                for key in (name, *aliases):
                    item = context.get(key)
                    if item is not None:
                        return item
            for key in (name, *aliases):
                item = getattr(context, key, None)
                if item is not None:
                    return item
            return None

        user_id = value("user_id")
        guild_id = value("guild_id")
        session_id = value("current_session_id", "session_id")
        # Dicts supplied by existing integrations often already contain a
        # canonical ``discord:...`` user id; preserve it as-is.
        if user_id is not None and str(user_id).startswith("discord:"):
            canonical_user = str(user_id)
            guild_text = str(guild_id) if guild_id is not None else None
            resolved_session = str(session_id) if session_id else None
            return NanobananaUsageContext(
                user_id=canonical_user,
                current_session_id=resolved_session,
                current_project_id=value("current_project_id", "project_id"),
                character_name=value("character_name"),
                guild_id=guild_text,
                conversation_id=(
                    str(value("conversation_id"))
                    if value("conversation_id") is not None
                    else None
                ),
                config=value("config", "privacy_config"),
                session_context=value("session_context"),
                project_metadata=value("project_metadata"),
            )
        return NanobananaUsageContext.from_discord(
            user_id,
            guild_id,
            session_id,
            character_name=value("character_name"),
            conversation_id=value("conversation_id"),
            config=value("config", "privacy_config"),
            session_context=value("session_context"),
            project_metadata=value("project_metadata"),
        )

    @staticmethod
    def _context_value(context: Any, name: str, *aliases: str) -> Any:
        """Read an optional policy value from object or mapping contexts."""

        if context is None:
            return None
        keys = (name, *aliases)
        if isinstance(context, Mapping):
            for key in keys:
                value = context.get(key)
                if value is not None:
                    return value
        for key in keys:
            value = getattr(context, key, None)
            if value is not None:
                return value
        return None

    def _privacy_gateway_for_request(
        self,
        *,
        usage_client: Any,
        usage_context: Any,
        resolved_context: Any,
    ) -> OutboundPrivacyGateway:
        """Build a request-scoped gateway with an explicit policy source.

        Nanobanana is an out-of-band Discord command and therefore does not
        always run inside the normal LLM turn.  Resolve config and effective
        session/project policy from the request first, then the service
        default, and finally the application Config.  If Config cannot be
        loaded, fail closed rather than allowing the gateway's ``None`` config
        to select the historical direct default.
        """

        candidates = (usage_context, usage_client, resolved_context, self)
        privacy_config = next(
            (
                value
                for candidate in candidates
                if (value := self._context_value(candidate, "config", "privacy_config"))
                is not None
            ),
            None,
        )
        if privacy_config is None:
            try:
                from ...config import Config

                privacy_config = Config()
            except Exception as exc:  # pragma: no cover - deployment failure
                raise PrivacyError(
                    "Nanobanana privacy configuration is unavailable; "
                    "external image generation is withheld"
                ) from exc

        inherited = get_privacy_policy_context()
        session_context = next(
            (
                value
                for candidate in candidates
                if isinstance(
                    value := self._context_value(candidate, "session_context"),
                    Mapping,
                )
            ),
            None,
        )
        project_metadata = next(
            (
                value
                for candidate in candidates
                if isinstance(
                    value := self._context_value(candidate, "project_metadata"),
                    Mapping,
                )
            ),
            None,
        )
        # A compact ``privacy_mode`` field is accepted for integrations that
        # cannot carry the full session/project maps.  The gateway still
        # applies its monotonic effective-mode rule.
        if session_context is None:
            mode = next(
                (
                    value
                    for candidate in candidates
                    if (value := self._context_value(candidate, "privacy_mode"))
                    not in (None, "")
                ),
                None,
            )
            if mode is not None:
                session_context = {"privacy_mode": str(mode)}
        if session_context is None:
            session_context = inherited.session_context
        if project_metadata is None:
            project_metadata = inherited.project_metadata

        resolved_user_id = self._context_value(resolved_context, "user_id")
        if resolved_user_id in (None, ""):
            getter = getattr(resolved_context, "_get_session_user_id", None)
            if callable(getter):
                try:
                    resolved_user_id = getter()
                except Exception:
                    resolved_user_id = None

        return OutboundPrivacyGateway(
            privacy_config,
            session_id=str(
                self._context_value(resolved_context, "current_session_id", "session_id")
                or ""
            ),
            user_id=str(
                resolved_user_id
                or self._context_value(resolved_context, "session_user_id")
                or ""
            ),
            session_context=(
                dict(session_context) if isinstance(session_context, Mapping) else None
            ),
            project_metadata=(
                dict(project_metadata)
                if isinstance(project_metadata, Mapping)
                else None
            ),
        )

    def _resolve_usage_client(
        self,
        *,
        usage_client: Any = None,
        usage_context: Any = None,
    ) -> Any:
        if usage_client is not None:
            return usage_client
        if usage_context is not None:
            return self._coerce_usage_context(usage_context)
        default_client = getattr(self, "usage_client", None)
        if default_client is not None:
            return default_client
        return _turn_usage_client()

    @staticmethod
    def _set_turn_context_for_usage(usage_client: Any) -> Any:
        """Install a temporary task-local context for web_search adapters."""
        if usage_client is None:
            return None
        try:
            from ...services.turn_context import set_turn_context

            getter = getattr(usage_client, "_get_session_user_id", None)
            user_id = getter() if callable(getter) else getattr(usage_client, "user_id", None)
            return set_turn_context(
                user_id=(str(user_id) if user_id else None),
                project_id=(
                    str(getattr(usage_client, "current_project_id", None))
                    if getattr(usage_client, "current_project_id", None)
                    else None
                ),
                session_id=(
                    str(getattr(usage_client, "current_session_id", None))
                    if getattr(usage_client, "current_session_id", None)
                    else None
                ),
            )
        except Exception:
            return None

    @staticmethod
    def _reset_turn_context(token: Any) -> None:
        if token is None:
            return
        try:
            from ...services.turn_context import reset_turn_context

            reset_turn_context(token)
        except Exception:
            logger.debug("Failed to reset Nanobanana turn context", exc_info=True)

    def _mark_usage_recorded(self, response: Any) -> bool:
        """Avoid duplicate usage rows for a response replayed by a wrapper."""
        try:
            if getattr(response, "_aoitalk_usage_recorded", False):
                return True
            object.__setattr__(response, "_aoitalk_usage_recorded", True)
            return False
        except Exception:
            recorded = getattr(self, "_recorded_usage_responses", None)
            if recorded is None:
                recorded = []
                self._recorded_usage_responses = recorded
            if any(item is response for item in recorded):
                return True
            recorded.append(response)
            del recorded[:-8]
            return False

    def _record_image_usage(
        self,
        response: Any,
        *,
        latency_ms: int = 0,
        usage_client: Any = None,
        usage_context: Any = None,
    ) -> bool:
        """Persist provider-reported OpenAI Images usage, if the API returned it.

        The Images API commonly returns image bytes without token counts.  We
        intentionally leave those requests unmetered instead of estimating
        usage from prompt characters or output bytes.
        """
        raw_usage = getattr(response, "usage", None)
        if raw_usage is None and isinstance(response, dict):
            raw_usage = response.get("usage")
        resolved_model = getattr(response, "model", None)
        if resolved_model is None and isinstance(response, dict):
            resolved_model = response.get("model")
        payload: Dict[str, Any] = normalize_usage(
            raw_usage,
            provider="openai",
            resolved_model=(str(resolved_model) if resolved_model else None),
        )
        if payload.get("input_tokens") is None and payload.get("output_tokens") is None:
            logger.info(
                "Image generation response has no token usage; "
                "leaving Nanobanana image request unmetered"
            )
            return False
        if self._mark_usage_recorded(response):
            return False
        try:
            resolved_usage_client = self._resolve_usage_client(
                usage_client=usage_client,
                usage_context=usage_context,
            )
            persist_usage_sync(
                resolved_usage_client,
                provider="openai",
                model="gpt-image-1",
                usage=payload,
                request_type="image",
                latency_ms=max(int(latency_ms or 0), 0),
            )
            return True
        except Exception:  # pragma: no cover - telemetry must not break command
            logger.debug("Image generation usage persistence failed", exc_info=True)
            return False

    def fetch_summary(
        self,
        usage_client: Any = None,
        usage_context: Any = None,
    ) -> str:
        """Fetch summary text via web_search tool"""
        logger.info("Fetching Nanobanana Pro summary via web_search tool")
        from ...tools.basic.web_search import web_search_impl

        resolved_usage_client = self._resolve_usage_client(
            usage_client=usage_client,
            usage_context=usage_context,
        )
        resolved_context = self._coerce_usage_context(resolved_usage_client)
        privacy_gateway = self._privacy_gateway_for_request(
            usage_client=usage_client,
            usage_context=usage_context,
            resolved_context=resolved_context,
        )
        turn_context_token = self._set_turn_context_for_usage(resolved_usage_client)
        privacy_token = None
        try:
            # Bind both policy maps while the out-of-band search runs.  The
            # web-search gateway receives the request config and usage context
            # explicitly, so a Discord command cannot silently fall back to a
            # direct/default provider policy.
            from ...services.outbound_privacy_service import set_privacy_policy_context

            privacy_token = set_privacy_policy_context(
                session_context=privacy_gateway.session_context,
                project_metadata=privacy_gateway.project_metadata,
            )
            try:
                result = web_search_impl(
                    self.SEARCH_QUERY,
                    config=privacy_gateway.config,
                    usage_context=resolved_context,
                )
            except TypeError as exc:
                # Preserve compatibility with integrations that monkeypatch
                # the historical one-argument helper; the in-tree helper
                # always receives the explicit request policy above.
                if "unexpected keyword" not in str(exc).lower():
                    raise
                result = web_search_impl(self.SEARCH_QUERY)
        finally:
            if privacy_token is not None:
                from ...services.outbound_privacy_service import reset_privacy_policy_context

                reset_privacy_policy_context(privacy_token)
            self._reset_turn_context(turn_context_token)
        if not isinstance(result, str) or not result.strip():
            return "Nanobanana Proの最新情報を取得できませんでした。"
        return result.strip()

    def build_embed_description(self, summary: str, max_items: int = 3, max_len: int = 900) -> str:
        """Build a compact bullet list for Discord embeds"""
        highlights = self._extract_highlights(summary, limit=max_items)
        if not highlights:
            return "Nanobanana Proに関する追加情報を取得できませんでした。"
        joined = "\n".join(f"・{item}" for item in highlights)
        return textwrap.shorten(joined, width=max_len, placeholder="...")

    def generate_image(
        self,
        summary: str,
        usage_client: Any = None,
        usage_context: Any = None,
    ) -> Tuple[Optional[bytes], str]:
        """Generate promotional image bytes and prompt"""
        prompt = self._build_image_prompt(summary)
        resolved_usage_client = self._resolve_usage_client(
            usage_client=usage_client,
            usage_context=usage_context,
        )
        resolved_context = self._coerce_usage_context(resolved_usage_client)
        try:
            privacy_gateway = self._privacy_gateway_for_request(
                usage_client=usage_client,
                usage_context=usage_context,
                resolved_context=resolved_context,
            )
            protected = privacy_gateway.protect_sync(
                {"prompt": prompt},
                provider="openai",
                source_kind="nanobanana_image",
            )
            if isinstance(protected.payload, Mapping):
                prompt = str(protected.payload.get("prompt") or prompt)
        except Exception as exc:
            # The prompt must never reach OpenAI without a resolved privacy
            # policy.  Keep the slash command usable as a summary-only reply.
            logger.warning("Nanobanana privacy gate blocked image generation: %s", exc)
            return None, prompt
        api_key = os.getenv('OPENAI_API_KEY')
        if not api_key:
            logger.warning("OPENAI_API_KEY is missing. Skipping image generation.")
            return None, prompt

        if self._client is None:
            self._client = OpenAI(api_key=api_key)

        import time

        started = time.monotonic()
        try:
            logger.info("Generating Nanobanana Pro hero image via OpenAI Images API")
            response = self._client.images.generate(
                model="gpt-image-1",
                prompt=prompt,
                size="1024x1024",
                quality="high",
                response_format="b64_json"
            )
            image_data = response.data[0].b64_json
            image_bytes = base64.b64decode(image_data)
            self._record_image_usage(
                response,
                latency_ms=int((time.monotonic() - started) * 1000),
                usage_client=usage_client,
                usage_context=usage_context,
            )
            return image_bytes, prompt
        except Exception as exc:  # pragma: no cover - network failure path
            logger.error("Image generation failed: %s", exc)
            return None, prompt

    def _extract_highlights(self, text: str, limit: int = 3) -> List[str]:
        sanitized = text.replace('\r', '\n')
        parts = re.split(r"[\n\r]+|(?<=[。．.!?])\s+", sanitized)
        cleaned: List[str] = []
        for part in parts:
            item = part.strip(" -*•\u3000")
            if len(item) < 20:
                continue
            cleaned.append(item)
            if len(cleaned) >= limit:
                break
        if not cleaned and text:
            cleaned.append(textwrap.shorten(text, width=150, placeholder="..."))
        return cleaned

    def _build_image_prompt(self, summary: str) -> str:
        keywords = self._extract_keywords(summary)
        keyword_text = ", ".join(keywords) if keywords else "creative AI studio"
        prompt = (
            "Create a cinematic 4K marketing render for Nanobanana Pro, a reasoning-first AI "
            "image and video suite. Highlight {features} with glossy banana-yellow accents, "
            "floating multitouch canvases, holographic UI, and pro-grade lighting. Include subtle "
            "SynthID watermark indicators and futuristic studio vibes.".format(features=keyword_text)
        )
        return prompt

    def _extract_keywords(self, text: str) -> List[str]:
        candidates = {
            "4K": "4K fidelity dashboards",
            "8K": "8K-ready canvas",
            "lossless": "lossless diffusion",
            "reasoning": "reasoning copilots",
            "video": "video + image hybrid workflows",
            "texture": "photoreal textures",
            "SynthID": "SynthID-visible security",
            "AR": "mixed-reality overlays"
        }
        found: List[str] = []
        lowered = text.lower()
        for token, phrase in candidates.items():
            if token.lower() in lowered:
                found.append(phrase)
        return found[:4]
