"""Pure planning helpers for the Agent Team to Agent revision migration.

P2 is deliberately split into two parts.  This module only reads a revision
and an App Config value and returns a deterministic conversion plan.  It does
not open a database connection, mutate a model, or write an audit row.  The
database-facing command in ``scripts/migrate_agent_revisions_v2.py`` consumes
the plan and owns the transaction boundary.

The command creates a new immutable AgentRevision. Existing AgentWorkItem and
AgentRun pins and lifecycle payloads are compatibility data, not migration
targets; new selections resolve the new latest revision.

The old resolver is still the authority for the ``before`` side of the plan.
That matters for profiles such as ``heavy``/``light``/``coding`` and for
custom Free Team pools: copying a raw provider/model pair would lose the
normalization performed by the live resolver.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any


# The design maps these old execution roles into the P2 role vocabulary.  The
# runtime list is imported lazily by ``_runtime_roles`` so importing this
# module remains cheap and so tests can inject a small resolver without
# importing provider code.
LEGACY_ROLE_TO_V2_ROLE: dict[str, str] = {
    # Team/Director main lanes have no dedicated legacy Subagent but still
    # resolve through the configured Chat Main route.
    "director": "chat",
    "coordinator": "chat",
    "team_main": "chat",
    "main": "chat",
    "planner": "research",
    "researcher": "research",
    "writer": "chat",
    "data_worker": "chat",
    "general_worker": "chat",
    "operator": "chat",
    "code_worker": "code",
    "explorer": "code",
    "reviewer": "review",
}

V2_ROLES = frozenset({"chat", "summarize", "code", "review", "research"})
WORKER_KINDS = frozenset({"codex", "antigravity", "claude", "grok", "muse"})


class AgentRevisionMigrationError(ValueError):
    """A malformed conversion input that cannot be safely planned."""


def _read(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _unique(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = _text(value)
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _config_get(config: Any, dotted: str, default: Any = None) -> Any:
    if isinstance(config, Mapping):
        current: Any = config
        for part in dotted.split("."):
            if not isinstance(current, Mapping) or part not in current:
                return default
            current = current[part]
        return current
    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            return getter(dotted, default)
        except TypeError:
            return default
    return default


def _runtime_roles() -> Mapping[str, tuple[str, ...]]:
    from .agent_model_routing import OPERATIONS_WORK_ROLES

    return OPERATIONS_WORK_ROLES


def _runtime_subagents(config: Any) -> dict[str, dict[str, Any]]:
    from .agent_team_v3 import agent_team_v3_subagents

    return {
        str(item.get("subagent_id")): dict(item)
        for item in agent_team_v3_subagents(config, include_disabled=True)
        if isinstance(item, Mapping) and str(item.get("subagent_id") or "").strip()
    }


def _runtime_teams(config: Any) -> dict[str, dict[str, Any]]:
    from .agent_team_v3 import agent_team_v3_teams

    return {
        str(item.get("team_id")): dict(item)
        for item in agent_team_v3_teams(config)
        if isinstance(item, Mapping) and str(item.get("team_id") or "").strip()
    }


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _route_signature(route: Mapping[str, Any] | None) -> str:
    return _canonical_json(dict(route or {}))


def _profile_section(config: Any, team_id: str, profile_id: str) -> Mapping[str, Any] | None:
    section = _config_get(config, "agent_team", {})
    teams = section.get("teams") if isinstance(section, Mapping) else None
    team = teams.get(team_id) if isinstance(teams, Mapping) else None
    profiles = team.get("execution_profiles") if isinstance(team, Mapping) else None
    raw = profiles.get(profile_id) if isinstance(profiles, Mapping) else None
    return raw if isinstance(raw, Mapping) else None


def _subagent_raw(config: Any, subagent_id: str) -> Mapping[str, Any] | None:
    section = _config_get(config, "agent_team", {})
    subagents = section.get("subagents") if isinstance(section, Mapping) else None
    value = subagents.get(subagent_id) if isinstance(subagents, Mapping) else None
    return value if isinstance(value, Mapping) else None


def _free_team_profile(config: Any) -> Mapping[str, Any]:
    # Use the same merge/default function as runtime.  The fallback is useful
    # for pure tests that provide a deliberately tiny config.
    try:
        from .free_team_service import free_team_profile

        value = free_team_profile(config)
        if isinstance(value, Mapping):
            return value
    except Exception:
        pass
    return {}


def _legacy_profile_defaults(profile_id: str) -> Mapping[str, Any] | None:
    try:
        from .agent_team_v3 import AGENT_TEAM_DEFAULT_LLM_PROFILES

        value = AGENT_TEAM_DEFAULT_LLM_PROFILES.get(profile_id)
        return value if isinstance(value, Mapping) else None
    except Exception:
        return None


def _free_team_overlay(config: Any, profile_id: str) -> Mapping[str, Any] | None:
    # Historical P2 conversion may still read the archived pre-P5 overlay;
    # current Free Team runtime never reads this path.
    raw = _config_get(config, "routing_profiles.free-team", {})
    overlays = raw.get("llm_profiles") if isinstance(raw, Mapping) else None
    value = overlays.get(profile_id) if isinstance(overlays, Mapping) else None
    return value if isinstance(value, Mapping) else None


def _route_spec_for(
    config: Any,
    revision: Any,
    subagent_id: str,
) -> Mapping[str, Any] | None:
    """Return the raw Team/profile route when it is available.

    This is only metadata for preserving pool/effort policy.  The effective
    provider/model still comes from ``resolve_agent_role_route``.
    """

    team_id = _text(_read(revision, "agent_team_id", _read(revision, "team_id", "")))
    profile_id = _text(
        _read(revision, "execution_profile_id", _read(revision, "execution_profile", ""))
    )
    profile = _profile_section(config, team_id, profile_id)
    if profile is not None:
        overrides = profile.get("overrides")
        if isinstance(overrides, Mapping) and isinstance(overrides.get(subagent_id), Mapping):
            return overrides[subagent_id]
        if isinstance(profile.get("default_route"), Mapping):
            return profile["default_route"]

    raw_subagent = _subagent_raw(config, subagent_id)
    llm_profile_id = _text(raw_subagent.get("llm_profile_id")) if raw_subagent else ""
    if llm_profile_id:
        overlay = _free_team_overlay(config, llm_profile_id)
        if overlay is not None:
            return overlay
        legacy = _legacy_profile_defaults(llm_profile_id)
        if legacy is not None:
            return legacy

    overlay = _free_team_overlay(config, profile_id)
    if overlay is not None:
        return overlay
    return _legacy_profile_defaults(profile_id)


def _free_team_pool(
    config: Any,
    revision: Any,
    subagent_id: str,
    raw_spec: Mapping[str, Any] | None,
) -> dict[str, Any]:
    profile = _free_team_profile(config)
    profile_id = _text(
        _read(revision, "execution_profile_id", _read(revision, "execution_profile", ""))
    )
    raw_spec = raw_spec or {}
    pool_id = _text(raw_spec.get("pool_id"))
    routing_profile_id = _text(raw_spec.get("routing_profile_id")) or "free-team"

    # ``free-team`` itself routes Main through the configured main pool.  The
    # named legacy profiles use their own overlay pool (heavy/light/coding),
    # and custom Team profiles may carry an explicit pool_id.
    if not pool_id:
        overlay = _free_team_overlay(config, profile_id)
        if overlay is not None:
            pool_id = _text(overlay.get("pool_id"))
            routing_profile_id = _text(overlay.get("routing_profile_id")) or routing_profile_id
    if not pool_id:
        raw_subagent = _subagent_raw(config, subagent_id)
        subagent_profile_id = _text(raw_subagent.get("llm_profile_id")) if raw_subagent else ""
        overlay = _free_team_overlay(config, subagent_profile_id)
        if overlay is not None:
            pool_id = _text(overlay.get("pool_id"))
            routing_profile_id = _text(overlay.get("routing_profile_id")) or routing_profile_id
    if not pool_id:
        pool_id = _text(profile.get("main_pool_id")) or "coordinator"

    pools = profile.get("pools") if isinstance(profile, Mapping) else {}
    if not isinstance(pools, Mapping) or pool_id not in pools:
        raise AgentRevisionMigrationError("free_team_pool_unavailable")
    pool = pools.get(pool_id) if isinstance(pools, Mapping) else None
    pool = pool if isinstance(pool, Mapping) else {}
    # The persisted P2 route contract is intentionally strict.  Pool
    # candidate order, tool mode, and effort policy remain owned by the
    # Free Team pool selected by ``pool_id`` and must not be copied into an
    # ``llm``/pool route object.
    del pool, routing_profile_id, profile_id
    return {"kind": "pool", "pool_id": pool_id}


def normalize_route(
    config: Any,
    revision: Any,
    route: Any,
    *,
    subagent_id: str = "",
    raw_spec: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Convert an effective legacy route to the P2 route shape.

    Provider credentials, base URLs, and API keys are intentionally omitted.
    Free Team metadata is resolved through its runtime profile/pool merger so
    a custom ``main_pool_id`` remains effective after migration.
    """

    provider = _text(_read(route, "provider")).lower()
    model = _text(_read(route, "model"))
    if provider == "routing-profile" and model == "free-team":
        return _free_team_pool(config, revision, subagent_id, raw_spec)
    if provider == "muse-remote":
        # P1's typed Muse route requires the stable model identifier even
        # though the legacy Team resolver represents the remote backend with
        # ``model=None``.  Runtime adapts muse-chat back to the same remote
        # AgentModelRoute; the migration also exposes delegate_worker:muse.
        if model and model != "muse-chat":
            raise AgentRevisionMigrationError("muse_remote_model_unsupported")
        return {"kind": "llm", "provider": provider, "model": "muse-chat"}
    if not provider or not model:
        raise AgentRevisionMigrationError("legacy route has no provider/model")
    result: dict[str, Any] = {"kind": "llm", "provider": provider, "model": model}
    effort = _text(_read(route, "effort", _read(route, "reasoning_effort", "")))
    if effort:
        result["effort"] = effort
    return result


def _normalize_global_route(config: Any, value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    routes = value.get("routes")
    raw = routes[0] if isinstance(routes, list) and routes else value
    if not isinstance(raw, Mapping):
        return None
    kind = _text(raw.get("kind")).lower()
    if kind == "pool" or raw.get("pool_id") not in (None, "") or raw.get("pool") not in (None, ""):
        pool_id = _text(raw.get("pool_id") or raw.get("pool"))
        if not pool_id:
            return None
        return {"kind": "pool", "pool_id": pool_id}
    provider = _text(raw.get("provider")).lower()
    model = _text(raw.get("model"))
    if provider == "routing-profile" and model == "free-team":
        pool_id = _text(_config_get(config, "routing_profiles.free-team.main_pool_id", "")) or "coordinator"
        return {"kind": "pool", "pool_id": pool_id}
    if provider and model:
        result = {"kind": "llm", "provider": provider, "model": model}
        effort = _text(raw.get("effort") or raw.get("reasoning_effort"))
        if effort:
            result["effort"] = effort
        return result
    return None


def _legacy_main_global_route(config: Any) -> dict[str, Any] | None:
    """Resolve the P1 Chat primary (legacy write-through) route."""

    provider = _text(_config_get(config, "llm_provider", "openai")).lower()
    model = _text(_config_get(config, "llm_model", ""))
    selection_kind = _text(_config_get(config, "llm_selection_kind", "")).lower()
    if provider == "routing-profile" or model == "free-team" or selection_kind == "routing_profile":
        pool_id = _text(
            _config_get(config, "routing_profiles.free-team.main_pool_id", "coordinator")
        ) or "coordinator"
        return {"kind": "pool", "pool_id": pool_id}
    try:
        from .model_roles_service import resolve_execution_main_route

        effective = resolve_execution_main_route(config)
        provider = _text(effective.get("provider")).lower() or provider
        model = _text(effective.get("model")) or model
        effort = _text(effective.get("effort") or effective.get("reasoning_effort"))
    except Exception:
        effort = ""
    if not provider or not model:
        return None
    route: dict[str, Any] = {"kind": "llm", "provider": provider, "model": model}
    if effort:
        route["effort"] = effort
    return route


def model_roles_table(config: Any, model_roles: Any = None) -> dict[str, dict[str, Any]]:
    """Return the first canonical route for each P1 role."""

    source = model_roles
    if source is None:
        source = _config_get(config, "model_roles", {})
    if not isinstance(source, Mapping):
        return {}
    roles = source.get("roles") if isinstance(source.get("roles"), Mapping) else source
    result: dict[str, dict[str, Any]] = {}
    chat_primary = _legacy_main_global_route(config)
    chat_block = roles.get("chat") if isinstance(roles, Mapping) else None
    if isinstance(chat_block, Mapping):
        # A few pre-release P1 snapshots used ``primary``.  Keep it readable,
        # while the current write-through contract takes Main from legacy
        # llm_provider/llm_model and stores only chat fallbacks.
        chat_primary = _normalize_global_route(config, chat_block.get("primary")) or chat_primary
    if chat_primary is not None:
        result_chat = copy.deepcopy(chat_primary)
        result["chat"] = copy.deepcopy(result_chat)
    else:
        result_chat = None
    for role, value in roles.items():
        clean_role = _text(role)
        if not clean_role or clean_role not in V2_ROLES:
            continue
        if clean_role == "chat":
            if result_chat is not None:
                result[clean_role] = copy.deepcopy(result_chat)
            continue
        route = _normalize_global_route(config, value)
        if route is not None:
            result[clean_role] = route
    # Empty/missing P1 role rows inherit the complete Chat route chain.  P2
    # only needs the primary for comparison because the legacy Team resolver
    # selects one route before its internal pool fallback.
    if result_chat is not None:
        for role in ("summarize", "code", "review", "research"):
            result.setdefault(role, copy.deepcopy(result_chat))
    return result


def _role_allow_paid_fallback(config: Any, role: str) -> bool | None:
    roles = _config_get(config, "model_roles.roles", {})
    item = roles.get(role) if isinstance(roles, Mapping) else None
    if isinstance(item, Mapping) and isinstance(item.get("allow_paid_fallback"), bool):
        return item["allow_paid_fallback"]
    return None


def _default_route_resolver(config: Any, revision: Any, role: str) -> Any:
    subagent_id = {
        "planner": "architecture_planner",
        "researcher": "general_researcher",
        "writer": "general_worker",
        "data_worker": "general_worker",
        "code_worker": "code_implementer",
        "reviewer": "code_reviewer",
        "operator": "employee_operator",
    }.get(role, role)
    return _default_subagent_route_resolver(config, revision, role, subagent_id)


def _default_subagent_route_resolver(
    config: Any,
    revision: Any,
    role: str,
    subagent_id: str,
) -> Any:
    main = _resolve_main_legacy_route(config, revision)
    raw_spec = _route_spec_for(config, revision, subagent_id)
    if isinstance(raw_spec, Mapping):
        from .agent_team_v3 import _apply_execution_route

        route = _apply_execution_route(dict(raw_spec), dict(main), config=config)
    else:
        route = dict(main)
    provider = _text(route.get("provider")).lower()
    model = route.get("model")
    return {
        "provider": provider,
        "model": model,
        "effort": _text(route.get("effort") or route.get("reasoning_effort")),
        "backend": "remote" if provider == "muse-remote" else ("cli" if provider.endswith("-cli") else "api"),
        "acting_subagent_id": subagent_id,
        "role": role,
    }


def _resolve_main_legacy_route(config: Any, revision: Any) -> Any:
    """Resolve a Team/Profile default route for the legacy main lane."""

    from .model_roles_service import resolve_execution_main_route
    from .agent_team_v3 import _apply_execution_route

    main = resolve_execution_main_route(config)
    profile_id = _text(
        _read(revision, "execution_profile_id", _read(revision, "execution_profile", ""))
    )
    if profile_id == "free-team":
        return {
            "provider": "routing-profile",
            "model": "free-team",
        }
    if profile_id == "manual":
        return main
    raw_spec = _route_spec_for(config, revision, "")
    if isinstance(raw_spec, Mapping):
        return _apply_execution_route(dict(raw_spec), dict(main), config=config)
    return main


def _closest_v2_role(role: str) -> str:
    clean = _text(role).casefold()
    if any(token in clean for token in ("review", "critic", "qa")):
        return "review"
    if any(token in clean for token in ("code", "engineer", "implement", "explor")):
        return "code"
    if any(token in clean for token in ("research", "plan", "architect", "investig")):
        return "research"
    if any(token in clean for token in ("summar", "digest")):
        return "summarize"
    return "chat"


def _validate_v2_role_route(
    config: Any,
    revision: Any,
    role: str,
    model_overrides: Mapping[str, Any],
) -> str | None:
    """Validate the planned role through the additive v2 resolver."""

    try:
        from .agent_revision_v2 import resolve_agent_role_route_v2

        if isinstance(revision, Mapping):
            candidate = dict(revision)
        else:
            candidate = {
                name: getattr(revision, name, None)
                for name in (
                    "id",
                    "agent_id",
                    "agent_team_id",
                    "execution_profile_id",
                    "allowed_subagent_ids",
                    "model_overrides",
                    "tool_ids",
                    "delegate_policy",
                )
            }
        candidate["model_overrides"] = copy.deepcopy(dict(model_overrides))
        resolve_agent_role_route_v2(config, candidate, role)
    except Exception as exc:
        return exc.__class__.__name__
    return None


def _resolve_v2_route_projection(
    config: Any,
    revision: Any,
    role: str,
    model_overrides: Mapping[str, Any],
    planned_route: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    """Resolve and project one final role route through the v2 reader."""

    try:
        from .agent_revision_v2 import resolve_agent_role_route_v2

        if isinstance(revision, Mapping):
            candidate = dict(revision)
        else:
            candidate = {
                name: getattr(revision, name, None)
                for name in (
                    "id",
                    "agent_id",
                    "agent_team_id",
                    "execution_profile_id",
                    "allowed_subagent_ids",
                    "model_overrides",
                    "tool_ids",
                    "delegate_policy",
                )
            }
        candidate["model_overrides"] = copy.deepcopy(dict(model_overrides))
        resolved = resolve_agent_role_route_v2(config, candidate, role)
        if isinstance(resolved, tuple) and resolved:
            resolved = resolved[0]
        if isinstance(resolved, Mapping) and _text(resolved.get("kind")).lower() == "pool":
            pool_id = _text(resolved.get("pool_id"))
            if pool_id:
                return {"kind": "pool", "pool_id": pool_id}, None
            return None, "v2_pool_route_missing_pool_id"
        provider = _text(_read(resolved, "provider")).lower()
        model = _read(resolved, "model")
        if provider == "routing-profile" and _text(model) == "free-team":
            if planned_route.get("kind") == "pool":
                return {"kind": "pool", "pool_id": _text(planned_route.get("pool_id"))}, None
            return None, "v2_pool_route_missing_pool_id"
        if model in (None, ""):
            return None, "v2_remote_route_requires_worker_tool"
        route: dict[str, Any] = {
            "kind": "llm",
            "provider": provider,
            "model": _text(model),
        }
        effort = _text(_read(resolved, "effort", _read(resolved, "reasoning_effort", "")))
        if effort:
            route["effort"] = effort
        return route, None
    except Exception as exc:
        return None, exc.__class__.__name__


def _known_team_members(config: Any, revision: Any) -> set[str]:
    team_id = _text(_read(revision, "agent_team_id", _read(revision, "team_id", "")))
    team = _runtime_teams(config).get(team_id) or {}
    return {_text(item) for item in (team.get("subagent_ids") or []) if _text(item)}


def _allowed_ids(revision: Any) -> list[str]:
    value = _read(revision, "allowed_subagent_ids", _read(revision, "allowed_subagents", []))
    return _unique(value if isinstance(value, (list, tuple, set, frozenset)) else [])


def _explicit_legacy_roles(revision: Any) -> list[str]:
    value = _read(revision, "legacy_work_roles", _read(revision, "work_roles", []))
    if isinstance(value, Mapping):
        return _unique(value.keys())
    if isinstance(value, (list, tuple, set, frozenset)):
        return _unique(value)
    return []


def _worker_kind(provider: str) -> str | None:
    provider = _text(provider).lower()
    if provider == "muse-remote":
        return "muse"
    for kind in ("codex", "antigravity", "claude", "grok"):
        if provider == f"{kind}-cli":
            return kind
    return None


def _derive_tools(
    config: Any,
    revision: Any,
    source_routes: Mapping[str, Mapping[str, Any]],
    extra_routes: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[list[str], dict[str, Any]]:
    try:
        from .agent_capabilities import AGENT_CAPABILITY_CATALOG as AGENT_TEAM_CAPABILITY_CATALOG

        catalog = AGENT_TEAM_CAPABILITY_CATALOG
    except Exception:
        catalog = {}
    ceiling = {
        _text(item)
        for item in (
            _read(revision, "capability_ceiling_json", _read(revision, "capability_ceiling", []))
            or []
        )
        if _text(item)
    }
    allowed = set(_allowed_ids(revision))
    subagents = _runtime_subagents(config)
    teams = _runtime_teams(config)
    team_id = _text(_read(revision, "agent_team_id", _read(revision, "team_id", "")))
    team = teams.get(team_id) or {}
    team_enabled = team.get("enabled", True) is not False
    team_members = {
        _text(item)
        for item in (team.get("subagent_ids") or [])
        if _text(item)
    }
    member_capabilities: set[str] = set()
    for subagent_id in sorted(allowed & team_members):
        item = subagents.get(subagent_id) or {}
        if item.get("enabled", True) is False:
            continue
        member_capabilities.update(
            _text(capability)
            for capability in (item.get("capability_ids") or [])
            if _text(capability) in catalog
        )
    # Legacy authority is the intersection of the revision ceiling and the
    # declared capabilities of enabled members of the pinned Team.  Keeping
    # the raw ceiling here would expose a capability that the legacy
    # Subagent/runtime could never use.  An empty ceiling therefore remains an
    # empty authority surface; no workspace_read (or other capability) is
    # inferred from a Subagent declaration.
    effective_capabilities = member_capabilities & ceiling if team_enabled else set()
    tool_ids = sorted(effective_capabilities & set(catalog))

    try:
        from .agent_team_v3 import agent_team_v3_delegation_enabled

        delegation_enabled = bool(agent_team_v3_delegation_enabled(config))
    except Exception:
        delegation_enabled = False
    route_values = list(source_routes.values()) + list((extra_routes or {}).values())
    worker_kinds = sorted(
        {
            kind
            for route in route_values
            if (
                _text(route.get("target_role", "chat")) != "chat"
                or _text(route.get("provider")).lower() == "muse-remote"
            )
            if (kind := _worker_kind(_text(route.get("provider")))) is not None
        }
    )
    # Muse worker delegation is a route-owned capability.  The legacy worker
    # path accepted the explicit remote ceiling even though the built-in
    # Subagent catalog intentionally does not declare this capability on every
    # local member.  Keep that narrow exception while still requiring the
    # route marker below before exposing the worker ID.
    if "remote_agent_delegate" in ceiling:
        worker_kinds = sorted(set(worker_kinds) | {"muse"})
    # The runtime registry owns the global Team delegation gate.  Multiple
    # allowed Subagents do not bypass it; ordinary aoi_tools such as
    # delegate_agent_work remain independent capability paths.
    delegation_tool = delegation_enabled
    browser_capable = any(
        subagent_id == "ui_qa_worker"
        or subagent_id.casefold() in {"browser", "browser_agent", "browser_agent_dedicated"}
        or bool(
            {
                "browser",
                "browser_agent",
                "browser_agent_dedicated",
                "browser_qa",
            }
            & {
                _text(capability).casefold()
                for capability in ((subagents.get(subagent_id) or {}).get("capability_ids") or [])
            }
        )
        for subagent_id in allowed
    )
    # Browser QA is parent-issued and therefore may be present on an explicit
    # ui_qa_worker even when that opt-in worker is not a default Team member.
    if browser_capable and ("browser_qa" in ceiling or "browser" in ceiling):
        tool_ids.append("browser")
    if delegation_tool and (allowed & team_members) and effective_capabilities:
        tool_ids.append("delegate")
    # Worker delegation is a route capability.  It remains visible even when
    # the legacy Agent Team delegation toggle was disabled; the revision's
    # explicit worker policy and existing authority guards still constrain it.
    for kind in worker_kinds:
        required = "remote_agent_delegate" if kind == "muse" else "aoi_tools"
        # Keep a typed Muse route marker in the migrated policy even when the
        # legacy ceiling did not include remote_agent_delegate; the runtime
        # capability guard still denies the call until that explicit ceiling
        # is present.  Local worker IDs require the effective aoi_tools
        # intersection before they are persisted.
        if kind == "muse" or required in effective_capabilities:
            tool_ids.append(f"delegate_worker:{kind}")
    tool_ids = sorted(dict.fromkeys(tool_ids))
    delegation_effective = bool(
        delegation_tool and (allowed & team_members) and effective_capabilities
    )
    policy_workers = sorted(
        tool_id.split(":", 1)[1]
        for tool_id in tool_ids
        if tool_id.startswith("delegate_worker:")
    )
    policy = {
        "agents": "any_owned" if delegation_effective else [],
        "workers": policy_workers,
    }
    return tool_ids, policy


@dataclass(frozen=True)
class AgentRevisionConversion:
    """Deterministic P2 conversion result for one legacy revision."""

    agent_id: str
    source_revision_id: str
    source_version: int
    model_overrides: dict[str, dict[str, Any]]
    tool_ids: list[str]
    delegate_policy: dict[str, Any]
    before_routes: dict[str, dict[str, Any]]
    after_routes: dict[str, dict[str, Any]]
    route_matches: dict[str, bool]
    candidate_routes: dict[str, dict[str, Any]] = field(default_factory=dict)
    collisions: list[dict[str, Any]] = field(default_factory=list)
    unmapped_roles: list[dict[str, Any]] = field(default_factory=list)
    unresolved: list[dict[str, Any]] = field(default_factory=list)
    resolutions_applied: list[str] = field(default_factory=list)
    resolved_collisions: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.collisions and not self.unresolved

    @property
    def blocked(self) -> bool:
        return not self.ok

    def patch(self) -> dict[str, Any]:
        return {
            "model_overrides": copy.deepcopy(self.model_overrides),
            "tool_ids": list(self.tool_ids),
            "delegate_policy": copy.deepcopy(self.delegate_policy),
        }

    def to_dict(self) -> dict[str, Any]:
        result = {
            "agent_id": self.agent_id,
            "source_revision_id": self.source_revision_id,
            "source_version": self.source_version,
            **self.patch(),
            "before_routes": copy.deepcopy(self.before_routes),
            "candidate_routes": copy.deepcopy(self.candidate_routes),
            "after_routes": copy.deepcopy(self.after_routes),
            "route_matches": dict(self.route_matches),
            "collisions": copy.deepcopy(self.collisions),
            "unmapped_roles": copy.deepcopy(self.unmapped_roles),
            "unresolved": copy.deepcopy(self.unresolved),
            "resolutions_applied": list(self.resolutions_applied),
            "resolved_collisions": copy.deepcopy(self.resolved_collisions),
            "notes": list(self.notes),
            "ok": self.ok,
        }
        return result


def convert_legacy_revision(
    revision: Any,
    config: Any,
    *,
    model_roles: Any = None,
    route_resolver: Callable[..., Any] | None = None,
    role_map: Mapping[str, str] | None = None,
    resolutions: Mapping[str, Any] | None = None,
) -> AgentRevisionConversion:
    """Build a pure conversion plan for one latest non-retired revision.

    ``route_resolver`` is injectable for unit tests and for a caller that has
    already pinned a historical resolver.  The default calls the current
    exact-role resolver, which is the required source of truth for the dry
    run.
    """

    mapping = dict(role_map or LEGACY_ROLE_TO_V2_ROLE)
    agent_id = _text(_read(revision, "agent_id"))
    revision_id = _text(_read(revision, "id"))
    try:
        version = int(_read(revision, "version", 0) or 0)
    except (TypeError, ValueError):
        version = 0
    if not agent_id:
        raise AgentRevisionMigrationError("revision has no agent_id")
    if version <= 0:
        raise AgentRevisionMigrationError("revision has invalid version")

    allowed = _allowed_ids(revision)
    members = _known_team_members(config, revision)
    subagents = _runtime_subagents(config)
    roles = _runtime_roles()
    explicit_roles = _explicit_legacy_roles(revision)
    source_roles = list(roles)
    for main_role in ("director", "coordinator"):
        if main_role not in source_roles:
            source_roles.append(main_role)
    for role in explicit_roles:
        if role not in source_roles:
            source_roles.append(role)

    unmapped: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    before_routes: dict[str, dict[str, Any]] = {}
    candidate_routes: dict[str, dict[str, Any]] = {}
    source_meta: dict[str, dict[str, Any]] = {}
    worker_route_markers: dict[str, dict[str, Any]] = {}
    for allowed_id in allowed:
        if allowed_id not in subagents:
            unmapped.append(
                {
                    "subagent_id": allowed_id,
                    "reason": "unknown_legacy_subagent",
                }
            )
            continue
        try:
            from .agent_revision_v2 import LEGACY_SUBAGENT_TO_MODEL_ROLE

            if allowed_id not in LEGACY_SUBAGENT_TO_MODEL_ROLE:
                unmapped.append(
                    {
                        "subagent_id": allowed_id,
                        "reason": "legacy_subagent_has_no_v2_role_mapping",
                        "target_role": _closest_v2_role(allowed_id),
                    }
                )
        except Exception:
            pass

    def resolve_candidate(source_role: str, subagent_id: str) -> Any:
        if route_resolver is None:
            return _default_subagent_route_resolver(
                config, revision, source_role, subagent_id
            )
        try:
            # Focused callers can inspect every allowed Subagent by accepting
            # the fourth argument.  The three-argument compatibility shape is
            # retained for existing pure tests/callers.
            return route_resolver(config, revision, source_role, subagent_id)  # type: ignore[misc]
        except TypeError:
            return route_resolver(config, revision, source_role)

    for source_role in source_roles:
        target_role = mapping.get(source_role)
        if not target_role:
            target_role = _closest_v2_role(source_role)
            unmapped.append(
                {
                    "role": source_role,
                    "target_role": target_role,
                    "reason": "unknown_legacy_role_closest_v2_role",
                }
            )
            if source_role in explicit_roles:
                unresolved.append(
                    {
                        "role": source_role,
                        "target_role": target_role,
                        "reason": "unknown_legacy_role_requires_operator_review",
                    }
                )
        if source_role in {"director", "coordinator", "team_main", "main"}:
            try:
                resolved = _resolve_main_legacy_route(config, revision)
                route = normalize_route(config, revision, resolved, subagent_id="")
            except Exception as exc:
                unresolved.append(
                    {
                        "role": source_role,
                        "reason": "main_route_unresolved",
                        "code": exc.__class__.__name__,
                    }
                )
                continue
            before_routes[source_role] = route
            candidate_key = f"{source_role}:main"
            candidate_routes[candidate_key] = route
            source_meta[candidate_key] = {
                "source_role": source_role,
                "target_role": target_role,
                "acting_subagent_id": "",
                "provider": _text(_read(resolved, "provider")).lower(),
            }
            continue
        candidates = tuple(roles.get(source_role, ()))
        usable = [
            candidate
            for candidate in candidates
            if candidate in allowed and (not members or candidate in members)
        ]
        if not usable:
            if source_role in explicit_roles:
                unresolved.append({"role": source_role, "reason": "no_allowed_subagent"})
            unmapped.append(
                {
                    "role": source_role,
                    "target_role": target_role,
                    "reason": "no_allowed_runtime_candidate",
                }
            )
            continue
        source_candidates: list[tuple[str, dict[str, Any]]] = []
        for candidate in usable:
            resolved = None
            try:
                resolved = resolve_candidate(source_role, candidate)
                provider = _text(_read(resolved, "provider")).lower()
                worker_route_markers[f"{source_role}:{candidate}"] = {
                    "provider": provider,
                    "target_role": target_role,
                }
                acting_id = _text(_read(resolved, "acting_subagent_id")) or candidate
                raw_spec = _route_spec_for(config, revision, acting_id)
                route = normalize_route(
                    config,
                    revision,
                    resolved,
                    subagent_id=acting_id,
                    raw_spec=raw_spec,
                )
            except Exception as exc:
                provider = _text(_read(resolved, "provider")).lower()
                if provider:
                    worker_route_markers[f"{source_role}:{candidate}"] = {
                        "provider": provider,
                        "target_role": target_role,
                    }
                unresolved.append(
                    {
                        "role": source_role,
                        "subagent_id": candidate,
                        "reason": "route_unresolved",
                        "code": exc.__class__.__name__,
                    }
                )
                continue
            source_candidates.append((candidate, route))
        if not source_candidates:
            continue
        # Keep the old work-role projection stable for reports, while retaining
        # every allowed Subagent route in candidate_routes for collision checks.
        before_routes[source_role] = copy.deepcopy(source_candidates[0][1])
        for candidate, route in source_candidates:
            candidate_key = f"{source_role}:{candidate}"
            candidate_routes[candidate_key] = route
            source_meta[candidate_key] = {
                "source_role": source_role,
                "target_role": target_role,
                "acting_subagent_id": candidate,
                "provider": _text(route.get("provider")).lower(),
            }

    grouped: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for candidate_key, route in candidate_routes.items():
        grouped.setdefault(source_meta[candidate_key]["target_role"], []).append(
            (candidate_key, route)
        )

    global_routes = model_roles_table(config, model_roles)
    model_overrides: dict[str, dict[str, Any]] = {}
    after_routes: dict[str, dict[str, Any]] = {}
    route_matches: dict[str, bool] = {}
    collisions: list[dict[str, Any]] = []
    resolutions_applied: list[str] = []
    resolved_collisions: list[dict[str, Any]] = []

    resolution_block: Mapping[str, Any] = {}
    if isinstance(resolutions, Mapping):
        # The script resolves by Agent id first and may additionally pass a
        # slug key.  A direct role map is accepted for pure callers that have
        # already selected one Agent.
        candidate = resolutions.get(agent_id)
        if not isinstance(candidate, Mapping):
            candidate = resolutions.get("roles")
        if not isinstance(candidate, Mapping) and any(
            key in resolutions for key in V2_ROLES
        ):
            candidate = resolutions
        if isinstance(candidate, Mapping):
            resolution_block = candidate

    for target_role, values in sorted(grouped.items()):
        signatures = {_route_signature(route) for _, route in values}
        resolved_raw = resolution_block.get(target_role)
        resolved_route = _normalize_global_route(config, resolved_raw)
        if resolved_route is not None:
            # A human supplied role resolution is an explicit acceptance of
            # the route chosen for the merged role.  It is recorded in the
            # report and compared against the new role, while the old per
            # work-role disagreement remains visible in ``before_routes``.
            route = resolved_route
            resolutions_applied.append(target_role)
            after_routes[target_role] = copy.deepcopy(route)
            override = {"routes": [copy.deepcopy(route)]}
            allow_paid = _role_allow_paid_fallback(config, target_role)
            if allow_paid is not None:
                override["allow_paid_fallback"] = allow_paid
            model_overrides[target_role] = override
            if len(signatures) > 1:
                resolved_collisions.append(
                    {
                        "role": target_role,
                        "legacy_roles": [source for source, _ in values],
                        "chosen_route": copy.deepcopy(route),
                        "reason": "resolved_by_operator",
                    }
                )
            for source_role, legacy_route in values:
                route_matches[source_role] = _route_signature(legacy_route) == _route_signature(route)
            continue
        if len(signatures) > 1:
            collisions.append(
                {
                    "role": target_role,
                    "legacy_roles": [source for source, _ in values],
                    "routes": [copy.deepcopy(route) for _, route in values],
                    "reason": "legacy_roles_resolve_to_different_routes",
                }
            )
            after_routes[target_role] = {"status": "collision", "routes": [copy.deepcopy(route) for _, route in values]}
            for source_role, _route in values:
                route_matches[source_role] = False
            continue
        route = copy.deepcopy(values[0][1])
        global_route = global_routes.get(target_role)
        matched = global_route is not None and _route_signature(route) == _route_signature(global_route)
        after_routes[target_role] = copy.deepcopy(global_route if matched else route)
        chosen_route = after_routes[target_role]
        route_matches.update(
            {
                source_role: _route_signature(route_value) == _route_signature(chosen_route)
                for source_role, route_value in values
            }
        )
        if not matched:
            override: dict[str, Any] = {"routes": [route]}
            allow_paid = _role_allow_paid_fallback(config, target_role)
            if allow_paid is not None:
                override["allow_paid_fallback"] = allow_paid
            model_overrides[target_role] = override

    tool_ids, delegate_policy = _derive_tools(
        config,
        revision,
        before_routes,
        extra_routes=worker_route_markers,
    )
    profile_id = _text(
        _read(revision, "execution_profile_id", _read(revision, "execution_profile", ""))
    )
    if profile_id == "manual":
        # Manual is explicit Main inheritance.  P2 stores no per-Agent model
        # override for this profile; the v2 resolver reads the P1 role table.
        model_overrides = {}
    resolved_collision_roles = {
        item.get("role") for item in resolved_collisions
    }
    for target_role in sorted(after_routes):
        if after_routes[target_role].get("status") == "collision":
            continue
        actual_route, error_code = _resolve_v2_route_projection(
            config,
            revision,
            target_role,
            model_overrides,
            after_routes[target_role],
        )
        if error_code:
            unresolved.append(
                {
                    "role": target_role,
                    "reason": "v2_route_unresolved",
                    "code": error_code,
                }
            )
            continue
        after_routes[target_role] = actual_route or after_routes[target_role]
        for candidate_key, legacy_route in grouped.get(target_role, []):
            matches = _route_signature(legacy_route) == _route_signature(after_routes[target_role])
            route_matches[candidate_key] = matches
            if not matches and target_role not in resolved_collision_roles:
                unresolved.append(
                    {
                        "role": target_role,
                        "subagent_id": candidate_key,
                        "reason": "before_after_route_mismatch",
                    }
                )
    notes: list[str] = []
    if not explicit_roles:
        notes.append("runtime work roles were derived from OPERATIONS_WORK_ROLES")
    if members and not members.issuperset(allowed):
        notes.append("allowed_subagent_ids contains ids outside the selected Team")
    if not model_roles_table(config, model_roles):
        notes.append("model_roles has no usable route; every resolved role is represented as an Agent override")
    for source_role in before_routes:
        values = [
            match
            for key, match in route_matches.items()
            if key == source_role or key.startswith(f"{source_role}:")
        ]
        if values:
            route_matches[source_role] = all(values)
    return AgentRevisionConversion(
        agent_id=agent_id,
        source_revision_id=revision_id,
        source_version=version,
        model_overrides=model_overrides,
        tool_ids=tool_ids,
        delegate_policy=delegate_policy,
        before_routes=before_routes,
        after_routes=after_routes,
        route_matches=route_matches,
        candidate_routes=candidate_routes,
        collisions=collisions,
        unmapped_roles=unmapped,
        unresolved=unresolved,
        resolutions_applied=resolutions_applied,
        resolved_collisions=resolved_collisions,
        notes=notes,
    )


# Friendly aliases used by migration callers and focused tests.
build_agent_revision_conversion = convert_legacy_revision
plan_agent_revision_migration = convert_legacy_revision
normalize_free_team_route = normalize_route


def canonical_revision_hash_material(revision: Any, patch: Mapping[str, Any]) -> dict[str, Any]:
    """Return the content mapping used by the immutable revision hash.

    The DB service remains the final authority for hashing.  This helper is
    useful to callers that need to precompute an idempotency key without
    serializing SQLAlchemy objects.
    """

    def value(name: str, default: Any = None) -> Any:
        return _read(revision, name, default)

    return {
        "agent_id": _text(value("agent_id")),
        "display_name": _text(value("display_name")),
        "mission": _text(value("mission")),
        "responsibility_summary": _text(value("responsibility_summary")),
        "operational_instructions": _text(value("operational_instructions")),
        "agent_team_id": _text(value("agent_team_id", value("team_id"))),
        "execution_profile_id": _text(value("execution_profile_id", value("execution_profile"))),
        "allowed_subagent_ids": list(value("allowed_subagent_ids", value("allowed_subagents", [])) or []),
        "capability_ceiling": list(value("capability_ceiling_json", value("capability_ceiling", [])) or []),
        "wake_policy": copy.deepcopy(value("wake_policy_json", value("wake_policy", {})) or {}),
        "budget_policy": copy.deepcopy(value("budget_policy_json", value("budget_policy", {})) or {}),
        "concurrency_policy": copy.deepcopy(value("concurrency_policy_json", value("concurrency_policy", {})) or {}),
        "model_overrides": copy.deepcopy(patch.get("model_overrides") or {}),
        "tool_ids": list(patch.get("tool_ids") or []),
        "delegate_policy": copy.deepcopy(patch.get("delegate_policy") or {}),
    }


def content_hash_for_patch(revision: Any, patch: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(canonical_revision_hash_material(revision, patch)).encode("utf-8")).hexdigest()


def detect_route_collisions(
    routes: Mapping[str, Mapping[str, Any]],
    role_map: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Detect old roles that collapse to different routes under one v2 role."""

    mapping = role_map or LEGACY_ROLE_TO_V2_ROLE
    grouped: dict[str, list[tuple[str, Mapping[str, Any]]]] = {}
    for source_role, route in routes.items():
        target = mapping.get(source_role)
        if target:
            grouped.setdefault(target, []).append((source_role, route))
    collisions: list[dict[str, Any]] = []
    for target, items in sorted(grouped.items()):
        if len({_route_signature(route) for _, route in items}) > 1:
            collisions.append(
                {
                    "role": target,
                    "legacy_roles": [source for source, _ in items],
                    "routes": [copy.deepcopy(dict(route)) for _, route in items],
                    "reason": "legacy_roles_resolve_to_different_routes",
                }
            )
    return collisions


def resolve_legacy_work_role_routes(
    revision: Any,
    config: Any,
    *,
    route_resolver: Callable[..., Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Return the redacted effective route for every resolvable old role."""

    report = convert_legacy_revision(revision, config, route_resolver=route_resolver)
    return copy.deepcopy(report.before_routes)


def convert_legacy_team_profile_subagents(
    revision: Any,
    config: Any,
    *,
    model_roles: Any = None,
    route_resolver: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Compatibility facade returning the JSON patch plus diagnostics."""

    result = convert_legacy_revision(
        revision,
        config,
        model_roles=model_roles,
        route_resolver=route_resolver,
    )
    return result.to_dict()


__all__ = [
    "AgentRevisionConversion",
    "AgentRevisionMigrationError",
    "LEGACY_ROLE_TO_V2_ROLE",
    "V2_ROLES",
    "WORKER_KINDS",
    "build_agent_revision_conversion",
    "canonical_revision_hash_material",
    "content_hash_for_patch",
    "convert_legacy_revision",
    "convert_legacy_team_profile_subagents",
    "detect_route_collisions",
    "model_roles_table",
    "normalize_free_team_route",
    "normalize_route",
    "plan_agent_revision_migration",
    "resolve_legacy_work_role_routes",
]
