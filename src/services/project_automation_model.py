"""Project Automation LLM route resolution and ephemeral client ownership."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

from ..llm.openai_compatible_local_engine import DEFAULT_LOCAL_API_KEY


PROJECT_AUTOMATION_ROUTE_CONFIG_KEY = "model_routing.classes.project_automation"

_MODE_INHERIT = "inherit"
_MODE_DEDICATED = "dedicated"
_SUPPORTED_MODES = frozenset({_MODE_INHERIT, _MODE_DEDICATED})

# These CLI adapters do not have a repository-verified native-tool disable
# contract for plain generation. Codex CLI is intentionally absent: its
# generate_plain_text_async path explicitly sets disable_native_tools=True.
_PROJECT_OVERVIEW_UNVERIFIED_CLI_PROVIDERS = frozenset(
    {"antigravity-cli", "claude-cli", "grok-cli"}
)


class ProjectAutomationRouteError(RuntimeError):
    """Project Automation routing cannot be resolved safely."""

    # These values are intentionally small, stable, and safe to expose to an
    # operator.  They are not copies of provider exception messages (which may
    # contain credentials, URLs, or request headers).
    def __init__(
        self,
        message: str,
        *,
        code: str = "invalid_route",
        stage: str = "route_resolution",
    ) -> None:
        self.code = _safe_diagnostic_token(code, "invalid_route")
        self.stage = _safe_diagnostic_token(stage, "route_resolution")
        super().__init__(message)

    @property
    def diagnostic_code(self) -> str:
        """Compatibility alias used by service-level diagnostics."""

        return self.code


@dataclass(frozen=True)
class ProjectAutomationRoute:
    provider: str
    model: str
    mode: str
    base_url: str = ""
    api_key: str = ""
    reasoning_effort: str = ""

    @property
    def inherit(self) -> bool:
        return self.mode == _MODE_INHERIT

    def to_dict(self, *, include_secret: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "inherit": self.inherit,
            "mode": self.mode,
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "reasoning_effort": self.reasoning_effort,
        }
        if include_secret:
            payload["api_key"] = self.api_key
        else:
            payload["api_key_configured"] = bool(self.api_key)
        return payload


_SAFE_DIAGNOSTIC_TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,127}$")


def _safe_diagnostic_token(value: Any, fallback: str) -> str:
    """Return a bounded token suitable for logs/API diagnostics."""

    normalized = str(value or "").strip().casefold()
    if _SAFE_DIAGNOSTIC_TOKEN_RE.fullmatch(normalized):
        return normalized
    return fallback


def _safe_route_observation(value: Any) -> str | None:
    """Return an enum-like route identifier without secret-like material."""

    text = _optional_text(value)
    if not text or len(text) > 256:
        return None
    if "://" in text or any(char in text for char in "@?#"):
        return None
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in text):
        return None
    normalized = text.casefold()
    if not _SAFE_DIAGNOSTIC_TOKEN_RE.fullmatch(normalized):
        return None
    return normalized


def _config_get(config: Any, key: str, default: Any = None) -> Any:
    if config is None:
        return default

    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            value = getter(key, None)
        except TypeError:
            value = getter(key)
        if value is not None:
            return value

    if isinstance(config, dict):
        current: Any = config
        for part in key.split("."):
            if not isinstance(current, dict) or part not in current:
                return default
            current = current[part]
        return current

    return default


def _route_config(config: Any) -> dict[str, Any]:
    raw = _config_get(config, PROJECT_AUTOMATION_ROUTE_CONFIG_KEY, {})
    return dict(raw) if isinstance(raw, dict) else {}


def _optional_text(value: Any) -> str:
    return str(value or "").strip()


def _required_text(value: Any, *, field: str) -> str:
    text = _optional_text(value)
    if not text:
        raise ProjectAutomationRouteError(
            f"Project Automation route requires {field}",
            code=f"missing_{field}",
        )
    if "\x00" in text:
        raise ProjectAutomationRouteError(
            f"Project Automation route has invalid {field}",
            code=f"invalid_{field}",
        )
    return text


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)

    normalized = str(value).strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False

    raise ProjectAutomationRouteError(
        "Project Automation route has invalid inherit value",
        code="invalid_inherit",
    )


def _resolve_mode(raw: dict[str, Any]) -> str:
    explicit_mode = _optional_text(raw.get("mode")).casefold()
    explicit_inherit = _optional_bool(
        raw.get("inherit") if "inherit" in raw else None
    )

    if explicit_mode:
        if explicit_mode not in _SUPPORTED_MODES:
            raise ProjectAutomationRouteError(
                f"Unsupported Project Automation routing mode: {explicit_mode}",
                code="unsupported_mode",
            )
        mode = explicit_mode
    elif explicit_inherit is None:
        mode = _MODE_INHERIT
    else:
        mode = _MODE_INHERIT if explicit_inherit else _MODE_DEDICATED

    if explicit_inherit is not None:
        if explicit_inherit != (mode == _MODE_INHERIT):
            raise ProjectAutomationRouteError(
                "Project Automation routing mode conflicts with inherit",
                code="mode_conflict",
            )

    return mode


def _supported_target_providers() -> frozenset[str]:
    try:
        from ..llm.manager import TARGET_CLIENT_PROVIDERS
    except Exception as exc:
        raise ProjectAutomationRouteError(
            "Project Automation provider registry is unavailable",
            code="provider_registry_unavailable",
        ) from exc

    providers = frozenset(
        str(provider or "").strip().casefold()
        for provider in TARGET_CLIENT_PROVIDERS
        if str(provider or "").strip()
    )
    if not providers:
        raise ProjectAutomationRouteError(
            "Project Automation provider registry is empty",
            code="provider_registry_empty",
        )
    return providers


def _validate_provider(value: Any) -> str:
    provider = _required_text(value, field="provider").casefold()
    if provider not in _supported_target_providers():
        raise ProjectAutomationRouteError(
            f"Unsupported Project Automation provider: {provider}",
            code="unsupported_provider",
        )
    return provider


def _validate_model(value: Any) -> str:
    model = _required_text(value, field="model")
    if len(model) > 512:
        raise ProjectAutomationRouteError(
            "Project Automation route has invalid model",
            code="invalid_model",
        )
    return model


def _validate_optional_route_text(
    value: Any,
    *,
    field: str,
    max_length: int,
) -> str:
    text = _optional_text(value)
    if "\x00" in text or len(text) > max_length:
        raise ProjectAutomationRouteError(
            f"Project Automation route has invalid {field}",
            code=f"invalid_{field}",
        )
    return text


def resolve_project_automation_route(
    config: Any,
) -> ProjectAutomationRoute:
    """Resolve the effective Project Automation LLM route."""

    raw = _route_config(config)
    mode = _resolve_mode(raw)

    if mode == _MODE_INHERIT:
        provider = _validate_provider(
            _config_get(config, "llm_provider", "")
        )
        model = _validate_model(
            _config_get(config, "llm_model", "")
        )
        return ProjectAutomationRoute(
            provider=provider,
            model=model,
            mode=_MODE_INHERIT,
        )

    provider = _validate_provider(raw.get("provider"))
    model = _validate_model(raw.get("model"))
    base_url = _validate_optional_route_text(
        raw.get("base_url"),
        field="base_url",
        max_length=2048,
    )
    api_key = _validate_optional_route_text(
        raw.get("api_key"),
        field="api_key",
        max_length=8192,
    )
    reasoning_effort = _validate_optional_route_text(
        raw.get("reasoning_effort"),
        field="reasoning_effort",
        max_length=64,
    )

    return ProjectAutomationRoute(
        provider=provider,
        model=model,
        mode=_MODE_DEDICATED,
        base_url=base_url,
        api_key=api_key,
        reasoning_effort=reasoning_effort,
    )


# Provider credentials are deliberately represented as presence flags in the
# diagnostics surface.  Keep this map local to this module so adding a new
# provider cannot accidentally make a secret value part of an API response.
_PROVIDER_CREDENTIALS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "openai": (("openai_api_key",), ("OPENAI_API_KEY",)),
    "openrouter": (("openrouter_api_key",), ("OPENROUTER_API_KEY",)),
    "gemini": (("gemini_api_key",), ("GEMINI_API_KEY",)),
    "deepseek": (("deepseek_api_key",), ("DEEPSEEK_API_KEY",)),
    "deepinfra": (("deepinfra_api_key",), ("DEEPINFRA_TOKEN",)),
    "kimi": (("kimi_api_key",), ("MOONSHOT_API_KEY",)),
    "ollama": (("ollama_api_key",), ("OLLAMA_API_KEY",)),
    "sglang": (("sglang_api_key",), ("SGLANG_API_KEY",)),
    "openai_compatible_local": (
        (
            "runtime.target_api_key",
            "openai_compatible_local.api_key",
            "openai_compatible_local_api_key",
        ),
        ("OPENAI_COMPATIBLE_LOCAL_API_KEY",),
    ),
    "codex-cli": (("codex_cli.api_key",), ("CODEX_API_KEY",)),
    "claude-cli": (("claude_cli.api_key",), ("ANTHROPIC_API_KEY",)),
    "grok-cli": (("grok_cli.api_key",), ("XAI_API_KEY",)),
    "antigravity-cli": (("antigravity_cli.api_key",), ()),
}

_PROVIDER_BASE_URLS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "openai": (
        ("openai.base_url", "openai_base_url"),
        ("OPENAI_BASE_URL",),
    ),
    "openrouter": (("openrouter_base_url", "openrouter.base_url"), ("OPENROUTER_BASE_URL",)),
    "deepseek": (("deepseek_base_url", "deepseek.base_url"), ("DEEPSEEK_BASE_URL",)),
    "deepinfra": (("deepinfra_base_url", "deepinfra.base_url"), ("DEEPINFRA_BASE_URL",)),
    "kimi": (("kimi_base_url", "kimi.base_url"), ("MOONSHOT_BASE_URL",)),
    "ollama": (("ollama_base_url", "ollama.base_url"), ("OLLAMA_BASE_URL",)),
    "openai_compatible_local": (
        (
            "runtime.target_base_url",
            "openai_compatible_local.base_url",
            "openai_compatible_local_base_url",
        ),
        ("OPENAI_COMPATIBLE_LOCAL_BASE_URL",),
    ),
    "sglang": (("sglang_base_url", "sglang.base_url"), ("SGLANG_BASE_URL",)),
}


def _configured_value(config: Any, paths: tuple[str, ...]) -> bool:
    return any(bool(_optional_text(_config_get(config, path, ""))) for path in paths)


def _configured_environment(names: tuple[str, ...]) -> bool:
    return any(bool(_optional_text(os.getenv(name, ""))) for name in names)


def _provider_credential_configured(
    config: Any,
    route: ProjectAutomationRoute,
) -> bool:
    provider = str(route.provider or "").strip().casefold()
    if _optional_text(route.api_key):
        # Dedicated route-owned credentials have highest precedence for both
        # local and non-local providers.
        return True
    if provider == "openai_compatible_local":
        # The local resolver has its own precedence rules (including the safe
        # built-in development key), so reuse it instead of duplicating them.
        return bool(resolve_project_automation_local_api_key(config, route=route))
    paths, env_names = _PROVIDER_CREDENTIALS.get(provider, ((), ()))
    return _configured_value(config, paths) or _configured_environment(env_names)


def _provider_base_url_configured(
    config: Any,
    route: ProjectAutomationRoute,
) -> bool:
    if route.base_url:
        return True
    provider = str(route.provider or "").strip().casefold()
    paths, env_names = _PROVIDER_BASE_URLS.get(provider, ((), ()))
    return _configured_value(config, paths) or _configured_environment(env_names)


def _safe_route_candidate(config: Any) -> dict[str, Any]:
    """Return non-secret route hints even when validation fails."""

    raw = _route_config(config)
    explicit_mode = _optional_text(raw.get("mode")).casefold()
    explicit_inherit_raw = raw.get("inherit") if "inherit" in raw else None
    try:
        explicit_inherit = (
            _optional_bool(explicit_inherit_raw)
            if explicit_inherit_raw is not None
            else None
        )
    except ProjectAutomationRouteError:
        explicit_inherit = None
    provider = _safe_route_observation(raw.get("provider"))
    provider = provider.casefold() if provider else None
    model = _safe_route_observation(raw.get("model"))
    if not explicit_mode and explicit_inherit is not False:
        provider = _safe_route_observation(
            _config_get(config, "llm_provider", "")
        )
        provider = provider.casefold() if provider else None
        model = _safe_route_observation(
            _config_get(config, "llm_model", "")
        )
    inferred_inherit = (
        explicit_inherit
        if explicit_inherit is not None
        else explicit_mode != _MODE_DEDICATED
    )
    return {
        "mode": explicit_mode or (
            _MODE_DEDICATED if explicit_inherit is False else _MODE_INHERIT
        ),
        "inherit": inferred_inherit,
        "provider": provider or None,
        "model": model or None,
        "base_url_configured": bool(_optional_text(raw.get("base_url"))),
        "api_key_configured": bool(_optional_text(raw.get("api_key"))),
        "reasoning_effort_configured": bool(
            _optional_text(raw.get("reasoning_effort"))
        ),
    }


def diagnose_project_automation_route(config: Any) -> dict[str, Any]:
    """Return a side-effect-free, secret-free effective route diagnostic.

    This function intentionally does not instantiate a provider client or make
    a network request.  It is safe for an operator-only diagnostics endpoint
    and for startup/worker logs.  Provider/model identifiers are useful
    operator context; credentials and endpoint values are reduced to boolean
    ``*_configured`` flags.
    """

    candidate = _safe_route_candidate(config)
    try:
        route = resolve_project_automation_route(config)
    except ProjectAutomationRouteError as exc:
        candidate.update(
            {
                "ok": False,
                "stage": exc.stage,
                "code": exc.code,
                "route_valid": False,
                "credential_required": bool(
                    candidate.get("provider")
                    and candidate.get("model")
                ),
            }
        )
        return candidate
    except Exception:
        # A malformed third-party config object should not turn diagnostics
        # into a traceback or leak its representation.
        candidate.update(
            {
                "ok": False,
                "stage": "route_resolution",
                "code": "invalid_route",
                "route_valid": False,
                "credential_required": False,
            }
        )
        return candidate

    candidate.update(
        {
            "ok": True,
            "stage": "route_resolution",
            "code": "ok",
            "route_valid": True,
            "mode": route.mode,
            "inherit": route.inherit,
            "provider": _safe_route_observation(route.provider),
            "model": _safe_route_observation(route.model),
            "reasoning_effort": _safe_route_observation(route.reasoning_effort),
            "base_url_configured": _provider_base_url_configured(config, route),
            "api_key_configured": _provider_credential_configured(config, route),
            "credential_required": route.provider
            not in {
                "ollama",
                "openai_compatible_local",
                "sglang",
                "routing-profile",
            },
        }
    )
    # A route can be structurally valid while still being unusable because a
    # required provider credential is absent.  Report that distinction without
    # embedding the provider SDK's eventual exception/message.
    if bool(candidate["credential_required"]) and not bool(
        candidate["api_key_configured"]
    ):
        candidate.update(
            {
                "ok": False,
                "stage": "credential_resolution",
                "code": "missing_api_key",
            }
        )
    return candidate


def resolve_project_automation_local_api_key(
    config: Any,
    route: ProjectAutomationRoute | None = None,
) -> str:
    """Resolve the effective API key for a local Project Automation route.

    Project Automation may inherit the main local provider, in which case the
    inherited route does not carry a dedicated key.  Match the local client
    precedence while keeping non-local providers from receiving local secrets.
    """

    resolved = route or resolve_project_automation_route(config)
    if str(resolved.provider or "").strip().casefold() != (
        "openai_compatible_local"
    ):
        return ""

    candidates = (
        resolved.api_key,
        _config_get(config, "runtime.target_api_key"),
        os.getenv("OPENAI_COMPATIBLE_LOCAL_API_KEY"),
        _config_get(config, "openai_compatible_local.api_key"),
        DEFAULT_LOCAL_API_KEY,
    )
    for candidate in candidates:
        value = str(candidate or "").strip()
        if value:
            return value
    return ""


def diagnose_project_automation_client(client: Any) -> dict[str, Any]:
    """Describe the OpenAI-style transport that was actually constructed.

    The transport helper reduces URLs to scheme/host/port and strips
    credentials, paths and query strings. This function never returns API
    keys, request/response content or exception text.
    """

    transport = getattr(client, "_openai_client", None)
    if transport is None:
        return {}
    try:
        from ..llm.native_runtime import openai_transport_diagnostic

        raw = openai_transport_diagnostic(transport)
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    allowed = {
        "base_url_origin",
        "proxy_mode",
        "proxy_source",
        "proxy_configured",
        "proxy_bypassed",
        "proxy_origin",
    }
    return {
        str(key): value
        for key, value in raw.items()
        if key in allowed
    }


def create_project_automation_llm_client(
    config: Any,
    *,
    route: ProjectAutomationRoute | None = None,
    enable_tools: bool = False,
    lightweight_client: bool = False,
) -> Any:
    """Create one ephemeral Project Automation client.

    Overview uses ``enable_tools=False`` plus ``lightweight_client=True``.
    Project Steward deliberately uses ``enable_tools=True`` and then installs
    its strict physical/read-only registry.
    """

    resolved = route or resolve_project_automation_route(config)

    try:
        from ..llm.manager import create_llm_client_for_target

        return create_llm_client_for_target(
            config,
            provider=resolved.provider,
            model=resolved.model,
            effort=resolved.reasoning_effort,
            base_url=resolved.base_url,
            api_key=resolved.api_key,
            provider_options={
                "ephemeral_session_client": True,
                "enable_tools": bool(enable_tools),
                "lightweight_client": bool(lightweight_client),
            },
        )
    except ProjectAutomationRouteError:
        raise
    except Exception as exc:
        raise ProjectAutomationRouteError(
            "Project Automation LLM client could not be created",
            code="client_creation_failed",
            stage="client_creation",
        ) from exc


def create_project_overview_llm_client(
    config: Any,
    *,
    route: ProjectAutomationRoute | None = None,
) -> Any:
    """Create the tool-disabled, lightweight client used by Overview."""

    resolved = route or resolve_project_automation_route(config)
    provider = str(resolved.provider or "").strip().casefold()
    if provider in _PROJECT_OVERVIEW_UNVERIFIED_CLI_PROVIDERS:
        raise ProjectAutomationRouteError(
            "Project Overview requires a provider with verified tool-free "
            "plain generation",
            code="overview_provider_not_tool_isolatable",
            stage="route_resolution",
        )

    return create_project_automation_llm_client(
        config,
        route=resolved,
        enable_tools=False,
        lightweight_client=True,
    )


async def cleanup_project_automation_llm_client(client: Any) -> None:
    """Release an ephemeral Project Automation client through the shared owner."""

    if client is None:
        return

    try:
        from .session_llm_generation import cleanup_ephemeral_llm_client

        await cleanup_ephemeral_llm_client(client)
    except Exception as exc:
        raise ProjectAutomationRouteError(
            "Project Automation LLM client cleanup failed"
        ) from exc


__all__ = [
    "PROJECT_AUTOMATION_ROUTE_CONFIG_KEY",
    "ProjectAutomationRoute",
    "ProjectAutomationRouteError",
    "cleanup_project_automation_llm_client",
    "create_project_automation_llm_client",
    "create_project_overview_llm_client",
    "diagnose_project_automation_client",
    "diagnose_project_automation_route",
    "resolve_project_automation_route",
    "resolve_project_automation_local_api_key",
]
