"""Request-scoped routing for Dreaming background LLM calls."""

from __future__ import annotations

from typing import Any, Callable


FREE_ONLY_MEMORY_PROVIDERS = {"openai", "openrouter"}
MEMORY_OUTPUT_TOKEN_LIMIT = 1200


def _config_value(config: Any, key: str, default: Any = None) -> Any:
    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            value = getter(key, default)
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


def requires_free_only_memory_route(config: Any) -> bool:
    """Whether Dreaming must use a quota-reserved complimentary route."""

    provider = str(_config_value(config, "llm_provider", "") or "").strip().lower()
    model = str(_config_value(config, "llm_model", "") or "").strip().lower()
    return provider in FREE_ONLY_MEMORY_PROVIDERS or (
        provider == "routing-profile" and model == "free-team"
    )


def dreaming_memory_client_config(config: Any) -> Any:
    """Return an in-memory Free Team overlay for paid-capable API providers.

    Free Team applies the daily request/token reservations and fails closed
    when no complimentary candidate has sufficient remaining quota. This
    overlay is deliberately local to Dreaming's background client.
    """

    if not requires_free_only_memory_route(config):
        return config

    from ..llm.manager import TargetConfig

    provider = str(_config_value(config, "llm_provider", "") or "").strip().lower()
    overrides: dict[str, Any] = {
        "runtime.free_team_strict_complimentary_only": True,
        "runtime.target_max_output_tokens": MEMORY_OUTPUT_TOKEN_LIMIT,
    }
    if provider in FREE_ONLY_MEMORY_PROVIDERS:
        overrides.update(
            {
                "llm_provider": "routing-profile",
                "llm_model": "free-team",
            }
        )
    return TargetConfig(config, overrides)


def create_dreaming_memory_client(
    config: Any,
    *,
    client_factory: Callable[[Any], Any] | None = None,
) -> Any:
    """Create Dreaming's client without sending a provider request."""

    if client_factory is None:
        from ..llm.manager import create_llm_client

        client_factory = create_llm_client
    return client_factory(dreaming_memory_client_config(config))


def route_scoped_memory_client(
    client: Any,
    *,
    client_factory: Callable[[Any], Any] | None = None,
) -> Any:
    """Replace a paid-capable foreground API client for a memory job only."""

    config = getattr(client, "config", None)
    if config is None or not requires_free_only_memory_route(config):
        return client
    return create_dreaming_memory_client(config, client_factory=client_factory)
