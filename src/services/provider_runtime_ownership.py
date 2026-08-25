"""Provider/runtime ownership and platform availability metadata.

This module centralises process-ownership semantics so Install/Delete/Stop
decisions do not grow provider-ID ``if`` chains across catalog, settings and
runtime code paths.
"""

from __future__ import annotations

import os
import sys
from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

from src.llm.deployment_resolver import resolve_llm_deployment
from src.llm.openai_compatible_local_profiles import (
    llama_cpp_model_profile,
    llama_cpp_runtime_declared,
)
from src.llm.sglang_url import resolve_sglang_base_url

_IS_WINDOWS = sys.platform == "win32"
_MANAGED_LOCAL_PROVIDERS = frozenset({"openai_compatible_local", "sglang"})
_DAEMON_PROVIDERS = frozenset({"ollama"})
_EXTERNAL_ONLY_PROVIDERS = frozenset(
    {
        "openai",
        "gemini",
        "openrouter",
        "deepseek",
        "deepinfra",
        "kimi",
        "codex-cli",
        "claude-cli",
        "antigravity-cli",
        "grok-cli",
    }
)


def _config_get(config: Any, key: str, default: Any = None) -> Any:
    if isinstance(config, dict):
        value: Any = config
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value
    getter = getattr(config, "get", None)
    return getter(key, default) if callable(getter) else default


def _is_loopback_host(host: str) -> bool:
    normalized = str(host or "").strip().lower().strip("[]")
    if normalized in {"localhost", "127.0.0.1", "::1"}:
        return True
    return False


def _endpoint_host(base_url: str) -> str:
    try:
        return str(urlparse(str(base_url or "").strip()).hostname or "").strip().lower()
    except Exception:
        return ""


@dataclass(frozen=True)
class ProviderRuntimeOwnership:
    """Stable ownership/capability contract for one provider."""

    provider_id: str
    process_owner: str
    managed_runtime: bool
    supports_install: bool
    supports_delete: bool
    supports_stop: bool
    platform_candidate: bool
    platform_reason: str = ""
    server_online: Optional[bool] = None
    server_state: str = "unknown"
    endpoint_classification: str = "external"

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["server_online"] = self.server_online
        return payload


def _openai_compatible_local_managed(config: Any, *, model: str | None = None) -> bool:
    selected_model = str(
        model
        or _config_get(config, "openai_compatible_local.model", "")
        or _config_get(config, "llm_model", "")
        or ""
    ).strip()
    if selected_model.casefold() == "local-model":
        return False
    auto_start = os.getenv(
        "LLAMA_CPP_AUTO_START",
        _config_get(config, "openai_compatible_local.llama_cpp.auto_start", True),
    )
    if auto_start is None:
        auto_start = True
    if isinstance(auto_start, str):
        auto_start = auto_start.strip().lower() in {"1", "true", "yes", "on"}
    if not bool(auto_start):
        # A known profile describes request capabilities, not ownership of a
        # manually started endpoint.  Only AoiTalk's auto-start path may
        # expose process install/delete/stop controls.
        return False
    if llama_cpp_model_profile(selected_model):
        return True
    raw_runtime = _config_get(config, "openai_compatible_local.llama_cpp", {})
    runtime_declared = llama_cpp_runtime_declared(raw_runtime)
    runtime_alias = str(
        _config_get(config, "openai_compatible_local.llama_cpp.model_alias", "")
        or _config_get(config, "openai_compatible_local.model", "")
        or ""
    ).strip()
    if bool(
        runtime_declared
        and runtime_alias
        and selected_model.casefold() == runtime_alias.casefold()
    ):
        return True

    # Personal installations historically treated the default loopback
    # llama-server endpoint as AoiTalk-managed even before a nested runtime
    # marker (model_alias/model_path) was persisted.  Keep that compatibility
    # while remaining conservative for operator-owned endpoints: an explicit
    # ``auto_start=false`` returned above, and a non-loopback configured URL
    # is external unless a profile/alias explicitly opts into management.
    configured_base_url = str(
        _config_get(config, "openai_compatible_local.base_url", "")
        or _config_get(config, "llm_base_url", "")
        or ""
    ).strip()
    if not configured_base_url:
        return True
    return _is_loopback_host(_endpoint_host(configured_base_url))


def _sglang_managed(config: Any) -> bool:
    if _config_get(config, "runtime.target_base_url"):
        return False
    if not bool(_config_get(config, "sglang.auto_start", True)):
        return False
    return not _IS_WINDOWS


def _sglang_managed_candidate(config: Any) -> bool:
    """Whether SGLang would be presented as an AoiTalk-managed local runtime."""

    if _config_get(config, "runtime.target_base_url"):
        return False
    return bool(_config_get(config, "sglang.auto_start", True))


def provider_runtime_ownership(
    provider_id: str,
    config: Any = None,
    *,
    model: str | None = None,
    ollama_status: Dict[str, Any] | None = None,
) -> ProviderRuntimeOwnership:
    """Resolve ownership/capability metadata for one provider."""

    provider = str(provider_id or "").strip().lower()
    deployment = resolve_llm_deployment(config)
    if deployment is not None:
        allowed, reason = deployment.provider_available(provider)
        if not allowed:
            return ProviderRuntimeOwnership(
                provider_id=provider,
                process_owner="deployment",
                managed_runtime=False,
                supports_install=False,
                supports_delete=False,
                supports_stop=False,
                platform_candidate=False,
                platform_reason=reason or "Provider unavailable for deployment",
                server_state="blocked",
                endpoint_classification="deployment",
            )

    if provider == "ollama":
        status = ollama_status if isinstance(ollama_status, dict) else {}
        online = status.get("online")
        if online is None and "reachable" in status:
            online = bool(status.get("reachable"))
        return ProviderRuntimeOwnership(
            provider_id=provider,
            process_owner="daemon",
            managed_runtime=False,
            supports_install=True,
            supports_delete=True,
            supports_stop=False,
            platform_candidate=True,
            server_online=online,
            server_state="online" if online else "offline",
            endpoint_classification="local_daemon",
        )

    if provider == "sglang":
        base_url = resolve_sglang_base_url(config)
        managed = _sglang_managed(config)
        managed_candidate = _sglang_managed_candidate(config)
        host = _endpoint_host(base_url)
        endpoint_class = "trusted_local" if _is_loopback_host(host) else "remote"
        platform_candidate = True
        platform_reason = ""
        if _IS_WINDOWS and managed_candidate:
            platform_candidate = False
            platform_reason = (
                "AoiTalk-managed SGLang is not offered on Windows; "
                "use an external OpenAI-compatible endpoint instead."
            )
        return ProviderRuntimeOwnership(
            provider_id=provider,
            process_owner="managed" if managed else "external",
            managed_runtime=managed,
            supports_install=False,
            supports_delete=False,
            supports_stop=managed,
            platform_candidate=platform_candidate,
            platform_reason=platform_reason,
            endpoint_classification=endpoint_class,
            server_state="external" if not managed else "managed",
        )

    if provider == "openai_compatible_local":
        managed = _openai_compatible_local_managed(config, model=model)
        base_url = str(
            _config_get(config, "openai_compatible_local.base_url", "")
            or _config_get(config, "llm_base_url", "")
            or ""
        ).strip()
        host = _endpoint_host(base_url)
        endpoint_class = "trusted_local" if _is_loopback_host(host) else "remote"
        return ProviderRuntimeOwnership(
            provider_id=provider,
            process_owner="managed" if managed else "external",
            managed_runtime=managed,
            supports_install=managed,
            supports_delete=managed,
            supports_stop=managed,
            platform_candidate=True,
            endpoint_classification=endpoint_class,
            server_state="managed" if managed else "external",
        )

    if provider in _EXTERNAL_ONLY_PROVIDERS:
        return ProviderRuntimeOwnership(
            provider_id=provider,
            process_owner="external",
            managed_runtime=False,
            supports_install=False,
            supports_delete=False,
            supports_stop=False,
            platform_candidate=True,
            endpoint_classification="external",
            server_state="external",
        )

    return ProviderRuntimeOwnership(
        provider_id=provider,
        process_owner="external",
        managed_runtime=False,
        supports_install=False,
        supports_delete=False,
        supports_stop=False,
        platform_candidate=True,
        endpoint_classification="external",
        server_state="unknown",
    )


def provider_platform_available(
    provider_id: str,
    config: Any = None,
    *,
    model: str | None = None,
    ollama_status: Dict[str, Any] | None = None,
) -> Tuple[bool, str]:
    """Return whether a provider should appear as a normal platform candidate."""

    ownership = provider_runtime_ownership(
        provider_id,
        config,
        model=model,
        ollama_status=ollama_status,
    )
    if ownership.platform_candidate:
        return True, ""
    return False, ownership.platform_reason


def enrich_provider_payload(
    provider_payload: Dict[str, Any],
    config: Any = None,
    *,
    ollama_status: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Attach ownership metadata to one catalog provider payload."""

    provider_id = str(provider_payload.get("id") or "").strip().lower()
    model = str(provider_payload.get("configured_model") or "").strip() or None
    ownership = provider_runtime_ownership(
        provider_id,
        config,
        model=model,
        ollama_status=ollama_status,
    )
    enriched = dict(provider_payload)
    enriched["ownership"] = ownership.to_dict()
    if not ownership.platform_candidate and provider_payload.get("available", True):
        enriched["available"] = False
        enriched["unavailable"] = True
        enriched["availability_reason"] = (
            ownership.platform_reason or provider_payload.get("availability_reason") or ""
        )
    if provider_id == "ollama" and ownership.server_online is False:
        enriched["server_online"] = False
        enriched.setdefault(
            "availability_reason",
            "Ollama daemon is offline; provider remains available once the daemon starts.",
        )
    return enriched


__all__ = [
    "ProviderRuntimeOwnership",
    "enrich_provider_payload",
    "provider_platform_available",
    "provider_runtime_ownership",
]
