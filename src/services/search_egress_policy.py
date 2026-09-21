"""Fail-closed egress preconditions for public search providers.

Normal web search has two very different routes: an operator-approved hosted
provider (currently OpenAI) and public HTTP search engines (Yahoo, Wikipedia,
etc.).  This module keeps the deployment/profile checks in one small,
dependency-free boundary so callers can perform them *before* privacy
redaction, permission prompts, or construction of an HTTP client.

The policy is intentionally conservative.  Personal deployments retain their
historical behaviour; Enterprise deployments must explicitly opt in to public
egress and Hosted Search additionally requires a non-empty, provider-scoped
credential.  Boolean settings are parsed strictly -- in particular the string
``"false"`` is never truthy merely because it is non-empty.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit


TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
FALSE_VALUES = frozenset({"", "0", "false", "no", "off"})

# Names used by the local search client and by provider/tool adapters.  Keep
# this list explicit: an arbitrary provider must not accidentally become a
# public route merely because its name contains ``search``.
HOSTED_SEARCH_ENGINES = frozenset(
    {
        "openai",
        "hosted",
        "hosted_openai",
        "openai_hosted",
        "openai_search",
        "grok",
        "xai",
        "grok_x_search",
    }
)
PUBLIC_SEARCH_ENGINES = frozenset(
    {
        "duckduckgo",
        "ddg",
        "yahoo",
        "yahoo_realtime",
        "wikipedia",
        "arxiv",
        "openalex",
        "pubmed",
    }
)
LOCAL_SEARCH_ENGINES = frozenset(
    {
        "local",
        "searxng",
        "local_knowledge",
        "memory",
        "bm25",
        "ollama",
        "sglang",
        "openai_compatible_local",
    }
)
NETWORK_LOCAL_SEARCH_ENGINES = frozenset(
    {"searxng", "ollama", "sglang", "openai_compatible_local"}
)

_MISSING = object()


def strict_bool(value: Any, default: bool = False) -> bool:
    """Parse a configuration boolean without Python truthiness surprises.

    ``None`` means that a setting was not supplied and returns ``default``.
    The accepted vocabulary mirrors the repository's other strict config
    parsers.  Invalid values fail closed to ``False``.
    """

    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in TRUE_VALUES:
            return True
        if normalized in FALSE_VALUES:
            return False
    return False


# Compatibility spellings used by callers/tests in adjacent workstreams.
parse_strict_bool = strict_bool
strict_config_bool = strict_bool


def _config_get(config: Any, key: str, default: Any = None) -> Any:
    if config is None:
        return default
    if isinstance(config, Mapping):
        value: Any = config
        for part in key.split("."):
            if not isinstance(value, Mapping) or part not in value:
                break
            value = value[part]
        else:
            return value
    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            value = getter(key, default)
        except TypeError:
            try:
                value = getter(key)
            except Exception:
                value = default
        except Exception:
            value = default
        if value is not None:
            return value
    return default


def _first_config_value(config: Any, keys: tuple[str, ...], default: Any = None) -> Any:
    for key in keys:
        value = _config_get(config, key, _MISSING)
        if value is not _MISSING and value is not None:
            return value
    return default


def is_enterprise_profile() -> bool:
    """Resolve Enterprise fail-closed from the central profile or env vars."""

    try:
        from ..features import Features

        return bool(Features.is_enterprise())
    except Exception:
        # Keep this fallback independent of Config so the gate can run during
        # bootstrap and in small provider-only tests.
        return any(
            str(os.getenv(name) or "").strip().casefold() == "enterprise"
            for name in ("AOITALK_PROFILE", "AIVTUBER_ENV")
        )


def normalize_search_engine(engine: Any) -> str:
    normalized = str(engine or "").strip().casefold().replace("-", "_")
    if normalized == "ddg":
        return "duckduckgo"
    if normalized in {"yahoo", "yahoo_realtime_search"}:
        return "yahoo_realtime"
    if normalized in {"hosted_openai", "openai_hosted", "openai_search"}:
        return "openai"
    return normalized


def _configured_credential(config: Any, engine: str) -> str:
    """Read only the credential belonging to the selected hosted provider."""

    normalized = normalize_search_engine(engine)
    if normalized == "openai":
        value = _first_config_value(
            config,
            (
                "search.openai_api_key",
                "openai_api_key",
            ),
            default=None,
        )
        if value is None:
            value = os.getenv("AOITALK_SEARCH_OPENAI_API_KEY")
        if value is None:
            value = os.getenv("OPENAI_API_KEY")
    elif normalized in {"grok", "xai", "grok_x_search"}:
        value = _first_config_value(
            config,
            (
                "search.grok_api_key",
                "search.xai_api_key",
                "grok_api_key",
                "xai_api_key",
            ),
            default=None,
        )
        if value is None:
            value = os.getenv("XAI_API_KEY") or os.getenv("GROK_API_KEY")
    else:
        # Public HTML/API engines intentionally do not borrow OPENAI_API_KEY
        # or another primary LLM credential.
        value = _first_config_value(
            config,
            (
                f"search.{normalized}_api_key",
                f"{normalized}_api_key",
            ),
            default=None,
        )
    return str(value or "").strip()


def configured_openai_api_key(config: Any = None) -> str:
    """Return the configured OpenAI search credential without exposing it."""

    return _configured_credential(config, "openai")


def approved_public_egress(config: Any = None) -> bool:
    """Return whether Enterprise public HTTP egress was explicitly enabled."""

    # Deployment environment is an explicit operator override and must win
    # over a persisted/default ``false`` value.  Parse it strictly so an
    # accidental value such as ``"enabled"`` remains fail-closed.
    for env_name in (
        "AOITALK_SEARCH_PUBLIC_EGRESS_ENABLED",
        "AOITALK_PUBLIC_SEARCH_EGRESS_ENABLED",
        "AOITALK_SEARCH_EGRESS_ENABLED",
        "AOITALK_HOSTED_SEARCH_EGRESS_ENABLED",
    ):
        if env_name in os.environ:
            return strict_bool(os.environ.get(env_name), False)
    configured = _first_config_value(
        config,
        (
            "search.public_egress_enabled",
            "search.public_search_egress_enabled",
            "search.external_egress_enabled",
            "search.egress_enabled",
            # Existing deployments use this key for the approved hosted route;
            # accepting it as an explicit egress approval preserves that
            # operator contract for Yahoo/public engines as well.
            "search.hosted_egress_enabled",
            "search.enterprise_hosted_egress_enabled",
        ),
        default=_MISSING,
    )
    if configured is not _MISSING:
        return strict_bool(configured, False)
    return False


def hosted_egress_approved(config: Any = None) -> bool:
    """Return whether the Hosted Search egress flag is explicitly enabled."""

    for env_name in (
        "AOITALK_SEARCH_HOSTED_EGRESS_ENABLED",
        "AOITALK_HOSTED_SEARCH_EGRESS_ENABLED",
        "AOITALK_SEARCH_EGRESS_ENABLED",
    ):
        if env_name in os.environ:
            return strict_bool(os.environ.get(env_name), False)
    configured = _first_config_value(
        config,
        (
            "search.hosted_egress_enabled",
            "search.enterprise_hosted_egress_enabled",
        ),
        default=_MISSING,
    )
    if configured is not _MISSING:
        return strict_bool(configured, False)
    return False


def resolve_searxng_url(config: Any = None) -> str:
    """Resolve the explicitly configured internal SearXNG endpoint."""

    configured = (
        os.getenv("AOITALK_DEEP_RESEARCH_SEARXNG_URL")
        or _config_get(config, "deep_research.searxng_url", "")
        or _config_get(config, "search.searxng_url", "")
    )
    return str(configured or "").strip().rstrip("/")


def _endpoint_is_local(endpoint: Any) -> bool:
    value = str(endpoint or "").strip()
    if not value:
        return False
    try:
        host = (urlsplit(value).hostname or "").strip().casefold().rstrip(".")
    except ValueError:
        return False
    if host in {"localhost", "localhost.localdomain"}:
        return True
    # IPv4/IPv6 loopback only.  Private/LAN hosts require explicit public
    # egress approval and are not silently treated as local.
    return host in {"127.0.0.1", "::1", "[::1]"}


def _endpoint_host(endpoint: Any) -> str:
    """Return a normalized endpoint host for explicit trust evaluation."""

    try:
        return (urlsplit(str(endpoint or "").strip()).hostname or "").strip().casefold().rstrip(".")
    except ValueError:
        return ""


def _endpoint_is_configured_local(config: Any, endpoint: Any) -> bool:
    """Whether the operator explicitly trusts a non-loopback local endpoint."""

    host = _endpoint_host(endpoint)
    if not host:
        return False
    configured = _first_config_value(
        config,
        (
            "search.trusted_local_hosts",
            "external_model_privacy.trusted_local_hosts",
        ),
        default=None,
    )
    if isinstance(configured, str):
        configured = configured.split(",")
    if not isinstance(configured, (list, tuple, set, frozenset)):
        configured = []
    trusted = {
        str(item).strip().casefold().rstrip(".")
        for item in configured
        if str(item).strip()
    }
    return host in trusted


@dataclass(frozen=True)
class SearchEgressDecision:
    """Machine-readable outcome that is safe to expose in logs/UI."""

    allowed: bool
    code: str = "ok"
    engine: str = ""
    credential_present: bool = False
    endpoint_local: bool = False

    def __bool__(self) -> bool:
        return self.allowed


class SearchEgressPreconditionError(RuntimeError):
    """Stable, secret-free failure raised before an outbound search request."""

    def __init__(self, code: str, *, engine: str = "") -> None:
        allowed_codes = {
            "credential_missing",
            "credential_invalid",
            "egress_unreachable",
            "provider_invalid",
            "local_search_unavailable",
        }
        self.code = code if code in allowed_codes else "egress_unreachable"
        self.engine = normalize_search_engine(engine)
        super().__init__(self.code)


# Name requested by adjacent tool-policy integrations.
SearchEgressError = SearchEgressPreconditionError
SearchEgressPrecondition = SearchEgressPreconditionError


def check_search_egress(
    config: Any = None,
    provider: Any = "openai",
    *,
    credential: Any = None,
    endpoint: Any = None,
    require_credential: bool | None = None,
) -> SearchEgressDecision:
    """Evaluate the Enterprise precondition without raising.

    Callers that need a fail-closed boundary should use one of the assertion
    helpers below.  Returning a decision keeps status/readiness APIs from
    having to catch a control-flow exception.
    """

    engine = normalize_search_engine(provider)
    if engine in LOCAL_SEARCH_ENGINES:
        # A provider id of ``searxng`` is not sufficient proof that the URL is
        # an internal service.  In Enterprise, a non-loopback endpoint must be
        # explicitly listed as a trusted local host or covered by the same
        # deliberate public-egress approval used by other HTTP engines.
        if is_enterprise_profile() and engine in NETWORK_LOCAL_SEARCH_ENGINES:
            endpoint_local = _endpoint_is_local(endpoint) or _endpoint_is_configured_local(
                config, endpoint
            )
            # A network-capable local provider is not safe when the endpoint
            # is omitted: the downstream adapter could choose an arbitrary
            # default/redirect.  Non-loopback endpoints require either an
            # explicit trusted-host entry or deliberate public-egress approval.
            if not endpoint:
                return SearchEgressDecision(False, "egress_unreachable", engine=engine)
            if not endpoint_local and not approved_public_egress(config):
                return SearchEgressDecision(False, "egress_unreachable", engine=engine)
        else:
            endpoint_local = _endpoint_is_local(endpoint) or _endpoint_is_configured_local(
                config, endpoint
            )
        return SearchEgressDecision(
            allowed=True,
            engine=engine,
            endpoint_local=endpoint_local,
        )
    if engine not in HOSTED_SEARCH_ENGINES and engine not in PUBLIC_SEARCH_ENGINES:
        return SearchEgressDecision(False, "provider_invalid", engine=engine)
    if not is_enterprise_profile():
        # Personal compatibility: no new egress gate.  Credential checks are
        # still performed by the selected provider's own transport.
        return SearchEgressDecision(
            allowed=True,
            engine=engine,
            credential_present=bool(str(credential if credential is not None else _configured_credential(config, engine)).strip()),
            endpoint_local=_endpoint_is_local(endpoint),
        )

    if engine in HOSTED_SEARCH_ENGINES:
        need_credential = True if require_credential is None else bool(require_credential)
        resolved_credential = (
            str(credential or "").strip()
            if credential is not None
            else _configured_credential(config, engine)
        )
        if need_credential and not resolved_credential:
            return SearchEgressDecision(
                False,
                "credential_missing",
                engine=engine,
                credential_present=False,
            )
        if not hosted_egress_approved(config):
            return SearchEgressDecision(
                False,
                "egress_unreachable",
                engine=engine,
                credential_present=bool(resolved_credential),
            )
        return SearchEgressDecision(
            True,
            engine=engine,
            credential_present=bool(resolved_credential),
        )

    # Public search engines do not have a shared credential requirement, but
    # still need a deliberate Enterprise egress approval.  A local endpoint
    # (for example an internal SearXNG-compatible proxy) is not public and is
    # allowed to proceed without that flag.
    local_endpoint = _endpoint_is_local(endpoint)
    if not local_endpoint and not approved_public_egress(config):
        return SearchEgressDecision(False, "egress_unreachable", engine=engine)
    return SearchEgressDecision(True, engine=engine, endpoint_local=local_endpoint)


def assert_search_egress_approved(
    config: Any = None,
    provider: Any = "openai",
    *,
    credential: Any = None,
    endpoint: Any = None,
    require_credential: bool | None = None,
) -> SearchEgressDecision:
    decision = check_search_egress(
        config,
        provider,
        credential=credential,
        endpoint=endpoint,
        require_credential=require_credential,
    )
    if not decision.allowed:
        raise SearchEgressPreconditionError(decision.code, engine=decision.engine)
    return decision


def assert_public_search_egress_approved(
    config: Any = None,
    *,
    engine: Any = "public",
    endpoint: Any = None,
) -> SearchEgressDecision:
    """Assert the Enterprise egress gate for a public search engine."""

    normalized = normalize_search_engine(engine)
    # ``public`` is a convenience for adapters that do not expose a concrete
    # engine name.  It intentionally maps to a public route, not Hosted OpenAI.
    if normalized == "public":
        normalized = "duckduckgo"
    return assert_search_egress_approved(config, normalized, endpoint=endpoint, require_credential=False)


def assert_openai_hosted_search_ready(config: Any = None) -> SearchEgressDecision:
    return assert_search_egress_approved(config, "openai", require_credential=True)


def search_egress_error_message(error: SearchEgressPreconditionError) -> str:
    """Render a stable Japanese message without exposing config/secret data."""

    messages = {
        "credential_missing": "検索の前提条件を満たせません（credential_missing）。管理者に検索プロバイダの認証情報を設定してください。",
        "credential_invalid": "検索の認証情報が無効です（credential_invalid）。管理者に設定を確認してください。",
        "egress_unreachable": "検索の前提条件を満たせません（egress_unreachable）。承認済みネットワーク経路を設定してください。",
        "provider_invalid": "検索の前提条件を満たせません（provider_invalid）。検索プロバイダ設定を確認してください。",
        "local_search_unavailable": "検索の前提条件を満たせません（local_search_unavailable）。内部検索エンジンを設定してください。",
    }
    return messages.get(getattr(error, "code", ""), messages["egress_unreachable"])


# Compatibility alias used by the MCP bridge and older tool adapters.
sanitized_precondition_message = search_egress_error_message


__all__ = [
    "HOSTED_SEARCH_ENGINES",
    "PUBLIC_SEARCH_ENGINES",
    "LOCAL_SEARCH_ENGINES",
    "NETWORK_LOCAL_SEARCH_ENGINES",
    "SearchEgressDecision",
    "SearchEgressError",
    "SearchEgressPrecondition",
    "SearchEgressPreconditionError",
    "strict_bool",
    "parse_strict_bool",
    "strict_config_bool",
    "is_enterprise_profile",
    "normalize_search_engine",
    "approved_public_egress",
    "hosted_egress_approved",
    "resolve_searxng_url",
    "configured_openai_api_key",
    "check_search_egress",
    "assert_search_egress_approved",
    "assert_public_search_egress_approved",
    "assert_openai_hosted_search_ready",
    "search_egress_error_message",
    "sanitized_precondition_message",
]
