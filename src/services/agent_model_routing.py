"""Canonical model routing for Operations Agent work.

The single source of truth for which provider/model/effort an Operations
Agent uses is::

    Operations Agent
      -> exact AgentRevision (pinned by AgentWorkItem / AgentRun)
      -> AgentRevision v2 model_overrides
      -> global model_roles (with Chat inheritance)
      -> provider / model / effort

Domain services (CrowdWorks, Web制作, アプリ開発, 調査 ...) never call
``create_llm_client(config)`` for Agent work and never copy this resolution.
They ask for a *work role* (planner / worker / reviewer ...) and receive an
``AgentModelRoute`` from the pinned v2 role route. Historical Team/Profile
pins remain available only in audit snapshots and legacy display projections.

Resolution is fail-closed: a missing v2 role route raises
``AgentModelRouteError`` instead of falling back to the retired Team/Profile
runtime.

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

from .session_llm_runtime_context import (
    bind_session_main_route_override,
    reset_session_main_route_override,
)
from .agent_revision_v2 import (
    model_role_for_legacy_subagent,
    model_role_for_work_role,
    revision_delegate_policy,
    revision_model_overrides,
    revision_role_route_candidates,
    revision_role_fallback_policy,
    revision_tool_ids,
    revision_v2_fields_present,
    resolve_agent_role_route_v2,
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
        "muse-remote",
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
    "operator": (
        "employee_operator",
        "general_worker",
        "docs_operator",
        "workspace_operator",
        "code_implementer",
        "personal_private_adapter",
    ),
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
    # Additive AgentRevision v2 policy fields.  They remain ``None`` for
    # legacy revisions, which is the compatibility switch for the resolver.
    model_overrides: Mapping[str, Any] | None = None
    tool_ids: tuple[str, ...] | None = None
    delegate_policy: Mapping[str, Any] | None = None
    complete_p2: bool = False

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

        # Validate the raw revision before the compatibility projections below
        # can normalize malformed fields into apparently valid empty values.
        from .agent_identity_service import AgentIdentityValidationError, _delegate_policy, _model_overrides, _tool_ids

        complete_p2 = revision_v2_fields_present(revision)
        if complete_p2:
            try:
                raw_models, raw_tools, raw_policy = read("model_overrides"), read("tool_ids"), read("delegate_policy")
                if not isinstance(raw_models, dict) or not isinstance(raw_tools, list) or not isinstance(raw_policy, dict):
                    complete_p2 = False
                else:
                    _model_overrides(raw_models)
                    _tool_ids(raw_tools, allow_concrete=True)
                    _delegate_policy(raw_policy)
            except AgentIdentityValidationError:
                complete_p2 = False
        allowed = read("allowed_subagent_ids") or []
        model_overrides = revision_model_overrides(revision)
        tool_ids = revision_tool_ids(revision)
        delegate_policy = revision_delegate_policy(revision)
        return cls(
            agent_id=str(read("agent_id")) if read("agent_id") else None,
            agent_revision_id=str(read("id")) if read("id") else None,
            agent_team_id=str(read("agent_team_id") or "").strip(),
            execution_profile_id=str(read("execution_profile_id") or "").strip(),
            allowed_subagent_ids=tuple(
                dict.fromkeys(str(item).strip() for item in allowed if str(item).strip())
            ),
            model_overrides=model_overrides,
            tool_ids=tuple(tool_ids) if tool_ids is not None else None,
            delegate_policy=delegate_policy,
            complete_p2=complete_p2,
        )


@dataclass(frozen=True)
class AgentModelRoute:
    """Resolved route for one Subagent acting for an Operations Agent."""

    agent_team_id: str
    execution_profile_id: str
    acting_subagent_id: str
    provider: str
    model: str | None
    effort: str | None = ""
    route_source: str = "profile_default"
    inherits_main: bool = False
    backend: str = "api"
    role: str | None = None
    agent_id: str | None = None
    agent_revision_id: str | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)
    # None identifies a historical route without a frozen candidate contract.
    fallback_routes: tuple[Mapping[str, Any], ...] = field(default_factory=tuple, compare=False)
    allow_paid_fallback: bool = field(default=False, compare=False)
    pool_id: str | None = field(default=None, compare=False)
    candidate_routes: tuple[Mapping[str, Any], ...] | None = field(default=None, compare=False)
    fallback_on: tuple[str, ...] | None = field(default=None, compare=False)
    selected_candidate_index: int | None = field(default=None, compare=False)

    def provenance(self) -> dict[str, Any]:
        """Safe audit projection: identifiers and model names only.

        Never includes credentials, base URLs, filesystem paths, prompts or
        reasoning.  The values are all configuration identifiers.
        """

        result: dict[str, Any] = {
            "agent_id": self.agent_id,
            "agent_revision_id": self.agent_revision_id,
            "role": self.role,
            "provider": self.provider,
            "model": self.model,
            "effort": self.effort or None,
            "route_source": self.route_source,
            "inherits_main": self.inherits_main,
            "backend": self.backend,
        }
        # These keys belong only to old persisted route records.  Current P2
        # routes omit them; historical readers still surface them when a
        # snapshot supplied the original values.
        if self.agent_team_id:
            result["agent_team_id"] = self.agent_team_id
        if self.execution_profile_id:
            result["execution_profile_id"] = self.execution_profile_id
        if self.acting_subagent_id:
            result["acting_subagent_id"] = self.acting_subagent_id
        if self.notes:
            result["notes"] = list(self.notes)
        if self.candidate_routes is not None:
            result["candidate_routes"] = [dict(route) for route in self.candidate_routes]
            result["allow_paid_fallback"] = self.allow_paid_fallback
            if self.fallback_on is not None:
                result["fallback_on"] = list(self.fallback_on)
        if self.selected_candidate_index is not None:
            result["selected_candidate_index"] = self.selected_candidate_index
            if self.pool_id is not None:
                result["pool_id"] = self.pool_id
        return result

    def with_role(self, role: str | None) -> "AgentModelRoute":
        return AgentModelRoute(**{**self.__dict__, "role": role})


def _clean(value: Any) -> str:
    return str(value or "").strip()


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

    # New revisions are self-contained role routes.  Team/Profile pins may be
    # NULL after the P5 schema transition; only historical snapshots or the
    # retired legacy resolver require those old values.
    if pins.route_snapshot is None and not revision_v2_fields_present(pins):
        raise AgentModelRouteError("agent_route_legacy_revision_retired")
    sid = _clean(subagent_id)
    if pins.route_snapshot is not None:
        # A per-turn snapshot is the frozen execution contract.  It must win
        # over the revision/global resolver even for v2 revisions, because
        # model_roles can change after the binding or run was created.
        if role:
            return _resolve_snapshot_role_route(pins, role, expected_subagent_id=sid)
        for raw_role, raw_route in _snapshot_roles(pins.route_snapshot).items():
            if _clean(raw_route.get("acting_subagent_id")) == sid:
                return _resolve_snapshot_role_route(pins, raw_role, expected_subagent_id=sid)
        raise AgentModelRouteError("agent_route_snapshot_role_missing")
    # P2 keeps legacy Subagent entry points available while route ownership
    # moves to the revision's role table.  Resolve the canonical role once,
    # then retain the requested Subagent id in the returned route so callers
    # and audit records keep their established identity shape.
    if revision_v2_fields_present(pins):
        model_role = model_role_for_legacy_subagent(sid)
        if not model_role:
            # A v2 revision must never silently fall back to the old Team
            # resolver for a custom/unknown Subagent id.  That would allow a
            # legacy profile to bypass the revision's role table.
            raise AgentModelRouteError("agent_route_subagent_unavailable")
        route = _resolve_v2_model_route(
            config,
            pins,
            model_role,
            main_route_override=main_route_override,
        )
        if route is None:
            raise AgentModelRouteError("agent_route_model_unavailable")
        return replace(route, acting_subagent_id=sid, role=role or model_role)
    raise AgentModelRouteError("agent_route_legacy_revision_retired")


def _resolve_v2_model_route(
    config: Any,
    pins: AgentRoutingPins,
    role: str,
    *,
    main_route_override: Mapping[str, Any] | None = None,
) -> AgentModelRoute | None:
    """Adapt the strict v2 tuple contract to the historical route object."""

    strict, source = resolve_agent_role_route_v2(
        config,
        pins,
        role,
        main_route_override=main_route_override,
    )
    if source == "legacy":
        return None
    try:
        candidates, allow_paid, _ = revision_role_route_candidates(
            config,
            pins,
            role,
            main_route_override=main_route_override,
        )
    except ValueError as exc:
        raise AgentModelRouteError("agent_route_model_unavailable") from exc
    if not isinstance(strict, Mapping) or not candidates:
        raise AgentModelRouteError("agent_route_model_unavailable")
    primary = candidates[0]
    kind = _clean(strict.get("kind")).casefold()
    if kind == "pool":
        provider = "routing-profile"
        model: str | None = "free-team"
        backend = "api"
        effort: str | None = ""
        pool_id = _clean(strict.get("pool_id")) or _clean(primary.get("pool_id")) or None
    else:
        provider = _clean(strict.get("provider")).casefold()
        model_value = strict.get("model")
        model = _clean(model_value) if model_value not in (None, "") else None
        if provider == "muse-remote":
            backend = "remote"
            effort = None
            model = None
        else:
            if provider not in ROUTABLE_PROVIDERS or not model:
                raise AgentModelRouteError("agent_route_model_unavailable")
            backend = "cli" if provider.endswith("-cli") else "api"
            effort = _clean(strict.get("effort") or primary.get("effort"))
        pool_id = None
    return AgentModelRoute(
        agent_id=pins.agent_id,
        agent_revision_id=pins.agent_revision_id,
        agent_team_id=pins.agent_team_id,
        execution_profile_id=pins.execution_profile_id,
        acting_subagent_id="",
        provider=provider,
        model=model,
        effort=effort,
        route_source=source,
        inherits_main=False,
        backend=backend,
        role=_clean(role),
        fallback_routes=tuple(copy.deepcopy(candidates[1:])),
        allow_paid_fallback=allow_paid,
        pool_id=pool_id,
        candidate_routes=tuple(copy.deepcopy(candidates)),
        fallback_on=revision_role_fallback_policy(pins, role),
    )


def resolve_native_code_route(config: Any, pins: AgentRoutingPins) -> AgentModelRoute:
    """Resolve the revision's native code route without generic work-role snapshots."""
    route = _resolve_v2_model_route(config, pins, "code")
    if route is None or route.route_source != "agent":
        raise AgentModelRouteError("agent_route_model_unavailable")
    return route


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
    if revision_v2_fields_present(pins):
        route = _resolve_v2_model_route(
            config,
            pins,
            role,
            main_route_override=main_route_override,
        )
        if route is None:
            raise AgentModelRouteError("agent_route_model_unavailable")
        return route
    raise AgentModelRouteError("agent_route_legacy_revision_retired")


EXECUTION_SELECTION_SOURCE_DEFAULT = "revision_default"
EXECUTION_SELECTION_SOURCE_EXPLICIT = "explicit"
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


def resolve_agent_execution_selection(
    config: Any,
    pins: AgentRoutingPins,
    execution_profile_id: Any = None,
) -> AgentExecutionSelection:
    """Keep the historical selection seam while rejecting new EP choices."""

    del config
    if not revision_v2_fields_present(pins):
        raise AgentModelRouteError("agent_route_legacy_revision_retired")
    if execution_profile_id is not None:
        raise AgentModelRouteError("agent_execution_profile_removed")
    return AgentExecutionSelection("", "")


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
        if role not in OPERATIONS_WORK_ROLES or backend not in {"api", "cli", "remote"}:
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
    contract_keys = {"candidate_routes", "allow_paid_fallback", "fallback_on", "selected_candidate_index"}
    if contract_keys.intersection(value):
        candidates = value.get("candidate_routes")
        paid = value.get("allow_paid_fallback")
        if not isinstance(candidates, (list, tuple)) or not candidates or not isinstance(paid, bool):
            return None
        clean_candidates = []
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                return None
            kind = candidate.get("kind")
            allowed = {"kind", "pool_id", "provider", "model"} if kind == "pool" else {"kind", "provider", "model", "effort"}
            if set(candidate) - allowed or kind not in ("pool", "llm"):
                return None
            if any(not isinstance(v, str) or not v.strip() or v != v.strip() or len(v) > 160 for v in candidate.values()):
                return None
            if kind == "pool":
                if not candidate.get("pool_id") or candidate.get("provider") != "routing-profile" or candidate.get("model") != "free-team":
                    return None
            elif candidate.get("provider") not in ROUTABLE_PROVIDERS or not candidate.get("model"):
                return None
            elif candidate.get("provider") == "muse-remote" and (candidate.get("model") != "muse-chat" or "effort" in candidate):
                return None
            clean_candidates.append(dict(candidate))
        index = value.get("selected_candidate_index", 0)
        if type(index) is not int or index < 0 or index >= len(clean_candidates):
            return None
        primary = clean_candidates[index]
        if primary.get("provider") != result.get("provider"):
            return None
        if primary.get("provider") != "muse-remote" and (primary.get("model") != result.get("model") or (primary.get("effort") or None) != (result.get("effort") or None)):
            return None
        if "selected_candidate_index" in value:
            if value.get("pool_id") != primary.get("pool_id"):
                return None
            result["selected_candidate_index"] = index
            if primary.get("pool_id") is not None:
                result["pool_id"] = primary["pool_id"]
        result["candidate_routes"] = clean_candidates
        result["allow_paid_fallback"] = paid
        if "fallback_on" in value:
            from .agent_route_fallback_policy import normalize_fallback_on
            try:
                policy = normalize_fallback_on(value["fallback_on"])
            except ValueError:
                return None
            if policy is None:
                return None
            result["fallback_on"] = list(policy)
    from ..llm.generation_budget import safe_generation_provenance

    generation = safe_generation_provenance(value.get("generation"))
    if generation:
        result["generation"] = generation
    notes = value.get("notes")
    if isinstance(notes, (list, tuple)):
        clean_notes = [str(note)[:80] for note in notes if isinstance(note, str) and note.strip()][:8]
        if clean_notes:
            result["notes"] = clean_notes
    provider = _clean(result.get("provider")).lower()
    backend = _clean(result.get("backend")).lower()
    if backend == "remote" or provider == "muse-remote":
        # A remote projection is valid only with its typed backend and with
        # no model/effort values.  This keeps the frozen snapshot honest.
        if provider != "muse-remote" or backend != "remote":
            return None
        if "model" in result or "effort" in result:
            return None
        return result
    return result if provider and result.get("model") else None


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
    """Capture every requested role against current P2 routing exactly once.

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
            route = resolve_agent_role_route(
                config,
                pins,
                role,
                main_route_override=main_route_override,
            )
            provenance = route.provenance()
            # Newly written snapshots contain only the P2 role route.  The
            # shared safe reader still accepts the old keys below so runs
            # created before P5 remain inspectable.
            for legacy_key in (
                "agent_team_id",
                "execution_profile_id",
                "acting_subagent_id",
            ):
                provenance.pop(legacy_key, None)
            raw_routes[role] = provenance
        except AgentModelRouteError as exc:
            raw_routes[role] = {"error": exc.code}
    snapshot: dict[str, Any] = {
        "schema_version": 2,
        "agent_id": pins.agent_id,
        "agent_revision_id": pins.agent_revision_id,
        "roles": safe_route_snapshot(raw_routes),
    }
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
    snapshot = pins.route_snapshot
    roles = snapshot.get("roles", snapshot) if isinstance(snapshot, Mapping) else {}
    raw = roles.get(role_name) if isinstance(roles, Mapping) else None
    if raw is None:
        raise AgentModelRouteError("agent_route_snapshot_role_missing")
    if not isinstance(raw, Mapping):
        raise AgentModelRouteError("agent_route_snapshot_invalid")
    error = _clean(raw.get("error"))
    if error:
        raise AgentModelRouteError(error if error.startswith("agent_route_") else "agent_route_snapshot_invalid")
    route = safe_route_projection(raw)
    if route is None:
        raise AgentModelRouteError("agent_route_snapshot_invalid")
    if route.get("role") and route["role"] != role_name:
        raise AgentModelRouteError("agent_route_snapshot_invalid")
    acting_subagent_id = _clean(route.get("acting_subagent_id"))
    if expected_subagent_id and acting_subagent_id != _clean(expected_subagent_id):
        raise AgentModelRouteError("agent_route_snapshot_subagent_mismatch")
    # Old complete P2 snapshots stored the canonical role as an audit label.
    # Accept only its original pinned provenance, without reviving an actor.
    if acting_subagent_id:
        canonical_p2 = (
            pins.complete_p2
            and acting_subagent_id == role_name
            and route.get("role") == role_name
            and route.get("agent_id") == pins.agent_id
            and route.get("agent_revision_id") == pins.agent_revision_id
            and route.get("agent_team_id") == pins.agent_team_id
            and route.get("execution_profile_id") == pins.execution_profile_id
            and bool(pins.agent_id and pins.agent_revision_id)
        )
        if not canonical_p2:
            raise AgentModelRouteError("agent_route_subagent_unavailable")
        acting_subagent_id = ""
    provider = _clean(route.get("provider")).lower()
    is_remote = provider == "muse-remote"
    model = _clean(route.get("model")) if not is_remote else None
    if is_remote:
        if route.get("backend") != "remote" or "model" in route or "effort" in route:
            raise AgentModelRouteError("agent_route_snapshot_invalid")
    elif not provider or not model:
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
        effort=None if is_remote else _clean(route.get("effort")),
        route_source=_clean(route.get("route_source")) or "profile_default",
        inherits_main=bool(route.get("inherits_main", False)),
        backend=("remote" if is_remote else (_clean(route.get("backend")) or ("cli" if provider.endswith("-cli") else "api"))),
        notes=tuple(str(item) for item in notes if isinstance(item, str)) if isinstance(notes, list) else (),
        candidate_routes=tuple(route["candidate_routes"]) if "candidate_routes" in route else None,
        fallback_routes=tuple(route["candidate_routes"][1:]) if "candidate_routes" in route else (),
        allow_paid_fallback=route.get("allow_paid_fallback", False),
        fallback_on=tuple(route["fallback_on"]) if "fallback_on" in route else None,
        pool_id=route["candidate_routes"][route.get("selected_candidate_index", 0)].get("pool_id") if "candidate_routes" in route else None,
        selected_candidate_index=route.get("selected_candidate_index"),
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
        hash_manifest = dict(manifest)
        legacy_manifest = hash_manifest.pop("legacy", None)
        if isinstance(legacy_manifest, Mapping):
            # AgentRun.to_dict groups historical route pins under ``legacy``
            # at the API boundary. Rebuild the stored pre-projection keys for
            # integrity verification without exposing them as current fields.
            for current_key, stored_key in (
                ("agent_team_id", "team_id"),
                ("execution_profile_id", "execution_profile_id"),
                ("acting_subagent_id", "subagent_id"),
                ("execution_selection", "execution_selection"),
            ):
                value = legacy_manifest.get(current_key)
                if current_key in legacy_manifest and stored_key not in hash_manifest:
                    hash_manifest[stored_key] = value
        actual_hash = hashlib.sha256(
            json.dumps(hash_manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
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
    legacy = manifest.get("legacy")
    legacy = legacy if isinstance(legacy, Mapping) else {}
    return (
        roles,
        manifest.get("execution_selection") or legacy.get("execution_selection"),
        manifest.get("execution_profile_id") or legacy.get("execution_profile_id"),
    )


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

    key = _clean(provider).lower()
    if key == "muse-remote":
        return "remote"

    from .cli_development_sessions import CLI_PROVIDER_PERMISSION_MODES
    from .llm_model_catalog import PROVIDER_CAPABILITIES

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
        if ingress == "api" and registered == "cli":
            raise AgentModelRouteError("cli_route_not_supported")
        raise AgentModelRouteError("agent_route_backend_mismatch")
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
    "verify_execution_selection",
    "ROUTABLE_PROVIDERS",
    "ROUTE_SOURCE_LABELS",
    "load_pinned_agent_pins",
    "resolve_agent_role_route",
    "resolve_agent_subagent_route",
    "safe_route_projection",
    "safe_route_projections",
    "safe_route_snapshot",
]
