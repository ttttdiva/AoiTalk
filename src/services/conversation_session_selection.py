"""Conversation session Agent Team selection helpers."""

from __future__ import annotations

import copy
import logging
from typing import Any

from .agent_team_v3 import agent_team_v3_teams
from .execution_profile_service import (
    FREE_TEAM_PROFILE_ID,
    list_team_execution_profiles,
    resolve_execution_main_route,
)

logger = logging.getLogger(__name__)


class SessionRouteStampError(Exception):
    """Raised when a client-provided new-session route cannot be persisted."""


_FREE_TEAM_ROUTE = {
    "provider": "routing-profile",
    "model": "free-team",
    "effort": "",
}

_TEAM_MODES = frozenset({"auto", "fixed"})
_CONTEXT_KEY = "chat_llm_settings"


def _clean_team_id(value: Any) -> str:
    return str(value or "").strip()


def _normalize_main_route_fragment(raw: Any) -> dict[str, str]:
    raw = raw if isinstance(raw, dict) else {}
    return {
        "provider": str(raw.get("provider") or "").strip().lower(),
        "model": str(raw.get("model") or "").strip(),
        "effort": str(raw.get("effort") or raw.get("reasoning_effort") or "").strip(),
    }


def _normalize_special_routing(raw: Any) -> dict[str, str]:
    raw = raw if isinstance(raw, dict) else {}
    return {
        "routing_profile_id": str(raw.get("routing_profile_id") or "").strip(),
    }


def _is_free_team_routing_active(settings: dict[str, Any]) -> bool:
    special = settings.get("special_routing")
    if not isinstance(special, dict):
        return False
    return (
        str(special.get("routing_profile_id") or "").strip() == FREE_TEAM_PROFILE_ID
    )


def is_free_team_special_routing_active(settings: dict[str, Any]) -> bool:
    return _is_free_team_routing_active(settings)


def free_team_main_route_fragment() -> dict[str, str]:
    return {
        "provider": _FREE_TEAM_ROUTE["provider"],
        "model": _FREE_TEAM_ROUTE["model"],
    }


async def stamp_new_session_main_route(
    repo: Any,
    session: Any,
    user_id: str,
    client_main_route: Any = None,
) -> Any:
    """Stamp a new session's persisted main_route.

    A client-provided explicit route always wins over last-used / global.
    Client stamp failure is fail-closed. Last-used fallback failure is logged.
    """
    from .user_llm_preference_service import (
        get_user_last_used_main_route,
        has_explicit_last_used_route,
        normalize_last_used_main_route,
    )

    client_explicit = has_explicit_last_used_route(client_main_route)
    route_to_stamp: dict[str, str] | None
    if client_explicit:
        route_to_stamp = normalize_last_used_main_route(client_main_route)
    else:
        try:
            last_used_route = await get_user_last_used_main_route(user_id)
        except Exception as exc:
            logger.warning(
                "Failed to read last-used LLM route for new session: %s",
                exc,
            )
            return session
        route_to_stamp = (
            last_used_route if has_explicit_last_used_route(last_used_route) else None
        )

    if not route_to_stamp:
        return session

    current_settings = read_session_llm_settings(getattr(session, "context", None))
    if not client_explicit and session_main_route_override_for_binding(current_settings) is not None:
        return session

    try:
        stamped_context = merge_session_llm_settings(
            getattr(session, "context", None),
            {"main_route": route_to_stamp},
        )
        stamped = await repo.update_session(
            str(session.id),
            touch_activity=False,
            context=stamped_context,
        )
        if not stamped:
            raise SessionRouteStampError(
                "Failed to persist displayed LLM route on new session"
            )
        session.context = stamped_context
        return session
    except SessionRouteStampError:
        if client_explicit:
            raise
        logger.warning("Failed to stamp last-used LLM route on new session")
        return session
    except Exception as exc:
        if client_explicit:
            raise SessionRouteStampError(
                "Failed to persist displayed LLM route on new session"
            ) from exc
        logger.warning(
            "Failed to stamp last-used LLM route on new session: %s",
            exc,
        )
        return session


def session_main_route_override_for_binding(
    settings: dict[str, Any],
) -> dict[str, str] | None:
    if _is_free_team_routing_active(settings):
        return dict(_FREE_TEAM_ROUTE)
    main_route = settings.get("main_route")
    if _has_explicit_session_route(main_route):
        return _normalize_main_route_fragment(main_route)
    return None


def default_session_llm_settings() -> dict[str, Any]:
    return {
        "agent_team_selection": {
            "mode": "auto",
            "team_id": "",
            "loaded_team_ids": [],
        },
        "main_route": _normalize_main_route_fragment({}),
        "special_routing": _normalize_special_routing({}),
        "execution_profile_id": "",
    }


def _existing_context(context: Any) -> dict[str, Any]:
    return copy.deepcopy(context) if isinstance(context, dict) else {}


def normalize_agent_team_selection(raw: Any) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    mode = str(raw.get("mode") or "auto").strip().lower()
    if mode not in _TEAM_MODES:
        mode = "auto"
    team_id = _clean_team_id(raw.get("team_id"))
    loaded = raw.get("loaded_team_ids")
    loaded_team_ids = []
    if isinstance(loaded, list):
        loaded_team_ids = sorted(
            dict.fromkeys(_clean_team_id(item) for item in loaded if _clean_team_id(item))
        )
    return {
        "mode": mode,
        "team_id": team_id if mode == "fixed" else "",
        "loaded_team_ids": loaded_team_ids,
    }


def _has_explicit_session_route(main_route: Any) -> bool:
    route = _normalize_main_route_fragment(main_route if isinstance(main_route, dict) else {})
    return bool(route.get("provider") and route.get("model"))


def _normalize_execution_profile_id(raw: Any) -> str:
    return str(raw or "").strip()


def normalize_session_llm_settings(raw: Any) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    team = normalize_agent_team_selection(raw.get("agent_team_selection"))
    special = _normalize_special_routing(raw.get("special_routing"))
    if special.get("routing_profile_id") != FREE_TEAM_PROFILE_ID:
        special = _normalize_special_routing({})
    execution_profile_id = _normalize_execution_profile_id(raw.get("execution_profile_id"))
    if team.get("mode") != "fixed" or special.get("routing_profile_id") == FREE_TEAM_PROFILE_ID:
        execution_profile_id = ""
    return {
        "agent_team_selection": team,
        "main_route": _normalize_main_route_fragment(raw.get("main_route")),
        "special_routing": special,
        "execution_profile_id": execution_profile_id,
    }


def read_session_llm_settings(context: Any) -> dict[str, Any]:
    root = _existing_context(context)
    stored = root.get(_CONTEXT_KEY)
    if not isinstance(stored, dict):
        return default_session_llm_settings()
    return normalize_session_llm_settings(stored)


def merge_session_llm_settings(context: Any, patch: Any) -> dict[str, Any]:
    root = _existing_context(context)
    current = read_session_llm_settings(root)
    patch = patch if isinstance(patch, dict) else {}
    merged = copy.deepcopy(current)
    if "agent_team_selection" in patch:
        team_patch = patch.get("agent_team_selection")
        team_current = merged["agent_team_selection"]
        team_next = normalize_agent_team_selection({**team_current, **(team_patch or {})})
        merged["agent_team_selection"] = team_next
    if "main_route" in patch:
        current_route = merged.get("main_route") if isinstance(merged.get("main_route"), dict) else {}
        route_patch = patch.get("main_route") if isinstance(patch.get("main_route"), dict) else {}
        merged["main_route"] = _normalize_main_route_fragment(
            {**current_route, **route_patch},
        )
    if "special_routing" in patch:
        current_special = (
            merged.get("special_routing")
            if isinstance(merged.get("special_routing"), dict)
            else {}
        )
        special_patch = (
            patch.get("special_routing")
            if isinstance(patch.get("special_routing"), dict)
            else {}
        )
        merged["special_routing"] = _normalize_special_routing(
            {**current_special, **special_patch},
        )
    if "execution_profile_id" in patch:
        merged["execution_profile_id"] = _normalize_execution_profile_id(
            patch.get("execution_profile_id"),
        )
    root[_CONTEXT_KEY] = normalize_session_llm_settings(merged)
    return root


def validate_agent_team_selection(
    selection: dict[str, Any],
    config: Any,
) -> tuple[dict[str, Any], list[str]]:
    normalized = normalize_agent_team_selection(selection)
    warnings: list[str] = []
    teams = {
        str(team.get("team_id") or ""): team
        for team in agent_team_v3_teams(config)
        if str(team.get("team_id") or "").strip()
    }
    if normalized["mode"] == "fixed":
        team_id = normalized["team_id"]
        team = teams.get(team_id)
        if not team:
            warnings.append(f"Unknown or disabled team: {team_id or '(empty)'}")
            normalized["team_id"] = ""
            normalized["mode"] = "auto"
        elif not team.get("enabled", True):
            warnings.append(f"Team is disabled: {team_id}")
            normalized["team_id"] = ""
            normalized["mode"] = "auto"
    loaded = []
    for team_id in normalized["loaded_team_ids"]:
        team = teams.get(team_id)
        if not team or not team.get("enabled", True):
            warnings.append(f"Loaded team unavailable: {team_id}")
            continue
        activation = team.get("activation") if isinstance(team, dict) else {}
        mode = str((activation or {}).get("mode") or "always").strip().lower()
        if mode != "manual":
            warnings.append(f"Team does not require manual loading: {team_id}")
            continue
        loaded.append(team_id)
    normalized["loaded_team_ids"] = loaded
    return normalized, warnings


def validate_session_llm_settings(
    patch: dict[str, Any],
    *,
    context: Any,
    config: Any,
) -> tuple[dict[str, Any], list[str]]:
    patch = patch if isinstance(patch, dict) else {}
    candidate_context = merge_session_llm_settings(context, patch)
    settings = read_session_llm_settings(candidate_context)
    warnings: list[str] = []
    team, team_warnings = validate_agent_team_selection(
        settings["agent_team_selection"],
        config,
    )
    warnings.extend(team_warnings)
    settings["agent_team_selection"] = team
    if "main_route" in patch:
        main_route = (
            settings.get("main_route") if isinstance(settings.get("main_route"), dict) else {}
        )
        provider = str(main_route.get("provider") or "").strip().lower()
        model = str(main_route.get("model") or "").strip()
        if provider and not model:
            warnings.append("Session main_route.model is required when provider is set")
            settings["main_route"] = _normalize_main_route_fragment({})
        elif model and not provider:
            warnings.append("Session main_route.provider is required when model is set")
            settings["main_route"] = _normalize_main_route_fragment({})
        elif provider == "openai_compatible_local" and model:
            from .llm_model_catalog import reasoning_effort_default_for_model
            from src.llm.openai_compatible_local_profiles import (
                llama_cpp_reasoning_effort_metadata,
            )

            effort = str(main_route.get("effort") or "").strip().lower()
            effort_metadata = llama_cpp_reasoning_effort_metadata(model)
            if effort_metadata:
                if not effort:
                    # Session route defaults are materialized in the session
                    # projection only; global config remains untouched.
                    effort = str(
                        reasoning_effort_default_for_model(provider, model) or ""
                    ).strip()
                    if effort:
                        settings["main_route"]["effort"] = effort
                        settings["main_route"]["reasoning_effort"] = effort
                elif effort not in effort_metadata["options"]:
                    warnings.append(
                        f"Invalid reasoning effort for {provider}/{model}: {effort}"
                    )
            # Persist the session route, but surface a canonical managed
            # runtime diagnostic immediately.  The turn builder repeats this
            # preflight against its request-scoped target config and refuses
            # to start generation when the warning is not actionable yet.
            try:
                from src.service_manager import resolve_llama_cpp_runtime

                resolved = resolve_llama_cpp_runtime(config, model=model)
                runtime_error = str(resolved.get("error") or "").strip()
                if runtime_error:
                    warnings.append(runtime_error)
            except Exception as exc:
                logger.debug("Session llama.cpp runtime preflight skipped: %s", exc)
    if "special_routing" in patch:
        candidate = (
            patch.get("special_routing")
            if isinstance(patch.get("special_routing"), dict)
            else {}
        )
        raw_id = str(candidate.get("routing_profile_id") or "").strip()
        if raw_id and raw_id != FREE_TEAM_PROFILE_ID:
            warnings.append(f"Unsupported special routing: {raw_id}")
            settings["special_routing"] = _normalize_special_routing({})
    team = settings.get("agent_team_selection") if isinstance(settings.get("agent_team_selection"), dict) else {}
    special = settings.get("special_routing") if isinstance(settings.get("special_routing"), dict) else {}
    execution_profile_id = _normalize_execution_profile_id(
        settings.get("execution_profile_id"),
    )
    if (
        str(team.get("mode") or "auto") != "fixed"
        or str(special.get("routing_profile_id") or "") == FREE_TEAM_PROFILE_ID
    ):
        settings["execution_profile_id"] = ""
    elif execution_profile_id:
        team_id = str(team.get("team_id") or "").strip()
        profiles = {
            str(item.get("profile_id") or ""): item
            for item in list_team_execution_profiles(config, team_id)
            if str(item.get("profile_id") or "").strip()
        }
        selected = profiles.get(execution_profile_id)
        if selected is None or not selected.get("enabled", True):
            warnings.append(
                f"Execution profile unavailable for team {team_id or '(empty)'}: {execution_profile_id}"
            )
            settings["execution_profile_id"] = ""
        else:
            settings["execution_profile_id"] = execution_profile_id
    else:
        settings["execution_profile_id"] = ""
    return settings, warnings


def session_loaded_team_ids(settings: dict[str, Any]) -> list[str]:
    team = settings.get("agent_team_selection") if isinstance(settings, dict) else {}
    team = team if isinstance(team, dict) else {}
    loaded = team.get("loaded_team_ids")
    if isinstance(loaded, list):
        return list(loaded)
    return []


def resolve_session_effective_main_route(config: Any, settings: dict[str, Any]) -> dict[str, Any]:
    from .session_llm_runtime_context import (
        bind_session_main_route_override,
        reset_session_main_route_override,
    )

    main_route_token = None
    try:
        override = session_main_route_override_for_binding(settings)
        if override is not None:
            main_route_token = bind_session_main_route_override(override)
        return resolve_execution_main_route(config)
    finally:
        if main_route_token is not None:
            reset_session_main_route_override(main_route_token)


def new_chat_effective_main_route(
    config: Any,
    last_used_route: Any,
) -> dict[str, Any]:
    from .session_llm_runtime_context import (
        bind_session_main_route_override,
        reset_session_main_route_override,
    )
    from .user_llm_preference_service import (
        has_explicit_last_used_route,
        normalize_last_used_main_route,
    )

    main_route_token = None
    try:
        normalized = normalize_last_used_main_route(last_used_route)
        if has_explicit_last_used_route(normalized):
            main_route_token = bind_session_main_route_override(normalized)
        return resolve_execution_main_route(config)
    finally:
        if main_route_token is not None:
            reset_session_main_route_override(main_route_token)


def new_chat_llm_defaults_envelope(
    config: Any,
    last_used_route: Any,
) -> dict[str, Any]:
    from .user_llm_preference_service import normalize_last_used_main_route

    last_used_main = normalize_last_used_main_route(last_used_route)
    effective_main = new_chat_effective_main_route(config, last_used_route)
    return {
        "last_used_main": last_used_main,
        "effective_main": {
            "provider": effective_main.get("provider"),
            "model": effective_main.get("model"),
            "effort": effective_main.get("effort")
            or effective_main.get("reasoning_effort"),
        },
    }


def session_llm_settings_envelope(
    context: Any,
    config: Any,
) -> dict[str, Any]:
    settings = read_session_llm_settings(context)
    effective_main = resolve_session_effective_main_route(config, settings)
    team = (
        settings.get("agent_team_selection")
        if isinstance(settings.get("agent_team_selection"), dict)
        else {}
    )
    team_id = str(team.get("team_id") or "").strip() if str(team.get("mode") or "") == "fixed" else ""
    team_profiles = [
        {
            "profile_id": str(item.get("profile_id") or ""),
            "name": str(item.get("name") or item.get("profile_id") or ""),
            "enabled": bool(item.get("enabled", True)),
        }
        for item in list_team_execution_profiles(config, team_id)
        if str(item.get("profile_id") or "").strip()
    ]
    return {
        "settings": settings,
        "active_execution_profile_id": str(settings.get("execution_profile_id") or ""),
        "execution_profiles": team_profiles,
        "loaded_team_ids": session_loaded_team_ids(settings),
        "effective_main": {
            "provider": effective_main.get("provider"),
            "model": effective_main.get("model"),
            "effort": effective_main.get("effort")
            or effective_main.get("reasoning_effort"),
        },
    }


__all__ = [
    "SessionRouteStampError",
    "default_session_llm_settings",
    "free_team_main_route_fragment",
    "is_free_team_special_routing_active",
    "merge_session_llm_settings",
    "new_chat_effective_main_route",
    "new_chat_llm_defaults_envelope",
    "read_session_llm_settings",
    "resolve_session_effective_main_route",
    "session_loaded_team_ids",
    "session_llm_settings_envelope",
    "session_main_route_override_for_binding",
    "stamp_new_session_main_route",
    "validate_session_llm_settings",
]
