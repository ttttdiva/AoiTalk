"""One-way migration boundary for legacy Agent Team schema input.

This module is intentionally the only place that understands v2 Template,
Member, Model Group, exposure, and legacy role-route fields.  It is imported
by App Config load/save before canonical schema-v3 normalization.  No function
here is used by normal runtime or API rendering.
"""

from __future__ import annotations

import copy
from typing import Any

from . import agent_team_service as _runtime_facade  # noqa: F401
from .agent_team_v3 import (
    AGENT_TEAM_DEFAULT_LLM_PROFILES,
    AGENT_TEAM_DEFAULT_TEAMS,
    AGENT_TEAM_SUBAGENT_CATALOG,
    _id,
    _profile,
    _subagent_normalized,
    _team,
    normalize_agent_team_v3,
    agent_team_v3_subagents,
    agent_team_v3_teams,
    resolve_agent_team_v3_route,
)

# Legacy reserved IDs are migration policy, not canonical runtime security.
AGENT_TEAM_RESERVED_MEMBER_IDS = frozenset({
    "agent_harness",
    "advanced_reasoning",
    "advanced_reasoning_assistant",
    "utility",
    "media",
    "media_operator",
    "spotify",
    "spotify_assistant",
})
AGENT_TEAM_RESERVED_TEMPLATE_IDS = frozenset(
    set(AGENT_TEAM_SUBAGENT_CATALOG) | set(AGENT_TEAM_RESERVED_MEMBER_IDS)
)
AGENT_TEAM_CUSTOM_TEMPLATE_KEYS = frozenset()
AGENT_TEAM_TEMPLATE_CATALOG = {
    key: {
        "template_id": key,
        "display_name": value["name"],
        "instructions": value["instructions"],
        "capability_ids": list(value["capability_ids"]),
        "scalable": value["scalable"],
        "default_instances": value["default_instances"],
        "max_instances": value["max_instances"],
        "max_workspace_access": value["max_workspace_access"],
        "allow_cli_native_tools": value["allow_cli_native_tools"],
        "built_in": True,
    }
    for key, value in AGENT_TEAM_SUBAGENT_CATALOG.items()
}
AGENT_TEAM_V2_MEMBER_KEYS = set(AGENT_TEAM_SUBAGENT_CATALOG)


def _legacy_team(team_id: str, raw: Any, valid_subagents: set[str] | None = None) -> dict[str, Any]:
    """Translate v2 exposure/activation fields before canonical normalization."""

    payload = copy.deepcopy(raw) if isinstance(raw, dict) else {}
    activation_value = payload.get("activation") if isinstance(payload.get("activation"), dict) else {}
    exposure_value = payload.get("exposure") if isinstance(payload.get("exposure"), dict) else {}
    contexts: list[str] = []
    if activation_value.get("development_status"):
        contexts.append("app_development")
    if activation_value.get("story_modes"):
        contexts.append("story")
    if activation_value.get("trpg") or activation_value.get("trpg_modes"):
        contexts.append("trpg")
    if contexts:
        payload["activation"] = {"mode": "contextual", "contexts": contexts}
    elif "mode" in activation_value:
        payload["activation"] = {
            "mode": "manual" if str(activation_value.get("mode") or "").lower() == "deferred" else activation_value.get("mode"),
            "contexts": activation_value.get("contexts") or [],
        }
    elif "mode" in exposure_value:
        payload["activation"] = {
            "mode": "manual" if str(exposure_value.get("mode") or "").lower() == "deferred" else exposure_value.get("mode"),
            "contexts": exposure_value.get("contexts") or [],
        }
    else:
        payload["activation"] = {"mode": "always", "contexts": []}
    return _team(team_id, payload, valid_subagents)


_LEGACY_TO_SUBAGENT = {
    "architect": "architecture_planner",
    "explorer": "code_explorer",
    "implementer": "code_implementer",
    "reviewer": "code_reviewer",
    "writing": "story_writer",
    "story_writer": "story_writer",
    "story_consistency_reviewer": "story_consistency_reviewer",
    "character_voice_reviewer": "character_voice_reviewer",
    "import": "story_import",
    "story_import": "story_import",
}
_DEFAULT_TEAM_BY_SUBAGENT = {**{key: "general" for key in ("general_worker", "general_researcher", "docs_operator", "project_operator", "workspace_operator")}, **{key: "app_development" for key in ("code_explorer", "architecture_planner", "code_implementer", "code_reviewer")}, **{key: "story" for key in ("story_writer", "story_consistency_reviewer", "character_voice_reviewer", "story_import")}}


def _legacy_profile(profile_id: str, route: dict[str, Any], group: dict[str, Any] | None = None) -> dict[str, Any]:
    effective = dict(group) if isinstance(group, dict) else {}
    effective.update(route)
    provider = str(effective.get("provider") or "").strip().lower()
    model = str(effective.get("model") or "").strip()
    effort = str(effective.get("effort") or effective.get("reasoning_effort") or effective.get("mode") or "").strip()
    pool_id = str(effective.get("pool_id") or "").strip()
    routing_profile_id = str(effective.get("routing_profile_id") or "").strip()
    target_type = "static" if provider and model else "pool" if pool_id or routing_profile_id else "inherit"
    return _profile(profile_id, {"name": effective.get("name") or profile_id, "target_type": target_type, "provider": provider, "model": model, "effort_policy": "explicit" if effort else str(effective.get("effort_policy") or "same"), "effort": effort, "pool_id": pool_id, "routing_profile_id": routing_profile_id})


def _is_stock_media_team(team_id: Any, raw: Any) -> bool:
    """Identify only the shipped v2 Media Team, not a user Team named media.

    Stable IDs are user-owned.  Therefore the ID alone is never enough to
    retire a Team during migration; all shipped topology fields must still
    match the old seed fingerprint.
    """
    if str(team_id or "").strip().casefold() != "media" or not isinstance(raw, dict):
        return False
    expected = {
        "team_id": "media",
        "name": "Media",
        "description": "Media and Spotify operations",
        "enabled": True,
        "sort_order": 50,
        "exposure": {"mode": "deferred"},
        "member_template_ids": ["media_operator", "spotify"],
    }
    return raw == expected


def _is_stock_operations_team(team_id: Any, raw: Any) -> bool:
    if str(team_id or "").strip() != "aoitalk_operations" or not isinstance(raw, dict):
        return False
    expected = {
        "team_id": "aoitalk_operations",
        "name": "AoiTalk Operations",
        "description": "Docs and project operations through high-level tools",
        "enabled": True,
        "sort_order": 20,
        "exposure": {"mode": "deferred"},
        "member_template_ids": ["docs_operator", "project_operator", "workspace_operator"],
    }
    return raw == expected


def _migrate_section(old: Any) -> dict[str, Any]:
    old = copy.deepcopy(old) if isinstance(old, dict) else {}
    try:
        schema = int(old.get("schema_version") or 0)
    except (TypeError, ValueError):
        schema = 0
    # A canonical v3 save is normalized without adding defaults, preserving
    # intentional Team/Subagent deletion.
    if schema >= 3:
        # A v3 envelope is authoritative even when a user intentionally
        # removed every Team/Subagent.  Never fall back to legacy catalog
        # defaults for a malformed-but-versioned section.  Keep old
        # llm_profiles / llm_profile_id so normalize can migrate them.
        return {
            "schema_version": 3,
            "delegation_enabled": bool(old.get("delegation_enabled", False)),
            "orchestration_mode": "director" if old.get("orchestration_mode") == "director" else "standard",
            "teams": copy.deepcopy(old.get("teams")) if isinstance(old.get("teams"), dict) else {},
            "subagents": copy.deepcopy(old.get("subagents")) if isinstance(old.get("subagents"), dict) else {},
            "llm_profiles": copy.deepcopy(old.get("llm_profiles")) if isinstance(old.get("llm_profiles"), dict) else {},
        }

    # v2 built-ins and custom template metadata become Subagent fields.  The
    # migration consumes, but never returns, the template/member/model-group
    # containers.
    catalog: dict[str, dict[str, Any]] = copy.deepcopy(AGENT_TEAM_SUBAGENT_CATALOG)
    custom = old.get("custom_templates") if isinstance(old.get("custom_templates"), dict) else {}
    for tid, raw in custom.items():
        tid = str(tid).strip()
        if _id(tid) and tid not in AGENT_TEAM_RESERVED_TEMPLATE_IDS and isinstance(raw, dict):
            catalog[tid] = {**raw, "subagent_id": tid, "name": raw.get("display_name") or raw.get("name") or tid}
    members = old.get("members") if isinstance(old.get("members"), dict) else {}
    model_routing = old.get("model_routing") if isinstance(old.get("model_routing"), dict) else {}
    overrides = model_routing.get("overrides") if isinstance(model_routing.get("overrides"), dict) else {}
    groups = old.get("model_groups") if isinstance(old.get("model_groups"), dict) else {}
    subagents: dict[str, dict[str, Any]] = {}
    profiles: dict[str, dict[str, Any]] = {}
    for key, raw in members.items():
        if not isinstance(raw, dict) or str(key).casefold() in {str(x).casefold() for x in AGENT_TEAM_RESERVED_MEMBER_IDS}:
            continue
        key = str(key)
        template_id = str(raw.get("template_id") or _LEGACY_TO_SUBAGENT.get(key) or key).strip()
        template_id = _LEGACY_TO_SUBAGENT.get(template_id, template_id)
        if template_id in {"media_operator", "spotify", "media"} or key in {"media_operator", "spotify", "media"}:
            # Shared Tool / Integration routes are intentionally not migrated
            # into the Agent Team → Subagent topology.
            continue
        seed = catalog.get(template_id, catalog.get(key, {}))
        sid = _id(raw.get("subagent_id") or raw.get("member_id") or raw.get("id") or template_id)
        if not sid:
            continue
        route = copy.deepcopy(raw.get("route") if isinstance(raw.get("route"), dict) else raw.get("override") if isinstance(raw.get("override"), dict) else {})
        if isinstance(overrides.get(key), dict):
            for rkey, value in overrides[key].items():
                route.setdefault(rkey, copy.deepcopy(value))
        for rkey in ("provider", "model", "effort", "effort_policy", "reasoning_effort"):
            if rkey not in route and raw.get(rkey) not in (None, ""):
                route[rkey] = copy.deepcopy(raw[rkey])
        group_id = str(route.get("group_id") or raw.get("group_id") or "").strip()
        route_kind = str(route.get("kind") or route.get("route_kind") or raw.get("route_kind") or "").strip().lower()
        has_explicit_route = bool(
            group_id
            or route_kind == "individual"
            or any(route.get(key) not in (None, "") for key in ("provider", "model", "effort", "reasoning_effort"))
        )
        # group routes intentionally share one LLM Profile.  An inherit route
        # remains a null binding; only individual/provider routes receive a
        # migration-generated profile.
        profile_id = _id(raw.get("llm_profile_id") or (group_id if group_id else f"legacy_{sid}" if has_explicit_route else ""))
        if profile_id:
            profile_source = groups.get(group_id) if group_id else None
            if profile_source is None and profile_id in AGENT_TEAM_DEFAULT_LLM_PROFILES:
                profile_source = AGENT_TEAM_DEFAULT_LLM_PROFILES[profile_id]
            profiles.setdefault(profile_id, _legacy_profile(profile_id, route, profile_source))
        data = {**seed, **raw, "subagent_id": sid, "name": raw.get("name") or raw.get("display_name") or raw.get("label") or seed.get("name") or sid, "description": raw.get("description") or seed.get("description") or "", "instructions": raw.get("instructions") or seed.get("instructions") or "", "llm_profile_id": profile_id or None}
        normalized = _subagent_normalized(sid, data)
        if profile_id:
            normalized["llm_profile_id"] = profile_id
        subagents[sid] = normalized

    old_teams = old.get("teams") if isinstance(old.get("teams"), dict) else {}
    teams: dict[str, dict[str, Any]] = {}
    for tid, raw in old_teams.items():
        if not isinstance(raw, dict):
            continue
        # Media/Spotify are Shared Integrations, not Agent Team members.  The
        # historical built-in ``media`` Team is therefore retired during the
        # one-way migration; user-created Teams with other IDs remain intact.
        if _is_stock_media_team(tid, raw):
            continue
        # v2 shipped a separate AoiTalk Operations Team.  Its high-level Docs,
        # Project and Workspace operators now belong to General; merge the
        # references without creating a second Team.
        target_tid = "general" if str(tid).strip() == "aoitalk_operations" else str(tid)
        ids = list(raw.get("subagent_ids") or []) if isinstance(raw.get("subagent_ids"), list) else []
        template_ids = raw.get("member_template_ids") if isinstance(raw.get("member_template_ids"), list) else []
        for template_id in template_ids:
            template_id = _LEGACY_TO_SUBAGENT.get(str(template_id).strip(), str(template_id).strip())
            # Media/Spotify are Shared Integrations and must never become
            # Subagents during migration, even when the old Team listed them.
            if template_id in {"media_operator", "spotify"}:
                continue
            if template_id in subagents:
                ids.append(template_id)
            elif template_id in catalog:
                sid = _id(template_id)
                if sid:
                    seed_value = dict(catalog[template_id])
                    # A custom Template with no Member is retained as a
                    # disabled standalone Subagent rather than being silently
                    # dropped or activated by a stock topology reference.
                    if template_id in custom:
                        seed_value["enabled"] = False
                    subagents.setdefault(sid, _subagent_normalized(sid, seed_value))
                    ids.append(sid)
        for sid, member in subagents.items():
            old_member = members.get(sid) if isinstance(members.get(sid), dict) else None
            if old_member and str(old_member.get("team_id") or "") == str(tid):
                ids.append(sid)
        team_payload = {**raw, "team_id": target_tid, "subagent_ids": ids}
        if target_tid == "general" and _is_stock_operations_team(tid, raw):
            # The stock Operations Team is folded into the stock General Team;
            # only its Subagent references survive the migration.
            for general_id in AGENT_TEAM_DEFAULT_TEAMS["general"]["subagent_ids"]:
                subagents.setdefault(
                    general_id,
                    _subagent_normalized(
                        general_id,
                        AGENT_TEAM_SUBAGENT_CATALOG[general_id],
                    ),
                )
            ids = list(
                dict.fromkeys(
                    list(AGENT_TEAM_DEFAULT_TEAMS["general"]["subagent_ids"])
                    + ids
                )
            )
            team_payload["subagent_ids"] = ids
            team_payload.update(
                {
                    "name": AGENT_TEAM_DEFAULT_TEAMS["general"]["name"],
                    "description": AGENT_TEAM_DEFAULT_TEAMS["general"]["description"],
                    "sort_order": AGENT_TEAM_DEFAULT_TEAMS["general"]["sort_order"],
                    "activation": copy.deepcopy(AGENT_TEAM_DEFAULT_TEAMS["general"]["activation"]),
                }
            )
        team = _legacy_team(target_tid, team_payload, set(subagents))
        if target_tid in teams:
            # Preserve both legacy General and AoiTalk Operations references
            # while keeping one canonical Team entry.
            teams[target_tid]["subagent_ids"] = list(dict.fromkeys(
                list(teams[target_tid].get("subagent_ids") or []) + list(team.get("subagent_ids") or [])
            ))
        else:
            teams[target_tid] = team
    # Preserve custom Templates that are not referenced by any v2 Member as
    # disabled Subagents.  Their definition remains available for the user to
    # re-enable/edit, while the v3 config contains no Template catalog.
    referenced_custom = {
        str(raw.get("template_id") or "").strip()
        for raw in members.values()
        if isinstance(raw, dict) and str(raw.get("template_id") or "").strip()
    }
    for custom_id, custom_raw in custom.items():
        clean_custom_id = _id(custom_id)
        if not clean_custom_id or clean_custom_id in subagents or clean_custom_id in referenced_custom:
            continue
        if not isinstance(custom_raw, dict):
            continue
        standalone = dict(custom_raw)
        standalone.update({"subagent_id": clean_custom_id, "enabled": False})
        standalone.setdefault("name", custom_raw.get("display_name") or clean_custom_id)
        subagents[clean_custom_id] = _subagent_normalized(clean_custom_id, standalone)

    if not teams and not old.get("teams"):
        # Legacy role config has no topology.  Bootstrap the v3 defaults once.
        # Leave execution_profiles absent so normalize can migrate custom routes.
        teams = copy.deepcopy(AGENT_TEAM_DEFAULT_TEAMS)
        for team in teams.values():
            team.pop("execution_profiles", None)
        for sid, seed in AGENT_TEAM_SUBAGENT_CATALOG.items():
            subagents.setdefault(sid, _subagent_normalized(sid, seed))
    for sid in list(subagents):
        if not any(sid in team.get("subagent_ids", []) for team in teams.values()):
            tid = _DEFAULT_TEAM_BY_SUBAGENT.get(sid)
            if tid in teams:
                teams[tid]["subagent_ids"].append(sid)
    # Fresh v3 catalog bindings refer to these shared profiles.  Add them only
    # while converting a legacy section; canonical v3 normalization below never
    # resurrects a profile a user intentionally deleted.
    for profile_id in {str(item.get("llm_profile_id") or "") for item in subagents.values()}:
        if profile_id and profile_id in AGENT_TEAM_DEFAULT_LLM_PROFILES:
            profiles.setdefault(profile_id, copy.deepcopy(AGENT_TEAM_DEFAULT_LLM_PROFILES[profile_id]))
    return {"schema_version": 3, "delegation_enabled": bool(old.get("delegation_enabled", old.get("enabled", False))), "orchestration_mode": "director" if old.get("orchestration_mode") == "director" else "standard", "teams": teams, "subagents": subagents, "llm_profiles": profiles}



def migrate_agent_team_config(value: Any) -> dict[str, Any]:
    """Convert a pre-v3 App Config envelope and strip all legacy containers."""

    result = copy.deepcopy(value) if isinstance(value, dict) else {}
    raw = result.get("agent_team") if isinstance(result.get("agent_team"), dict) else {}
    result["agent_team"] = normalize_agent_team_v3(
        _migrate_section(raw),
        global_execution_profiles=result.get("execution_profiles"),
    )
    return result


def migrate_agent_team_v3(value: Any) -> dict[str, Any]:
    return migrate_agent_team_config(value)


def migrate_agent_team_v2(value: Any) -> dict[str, Any]:
    return migrate_agent_team_config(value)


def normalize_agent_team_v2(value: Any, *, migrate_legacy: bool = True) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    if migrate_legacy:
        return _migrate_section(raw)
    # Migration-only compatibility reader; never called by runtime/API.
    return _migrate_section(raw)


def agent_team_builtin_templates() -> dict[str, dict[str, Any]]:
    return copy.deepcopy(AGENT_TEAM_SUBAGENT_CATALOG)


def validate_agent_team_v2_custom_templates(value: Any) -> dict[str, dict[str, Any]]:
    if value in (None, {}):
        return {}
    raise ValueError("custom_templates is not supported by Agent Team schema v3")


def agent_team_v2_teams(config: Any) -> list[dict[str, Any]]:
    result = []
    for team in agent_team_v3_teams(config):
        item = copy.deepcopy(team)
        mode = str((item.get("activation") or {}).get("mode") or "always")
        item["exposure"] = {"mode": "deferred" if mode == "manual" else mode}
        result.append(item)
    return result


def agent_team_v2_members(config: Any, *, team_id: str | None = None, include_disabled: bool = True) -> list[dict[str, Any]]:
    result = []
    for subagent in agent_team_v3_subagents(config, include_disabled=include_disabled):
        team_ids = list(subagent.get("team_ids") or [])
        if team_id and str(team_id) not in team_ids:
            continue
        item = copy.deepcopy(subagent)
        item.update({"member_id": item["subagent_id"], "team_id": team_ids[0] if team_ids else "", "template_id": item["subagent_id"], "display_name": item["name"], "label": item["name"], "tools": list(item.get("capability_ids") or [])})
        item["template"] = copy.deepcopy(item)
        result.append(item)
    return result


def agent_team_v2_member(config: Any, member_id: str) -> dict[str, Any] | None:
    clean = str(member_id or "").strip()
    canonical = _LEGACY_TO_SUBAGENT.get(clean, clean)
    item = next((item for item in agent_team_v2_members(config) if item.get("member_id") == canonical), None)
    if item is None:
        return None
    if canonical != clean:
        # Compatibility projection for legacy runtime callers.  The persisted
        # graph still contains only the canonical Subagent ID.
        item = copy.deepcopy(item)
        item["canonical_subagent_id"] = canonical
        item["member_id"] = clean
        item["template_id"] = canonical
    return item


def resolve_agent_team_v2_route(config: Any, member_id: str, *, main_route: dict[str, Any] | None = None) -> dict[str, Any] | None:
    clean = str(member_id or "").strip()
    canonical = _LEGACY_TO_SUBAGENT.get(clean, clean)
    result = resolve_agent_team_v3_route(config, canonical, main_route=main_route)
    if result is not None and canonical != clean:
        result = dict(result)
        result["member_id"] = clean
        result["template_id"] = canonical
    return result


