"""Outbound privacy boundary for model/provider requests.

The service deliberately lives outside Agent Team and provider-specific code.
It is a small, dependency-free gate which can be used both by native async
runtime transports and by synchronous provider adapters.  In ``direct`` mode
it is a no-op; ``protected`` mode applies deterministic redaction first and
optionally asks an injected local semantic redactor for exact substrings; and
``local_only`` refuses requests whose *resolved* provider is not trusted local.

No alias table is persisted.  A gateway instance is scoped to one session and
therefore keeps reversible aliases consistent for the duration of a turn.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import concurrent.futures
import hashlib
import inspect
import ipaddress
import json
import logging
import math
import os
import re
import contextvars
import threading
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable, Iterable, Mapping, MutableMapping
from urllib.parse import urlsplit

import httpx

from .secret_patterns import HIGH_CONFIDENCE_API_TOKEN_RE

logger = logging.getLogger(__name__)

# Synchronous provider adapters can call ``protect_sync`` from an already
# running event loop. Keep those bridge threads bounded so a callback that
# ignores cancellation cannot create an unbounded thread per request. A
# semaphore is used instead of ThreadPoolExecutor because executor workers
# are non-daemon and can keep process shutdown alive after a timed-out task.
_PROTECT_SYNC_SLOTS = threading.BoundedSemaphore(4)


def _consume_async_task(task: asyncio.Task[Any]) -> None:
    """Retrieve a detached privacy task's result/exception."""

    if task.cancelled():
        return
    try:
        task.exception()
    except BaseException:
        return


async def _await_bounded(awaitable: Awaitable[Any], timeout: float) -> Any:
    """Await a sidecar callback without waiting for cancellation propagation."""

    task = asyncio.ensure_future(awaitable)
    try:
        _done, pending = await asyncio.wait({task}, timeout=timeout)
        if pending:
            task.cancel()
            task.add_done_callback(_consume_async_task)
            raise asyncio.TimeoutError
        return task.result()
    except asyncio.CancelledError:
        if not task.done():
            task.cancel()
            task.add_done_callback(_consume_async_task)
        raise

PrivacyMode = str
ReviewPolicy = str
MASKING_APPLIED = "MASKING_APPLIED"
MASKING_UNNECESSARY = "MASKING_UNNECESSARY"


@dataclass(frozen=True)
class PrivacyPolicyContext:
    """Request-local policy metadata inherited by nested Agent Team calls.

    The context is carried by :mod:`contextvars`, rather than a process-global
    mutable setting, so an Agent/Tool child sees the same effective project and
    session policy as its parent while concurrent users remain isolated.
    """

    session_context: Mapping[str, Any] | None = None
    project_metadata: Mapping[str, Any] | None = None


_privacy_policy_context: contextvars.ContextVar[PrivacyPolicyContext] = contextvars.ContextVar(
    "aoitalk_privacy_policy_context", default=PrivacyPolicyContext()
)


def set_privacy_policy_context(
    *,
    session_context: Mapping[str, Any] | None = None,
    project_metadata: Mapping[str, Any] | None = None,
) -> contextvars.Token[PrivacyPolicyContext]:
    """Bind effective session/project policy for the current assistant turn."""

    return _privacy_policy_context.set(
        PrivacyPolicyContext(
            session_context=(dict(session_context) if isinstance(session_context, Mapping) else None),
            project_metadata=(dict(project_metadata) if isinstance(project_metadata, Mapping) else None),
        )
    )


def reset_privacy_policy_context(token: contextvars.Token[PrivacyPolicyContext]) -> None:
    _privacy_policy_context.reset(token)


def get_privacy_policy_context() -> PrivacyPolicyContext:
    return _privacy_policy_context.get()


def current_effective_privacy_mode(config: Any | None = None) -> str:
    """Resolve policy using the request-local inherited context, if present."""

    context = get_privacy_policy_context()
    return effective_privacy_mode(
        config,
        session_context=context.session_context,
        project_metadata=context.project_metadata,
    )

EXTERNAL_PROVIDER_IDS = frozenset(
    {
        "jev",
        "browser_agent",
        "openai",
        "openai_realtime",
        "gemini",
        "openrouter",
        "deepseek",
        "deepinfra",
        "kimi",
        "chatgpt-web",
        "chatgpt_web",
        "web-chatgpt",
        "codex-cli",
        "claude-cli",
        "antigravity-cli",
        "grok-cli",
        "claude",
        "grok",
        "yahoo_realtime",
        # Canonical external sink labels used by adapter descriptors.  Their
        # endpoint/transport is still bound into each transaction; the labels
        # simply prevent an unknown provider from being treated as trusted.
        "mcp_external",
        "nijivoice",
        "discord",
        "webex",
        "google_calendar",
        "growi",
        "remote_aoitalk",
        "web_push",
        "spotify",
        "youtube",
        "niconico",
        "google_speech",
        "heartbeat_webhook",
        "ogp_fetch",
        "xai",
        "weather-api",
        "yahoo-search",
        "google_calendar",
    }
)
LOCAL_PROVIDER_IDS = frozenset(
    {
        "ollama",
        "sglang",
        "openai_compatible_local",
        "mage_vl",
        "speech_recognition",
        "aivisspeech",
        "voicevox",
        "comfyui",
        "hydrus",
    }
)
CLI_PROVIDER_IDS = frozenset(
    {"codex-cli", "claude-cli", "antigravity-cli", "grok-cli"}
)

_SECRET_RE = re.compile(
    r"(?i)(?P<prefix>\b(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|auth[_ -]?token|session[_ -]?token|client[_ -]?secret|saml[_ -]?response|relay[_ -]?state|secret|token|password|passwd|credential|authorization|cookie)\s*[\"']?\s*[:=]\s*)"
    # Keep quoted JSON/YAML values intact while consuming delimiter-like
    # characters in unquoted secrets.  A value such as ``abc]def`` must not
    # leave ``]def`` in the provider payload.
    r"(?P<value>\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|(?!Basic\b)[^\s,;]+)"
)
# Display-only redaction intentionally remains narrower than the protected
# outbound gateway.  It must hide credential material without masking ordinary
# work content such as file paths, emails, or internal hosts in an approval
# preview.
_DISPLAY_SECRET_KEY_RE = re.compile(
    r"(?:"
    # Credential words separated by punctuation/whitespace, or used as a
    # complete key.  The alphanumeric boundaries avoid treating ordinary
    # fields such as ``tokenizer`` as credentials.
    r"(?i:(?<![a-z0-9])"
    r"(?:api[_\- ]?key|access[_\- ]?token|refresh[_\- ]?token|"
    r"auth[_\- ]?token|session[_\- ]?token|client[_\- ]?secret|"
    r"token|secret|password|passwd|credential|authorization|cookie)"
    r"(?![a-z0-9]))"
    # CamelCase credential components (for example ``sourceToken``) are
    # bounded by the lower-to-upper transition rather than a separator.
    r"|(?<=[a-z])(?:Token|Secret|Password|Passwd|Credential|Authorization|Cookie|Key)"
    r"(?![A-Za-z])"
    r")"
)
_DISPLAY_SECRET_MARKER = "[REDACTED]"
# RFC 7617 Basic credentials are base64-encoded ``user:password`` pairs.  A
# scheme followed by a sufficiently long standard-base64 token is a
# high-confidence credential form; redact the token while retaining only the
# harmless scheme label for human readability.
_BASIC_AUTH_RE = re.compile(
    r"(?i)(?P<prefix>\bBasic\s+)(?P<value>[A-Za-z0-9+/]{1,}={0,2})(?![A-Za-z0-9+/=])"
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{1,}")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_AWS_ACCESS_KEY_RE = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_PRIVATE_IP_RE = re.compile(
    r"(?<![\w.])(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2}|127(?:\.\d{1,3}){3})(?![\w.])"
)
_URL_RE = re.compile(
    r"(?i)\bhttps?://(?:[^\s/@:]+(?::[^\s/@]*)?@)?(?:[A-Za-z0-9_-]+\.)*(?:internal|local|localhost|intranet|corp|lan)(?::\d+)?(?:/[^\s]*)?"
)
_WINDOWS_PATH_RE = re.compile(r"(?<![\w])(?:[A-Za-z]:\\|\\\\)[^\r\n\t\"']+")
_UNIX_PATH_RE = re.compile(r"(?<![\w])/(?:home|Users|Users|var|tmp|opt|srv|mnt|workspace|work)/[^\r\n\t\"']+")
_DATA_URL_RE = re.compile(r"^data:(?:image|audio|video)/[^;,]+(?:;[^,]*)?,", re.I)
_BASE64_RE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
_MEDIA_CONTAINER_KEYS = frozenset(
    {
        "image",
        "image_url",
        "input_image",
        "input_audio",
        "audio",
        "video",
        "media",
        "inline_data",
        "source",
        "content_block",
    }
)
_MEDIA_BASE64_KEYS = frozenset(
    {
        "base64",
        "base64_data",
        "content_base64",
        "data_base64",
        "image_base64",
        "audio_base64",
        "video_base64",
        "reference_base64",
        "reference_audio_base64",
    }
)

# Tool arguments are rehydrated only for AoiTalk-local execution.  External
# egress tools must keep aliases in their query/payload; those tools apply a
# fresh gateway at their own provider boundary and must never receive the raw
# value merely because the model requested a search.
_EXTERNAL_EGRESS_TOOL_NAMES = frozenset(
    {
        "browser_agent",
        "web_search",
        "grok_x_search",
        "web_search_mcp",
        "x_search",
    }
)


def is_external_egress_tool_name(tool_name: Any) -> bool:
    """Return whether tool arguments must remain aliased until egress.

    MCP wrappers are dynamically named (``mcp_<server>_<tool>``), so a fixed
    allow-list cannot safely cover all configured external servers.
    """

    normalized = str(tool_name or "").strip().lower()
    return bool(
        normalized in _EXTERNAL_EGRESS_TOOL_NAMES
        or normalized == "mcp"
        or normalized.startswith("mcp_")
        or normalized.startswith("external_")
        or normalized.endswith("_mcp")
        or "_mcp_" in normalized
    )


class PrivacyError(RuntimeError):
    """Base error raised when the outbound boundary cannot safely proceed."""


class ExternalProviderBlocked(PrivacyError):
    """Raised when local-only policy would require an external provider."""


class PrivacyReviewDenied(PrivacyError):
    """Raised when a user review callback rejects or cannot approve payload."""


class RawMediaBlocked(PrivacyError):
    """Raised instead of sending unredactable binary/media content externally."""


def redact_secret_for_local_display(value: Any) -> Any:
    """Redact credential-like values for a local human-facing projection.

    This helper is deliberately *secret-only*: unlike the outbound privacy
    gateway it does not replace names, emails, paths, private hosts, or other
    ordinary work data.  Mapping keys that clearly denote credentials are
    replaced wholesale, while textual values are scanned only for high
    confidence secret forms (labelled secrets, bearer/JWT/AWS tokens).  The
    function is pure and never records a reversible alias, making it safe for
    websocket/audit previews.  Every mapping key is passed through the same
    text redactor as values; credential-label detection still uses the raw key
    so that the associated value can be replaced wholesale before recursion.
    """

    def redact_text(text: str) -> str:
        result = str(text)

        def labelled(match: re.Match[str]) -> str:
            prefix = match.groupdict().get("prefix") or ""
            value = match.groupdict().get("value") or ""
            if len(value) >= 2 and value[0] in {'"', "'"} and value[-1] == value[0]:
                marker = f"{value[0]}{_DISPLAY_SECRET_MARKER}{value[-1]}"
            else:
                marker = _DISPLAY_SECRET_MARKER
            return f"{prefix}{marker}"

        # Replace high-confidence token forms before labelled ``key: value``
        # matching; otherwise the generic matcher would consume only the
        # scheme (``Basic``/``Bearer``) and leave its credential suffix
        # visible.  Keep the Basic scheme so reviewers can identify the
        # credential type without exposing the encoded user/password.
        result = _BASIC_AUTH_RE.sub(
            lambda match: f"{match.group('prefix')}{_DISPLAY_SECRET_MARKER}",
            result,
        )
        result = _BEARER_RE.sub(_DISPLAY_SECRET_MARKER, result)
        result = _JWT_RE.sub(_DISPLAY_SECRET_MARKER, result)
        result = _AWS_ACCESS_KEY_RE.sub(_DISPLAY_SECRET_MARKER, result)
        # Provider API tokens are redacted before the generic labelled-secret
        # matcher so a token embedded in otherwise innocuous plan text cannot
        # leak through a preview or interaction audit projection.
        result = HIGH_CONFIDENCE_API_TOKEN_RE.sub(_DISPLAY_SECRET_MARKER, result)
        result = _SECRET_RE.sub(labelled, result)
        return result

    def walk(node: Any) -> Any:
        if isinstance(node, Mapping):
            output: dict[str, Any] = {}
            for key, item in node.items():
                # Redact keys as display text too.  Credential-label matching
                # intentionally remains on the raw key, before token masking,
                # so ``api_key``/``authorization`` values are never traversed
                # and accidentally exposed through a nested projection.
                key_text = str(key)
                redacted_key = redact_text(key_text)
                if redacted_key in output:
                    # A display redaction can collapse two distinct source
                    # keys (for example ``secret=one`` and ``secret=two``).
                    # Refuse to produce an ambiguous human-facing object
                    # rather than silently dropping or merging evidence.
                    raise ValueError("display_redaction_key_collision")
                if _DISPLAY_SECRET_KEY_RE.search(key_text):
                    output[redacted_key] = _DISPLAY_SECRET_MARKER if item not in (None, "") else item
                else:
                    output[redacted_key] = walk(item)
            return output
        if isinstance(node, list):
            return [walk(item) for item in node]
        if isinstance(node, tuple):
            return tuple(walk(item) for item in node)
        if isinstance(node, str):
            return redact_text(node)
        # Bytes/media are rejected by the material-action preview builder; the
        # marker here is a defensive fallback for direct callers.
        if isinstance(node, (bytes, bytearray, memoryview)):
            return "[REDACTED_BINARY]"
        return node

    return walk(value)


@dataclass(frozen=True)
class RedactionFinding:
    category: str
    placeholder: str
    count: int = 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "placeholder": self.placeholder,
            "count": self.count,
        }


@dataclass(frozen=True)
class PrivacyResult:
    # Payload projections can contain raw user content.  They are deliberately
    # excluded from the generated dataclass repr: review callbacks receive the
    # fields explicitly, while accidental ``repr(result)``/exception logging
    # must never become a raw-payload sink.
    payload: Any = field(repr=False)
    findings: tuple[RedactionFinding, ...] = ()
    mode: PrivacyMode = "direct"
    provider_class: str = "unknown"
    semantic_status: str = "disabled"
    risk_level: str = "low"
    cache_hit: bool = False
    source_kind: str = field(default="model_request", repr=False)
    provider: str = field(default="", repr=False)
    model: str = field(default="", repr=False)
    # Review/decision state is kept on the result so the audit trail can
    # describe whether this payload was sent directly, approved by a reviewer,
    # served from cache, or denied before transport.  These fields deliberately
    # contain status only; the payload and any raw values never enter the audit.
    review: str = "not_required"
    decision: str = "allow"
    # v2 transaction projections.  ``payload`` remains the compatibility
    # alias for the candidate/final payload returned to existing adapters;
    # these fields make the three stages explicit for egress review.
    original_payload: Any = field(default=None, repr=False)
    candidate_payload: Any = field(default=None, repr=False)
    final_payload: Any = field(default=None, repr=False)
    masking_status: str = MASKING_UNNECESSARY
    egress_descriptor: Any = field(default=None, repr=False, compare=False)

    @property
    def egress(self) -> Any:
        """Compatibility alias for the canonical egress descriptor."""

        return self.egress_descriptor


@dataclass(frozen=True)
class EgressDescriptor:
    """Stable route metadata bound into one external-send transaction."""

    action: str = ""
    transport: str = ""
    destination: str = ""
    provider: str = ""
    tool: str = ""
    model: str = ""

    def normalized(self) -> "EgressDescriptor":
        """Return a type-safe descriptor with required route fields present.

        Descriptors are part of the approval binding.  Treating arbitrary
        values as strings would allow malformed metadata (or a mutable object
        with surprising ``__str__`` behaviour) to reach the review protocol.
        Callers may omit optional ``tool``/``model`` labels, but action,
        transport, destination and provider must be non-empty strings.
        """

        fields = ("action", "transport", "destination", "provider", "tool", "model")
        if any(type(getattr(self, field)) is not str for field in fields):
            raise PrivacyError("malformed egress descriptor")
        if any(not getattr(self, field).strip() for field in ("action", "transport", "destination", "provider")):
            raise PrivacyError("egress descriptor is missing required metadata")
        return self

    def as_dict(self) -> dict[str, str]:
        return {
            "action": str(self.action or ""),
            "transport": str(self.transport or ""),
            "destination": str(self.destination or ""),
            "provider": str(self.provider or ""),
            "tool": str(self.tool or ""),
            "model": str(self.model or ""),
        }


@dataclass(frozen=True)
class PrivacyConfig:
    mode: PrivacyMode = "direct"
    review_policy: ReviewPolicy = "high_risk"
    notify: bool = True
    semantic_redaction_enabled: bool = True
    local_provider: str = "openai_compatible_local"
    local_model: str = ""
    redaction_terms: tuple[str, ...] = ()
    trusted_local_hosts: tuple[str, ...] = ()
    raw_media_policy: str = "block"
    cache_enabled: bool = True
    semantic_timeout_seconds: float = 8.0
    # ``semantic_timeout_seconds`` bounds one sidecar request.  The callback
    # timeout is kept as a separate field so an injected detector cannot
    # accidentally inherit an unbounded/legacy transport timeout.
    semantic_callback_timeout_seconds: float = 8.0
    semantic_total_timeout_seconds: float = 30.0
    semantic_max_output_tokens: int = 512
    semantic_max_calls_per_payload: int = 64
    semantic_max_input_chars_per_payload: int = 24_000
    semantic_max_entities_per_payload: int = 64


def _get(config: Any, key: str, default: Any = None) -> Any:
    if isinstance(config, Mapping):
        current: Any = config
        for part in key.split("."):
            if not isinstance(current, Mapping) or part not in current:
                return default
            current = current[part]
        return current
    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            return getter(key, default)
        except TypeError:
            try:
                return getter(key)
            except Exception:  # noqa: BLE001
                return default
        except Exception:  # noqa: BLE001
            return default
    return default


def _bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _bounded_float(value: Any, *, default: float, minimum: float, maximum: float) -> float:
    """Parse one finite timeout and clamp it to a safe operator range."""

    try:
        candidate = float(value)
    except (TypeError, ValueError):
        candidate = float(default)
    if not math.isfinite(candidate):
        candidate = float(default)
    return max(float(minimum), min(candidate, float(maximum)))


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    """Parse one integer budget and clamp it to a safe operator range."""

    try:
        # ``int(1.5)`` silently rounds toward zero.  Configuration is
        # persisted as scalar JSON/YAML values, so reject non-integral floats
        # rather than letting a typo weaken a bound unexpectedly.
        if isinstance(value, float) and not value.is_integer():
            raise ValueError
        candidate = int(value)
    except (TypeError, ValueError, OverflowError):
        candidate = int(default)
    return max(int(minimum), min(candidate, int(maximum)))


def privacy_config(config: Any | None = None) -> PrivacyConfig:
    """Resolve and normalize independent ``external_model_privacy`` settings."""

    # Enterprise defaults to protected, while Personal keeps the historical
    # direct path.  Import lazily to avoid config/features import cycles.
    mode_value = _get(config, "external_model_privacy.mode", None)
    if mode_value is None:
        try:
            from ..features import Features

            default_mode = "protected" if Features.is_enterprise() else "direct"
        except Exception:  # noqa: BLE001
            default_mode = "direct"
        mode_value = default_mode
    mode = str(mode_value or "direct").strip().lower()
    if mode not in {"direct", "protected", "local_only"}:
        mode = "direct"
    policy = str(_get(config, "external_model_privacy.review_policy", "high_risk") or "high_risk").strip().lower()
    if policy not in {"never", "high_risk", "always"}:
        policy = "high_risk"
    terms = _get(config, "external_model_privacy.redaction_terms", ())
    if isinstance(terms, str):
        terms = [terms]
    if not isinstance(terms, Iterable):
        terms = ()
    normalized_terms = tuple(dict.fromkeys(str(term).strip() for term in terms if str(term).strip()))
    hosts = _get(config, "external_model_privacy.trusted_local_hosts", ())
    if isinstance(hosts, str):
        hosts = [hosts]
    if not isinstance(hosts, Iterable):
        hosts = ()
    normalized_hosts = tuple(dict.fromkeys(str(host).strip().lower() for host in hosts if str(host).strip()))
    raw_media_policy = str(
        _get(config, "external_model_privacy.raw_media_policy", "block") or "block"
    ).strip().lower()
    if raw_media_policy not in {"block", "confirm"}:
        raw_media_policy = "block"
    semantic_timeout_seconds = _bounded_float(
        _config_value(
            config,
            "external_model_privacy.semantic_timeout_seconds",
            "external_model_privacy.semantic_timeout",
            "external_model_privacy.local_model_timeout_seconds",
            default=8.0,
        ),
        default=8.0,
        minimum=0.1,
        maximum=30.0,
    )
    semantic_callback_timeout_seconds = _bounded_float(
        _config_value(
            config,
            "external_model_privacy.semantic_callback_timeout_seconds",
            "external_model_privacy.semantic_callback_timeout",
            "external_model_privacy.local_model_callback_timeout_seconds",
            default=semantic_timeout_seconds,
        ),
        default=semantic_timeout_seconds,
        minimum=0.1,
        maximum=60.0,
    )
    semantic_total_timeout_seconds = _bounded_float(
        _config_value(
            config,
            "external_model_privacy.semantic_total_timeout_seconds",
            "external_model_privacy.semantic_aggregate_timeout_seconds",
            "external_model_privacy.semantic_max_total_timeout_seconds",
            "external_model_privacy.local_model_total_timeout_seconds",
            default=max(semantic_callback_timeout_seconds, 30.0),
        ),
        default=max(semantic_callback_timeout_seconds, 30.0),
        minimum=0.1,
        maximum=300.0,
    )
    semantic_max_output_tokens = _bounded_int(
        _config_value(
            config,
            "external_model_privacy.semantic_max_output_tokens",
            "external_model_privacy.semantic_max_tokens",
            "external_model_privacy.local_model_max_tokens",
            default=512,
        ),
        default=512,
        minimum=64,
        maximum=2048,
    )
    semantic_max_calls_per_payload = _bounded_int(
        _config_value(
            config,
            "external_model_privacy.semantic_max_calls_per_payload",
            "external_model_privacy.semantic_max_calls",
            "external_model_privacy.semantic_call_budget",
            "external_model_privacy.semantic_max_sidecar_calls",
            "external_model_privacy.semantic_max_requests_per_payload",
            "external_model_privacy.local_model_max_calls_per_payload",
            "external_model_privacy.local_model_call_budget",
            "external_model_privacy.local_model_max_calls",
            default=64,
        ),
        default=64,
        minimum=1,
        maximum=256,
    )
    semantic_max_input_chars_per_payload = _bounded_int(
        _config_value(
            config,
            "external_model_privacy.semantic_max_input_chars_per_payload",
            "external_model_privacy.semantic_max_input_chars_per_call",
            "external_model_privacy.semantic_max_total_input_chars",
            "external_model_privacy.semantic_input_budget",
            "external_model_privacy.semantic_max_input_chars",
            "external_model_privacy.local_model_max_input_chars_per_payload",
            "external_model_privacy.local_model_input_budget",
            "external_model_privacy.local_model_max_input_chars",
            default=24_000,
        ),
        default=24_000,
        minimum=1,
        maximum=1_000_000,
    )
    semantic_max_entities_per_payload = _bounded_int(
        _config_value(
            config,
            "external_model_privacy.semantic_max_entities_per_payload",
            "external_model_privacy.semantic_max_entities_per_call",
            "external_model_privacy.semantic_max_total_entities",
            "external_model_privacy.semantic_entity_budget",
            "external_model_privacy.semantic_max_entities",
            "external_model_privacy.local_model_max_entities_per_payload",
            "external_model_privacy.local_model_entity_budget",
            "external_model_privacy.local_model_max_entities",
            default=64,
        ),
        default=64,
        minimum=1,
        maximum=256,
    )
    return PrivacyConfig(
        mode=mode,
        review_policy=policy,
        notify=_bool(_get(config, "external_model_privacy.notify", True), True),
        semantic_redaction_enabled=_bool(
            _get(config, "external_model_privacy.semantic_redaction_enabled", True), True
        ),
        local_provider=str(_get(config, "external_model_privacy.local_provider", "openai_compatible_local") or "openai_compatible_local").strip().lower(),
        local_model=str(_get(config, "external_model_privacy.local_model", "") or "").strip(),
        redaction_terms=normalized_terms,
        trusted_local_hosts=normalized_hosts,
        raw_media_policy=raw_media_policy,
        cache_enabled=_bool(_get(config, "external_model_privacy.cache_enabled", True), True),
        semantic_timeout_seconds=semantic_timeout_seconds,
        semantic_callback_timeout_seconds=semantic_callback_timeout_seconds,
        semantic_total_timeout_seconds=semantic_total_timeout_seconds,
        semantic_max_output_tokens=semantic_max_output_tokens,
        semantic_max_calls_per_payload=semantic_max_calls_per_payload,
        semantic_max_input_chars_per_payload=semantic_max_input_chars_per_payload,
        semantic_max_entities_per_payload=semantic_max_entities_per_payload,
    )


def _config_value(config: Any | None, *keys: str, default: Any = "") -> Any:
    """Return the first configured value from dotted or flat aliases."""

    for key in keys:
        value = _get(config, key, None)
        if value not in (None, ""):
            return value
    return default


def _local_sidecar_endpoint(
    config: Any | None,
    provider: str,
) -> tuple[str, str]:
    """Resolve a privacy sidecar endpoint without consulting main deployment.

    The sidecar intentionally reuses existing provider connection settings and
    environment variables.  It is never allowed to inherit the selected cloud
    provider's URL or credentials by accident.
    """

    normalized = str(provider or "").strip().lower()
    aliases = {
        "ollama": (
            ("ollama.base_url", "ollama_base_url"),
            "OLLAMA_BASE_URL",
            "http://127.0.0.1:11434/v1",
        ),
        "sglang": (
            ("sglang.base_url", "sglang_base_url"),
            "SGLANG_BASE_URL",
            "http://127.0.0.1:30000/v1",
        ),
        "openai_compatible_local": (
            (
                "openai_compatible_local.base_url",
                "openai_compatible_local_base_url",
            ),
            "OPENAI_COMPATIBLE_LOCAL_BASE_URL",
            "",
        ),
    }
    keys, env_name, fallback = aliases.get(normalized, ((), "", ""))
    configured = str(_config_value(config, *keys, default="") or "").strip()
    endpoint = configured or str(os.getenv(env_name, "") or "").strip() or fallback
    api_key = str(
        _config_value(
            config,
            f"{normalized}.api_key",
            f"{normalized}_api_key",
            default="",
        )
        or ""
    ).strip()
    # A semantic privacy sidecar is a dedicated local boundary.  Never borrow
    # the primary hosted provider's credential here: doing so would make an
    # apparently local detector an accidental cloud request and could leak the
    # key into a local audit/log surface.  Local OpenAI-compatible servers may
    # still require an operator-owned token, so keep that token provider-scoped
    # (or use the harmless ``local`` sentinel at call time).
    if not api_key:
        api_key = str(
            os.getenv(
                {
                    "ollama": "OLLAMA_API_KEY",
                    "sglang": "SGLANG_API_KEY",
                    "openai_compatible_local": "OPENAI_COMPATIBLE_LOCAL_API_KEY",
                }.get(normalized, ""),
                "",
            )
            or ""
        ).strip()
    return endpoint, api_key


def _local_sidecar_model(config: Any | None, settings: PrivacyConfig) -> str:
    # ``local_model`` is deliberately an explicit privacy setting.  Falling
    # back to the primary provider/model (especially an Enterprise Qwen route)
    # would make the semantic detector inherit an unverified reasoning profile
    # and defeat the privacy boundary.  Provider model settings remain useful
    # for ordinary generation, never for this sidecar.
    return str(settings.local_model or "").strip()


def _local_sidecar_profile(
    config: Any | None,
    model: str,
) -> tuple[dict[str, Any], bool]:
    """Return an explicitly declared sidecar capability profile.

    Profiles can be supplied beside ``local_model`` using either the compact
    ``local_model_profile`` mapping or the individual capability aliases.  If
    a model is known to AoiTalk's local model registry, its profile is also
    considered authoritative.  The boolean indicates whether a profile was
    found at all; callers use this to avoid inventing a non-thinking wire.
    """

    explicit = _config_value(
        config,
        "external_model_privacy.local_model_profile",
        "external_model_privacy.sidecar_profile",
        default=None,
    )
    profile: dict[str, Any] = {}
    # Consult the canonical managed llama.cpp registry first.  Its capability
    # declaration lives under ``capabilities.reasoning`` rather than the
    # generic local-server profile helper (which only covers a subset of
    # platforms).
    if model:
        try:
            from ..llm.openai_compatible_local_profiles import (
                llama_cpp_model_profile,
                llama_cpp_profile_capabilities,
                local_server_profile_for_model,
            )

            known = llama_cpp_model_profile(model) or local_server_profile_for_model(model)
            if isinstance(known, Mapping):
                profile.update(dict(known))
                capabilities = llama_cpp_profile_capabilities(profile=known)
                if isinstance(capabilities, Mapping):
                    profile.setdefault("capabilities", dict(capabilities))
                    if "reasoning" in capabilities:
                        profile.setdefault("supports_reasoning", bool(capabilities["reasoning"]))
        except Exception:  # pragma: no cover - optional registry import
            pass
    if isinstance(explicit, Mapping):
        explicit_profile = dict(explicit)
        # A managed model profile is authoritative.  An operator overlay may
        # add metadata for an otherwise unknown model, but it cannot turn a
        # registry-declared reasoning model (for example Qwen3) into a
        # purported non-thinking detector.
        known_reasoning = profile.get("supports_reasoning")
        if known_reasoning is None:
            known_reasoning = profile.get("reasoning")
        if known_reasoning is None and isinstance(profile.get("capabilities"), Mapping):
            known_reasoning = profile["capabilities"].get("reasoning")
        explicit_reasoning = explicit_profile.get("supports_reasoning")
        if explicit_reasoning is None:
            explicit_reasoning = explicit_profile.get("reasoning")
        explicit_non_reasoning = explicit_profile.get("supports_non_reasoning")
        if explicit_non_reasoning is None:
            explicit_non_reasoning = explicit_profile.get("non_reasoning_supported")
        if isinstance(known_reasoning, str):
            known_reasoning = known_reasoning.strip().lower() in {"1", "true", "yes", "on"}
        if isinstance(explicit_reasoning, str):
            normalized = explicit_reasoning.strip().lower()
            explicit_reasoning = (
                normalized in {"1", "true", "yes", "on"}
                if normalized in {"0", "1", "true", "false", "yes", "no", "on", "off"}
                else explicit_reasoning
            )
        if isinstance(explicit_non_reasoning, str):
            normalized = explicit_non_reasoning.strip().lower()
            explicit_non_reasoning = (
                normalized in {"1", "true", "yes", "on"}
                if normalized in {"0", "1", "true", "false", "yes", "no", "on", "off"}
                else explicit_non_reasoning
            )
        if (
            bool(known_reasoning) is True
            and (
                explicit_reasoning is False
                or explicit_non_reasoning is True
                or any(key in explicit_profile for key in ("non_reasoning_wire", "reasoning_disable_wire", "wire"))
            )
        ):
            profile["_non_reasoning_profile_conflict"] = True
        else:
            profile.update(explicit_profile)
    # Accept a flat capability declaration for deployments that keep settings
    # in an environment-backed key/value store.
    for key in (
        "supports_non_reasoning",
        "non_reasoning_supported",
        "supports_reasoning",
        "reasoning",
        "reasoning_enabled",
        "non_reasoning_wire",
        "reasoning_disable_wire",
        "wire",
    ):
        value = _config_value(
            config,
            f"external_model_privacy.local_model_{key}",
            default=None,
        )
        if value is not None and key not in profile:
            profile[key] = value

    return profile, bool(profile)


def _sidecar_non_reasoning_wire(profile: Mapping[str, Any]) -> dict[str, Any] | None:
    """Resolve a formally declared disable-thinking request wire.

    The privacy detector is intentionally conservative: a profile must either
    provide an explicit, supported wire or explicitly declare that the model
    has no reasoning capability.  We never infer a wire from a model name or
    silently pass a provider-specific option that the profile did not claim.
    """

    for key in ("non_reasoning_wire", "reasoning_disable_wire", "wire"):
        value = profile.get(key)
        if isinstance(value, Mapping) and value:
            candidate = dict(value)
            # At present llama.cpp/Qwen-compatible local servers expose this
            # exact extra_body shape.  Reject arbitrary transport keys rather
            # than pretending they disable hidden reasoning.
            if candidate == {
                "chat_template_kwargs": {"enable_thinking": False}
            }:
                return candidate
            return None
    # A profile that explicitly declares reasoning disabled may use the common
    # Qwen/llama.cpp chat-template flag.  This fallback is still declaration
    # driven; no model-id heuristic is allowed to manufacture it.
    supports_non_reasoning = profile.get("supports_non_reasoning")
    if supports_non_reasoning is None:
        supports_non_reasoning = profile.get("non_reasoning_supported")
    if isinstance(supports_non_reasoning, str):
        supports_non_reasoning = (
            supports_non_reasoning.strip().lower() in {"1", "true", "yes", "on"}
            if supports_non_reasoning.strip().lower()
            in {"0", "1", "true", "false", "yes", "no", "on", "off"}
            else None
        )
    effort_supports_disable = profile.get("reasoning_effort_supports_disable")
    if isinstance(effort_supports_disable, str):
        effort_supports_disable = effort_supports_disable.strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
    if supports_non_reasoning is None and effort_supports_disable is True:
        supports_non_reasoning = True
    reasoning = profile.get("reasoning")
    supports_reasoning = profile.get("supports_reasoning")
    if reasoning is None and isinstance(profile.get("capabilities"), Mapping):
        reasoning = profile["capabilities"].get("reasoning")
    if supports_reasoning is None and isinstance(profile.get("capabilities"), Mapping):
        supports_reasoning = profile["capabilities"].get("reasoning")
    for name, value in (("reasoning", reasoning), ("supports_reasoning", supports_reasoning)):
        if isinstance(value, str):
            normalized = value.strip().lower()
            parsed = (
                normalized in {"1", "true", "yes", "on"}
                if normalized in {"0", "1", "true", "false", "yes", "no", "on", "off"}
                else None
            )
            if name == "reasoning":
                reasoning = parsed
            else:
                supports_reasoning = parsed
    if (
        supports_non_reasoning is True
        or reasoning is False
        or supports_reasoning is False
    ):
        return {"chat_template_kwargs": {"enable_thinking": False}}
    return None


_SEMANTIC_REASONING_FIELD_RE = re.compile(
    r"(?:reasoning|thinking|chain[_-]?of[_-]?thought|deliberation|thoughts?|analysis)",
    re.IGNORECASE,
)
# Values used solely to describe a multimodal wire are not user text.  Skip
# these literals in the semantic detector so ``raw_media_policy=confirm`` can
# still reach its explicit review callback when no textual content is present.
_SEMANTIC_PROTOCOL_LITERALS = frozenset(
    {
        "system",
        "user",
        "assistant",
        "tool",
        "function",
        "text",
        "input_text",
        "input_image",
        "input_audio",
        "image_url",
        "audio_url",
        "video_url",
        "image",
        "audio",
        "video",
        "media",
        "source",
        "content_block",
    }
)
_MISSING = object()


def _response_field(value: Any, name: str, default: Any = _MISSING) -> Any:
    """Read a field from an OpenAI object or a mapping without coercion."""

    if isinstance(value, Mapping):
        return value.get(name, default)
    try:
        return getattr(value, name)
    except Exception:  # noqa: BLE001
        return default


def _response_items(value: Any) -> Iterable[tuple[str, Any]]:
    """Yield serializable fields from SDK/Pydantic objects for safety checks."""

    if isinstance(value, Mapping):
        yield from ((str(key), item) for key, item in value.items())
        return
    try:
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            dumped = model_dump()
            if isinstance(dumped, Mapping):
                yield from ((str(key), item) for key, item in dumped.items())
                return
    except Exception:  # noqa: BLE001
        pass
    try:
        attributes = vars(value)
    except Exception:  # noqa: BLE001
        attributes = {}
    if isinstance(attributes, Mapping):
        yield from ((str(key), item) for key, item in attributes.items())


def _reject_reasoning_fields(value: Any, *, _seen: set[int] | None = None) -> None:
    """Reject any reasoning/thinking metadata anywhere in a model response."""

    seen = _seen if _seen is not None else set()
    identity = id(value)
    if identity in seen or value is None or isinstance(value, (str, bytes, bytearray)):
        return
    seen.add(identity)
    for key, item in _response_items(value):
        normalized = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
        if _SEMANTIC_REASONING_FIELD_RE.search(normalized):
            raise PrivacyError(
                f"semantic privacy sidecar returned reasoning field: {key}"
            )
        if isinstance(item, (Mapping, list, tuple, set)) or not isinstance(
            item, (str, bytes, bytearray, int, float, bool, type(None))
        ):
            if isinstance(item, (list, tuple, set)):
                for nested in item:
                    _reject_reasoning_fields(nested, _seen=seen)
            else:
                _reject_reasoning_fields(item, _seen=seen)


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """JSON decoder hook that rejects duplicate keys at every object level."""

    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise ValueError(f"duplicate JSON key: {key}")
        output[key] = value
    return output


def _parse_semantic_entities(
    content: str,
    source_text: str,
    *,
    max_entities: int,
) -> list[dict[str, str]]:
    """Parse and validate exactly one strict semantic entity document."""

    if not isinstance(content, str) or not content.strip():
        raise PrivacyError("semantic privacy sidecar returned empty output")
    content = content.strip()
    try:
        decoder = json.JSONDecoder(object_pairs_hook=_reject_duplicate_pairs)
        parsed, end = decoder.raw_decode(content)
    except Exception as exc:
        raise PrivacyError("semantic privacy sidecar returned malformed JSON") from exc
    if content[end:].strip():
        raise PrivacyError("semantic privacy sidecar returned extra data")
    return _validate_semantic_entities(
        parsed,
        source_text,
        max_entities=max_entities,
    )


def _validate_semantic_entities(
    parsed: Any,
    source_text: str,
    *,
    max_entities: int,
) -> list[dict[str, str]]:
    """Validate an already-decoded semantic envelope without coercion."""

    if not isinstance(parsed, Mapping) or set(parsed) != {"entities"}:
        raise PrivacyError("semantic privacy sidecar schema mismatch")
    entities = parsed.get("entities")
    if not isinstance(entities, list) or len(entities) > max_entities:
        raise PrivacyError("semantic privacy sidecar entities must be an array")
    normalized: list[dict[str, str]] = []
    seen_entities: set[tuple[str, str]] = set()
    for item in entities:
        if not isinstance(item, Mapping) or set(item) != {"text", "category"}:
            raise PrivacyError("semantic privacy sidecar entity schema mismatch")
        value = item.get("text")
        category = item.get("category")
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value) > 512
            or not isinstance(category, str)
            or not category.strip()
            or len(category) > 64
        ):
            raise PrivacyError("semantic privacy sidecar entity is invalid")
        if value not in source_text:
            raise PrivacyError("semantic privacy sidecar entity is not an exact substring")
        identity = (value, category)
        if identity in seen_entities:
            raise PrivacyError("semantic privacy sidecar entity is duplicated")
        seen_entities.add(identity)
        normalized.append({"text": value, "category": category})
    return normalized


def build_semantic_redactor(
    config: Any | None,
    settings: PrivacyConfig | None = None,
) -> Callable[..., Awaitable[Any]] | None:
    """Create a tool-free local sidecar callback for semantic extraction.

    ``None`` is returned only when semantic redaction is explicitly disabled.
    In protected mode, enabling semantic redaction without a dedicated local
    model returns a callback that fails closed; it must not silently become a
    deterministic/raw cloud path.  A configured but untrusted endpoint follows
    the same callback-failure behavior.
    """

    active = settings or privacy_config(config)
    if not active.semantic_redaction_enabled:
        return None
    model = _local_sidecar_model(config, active)
    if not model:
        async def _missing_sidecar(_text: str, *_args: Any) -> Any:
            raise PrivacyError(
                "semantic privacy sidecar local_model is not configured"
            )

        return _missing_sidecar
    provider = active.local_provider
    endpoint, api_key = _local_sidecar_endpoint(config, provider)
    classification = provider_classification(
        provider,
        base_url=endpoint,
        trusted_local_hosts=active.trusted_local_hosts,
    )

    async def _failed_sidecar(_text: str, *_args: Any) -> Any:
        raise PrivacyError(
            f"semantic privacy sidecar endpoint is not trusted ({provider}: {endpoint})"
        )

    if classification != "local":
        return _failed_sidecar

    profile, profile_known = _local_sidecar_profile(config, model)
    # A known reasoning-capable profile is not an acceptable privacy detector
    # unless its operator supplied an explicit non-reasoning capability.  A
    # reasoning-only response can omit the requested JSON while still returning
    # HTTP 200, which is indistinguishable from an unsafe detector failure.
    profile_reasoning = profile.get("supports_reasoning")
    if profile_reasoning is None:
        profile_reasoning = profile.get("reasoning")
    if profile_reasoning is None:
        profile_reasoning = profile.get("reasoning_enabled")
    if profile_reasoning is None and isinstance(profile.get("capabilities"), Mapping):
        profile_reasoning = profile["capabilities"].get("reasoning")
    if isinstance(profile_reasoning, str):
        normalized_reasoning = profile_reasoning.strip().lower()
        if normalized_reasoning in {"0", "false", "no", "off"}:
            profile_reasoning = False
        elif normalized_reasoning in {"1", "true", "yes", "on"}:
            profile_reasoning = True
    non_reasoning_wire = _sidecar_non_reasoning_wire(profile)

    async def _redact(text: str, requested_model: str = "") -> Any:
        # Import lazily to keep optional provider paths import-safe and to make
        # it explicit that this client bypasses the main outbound gateway only
        # after the endpoint was classified as trusted local.
        from openai import AsyncOpenAI

        # The sidecar transport has an explicit finite timeout and disables SDK
        # retries.  Protected payloads must fail closed promptly when the local
        # model is unavailable; an SDK retry could otherwise hold the parent
        # request open indefinitely and obscure the privacy decision.
        requested = str(requested_model or "").strip()
        # ``requested_model`` is accepted for backwards-compatible callback
        # signatures, but it may not switch the selected privacy model after
        # the profile/capability decision has been made.
        if requested and requested != model:
            raise PrivacyError("semantic privacy sidecar model override is not permitted")
        sidecar_model = model
        source_text = str(text or "")
        if len(source_text) > active.semantic_max_input_chars_per_payload:
            raise PrivacyError("semantic privacy sidecar input budget exceeded")
        if not profile_known:
            raise PrivacyError(
                "semantic privacy sidecar model has no declared capability profile"
            )
        if profile.get("_non_reasoning_profile_conflict"):
            raise PrivacyError(
                "semantic privacy sidecar profile conflicts with managed model capabilities"
            )
        if profile_known and profile_reasoning is not False and non_reasoning_wire is None:
            raise PrivacyError(
                "semantic privacy sidecar model has no declared non-reasoning profile"
            )
        timeout = httpx.Timeout(
            active.semantic_timeout_seconds,
            connect=min(2.0, active.semantic_timeout_seconds),
        )
        client = AsyncOpenAI(
            api_key=api_key or "local",
            base_url=endpoint,
            timeout=timeout,
            max_retries=0,
        )
        system = (
            "You are AoiTalk's local privacy detector. Return JSON only in the "
            '{"entities":[{"text":"...","category":"..."}]} format. '
            "Extract exact sensitive substrings from the user text. Never rewrite, "
            "summarize, delete, or call tools. If no entities exist, return an empty list."
        )
        request = {
            "model": sidecar_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": source_text},
            ],
            "temperature": 0,
            "max_tokens": active.semantic_max_output_tokens,
        }
        if non_reasoning_wire:
            request["extra_body"] = dict(non_reasoning_wire)
        try:
            schema = {
                "type": "json_schema",
                "json_schema": {
                    "name": "privacy_entities",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["entities"],
                        "properties": {
                            "entities": {
                                "type": "array",
                                "maxItems": active.semantic_max_entities_per_payload,
                                "items": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "required": ["text", "category"],
                                    "properties": {
                                        "text": {
                                            "type": "string",
                                            "minLength": 1,
                                            "maxLength": 512,
                                        },
                                        "category": {
                                            "type": "string",
                                            "minLength": 1,
                                            "maxLength": 64,
                                        },
                                    },
                                },
                            }
                        },
                    },
                },
            }
            try:
                response = await _await_bounded(
                    client.chat.completions.create(
                        **request,
                        response_format=schema,
                    ),
                    active.semantic_timeout_seconds,
                )
            except asyncio.TimeoutError as exc:
                raise PrivacyError("semantic privacy sidecar timed out") from exc
        finally:
            try:
                close = getattr(client, "close", None)
                if callable(close):
                    closed = close()
                    if inspect.isawaitable(closed):
                        # Client cleanup is part of the sidecar boundary too;
                        # a custom transport must not turn a finite semantic
                        # deadline into an unbounded close wait.
                        await _await_bounded(closed, 1.0)
            except Exception:
                pass
        # Chat completion responses must contain one non-reasoning choice and
        # a normal stop finish.  We intentionally do not fall back to
        # ``output_text``: accepting a second response shape can hide a
        # reasoning-only/tool/partial response from the privacy gate.
        _reject_reasoning_fields(response)
        choices = _response_field(response, "choices", _MISSING)
        if not isinstance(choices, (list, tuple)) or len(choices) != 1:
            raise PrivacyError("semantic privacy sidecar response choices mismatch")
        choice = choices[0]
        if _response_field(choice, "finish_reason", _MISSING) != "stop":
            raise PrivacyError("semantic privacy sidecar finish_reason must be stop")
        message = _response_field(choice, "message", _MISSING)
        content = _response_field(message, "content", _MISSING)
        if not isinstance(content, str):
            raise PrivacyError("semantic privacy sidecar returned empty output")
        return {
            "entities": _parse_semantic_entities(
                content,
                source_text,
                max_entities=active.semantic_max_entities_per_payload,
            )
        }

    return _redact


async def request_external_privacy_review(
    result: "PrivacyResult",
    *,
    provider: str,
    model: str | None = None,
    notify: bool = True,
) -> Mapping[str, Any] | None:
    """Bridge the gateway's review step to the existing WebUI dialog.

    The permission manager edits a text field, so textual payloads are encoded
    as JSON and decoded back before transport.  Binary media is never logged or
    copied into the dialog; an explicit approval simply retains the original
    in-memory value for the already-authorized ``raw_media_policy=confirm`` path.
    """

    from ..tools.external_llm_permission import request_external_model_prompt

    candidate_payload = (
        result.candidate_payload
        if result.candidate_payload is not None
        else result.payload
    )
    original_payload = (
        result.original_payload
        if result.original_payload is not None
        else candidate_payload
    )
    has_binary = _contains_raw_media(candidate_payload)
    if has_binary:
        prompt = "(raw media payload; bytes are withheld from the review editor)"
        redacted_prompt = prompt
    else:
        try:
            prompt = json.dumps(candidate_payload, ensure_ascii=False, sort_keys=True)
        except Exception:
            prompt = str(candidate_payload)
        redacted_prompt = prompt
    descriptor = getattr(result, "egress_descriptor", None)
    if isinstance(descriptor, EgressDescriptor):
        descriptor_values = descriptor.as_dict()
    elif isinstance(descriptor, Mapping):
        descriptor_values = {str(key): value for key, value in descriptor.items()}
    else:
        descriptor_values = {
            "action": str(result.source_kind or "external_egress"),
            "transport": "provider",
            "destination": str(provider or ""),
            "provider": str(provider or ""),
            "tool": "",
            "model": str(model or ""),
        }
    approved = await request_external_model_prompt(
        prompt,
        redacted_prompt=redacted_prompt,
        redaction_findings=[finding.as_dict() for finding in result.findings],
        provider=str(provider or ""),
        model=str(model or ""),
        description=(
            f"{result.source_kind} の外部送信 payload を確認してください "
            f"(risk={result.risk_level})"
        ),
        confirm=True,
        notify=notify,
        request_kind="external_data_review",
        source_kind=result.source_kind,
        risk_level=result.risk_level,
        semantic_status=result.semantic_status,
        warning=(
            "意味ベースの秘匿化に失敗しました。確認済みの秘匿版だけを送信してください。"
            if result.semantic_status == "failed"
            else ""
        ),
        # All external privacy reviews use the v2 transaction protocol.  The
        # manager keeps the legacy prompt path available for ordinary model
        # integrations, but a gateway review must never be suppressible by
        # AUTO_APPROVE or an omitted UI callback.
        egress_transaction=True,
        original_payload=original_payload,
        candidate_payload=candidate_payload,
        action=str(descriptor_values.get("action") or result.source_kind or "external_egress"),
        transport=str(descriptor_values.get("transport") or ""),
        destination=str(descriptor_values.get("destination") or ""),
        tool=str(descriptor_values.get("tool") or ""),
        contract_version=2,
    )
    if approved is None:
        return {"approved": False}
    # Embedded/legacy review callbacks may already return a structured
    # response mapping (the v2 manager returns the exact string instead).
    # Preserve that payload for compatibility while still normalizing the
    # authoritative final_payload field when present.
    if isinstance(approved, Mapping):
        # A review response is a protocol value, not a truthy flag.  Never
        # coerce ``1``/``"true"`` into approval, and do not allow an edited
        # media descriptor to escape the in-memory binding below.
        if type(approved.get("approved")) is not bool or approved.get("approved") is not True:
            return {"approved": False}
        if "final_payload" in approved:
            final_value = approved.get("final_payload")
            if not isinstance(final_value, str):
                return {"approved": False}
            if has_binary:
                from ..tools.external_llm_permission import _safe_event_payload_text

                if final_value != _safe_event_payload_text(candidate_payload):
                    return {"approved": False}
                normalized_response = {
                    "approved": True,
                    "final_payload": final_value,
                    "payload": candidate_payload,
                    "media_projection_approved": True,
                }
                for key in (
                    "request_id",
                    "contract_version",
                    "review_nonce",
                    "binding_digest",
                    "final_binding_digest",
                ):
                    if key in approved:
                        normalized_response[key] = approved[key]
                return normalized_response
            normalized_response = {
                "approved": True,
                "final_payload": final_value,
                "payload": approved.get("payload"),
            }
            # Preserve the server-generated v2 binding attestation so the
            # gateway can verify the exact editor string immediately before
            # decoding/revalidating it.  These fields contain only digests
            # and identifiers; raw payloads remain in-memory on this side.
            for key in (
                "request_id",
                "contract_version",
                "review_nonce",
                "binding_digest",
                "final_binding_digest",
            ):
                if key in approved:
                    normalized_response[key] = approved[key]
            return normalized_response
        if "payload" in approved:
            if has_binary and approved.get("payload") != candidate_payload:
                return {"approved": False}
            if has_binary:
                from ..tools.external_llm_permission import _safe_event_payload_text

                normalized_response = {
                    "approved": True,
                    "payload": candidate_payload,
                    "final_payload": _safe_event_payload_text(candidate_payload),
                    "media_projection_approved": True,
                }
                for key in (
                    "request_id",
                    "contract_version",
                    "review_nonce",
                    "binding_digest",
                    "final_binding_digest",
                ):
                    if key in approved:
                        normalized_response[key] = approved[key]
                return normalized_response
            return {
                "approved": True,
                "payload": approved.get("payload"),
            }
    if has_binary:
        # A raw media candidate is withheld from the editor.  The v2 manager
        # still requires an exact final_payload string; preserve the approved
        # in-memory candidate as JSON only for the callback result and let the
        # gateway decode it before sender invocation.
        from ..tools.external_llm_permission import _safe_event_payload_text

        # The only safe editable value for a media review is the unchanged
        # descriptor projection.  Any edited descriptor would no longer bind
        # to the in-memory bytes and is denied rather than sent as a marker.
        if str(approved) != _safe_event_payload_text(candidate_payload):
            return {"approved": False}
        normalized_response = {
            "approved": True,
            "final_payload": str(approved),
            "payload": candidate_payload,
            "media_projection_approved": True,
        }
        for key in (
            "request_id",
            "review_nonce",
            "binding_digest",
            "final_binding_digest",
        ):
            value = getattr(approved, key, None)
            if isinstance(value, str) and value:
                normalized_response[key] = value
        return normalized_response
    # The manager returns the exact final_payload string.  Do not replace it
    # with the candidate when it is empty or otherwise malformed; the gateway
    # treats a non-JSON scalar as an intentional textual payload.
    try:
        edited = json.loads(str(approved))
    except Exception:
        edited = approved
    normalized_response = {
        "approved": True,
        "final_payload": str(approved),
        "payload": edited,
    }
    # ``EgressApproval`` is a string-compatible result returned by the v2
    # permission manager.  Carry its non-sensitive binding metadata through
    # the gateway without changing the legacy string-facing API.
    for key in (
        "request_id",
        "review_nonce",
        "binding_digest",
        "final_binding_digest",
    ):
        value = getattr(approved, key, None)
        if isinstance(value, str) and value:
            normalized_response[key] = value
    return normalized_response


def effective_privacy_mode(
    config: Any | None = None,
    *,
    session_context: Mapping[str, Any] | None = None,
    project_metadata: Mapping[str, Any] | None = None,
) -> str:
    """Resolve global/session/project mode without allowing a weaker override.

    ``direct < protected < local_only`` is intentionally enforced here rather
    than in UI code, so mobile/CLI callers that omit a mode cannot bypass a
    project policy.
    """

    values = [privacy_config(config).mode]
    project = project_metadata.get("privacy_mode") if isinstance(project_metadata, Mapping) else None
    session = session_context.get("privacy_mode") if isinstance(session_context, Mapping) else None
    for value in (project, session):
        normalized = str(value or "").strip().lower()
        if normalized in {"direct", "protected", "local_only"}:
            values.append(normalized)
    rank = {"direct": 0, "protected": 1, "local_only": 2}
    return max(values, key=lambda item: rank.get(item, 0))


def _host_is_trusted_local(host: str, trusted_hosts: Iterable[str] = ()) -> bool:
    normalized = str(host or "").strip().rstrip(".").lower()
    if not normalized:
        return False
    trusted = {str(item).strip().rstrip(".").lower() for item in trusted_hosts if str(item).strip()}
    if normalized in trusted:
        return True
    if normalized in {"localhost", "localhost.localdomain"}:
        return True
    try:
        # Private RFC1918/ULA addresses are not implicitly trusted.  A local
        # network can contain an attacker-controlled service, so only an
        # actual loopback address (or an explicitly configured host above) is
        # safe by default.
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def provider_classification(
    provider: str,
    *,
    base_url: str | None = None,
    trusted_local_hosts: Iterable[str] = (),
) -> str:
    """Return ``local``, ``external`` or ``unknown`` for the resolved route.

    A provider name alone never makes an ``openai_compatible_local`` endpoint
    safe: a URL must point at loopback/trusted host.  Private LAN/ULA hosts
    require an explicit ``trusted_local_hosts`` entry.
    """

    normalized = str(provider or "").strip().lower().replace("_", "-")
    if normalized in {item.replace("_", "-") for item in EXTERNAL_PROVIDER_IDS}:
        return "external"
    if normalized in {item.replace("_", "-") for item in LOCAL_PROVIDER_IDS}:
        raw_url = str(base_url or "").strip()
        if not raw_url:
            # Local adapters with no network endpoint (Ollama/Mage-VL/STT)
            # remain local; OpenAI-compatible/SGLang need an explicit URL.
            return (
                "local"
                if normalized in {
                    "ollama",
                    "mage-vl",
                    "speech-recognition",
                    "aivisspeech",
                    "voicevox",
                    "comfyui",
                    "hydrus",
                }
                else "unknown"
            )
        try:
            host = urlsplit(raw_url).hostname or ""
        except ValueError:
            return "unknown"
        return "local" if _host_is_trusted_local(host, trusted_local_hosts) else "external"
    return "unknown"


def is_external_provider(provider: str, *, base_url: str | None = None, trusted_local_hosts: Iterable[str] = ()) -> bool:
    return provider_classification(provider, base_url=base_url, trusted_local_hosts=trusted_local_hosts) != "local"


def _payload_hash(value: Any) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    except Exception:  # noqa: BLE001
        encoded = repr(value).encode("utf-8", "replace")
    return hashlib.sha256(encoded).hexdigest()


def _snapshot_payload(value: Any) -> Any:
    """Capture the pre-transform payload without mutating caller-owned data."""

    try:
        return copy.deepcopy(value)
    except Exception:
        # Some provider SDK objects are not deepcopyable.  Keep an immutable
        # serialized scalar for binding/review rather than allowing a mutable
        # object to become the apparent "original" after transformation.
        try:
            return json.loads(json.dumps(value, ensure_ascii=False, default=str))
        except Exception:
            return repr(value)


def _is_plain_base64(value: Any) -> bool:
    """Return true for a standalone base64 blob, not ordinary text.

    Provider payloads (notably OpenAI ``input_audio``/``image`` blocks) often
    carry bytes in a plain ``data`` field instead of a data URL.  Restrict this
    detector to canonical base64 and require at least three decoded bytes so
    short identifiers and empty strings are not treated as media.
    """

    if not isinstance(value, str):
        return False
    encoded = value.strip()
    if len(encoded) < 4 or len(encoded) % 4:
        return False
    if not _BASE64_RE.fullmatch(encoded):
        return False
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except Exception:
        return False
    return len(decoded) >= 3


def _is_media_value(value: Any) -> bool:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return True
    if isinstance(value, str) and _DATA_URL_RE.match(value.strip()):
        return True
    return False


_MAX_PAYLOAD_DEPTH = 64
_MAX_PAYLOAD_NODES = 10_000


def _contains_raw_media(value: Any, *, media_context: bool = False) -> bool:
    """Detect binary/data-URL/base64 media before cache lookup or redaction.

    Payloads originate at provider/tool boundaries and are not necessarily
    JSON trees: an embedding may hand us a cyclic mapping or an accidentally
    enormous list.  Walk iteratively with bounded depth/node budgets so the
    privacy gate fails closed instead of recursing forever or turning a
    malformed request into a CPU/memory denial of service.
    """

    stack: list[tuple[Any, bool, int, frozenset[int]]] = [
        (value, media_context, 0, frozenset())
    ]
    visited_nodes = 0
    while stack:
        current, current_media_context, depth, ancestors = stack.pop()
        visited_nodes += 1
        if visited_nodes > _MAX_PAYLOAD_NODES:
            raise PrivacyError("privacy payload node budget exceeded")
        if depth > _MAX_PAYLOAD_DEPTH:
            raise PrivacyError("privacy payload depth exceeded")
        if _is_media_value(current):
            return True
        if isinstance(current, Mapping):
            identity = id(current)
            if identity in ancestors:
                raise PrivacyError("privacy payload cycle detected")
            next_ancestors = ancestors | {identity}
            # A plain ``data`` field is media when nested below a provider
            # media block (OpenAI input_audio/image, Anthropic source, etc.).
            # Explicit *_base64 keys are always media, even when a provider
            # omits the block type and sends a compact payload.
            for key, item in current.items():
                normalized_key = str(key or "").strip().lower().replace("-", "_")
                child_context = current_media_context or normalized_key in _MEDIA_CONTAINER_KEYS
                if normalized_key in _MEDIA_BASE64_KEYS and _is_plain_base64(item):
                    return True
                if normalized_key == "data" and _is_plain_base64(item):
                    return True
                stack.append((item, child_context, depth + 1, next_ancestors))
            continue
        if isinstance(current, (list, tuple, set, frozenset)):
            identity = id(current)
            if identity in ancestors:
                raise PrivacyError("privacy payload cycle detected")
            next_ancestors = ancestors | {identity}
            for item in current:
                stack.append((item, current_media_context, depth + 1, next_ancestors))
    return False


@dataclass
class _SemanticBudget:
    """Per-protected-payload budget shared by all semantic callback calls."""

    deadline: float
    calls: int = 0
    input_chars: int = 0
    entities: int = 0


class OutboundPrivacyGateway:
    """Session-scoped gateway used immediately before provider transport."""

    _cache: "OrderedDict[tuple[Any, ...], tuple[str, tuple[tuple[str, str], ...]]]" = OrderedDict()
    _cache_limit = 256

    def __init__(
        self,
        config: Any | None = None,
        *,
        session_id: str | None = None,
        user_id: str | None = None,
        semantic_redactor: Callable[..., Any] | None = None,
        review_callback: Callable[..., Any] | None = None,
        session_context: Mapping[str, Any] | None = None,
        project_metadata: Mapping[str, Any] | None = None,
    ) -> None:
        self.config = config
        inherited = get_privacy_policy_context()
        if session_context is None:
            session_context = inherited.session_context
        if project_metadata is None:
            project_metadata = inherited.project_metadata
        self.session_context = dict(session_context) if isinstance(session_context, Mapping) else None
        self.project_metadata = dict(project_metadata) if isinstance(project_metadata, Mapping) else None
        self.settings = privacy_config(config)
        self.settings = replace(
            self.settings,
            mode=effective_privacy_mode(
                config,
                session_context=self.session_context,
                project_metadata=self.project_metadata,
            ),
        )
        self.session_id = str(session_id or "")
        self.user_id = str(user_id or "")
        self.semantic_redactor = semantic_redactor
        self.review_callback = review_callback
        self._raw_to_alias: dict[str, str] = {}
        self._alias_to_raw: dict[str, str] = {}
        self._counters: dict[str, int] = {}
        self.audit: list[dict[str, Any]] = []
        self._semantic_redactor = semantic_redactor
        if self._semantic_redactor is None:
            self._semantic_redactor = build_semantic_redactor(config, self.settings)
        # Keep the public attribute backwards-compatible for test/embedding
        # adapters that inspect or replace the callback after construction.
        self.semantic_redactor = self._semantic_redactor

    @property
    def mode(self) -> str:
        return self.settings.mode

    async def materialize_one_way(
        self,
        payload: Any,
        *,
        source_kind: str = "masking",
        model: str | None = None,
        semantic_exempt_values: Iterable[str] | None = None,
    ) -> PrivacyResult:
        """Materialize a permanently masked projection of ``payload``.

        The ordinary gateway is intentionally reversible: ``protect`` keeps a
        request-local alias table so provider responses/tool arguments can be
        restored before they return to the user.  A manual ``/masking`` turn is
        a different lifecycle.  Its output is a user-facing artifact and must
        never be restored (or be affected by a previous turn's aliases), even
        when the application's configured privacy mode is ``direct``.

        This method therefore runs the existing deterministic and semantic
        transformation path in an isolated, review-free, non-caching mode.
        The synthetic provider label is deliberately *not* a real transport;
        no sender is invoked here.  A caller should create one fresh gateway
        for each masking invocation and retain the returned projection only.
        Semantic sidecar errors propagate through the existing fail-closed
        ``PrivacyError`` contract.
        """

        previous = self.settings
        # ``protect`` skips transformation for ``direct`` mode and trusted
        # local providers.  Force the protected branch with a non-transport
        # label while preserving all configured redaction/sidecar settings.
        # Review and cache are deliberately disabled: a masking artifact is
        # never a provider egress transaction and must not be served from a
        # reusable alias cache.
        self.settings = replace(
            self.settings,
            mode="protected",
            review_policy="never",
            cache_enabled=False,
        )
        try:
            result = await self.protect(
                payload,
                provider="masking_local",
                source_kind=source_kind,
                model=model,
                descriptor=EgressDescriptor(
                    action=source_kind or "masking",
                    transport="local_materialization",
                    destination="local",
                    provider="masking_local",
                    model=str(model or ""),
                ),
                semantic_exempt_values=semantic_exempt_values,
            )
        finally:
            self.settings = previous
        # ``protect`` already raises when semantic detection fails.  Keep this
        # explicit guard so a future implementation cannot accidentally expose
        # an uncertain result by returning a failed status.
        if result.semantic_status == "failed":
            raise PrivacyError("semantic privacy redaction failed")
        return result

    def materialize_one_way_sync(
        self,
        payload: Any,
        *,
        source_kind: str = "masking",
        model: str | None = None,
        semantic_exempt_values: Iterable[str] | None = None,
    ) -> PrivacyResult:
        """Synchronous bridge for file/CLI callers.

        ``protect_sync`` cannot be used directly because its keyword arguments
        are forwarded to ``protect`` and this method needs the temporary
        forced-protected settings above.  Run the coroutine in a bounded
        daemon thread when an event loop is already active, matching the
        gateway's existing synchronous privacy bridge behaviour.
        """

        coroutine = self.materialize_one_way(
            payload,
            source_kind=source_kind,
            model=model,
            semantic_exempt_values=semantic_exempt_values,
        )
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coroutine)

        future: concurrent.futures.Future[PrivacyResult] = concurrent.futures.Future()

        def run() -> None:
            try:
                future.set_result(asyncio.run(coroutine))
            except BaseException as exc:  # noqa: BLE001
                try:
                    future.set_exception(exc)
                except concurrent.futures.InvalidStateError:
                    pass

        threading.Thread(
            target=run,
            name="aoitalk-mask-materializer",
            daemon=True,
        ).start()
        try:
            # Sidecar limits are finite; retain a small bridge margin for
            # callback cleanup and never wait forever on a broken detector.
            timeout = max(
                float(self.settings.semantic_total_timeout_seconds),
                float(self.settings.semantic_callback_timeout_seconds),
                1.0,
            ) + 2.0
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise PrivacyError("privacy masking timed out") from exc

    def update_policy_context(
        self,
        *,
        session_context: Mapping[str, Any] | None = None,
        project_metadata: Mapping[str, Any] | None = None,
    ) -> None:
        """Refresh effective policy while retaining this session's aliases.

        ``None`` is an explicit empty scope when metadata is supplied by a
        caller.  A no-argument refresh inherits the current contextvar and
        *replaces* both fields, including clearing stale values when the
        active turn has no project/session metadata.  This prevents a shared
        provider gateway from leaking the previous user's policy into a new
        turn while preserving reversible aliases for the gateway instance.
        """

        if session_context is None and project_metadata is None:
            inherited = get_privacy_policy_context()
            session_context = inherited.session_context
            project_metadata = inherited.project_metadata
        self.session_context = (
            dict(session_context) if isinstance(session_context, Mapping) else None
        )
        self.project_metadata = (
            dict(project_metadata) if isinstance(project_metadata, Mapping) else None
        )
        self.settings = replace(
            self.settings,
            mode=effective_privacy_mode(
                self.config,
                session_context=self.session_context,
                project_metadata=self.project_metadata,
            ),
        )

    def provider_class(self, provider: str, base_url: str | None = None) -> str:
        return provider_classification(
            provider,
            base_url=base_url,
            trusted_local_hosts=self.settings.trusted_local_hosts,
        )

    def ensure_provider_allowed(self, provider: str, *, base_url: str | None = None) -> str:
        classification = self.provider_class(provider, base_url)
        if self.settings.mode == "local_only" and classification != "local":
            raise ExternalProviderBlocked(
                f"external provider '{provider}' is blocked by local_only privacy mode"
            )
        return classification

    def _placeholder(self, category: str, raw: str) -> str:
        existing = self._raw_to_alias.get(raw)
        if existing:
            return existing
        normalized_category = re.sub(r"[^A-Z0-9]+", "_", category.upper()).strip("_") or "VALUE"
        self._counters[normalized_category] = self._counters.get(normalized_category, 0) + 1
        alias = f"[AOI_{normalized_category}_{self._counters[normalized_category]}]"
        self._raw_to_alias[raw] = alias
        self._alias_to_raw[alias] = raw
        return alias

    def _replace_pattern(self, text: str, pattern: re.Pattern[str], category: str, findings: list[RedactionFinding]) -> str:
        count = 0

        def replace(match: re.Match[str]) -> str:
            nonlocal count
            count += 1
            value = match.group(0)
            if category == "SECRET" and match.groupdict().get("prefix"):
                prefix = match.group("prefix")
                raw_value = match.groupdict().get("value") or value
                quote = ""
                if len(raw_value) >= 2 and raw_value[0] in {'"', "'"} and raw_value[-1] == raw_value[0]:
                    quote = raw_value[0]
                    # Bind the actual credential body, not its JSON quoting,
                    # so local alias restoration cannot manufacture malformed
                    # quotes in a returned advisory.
                    raw_value = raw_value[1:-1]
                alias = self._placeholder(category, raw_value)
                return f"{prefix}{quote}{alias}{quote}"
            return self._placeholder(category, value)

        result = pattern.sub(replace, text)
        if count:
            # Keep a single finding per category/alias while retaining count.
            aliases = [alias for raw, alias in self._raw_to_alias.items() if alias.startswith(f"[AOI_{category}_")]
            findings.extend(RedactionFinding(category, alias, 1) for alias in aliases[-count:])
        return result

    def _deterministic_redact(self, text: str) -> tuple[str, list[RedactionFinding]]:
        findings: list[RedactionFinding] = []
        redacted = text
        for pattern, category in (
            # Basic auth must precede the generic labelled-secret matcher so
            # ``Authorization: Basic <base64>`` cannot leave the credential
            # suffix visible after only ``Authorization: Basic`` is masked.
            (_BASIC_AUTH_RE, "SECRET"),
            # Bearer/JWT/AWS forms must likewise run before the generic
            # ``authorization: value`` matcher; otherwise only the scheme is
            # consumed as a labelled value and the token suffix survives.
            (_BEARER_RE, "SECRET"),
            (_JWT_RE, "JWT"),
            (_AWS_ACCESS_KEY_RE, "AWS_ACCESS_KEY"),
            (_SECRET_RE, "SECRET"),
            (_EMAIL_RE, "EMAIL"),
            (_PRIVATE_IP_RE, "INTERNAL_HOST"),
            (_URL_RE, "INTERNAL_URL"),
            (_WINDOWS_PATH_RE, "LOCAL_PATH"),
            (_UNIX_PATH_RE, "LOCAL_PATH"),
        ):
            redacted = self._replace_pattern(redacted, pattern, category, findings)
        for term in self.settings.redaction_terms:
            if term and term in redacted:
                alias = self._placeholder("CONFIDENTIAL_TERM", term)
                redacted = redacted.replace(term, alias)
                findings.append(RedactionFinding("CONFIDENTIAL_TERM", alias))
        # Deduplicate findings without changing first-seen order.
        unique: list[RedactionFinding] = []
        seen: set[tuple[str, str]] = set()
        for finding in findings:
            key = (finding.category, finding.placeholder)
            if key not in seen:
                seen.add(key)
                unique.append(finding)
        return redacted, unique

    async def _semantic_entities(
        self,
        text: str,
        *,
        budget: _SemanticBudget | None = None,
    ) -> tuple[list[dict[str, str]], str]:
        callback = self.semantic_redactor
        if callback is None:
            candidate = _get(self.config, "external_model_privacy.semantic_redactor", None)
            if callable(candidate):
                callback = candidate
        if not self.settings.semantic_redaction_enabled or not callback:
            return [], "disabled"
        try:
            if budget is not None:
                now = asyncio.get_running_loop().time()
                if now >= budget.deadline:
                    raise PrivacyError("semantic privacy sidecar aggregate timeout exceeded")
                if budget.calls >= self.settings.semantic_max_calls_per_payload:
                    raise PrivacyError("semantic privacy sidecar call budget exceeded")
                input_chars = len(text)
                if (
                    input_chars > self.settings.semantic_max_input_chars_per_payload
                    or budget.input_chars + input_chars
                    > self.settings.semantic_max_input_chars_per_payload
                ):
                    raise PrivacyError("semantic privacy sidecar input budget exceeded")
                budget.calls += 1
                budget.input_chars += input_chars
            # Inspect the callback signature instead of retrying on a
            # ``TypeError``.  A callback may itself raise TypeError while
            # parsing/transporting the model response; retrying would create a
            # second sidecar request and make failure semantics nondeterministic.
            accepts_model = False
            try:
                signature = inspect.signature(callback)
                positional = [
                    parameter
                    for parameter in signature.parameters.values()
                    if parameter.kind
                    in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
                ]
                accepts_model = any(
                    parameter.kind == inspect.Parameter.VAR_POSITIONAL
                    for parameter in signature.parameters.values()
                ) or len(positional) >= 2
            except (TypeError, ValueError):
                accepts_model = False
            timeout = self.settings.semantic_callback_timeout_seconds
            if budget is not None:
                timeout = min(
                    timeout,
                    max(0.0, budget.deadline - asyncio.get_running_loop().time()),
                )
            if timeout <= 0:
                raise PrivacyError("semantic privacy sidecar aggregate timeout exceeded")

            callback_args = (text, self.settings.local_model) if accepts_model else (text,)

            async def invoke_callback() -> Any:
                # Async callbacks stay on the event loop; synchronous
                # callbacks run in a short-lived daemon worker so the outer
                # timeout also applies to a blocking injected detector.
                # ``asyncio.to_thread`` uses the event loop's default
                # executor; ``asyncio.run`` waits for that executor during
                # shutdown, which would turn a nominal 100ms privacy budget
                # into an unbounded wait when a detector hangs.  A daemon
                # thread lets the caller fail closed promptly.  The callback
                # still receives a copied ContextVar context, and late
                # completion is discarded once the awaiting Future is done.
                is_async = inspect.iscoroutinefunction(callback) or inspect.iscoroutinefunction(
                    getattr(callback, "__call__", None)
                )
                if is_async:
                    candidate = callback(*callback_args)
                else:
                    loop = asyncio.get_running_loop()
                    future: asyncio.Future[Any] = loop.create_future()
                    callback_context = contextvars.copy_context()

                    def complete_result(value: Any = None, error: BaseException | None = None) -> None:
                        if future.done():
                            return
                        if error is not None:
                            future.set_exception(error)
                        else:
                            future.set_result(value)

                    def run_sync_callback() -> None:
                        try:
                            value = callback_context.run(callback, *callback_args)
                        except BaseException as exc:  # noqa: BLE001
                            try:
                                loop.call_soon_threadsafe(complete_result, None, exc)
                            except RuntimeError:
                                # The event loop may have been closed after a
                                # timeout; the daemon thread must not revive
                                # it or log an unhandled callback exception.
                                pass
                            return
                        try:
                            loop.call_soon_threadsafe(complete_result, value, None)
                        except RuntimeError:
                            pass

                    threading.Thread(
                        target=run_sync_callback,
                        name="aoitalk-privacy-sidecar",
                        daemon=True,
                    ).start()
                    candidate = await future
                if inspect.isawaitable(candidate):
                    return await candidate
                return candidate

            try:
                result = await _await_bounded(invoke_callback(), timeout)
            except asyncio.TimeoutError as exc:
                raise PrivacyError("semantic privacy sidecar callback timed out") from exc
            if isinstance(result, str):
                result = _parse_semantic_entities(
                    result,
                    text,
                    max_entities=self.settings.semantic_max_entities_per_payload,
                )
            elif isinstance(result, Mapping):
                # Mapping callbacks are a supported embedding/test seam.  Do
                # not JSON-coerce tuples/sets into lists: the callback result
                # itself must already match the strict schema.
                result = _validate_semantic_entities(
                    result,
                    text,
                    max_entities=self.settings.semantic_max_entities_per_payload,
                )
            else:
                raise PrivacyError("semantic sidecar envelope schema mismatch")
            normalized = result
            if budget is not None:
                if budget.entities + len(normalized) > self.settings.semantic_max_entities_per_payload:
                    raise PrivacyError("semantic privacy sidecar entity budget exceeded")
                budget.entities += len(normalized)
            return normalized, "success"
        except Exception:  # noqa: BLE001
            # Detector/provider exception text may echo protected input or
            # endpoint credentials.  Keep server diagnostics deliberately
            # non-sensitive; callers receive only the stable failed status.
            logger.warning("semantic privacy redactor failed")
            return [], "failed"

    async def _revalidate_final_payload(
        self,
        final_payload: Any,
        *,
        candidate_payload: Any,
        semantic_exempt_values: frozenset[str] = frozenset(),
    ) -> Any:
        """Validate the exact value selected by a human review.

        Review editors operate on a textual projection of the protected
        candidate.  A malicious/stale client can nevertheless submit a new
        value containing a credential, PII, or media that was not present in
        the candidate.  The review callback is therefore not the final trust
        boundary: before transport, run the same deterministic/semantic
        checks again and fail closed when the selected value would require a
        different protected projection.  Returning a newly masked value here
        would silently change the value the user approved, so edits are
        rejected rather than auto-mutated.
        """

        # Binary/media values are intentionally never editable.  Preserve the
        # historical explicit-confirm path only when the final value is an
        # exact structural match for the candidate captured before review.
        try:
            candidate_has_media = _contains_raw_media(candidate_payload)
            final_has_media = _contains_raw_media(final_payload)
        except PrivacyError as exc:
            raise PrivacyReviewDenied(
                "external egress final payload failed privacy validation"
            ) from exc
        if candidate_has_media or final_has_media:
            try:
                same_payload = final_payload == candidate_payload
            except Exception:
                same_payload = _payload_hash(final_payload) == _payload_hash(candidate_payload)
            if not same_payload:
                raise PrivacyReviewDenied(
                    "external egress final payload changed a protected media value"
                )
            return _snapshot_payload(final_payload)

        # Generic review bridges may wrap the candidate in a documented
        # envelope (for example ``{"edited": <candidate>}``), but an
        # unreviewed mapping field must never be appended after approval.  A
        # raw string in such a new field can evade deterministic/semantic
        # scanners when the sidecar is disabled, so reject additions unless
        # the value is an exact copy of the reviewed candidate snapshot.
        def has_unreviewed_mapping_addition(final: Any, candidate: Any) -> bool:
            if not isinstance(final, Mapping) or not isinstance(candidate, Mapping):
                return False
            for key, item in final.items():
                if key not in candidate:
                    if item == candidate:
                        # A compatibility envelope may intentionally wrap the
                        # entire reviewed candidate without adding content.
                        continue
                    return True
                if has_unreviewed_mapping_addition(item, candidate[key]):
                    return True
            return False

        if has_unreviewed_mapping_addition(final_payload, candidate_payload):
            raise PrivacyReviewDenied(
                "external egress final payload added an unreviewed field"
            )

        # An unchanged, immutable candidate already passed the full initial
        # semantic transformation.  Re-run deterministic checks below, but do
        # not invoke the flaky/expensive semantic sidecar a second time for
        # every JSON leaf.  The comparison is against the pre-review deep
        # snapshot, so a callback mutation cannot take this path.
        candidate_unchanged = False
        try:
            candidate_unchanged = final_payload == candidate_payload
        except Exception:
            candidate_unchanged = _payload_hash(final_payload) == _payload_hash(candidate_payload)

        budget: _SemanticBudget | None = None
        if self.settings.semantic_redaction_enabled and self.semantic_redactor is not None:
            budget = _SemanticBudget(
                deadline=(
                    asyncio.get_running_loop().time()
                    + self.settings.semantic_total_timeout_seconds
                )
            )
        ancestors: set[int] = set()
        nodes = 0

        if candidate_unchanged:
            candidate_strings: list[str] = []

            def collect_candidate_strings(value: Any) -> None:
                if isinstance(value, str):
                    candidate_strings.append(value)
                elif isinstance(value, Mapping):
                    for key, item in list(value.items())[:_MAX_PAYLOAD_NODES]:
                        if isinstance(key, str):
                            candidate_strings.append(key)
                        collect_candidate_strings(item)
                elif isinstance(value, (list, tuple, set, frozenset)):
                    for item in list(value)[:_MAX_PAYLOAD_NODES]:
                        collect_candidate_strings(item)

            collect_candidate_strings(candidate_payload)
            semantic_exempt_values = frozenset(
                (*semantic_exempt_values, *candidate_strings)
            )

        def neutralize_known_alias_assignments(text: str) -> tuple[str, dict[str, str]]:
            """Hide gateway-owned aliases from a second deterministic scan.

            Revalidation must still reject attacker-supplied ``[AOI_*]`` or
            ``[WF_*]`` lookalikes.  Only aliases minted by this gateway's
            private raw-to-alias table are neutralized, and the surrounding
            credential label is included so ``password=[AOI_SECRET_1]`` is
            not mistaken for a newly supplied secret on the second pass.
            """

            replacements: dict[str, str] = {}
            candidate = text
            labels = (
                "api[_ -]?key",
                "access[_ -]?token",
                "refresh[_ -]?token",
                "auth[_ -]?token",
                "session[_ -]?token",
                "client[_ -]?secret",
                "saml[_ -]?response",
                "relay[_ -]?state",
                "secret",
                "token",
                "password",
                "passwd",
                "credential",
                "authorization",
                "cookie",
            )
            prefix = rf"(?i)\b(?:{'|'.join(labels)})\s*[:=]\s*"
            for index, alias in enumerate(
                sorted(self._alias_to_raw, key=len, reverse=True),
                start=1,
            ):
                marker = f"__AOITALK_SAFE_ASSIGN_{index}__"
                pattern = re.compile(prefix + re.escape(alias))
                match = pattern.search(candidate)
                if match:
                    candidate = pattern.sub(marker, candidate)
                    replacements[marker] = match.group(0)
            return candidate, replacements

        def restore_known_alias_assignments(text: str, replacements: Mapping[str, str]) -> str:
            result = text
            for marker, alias in replacements.items():
                result = result.replace(marker, alias)
            return result

        async def walk(value: Any, *, depth: int = 0) -> Any:
            nonlocal nodes
            nodes += 1
            if nodes > _MAX_PAYLOAD_NODES:
                raise PrivacyReviewDenied(
                    "external egress final payload exceeded privacy limits"
                )
            if depth > _MAX_PAYLOAD_DEPTH:
                raise PrivacyReviewDenied(
                    "external egress final payload exceeded privacy limits"
                )
            if _is_media_value(value):
                raise PrivacyReviewDenied(
                    "external egress final payload contains raw media"
                )
            if isinstance(value, str):
                if value in semantic_exempt_values:
                    # Workflow-attested protocol strings (including the exact
                    # serialized query) already contain opaque aliases.  Do
                    # not remask them on revalidation; ordinary caller text
                    # still takes the strict deterministic path below.
                    scan_value, alias_assignments = value, {}
                    redacted = value
                else:
                    scan_value, alias_assignments = neutralize_known_alias_assignments(value)
                    redacted, _findings = self._deterministic_redact(scan_value)
                    redacted = restore_known_alias_assignments(redacted, alias_assignments)
                if redacted != value:
                    raise PrivacyReviewDenied(
                        "external egress final payload failed redaction revalidation"
                    )
                if self.settings.semantic_redaction_enabled and self.semantic_redactor:
                    if value.strip().lower() not in (
                        _MEDIA_CONTAINER_KEYS | _SEMANTIC_PROTOCOL_LITERALS
                    ) and value not in semantic_exempt_values:
                        entities, status = await self._semantic_entities(
                            scan_value,
                            budget=budget,
                        )
                        if status == "failed" or entities:
                            raise PrivacyReviewDenied(
                                "external egress final payload failed semantic revalidation"
                            )
                return value
            if isinstance(value, Mapping):
                identity = id(value)
                if identity in ancestors:
                    raise PrivacyReviewDenied(
                        "external egress final payload contains a cycle"
                    )
                ancestors.add(identity)
                try:
                    checked: dict[Any, Any] = {}
                    for key, item in value.items():
                        key_text = str(key)
                        # Credential-labelled fields are never accepted as a
                        # newly edited value.  A placeholder produced by this
                        # gateway is safe to retain; any other non-empty value
                        # would reintroduce a secret without a deterministic
                        # redaction step in the original candidate.
                        if _DISPLAY_SECRET_KEY_RE.search(key_text) and item not in (None, ""):
                            # A candidate can legitimately contain a
                            # redaction marker that was produced upstream
                            # (for example ``[REDACTED]`` in a persisted
                            # context snapshot) rather than by this gateway's
                            # reversible alias table.  These markers are
                            # already non-sensitive and must survive the
                            # final-payload revalidation; rejecting them would
                            # make an otherwise safe masked request
                            # impossible to approve.  Reversible aliases are
                            # still limited to this gateway instance below.
                            is_redaction_marker = (
                                isinstance(item, str)
                                and item.startswith("[REDACTED")
                                and item.endswith("]")
                            )
                            if not (
                                isinstance(item, str)
                                and (item in self._alias_to_raw or is_redaction_marker)
                            ):
                                raise PrivacyReviewDenied(
                                    "external egress final payload contains credential material"
                                )
                        checked[key] = await walk(item, depth=depth + 1)
                    return checked
                finally:
                    ancestors.discard(identity)
            if isinstance(value, (list, tuple, set, frozenset)):
                identity = id(value)
                if identity in ancestors:
                    raise PrivacyReviewDenied(
                        "external egress final payload contains a cycle"
                    )
                ancestors.add(identity)
                try:
                    items = [await walk(item, depth=depth + 1) for item in value]
                    if isinstance(value, tuple):
                        return tuple(items)
                    if isinstance(value, frozenset):
                        return frozenset(items)
                    if isinstance(value, set):
                        return set(items)
                    return items
                finally:
                    ancestors.discard(identity)
            return value

        try:
            return await walk(_snapshot_payload(final_payload))
        except PrivacyReviewDenied:
            raise
        except Exception as exc:  # noqa: BLE001
            raise PrivacyReviewDenied(
                "external egress final payload failed privacy validation"
            ) from exc

    async def protect(
        self,
        payload: Any,
        *,
        provider: str,
        base_url: str | None = None,
        source_kind: str = "model_request",
        model: str | None = None,
        descriptor: EgressDescriptor | Mapping[str, Any] | None = None,
        semantic_exempt_values: Iterable[str] | None = None,
    ) -> PrivacyResult:
        # ``provider`` is part of the route binding.  Reject malformed values
        # before constructing a descriptor or classifying the endpoint; never
        # coerce an arbitrary object through ``str()`` at this security
        # boundary.
        if type(provider) is not str or not provider.strip():
            raise PrivacyError("external egress provider is malformed")
        # Capture before any redaction/caching/review callback.  The original
        # is retained only in this request's in-memory result so the v2 review
        # event can show exactly what would leave the process; _audit never
        # serializes it.
        original_payload = _snapshot_payload(payload)
        # Trusted adapters may identify immutable application protocol literals
        # (for example the Cloud Advisor system prompt).  Exact-value matching
        # keeps this opt-out narrow: caller-controlled/user-derived strings
        # remain subject to deterministic and semantic checks.
        semantic_exempt = frozenset(
            value[:8_000]
            for value in (semantic_exempt_values or ())
            if isinstance(value, str)
        )
        if descriptor is None:
            egress_descriptor = EgressDescriptor(
                action=str(source_kind or "external_egress"),
                transport="provider",
                destination=str(base_url or provider or ""),
                provider=str(provider or ""),
                model=str(model or ""),
            )
        elif isinstance(descriptor, EgressDescriptor):
            egress_descriptor = descriptor
        elif isinstance(descriptor, Mapping):
            allowed_descriptor_keys = {
                "action",
                "transport",
                "destination",
                "provider",
                "tool",
                "model",
            }
            values: dict[str, str] = {}
            for key in allowed_descriptor_keys:
                if key not in descriptor:
                    continue
                value = descriptor[key]
                if type(value) is not str:
                    raise PrivacyError("malformed egress descriptor")
                values[key] = value
            egress_descriptor = EgressDescriptor(**values)
        else:
            raise PrivacyError("malformed egress descriptor")
        # The provider argument is authoritative for route classification; a
        # descriptor from an untrusted caller may not silently retarget it.
        if egress_descriptor.provider and str(egress_descriptor.provider).strip().lower() != str(provider or "").strip().lower():
            raise PrivacyError("egress descriptor provider mismatch")
        if not egress_descriptor.provider:
            egress_descriptor = replace(egress_descriptor, provider=str(provider or ""))
        if not egress_descriptor.transport:
            egress_descriptor = replace(egress_descriptor, transport="provider")
        if not egress_descriptor.destination:
            egress_descriptor = replace(
                egress_descriptor,
                destination=str(base_url or provider or ""),
            )
        if not egress_descriptor.action:
            egress_descriptor = replace(
                egress_descriptor,
                action=str(source_kind or "external_egress"),
            )
        egress_descriptor = egress_descriptor.normalized()
        classification = self.provider_class(provider, base_url=base_url)
        if self.settings.mode == "local_only" and classification != "local":
            # A blocked provider attempt is still an auditable decision, but
            # never retain the raw payload in the result/audit trail.
            blocked = PrivacyResult(
                payload=None,
                mode=self.settings.mode,
                provider_class=classification,
                risk_level="high",
                source_kind=source_kind,
                provider=str(provider or ""),
                model=str(model or ""),
                review="denied",
                decision="deny",
                original_payload=original_payload,
                candidate_payload=None,
                final_payload=None,
                masking_status=MASKING_UNNECESSARY,
                egress_descriptor=egress_descriptor,
            )
            self._audit(blocked)
            raise ExternalProviderBlocked(
                f"external provider '{provider}' is blocked by local_only privacy mode"
            )
        resolved_model = model
        if not resolved_model and isinstance(payload, Mapping):
            candidate_model = payload.get("model")
            if candidate_model not in (None, ""):
                resolved_model = str(candidate_model)
        if self.settings.mode == "direct" or classification == "local":
            # Use the immutable request snapshot as both candidate and initial
            # final value.  A caller mutating its original object while a UI
            # review is pending must not create a TOCTOU payload change.
            direct_candidate = _snapshot_payload(payload)
            result = PrivacyResult(
                payload=direct_candidate,
                mode=self.settings.mode,
                provider_class=classification,
                source_kind=source_kind,
                provider=str(provider or ""),
                model=str(resolved_model or ""),
                original_payload=original_payload,
                candidate_payload=direct_candidate,
                final_payload=direct_candidate,
                masking_status=MASKING_UNNECESSARY,
                egress_descriptor=egress_descriptor,
            )
            # Trusted local routes are never review-gated.  ``direct`` mode
            # external routes still need the explicit always-review path, so
            # only return early when no external transaction review applies.
            if classification == "local" or self.settings.review_policy != "always":
                self._audit(result)
                return result
            # ``direct + always`` deliberately keeps the true payload
            # unchanged but still requires the egress transaction review.
            # Do not enter the protected redaction/cache path below: direct
            # mode's candidate is exactly the original.
            try:
                reviewed = await self._review(result)
            except PrivacyReviewDenied as exc:
                review_status = "failed" if exc.__cause__ is not None else "denied"
                self._audit(replace(result, review=review_status, decision="deny"))
                raise
            final_payload = direct_candidate
            if reviewed is True:
                # A small embedding may return a boolean approval instead of
                # the v2 response mapping.  Approval still binds to the
                # immutable candidate captured above; there is no editable
                # payload to decode in this compatibility path.
                final_payload = direct_candidate
            elif not isinstance(reviewed, Mapping):
                denied = replace(result, review="failed", decision="deny")
                self._audit(denied)
                raise PrivacyReviewDenied("external egress review omitted final payload")
            else:
                try:
                    if reviewed.get("media_projection_approved"):
                        final_payload = direct_candidate
                    elif "final_payload" in reviewed:
                        final_payload = self._decode_final_payload(
                            reviewed["final_payload"],
                            template=direct_candidate,
                        )
                    elif "payload" in reviewed:
                        final_payload = reviewed["payload"]
                    else:
                        raise PrivacyReviewDenied("external egress review omitted final payload")
                except PrivacyReviewDenied:
                    denied = replace(result, review="failed", decision="deny")
                    self._audit(denied)
                    raise
            try:
                final_payload = await self._revalidate_final_payload(
                    final_payload,
                    candidate_payload=direct_candidate,
                    semantic_exempt_values=semantic_exempt,
                )
            except PrivacyReviewDenied:
                denied = replace(result, review="failed", decision="deny")
                self._audit(denied)
                raise
            result = replace(
                result,
                payload=final_payload,
                candidate_payload=direct_candidate,
                final_payload=final_payload,
                review="approved",
                decision="allow",
            )
            self._audit(result)
            return result

        cache_hit = False
        try:
            contains_media = _contains_raw_media(payload)
        except PrivacyError:
            # Structural traversal failures (cycles/depth/node budget) are
            # policy denials, not ordinary provider errors.  Record only the
            # sanitized decision before propagating the boundary exception.
            blocked = PrivacyResult(
                payload=None,
                mode=self.settings.mode,
                provider_class=classification,
                semantic_status="failed",
                risk_level="high",
                source_kind=source_kind,
                provider=str(provider or ""),
                model=str(model or resolved_model or ""),
                review="denied",
                decision="deny",
                original_payload=original_payload,
                candidate_payload=None,
                final_payload=None,
                masking_status=MASKING_UNNECESSARY,
                egress_descriptor=egress_descriptor,
            )
            self._audit(blocked)
            raise
        # Preflight catches provider-nested/plain-base64 media whose leaf is a
        # normal string and therefore would not trip ``transform``'s bytes or
        # data-URL branch.  ``raw_media_policy=block`` is unconditional: deny
        # and audit before cache/review callbacks can approve the payload.
        if contains_media and self.settings.raw_media_policy == "block":
            blocked = PrivacyResult(
                payload=None,
                mode=self.settings.mode,
                provider_class=classification,
                risk_level="high",
                source_kind=source_kind,
                provider=str(provider or ""),
                model=str(model or resolved_model or ""),
                review="denied",
                decision="deny",
                original_payload=original_payload,
                candidate_payload=None,
                final_payload=None,
                masking_status=MASKING_UNNECESSARY,
                egress_descriptor=egress_descriptor,
            )
            self._audit(blocked)
            raise RawMediaBlocked("raw media is blocked in protected privacy mode")
        # A bounded process cache is safe only when both identity dimensions
        # are present.  Anonymous/legacy callers keep their alias table local
        # to this gateway instance rather than sharing an empty-key bucket.
        # ``always`` means every request must go through the review callback;
        # serving a previously approved payload would silently bypass that
        # requirement.
        cache_allowed = bool(self.user_id and self.session_id) and self.settings.review_policy != "always"
        provider_key = str(provider or "").strip().lower()
        base_url_key = str(base_url or "").strip()
        source_kind_key = str(source_kind or "").strip()
        model_key = str(resolved_model or "").strip()
        review_callback_key = (
            str(id(self.review_callback)) if self.review_callback is not None else ""
        )
        semantic_callback_key = (
            str(id(self.semantic_redactor)) if self.semantic_redactor is not None else ""
        )
        # Cache only sanitized textual values.  Session alias mapping is still
        # populated on cache hits to keep tool-loop/final-answer restoration.
        cache_key = (
            "privacy-v4",
            self.user_id,
            self.session_id,
            provider_key,
            base_url_key,
            source_kind_key,
            model_key,
            _payload_hash(payload),
            self.settings.mode,
            self.settings.review_policy,
            self.settings.notify,
            self.settings.redaction_terms,
            self.settings.trusted_local_hosts,
            self.settings.semantic_redaction_enabled,
            self.settings.local_provider,
            self.settings.local_model,
            self.settings.raw_media_policy,
            self.settings.semantic_timeout_seconds,
            self.settings.semantic_callback_timeout_seconds,
            self.settings.semantic_total_timeout_seconds,
            self.settings.semantic_max_output_tokens,
            self.settings.semantic_max_calls_per_payload,
            self.settings.semantic_max_input_chars_per_payload,
            self.settings.semantic_max_entities_per_payload,
            tuple(sorted(semantic_exempt)),
            review_callback_key,
            semantic_callback_key,
        )
        cached_entry = self._cache.get(cache_key)
        cached_findings = bool(cached_entry and cached_entry[1])
        if (
            cache_allowed
            and self.settings.cache_enabled
            and not contains_media
            and cache_key in self._cache
            # High-risk redaction entries must not bypass a fresh approval.
            and not (self.settings.review_policy == "high_risk" and cached_findings)
        ):
            serialized, aliases = self._cache.pop(cache_key)
            self._cache[cache_key] = (serialized, aliases)
            for raw, alias in aliases:
                self._raw_to_alias[raw] = alias
                self._alias_to_raw[alias] = raw
            try:
                cached_payload = json.loads(serialized)
                cache_hit = True
                result = PrivacyResult(
                    payload=cached_payload,
                    mode=self.settings.mode,
                    provider_class=classification,
                    semantic_status="cached",
                    risk_level="high" if aliases else "low",
                    cache_hit=True,
                    source_kind=source_kind,
                    provider=str(provider or ""),
                    model=str(resolved_model or ""),
                    review="not_required",
                    decision="allow",
                    original_payload=original_payload,
                    candidate_payload=cached_payload,
                    final_payload=cached_payload,
                    masking_status=MASKING_APPLIED if aliases else MASKING_UNNECESSARY,
                    egress_descriptor=egress_descriptor,
                )
                self._audit(result)
                return result
            except Exception:  # noqa: BLE001
                pass

        findings: list[RedactionFinding] = []
        semantic_status = "disabled"
        semantic_budget: _SemanticBudget | None = None
        if self.settings.semantic_redaction_enabled:
            semantic_budget = _SemanticBudget(
                deadline=(
                    asyncio.get_running_loop().time()
                    + self.settings.semantic_total_timeout_seconds
                )
            )
        # ``_contains_raw_media`` also recognizes provider-nested plain
        # base64 (for example OpenAI ``input_audio.data``).  Seed the
        # transform flag from that preflight so ``raw_media_policy=confirm``
        # cannot accidentally inherit ``review_policy=never`` for nested
        # payloads whose leaf is an ordinary string.
        raw_media_found = contains_media
        transform_nodes = 0
        transform_ancestors: set[int] = set()

        async def transform(
            value: Any,
            *,
            media_context: bool = False,
            depth: int = 0,
        ) -> Any:
            nonlocal raw_media_found, semantic_status, transform_nodes
            transform_nodes += 1
            if transform_nodes > _MAX_PAYLOAD_NODES:
                raise PrivacyError("privacy payload node budget exceeded")
            if depth > _MAX_PAYLOAD_DEPTH:
                raise PrivacyError("privacy payload depth exceeded")
            if _is_media_value(value):
                raw_media_found = True
                if self.settings.raw_media_policy == "block":
                    raise RawMediaBlocked("raw media is blocked in protected privacy mode")
                # ``confirm`` keeps the original value only for an explicitly
                # approved review callback.  Without a callback the high-risk
                # review below fails closed; never silently replace binary
                # content with a marker and continue to an external endpoint.
                return value
            if isinstance(value, str) and media_context and _is_plain_base64(value):
                # OpenAI-compatible multimodal blocks commonly put raw
                # base64 in ``data`` rather than using a data URL.  Preserve
                # it only for the explicit confirm path; the preflight above
                # has already blocked it for ``raw_media_policy=block``.
                raw_media_found = True
                if self.settings.raw_media_policy == "block":
                    raise RawMediaBlocked("raw media is blocked in protected privacy mode")
                return value
            if isinstance(value, str):
                if value in semantic_exempt:
                    # The exact value has passed the Cloud workflow's strict
                    # projection/digest attestation; preserve its opaque
                    # aliases instead of re-running credential replacement.
                    redacted, deterministic = value, []
                else:
                    redacted, deterministic = self._deterministic_redact(value)
                findings.extend(deterministic)
                # Provider media discriminator values (for example
                # ``input_image``) are protocol structure, not user content.
                # Do not turn an absent semantic sidecar into a false failure
                # for an otherwise review-gated raw-media payload.
                if value in semantic_exempt:
                    # Protocol literals are adapter-owned text rather than
                    # user material.  Deterministic checks still run first so
                    # a future literal containing a credential cannot bypass
                    # the first-line patterns.
                    entities, status = [], "skipped"
                elif redacted.strip().lower() in (
                    _MEDIA_CONTAINER_KEYS | _SEMANTIC_PROTOCOL_LITERALS
                ):
                    entities, status = [], "disabled"
                elif semantic_status == "failed":
                    # A prior detector failure is terminal for this payload;
                    # do not keep invoking the callback for every remaining
                    # scalar in a large object.
                    raise PrivacyError("semantic privacy redaction failed")
                else:
                    entities, status = await self._semantic_entities(
                        redacted,
                        budget=semantic_budget,
                    )
                semantic_status = status if status != "disabled" else semantic_status
                if status == "failed":
                    raise PrivacyError("semantic privacy redaction failed")
                for entity in entities:
                    raw = entity["text"]
                    alias = self._placeholder(entity["category"], raw)
                    if raw in redacted:
                        redacted = redacted.replace(raw, alias)
                        findings.append(RedactionFinding(entity["category"], alias))
                return redacted
            if isinstance(value, Mapping):
                identity = id(value)
                if identity in transform_ancestors:
                    raise PrivacyError("privacy payload cycle detected")
                transform_ancestors.add(identity)
                try:
                    transformed: dict[Any, Any] = {}
                    for key, item in value.items():
                        normalized_key = str(key or "").strip().lower().replace("-", "_")
                        child_media_context = media_context or normalized_key in _MEDIA_CONTAINER_KEYS
                        if normalized_key in _MEDIA_BASE64_KEYS or normalized_key == "data":
                            child_media_context = True
                        # A mapping key such as ``password`` is itself the
                        # credential label; transforming only its value would
                        # lose the context required by the labelled-secret
                        # matcher.  Protect the complete value before the
                        # recursive walk (and keep the alias in this gateway
                        # so approved local restoration still works).
                        if (
                            isinstance(key, str)
                            and _DISPLAY_SECRET_KEY_RE.search(key)
                            and item not in (None, "")
                            and not child_media_context
                        ):
                            if (
                                isinstance(item, str)
                                and item.startswith("[REDACTED")
                                and item.endswith("]")
                            ):
                                # Preserve an upstream one-way marker.  It
                                # is already non-sensitive and carries no
                                # reversible alias that this gateway needs to
                                # restore.
                                transformed[key] = item
                                continue
                            if isinstance(item, str):
                                # Authorization values often carry a scheme
                                # (Bearer/Basic) whose token is shorter than
                                # the generic high-confidence regex minimum.
                                # Preserve only the harmless scheme label and
                                # alias the complete credential suffix.
                                auth_key = normalized_key in {
                                    "authorization",
                                    "proxy_authorization",
                                }
                                if auth_key:
                                    scheme, separator, credential = item.partition(" ")
                                    if separator and scheme.casefold() in {"bearer", "basic"} and credential:
                                        alias = self._placeholder("SECRET", credential)
                                        transformed[key] = f"{scheme} {alias}"
                                        findings.append(RedactionFinding("SECRET", alias))
                                        continue
                                labelled_value = f"{key}={item}"
                            else:
                                try:
                                    labelled_value = json.dumps(
                                        item,
                                        ensure_ascii=False,
                                        sort_keys=True,
                                        separators=(",", ":"),
                                    )
                                except (TypeError, ValueError):
                                    labelled_value = "<unsupported-secret-value>"
                            redacted_labelled, deterministic = self._deterministic_redact(labelled_value)
                            findings.extend(deterministic)
                            if isinstance(item, str) and "=" in redacted_labelled:
                                transformed[key] = redacted_labelled.split("=", 1)[1]
                            else:
                                transformed[key] = redacted_labelled
                            continue
                        transformed[key] = await transform(
                            item,
                            media_context=child_media_context,
                            depth=depth + 1,
                        )
                    return transformed
                finally:
                    transform_ancestors.discard(identity)
            if isinstance(value, list):
                identity = id(value)
                if identity in transform_ancestors:
                    raise PrivacyError("privacy payload cycle detected")
                transform_ancestors.add(identity)
                try:
                    return [
                        await transform(
                            item,
                            media_context=media_context,
                            depth=depth + 1,
                        )
                        for item in value
                    ]
                finally:
                    transform_ancestors.discard(identity)
            if isinstance(value, tuple):
                identity = id(value)
                if identity in transform_ancestors:
                    raise PrivacyError("privacy payload cycle detected")
                transform_ancestors.add(identity)
                try:
                    return tuple(
                        [
                            await transform(
                                item,
                                media_context=media_context,
                                depth=depth + 1,
                            )
                            for item in value
                        ]
                    )
                finally:
                    transform_ancestors.discard(identity)
            if isinstance(value, (set, frozenset)):
                identity = id(value)
                if identity in transform_ancestors:
                    raise PrivacyError("privacy payload cycle detected")
                transform_ancestors.add(identity)
                try:
                    return type(value)(
                        [
                            await transform(
                                item,
                                media_context=media_context,
                                depth=depth + 1,
                            )
                            for item in value
                        ]
                    )
                finally:
                    transform_ancestors.discard(identity)
            return value

        try:
            protected_payload = await transform(payload)
        except RawMediaBlocked:
            blocked = PrivacyResult(
                payload=None,
                findings=tuple(findings),
                mode=self.settings.mode,
                provider_class=classification,
                semantic_status=semantic_status,
                risk_level="high",
                source_kind=source_kind,
                provider=str(provider or ""),
                model=str(resolved_model or ""),
                review="denied",
                decision="deny",
                original_payload=original_payload,
                candidate_payload=None,
                final_payload=None,
                masking_status=MASKING_UNNECESSARY,
                egress_descriptor=egress_descriptor,
            )
            self._audit(blocked)
            raise
        except ExternalProviderBlocked:
            # Any nested provider check remains auditable just like the
            # initial provider decision above.
            blocked = PrivacyResult(
                payload=None,
                findings=tuple(findings),
                mode=self.settings.mode,
                provider_class=classification,
                semantic_status=semantic_status,
                risk_level="high",
                source_kind=source_kind,
                provider=str(provider or ""),
                model=str(resolved_model or ""),
                review="denied",
                decision="deny",
                original_payload=original_payload,
                candidate_payload=None,
                final_payload=None,
                masking_status=MASKING_UNNECESSARY,
                egress_descriptor=egress_descriptor,
            )
            self._audit(blocked)
            raise
        except Exception as exc:  # noqa: BLE001
            # Never fall back to raw payload after a protected transformation
            # failure.  Callers can present a warning/review or stop the turn.
            if semantic_status == "failed":
                denied = PrivacyResult(
                    payload=None,
                    findings=tuple(findings),
                    mode=self.settings.mode,
                    provider_class=classification,
                    semantic_status=semantic_status,
                    risk_level="high",
                    source_kind=source_kind,
                    provider=str(provider or ""),
                    model=str(model or resolved_model or ""),
                    review="denied",
                    decision="deny",
                    original_payload=original_payload,
                    candidate_payload=None,
                    final_payload=None,
                    masking_status=MASKING_UNNECESSARY,
                    egress_descriptor=egress_descriptor,
                )
                self._audit(denied)
                raise PrivacyError(
                    "semantic privacy redaction failed; outbound payload withheld"
                ) from exc
            raise PrivacyError("privacy redaction failed; outbound payload withheld") from exc

        # A semantic detector failure means we cannot prove that the
        # deterministic projection is complete.  Do not send that uncertain
        # payload to a human review or an external provider; fail closed for
        # every review policy (including ``high_risk``).
        risk = "high" if findings or semantic_status == "failed" or raw_media_found else "low"
        result = PrivacyResult(
            payload=protected_payload,
            findings=tuple(findings),
            mode=self.settings.mode,
            provider_class=classification,
            semantic_status=semantic_status,
            risk_level=risk,
            cache_hit=cache_hit,
            source_kind=source_kind,
            provider=str(provider or ""),
            model=str(resolved_model or ""),
            review="not_required",
            decision="allow",
            original_payload=original_payload,
            candidate_payload=protected_payload,
            final_payload=protected_payload,
            masking_status=(
                MASKING_APPLIED
                if _payload_hash(original_payload) != _payload_hash(protected_payload)
                else MASKING_UNNECESSARY
            ),
            egress_descriptor=egress_descriptor,
        )
        if semantic_status == "failed":
            denied = replace(result, decision="deny")
            self._audit(denied)
            raise PrivacyError("semantic privacy redaction failed; outbound payload withheld")
        # Raw media is inherently high risk and cannot inherit the global
        # ``never`` review policy when ``raw_media_policy=confirm`` is used.
        # Force the explicit review callback; absent/denied callbacks fail
        # closed through ``_review`` and never reach an external transport.
        media_review_required = bool(
            raw_media_found and self.settings.raw_media_policy == "confirm"
        )
        review_required = media_review_required or self.settings.review_policy == "always" or (
            self.settings.review_policy == "high_risk" and risk == "high"
        )
        if review_required:
            # Capture an immutable candidate before invoking any review/UI
            # callback.  The callback receives a mutable PrivacyResult for
            # backwards compatibility; a malicious or stale callback can
            # otherwise mutate ``candidate_payload`` in place and make a
            # newly injected raw value compare equal at the commit point.
            candidate_snapshot = _snapshot_payload(result.candidate_payload)
            try:
                reviewed = await self._review(result)
            except PrivacyReviewDenied as exc:
                # Keep an auditable deny record even though the exception is
                # propagated to the transport caller and no payload is sent.
                review_status = "failed" if exc.__cause__ is not None else "denied"
                self._audit(replace(result, review=review_status, decision="deny"))
                raise
            try:
                candidate_was_mutated = (
                    _payload_hash(result.candidate_payload)
                    != _payload_hash(candidate_snapshot)
                )
            except Exception:
                candidate_was_mutated = True
            if candidate_was_mutated:
                # The callback received a mutable compatibility object, but
                # the UI-approved snapshot is immutable.  Any in-place edit
                # after the snapshot is unreviewed, even when the callback
                # returns that same object as ``payload``.
                denied = replace(result, review="failed", decision="deny")
                self._audit(denied)
                raise PrivacyReviewDenied(
                    "external egress review candidate was mutated with an unreviewed field"
                )
            result = replace(result, review="approved", decision="allow")
            if reviewed is True:
                # Boolean approval is a compatibility seam for embedded
                # callers; it can only approve the already masked candidate.
                final_payload = candidate_snapshot
                result = replace(
                    result,
                    payload=final_payload,
                    final_payload=final_payload,
                )
            elif isinstance(reviewed, Mapping):
                if reviewed.get("media_projection_approved"):
                    final_payload = candidate_snapshot
                elif "final_payload" in reviewed:
                    try:
                        final_payload = self._decode_final_payload(
                            reviewed["final_payload"],
                            template=result.candidate_payload,
                        )
                    except PrivacyReviewDenied:
                        denied = replace(result, review="failed", decision="deny")
                        self._audit(denied)
                        raise
                elif "payload" in reviewed:
                    final_payload = reviewed["payload"]
                else:
                    denied = replace(result, review="failed", decision="deny")
                    self._audit(denied)
                    raise PrivacyReviewDenied("external egress review omitted final payload")
                if "payload" in reviewed:
                    # Mapping-valued review callbacks are legacy compatibility
                    # seams.  Permit a structural wrapper that contains the
                    # exact approved candidate, but do not accept a changed
                    # copy: it has no server-issued final binding attestation
                    # and could carry arbitrary raw text when semantic review
                    # is disabled.  Text ``final_payload`` editors retain the
                    # existing explicit revalidation path below.
                    payload_is_exact_wrapper = (
                        isinstance(final_payload, Mapping)
                        and any(item == candidate_snapshot for item in final_payload.values())
                    )
                    if final_payload != candidate_snapshot and not payload_is_exact_wrapper:
                        denied = replace(result, review="failed", decision="deny")
                        self._audit(denied)
                        raise PrivacyReviewDenied(
                            "external egress review payload changed without attestation"
                        )
            else:
                denied = replace(result, review="failed", decision="deny")
                self._audit(denied)
                raise PrivacyReviewDenied("external egress review omitted final payload")
            # The browser/editor response is untrusted even after the review
            # callback has approved it.  Re-run the privacy checks against the
            # exact value selected by the reviewer before committing to the
            # provider sender; never silently mask a new edit after approval.
            try:
                final_payload = await self._revalidate_final_payload(
                    final_payload,
                    candidate_payload=candidate_snapshot,
                    semantic_exempt_values=semantic_exempt,
                )
            except PrivacyReviewDenied:
                denied = replace(result, review="failed", decision="deny")
                self._audit(denied)
                raise
            result = replace(
                result,
                payload=final_payload,
                final_payload=final_payload,
            )
        if (
            cache_allowed
            and self.settings.cache_enabled
            and not contains_media
            # Findings are high-risk, but historical ``review_policy=never``
            # callers intentionally permit deterministic redaction caching.
            # Any policy that actually requires approval (high_risk/always)
            # must not cache an approved payload, so the next request is
            # reviewed again.
            and (result.risk_level == "low" or self.settings.review_policy == "never")
        ):
            try:
                serialized = json.dumps(result.payload, ensure_ascii=False, sort_keys=True, default=str)
                aliases = tuple(self._raw_to_alias.items())
                self._cache[cache_key] = (serialized, aliases)
                self._cache.move_to_end(cache_key)
                while len(self._cache) > self._cache_limit:
                    self._cache.popitem(last=False)
            except Exception:  # noqa: BLE001
                pass
        self._audit(result)
        return result

    def protect_sync(self, payload: Any, **kwargs: Any) -> PrivacyResult:
        """Synchronous transport adapter with a bounded privacy deadline.

        Both ordinary synchronous callers and callers already inside an event
        loop use the same capped bridge pool.  This is intentional: an
        injected detector that suppresses cancellation must not be able to
        hold either call site indefinitely.
        """
        timeout = max(
            float(self.settings.semantic_total_timeout_seconds),
            float(self.settings.semantic_callback_timeout_seconds),
        ) + 1.0
        timeout = max(timeout, 1.0)
        # This method may itself run on an event-loop thread (the hosted
        # search bridge does that), so never block synchronously waiting for a
        # slot held by a timed-out callback.  Fail closed and let the caller
        # retry at its normal request boundary.
        if not _PROTECT_SYNC_SLOTS.acquire(blocking=False):
            raise PrivacyError(
                "privacy protection worker capacity exhausted; outbound payload withheld"
            )

        # Provider sync calls normally run outside an event loop.  If called
        # from an active loop, a daemon worker bridge avoids nested
        # ``asyncio.run`` while preserving the privacy boundary.  The copied
        # context keeps request-local policy metadata available to callbacks.
        future: concurrent.futures.Future[PrivacyResult] = concurrent.futures.Future()
        callback_context = contextvars.copy_context()

        def run_protection() -> None:
            try:
                result = callback_context.run(
                    asyncio.run,
                    self.protect(payload, **kwargs),
                )
            except BaseException as exc:  # noqa: BLE001
                try:
                    future.set_exception(exc)
                except concurrent.futures.InvalidStateError:
                    # The synchronous caller may have timed out and canceled
                    # the Future concurrently; the daemon worker's late
                    # exception is intentionally discarded.
                    pass
            else:
                try:
                    future.set_result(result)
                except concurrent.futures.InvalidStateError:
                    pass
            finally:
                _PROTECT_SYNC_SLOTS.release()

        try:
            threading.Thread(
                target=run_protection,
                name="aoitalk-privacy",
                daemon=True,
            ).start()
        except BaseException:
            _PROTECT_SYNC_SLOTS.release()
            raise
        try:
            # ``protect`` bounds individual/aggregate semantic work, but an
            # injected callback or review adapter may ignore cancellation.
            # Bound this synchronous bridge as well so callers running inside
            # an event loop cannot block forever behind a misbehaving sidecar.
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise PrivacyError(
                "privacy protection timed out; outbound payload withheld"
            ) from exc
        finally:
            if not future.done():
                future.cancel()

    @staticmethod
    def _resolve_descriptor(
        descriptor: EgressDescriptor | Mapping[str, Any] | None,
        *,
        provider: str,
        base_url: str | None,
        source_kind: str,
        model: str | None,
    ) -> EgressDescriptor:
        if descriptor is None:
            return EgressDescriptor(
                action=str(source_kind or "external_egress"),
                transport="",
                destination=str(base_url or ""),
                provider=str(provider or ""),
                model=str(model or ""),
            )
        if isinstance(descriptor, EgressDescriptor):
            active = descriptor
        elif isinstance(descriptor, Mapping):
            values: dict[str, str] = {}
            for key in ("action", "transport", "destination", "provider", "tool", "model"):
                if key not in descriptor:
                    continue
                value = descriptor[key]
                if type(value) is not str:
                    raise PrivacyError("malformed egress descriptor")
                values[key] = value
            active = EgressDescriptor(
                **values,
            )
        else:
            raise PrivacyError("malformed egress descriptor")
        if active.provider and active.provider.strip().lower() != str(provider or "").strip().lower():
            raise PrivacyError("egress descriptor provider mismatch")
        if not active.provider:
            active = replace(active, provider=str(provider or ""))
        if not active.model and model:
            active = replace(active, model=str(model))
        if not active.destination and base_url:
            active = replace(active, destination=str(base_url))
        if not active.destination:
            active = replace(active, destination=str(provider or ""))
        if not active.transport:
            active = replace(active, transport="provider")
        if not active.action:
            active = replace(active, action=str(source_kind or "external_egress"))
        return active.normalized()

    async def execute(
        self,
        payload: Any,
        provider: str | None = None,
        descriptor: EgressDescriptor | Mapping[str, Any] | None = None,
        sender: Callable[[Any], Any] | None = None,
        *,
        send: Callable[[Any], Any] | None = None,
        base_url: str | None = None,
        source_kind: str = "external_egress",
        model: str | None = None,
        semantic_exempt_values: Iterable[str] | None = None,
    ) -> Any:
        """Protect, review and invoke one external sender exactly once.

        ``sender`` is called with the gateway's final payload and no retries,
        redirect/fanout, or legacy ``check_permission`` call is performed by
        this method.  Adapters should create a fresh descriptor for each
        independently retried or redirected request.
        """

        provider_value: Any = provider
        if provider_value is None and isinstance(descriptor, EgressDescriptor):
            provider_value = descriptor.provider
        elif provider_value is None and isinstance(descriptor, Mapping):
            provider_value = descriptor.get("provider")
        if type(provider_value) is not str:
            raise PrivacyError("external egress provider is malformed")
        active_provider = provider_value.strip()
        if not active_provider:
            raise PrivacyError("external egress provider is required")
        active_sender = sender or send
        if not callable(active_sender):
            raise PrivacyError("external egress sender is required")
        active_descriptor = self._resolve_descriptor(
            descriptor,
            provider=active_provider,
            base_url=base_url,
            source_kind=source_kind,
            model=model,
        )
        result = await self.protect(
            payload,
            provider=active_provider,
            base_url=base_url,
            source_kind=source_kind,
            model=model or active_descriptor.model,
            descriptor=active_descriptor,
            semantic_exempt_values=semantic_exempt_values,
        )
        # Older embedding seams may return a minimal ``PrivacyResult``-like
        # object exposing only ``payload`` from a patched ``protect`` hook.
        # Treat that shape as the already-approved final value, but never
        # coerce arbitrary objects or fall back to the original input.
        final_payload = getattr(result, "final_payload", None)
        if final_payload is None and getattr(result, "payload", None) is not None:
            final_payload = result.payload
        # Calling the sender is the commit point.  Keep it outside the review
        # callback and invoke it once with the exact approved value.
        sent = active_sender(final_payload)
        if inspect.isawaitable(sent):
            sent = await sent
        return sent

    def execute_sync(
        self,
        payload: Any,
        provider: str | None = None,
        descriptor: EgressDescriptor | Mapping[str, Any] | None = None,
        sender: Callable[[Any], Any] | None = None,
        *,
        send: Callable[[Any], Any] | None = None,
        base_url: str | None = None,
        source_kind: str = "external_egress",
        model: str | None = None,
        semantic_exempt_values: Iterable[str] | None = None,
    ) -> Any:
        """Synchronous counterpart of :meth:`execute` for blocking adapters."""

        provider_value: Any = provider
        if provider_value is None and isinstance(descriptor, EgressDescriptor):
            provider_value = descriptor.provider
        elif provider_value is None and isinstance(descriptor, Mapping):
            provider_value = descriptor.get("provider")
        if type(provider_value) is not str:
            raise PrivacyError("external egress provider is malformed")
        active_provider = provider_value.strip()
        if not active_provider:
            raise PrivacyError("external egress provider is required")
        active_sender = sender or send
        if not callable(active_sender):
            raise PrivacyError("external egress sender is required")
        active_descriptor = self._resolve_descriptor(
            descriptor,
            provider=active_provider,
            base_url=base_url,
            source_kind=source_kind,
            model=model,
        )
        result = self.protect_sync(
            payload,
            provider=active_provider,
            base_url=base_url,
            source_kind=source_kind,
            model=model or active_descriptor.model,
            descriptor=active_descriptor,
            semantic_exempt_values=semantic_exempt_values,
        )
        final_payload = getattr(result, "final_payload", None)
        if final_payload is None and getattr(result, "payload", None) is not None:
            final_payload = result.payload
        sent = active_sender(final_payload)
        if inspect.isawaitable(sent):
            # A blocking adapter occasionally runs on an event-loop thread.
            # Use a daemon bridge rather than nest asyncio.run or block an
            # unbounded executor shutdown.
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                sent = asyncio.run(sent)
            else:
                future: concurrent.futures.Future[Any] = concurrent.futures.Future()

                def run_sender() -> None:
                    try:
                        future.set_result(asyncio.run(sent))
                    except BaseException as exc:  # noqa: BLE001
                        try:
                            future.set_exception(exc)
                        except concurrent.futures.InvalidStateError:
                            pass

                threading.Thread(
                    target=run_sender,
                    name="aoitalk-egress-sender",
                    daemon=True,
                ).start()
                try:
                    sent = future.result(timeout=60.0)
                except concurrent.futures.TimeoutError as exc:
                    future.cancel()
                    raise PrivacyError("external egress sender timed out") from exc
        return sent

    async def _review(self, result: PrivacyResult) -> Any:
        callback = self.review_callback
        if callback is None:
            callback = lambda value: request_external_privacy_review(
                value,
                provider=value.provider,
                model=value.model,
                notify=self.settings.notify,
            )
        # Most request entry points bind the permission scope in the parent
        # chat/worker context.  Standalone integrations can still construct a
        # gateway with an authenticated user/session pair; bridge that pair
        # into the permission manager for the duration of this one review so
        # the interactive request is not rejected as synthetic ``default``.
        # Never invent a scope when either identifier is missing: background
        # or anonymous review attempts remain fail-closed.
        permission_scope_token = None
        try:
            if self.user_id and self.session_id:
                from ..tools.external_llm_permission import (
                    get_permission_request_scope,
                    set_permission_session_key,
                    reset_permission_session_key,
                    _scope_is_real,
                )

                current_user, current_session = get_permission_request_scope()
                # Treat the legacy ``default|default`` scope as synthetic in
                # the same way as the unscoped default key.  An authenticated
                # gateway owns the authoritative user/session pair for this
                # transaction; an anonymous gateway must never invent one.
                if not _scope_is_real(current_user, current_session):
                    permission_scope_token = set_permission_session_key(
                        f"{self.user_id}|{self.session_id}"
                    )

            approved = callback(result)
            if inspect.isawaitable(approved):
                approved = await approved
            if isinstance(approved, Mapping):
                response = approved
                approved = response.get("approved", False)
                if type(approved) is bool and approved is True:
                    # The built-in v2 manager returns a server-generated
                    # attestation for the exact editor string.  Verify it at
                    # the gateway commit point, after the callback and before
                    # any decode/revalidation, so a stale or mutated final
                    # value cannot ride on the candidate-only challenge.
                    final_value = response.get("final_payload")
                    final_binding = response.get("final_binding_digest")
                    initial_binding = response.get("binding_digest")
                    if final_binding is not None or initial_binding is not None:
                        if (
                            not isinstance(final_value, str)
                            or not isinstance(initial_binding, str)
                            or not initial_binding
                            or not isinstance(final_binding, str)
                            or not final_binding
                        ):
                            raise PrivacyReviewDenied(
                                "external egress final binding is malformed"
                            )
                        from ..tools.external_llm_permission import (
                            build_egress_final_binding_digest,
                        )

                        expected_final_binding = build_egress_final_binding_digest(
                            binding_digest=initial_binding,
                            final_payload=final_value,
                        )
                        if expected_final_binding != final_binding:
                            raise PrivacyReviewDenied(
                                "external egress final binding mismatch"
                            )
                    return response
                if type(approved) is bool and approved is False:
                    raise PrivacyReviewDenied("external payload review was denied")
                raise PrivacyReviewDenied("external payload review response was malformed")
            if type(approved) is not bool or approved is not True:
                raise PrivacyReviewDenied("external payload review was denied")
            return approved
        except PrivacyReviewDenied:
            raise
        except Exception as exc:  # noqa: BLE001
            raise PrivacyReviewDenied("external payload review failed") from exc
        finally:
            if permission_scope_token is not None:
                reset_permission_session_key(permission_scope_token)

    @staticmethod
    def _decode_final_payload(value: Any, *, template: Any = None) -> Any:
        """Decode the v2 final_payload envelope without candidate fallback.

        The browser returns an exact string.  Structured payloads use JSON;
        providers that accept plain text may intentionally return a non-JSON
        scalar, which is preserved byte-for-byte as a string.
        """

        if not isinstance(value, str):
            raise PrivacyReviewDenied("external egress final_payload must be a string")
        # Textual provider payloads are edited as text.  A value such as
        # ``"123"`` must remain the string ``"123"`` rather than being
        # implicitly converted to the integer 123 merely because it happens to
        # be valid JSON.  Structured candidates, on the other hand, use the
        # canonical JSON editor representation and are decoded back to their
        # typed mapping/list/scalar for the provider SDK.
        if isinstance(template, str):
            return value
        try:
            parsed = json.loads(value)
        except Exception as exc:
            # A structured candidate must stay structured.  Passing a
            # malformed editor string through to an adapter would turn a
            # rejected edit into an unexpected provider request.
            raise PrivacyReviewDenied(
                "external egress final payload is malformed JSON"
            ) from exc

        if isinstance(template, Mapping):
            if not isinstance(parsed, Mapping):
                raise PrivacyReviewDenied(
                    "external egress final payload shape mismatch"
                )
        elif isinstance(template, (list, tuple, set, frozenset)):
            if not isinstance(parsed, list):
                raise PrivacyReviewDenied(
                    "external egress final payload shape mismatch"
                )
        elif template is None:
            if parsed is not None:
                raise PrivacyReviewDenied(
                    "external egress final payload shape mismatch"
                )
        elif isinstance(template, bool):
            if type(parsed) is not bool:
                raise PrivacyReviewDenied(
                    "external egress final payload shape mismatch"
                )
        elif isinstance(template, int) and not isinstance(template, bool):
            if type(parsed) is not int:
                raise PrivacyReviewDenied(
                    "external egress final payload shape mismatch"
                )
        elif isinstance(template, float):
            if not isinstance(parsed, (int, float)) or isinstance(parsed, bool):
                raise PrivacyReviewDenied(
                    "external egress final payload shape mismatch"
                )
        return parsed

    def restore_aliases(self, value: Any) -> Any:
        """Restore aliases only for local execution/final user display."""
        if isinstance(value, str):
            restored = value
            for alias, raw in sorted(self._alias_to_raw.items(), key=lambda pair: len(pair[0]), reverse=True):
                restored = restored.replace(alias, raw)
            return restored
        if isinstance(value, Mapping):
            return {key: self.restore_aliases(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.restore_aliases(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self.restore_aliases(item) for item in value)
        return value

    # Short compatibility name used by provider adapters when restoring a
    # final user-facing response.
    def restore(self, value: Any) -> Any:
        return self.restore_aliases(value)

    def restore_tool_arguments(
        self,
        arguments: Mapping[str, Any],
        *,
        tool_name: str | None = None,
    ) -> dict[str, Any]:
        """Restore aliases only before an internal tool executes.

        External-egress tools (web/X search and MCP wrappers) must retain
        aliases until their own outbound gateway invocation.  Keeping this
        guard in the shared service prevents provider-specific loops from
        accidentally rehydrating a query before it leaves AoiTalk.
        """

        if is_external_egress_tool_name(tool_name):
            return dict(arguments)
        return dict(self.restore_aliases(arguments))

    def _audit(self, result: PrivacyResult) -> None:
        review_status = str(result.review or "not_required")
        review_performed = review_status in {"approved", "denied", "failed"}
        finding_counts: dict[str, int] = {}
        for finding in result.findings:
            try:
                count = max(int(finding.count), 1)
            except (TypeError, ValueError):
                count = 1
            finding_counts[finding.category] = finding_counts.get(finding.category, 0) + count
        self.audit.append(
            {
                "mode": result.mode,
                "provider": result.provider,
                "model": result.model,
                "provider_class": result.provider_class,
                "findings": finding_counts,
                "semantic_status": result.semantic_status,
                "risk_level": result.risk_level,
                "cache_hit": result.cache_hit,
                "source_kind": result.source_kind,
                # Keep both the explicit review fields and compact aliases for
                # consumers that already treat audit rows as flat records.
                "review": review_status,
                "review_status": review_status,
                "review_required": review_performed,
                "review_performed": review_performed,
                "review_decision": review_status if review_performed else "not_required",
                "reviewed": review_performed,
                "approved": review_status == "approved",
                "decision": result.decision,
            }
        )


def protect_outbound_payload_sync(
    payload: Any,
    *,
    config: Any | None = None,
    provider: str,
    base_url: str | None = None,
    source_kind: str = "model_request",
    gateway: OutboundPrivacyGateway | None = None,
    descriptor: EgressDescriptor | Mapping[str, Any] | None = None,
) -> PrivacyResult:
    """Convenience helper for synchronous provider adapters."""

    active = gateway or OutboundPrivacyGateway(config)
    return active.protect_sync(
        payload,
        provider=provider,
        base_url=base_url,
        source_kind=source_kind,
        descriptor=descriptor,
    )


async def materialize_one_way(
    payload: Any,
    *,
    config: Any | None = None,
    semantic_redactor: Callable[..., Any] | None = None,
    source_kind: str = "masking",
) -> PrivacyResult:
    """One-shot permanent masking helper.

    This is intentionally separate from :func:`protect_outbound_payload_sync`:
    callers obtain a fresh gateway and no reversible alias scope is shared with
    ordinary provider traffic.  The gateway method forces deterministic and
    trusted-local semantic materialization regardless of ``direct`` mode.
    """

    gateway = OutboundPrivacyGateway(
        config,
        semantic_redactor=semantic_redactor,
        session_context={},
        project_metadata={},
    )
    return await gateway.materialize_one_way(payload, source_kind=source_kind)


def materialize_one_way_sync(
    payload: Any,
    *,
    config: Any | None = None,
    semantic_redactor: Callable[..., Any] | None = None,
    source_kind: str = "masking",
) -> PrivacyResult:
    """Synchronous one-shot counterpart of :func:`materialize_one_way`."""

    gateway = OutboundPrivacyGateway(
        config,
        semantic_redactor=semantic_redactor,
        session_context={},
        project_metadata={},
    )
    return gateway.materialize_one_way_sync(payload, source_kind=source_kind)


__all__ = [
    "CLI_PROVIDER_IDS",
    "EXTERNAL_PROVIDER_IDS",
    "LOCAL_PROVIDER_IDS",
    "MASKING_APPLIED",
    "MASKING_UNNECESSARY",
    "ExternalProviderBlocked",
    "PrivacyPolicyContext",
    "OutboundPrivacyGateway",
    "EgressDescriptor",
    "PrivacyConfig",
    "PrivacyError",
    "PrivacyResult",
    "PrivacyReviewDenied",
    "RawMediaBlocked",
    "RedactionFinding",
    "redact_secret_for_local_display",
    "is_external_provider",
    "privacy_config",
    "build_semantic_redactor",
    "materialize_one_way",
    "materialize_one_way_sync",
    "request_external_privacy_review",
    "set_privacy_policy_context",
    "reset_privacy_policy_context",
    "get_privacy_policy_context",
    "effective_privacy_mode",
    "current_effective_privacy_mode",
    "protect_outbound_payload_sync",
    "provider_classification",
    "is_external_egress_tool_name",
    "_payload_hash",
]
