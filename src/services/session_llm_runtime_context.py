"""Request-scoped session LLM overrides (agent team selection + main route)."""

from __future__ import annotations

import contextvars
from typing import Any

_SESSION_AGENT_TEAM_SELECTION: contextvars.ContextVar[dict[str, Any] | None] = (
    contextvars.ContextVar(
        "aoitalk_session_agent_team_selection",
        default=None,
    )
)
_SESSION_MAIN_ROUTE_OVERRIDE: contextvars.ContextVar[dict[str, str] | None] = (
    contextvars.ContextVar(
        "aoitalk_session_main_route_override",
        default=None,
    )
)
_SESSION_EXECUTION_PROFILE_ID: contextvars.ContextVar[str] = contextvars.ContextVar(
    "aoitalk_session_execution_profile_id",
    default="",
)


def session_agent_team_selection() -> dict[str, Any] | None:
    raw = _SESSION_AGENT_TEAM_SELECTION.get()
    if not isinstance(raw, dict):
        return None
    mode = str(raw.get("mode") or "auto").strip().lower()
    if mode not in {"auto", "fixed"}:
        mode = "auto"
    team_id = str(raw.get("team_id") or "").strip()
    loaded = raw.get("loaded_team_ids")
    loaded_team_ids: list[str] = []
    if isinstance(loaded, list):
        loaded_team_ids = sorted(
            dict.fromkeys(
                str(item).strip()
                for item in loaded
                if str(item).strip()
            )
        )
    return {
        "mode": mode,
        "team_id": team_id if mode == "fixed" else "",
        "loaded_team_ids": loaded_team_ids,
    }


def session_execution_profile_id() -> str:
    return str(_SESSION_EXECUTION_PROFILE_ID.get() or "").strip()


def session_main_route_override() -> dict[str, str] | None:
    raw = _SESSION_MAIN_ROUTE_OVERRIDE.get()
    if not isinstance(raw, dict):
        return None
    fragment = {
        key: str(raw.get(key) or "").strip()
        for key in ("provider", "model", "effort")
    }
    if not any(fragment.values()):
        return None
    return fragment


def bind_session_agent_team_selection(
    selection: dict[str, Any] | None,
) -> contextvars.Token[dict[str, Any] | None]:
    if not isinstance(selection, dict):
        return _SESSION_AGENT_TEAM_SELECTION.set(None)
    mode = str(selection.get("mode") or "auto").strip().lower()
    if mode not in {"auto", "fixed"}:
        mode = "auto"
    team_id = str(selection.get("team_id") or "").strip()
    loaded = selection.get("loaded_team_ids")
    loaded_team_ids: list[str] = []
    if isinstance(loaded, list):
        loaded_team_ids = sorted(
            dict.fromkeys(
                str(item).strip()
                for item in loaded
                if str(item).strip()
            )
        )
    return _SESSION_AGENT_TEAM_SELECTION.set(
        {
            "mode": mode,
            "team_id": team_id if mode == "fixed" else "",
            "loaded_team_ids": loaded_team_ids,
        }
    )


def bind_session_execution_profile_id(
    profile_id: str | None,
) -> contextvars.Token[str]:
    return _SESSION_EXECUTION_PROFILE_ID.set(str(profile_id or "").strip())


def bind_session_main_route_override(
    main_route: dict[str, Any] | None,
) -> contextvars.Token[dict[str, str] | None]:
    if not isinstance(main_route, dict):
        return _SESSION_MAIN_ROUTE_OVERRIDE.set(None)
    fragment = {
        "provider": str(main_route.get("provider") or "").strip().lower(),
        "model": str(main_route.get("model") or "").strip(),
        "effort": str(
            main_route.get("effort") or main_route.get("reasoning_effort") or ""
        ).strip(),
    }
    if not any(fragment.values()):
        return _SESSION_MAIN_ROUTE_OVERRIDE.set(None)
    return _SESSION_MAIN_ROUTE_OVERRIDE.set(fragment)


def reset_session_agent_team_selection(
    token: contextvars.Token[dict[str, Any] | None],
) -> None:
    _SESSION_AGENT_TEAM_SELECTION.reset(token)


def reset_session_main_route_override(
    token: contextvars.Token[dict[str, str] | None],
) -> None:
    _SESSION_MAIN_ROUTE_OVERRIDE.reset(token)


def reset_session_execution_profile_id(token: contextvars.Token[str]) -> None:
    _SESSION_EXECUTION_PROFILE_ID.reset(token)


__all__ = [
    "bind_session_agent_team_selection",
    "bind_session_execution_profile_id",
    "bind_session_main_route_override",
    "reset_session_agent_team_selection",
    "reset_session_execution_profile_id",
    "reset_session_main_route_override",
    "session_agent_team_selection",
    "session_execution_profile_id",
    "session_main_route_override",
]
