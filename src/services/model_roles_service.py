"""Configuration facade for the P1/P2 role-to-model table.

Primary routes remain owned by their existing settings.  This module stores
Chat fallbacks, P2 route-list role overrides, and image-generation selection.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from .agent_model_routing import ROUTABLE_PROVIDERS


class ModelRolesVersionConflict(RuntimeError):
    """The persisted model_roles version changed before the update."""


class ModelRolesPersistenceError(RuntimeError):
    """The model_roles update could not be persisted."""


_ROUTE_LIST_ROLES = ("summarize", "code", "review", "research")


def _config_get(config: Any, key: str, default: Any = None) -> Any:
    if isinstance(config, Mapping):
        value: Any = config
        for part in key.split("."):
            if not isinstance(value, Mapping) or part not in value:
                return default
            value = value[part]
        return value
    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            value = getter(key, default)
        except TypeError:
            value = getter(key)
        return default if value is None else value
    return default


def _catalog_main_effort(config: Any, provider: str, model: str) -> str:
    """Return the configured Main effort when the selected model accepts it."""

    provider_id = str(provider or "").strip().lower()
    model_id = str(model or "").strip()
    if not provider_id or not model_id:
        return ""
    if provider_id == "openai_compatible_local":
        from ..llm.openai_compatible_local_profiles import managed_local_runtime_for_model
        from ..llm.provider_mode_adapters import resolve_strata_response_mode

        if managed_local_runtime_for_model(config, model_id) == "strata":
            return resolve_strata_response_mode(config)
    effort_keys = {
        "openai": "openai.reasoning_effort",
        "deepseek": "deepseek.reasoning_effort",
        "deepinfra": "deepinfra.reasoning_effort",
        "kimi": "kimi.reasoning_effort",
        "codex-cli": "codex_cli.reasoning_effort",
        "claude-cli": "claude_cli.reasoning_effort",
        "openai_compatible_local": "openai_compatible_local.llama_cpp.reasoning_effort",
    }
    effort = str(_config_get(config, effort_keys.get(provider_id, ""), "") or "").strip()
    if not effort and provider_id == "deepseek":
        effort = "high"
    if not effort and provider_id == "openai":
        model_leaf = model_id.lower().rsplit("/", 1)[-1]
        if model_leaf.startswith("gpt-5.6-luna"):
            effort = "max"
    if not effort and provider_id == "openai_compatible_local":
        try:
            from .llm_model_catalog import reasoning_effort_default_for_model

            effort = str(
                reasoning_effort_default_for_model(provider_id, model_id) or ""
            ).strip()
        except Exception:
            effort = ""
    if not effort:
        return ""
    try:
        from .llm_model_catalog import reasoning_effort_options_for_model

        options = {
            str(item).strip()
            for item in reasoning_effort_options_for_model(
                provider_id, model_id, config=config
            )
            if str(item).strip()
        }
    except Exception:
        options = set()
    if effort in options or (not options and provider_id == "openai"):
        return effort
    return ""


def _main_route(config: Any) -> dict[str, Any]:
    """Resolve the configured Main provider/model without Team state."""

    provider = str(_config_get(config, "llm_provider", "openai") or "openai").strip().lower()
    model = str(_config_get(config, "llm_model", "") or "").strip()
    if not model:
        provider_model_keys = {
            "openai": ("openai.model",),
            "gemini": ("gemini.model",),
            "openrouter": ("openrouter.model",),
            "deepseek": ("deepseek.model",),
            "deepinfra": ("deepinfra.model",),
            "kimi": ("kimi.model",),
            "ollama": ("ollama.model",),
            "sglang": ("sglang.model",),
            "openai_compatible_local": ("openai_compatible_local.model",),
            "codex-cli": ("codex_cli.model",),
            "claude-cli": ("claude_cli.model",),
            "antigravity-cli": ("antigravity_cli.model",),
            "grok-cli": ("grok_cli.model",),
        }
        for key in provider_model_keys.get(provider, ()):
            model = str(_config_get(config, key, "") or "").strip()
            if model:
                break
    from ..llm.deployment_resolver import canonical_model_for_provider, is_retired_model

    if is_retired_model(model) or not model:
        model = canonical_model_for_provider(config, provider)
    route: dict[str, Any] = {"provider": provider, "model": model}
    effort = _catalog_main_effort(config, provider, model)
    if effort:
        route["effort"] = effort
        route["reasoning_effort"] = effort
    return route


def _apply_session_main_route_override(route: dict[str, Any]) -> dict[str, Any]:
    from .session_llm_runtime_context import session_main_route_override

    override = session_main_route_override()
    if not override:
        return route
    result = dict(route)
    provider = str(override.get("provider") or "").strip().lower()
    model = str(override.get("model") or "").strip()
    effort = str(override.get("effort") or "").strip()
    pool_id = str(override.get("pool_id") or "").strip()
    if provider:
        result["provider"] = provider
    if model:
        result["model"] = model
    if effort:
        result["effort"] = effort
        result["reasoning_effort"] = effort
    elif provider and model:
        result.pop("effort", None)
        result.pop("reasoning_effort", None)
    if pool_id:
        result["pool_id"] = pool_id
    elif provider or model:
        result.pop("pool_id", None)
    return result


def _catalogize_main_route_effort(route: dict[str, Any], config: Any) -> dict[str, Any]:
    """Keep only an effort accepted by the resolved Main model catalog."""

    result = dict(route)
    provider = str(result.get("provider") or "").strip().lower()
    model = str(result.get("model") or "").strip()
    effort = str(result.get("effort") or result.get("reasoning_effort") or "").strip()
    if provider == "deepseek" and model and not effort:
        effort = "high"
    if provider == "openai_compatible_local" and model and not effort:
        from .llm_model_catalog import reasoning_effort_default_for_model

        effort = str(reasoning_effort_default_for_model(provider, model) or "").strip()
    if not provider or not model or not effort:
        result.pop("effort", None)
        result.pop("reasoning_effort", None)
        return result
    from .llm_model_catalog import reasoning_effort_options_for_model

    options = {
        str(item).strip()
        for item in reasoning_effort_options_for_model(provider, model, config=config)
        if str(item).strip()
    }
    if (options and effort not in options) or (not options and provider != "openai"):
        result.pop("effort", None)
        result.pop("reasoning_effort", None)
        return result
    result["effort"] = effort
    result["reasoning_effort"] = effort
    return result


def resolve_execution_main_route(config: Any) -> dict[str, Any]:
    """Resolve the live Main route from deployment and model_roles settings."""

    from ..llm.deployment_resolver import resolve_llm_deployment

    deployment = resolve_llm_deployment(config)
    if deployment is not None and deployment.fixed:
        return _catalogize_main_route_effort(
            {"provider": deployment.effective_provider, "model": deployment.effective_model},
            config,
        )
    return _catalogize_main_route_effort(
        _apply_session_main_route_override(_main_route(config)), config
    )


def _free_team_profile(config: Any) -> dict[str, Any]:
    if config is None:
        from ..config_defaults import load_default_config

        config = load_default_config()
    from .free_team_service import free_team_profile

    return free_team_profile(config)


def _database_candidate_billing_modes(provider: str, model: str) -> set[str]:
    """Read billing metadata for matching Free Team candidates only.

    The query deliberately selects no credential or candidate secrets. The
    DB tables are authoritative; missing/unavailable data is treated as an
    unknown route by the caller and therefore classified as paid.
    """

    try:
        from sqlalchemy import select

        from ..memory.database import get_database_manager
        from ..memory.models.free_team import (
            FreeTeamCandidateModel,
            FreeTeamCredentialProfile,
        )

        db_manager = get_database_manager()
        if not db_manager.is_initialized():
            return set()
        statement = (
            select(FreeTeamCredentialProfile.billing_mode)
            .join(
                FreeTeamCandidateModel,
                FreeTeamCandidateModel.credential_profile_id
                == FreeTeamCredentialProfile.id,
            )
            .where(
                FreeTeamCandidateModel.provider == provider,
                FreeTeamCandidateModel.model == model,
            )
        )
        with db_manager.get_sync_session() as session:
            values = session.execute(statement).scalars().all()
        return {
            str(value or "").strip().lower()
            for value in values
            if str(value or "").strip()
        }
    except Exception:  # noqa: BLE001 - unknown billing must remain paid
        return set()


def _normalize_route(
    raw: Any,
    *,
    config: Any,
    image_generation: bool = False,
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError("route must be an object")
    kind = raw.get("kind")
    if kind == "pool":
        if image_generation:
            raise ValueError("image_generation.primary must be a Gemini LLM route")
        if set(raw) - {"kind", "pool_id"}:
            raise ValueError("pool route contains unsupported fields")
        pool_id = raw.get("pool_id")
        if not isinstance(pool_id, str) or not pool_id.strip():
            raise ValueError("pool_id must be a non-empty string")
        pool_id = pool_id.strip()
        pools = _free_team_profile(config).get("pools", {})
        if not isinstance(pools, Mapping) or pool_id not in pools:
            raise ValueError(f"unknown free-team pool: {pool_id}")
        return {"kind": "pool", "pool_id": pool_id}

    if kind != "llm":
        raise ValueError("route kind must be llm or pool")
    if set(raw) - {"kind", "provider", "model", "effort"}:
        raise ValueError("llm route contains unsupported fields")
    provider = raw.get("provider")
    model = raw.get("model")
    if not isinstance(provider, str) or not provider.strip():
        raise ValueError("provider must be a non-empty string")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("model must be a non-empty string")
    provider = provider.strip().lower()
    model = model.strip()
    if provider not in ROUTABLE_PROVIDERS:
        raise ValueError(f"unsupported provider: {provider}")
    if provider == "muse-remote" and model != "muse-chat":
        raise ValueError("muse-remote routes require model muse-chat")
    if provider == "routing-profile" and model != "free-team":
        raise ValueError("routing-profile routes require model free-team")
    if image_generation and provider != "gemini":
        raise ValueError("image_generation.primary provider must be gemini")
    effort = raw.get("effort")
    if effort is not None and not isinstance(effort, str):
        raise ValueError("effort must be a string or null")
    normalized_effort = effort.strip() if isinstance(effort, str) else ""
    return {
        "kind": "llm",
        "provider": provider,
        "model": model,
        "effort": normalized_effort or None,
    }


def normalize_model_roles(raw: Any, *, config: Any = None) -> dict[str, Any]:
    """Validate and return the canonical model_roles storage shape."""

    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ValueError("model_roles must be an object")
    if set(raw) - {"version", "roles"}:
        raise ValueError("model_roles contains unsupported fields")
    version = raw.get("version", 0)
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise ValueError("model_roles.version must be a non-negative integer")
    roles_raw = raw.get("roles", {})
    if not isinstance(roles_raw, Mapping):
        raise ValueError("model_roles.roles must be an object")
    supported_roles = {"chat", "image_generation", *_ROUTE_LIST_ROLES}
    if set(roles_raw) - supported_roles:
        raise ValueError("unsupported model role")

    chat_raw = roles_raw.get("chat", {})
    if not isinstance(chat_raw, Mapping):
        raise ValueError("roles.chat must be an object")
    if set(chat_raw) - {"fallbacks", "allow_paid_fallback"}:
        raise ValueError("roles.chat contains unsupported fields")
    fallbacks_raw = chat_raw.get("fallbacks", [])
    if not isinstance(fallbacks_raw, list):
        raise ValueError("roles.chat.fallbacks must be an array")
    if len(fallbacks_raw) > 5:
        raise ValueError("roles.chat.fallbacks may contain at most 5 routes")
    allow_paid_fallback = chat_raw.get("allow_paid_fallback", False)
    if not isinstance(allow_paid_fallback, bool):
        raise ValueError("roles.chat.allow_paid_fallback must be a boolean")
    fallbacks = [
        _normalize_route(route, config=config)
        for route in fallbacks_raw
    ]
    seen: set[tuple[str, ...]] = set()
    for route in fallbacks:
        # The same provider/model is not a meaningful fallback even if the
        # caller changes only its effort setting.
        identity = (
            ("pool", route["pool_id"])
            if route["kind"] == "pool"
            else ("llm", route["provider"], route["model"])
        )
        if identity in seen:
            raise ValueError("roles.chat.fallbacks must not contain duplicates")
        seen.add(identity)

    image_raw = roles_raw.get("image_generation", {})
    if not isinstance(image_raw, Mapping):
        raise ValueError("roles.image_generation must be an object")
    if set(image_raw) - {"primary"}:
        raise ValueError("roles.image_generation contains unsupported fields")
    primary_raw = image_raw.get("primary")
    primary = (
        _normalize_route(primary_raw, config=config, image_generation=True)
        if primary_raw is not None
        else None
    )

    normalized_roles: dict[str, Any] = {
        "chat": {
            "fallbacks": fallbacks,
            "allow_paid_fallback": allow_paid_fallback,
        },
        "image_generation": {"primary": primary},
    }
    for role_name in _ROUTE_LIST_ROLES:
        if role_name not in roles_raw:
            continue
        role_raw = roles_raw[role_name]
        if not isinstance(role_raw, Mapping):
            raise ValueError(f"roles.{role_name} must be an object")
        if set(role_raw) - {"routes"}:
            raise ValueError(f"roles.{role_name} contains unsupported fields")
        routes_raw = role_raw.get("routes", [])
        if not isinstance(routes_raw, list):
            raise ValueError(f"roles.{role_name}.routes must be an array")
        routes = [_normalize_route(route, config=config) for route in routes_raw]
        normalized_roles[role_name] = {"routes": routes}

    return {
        "version": version,
        "roles": normalized_roles,
    }


def route_billing_class(config: Any, route: Any) -> str:
    """Classify a route as free, subscription, paid, or local."""

    if not isinstance(route, Mapping):
        return "paid"
    kind = route.get("kind")
    if kind == "pool":
        return "free"
    if kind != "llm":
        return "paid"
    provider = str(route.get("provider") or "").strip().lower()
    model = str(route.get("model") or "").strip()
    if provider in {"ollama", "sglang", "openai_compatible_local", "llama-cpp"}:
        return "local"
    if provider.endswith("-cli"):
        return "subscription"
    if not provider or not model:
        return "paid"
    billing_modes = _database_candidate_billing_modes(provider, model)
    if billing_modes.intersection({"complimentary", "free_tier", "promo_credit"}):
        return "free"
    return "paid"


def model_roles_view(config: Any) -> dict[str, Any]:
    """Return normalized role settings and billing labels for every route."""

    normalized = normalize_model_roles(_config_get(config, "model_roles", {}), config=config)
    billing_classes: dict[str, str] = {}
    routes: list[dict[str, Any]] = []
    roles = normalized["roles"]
    routes.extend(roles["chat"]["fallbacks"])
    primary = roles["image_generation"]["primary"]
    if primary is not None:
        routes.append(primary)
    for role_name in _ROUTE_LIST_ROLES:
        role = roles.get(role_name)
        if role is not None:
            routes.extend(role["routes"])
    for route in routes:
        if route["kind"] == "pool":
            key = f"pool/{route['pool_id']}"
        else:
            key = f"{route['provider']}/{route['model']}"
        billing_classes[key] = route_billing_class(config, route)
    return {
        "version": normalized["version"],
        "roles": normalized["roles"],
        "billing_classes": billing_classes,
    }


def update_model_roles(
    store_or_config: Any,
    payload: Any,
    expected_version: int,
) -> dict[str, Any]:
    """CAS-update the model_roles section and return its API view.

    Plain dictionaries are useful for isolated callers and tests.  Config
    stores use the existing row-locked dotted patch helper so concurrent
    requests cannot overwrite a newer role table.
    """

    if isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 0:
        raise ValueError("expected_version must be a non-negative integer")
    config_dict = (
        store_or_config
        if isinstance(store_or_config, dict)
        else getattr(store_or_config, "config", None)
    )
    if not isinstance(config_dict, dict):
        config_dict = store_or_config
    current = normalize_model_roles(
        _config_get(config_dict, "model_roles", {}),
        config=config_dict,
    )
    if current["version"] != expected_version:
        raise ModelRolesVersionConflict("model_roles_version_conflict")

    roles_raw = payload.get("roles") if isinstance(payload, Mapping) and set(payload) == {"roles"} else payload
    candidate = normalize_model_roles(
        {"version": expected_version, "roles": roles_raw},
        config=config_dict,
    )
    updated = {"version": expected_version + 1, "roles": candidate["roles"]}

    if isinstance(store_or_config, dict):
        # A plain mutable config has no backing store; its in-process value is
        # still updated only after the version check above.
        store_or_config["model_roles"] = copy.deepcopy(updated)
        return model_roles_view(store_or_config)

    saver = getattr(store_or_config, "save_to_file", None)
    if callable(saver) and not isinstance(getattr(store_or_config, "config", None), dict):
        # Small store adapters (including the route test adapter) own their
        # persistence boundary. The production Config object exposes its
        # backing ``config`` dict and follows the row-locked CAS path below.
        if not saver("model_roles", copy.deepcopy(updated)):
            raise ModelRolesPersistenceError("model_roles_persist_failed")
        return model_roles_view(store_or_config)

    from ..app_config_store import load_app_config_sync, update_app_config_keys_sync

    persisted = update_app_config_keys_sync(
        {"model_roles": updated},
        expected_values={"model_roles.version": expected_version},
    )
    if not persisted:
        try:
            latest = load_app_config_sync()
            latest_roles = normalize_model_roles(
                _config_get(latest, "model_roles", {}),
                config=latest,
            )
        except Exception:  # noqa: BLE001 - report generic persistence failure
            latest_roles = current
        if latest_roles["version"] != expected_version:
            raise ModelRolesVersionConflict("model_roles_version_conflict")
        raise ModelRolesPersistenceError("model_roles_persist_failed")

    setter = getattr(store_or_config, "set", None)
    if callable(setter):
        setter("model_roles", copy.deepcopy(updated))
    elif isinstance(config_dict, dict):
        config_dict["model_roles"] = copy.deepcopy(updated)
    return model_roles_view(config_dict)


__all__ = [
    "ModelRolesPersistenceError",
    "ModelRolesVersionConflict",
    "model_roles_view",
    "normalize_model_roles",
    "route_billing_class",
    "update_model_roles",
]
