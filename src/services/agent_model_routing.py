"""Canonical model routing for Operations Agent work.

The single source of truth for which provider/model/effort an Operations
Agent uses is::

    Operations Agent
      -> exact AgentRevision (pinned by AgentWorkItem / AgentRun)
      -> Agent Team (agent_team_id)
      -> Team Execution Profile (execution_profile_id)
      -> Subagent (acting_subagent_id; per-Subagent override > Default Route)
      -> provider / model / effort

Domain services (CrowdWorks, Web制作, アプリ開発, 調査 ...) never call
``create_llm_client(config)`` for Agent work and never copy this resolution.
They ask for a *work role* (planner / worker / reviewer ...) and receive an
``AgentModelRoute`` for a Subagent that is a member of the pinned Team and
explicitly allowed by the pinned revision.

Resolution is fail-closed: a missing/disabled Team, Profile or Subagent
raises ``AgentModelRouteError`` instead of falling back to the global Base
Model.  The ``manual`` system profile ("標準モデル") is an explicit user choice
of the configured Main model and is reported as such in provenance.

This module is pure configuration logic plus one exact-revision DB lookup; it
never creates provider clients, reads credentials or persists anything.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from .agent_team_v3 import (
    AGENT_TEAM_DEFAULT_LLM_PROFILES,
    AGENT_TEAM_DEFAULT_TEAMS,
    AGENT_TEAM_SUBAGENT_CATALOG,
    _apply_execution_route,
    agent_team_v3_subagents,
    agent_team_v3_teams,
)
from .execution_profile_service import (
    FREE_TEAM_PROFILE_ID,
    MANUAL_EXECUTION_PROFILE_ID,
    list_team_execution_profiles,
    resolve_execution_main_route,
)
from .session_llm_runtime_context import (
    bind_session_main_route_override,
    reset_session_main_route_override,
)

# Providers that ``llm.manager.create_llm_client_for_target`` can construct.
# Kept local so route resolution never imports provider SDKs.
ROUTABLE_PROVIDERS = frozenset(
    {
        "openai",
        "gemini",
        "openrouter",
        "deepseek",
        "deepinfra",
        "kimi",
        "sglang",
        "ollama",
        "openai_compatible_local",
        "routing-profile",
        "antigravity-cli",
        "claude-cli",
        "codex-cli",
        "grok-cli",
    }
)

# Work roles shared by every Operations domain.  Each role lists existing
# Agent Team Subagents in preference order; the first one that is both a member
# of the pinned Team and allowed by the pinned revision acts for the role.
# ``employee_operator`` closes every chain so a minimal Employee Team (one
# operator) keeps working with a single explicit route.  A Team whose allowed
# members match none of the candidates fails closed for that role.
#
# "Thinking" roles (planner / reviewer) prefer the Subagents users typically
# route to a strong model; "doing" roles prefer implementer/worker Subagents
# that can be routed to a local/OSS model via a per-Subagent override.
OPERATIONS_WORK_ROLES: dict[str, tuple[str, ...]] = {
    "planner": ("architecture_planner", "general_researcher", "general_worker", "employee_operator"),
    "explorer": ("code_explorer", "general_researcher", "general_worker", "employee_operator"),
    "researcher": ("general_researcher", "code_explorer", "general_worker", "employee_operator"),
    "writer": ("general_worker", "employee_operator"),
    "data_worker": ("general_worker", "workspace_operator", "employee_operator"),
    "code_worker": ("code_implementer", "workspace_operator", "general_worker", "employee_operator"),
    "general_worker": ("general_worker", "employee_operator"),
    "reviewer": ("code_reviewer", "general_researcher", "architecture_planner", "employee_operator"),
    # ``docs_operator`` is a text-capable Operations actor as well.  Keeping
    # it at the end preserves the normal operator preference while allowing a
    # Docs-only AgentRevision to receive a frozen Chat route.
    # ``workspace_operator`` follows for the same reason: a revision scoped
    # only to Workspace operation (for example a dedicated local-agent task
    # Agent) still has an operator.  Authority remains the revision ceiling.
    # App Development can use its already-allowed implementer as the doing actor.
    # Existing operator preference and revision authority remain unchanged.
    "operator": ("employee_operator", "general_worker", "docs_operator", "workspace_operator", "code_implementer"),
}

ROUTE_SOURCE_LABELS = {
    "subagent_override": "Subagent Override",
    "profile_default": "Default Subagent Route",
    "standard_model": "標準モデル（Base Model）",
    "free_team": "Free Team",
    "legacy_profile": "旧Profile（Base Model継承）",
    "legacy_policy_override": "旧Policy指定（非推奨）",
}


class AgentModelRouteError(RuntimeError):
    """Safe routing failure; ``code`` never contains config values or secrets."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class AgentRoutingPins:
    """Identity/topology pins copied from one exact AgentRevision."""

    agent_id: str | None
    agent_revision_id: str | None
    agent_team_id: str
    execution_profile_id: str
    allowed_subagent_ids: tuple[str, ...] = ()
    # A durable AgentRun may carry the route snapshot captured at WorkItem
    # start.  Once present, resolution must use it instead of App Config.
    route_snapshot: Mapping[str, Any] | None = None
    # Server-owned explicit ExecutionProfile selection (safe selection
    # record).  ``None`` means the revision default; then
    # ``execution_profile_id`` is the revision's own value.
    execution_selection: Mapping[str, Any] | None = None

    def with_execution_selection(
        self, selection: "AgentExecutionSelection"
    ) -> "AgentRoutingPins":
        """Apply a validated selection without touching role identity."""

        if self.execution_selection is not None or (
            selection.default_execution_profile_id != self.execution_profile_id
        ):
            raise AgentModelRouteError("agent_route_selection_invalid")
        if not selection.is_explicit:
            return self
        return replace(
            self,
            execution_profile_id=selection.selected_execution_profile_id,
            execution_selection=selection.to_dict(),
        )

    @classmethod
    def from_revision(cls, revision: Any) -> "AgentRoutingPins":
        def read(name: str) -> Any:
            if isinstance(revision, Mapping):
                return revision.get(name)
            return getattr(revision, name, None)

        allowed = read("allowed_subagent_ids") or []
        return cls(
            agent_id=str(read("agent_id")) if read("agent_id") else None,
            agent_revision_id=str(read("id")) if read("id") else None,
            agent_team_id=str(read("agent_team_id") or "").strip(),
            execution_profile_id=str(read("execution_profile_id") or "").strip(),
            allowed_subagent_ids=tuple(
                dict.fromkeys(str(item).strip() for item in allowed if str(item).strip())
            ),
        )


@dataclass(frozen=True)
class AgentModelRoute:
    """Resolved route for one Subagent acting for an Operations Agent."""

    agent_team_id: str
    execution_profile_id: str
    acting_subagent_id: str
    provider: str
    model: str
    effort: str = ""
    route_source: str = "profile_default"
    inherits_main: bool = False
    backend: str = "api"
    role: str | None = None
    agent_id: str | None = None
    agent_revision_id: str | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    def provenance(self) -> dict[str, Any]:
        """Safe audit projection: identifiers and model names only.

        Never includes credentials, base URLs, filesystem paths, prompts or
        reasoning.  The values are all configuration identifiers.
        """

        result: dict[str, Any] = {
            "agent_id": self.agent_id,
            "agent_revision_id": self.agent_revision_id,
            "agent_team_id": self.agent_team_id,
            "execution_profile_id": self.execution_profile_id,
            "acting_subagent_id": self.acting_subagent_id,
            "role": self.role,
            "provider": self.provider,
            "model": self.model,
            "effort": self.effort or None,
            "route_source": self.route_source,
            "inherits_main": self.inherits_main,
            "backend": self.backend,
        }
        if self.notes:
            result["notes"] = list(self.notes)
        return result

    def with_role(self, role: str | None) -> "AgentModelRoute":
        return AgentModelRoute(**{**self.__dict__, "role": role})


def _clean(value: Any) -> str:
    return str(value or "").strip()


def known_agent_teams(config: Any) -> dict[str, dict[str, Any]]:
    """Code-owned default Teams overlaid by the saved canonical topology."""

    teams = {key: copy.deepcopy(value) for key, value in AGENT_TEAM_DEFAULT_TEAMS.items()}
    try:
        for item in agent_team_v3_teams(config):
            if isinstance(item, Mapping) and _clean(item.get("team_id")):
                teams[_clean(item["team_id"])] = dict(item)
    except Exception:
        # A malformed optional config must never broaden membership; the
        # code-owned defaults remain the only known Teams.
        pass
    return teams


def known_agent_subagents(config: Any) -> dict[str, dict[str, Any]]:
    """Code catalog overlaid by saved Subagents (saved disabled state wins)."""

    subagents = {key: copy.deepcopy(value) for key, value in AGENT_TEAM_SUBAGENT_CATALOG.items()}
    try:
        for item in agent_team_v3_subagents(config, include_disabled=True):
            if isinstance(item, Mapping) and _clean(item.get("subagent_id")):
                subagents[_clean(item["subagent_id"])] = dict(item)
    except Exception:
        pass
    return subagents


def _require_pins(pins: AgentRoutingPins) -> None:
    for value in (pins.agent_team_id, pins.execution_profile_id):
        if not value or len(value) > 128 or value != value.strip():
            raise AgentModelRouteError("agent_route_pins_invalid")


def _team_for(config: Any, pins: AgentRoutingPins) -> dict[str, Any]:
    team = known_agent_teams(config).get(pins.agent_team_id)
    if not team or team.get("enabled", True) is False:
        raise AgentModelRouteError("agent_route_team_unavailable")
    return team


def _main_route(
    config: Any,
    main_route_override: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    # Operations work must not inherit an ambient Chat session's Main model
    # override (the request-scoped model picker of a conversation).  Only an
    # explicit override handed in by the caller applies: a conversation's own
    # persisted Main route, resolved when its Agent binding is snapshotted.
    token = bind_session_main_route_override(main_route_override)
    try:
        return dict(resolve_execution_main_route(config))
    finally:
        reset_session_main_route_override(token)


def _route_spec(
    config: Any,
    pins: AgentRoutingPins,
    subagent_id: str,
) -> tuple[dict[str, Any] | None, str]:
    """Return (execution route spec, route_source); None spec means Main."""

    profile_id = pins.execution_profile_id
    if profile_id == MANUAL_EXECUTION_PROFILE_ID:
        return None, "standard_model"
    if profile_id == FREE_TEAM_PROFILE_ID:
        return (
            {"inherit_model": False, "provider": "routing-profile", "model": "free-team",
             "effort_policy": "default", "effort": ""},
            "free_team",
        )
    profile = next(
        (
            item
            for item in list_team_execution_profiles(config, pins.agent_team_id)
            if _clean(item.get("profile_id")) == profile_id
        ),
        None,
    )
    if profile is None:
        legacy = AGENT_TEAM_DEFAULT_LLM_PROFILES.get(profile_id)
        if legacy is None:
            raise AgentModelRouteError("agent_route_profile_unavailable")
        # Pre-Team-profile revisions (heavy/light/coding) were all Main-
        # inheriting LLM Profiles; keep their effort policy only.
        return (
            {"inherit_model": True, "effort_policy": legacy.get("effort_policy") or "same"},
            "legacy_profile",
        )
    if profile.get("enabled", True) is False:
        raise AgentModelRouteError("agent_route_profile_unavailable")
    overrides = profile.get("overrides") if isinstance(profile.get("overrides"), Mapping) else {}
    if subagent_id in overrides:
        return dict(overrides[subagent_id] or {}), "subagent_override"
    default_route = profile.get("default_route")
    return (dict(default_route) if isinstance(default_route, Mapping) else None), "profile_default"


def _route_spec_is_well_formed(spec: Mapping[str, Any]) -> bool:
    """Reject malformed persisted explicit routes before inherit normalization."""

    inherit_model = spec.get("inherit_model")
    if inherit_model is not None and not isinstance(inherit_model, bool):
        return False
    for key in ("provider", "model"):
        value = spec.get(key)
        if value not in (None, "") and not isinstance(value, str):
            return False
    provider = _clean(spec.get("provider")).lower()
    model = _clean(spec.get("model"))
    # An explicit route must not silently collapse to Main when corruption
    # removes either half of its provider/model pair.  Inherit routes may omit
    # both values, as intended by the manual and legacy compatibility paths.
    if inherit_model is False and (not provider or not model):
        return False
    if inherit_model is not True and bool(provider) != bool(model):
        return False
    return True


def _raw_persisted_route_spec(
    config: Any,
    pins: AgentRoutingPins,
    subagent_id: str,
) -> tuple[Any, bool]:
    """Read the unnormalized persisted route for corruption checks only."""

    if isinstance(config, Mapping):
        section = config.get("agent_team", {})
    else:
        getter = getattr(config, "get", None)
        section = getter("agent_team", {}) if callable(getter) else {}
    if not isinstance(section, Mapping):
        return None, False
    teams = section.get("teams")
    team = teams.get(pins.agent_team_id) if isinstance(teams, Mapping) else None
    profile, present = _raw_team_profile(config, pins.agent_team_id, pins.execution_profile_id)
    if not present:
        return None, False
    if not isinstance(profile, Mapping) or not _raw_profile_is_well_formed(profile):
        # Non-mapping marker: the caller rejects it instead of normalizing.
        return "corrupt-profile", True
    overrides = profile.get("overrides")
    if isinstance(overrides, Mapping) and subagent_id in overrides:
        return overrides.get(subagent_id), True
    if "default_route" in profile:
        return profile.get("default_route"), True
    return None, False


def resolve_agent_subagent_route(
    config: Any,
    pins: AgentRoutingPins,
    subagent_id: str,
    *,
    role: str | None = None,
    require_allowed: bool = True,
    main_route_override: Mapping[str, Any] | None = None,
) -> AgentModelRoute:
    """Resolve provider/model/effort for one Subagent of a pinned revision."""

    _require_pins(pins)
    sid = _clean(subagent_id)
    if pins.route_snapshot is not None:
        if role:
            return _resolve_snapshot_role_route(pins, role, expected_subagent_id=sid)
        for raw_role, raw_route in _snapshot_roles(pins.route_snapshot).items():
            if _clean(raw_route.get("acting_subagent_id")) == sid:
                return _resolve_snapshot_role_route(pins, raw_role, expected_subagent_id=sid)
        raise AgentModelRouteError("agent_route_snapshot_role_missing")
    team = _team_for(config, pins)
    members = {_clean(item) for item in (team.get("subagent_ids") or []) if _clean(item)}
    subagent = known_agent_subagents(config).get(sid)
    if (
        not sid
        or sid not in members
        or subagent is None
        or subagent.get("enabled", True) is False
        or (require_allowed and sid not in pins.allowed_subagent_ids)
    ):
        raise AgentModelRouteError("agent_route_subagent_unavailable")
    raw_spec, raw_spec_present = _raw_persisted_route_spec(config, pins, sid)
    if raw_spec_present and raw_spec is not None and (
        not isinstance(raw_spec, Mapping)
        or not _route_spec_is_well_formed(raw_spec)
    ):
        raise AgentModelRouteError("agent_route_model_unavailable")
    spec, source = _route_spec(config, pins, sid)
    if spec is not None and not _route_spec_is_well_formed(spec):
        raise AgentModelRouteError("agent_route_model_unavailable")
    main = _main_route(config, main_route_override)
    if spec is None:
        route = dict(main)
        route["inherit_model"] = True
    else:
        route = _apply_execution_route(spec, main, config=config)
    provider = _clean(route.get("provider")).lower()
    model = _clean(route.get("model"))
    if not provider or not model:
        raise AgentModelRouteError("agent_route_model_unavailable")
    if provider not in ROUTABLE_PROVIDERS:
        raise AgentModelRouteError("agent_route_provider_unsupported")
    effort = _clean(route.get("effort") or route.get("reasoning_effort"))
    return AgentModelRoute(
        agent_id=pins.agent_id,
        agent_revision_id=pins.agent_revision_id,
        agent_team_id=pins.agent_team_id,
        execution_profile_id=pins.execution_profile_id,
        acting_subagent_id=sid,
        role=role,
        provider=provider,
        model=model,
        effort=effort,
        route_source=source,
        inherits_main=bool(route.get("inherit_model", spec is None)),
        backend="cli" if provider.endswith("-cli") else "api",
    )


def select_role_subagent(config: Any, pins: AgentRoutingPins, role: str) -> str:
    """Pick the Subagent that acts for ``role`` inside the pinned revision."""

    candidates = OPERATIONS_WORK_ROLES.get(_clean(role))
    if not candidates:
        raise AgentModelRouteError("agent_route_role_unknown")
    if pins.route_snapshot is not None:
        route = _resolve_snapshot_role_route(pins, role)
        return route.acting_subagent_id
    team = _team_for(config, pins)
    members = [_clean(item) for item in (team.get("subagent_ids") or []) if _clean(item)]
    subagents = known_agent_subagents(config)
    usable = {
        sid
        for sid in members
        if sid in pins.allowed_subagent_ids
        and (subagents.get(sid) or {}).get("enabled", True) is not False
        and sid in subagents
    }
    for candidate in candidates:
        if candidate in usable:
            return candidate
    raise AgentModelRouteError("agent_route_role_unavailable")


def resolve_agent_role_route(
    config: Any,
    pins: AgentRoutingPins,
    role: str,
    *,
    main_route_override: Mapping[str, Any] | None = None,
) -> AgentModelRoute:
    """Resolve the route of the Subagent acting for a work role."""

    if pins.route_snapshot is not None:
        return _resolve_snapshot_role_route(pins, role)
    subagent_id = select_role_subagent(config, pins, role)
    return resolve_agent_subagent_route(
        config,
        pins,
        subagent_id,
        role=role,
        main_route_override=main_route_override,
    )


EXECUTION_SELECTION_SOURCE_DEFAULT = "revision_default"
EXECUTION_SELECTION_SOURCE_EXPLICIT = "explicit"
# Formal system ExecutionProfiles selectable for any Team.  Legacy
# pre-Team-profile ids (heavy/light/coding) are deliberately excluded: an
# explicit selection never takes a legacy fallback.
SYSTEM_EXECUTION_PROFILE_IDS = frozenset({MANUAL_EXECUTION_PROFILE_ID, FREE_TEAM_PROFILE_ID})
_SELECTION_PROFILE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class AgentExecutionSelection:
    """Server-owned runtime selection, independent of the Agent role.

    ``default_execution_profile_id`` is the pinned revision's own value;
    ``selected_execution_profile_id`` is what routes this invocation.
    ``source`` is ``revision_default`` or ``explicit``.
    """

    default_execution_profile_id: str
    selected_execution_profile_id: str
    source: str = EXECUTION_SELECTION_SOURCE_DEFAULT

    @property
    def is_explicit(self) -> bool:
        return self.source == EXECUTION_SELECTION_SOURCE_EXPLICIT

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "source": self.source,
            "default_execution_profile_id": self.default_execution_profile_id,
            "selected_execution_profile_id": self.selected_execution_profile_id,
        }


def _raw_team_profile(config: Any, team_id: str, profile_id: str) -> tuple[Any, bool]:
    """Find the raw stored profile by storage key or embedded ``profile_id``.

    A key/id mismatch or an ambiguous match is returned as a non-mapping so
    callers treat it as corruption instead of normalizing it away.
    """

    if isinstance(config, Mapping):
        section = config.get("agent_team", {})
    else:
        getter = getattr(config, "get", None)
        section = getter("agent_team", {}) if callable(getter) else {}
    teams = section.get("teams") if isinstance(section, Mapping) else None
    team = teams.get(team_id) if isinstance(teams, Mapping) else None
    profiles = team.get("execution_profiles") if isinstance(team, Mapping) else None
    if not isinstance(profiles, Mapping):
        return None, False
    matches = []
    for key, raw in profiles.items():
        embedded = _clean(raw.get("profile_id")) if isinstance(raw, Mapping) else ""
        if key == profile_id or embedded == profile_id:
            if isinstance(raw, Mapping) and embedded and embedded != key:
                return None, True
            matches.append(raw)
    if not matches:
        return None, False
    if len(matches) > 1:
        return None, True
    return matches[0], True


def _raw_profile_is_well_formed(raw: Any) -> bool:
    if not isinstance(raw, Mapping):
        return False
    if not isinstance(raw.get("enabled", True), bool):
        return False
    default_route = raw.get("default_route")
    if default_route is not None and (
        not isinstance(default_route, Mapping) or not _route_spec_is_well_formed(default_route)
    ):
        return False
    overrides = raw.get("overrides")
    if overrides is None:
        return True
    if not isinstance(overrides, Mapping):
        return False
    return all(
        isinstance(spec, Mapping) and _route_spec_is_well_formed(spec)
        for spec in overrides.values()
    )


def resolve_agent_execution_selection(
    config: Any,
    pins: AgentRoutingPins,
    execution_profile_id: Any = None,
) -> AgentExecutionSelection:
    """Validate an optional per-invocation ExecutionProfile selection.

    ``None`` keeps the revision default (legacy behavior).  An explicit value
    must be a formal system Profile or an enabled, well-formed Profile of the
    pinned revision's own Team.  Empty/unknown/disabled/foreign/corrupt
    values raise ``AgentModelRouteError`` and never fall back.
    """

    _require_pins(pins)
    if pins.execution_selection is not None:
        raise AgentModelRouteError("agent_route_selection_invalid")
    default_id = pins.execution_profile_id
    if execution_profile_id is None:
        return AgentExecutionSelection(default_id, default_id)
    if not isinstance(execution_profile_id, str):
        raise AgentModelRouteError("agent_execution_profile_invalid")
    selected = execution_profile_id
    if not _SELECTION_PROFILE_ID_RE.fullmatch(selected):
        raise AgentModelRouteError("agent_execution_profile_invalid")
    _team_for(config, pins)
    if selected not in SYSTEM_EXECUTION_PROFILE_IDS:
        profile = next(
            (
                item
                for item in list_team_execution_profiles(config, pins.agent_team_id)
                if _clean(item.get("profile_id")) == selected
            ),
            None,
        )
        if profile is None or profile.get("enabled", True) is not True:
            raise AgentModelRouteError("agent_execution_profile_unavailable")
        raw, present = _raw_team_profile(config, pins.agent_team_id, selected)
        if present and not _raw_profile_is_well_formed(raw):
            raise AgentModelRouteError("agent_execution_profile_unavailable")
    return AgentExecutionSelection(default_id, selected, EXECUTION_SELECTION_SOURCE_EXPLICIT)


def _roles_digest(roles: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        safe_route_snapshot(roles), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _role_backends(roles: Mapping[str, Any]) -> dict[str, str]:
    return {
        role: route["backend"]
        for role, route in safe_route_snapshot(roles).items()
        if isinstance(route.get("backend"), str)
    }


def _selection_record(
    selection: Mapping[str, Any],
    *,
    team_id: str,
    roles: Mapping[str, Any],
) -> dict[str, Any]:
    """Durable selection block: ids, per-role backend and a roles digest."""

    return {
        "schema_version": 1,
        "source": EXECUTION_SELECTION_SOURCE_EXPLICIT,
        "agent_team_id": team_id,
        "default_execution_profile_id": _clean(selection.get("default_execution_profile_id")),
        "selected_execution_profile_id": _clean(selection.get("selected_execution_profile_id")),
        "role_backends": _role_backends(roles),
        "roles_sha256": _roles_digest(roles),
    }


def safe_execution_selection(value: Any) -> dict[str, Any] | None:
    """Whitelist a durable selection block (identifiers and digests only)."""

    if not isinstance(value, Mapping):
        return None
    if value.get("schema_version") != 1 or value.get("source") != EXECUTION_SELECTION_SOURCE_EXPLICIT:
        return None
    team_id = value.get("agent_team_id")
    if (
        not isinstance(team_id, str)
        or not team_id
        or len(team_id) > 128
        or team_id != team_id.strip()
        or "/" in team_id
        or "\\" in team_id
    ):
        return None
    result: dict[str, Any] = {
        "schema_version": 1,
        "source": EXECUTION_SELECTION_SOURCE_EXPLICIT,
        "agent_team_id": team_id,
    }
    for key in ("default_execution_profile_id", "selected_execution_profile_id"):
        item = value.get(key)
        if not isinstance(item, str) or not _SELECTION_PROFILE_ID_RE.fullmatch(item):
            return None
        result[key] = item
    digest = value.get("roles_sha256")
    if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
        return None
    backends = value.get("role_backends")
    if not isinstance(backends, Mapping) or len(backends) > len(OPERATIONS_WORK_ROLES):
        return None
    clean_backends: dict[str, str] = {}
    for role, backend in backends.items():
        if role not in OPERATIONS_WORK_ROLES or backend not in {"api", "cli"}:
            return None
        clean_backends[str(role)] = str(backend)
    result["role_backends"] = clean_backends
    result["roles_sha256"] = digest
    return result


def verify_execution_selection(
    *,
    default_execution_profile_id: str,
    agent_team_id: str,
    roles: Any,
    selection: Any = None,
    snapshot_execution_profile_id: Any = None,
) -> AgentExecutionSelection:
    """Rebuild a stored selection and reject any missing/inconsistent part.

    Without a selection block the snapshot must be a revision-default one:
    every role (and the snapshot header, when present) names the revision
    default Profile.  With a block, team/default must match the pinned
    revision, every role names the selected Profile and the roles digest and
    backends are unchanged.
    """

    default_id = _clean(default_execution_profile_id)
    team_id = _clean(agent_team_id)
    role_routes = safe_route_snapshot(roles if isinstance(roles, Mapping) else {})
    if selection is None:
        expected = default_id
        result = AgentExecutionSelection(default_id, default_id)
    else:
        record = safe_execution_selection(selection)
        if (
            record is None
            or _thaw(selection) != record
            or record["agent_team_id"] != team_id
            or record["default_execution_profile_id"] != default_id
            or record["roles_sha256"] != _roles_digest(role_routes)
            or record["role_backends"] != _role_backends(role_routes)
        ):
            raise AgentModelRouteError("agent_route_selection_invalid")
        expected = record["selected_execution_profile_id"]
        result = AgentExecutionSelection(default_id, expected, EXECUTION_SELECTION_SOURCE_EXPLICIT)
    header = _clean(snapshot_execution_profile_id)
    if header and header != expected:
        raise AgentModelRouteError("agent_route_selection_invalid")
    for route in role_routes.values():
        profile = route.get("execution_profile_id")
        if profile is not None and profile != expected:
            raise AgentModelRouteError("agent_route_selection_invalid")
        team = route.get("agent_team_id")
        if team is not None and team != team_id:
            raise AgentModelRouteError("agent_route_selection_invalid")
    return result


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return value


def pins_with_verified_selection(
    pins: AgentRoutingPins,
    *,
    roles: Any,
    selection: Any = None,
    snapshot_execution_profile_id: Any = None,
) -> AgentRoutingPins:
    """Return revision pins carrying a verified stored selection and roles."""

    if pins.execution_selection is not None:
        raise AgentModelRouteError("agent_route_selection_invalid")
    verified = verify_execution_selection(
        default_execution_profile_id=pins.execution_profile_id,
        agent_team_id=pins.agent_team_id,
        roles=roles,
        selection=selection,
        snapshot_execution_profile_id=snapshot_execution_profile_id,
    )
    frozen = replace(pins, route_snapshot=safe_route_snapshot(roles))
    if not verified.is_explicit:
        return frozen
    return replace(
        frozen,
        execution_profile_id=verified.selected_execution_profile_id,
        execution_selection=safe_execution_selection(selection),
    )


def execution_manifest_route_fields(route_snapshot: Any) -> dict[str, Any]:
    """Run-manifest fields carrying frozen roles plus an explicit selection.

    Revision-default snapshots yield only ``route_snapshot`` (the historical
    manifest shape); explicit selections also carry the selection block and
    the selected ``execution_profile_id`` so the manifest hash binds them.
    """

    snapshot = route_snapshot if isinstance(route_snapshot, Mapping) else {}
    fields: dict[str, Any] = {"route_snapshot": safe_route_snapshot(snapshot.get("roles", snapshot))}
    record = safe_execution_selection(snapshot.get("execution_selection"))
    if record is not None:
        fields["execution_selection"] = record
        fields["execution_profile_id"] = record["selected_execution_profile_id"]
    return fields


def preview_execution_profile_routes(
    config: Any,
    agent_team_id: str,
    execution_profile_id: str,
    *,
    subagent_ids: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Read-only effective route per Team member for UI previews.

    Errors are reported per Subagent as a safe code; no credential, base URL
    or path is ever included.
    """

    team = known_agent_teams(config).get(_clean(agent_team_id)) or {}
    members = [_clean(item) for item in (team.get("subagent_ids") or []) if _clean(item)]
    if subagent_ids is not None:
        wanted = {_clean(item) for item in subagent_ids}
        members = [sid for sid in members if sid in wanted]
    pins = AgentRoutingPins(
        agent_id=None,
        agent_revision_id=None,
        agent_team_id=_clean(agent_team_id),
        execution_profile_id=_clean(execution_profile_id),
        allowed_subagent_ids=tuple(members),
    )
    subagents = known_agent_subagents(config)
    result: list[dict[str, Any]] = []
    for sid in members:
        item: dict[str, Any] = {
            "subagent_id": sid,
            "name": str((subagents.get(sid) or {}).get("name") or sid),
        }
        try:
            route = resolve_agent_subagent_route(config, pins, sid)
        except AgentModelRouteError as exc:
            item["error_code"] = exc.code
        except Exception:
            item["error_code"] = "agent_route_unavailable"
        else:
            item.update(
                provider=route.provider,
                model=route.model,
                effort=route.effort or None,
                route_source=route.route_source,
                inherits_main=route.inherits_main,
                backend=route.backend,
            )
        result.append(item)
    return result


_PROVENANCE_KEYS = (
    "agent_id",
    "agent_revision_id",
    "agent_team_id",
    "execution_profile_id",
    "acting_subagent_id",
    "role",
    "provider",
    "model",
    "effort",
    "route_source",
    "inherits_main",
    "backend",
)


def safe_route_projection(value: Any) -> dict[str, Any] | None:
    """Whitelist one route provenance mapping for durable audit rows."""

    if not isinstance(value, Mapping):
        return None
    result: dict[str, Any] = {}
    for key in _PROVENANCE_KEYS:
        item = value.get(key)
        if item is None:
            continue
        if isinstance(item, bool):
            result[key] = item
        elif isinstance(item, str) and item.strip():
            result[key] = item.strip()[:160]
    from ..llm.generation_budget import safe_generation_provenance

    generation = safe_generation_provenance(value.get("generation"))
    if generation:
        result["generation"] = generation
    notes = value.get("notes")
    if isinstance(notes, (list, tuple)):
        clean_notes = [str(note)[:80] for note in notes if isinstance(note, str) and note.strip()][:8]
        if clean_notes:
            result["notes"] = clean_notes
    return result if result.get("provider") and result.get("model") else None


def safe_route_projections(value: Any) -> dict[str, dict[str, Any]]:
    """Whitelist a ``{phase: route}`` mapping (phase names are identifiers)."""

    if not isinstance(value, Mapping):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for phase, route in list(value.items())[:16]:
        name = _clean(phase)[:64]
        projected = safe_route_projection(route)
        if name and projected:
            result[name] = projected
    return result


def safe_route_snapshot(value: Any) -> dict[str, dict[str, Any]]:
    """Whitelist role routes plus fail-closed role errors for a run snapshot."""

    if not isinstance(value, Mapping):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for role, raw_route in list(value.items())[: len(OPERATIONS_WORK_ROLES) + 8]:
        name = _clean(role)[:64]
        if not name or not isinstance(raw_route, Mapping):
            continue
        error = _clean(raw_route.get("error"))
        if error and error.startswith("agent_route_") and len(error) <= 96:
            result[name] = {"error": error}
            continue
        projected = safe_route_projection(raw_route)
        if projected:
            result[name] = projected
    return result


def build_agent_routing_snapshot(
    config: Any,
    pins: AgentRoutingPins,
    *,
    roles: Iterable[str] | None = None,
    main_route_override: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Capture every requested role against the current Profile exactly once.

    Missing roles are retained as safe ``agent_route_*`` errors.  This is
    intentional: a later retry must not re-read App Config and turn a role
    that was unavailable at Work start into a newly executable role.
    """

    selected_roles = tuple(dict.fromkeys(
        _clean(role) for role in (roles if roles is not None else OPERATIONS_WORK_ROLES)
        if _clean(role)
    ))
    raw_routes: dict[str, dict[str, Any]] = {}
    for role in selected_roles:
        try:
            raw_routes[role] = resolve_agent_role_route(
                config,
                pins,
                role,
                main_route_override=main_route_override,
            ).provenance()
        except AgentModelRouteError as exc:
            raw_routes[role] = {"error": exc.code}
    snapshot: dict[str, Any] = {
        "schema_version": 1,
        "agent_id": pins.agent_id,
        "agent_revision_id": pins.agent_revision_id,
        "team_id": pins.agent_team_id,
        "execution_profile_id": pins.execution_profile_id,
        "roles": safe_route_snapshot(raw_routes),
    }
    if pins.execution_selection is not None:
        # Only explicit selections add a block, so revision-default snapshots
        # keep exactly their historical hash material.
        snapshot["execution_selection"] = _selection_record(
            pins.execution_selection,
            team_id=pins.agent_team_id,
            roles=snapshot["roles"],
        )
    return snapshot


def _snapshot_roles(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, Mapping):
        return {}
    roles = value.get("roles", value)
    return safe_route_snapshot(roles)


def _resolve_snapshot_role_route(
    pins: AgentRoutingPins,
    role: str,
    *,
    expected_subagent_id: str | None = None,
) -> AgentModelRoute:
    role_name = _clean(role)
    if role_name not in OPERATIONS_WORK_ROLES:
        raise AgentModelRouteError("agent_route_role_unknown")
    raw = _snapshot_roles(pins.route_snapshot).get(role_name)
    if raw is None:
        raise AgentModelRouteError("agent_route_snapshot_role_missing")
    error = _clean(raw.get("error"))
    if error:
        raise AgentModelRouteError(error if error.startswith("agent_route_") else "agent_route_snapshot_invalid")
    route = safe_route_projection(raw)
    if route is None:
        raise AgentModelRouteError("agent_route_snapshot_invalid")
    acting_subagent_id = _clean(route.get("acting_subagent_id"))
    if not acting_subagent_id or (
        expected_subagent_id and acting_subagent_id != _clean(expected_subagent_id)
    ):
        raise AgentModelRouteError("agent_route_snapshot_subagent_mismatch")
    if acting_subagent_id not in pins.allowed_subagent_ids:
        raise AgentModelRouteError("agent_route_subagent_unavailable")
    provider = _clean(route.get("provider")).lower()
    model = _clean(route.get("model"))
    if not provider or not model:
        raise AgentModelRouteError("agent_route_model_unavailable")
    if provider not in ROUTABLE_PROVIDERS:
        raise AgentModelRouteError("agent_route_provider_unsupported")
    notes = route.get("notes")
    return AgentModelRoute(
        agent_id=pins.agent_id,
        agent_revision_id=pins.agent_revision_id,
        agent_team_id=pins.agent_team_id,
        execution_profile_id=pins.execution_profile_id,
        acting_subagent_id=acting_subagent_id,
        role=role_name,
        provider=provider,
        model=model,
        effort=_clean(route.get("effort")),
        route_source=_clean(route.get("route_source")) or "profile_default",
        inherits_main=bool(route.get("inherits_main", False)),
        backend=_clean(route.get("backend")) or ("cli" if provider.endswith("-cli") else "api"),
        notes=tuple(str(item) for item in notes if isinstance(item, str)) if isinstance(notes, list) else (),
    )


def _run_route_snapshot(
    run: Mapping[str, Any] | None,
) -> tuple[dict[str, dict[str, Any]], Any, Any] | None:
    if not isinstance(run, Mapping):
        return None
    manifest = run.get("resolved_execution_manifest")
    if not isinstance(manifest, Mapping):
        manifest = run.get("execution_manifest")
    durable = bool(run.get("id") or run.get("execution_manifest_hash") or manifest is not None)
    if not isinstance(manifest, Mapping):
        if durable:
            raise AgentModelRouteError("agent_route_snapshot_missing")
        return None
    expected_hash = _clean(run.get("execution_manifest_hash")).lower()
    if expected_hash:
        actual_hash = hashlib.sha256(
            json.dumps(dict(manifest), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if actual_hash != expected_hash:
            raise AgentModelRouteError("agent_route_snapshot_hash_mismatch")
    snapshot = manifest.get("route_snapshot")
    if not isinstance(snapshot, Mapping):
        if durable:
            raise AgentModelRouteError("agent_route_snapshot_missing")
        return None
    roles = safe_route_snapshot(snapshot.get("roles", snapshot))
    if not roles:
        raise AgentModelRouteError("agent_route_snapshot_invalid")
    return roles, manifest.get("execution_selection"), manifest.get("execution_profile_id")


async def load_pinned_agent_pins(
    session: Any,
    *,
    agent_revision_id: Any,
    agent_id: Any = None,
    run: Mapping[str, Any] | None = None,
) -> AgentRoutingPins:
    """Load routing pins from the exact AgentRevision a WorkItem/Run pinned.

    Never reads "the latest revision": a revision edited after the work was
    claimed must not change the model of the in-flight job.
    """

    from uuid import UUID

    from ..memory.models import AgentRevision

    try:
        revision_uuid = UUID(str(agent_revision_id))
    except (TypeError, ValueError):
        raise AgentModelRouteError("agent_route_revision_missing") from None
    if isinstance(run, Mapping):
        run_revision = _clean(run.get("agent_revision_id"))
        if run_revision and run_revision != str(revision_uuid):
            raise AgentModelRouteError("agent_route_revision_mismatch")
    revision = await session.get(AgentRevision, revision_uuid)
    if revision is None:
        raise AgentModelRouteError("agent_route_revision_missing")
    if agent_id and str(revision.agent_id) != _clean(agent_id):
        raise AgentModelRouteError("agent_route_revision_mismatch")
    pins = AgentRoutingPins.from_revision(revision)
    stored = _run_route_snapshot(run)
    if stored is None:
        return pins
    roles, selection, header = stored
    # The stored selection/routes win over the revision default; a missing,
    # inconsistent or tampered selection fails closed.
    return pins_with_verified_selection(
        pins,
        roles=roles,
        selection=selection,
        snapshot_execution_profile_id=header,
    )


def registered_route_backend(provider: Any) -> str:
    """Return the backend whose *registered* adapter executes ``provider``.

    The answer comes from the real adapter registries, never from a provider
    name suffix: ``cli`` only for providers the CLI session adapter accepts
    (``CLI_PROVIDER_PERMISSION_MODES``), ``api`` only for tool-capable API
    clients (``PROVIDER_CAPABILITIES``).  Anything else (e.g. ``foo-cli`` or
    ``routing-profile``) fails closed.  Imports stay lazy so route resolution
    never loads provider SDKs or DB models at import time.
    """

    from .cli_development_sessions import CLI_PROVIDER_PERMISSION_MODES
    from .llm_model_catalog import PROVIDER_CAPABILITIES

    key = _clean(provider).lower()
    if key and key in CLI_PROVIDER_PERMISSION_MODES:
        return "cli"
    capability = PROVIDER_CAPABILITIES.get(key) if key else None
    if capability is not None and getattr(capability, "supports_tools", False) is True:
        return "api"
    raise AgentModelRouteError("agent_route_provider_unsupported")


def assert_registered_route_backend(provider: Any, backend: Any, *, ingress: str) -> str:
    """Fail closed unless the declared backend, registry and ingress agree.

    Used for explicit ExecutionProfile selections.  An empty backend is not
    implied; an API-only provider at the CLI ingress (or the reverse) is
    rejected with the ingress' fixed code.
    """

    registered = registered_route_backend(provider)
    declared = _clean(backend).lower()
    if declared != registered:
        raise AgentModelRouteError("agent_route_backend_mismatch")
    if registered != ingress:
        raise AgentModelRouteError(
            "cli_route_not_supported" if ingress == "api" else "agent_route_backend_mismatch"
        )
    return registered


__all__ = [
    "AgentExecutionSelection",
    "AgentModelRoute",
    "AgentModelRouteError",
    "AgentRoutingPins",
    "assert_registered_route_backend",
    "build_agent_routing_snapshot",
    "EXECUTION_SELECTION_SOURCE_DEFAULT",
    "EXECUTION_SELECTION_SOURCE_EXPLICIT",
    "execution_manifest_route_fields",
    "OPERATIONS_WORK_ROLES",
    "pins_with_verified_selection",
    "registered_route_backend",
    "resolve_agent_execution_selection",
    "safe_execution_selection",
    "SYSTEM_EXECUTION_PROFILE_IDS",
    "verify_execution_selection",
    "ROUTABLE_PROVIDERS",
    "ROUTE_SOURCE_LABELS",
    "known_agent_teams",
    "known_agent_subagents",
    "load_pinned_agent_pins",
    "preview_execution_profile_routes",
    "resolve_agent_role_route",
    "resolve_agent_subagent_route",
    "safe_route_projection",
    "safe_route_projections",
    "safe_route_snapshot",
    "select_role_subagent",
]
