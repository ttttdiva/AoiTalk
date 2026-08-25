"""Resolve the runtime LLM deployment contract.

The database stores an operator's provider/model selection, while an
Enterprise release may constrain the backend that is actually running.  This
module is deliberately independent from :mod:`src.config`: it can therefore
be used by startup, the model catalog and API routes without mutating the
persisted configuration.  Personal installations (or Enterprise installs
without an explicit deployment backend) retain the historical behaviour.

The resolver is intentionally conservative.  A fixed deployment never uses a
persisted provider merely because that provider happens to be present in the
database; callers must use ``effective_overrides`` before constructing a
client and ``preflight_deployment`` before accepting a hot switch.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional, Sequence, Tuple
from urllib.parse import urlsplit, urlunsplit


class DeploymentConfigurationError(RuntimeError):
    """The deployment environment contains an unknown or unsafe backend."""


class DeploymentMismatchError(RuntimeError):
    """A requested provider/model cannot be used by the active deployment."""


_MISSING = object()

# Keep the provider list in one place for availability metadata.  ``xai`` is
# not an AoiTalk chat provider yet, so it intentionally does not appear here;
# the secret can still be supplied for future provider implementations.
KNOWN_PROVIDER_IDS: Tuple[str, ...] = (
    "openai",
    "gemini",
    "openrouter",
    "deepseek",
    "deepinfra",
    "kimi",
    "ollama",
    "sglang",
    "openai_compatible_local",
    "routing-profile",
    "codex-cli",
    "claude-cli",
    "antigravity-cli",
    "grok-cli",
)

EXTERNAL_PROVIDER_IDS: Tuple[str, ...] = (
    "openai",
    "gemini",
    "openrouter",
    "deepseek",
    "deepinfra",
    "kimi",
    "openai_compatible_local",
    "codex-cli",
    "claude-cli",
    "antigravity-cli",
    "grok-cli",
)

# These providers use their SDK's official API endpoint when no override is
# configured.  OpenAI-compatible/custom endpoints remain explicit so a
# missing URL cannot silently send traffic to the wrong service.
_BASE_URL_OPTIONAL_PROVIDER_IDS: Tuple[str, ...] = (
    "openai",
    "gemini",
    "codex-cli",
    "claude-cli",
    "antigravity-cli",
    "grok-cli",
)

_BACKEND_ALIASES = {
    "external": "external",
    "core": "external",
    "http": "external",
    "remote": "external",
    "gemma-vllm": "gemma-vllm",
    "gemma_vllm": "gemma-vllm",
    "gemma": "gemma-vllm",
    "vllm": "gemma-vllm",
    "deepseek-llamacpp": "deepseek-llamacpp",
    "deepseek_llamacpp": "deepseek-llamacpp",
    "deepseek-llama.cpp": "deepseek-llamacpp",
    "llama.cpp": "deepseek-llamacpp",
    "llamacpp": "deepseek-llamacpp",
    "sglang-cuda": "sglang-cuda",
    "sglang_cuda": "sglang-cuda",
    "sglang": "sglang-cuda",
}

_BACKEND_ENV_NAMES: Tuple[str, ...] = (
    # New explicit contract.  The aliases below keep the existing Compose and
    # operator files working while they are migrated by the deployment layer.
    "AOITALK_ENTERPRISE_BACKEND",
    "AOITALK_BACKEND",
    "AOITALK_ACTIVE_DEPLOYMENT_BACKEND",
    "AOITALK_LLM_BACKEND",
    "AOITALK_DEPLOYMENT_BACKEND",
    "AOITALK_RUNTIME_BACKEND",
)

# ``AOITALK_LLM_MODE`` is the pre-contract provider/mode setting used by old
# Compose files.  It must be consulted only after an explicit backend: a
# deployment may legitimately set the legacy value to ``openai_compatible_local``
# while a newer overlay sets ``AOITALK_LLM_BACKEND=gemma-vllm``.
_LEGACY_BACKEND_ENV_NAMES: Tuple[str, ...] = ("AOITALK_LLM_MODE",)

_TRANSPORT_ENV_NAMES: Tuple[str, ...] = (
    "AOITALK_LLM_TRANSPORT",
    "AOITALK_DEPLOYMENT_TRANSPORT",
    "AOITALK_TRANSPORT",
)


def _config_get(config: Any, key: str, default: Any = None) -> Any:
    """Read both Config-style dotted keys and plain nested dictionaries."""

    if config is None:
        return default
    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            value = getter(key, _MISSING)
        except TypeError:
            try:
                value = getter(key)
            except Exception:
                value = _MISSING
        except Exception:
            value = _MISSING
        if value is not _MISSING and value is not None:
            return value

    if isinstance(config, dict):
        value: Any = config
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value
    return default


def _first_env(*names: str) -> str:
    for name in names:
        value = os.getenv(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _normalise_provider(value: Any) -> str:
    return str(value or "").strip().lower()


def _normalise_model(value: Any) -> str:
    return str(value or "").strip()


def _normalise_base_url(value: Any, *, default: str = "") -> str:
    raw = str(value or "").strip().rstrip("/")
    if not raw:
        return default.rstrip("/")
    if raw.endswith("/v1"):
        return raw
    return f"{raw}/v1"


def _non_sglang_base_url(value: Any) -> str:
    """Ignore stale SGLang endpoint settings on non-SGLang backends."""

    raw = str(value or "").strip()
    if not raw:
        return ""
    lowered = raw.lower()
    if "sglang" in lowered or ":30000" in lowered:
        return ""
    return raw


def _safe_base_url(value: str) -> str:
    """Return a credential-free URL suitable for diagnostics/UI metadata."""

    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
        if not parsed.scheme or not parsed.hostname:
            return raw.split("?", 1)[0].split("#", 1)[0]
        host = parsed.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        netloc = host
        if parsed.port:
            netloc = f"{netloc}:{parsed.port}"
        return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
    except Exception:
        # Never return query/fragment (which are the most likely place for a
        # bearer token) when an operator supplies a malformed URL.
        return raw.split("?", 1)[0].split("#", 1)[0]


def _parse_provider_list(value: Any) -> Tuple[str, ...]:
    if isinstance(value, str):
        values: Iterable[Any] = value.replace(";", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        values = value
    else:
        return ()
    result = []
    for item in values:
        provider = _normalise_provider(item)
        if provider and provider in KNOWN_PROVIDER_IDS and provider not in result:
            result.append(provider)
    return tuple(result)


def _runtime_backend(config: Any = None) -> Optional[str]:
    for name in _BACKEND_ENV_NAMES:
        value = os.getenv(name)
        if value is None or not str(value).strip():
            continue
        normalized = str(value).strip().lower()
        backend = _BACKEND_ALIASES.get(normalized)
        if backend:
            return backend
        raise DeploymentConfigurationError(
            f"Unsupported LLM deployment backend: {value!r}"
        )
    for name in _LEGACY_BACKEND_ENV_NAMES:
        value = os.getenv(name)
        if value is None or not str(value).strip():
            continue
        normalized = str(value).strip().lower()
        backend = _BACKEND_ALIASES.get(normalized)
        if backend:
            return backend
        # Old LLM_MODE values describe an AoiTalk provider rather than a
        # deployment backend (for example ``openai_compatible_local``).  Do
        # not reinterpret those as a new fixed deployment.
        continue
    # Runtime YAML is useful for native launches that do not have a Compose
    # overlay.  It is consulted only after explicit environment selectors.
    for key in (
        "enterprise_deployment.backend",
        "enterprise_deployment.active_backend",
        "deployment.backend",
    ):
        value = _config_get(config, key, "")
        if not str(value or "").strip():
            continue
        backend = _BACKEND_ALIASES.get(str(value).strip().lower())
        if backend:
            return backend
        raise DeploymentConfigurationError(
            f"Unsupported LLM deployment backend: {value!r}"
        )
    return None


def _transport(backend: str, base_url: str, config: Any = None) -> str:
    raw = (
        _first_env(*_TRANSPORT_ENV_NAMES)
        or str(_config_get(config, "enterprise_deployment.transport", "") or "")
    ).lower().replace("_", "-")
    aliases = {
        "httpredirect": "http-redirect",
        "redirect": "http-redirect",
        "tls": "https",
    }
    raw = aliases.get(raw, raw)
    if raw in {"http", "https", "http-redirect"}:
        return raw
    # External APIs are expected to be HTTPS; local backends are internal HTTP
    # services unless the deployment explicitly advertises an HTTPS endpoint.
    try:
        scheme = urlsplit(base_url).scheme.lower()
    except Exception:
        scheme = ""
    if scheme == "https":
        return "https"
    return "https" if backend == "external" else "http"


def _provider_model(config: Any, provider: str) -> str:
    for key in {
        "openai": ("openai.model", "openai_model"),
        "gemini": ("gemini.model", "gemini_model"),
        "openrouter": ("openrouter.model", "openrouter_model"),
        "deepseek": ("deepseek.model", "deepseek_model"),
        "deepinfra": ("deepinfra.model", "deepinfra_model"),
        "kimi": ("kimi.model", "kimi_model"),
        "openai_compatible_local": (
            "openai_compatible_local.model",
            "openai_compatible_local_model",
        ),
    }.get(provider, ()):
        value = _normalise_model(_config_get(config, key, ""))
        if value:
            return value
    return ""


def _provider_base_url(config: Any, provider: str) -> str:
    keys = {
        "openrouter": ("openrouter.base_url", "openrouter_base_url"),
        "deepseek": ("deepseek.base_url", "deepseek_base_url"),
        "deepinfra": ("deepinfra.base_url", "deepinfra_base_url"),
        "kimi": ("kimi.base_url", "kimi_base_url"),
        "openai_compatible_local": (
            "openai_compatible_local.base_url",
            "openai_compatible_local_base_url",
        ),
    }.get(provider, ())
    for key in keys:
        value = str(_config_get(config, key, "") or "").strip()
        if value:
            return (
                _normalise_base_url(value)
                if provider == "openai_compatible_local"
                else value.rstrip("/")
            )
    defaults = {
        "openrouter": "https://openrouter.ai/api/v1",
        "deepseek": "https://api.deepseek.com/v1",
        "deepinfra": "https://api.deepinfra.com/v1/openai",
        "kimi": "https://api.moonshot.ai/v1",
    }
    return defaults.get(provider, "")


def _deployment_model(config: Any, backend: str, provider: str) -> str:
    if backend == "gemma-vllm":
        return (
            _first_env(
                "AOITALK_GEMMA_MODEL",
                "AOITALK_GEMMA_VLLM_MODEL",
                "AOITALK_GEMMA_SERVED_MODEL",
                "AOITALK_EFFECTIVE_LLM_MODEL",
                "AOITALK_EXTERNAL_MODEL",
                "GEMMA_MODEL",
                "VLLM_MODEL",
                "OPENAI_COMPATIBLE_LOCAL_MODEL",
            )
            or _normalise_model(_config_get(config, "gemma_vllm.model", ""))
            or _normalise_model(_config_get(config, "vllm.model", ""))
            or _provider_model(config, "openai_compatible_local")
            or "google/gemma-4-E4B-it"
        )
    if backend == "deepseek-llamacpp":
        return (
            _first_env(
                "AOITALK_DEEPSEEK_MODEL",
                "AOITALK_DEEPSEEK_LLAMACPP_MODEL",
                "DEEPSEEK_LLAMACPP_MODEL",
            )
            or _normalise_model(_config_get(config, "deepseek_llamacpp.model", ""))
            or _provider_model(config, "openai_compatible_local")
            or "deepseek-ai/DeepSeek-V4-Flash"
        )
    if backend == "sglang-cuda":
        return (
            _first_env("SGLANG_MODEL", "AOITALK_SGLANG_MODEL")
            or _normalise_model(_config_get(config, "sglang.model", ""))
            or _normalise_model(_config_get(config, "llm_model", ""))
            or "default"
        )
    # External: honour an explicitly selected provider/model first, then the
    # deployment's external defaults.
    persisted_provider = _normalise_provider(_config_get(config, "llm_provider", ""))
    persisted_model = _normalise_model(_config_get(config, "llm_model", ""))
    if persisted_provider == provider and persisted_model:
        return persisted_model
    return (
        _first_env("AOITALK_EXTERNAL_MODEL", "AOITALK_LLM_MODEL")
        or _normalise_model(_config_get(config, "external.model", ""))
        or _normalise_model(_config_get(config, "llm_external_model", ""))
        or _provider_model(config, provider)
        or _normalise_model(_config_get(config, "llm_model", ""))
        or {
            "openrouter": "openai/gpt-4o-mini",
            "openai": "gpt-4o",
            "gemini": "gemini-2.5-flash",
            "deepseek": "deepseek-v4-flash",
            "deepinfra": "deepseek-ai/DeepSeek-V4-Flash",
            "kimi": "kimi-k3",
        }.get(provider, "local-model")
    )


def _deployment_base_url(config: Any, backend: str, provider: str) -> str:
    if backend == "gemma-vllm":
        generic_local_url = _non_sglang_base_url(
            _first_env("OPENAI_COMPATIBLE_LOCAL_BASE_URL")
            or _config_get(config, "openai_compatible_local.base_url", "")
            or _config_get(config, "openai_compatible_local_base_url", "")
        )
        return _normalise_base_url(
            _non_sglang_base_url(
                _first_env(
                    "AOITALK_GEMMA_VLLM_BASE_URL",
                    "AOITALK_GEMMA_BASE_URL",
                    "AOITALK_EFFECTIVE_LLM_BASE_URL",
                    "AOITALK_EXTERNAL_BASE_URL",
                    "GEMMA_VLLM_BASE_URL",
                    "VLLM_BASE_URL",
                )
            )
            or _non_sglang_base_url(_config_get(config, "gemma_vllm.base_url", ""))
            or _non_sglang_base_url(_config_get(config, "vllm.base_url", ""))
            or generic_local_url
            or "http://gemma-vllm:8000/v1"
        )
    if backend == "deepseek-llamacpp":
        generic_local_url = _non_sglang_base_url(
            _first_env("OPENAI_COMPATIBLE_LOCAL_BASE_URL")
            or _config_get(config, "openai_compatible_local.base_url", "")
            or _config_get(config, "openai_compatible_local_base_url", "")
        )
        return _normalise_base_url(
            _non_sglang_base_url(
                _first_env(
                    "AOITALK_DEEPSEEK_LLAMACPP_BASE_URL",
                    "DEEPSEEK_LLAMACPP_BASE_URL",
                )
            )
            or _non_sglang_base_url(
                _config_get(config, "deepseek_llamacpp.base_url", "")
            )
            or generic_local_url
            or "http://deepseek-llamacpp:8080/v1"
        )
    if backend == "sglang-cuda":
        return _normalise_base_url(
            _first_env("SGLANG_BASE_URL", "AOITALK_SGLANG_BASE_URL")
            or _config_get(config, "sglang_base_url", "")
            or _config_get(config, "sglang.base_url", "")
            or "http://sglang:30000/v1"
        )
    persisted_provider = _normalise_provider(_config_get(config, "llm_provider", ""))
    persisted_base = _provider_base_url(config, provider) if persisted_provider == provider else ""
    raw = (
        persisted_base
        or (
        _first_env(
            "AOITALK_EXTERNAL_BASE_URL",
            "AOITALK_EFFECTIVE_LLM_BASE_URL",
            "AOITALK_LLM_BASE_URL",
        )
        or _config_get(config, "external.base_url", "")
        or _provider_base_url(config, provider)
        )
    )
    if provider == "openai_compatible_local":
        return _normalise_base_url(raw)
    return str(raw or "").strip().rstrip("/")


def _external_provider(config: Any, persisted_provider: str) -> str:
    explicitly_effective = _normalise_provider(
        _first_env("AOITALK_EFFECTIVE_LLM_PROVIDER")
    )
    if explicitly_effective in EXTERNAL_PROVIDER_IDS:
        return explicitly_effective
    if persisted_provider in EXTERNAL_PROVIDER_IDS:
        return persisted_provider
    requested = _normalise_provider(
        _first_env(
            "AOITALK_EXTERNAL_PROVIDER",
            "AOITALK_EFFECTIVE_LLM_PROVIDER",
            "AOITALK_LLM_PROVIDER",
        )
        or _config_get(config, "external.provider", "")
        or _config_get(config, "llm_external_provider", "")
    )
    if requested in EXTERNAL_PROVIDER_IDS:
        return requested
    return "openrouter"


@dataclass(frozen=True)
class LLMDeployment:
    """An immutable persisted/effective deployment projection."""

    backend: str
    transport: str
    fixed: bool
    effective_provider: str
    effective_model: str
    effective_base_url: str
    server_profile: str = "auto"
    tool_capability: bool = True
    allowed_provider_ids: Tuple[str, ...] = ()
    unavailable_provider_ids: Tuple[str, ...] = ()
    reason: str = ""
    persisted_provider: str = ""
    persisted_model: str = ""
    persisted_base_url: str = ""
    ready: bool = True

    @property
    def effective(self) -> Dict[str, Any]:
        return {
            "provider": self.effective_provider,
            "model": self.effective_model,
            "base_url": _safe_base_url(self.effective_base_url),
            "server_profile": self.server_profile,
            "tool_capability": self.tool_capability,
        }

    @property
    def persisted(self) -> Dict[str, Any]:
        return {
            "provider": self.persisted_provider,
            "model": self.persisted_model,
            "base_url": _safe_base_url(self.persisted_base_url),
        }

    def metadata(self) -> Dict[str, Any]:
        """Return the stable, secret-free API/UI contract."""

        return {
            "backend": self.backend,
            "transport": self.transport,
            "fixed": self.fixed,
            "ready": self.ready,
            "effective_provider": self.effective_provider,
            "effective_model": self.effective_model,
            "allowed_provider_ids": list(self.allowed_provider_ids),
            "unavailable_provider_ids": list(self.unavailable_provider_ids),
            "reason": self.reason,
            # Nested projections make the persisted/effective distinction
            # explicit for diagnostics while keeping the legacy flat fields.
            "persisted": self.persisted,
            "effective": self.effective,
        }

    def provider_available(self, provider: str) -> Tuple[bool, Optional[str]]:
        provider_id = _normalise_provider(provider)
        if provider_id in self.allowed_provider_ids:
            return True, None
        if provider_id in self.unavailable_provider_ids or provider_id:
            return False, (
                f"Provider '{provider_id or '(empty)'}' is unavailable for "
                f"deployment backend '{self.backend}'"
            )
        return False, "Provider is not configured"

    def effective_overrides(self, config: Any = None) -> Dict[str, Any]:
        """Build non-persistent Config overrides for the active backend."""

        overrides: Dict[str, Any] = {}
        stale_provider = self.persisted_provider not in self.allowed_provider_ids
        explicit_effective_provider = _normalise_provider(
            _first_env("AOITALK_EFFECTIVE_LLM_PROVIDER")
        )
        provider_changed = bool(
            explicit_effective_provider
            and explicit_effective_provider != self.persisted_provider
        )
        if self.fixed or stale_provider or provider_changed:
            overrides["llm_provider"] = self.effective_provider
            overrides["llm_model"] = self.effective_model

        if self.fixed and self.effective_provider == "openai_compatible_local":
            current_local = _config_get(config, "openai_compatible_local", {})
            local = dict(current_local) if isinstance(current_local, dict) else {}
            local.update(
                {
                    "model": self.effective_model,
                    "base_url": self.effective_base_url,
                    "server_profile": self.server_profile,
                    "enable_tools": self.tool_capability,
                }
            )
            overrides["openai_compatible_local"] = local
            overrides["runtime.target_model"] = self.effective_model
            overrides["runtime.target_base_url"] = self.effective_base_url
        elif self.fixed and self.effective_provider == "sglang":
            overrides["sglang.model"] = self.effective_model
            overrides["sglang_base_url"] = self.effective_base_url
            overrides["runtime.target_model"] = self.effective_model
            overrides["runtime.target_base_url"] = self.effective_base_url

        return overrides


def resolve_llm_deployment(config: Any = None) -> Optional[LLMDeployment]:
    """Resolve an explicit runtime backend, or ``None`` for personal mode."""

    backend = _runtime_backend(config)
    if not backend:
        return None

    persisted_provider = _normalise_provider(_config_get(config, "llm_provider", "openai"))
    persisted_model = _normalise_model(_config_get(config, "llm_model", ""))
    persisted_base_url = _normalise_base_url(
        _config_get(config, "llm_base_url", "")
        or _provider_base_url(config, persisted_provider)
    )

    if backend == "external":
        allowed = _parse_provider_list(
            _first_env("AOITALK_ALLOWED_PROVIDER_IDS", "AOITALK_LLM_ALLOWED_PROVIDERS")
            or _config_get(config, "external.allowed_provider_ids", "")
        ) or EXTERNAL_PROVIDER_IDS
        effective_provider = _external_provider(config, persisted_provider)
        if effective_provider not in allowed:
            effective_provider = allowed[0] if allowed else "openrouter"
        effective_model = _deployment_model(config, backend, effective_provider)
        effective_base_url = _deployment_base_url(config, backend, effective_provider)
        fixed = False
        profile = "auto"
        tools = True
        if persisted_provider not in allowed:
            reason = (
                f"Persisted provider '{persisted_provider or '(empty)'}' is not "
                f"available for external deployment; using '{effective_provider}'."
            )
        else:
            reason = "External provider selection is available for this deployment."
    else:
        allowed = ("openai_compatible_local",) if backend in {
            "gemma-vllm",
            "deepseek-llamacpp",
        } else ("sglang",)
        effective_provider = allowed[0]
        effective_model = _deployment_model(config, backend, effective_provider)
        effective_base_url = _deployment_base_url(config, backend, effective_provider)
        fixed = True
        profile = {
            "gemma-vllm": "vllm",
            "deepseek-llamacpp": "llama.cpp",
            "sglang-cuda": "sglang",
        }[backend]
        tools = str(
            _first_env(
                "AOITALK_GEMMA_VLLM_ENABLE_TOOLS",
                "OPENAI_COMPATIBLE_LOCAL_TOOLS",
                "AOITALK_LLM_ENABLE_TOOLS",
            )
            or _config_get(config, "openai_compatible_local.enable_tools", "")
            or _config_get(config, "openai_compatible_local.tools", "")
            or "true"
        ).strip().lower() in {"1", "true", "yes", "on"}
        reason = f"Deployment backend '{backend}' is fixed to {effective_provider}."
        if persisted_provider != effective_provider or persisted_model != effective_model:
            reason += " Persisted provider/model are retained for diagnostics only."

    transport = _transport(backend, effective_base_url, config)
    unavailable = tuple(provider for provider in KNOWN_PROVIDER_IDS if provider not in allowed)
    endpoint_ready = bool(effective_base_url) or (
        backend == "external"
        and effective_provider in _BASE_URL_OPTIONAL_PROVIDER_IDS
    )
    ready = bool(effective_provider and effective_model and endpoint_ready)
    declared_provider = _normalise_provider(
        _first_env("AOITALK_EFFECTIVE_LLM_PROVIDER")
    )
    declared_model = _normalise_model(_first_env("AOITALK_EFFECTIVE_LLM_MODEL"))
    if declared_provider and declared_provider != effective_provider:
        ready = False
        reason += " Effective provider declaration does not match the backend."
    if declared_model and declared_model != effective_model:
        ready = False
        reason += " Effective model declaration does not match the backend."
    return LLMDeployment(
        backend=backend,
        transport=transport,
        fixed=fixed,
        effective_provider=effective_provider,
        effective_model=effective_model,
        effective_base_url=effective_base_url,
        server_profile=profile,
        tool_capability=tools,
        allowed_provider_ids=tuple(allowed),
        unavailable_provider_ids=unavailable,
        reason=reason,
        persisted_provider=persisted_provider,
        persisted_model=persisted_model,
        persisted_base_url=persisted_base_url,
        ready=ready,
    )


# Short aliases are useful for callers that use the older ``deployment``
# terminology and keep the contract easy to discover in tests/integrations.
resolve_deployment = resolve_llm_deployment
resolve_effective_deployment = resolve_llm_deployment


def deployment_metadata(config: Any = None) -> Optional[Dict[str, Any]]:
    deployment = resolve_llm_deployment(config)
    return deployment.metadata() if deployment else None


get_deployment_metadata = deployment_metadata


def preflight_deployment(
    config: Any = None,
    *,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
) -> Optional[LLMDeployment]:
    """Reject a requested engine before persistence or network traffic."""

    deployment = resolve_llm_deployment(config)
    if deployment is None:
        return None
    requested_provider = _normalise_provider(provider) if provider is not None else ""
    requested_model = _normalise_model(model) if model is not None else ""
    if requested_provider:
        allowed, reason = deployment.provider_available(requested_provider)
        if not allowed:
            raise DeploymentMismatchError(reason or "Provider is unavailable")
        if deployment.fixed and requested_provider != deployment.effective_provider:
            raise DeploymentMismatchError(
                f"Deployment backend '{deployment.backend}' is fixed to "
                f"{deployment.effective_provider}; requested {requested_provider}"
            )
    if deployment.fixed and requested_model and requested_model != deployment.effective_model:
        raise DeploymentMismatchError(
            f"Deployment backend '{deployment.backend}' serves only model "
            f"{deployment.effective_model}; requested {requested_model}"
        )
    if deployment.fixed and base_url:
        expected = _safe_base_url(deployment.effective_base_url)
        requested = _safe_base_url(_normalise_base_url(base_url))
        if expected and requested and expected != requested:
            raise DeploymentMismatchError(
                f"Deployment backend '{deployment.backend}' uses a fixed endpoint"
            )
    if not deployment.ready:
        raise DeploymentMismatchError(
            f"Deployment backend '{deployment.backend}' is not ready"
        )
    return deployment


def effective_config_overrides(config: Any = None) -> Dict[str, Any]:
    deployment = resolve_llm_deployment(config)
    return deployment.effective_overrides(config) if deployment else {}


__all__ = [
    "KNOWN_PROVIDER_IDS",
    "EXTERNAL_PROVIDER_IDS",
    "DeploymentConfigurationError",
    "DeploymentMismatchError",
    "LLMDeployment",
    "deployment_metadata",
    "effective_config_overrides",
    "preflight_deployment",
    "resolve_deployment",
    "resolve_effective_deployment",
    "resolve_llm_deployment",
    "get_deployment_metadata",
]
