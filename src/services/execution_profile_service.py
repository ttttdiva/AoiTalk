"""Team-scoped Execution Profile helpers and leftover global-EP compatibility.

Runtime route resolution uses Team ``execution_profiles`` plus the session
selection.  Global ``execution_profiles`` / ``EP.main`` no longer override Main.
"""

from __future__ import annotations

import copy
import re
from typing import Any

from .agent_team_v3 import (
    _execution_route,
    _main_route,
    agent_team_v3_teams,
    resolve_agent_team_v3_route,
)
from .session_llm_runtime_context import session_main_route_override

_MANUAL_PROFILE_ID = "manual"
FREE_TEAM_PROFILE_ID = "free-team"
MANUAL_EXECUTION_PROFILE_ID = _MANUAL_PROFILE_ID
_PROFILE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_KNOWN_EXECUTION_PROFILE_PROVIDERS = frozenset(
    {
        "openai",
        "gemini",
        "ollama",
        "openrouter",
        "openai_compatible_local",
        "sglang",
        "codex-cli",
        "claude-cli",
        "antigravity-cli",
        "grok-cli",
        "kimi",
        "deepseek",
        "deepinfra",
        "routing-profile",
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


def _clean_id(value: Any, *, fallback: str = "") -> str:
    text = str(value or "").strip()
    return text if _PROFILE_ID_RE.fullmatch(text) else fallback


def _route_fragment(raw: Any) -> dict[str, str]:
    raw = raw if isinstance(raw, dict) else {}
    return {
        "provider": str(raw.get("provider") or "").strip().lower(),
        "model": str(raw.get("model") or "").strip(),
        "effort": str(raw.get("effort") or raw.get("reasoning_effort") or "").strip(),
    }


def normalize_execution_profile(profile_id: str, raw: Any) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    pid = _clean_id(raw.get("profile_id") or profile_id, fallback=_clean_id(profile_id))
    main = _route_fragment(raw.get("main"))
    overrides_raw = raw.get("llm_profile_overrides")
    overrides: dict[str, dict[str, Any]] = {}
    if isinstance(overrides_raw, dict):
        for key, item in overrides_raw.items():
            clean_key = _clean_id(key)
            if not clean_key:
                continue
            item = item if isinstance(item, dict) else {}
            target_type = str(item.get("target_type") or "inherit").strip().lower()
            if target_type not in {"inherit", "static", "pool"}:
                target_type = "inherit"
            overrides[clean_key] = {
                "profile_id": clean_key,
                "name": str(item.get("name") or clean_key).strip(),
                "target_type": target_type,
                "provider": str(item.get("provider") or "").strip().lower(),
                "model": str(item.get("model") or "").strip(),
                "effort_policy": str(item.get("effort_policy") or "same").strip().lower(),
                "effort": str(item.get("effort") or "").strip(),
                "pool_id": str(item.get("pool_id") or "").strip(),
                "routing_profile_id": str(item.get("routing_profile_id") or "").strip(),
            }
    return {
        "profile_id": pid,
        "display_name": str(raw.get("display_name") or raw.get("name") or pid).strip(),
        "enabled": bool(raw.get("enabled", True)),
        "system": bool(raw.get("system", False)),
        "main": main,
        "llm_profile_overrides": overrides,
    }


def default_execution_profiles() -> dict[str, dict[str, Any]]:
    return {
        _MANUAL_PROFILE_ID: normalize_execution_profile(
            _MANUAL_PROFILE_ID,
            {
                "profile_id": _MANUAL_PROFILE_ID,
                "display_name": "Manual",
                "enabled": True,
                "system": True,
                "main": {},
                "llm_profile_overrides": {},
            },
        ),
        FREE_TEAM_PROFILE_ID: normalize_execution_profile(
            FREE_TEAM_PROFILE_ID,
            {
                "profile_id": FREE_TEAM_PROFILE_ID,
                "display_name": "Free Team",
                "enabled": True,
                "system": True,
                "main": {
                    "provider": "routing-profile",
                    "model": "free-team",
                },
                "llm_profile_overrides": {},
            },
        ),
    }


def execution_profiles_section(config: Any) -> dict[str, dict[str, Any]]:
    raw = _config_get(config, "execution_profiles", {}) or {}
    defaults = default_execution_profiles()
    if not isinstance(raw, dict):
        return defaults
    if isinstance(raw.get("profiles"), dict):
        merged = dict(defaults)
        for profile_id, item in raw["profiles"].items():
            clean_id = _clean_id(profile_id)
            if not clean_id or clean_id in defaults:
                continue
            merged[clean_id] = normalize_execution_profile(clean_id, item)
        return merged
    merged = dict(defaults)
    for profile_id, item in raw.items():
        if profile_id == "active_profile_id":
            continue
        clean_id = _clean_id(profile_id)
        if not clean_id or clean_id in defaults:
            continue
        merged[clean_id] = normalize_execution_profile(clean_id, item)
    return merged


def _persisted_active_execution_profile_id(config: Any) -> str:
    raw = _config_get(config, "execution_profiles", {}) or {}
    if isinstance(raw, dict):
        nested = str(raw.get("active_profile_id") or "").strip()
        if nested:
            return nested
    legacy = str(_config_get(config, "llm.active_execution_profile_id", "") or "").strip()
    return legacy or _MANUAL_PROFILE_ID


def _write_active_execution_profile_id(config: Any, profile_id: str) -> None:
    if not hasattr(config, "set"):
        return
    clean = str(profile_id or _MANUAL_PROFILE_ID).strip() or _MANUAL_PROFILE_ID
    config.set("llm.active_execution_profile_id", clean)
    raw = _config_get(config, "execution_profiles", {}) or {}
    profiles_map: dict[str, Any]
    if isinstance(raw, dict) and isinstance(raw.get("profiles"), dict):
        profiles_map = dict(raw.get("profiles") or {})
    elif isinstance(raw, dict):
        profiles_map = {
            key: value
            for key, value in raw.items()
            if key != "active_profile_id"
        }
    else:
        profiles_map = {}
    config.set(
        "execution_profiles",
        {
            "active_profile_id": clean,
            "profiles": profiles_map,
        },
    )


def list_execution_profiles(config: Any) -> list[dict[str, Any]]:
    profiles = execution_profiles_section(config)
    return sorted(
        profiles.values(),
        key=lambda item: (
            0 if item.get("system") else 1,
            str(item.get("display_name") or item.get("profile_id") or ""),
        ),
    )


def get_execution_profile(config: Any, profile_id: str) -> dict[str, Any] | None:
    clean_id = _clean_id(profile_id)
    if not clean_id:
        return None
    profiles = execution_profiles_section(config)
    return profiles.get(clean_id)


def active_execution_profile_id(config: Any) -> str:
    return _persisted_active_execution_profile_id(config)


def is_manual_execution_profile(profile_id: str | None) -> bool:
    clean = str(profile_id or "").strip().lower()
    return not clean or clean == _MANUAL_PROFILE_ID


def resolve_active_execution_profile(config: Any) -> dict[str, Any]:
    profile_id = active_execution_profile_id(config)
    profile = get_execution_profile(config, profile_id)
    if profile is None:
        return execution_profiles_section(config)[_MANUAL_PROFILE_ID]
    return profile


def apply_main_execution_profile(
    main_route: dict[str, Any],
    profile: dict[str, Any] | None,
) -> dict[str, Any]:
    """No-op. Execution Profiles no longer override Main."""

    del profile
    return dict(main_route)


def effective_llm_profile(
    config: Any,
    profile_id: str,
    *,
    active_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compatibility stub. User-facing LLM Profiles are no longer canonical."""

    del config, active_profile
    return {
        "profile_id": str(profile_id or ""),
        "name": str(profile_id or "inherit"),
        "target_type": "inherit",
        "provider": "",
        "model": "",
        "effort_policy": "same",
        "effort": "",
        "pool_id": "",
        "routing_profile_id": "",
    }


def _apply_session_main_route_override(route: dict[str, Any]) -> dict[str, Any]:
    override = session_main_route_override()
    if not override:
        return route
    result = dict(route)
    override_provider = str(override.get("provider") or "").strip().lower()
    override_model = str(override.get("model") or "").strip()
    override_effort = str(override.get("effort") or "").strip()
    if override_provider:
        result["provider"] = override_provider
    if override_model:
        result["model"] = override_model
    if override_effort:
        result["effort"] = override_effort
        result["reasoning_effort"] = override_effort
    elif override_provider and override_model:
        result.pop("effort", None)
        result.pop("reasoning_effort", None)
    return result


def _catalogize_main_route_effort(route: dict[str, Any]) -> dict[str, Any]:
    """Keep only a raw effort accepted by the resolved Main model catalog."""

    result = dict(route)
    provider = str(result.get("provider") or "").strip().lower()
    model = str(result.get("model") or "").strip()
    effort = str(result.get("effort") or result.get("reasoning_effort") or "").strip()
    # A session Main override can replace a configured provider/model without
    # carrying the old provider's effort.  DeepSeek's request boundary uses
    # high when its setting is empty, so expose that same effective value for
    # inherit same/lower resolution.  Static Team model-default routes clear
    # this Main effort later in ``_profile_route`` and remain no-explicit.
    if provider == "deepseek" and model and not effort:
        effort = "high"
    # Managed Qwen3.8 llama.cpp profiles carry their default in profile
    # metadata.  Materialize it on the effective route only; never write this
    # derived value back into global config (so a session/model switch cannot
    # contaminate another profile).
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
        for item in reasoning_effort_options_for_model(provider, model)
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
    main = _main_route(config)
    return _catalogize_main_route_effort(_apply_session_main_route_override(main))


def resolve_execution_profile_route(
    config: Any,
    subagent_id: str,
    *,
    main_route: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    return resolve_agent_team_v3_route(config, subagent_id, main_route=main_route)


def _execution_profiles_payload(
    config: Any,
    active_profile_id: str,
    profiles_map: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if profiles_map is None:
        raw = _config_get(config, "execution_profiles", {}) or {}
        if isinstance(raw, dict) and isinstance(raw.get("profiles"), dict):
            profiles_map = dict(raw.get("profiles") or {})
        elif isinstance(raw, dict):
            profiles_map = {
                key: value
                for key, value in raw.items()
                if key != "active_profile_id"
            }
        else:
            profiles_map = {}
    clean_active = str(active_profile_id or _MANUAL_PROFILE_ID).strip() or _MANUAL_PROFILE_ID
    return {
        "active_profile_id": clean_active,
        "profiles": profiles_map,
    }


def execution_profiles_config_changes(
    config: Any,
    active_profile_id: str,
    profiles_map: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = _execution_profiles_payload(config, active_profile_id, profiles_map)
    return {
        "llm.active_execution_profile_id": payload["active_profile_id"],
        "execution_profiles": payload,
    }


def require_execution_profile(config: Any, profile_id: str) -> dict[str, Any]:
    """Read and validate a profile for activation without mutating config."""
    profile = get_execution_profile(config, profile_id)
    if profile is None:
        raise ValueError(f"Execution profile not found: {profile_id}")
    if not profile.get("enabled", True):
        raise ValueError(f"Execution profile is disabled: {profile_id}")
    errors = validate_execution_profile(config, profile)
    if errors:
        raise ValueError(errors[0])
    return profile


def deactivate_execution_profile(config: Any) -> None:
    _write_active_execution_profile_id(config, _MANUAL_PROFILE_ID)


def activate_execution_profile(config: Any, profile_id: str) -> dict[str, Any]:
    """Persist active profile id immediately (legacy callers). Prefer staged activation."""
    profile = require_execution_profile(config, profile_id)
    _write_active_execution_profile_id(config, profile["profile_id"])
    return profile


def execution_profile_envelope(config: Any) -> dict[str, Any]:
    """Thin leftover envelope. Global EP is not a routing source of truth."""

    main_route = resolve_execution_main_route(config)
    return {
        "active_profile_id": "",
        "active_profile": None,
        "manual": True,
        "profiles": [],
        "effective_main": {
            "provider": main_route.get("provider"),
            "model": main_route.get("model"),
            "effort": main_route.get("effort") or main_route.get("reasoning_effort"),
        },
        "canonical_llm_profiles": [],
        "deprecated": True,
        "source": "session_team_execution_profile",
    }


def _validate_execution_profile_id_key(
    source_profile_id: str,
    normalized_profile_id: str,
) -> list[str]:
    raw = str(source_profile_id or "").strip()
    clean_key = _clean_id(source_profile_id)
    norm_id = str(normalized_profile_id or "").strip()
    errors: list[str] = []
    if not clean_key or not _PROFILE_ID_RE.fullmatch(clean_key):
        errors.append(f"Invalid execution profile id key: {raw or '(empty)'}")
        return errors
    if norm_id != clean_key:
        errors.append(
            f"Execution profile id mismatch: key={raw} profile_id={norm_id or '(empty)'}",
        )
    return errors


def validate_execution_route(
    config: Any,
    route: dict[str, Any],
    *,
    label: str,
    main_route: dict[str, Any] | None = None,
) -> list[str]:
    from .llm_model_catalog import provider_models, reasoning_effort_options_for_model

    warnings: list[str] = []
    if not isinstance(route, dict):
        return [f"{label} must be an object"]
    # Validate the submitted shape before read-time normalization.  Explicit
    # provider/model routes may either name a catalog effort or explicitly opt
    # into the target model's own default.  Keeping this distinction here is
    # important because runtime normalization also accepts legacy payloads.
    submitted_provider = str(route.get("provider") or "").strip()
    submitted_model = str(route.get("model") or "").strip()
    submitted_inherit = route.get("inherit_model")
    submitted_is_explicit_model = submitted_inherit is False or (
        submitted_inherit is None and bool(submitted_provider and submitted_model)
    )
    if submitted_is_explicit_model:
        raw_provider = str(route.get("provider") or "").strip()
        raw_model = str(route.get("model") or "").strip()
        if not raw_provider or not raw_model:
            warnings.append(
                f"{label} requires both provider and model when inherit_model is false",
            )
            return warnings
    normalized = _execution_route(route)
    provider = str(normalized.get("provider") or "").strip().lower()
    model = str(normalized.get("model") or "").strip()
    effort = str(normalized.get("effort") or "").strip()
    if normalized.get("inherit_model", True):
        # inherit + same/lower/default: no value check.
        # inherit + explicit: persist the official effort name only.  Do not
        # reject just because the current Base/Main model does not expose it;
        # runtime applies the name against the Chat session Main.  An empty
        # explicit choice is different: it is incomplete and must be selected
        # before saving.
        if (
            str(route.get("effort_policy") or "").strip().lower() == "explicit"
            and not effort
        ):
            warnings.append(f"{label} requires an effort when effort_policy=explicit")
        return warnings
    if not provider or not model:
        warnings.append(f"{label} requires both provider and model when inherit_model is false")
        return warnings
    if provider not in _KNOWN_EXECUTION_PROFILE_PROVIDERS:
        warnings.append(f"Unknown provider in {label}: {provider or '(empty)'}")
        return warnings
    if provider == "routing-profile":
        if model and model != "free-team":
            warnings.append(f"Unknown routing profile model in {label}: {model}")
        return warnings
    raw_policy = str(route.get("effort_policy") or "").strip().lower()
    if provider == "gemini" and raw_policy == "explicit" and effort:
        warnings.append(
            f"Gemini explicit effort is unavailable in the Agent Team runtime for {label}",
        )
        return warnings
    if raw_policy not in {"explicit", "default"}:
        warnings.append(
            f"{label} requires effort_policy=default or explicit for an explicit provider/model route",
        )
        return warnings
    models, _error = provider_models(provider, config)
    model_ids = {
        str(item.get("id") or "").strip()
        for item in models
        if str(item.get("id") or "").strip()
    }
    options = [
        str(item).strip()
        for item in reasoning_effort_options_for_model(provider, model)
        if str(item).strip()
    ]
    if model_ids and model not in model_ids and raw_policy != "default" and not options:
        # A provider/model catalog can expose configured or remote IDs that
        # are not in the static provider list.  Known capability metadata is
        # sufficient for explicit values; model-default intentionally accepts
        # such custom IDs because it sends no provider-specific effort.
        warnings.append(f"Unknown model for {provider} in {label}: {model}")
    if raw_policy == "default":
        if effort:
            warnings.append(
                f"{label} must leave effort empty when effort_policy=default",
            )
        return warnings
    if not options:
        warnings.append(
            f"Effort is not supported for {provider}/{model} in {label}: {effort}",
        )
    elif not effort:
        warnings.append(
            f"Effort is required for {provider}/{model} in {label}",
        )
    elif effort not in options:
        warnings.append(
            f"Invalid effort for {provider}/{model} in {label}: {effort}",
        )
    return warnings


def validate_team_execution_profile(
    config: Any,
    profile: dict[str, Any],
    *,
    source_profile_id: str | None = None,
    known_subagent_ids: set[str] | None = None,
) -> list[str]:
    warnings: list[str] = []
    if not isinstance(profile, dict):
        return ["Execution profile must be an object"]
    profile_id = str(profile.get("profile_id") or "").strip()
    if source_profile_id is not None:
        warnings.extend(_validate_execution_profile_id_key(source_profile_id, profile_id))
        if warnings:
            return warnings
    if not _PROFILE_ID_RE.fullmatch(profile_id):
        warnings.append(f"Invalid execution profile id: {profile_id or '(empty)'}")
        return warnings
    if "enabled" in profile and not isinstance(profile.get("enabled"), bool):
        warnings.append(f"Execution profile enabled must be boolean: {profile_id}")
    default_route = profile.get("default_route")
    if default_route not in (None, "") and not isinstance(default_route, dict):
        warnings.append(f"default_route must be an object: {profile_id}")
    elif isinstance(default_route, dict):
        warnings.extend(
            validate_execution_route(
                config,
                default_route,
                label=f"execution_profiles[{profile_id}].default_route",
            )
        )
    overrides = profile.get("overrides")
    if overrides is None:
        return warnings
    if not isinstance(overrides, dict):
        warnings.append(f"overrides must be an object: {profile_id}")
        return warnings
    known = known_subagent_ids
    if known is None:
        known = {
            str(sid).strip()
            for team in agent_team_v3_teams(config)
            for sid in (team.get("subagent_ids") or [])
            if str(sid).strip()
        }
    for key, item in overrides.items():
        clean_key = _clean_id(key)
        if not clean_key:
            warnings.append(f"Invalid overrides key in {profile_id}: {key}")
            continue
        if known is not None and clean_key not in known:
            warnings.append(f"Unknown overrides key in {profile_id}: {clean_key}")
        if isinstance(item, dict):
            warnings.extend(
                validate_execution_route(
                    config,
                    item,
                    label=f"execution_profiles[{profile_id}].overrides[{clean_key}]",
                )
            )
        else:
            warnings.append(
                f"overrides[{clean_key}] must be an object in {profile_id}",
            )
    return warnings


def list_team_execution_profiles(config: Any, team_id: str) -> list[dict[str, Any]]:
    clean = str(team_id or "").strip()
    team = next(
        (
            item
            for item in agent_team_v3_teams(config)
            if str(item.get("team_id") or "") == clean
        ),
        None,
    )
    if not team:
        return []
    profiles = team.get("execution_profiles") if isinstance(team.get("execution_profiles"), dict) else {}
    return sorted(
        (copy.deepcopy(item) for item in profiles.values() if isinstance(item, dict)),
        key=lambda item: str(item.get("profile_id") or ""),
    )


def validate_execution_profile(
    config: Any,
    profile: dict[str, Any],
    *,
    source_profile_id: str | None = None,
) -> list[str]:
    """Validate one execution profile definition against catalog/topology."""

    from .llm_model_catalog import provider_models, reasoning_effort_options_for_model

    warnings: list[str] = []
    if not isinstance(profile, dict):
        return ["Execution profile must be an object"]

    profile_id = str(profile.get("profile_id") or "").strip()
    if source_profile_id is not None:
        warnings.extend(_validate_execution_profile_id_key(source_profile_id, profile_id))
        if warnings:
            return warnings
    if not _PROFILE_ID_RE.fullmatch(profile_id):
        warnings.append(f"Invalid execution profile id: {profile_id or '(empty)'}")
        return warnings

    def _validate_route(
        route: dict[str, Any],
        *,
        label: str,
        require_provider: bool,
    ) -> None:
        provider = str(route.get("provider") or "").strip().lower()
        model = str(route.get("model") or "").strip()
        effort = str(route.get("effort") or route.get("reasoning_effort") or "").strip()
        inherit_model = route.get("inherit_model") is not False and not (
            route.get("inherit_model") is None and provider and model
        )
        if not provider and not model and not effort:
            return
        if require_provider and (not provider or not model):
            warnings.append(f"{label} requires both provider and model")
            return
        if provider and provider not in _KNOWN_EXECUTION_PROFILE_PROVIDERS:
            warnings.append(f"Unknown provider in {label}: {provider or '(empty)'}")
            return
        if provider == "routing-profile":
            if model and model != "free-team":
                warnings.append(f"Unknown routing profile model in {label}: {model}")
            return
        if inherit_model:
            # An inherited explicit route is resolved against the actual
            # session Main model at runtime.  Keep that deferred capability
            # lookup, but do not persist an incomplete explicit selection.
            if (
                str(route.get("effort_policy") or "").strip().lower() == "explicit"
                and not effort
            ):
                warnings.append(f"{label} requires an effort when effort_policy=explicit")
            return
        model_default = False
        if not inherit_model:
            raw_policy = str(route.get("effort_policy") or "").strip().lower()
            if provider == "gemini" and raw_policy == "explicit" and effort:
                warnings.append(
                    f"Gemini explicit effort is unavailable in the Agent Team runtime for {label}",
                )
                return
            if raw_policy not in {"explicit", "default"}:
                warnings.append(
                    f"{label} requires effort_policy=default or explicit for an explicit provider/model route",
                )
                return
            if raw_policy == "default":
                model_default = True
                if effort:
                    warnings.append(
                        f"{label} must leave effort empty when effort_policy=default",
                    )
            elif not effort:
                warnings.append(f"Effort is required for {provider}/{model} in {label}")
                return
        if provider and model:
            models, _error = provider_models(provider, config)
            model_ids = {
                str(item.get("id") or "").strip()
                for item in models
                if str(item.get("id") or "").strip()
            }
            options = reasoning_effort_options_for_model(provider, model)
            if model_ids and model not in model_ids and not model_default and not options:
                warnings.append(f"Unknown model for {provider} in {label}: {model}")
        else:
            options = reasoning_effort_options_for_model(provider, model) if provider and model else []
        if provider and model and effort and not model_default:
            if not options:
                warnings.append(
                    f"Effort is not supported for {provider}/{model} in {label}: {effort}",
                )
            elif effort not in options:
                warnings.append(
                    f"Invalid effort for {provider}/{model} in {label}: {effort}",
                )

    main = profile.get("main") if isinstance(profile.get("main"), dict) else {}
    _validate_route(main, label="execution profile main route", require_provider=True)
    overrides = profile.get("llm_profile_overrides")
    if isinstance(overrides, dict):
        for key, item in overrides.items():
            clean_key = _clean_id(key)
            if isinstance(item, dict):
                _validate_route(
                    item,
                    label=f"llm_profile_overrides[{clean_key}]",
                    require_provider=False,
                )
    default_route = profile.get("default_route")
    if isinstance(default_route, dict):
        warnings.extend(
            validate_execution_route(
                config,
                default_route,
                label=f"execution profile {profile_id} default_route",
            )
        )
    team_overrides = profile.get("overrides")
    if isinstance(team_overrides, dict):
        for key, item in team_overrides.items():
            clean_key = _clean_id(key)
            if isinstance(item, dict):
                warnings.extend(
                    validate_execution_route(
                        config,
                        item,
                        label=f"overrides[{clean_key}]",
                    )
                )
    return warnings


__all__ = [
    "activate_execution_profile",
    "active_execution_profile_id",
    "apply_main_execution_profile",
    "deactivate_execution_profile",
    "effective_llm_profile",
    "execution_profiles_config_changes",
    "execution_profile_envelope",
    "execution_profiles_section",
    "get_execution_profile",
    "is_manual_execution_profile",
    "list_execution_profiles",
    "list_team_execution_profiles",
    "normalize_execution_profile",
    "require_execution_profile",
    "resolve_active_execution_profile",
    "resolve_execution_main_route",
    "resolve_execution_profile_route",
    "validate_execution_profile",
    "validate_execution_route",
    "validate_team_execution_profile",
]
