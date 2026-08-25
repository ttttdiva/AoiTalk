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
import hashlib
import inspect
import ipaddress
import json
import logging
import os
import re
import contextvars
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable, Iterable, Mapping, MutableMapping
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

PrivacyMode = str
ReviewPolicy = str


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
    }
)
LOCAL_PROVIDER_IDS = frozenset(
    {"ollama", "sglang", "openai_compatible_local", "mage_vl", "speech_recognition"}
)
CLI_PROVIDER_IDS = frozenset(
    {"codex-cli", "claude-cli", "antigravity-cli", "grok-cli"}
)

_SECRET_RE = re.compile(
    r"(?i)(?P<prefix>\b(?:api[_ -]?key|secret|token|password|passwd|authorization)\s*[:=]\s*)(?P<value>[^\s,;]+)"
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
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
    payload: Any
    findings: tuple[RedactionFinding, ...] = ()
    mode: PrivacyMode = "direct"
    provider_class: str = "unknown"
    semantic_status: str = "disabled"
    risk_level: str = "low"
    cache_hit: bool = False
    source_kind: str = "model_request"
    provider: str = ""
    model: str = ""
    # Review/decision state is kept on the result so the audit trail can
    # describe whether this payload was sent directly, approved by a reviewer,
    # served from cache, or denied before transport.  These fields deliberately
    # contain status only; the payload and any raw values never enter the audit.
    review: str = "not_required"
    decision: str = "allow"


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
    if not api_key:
        api_key = str(os.getenv("OPENAI_API_KEY", "") or "").strip()
    return endpoint, api_key


def _local_sidecar_model(config: Any | None, settings: PrivacyConfig) -> str:
    provider = settings.local_provider
    return str(
        settings.local_model
        or _config_value(
            config,
            f"{provider}.model",
            f"{provider}_model",
            default="",
        )
        or ""
    ).strip()


def build_semantic_redactor(
    config: Any | None,
    settings: PrivacyConfig | None = None,
) -> Callable[..., Awaitable[Any]] | None:
    """Create a tool-free local sidecar callback for semantic extraction.

    ``None`` means semantic redaction is intentionally disabled because no
    local model was configured.  A configured but untrusted endpoint returns a
    callback that raises at call time; the gateway records a failed semantic
    pass and applies its fail-closed review policy instead of falling back to a
    raw cloud request.
    """

    active = settings or privacy_config(config)
    if not active.semantic_redaction_enabled:
        return None
    model = _local_sidecar_model(config, active)
    if not model:
        return None
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

    async def _redact(text: str, requested_model: str = "") -> Any:
        # Import lazily to keep optional provider paths import-safe and to make
        # it explicit that this client bypasses the main outbound gateway only
        # after the endpoint was classified as trusted local.
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=api_key or "local", base_url=endpoint)
        sidecar_model = str(requested_model or model).strip() or model
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
                {"role": "user", "content": str(text or "")[:24_000]},
            ],
            "temperature": 0,
        }
        try:
            try:
                response = await client.chat.completions.create(
                    **request,
                    response_format={"type": "json_object"},
                )
            except TypeError:
                # A few OpenAI-compatible local servers expose JSON-only
                # prompts but reject the optional response_format parameter.
                # The fixed prompt still requires JSON; retrying here never
                # changes the trusted-local-only boundary.
                response = await client.chat.completions.create(**request)
        finally:
            try:
                await client.close()
            except Exception:
                pass
        content = ""
        try:
            content = str(response.choices[0].message.content or "")
        except Exception:
            content = str(getattr(response, "output_text", "") or "")
        content = content.strip()
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.I | re.S)
        return json.loads(content or '{"entities":[]}')

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

    has_binary = _contains_raw_media(result.payload)
    if has_binary:
        prompt = "(raw media payload; bytes are withheld from the review editor)"
        redacted_prompt = prompt
    else:
        try:
            prompt = json.dumps(result.payload, ensure_ascii=False, sort_keys=True)
        except Exception:
            prompt = str(result.payload)
        redacted_prompt = prompt
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
    )
    if approved is None:
        return {"approved": False}
    if has_binary:
        return {"approved": True, "payload": result.payload}
    try:
        edited = json.loads(str(approved))
    except Exception:
        # A manually edited scalar prompt is still a valid outbound payload for
        # transports whose input accepts plain text.
        edited = approved
    return {"approved": True, "payload": edited}


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
                if normalized in {"ollama", "mage-vl", "speech-recognition"}
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


def _contains_raw_media(value: Any, *, media_context: bool = False) -> bool:
    """Detect binary/data-URL/base64 media before cache lookup or redaction."""
    if _is_media_value(value):
        return True
    if isinstance(value, Mapping):
        # A plain ``data`` field is media when nested below a provider media
        # block (OpenAI input_audio/image, Anthropic source, etc.).  Explicit
        # *_base64 keys are always media, even when a provider omits the block
        # type and sends a compact payload.
        for key, item in value.items():
            normalized_key = str(key or "").strip().lower().replace("-", "_")
            child_context = media_context or normalized_key in _MEDIA_CONTAINER_KEYS
            if normalized_key in _MEDIA_BASE64_KEYS and _is_plain_base64(item):
                return True
            if normalized_key == "data" and _is_plain_base64(item):
                return True
            if _contains_raw_media(item, media_context=child_context):
                return True
        return False
    if isinstance(value, (list, tuple, set)):
        return any(_contains_raw_media(item, media_context=media_context) for item in value)
    return False


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
                alias = self._placeholder(category, raw_value)
                return f"{prefix}{alias}"
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
            (_SECRET_RE, "SECRET"),
            (_BEARER_RE, "SECRET"),
            (_JWT_RE, "JWT"),
            (_AWS_ACCESS_KEY_RE, "AWS_ACCESS_KEY"),
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

    async def _semantic_entities(self, text: str) -> tuple[list[dict[str, str]], str]:
        callback = self.semantic_redactor
        if callback is None:
            candidate = _get(self.config, "external_model_privacy.semantic_redactor", None)
            if callable(candidate):
                callback = candidate
        if not self.settings.semantic_redaction_enabled or not callback:
            return [], "disabled"
        try:
            try:
                result = callback(text, self.settings.local_model)  # type: ignore[misc]
            except TypeError:
                # Test/embedding adapters commonly expose a text-only
                # callable; accepting it does not change the sidecar trust
                # boundary because the callable is still explicitly injected.
                result = callback(text)
            if inspect.isawaitable(result):
                result = await result
            if isinstance(result, str):
                result = json.loads(result)
            entities = result.get("entities", []) if isinstance(result, Mapping) else result
            if not isinstance(entities, list):
                return [], "failed"
            normalized: list[dict[str, str]] = []
            for item in entities:
                if not isinstance(item, Mapping):
                    continue
                value = str(item.get("text") or "")
                category = str(item.get("category") or "SEMANTIC")
                if value and category and value in text:
                    normalized.append({"text": value, "category": category})
            return normalized, "success"
        except Exception:  # noqa: BLE001
            logger.warning("semantic privacy redactor failed", exc_info=True)
            return [], "failed"

    async def protect(
        self,
        payload: Any,
        *,
        provider: str,
        base_url: str | None = None,
        source_kind: str = "model_request",
        model: str | None = None,
    ) -> PrivacyResult:
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
            result = PrivacyResult(
                payload=payload,
                mode=self.settings.mode,
                provider_class=classification,
                source_kind=source_kind,
                provider=str(provider or ""),
                model=str(resolved_model or ""),
            )
            self._audit(result)
            return result

        cache_hit = False
        contains_media = _contains_raw_media(payload)
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
            "privacy-v3",
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
                )
                self._audit(result)
                return result
            except Exception:  # noqa: BLE001
                pass

        findings: list[RedactionFinding] = []
        semantic_status = "disabled"
        # ``_contains_raw_media`` also recognizes provider-nested plain
        # base64 (for example OpenAI ``input_audio.data``).  Seed the
        # transform flag from that preflight so ``raw_media_policy=confirm``
        # cannot accidentally inherit ``review_policy=never`` for nested
        # payloads whose leaf is an ordinary string.
        raw_media_found = contains_media

        async def transform(value: Any) -> Any:
            nonlocal raw_media_found, semantic_status
            if _is_media_value(value):
                raw_media_found = True
                if self.settings.raw_media_policy == "block":
                    raise RawMediaBlocked("raw media is blocked in protected privacy mode")
                # ``confirm`` keeps the original value only for an explicitly
                # approved review callback.  Without a callback the high-risk
                # review below fails closed; never silently replace binary
                # content with a marker and continue to an external endpoint.
                return value
            if isinstance(value, str):
                redacted, deterministic = self._deterministic_redact(value)
                findings.extend(deterministic)
                entities, status = await self._semantic_entities(redacted)
                semantic_status = status if status != "disabled" else semantic_status
                for entity in entities:
                    raw = entity["text"]
                    alias = self._placeholder(entity["category"], raw)
                    if raw in redacted:
                        redacted = redacted.replace(raw, alias)
                        findings.append(RedactionFinding(entity["category"], alias))
                return redacted
            if isinstance(value, Mapping):
                return {key: await transform(item) for key, item in value.items()}
            if isinstance(value, list):
                return [await transform(item) for item in value]
            if isinstance(value, tuple):
                return tuple([await transform(item) for item in value])
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
            )
            self._audit(blocked)
            raise
        except Exception as exc:  # noqa: BLE001
            # Never fall back to raw payload after a protected transformation
            # failure.  Callers can present a warning/review or stop the turn.
            raise PrivacyError("privacy redaction failed; outbound payload withheld") from exc

        # Semantic redactor failures are high risk.  It is safe to continue
        # only when deterministic findings made the payload non-raw and the
        # caller explicitly permits high-risk review handling.
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
        )
        if semantic_status == "failed" and self.settings.review_policy == "never":
            # A failed semantic pass must not silently degrade to a raw cloud
            # request.  ``high_risk``/``always`` can still be handled by the
            # existing human review dialog below.
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
            try:
                reviewed = await self._review(result)
            except PrivacyReviewDenied as exc:
                # Keep an auditable deny record even though the exception is
                # propagated to the transport caller and no payload is sent.
                review_status = "failed" if exc.__cause__ is not None else "denied"
                self._audit(replace(result, review=review_status, decision="deny"))
                raise
            result = replace(result, review="approved", decision="allow")
            if isinstance(reviewed, Mapping) and "payload" in reviewed:
                result = replace(result, payload=reviewed["payload"])
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
        """Synchronous transport adapter; fails clearly inside a running loop."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.protect(payload, **kwargs))
        # Provider sync calls run outside the event loop in normal operation.
        # If called from an active loop, execute the small transformation in a
        # worker thread rather than bypassing privacy.
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(lambda: asyncio.run(self.protect(payload, **kwargs))).result()

    async def _review(self, result: PrivacyResult) -> Any:
        callback = self.review_callback
        if callback is None:
            callback = lambda value: request_external_privacy_review(
                value,
                provider=value.provider,
                model=value.model,
                notify=self.settings.notify,
            )
        try:
            approved = callback(result)
            if inspect.isawaitable(approved):
                approved = await approved
            if isinstance(approved, Mapping):
                response = approved
                approved = response.get("approved", False)
                if approved is not False and approved is not None:
                    return response
            if approved is False or approved is None:
                raise PrivacyReviewDenied("external payload review was denied")
            return approved
        except PrivacyReviewDenied:
            raise
        except Exception as exc:  # noqa: BLE001
            raise PrivacyReviewDenied("external payload review failed") from exc

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
) -> PrivacyResult:
    """Convenience helper for synchronous provider adapters."""

    active = gateway or OutboundPrivacyGateway(config)
    return active.protect_sync(payload, provider=provider, base_url=base_url, source_kind=source_kind)


__all__ = [
    "CLI_PROVIDER_IDS",
    "EXTERNAL_PROVIDER_IDS",
    "LOCAL_PROVIDER_IDS",
    "ExternalProviderBlocked",
    "PrivacyPolicyContext",
    "OutboundPrivacyGateway",
    "PrivacyConfig",
    "PrivacyError",
    "PrivacyResult",
    "PrivacyReviewDenied",
    "RawMediaBlocked",
    "RedactionFinding",
    "is_external_provider",
    "privacy_config",
    "build_semantic_redactor",
    "request_external_privacy_review",
    "set_privacy_policy_context",
    "reset_privacy_policy_context",
    "get_privacy_policy_context",
    "effective_privacy_mode",
    "current_effective_privacy_mode",
    "protect_outbound_payload_sync",
    "provider_classification",
    "is_external_egress_tool_name",
]
