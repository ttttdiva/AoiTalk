"""Deterministic authority projections for Agent revision migration checks.

The P2 migration stores an explicit tool list on a revision.  A legacy row has
``tool_ids is NULL`` and therefore uses the capability-only policy.  This
module keeps the comparison used by the restore command pure: it reads only a
revision-shaped object and the configured Team/Subagent topology, and it does
not open a database connection or mutate either object.

The effective capability set follows the legacy Agent Team boundary:

``revision capability ceiling ∩ enabled Team members' declared capabilities``

An empty ceiling is an empty capability intersection.  No workspace or other
capability is inferred from a Subagent declaration alone.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any


def _read(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _values(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        raw: Iterable[Any] = (value,)
    elif isinstance(value, (list, tuple, set, frozenset)):
        raw = value
    else:
        raw = ()
    result: set[str] = set()
    for item in raw:
        clean = _text(item)
        if clean:
            result.add(clean)
    return tuple(sorted(result))


def _revision_value(revision: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(revision, Mapping):
            if name in revision:
                return revision[name]
        elif hasattr(revision, name):
            return getattr(revision, name)
    return default


def _catalog() -> dict[str, dict[str, Any]]:
    try:
        from .agent_team_v3 import AGENT_TEAM_CAPABILITY_CATALOG

        return AGENT_TEAM_CAPABILITY_CATALOG
    except Exception:
        return {}


def _known_topology(config: Any) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    try:
        from .agent_model_routing import known_agent_subagents, known_agent_teams

        return known_agent_teams(config), known_agent_subagents(config)
    except Exception:
        return {}, {}


def _delegation_enabled(config: Any) -> bool:
    try:
        from .agent_team_v3 import agent_team_v3_delegation_enabled

        return bool(agent_team_v3_delegation_enabled(config))
    except Exception:
        return False


def _tool_policy_parts() -> tuple[Any, Any, Any, Any, Any, Any]:
    """Load policy helpers lazily so pure conversion callers stay lightweight."""

    try:
        from ..llm.tool_policy import (
            _AGENT_REVISION_ALWAYS_VISIBLE_TOOL_NAMES,
            _AGENT_REVISION_TOOL_GROUPS,
            _AGENT_TOOL_CAPABILITY_FAMILIES,
            agent_capability_ids_for_tool,
            agent_revision_tool_id_permits,
        )

        return (
            _AGENT_REVISION_ALWAYS_VISIBLE_TOOL_NAMES,
            _AGENT_REVISION_TOOL_GROUPS,
            _AGENT_TOOL_CAPABILITY_FAMILIES,
            agent_capability_ids_for_tool,
            agent_revision_tool_id_permits,
            True,
        )
    except Exception:
        return frozenset(), {}, {}, None, None, False


def _revision_tool_ids(revision: Any) -> list[str] | None:
    """Read P2 tool IDs while preserving NULL versus explicit ``[]``."""

    try:
        from .agent_revision_v2 import revision_tool_ids

        return revision_tool_ids(revision)
    except Exception:
        value = _revision_value(revision, "tool_ids", "tool_ids_json", default=None)
        if value is None:
            return None
        return list(_values(value))


@dataclass(frozen=True)
class SubagentAuthority:
    """Static authority for one revision-allowed Subagent."""

    subagent_id: str
    enabled: bool
    team_member: bool
    declared_capabilities: tuple[str, ...] = ()
    effective_capabilities: tuple[str, ...] = ()
    workspace_access: str = "none"
    backend: str = "api"
    child_capabilities: dict[str, dict[str, tuple[str, ...]]] = field(default_factory=dict)
    child_tool_names: dict[str, dict[str, tuple[str, ...]]] = field(default_factory=dict)

    @property
    def effective_read_capabilities(self) -> tuple[str, ...]:
        return tuple(self.child_capabilities.get(self.backend, {}).get("read", ()))

    @property
    def effective_write_capabilities(self) -> tuple[str, ...]:
        return tuple(self.child_capabilities.get(self.backend, {}).get("write", ()))

    @property
    def read_tool_names(self) -> tuple[str, ...]:
        return tuple(self.child_tool_names.get(self.backend, {}).get("read", ()))

    @property
    def write_tool_names(self) -> tuple[str, ...]:
        return tuple(self.child_tool_names.get(self.backend, {}).get("write", ()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "subagent_id": self.subagent_id,
            "enabled": self.enabled,
            "team_member": self.team_member,
            "declared_capabilities": list(self.declared_capabilities),
            "effective_capabilities": list(self.effective_capabilities),
            "workspace_access": self.workspace_access,
            "backend": self.backend,
            "effective_read_capabilities": list(self.effective_read_capabilities),
            "effective_write_capabilities": list(self.effective_write_capabilities),
            "read_tool_names": list(self.read_tool_names),
            "write_tool_names": list(self.write_tool_names),
            "child_capabilities": {
                backend: {
                    mode: list(values)
                    for mode, values in modes.items()
                }
                for backend, modes in self.child_capabilities.items()
            },
            "child_tool_names": {
                backend: {
                    mode: list(values)
                    for mode, values in modes.items()
                }
                for backend, modes in self.child_tool_names.items()
            },
        }


@dataclass(frozen=True)
class RevisionAuthority:
    """A serializable, read-only authority projection for one revision."""

    mode: str
    agent_id: str
    revision_id: str
    version: int
    team_id: str
    team_enabled: bool
    team_member_ids: tuple[str, ...]
    allowed_subagent_ids: tuple[str, ...]
    capability_ceiling: tuple[str, ...]
    team_capabilities: tuple[str, ...]
    effective_capabilities: tuple[str, ...]
    subagents: tuple[SubagentAuthority, ...] = ()
    tool_ids: tuple[str, ...] | None = None
    effective_tool_ids: tuple[str, ...] = ()
    tool_names: tuple[str, ...] = ()

    @property
    def explicit_tool_ids(self) -> bool:
        return self.tool_ids is not None

    @property
    def scope(self) -> dict[str, Any]:
        return {
            "agent": {
                "agent_id": self.agent_id,
                "revision_id": self.revision_id,
                "version": self.version,
                "capability_ceiling": list(self.capability_ceiling),
                "allowed_subagent_ids": list(self.allowed_subagent_ids),
            },
            "team": {
                "team_id": self.team_id,
                "enabled": self.team_enabled,
                "member_ids": list(self.team_member_ids),
                "capabilities": list(self.team_capabilities),
            },
            "subagents": [item.to_dict() for item in self.subagents],
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "agent_id": self.agent_id,
            "revision_id": self.revision_id,
            "version": self.version,
            "team_id": self.team_id,
            "team_enabled": self.team_enabled,
            "team_member_ids": list(self.team_member_ids),
            "allowed_subagent_ids": list(self.allowed_subagent_ids),
            "capability_ceiling": list(self.capability_ceiling),
            "team_capabilities": list(self.team_capabilities),
            "effective_capabilities": list(self.effective_capabilities),
            "subagents": [item.to_dict() for item in self.subagents],
            "tool_ids": list(self.tool_ids) if self.tool_ids is not None else None,
            "explicit_tool_ids": self.explicit_tool_ids,
            "effective_tool_ids": list(self.effective_tool_ids),
            "tool_names": list(self.tool_names),
            "scope": copy.deepcopy(self.scope),
        }


@dataclass(frozen=True)
class AuthorityComparison:
    """Before/after authority delta and the minimal additive restore patch."""

    before: RevisionAuthority
    after: RevisionAuthority
    reduced_capability_ids: tuple[str, ...] = ()
    reduced_tool_ids: tuple[str, ...] = ()
    reduced_tool_names: tuple[str, ...] = ()
    restore_capability_ids: tuple[str, ...] = ()
    restore_tool_ids: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(
            self.reduced_capability_ids
            or self.reduced_tool_ids
            or self.reduced_tool_names
        )

    @property
    def reduced(self) -> bool:
        return self.changed

    @property
    def restorable(self) -> bool:
        """Whether this bounded command has additive fields to restore."""

        return bool(self.restore_capability_ids or self.restore_tool_ids)

    def restore_patch(self) -> dict[str, list[str]]:
        ceiling = set(self.after.capability_ceiling)
        ceiling.update(self.restore_capability_ids)
        current_tool_ids = set(self.after.tool_ids or ())
        current_tool_ids.update(self.restore_tool_ids)
        return {
            "capability_ceiling": sorted(ceiling),
            "tool_ids": sorted(current_tool_ids),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "changed": self.changed,
            "reduced": self.reduced,
            "restorable": self.restorable,
            "before": self.before.to_dict(),
            "after": self.after.to_dict(),
            "reduced_capability_ids": list(self.reduced_capability_ids),
            "reduced_tool_ids": list(self.reduced_tool_ids),
            "reduced_tool_names": list(self.reduced_tool_names),
            "restore_capability_ids": list(self.restore_capability_ids),
            "restore_tool_ids": list(self.restore_tool_ids),
            "restore_patch": self.restore_patch(),
        }


def _workspace_access(subagent: Mapping[str, Any]) -> str:
    configured = _text(subagent.get("max_workspace_access")).casefold()
    if configured in {"none", "read", "write"}:
        return configured
    declared = set(_values(subagent.get("capability_ids")))
    if declared & {"workspace_write", "command_execute"}:
        return "write"
    if declared & {"workspace_read", "repo_map"}:
        return "read"
    return "none"


def _child_backends(
    config: Any,
    revision: Any,
    subagent_id: str,
    subagent: Mapping[str, Any],
) -> tuple[str, ...]:
    configured = _text(
        subagent.get("backend")
        or subagent.get("execution_backend")
        or subagent.get("route_backend")
    ).casefold()
    if configured in {"api", "cli"}:
        preferred = configured
    else:
        preferred = ""
        try:
            from .agent_model_routing import (
                AgentRoutingPins,
                resolve_agent_subagent_route,
            )

            route = resolve_agent_subagent_route(
                config,
                AgentRoutingPins.from_revision(revision),
                subagent_id,
                require_allowed=False,
            )
            route_backend = _text(getattr(route, "backend", "")).casefold()
            if route_backend in {"api", "cli"}:
                preferred = route_backend
        except Exception:
            preferred = ""
    if preferred:
        return (preferred, "cli" if preferred == "api" else "api")
    provider = _text(subagent.get("provider")).casefold()
    preferred = "cli" if provider.endswith("-cli") else "api"
    return (preferred, "api" if preferred == "cli" else "cli")


def _child_capabilities(
    subagent: Mapping[str, Any],
    parent_ceiling: set[str],
    *,
    work_mode: str,
    backend: str,
    active: bool,
) -> tuple[str, ...]:
    if not active:
        return ()
    declared_parent_capabilities = set(_values(subagent.get("capability_ids"))) & parent_ceiling
    try:
        from .agent_team_v3 import filter_agent_team_capabilities

        filtered = set(
            filter_agent_team_capabilities(
                dict(subagent),
                requested=declared_parent_capabilities,
                work_mode=work_mode,
                backend=backend,
            )
        )
    except Exception:
        filtered = set()
    # ``specialist_delegate`` carries the logical workspace markers back into
    # the API bridge after the canonical filter removes native capabilities;
    # mirror that exact carry step before calculating child ToolDefinitions.
    declared = declared_parent_capabilities
    workspace_access = _workspace_access(subagent)
    if backend != "cli":
        if "workspace_read" in declared and workspace_access in {"read", "write"}:
            filtered.add("workspace_read")
        if (
            "workspace_write" in declared
            and workspace_access == "write"
            and work_mode == "write"
        ):
            filtered.add("workspace_write")
    elif "workspace_read" in declared:
        filtered.add("workspace_read")
    # The filter owns the catalog and mode/workspace/backend gates.  This
    # final intersection prevents a future carry rule from widening beyond
    # the immutable parent ceiling.
    return tuple(sorted(filtered & parent_ceiling))


def _fallback_child_tool_names(
    capabilities: Iterable[str],
    *,
    work_mode: str,
    backend: str,
) -> tuple[str, ...]:
    names: set[str] = set()
    caps = set(capabilities)
    if "workspace_read" in caps:
        names.update(
            {
                "read_file",
                "search_files",
                "list_directory",
                "get_workspace_file_info",
                "bm25_search",
            }
        )
    if "repo_map" in caps:
        names.add("get_repo_map")
    if "web_read" in caps:
        names.add("web_search")
    if "docs_read" in caps:
        names.update({"search_docs", "get_doc", "list_docs"})
    if "docs_write" in caps and work_mode == "write":
        names.update({"create_doc", "update_doc", "delete_doc"})
    if "project_read" in caps:
        names.update({"get_project_context", "list_projects", "list_tasks", "get_task"})
    if "project_write" in caps and work_mode == "write":
        names.update({"create_task", "update_task", "delete_task"})
    if backend == "cli":
        # Native filesystem/shell tools are provider-owned and are not child
        # ToolDefinitions; BM25 remains the managed bridge.
        names -= {
            "read_file",
            "search_files",
            "list_directory",
            "get_workspace_file_info",
        }
    return tuple(sorted(names))


def _child_tool_names(
    config: Any,
    subagent: Mapping[str, Any],
    capabilities: tuple[str, ...],
    *,
    work_mode: str,
    backend: str,
) -> tuple[str, ...]:
    if not capabilities:
        return ()
    try:
        from ..llm.specialist_delegate import _agent_team_tools_for_capabilities

        definitions = _agent_team_tools_for_capabilities(
            capabilities,
            tools_required=True,
            work_mode=work_mode,
            backend=backend,
            config=config,
            app_development_team=False,
            app_subagent=dict(subagent),
            project_context=None,
        )
        return tuple(
            sorted(
                {
                    _text(getattr(item, "name", ""))
                    for item in definitions
                    if _text(getattr(item, "name", ""))
                }
            )
        )
    except Exception:
        return _fallback_child_tool_names(
            capabilities,
            work_mode=work_mode,
            backend=backend,
        )


def _child_surfaces(
    config: Any,
    revision: Any,
    subagent_id: str,
    subagent: Mapping[str, Any],
    parent_ceiling: set[str],
    *,
    active: bool,
) -> tuple[str, dict[str, dict[str, tuple[str, ...]]], dict[str, dict[str, tuple[str, ...]]]]:
    backends = _child_backends(config, revision, subagent_id, subagent)
    preferred_backend = backends[0]
    capabilities: dict[str, dict[str, tuple[str, ...]]] = {}
    tool_names: dict[str, dict[str, tuple[str, ...]]] = {}
    for backend in backends:
        modes: dict[str, tuple[str, ...]] = {}
        names: dict[str, tuple[str, ...]] = {}
        for mode in ("read", "write"):
            effective = _child_capabilities(
                subagent,
                parent_ceiling,
                work_mode=mode,
                backend=backend,
                active=active,
            )
            modes[mode] = effective
            names[mode] = _child_tool_names(
                config,
                subagent,
                effective,
                work_mode=mode,
                backend=backend,
            )
        capabilities[backend] = modes
        tool_names[backend] = names
    return preferred_backend, capabilities, tool_names


def _tool_candidates() -> set[str]:
    always, groups, families, _capability_ids_for_tool, _permits, _loaded = _tool_policy_parts()
    candidates: set[str] = set(str(item) for item in always)
    for values in (groups, families):
        if isinstance(values, Mapping):
            for _key, names in values.items():
                if isinstance(names, (set, frozenset, list, tuple)):
                    candidates.update(_text(item) for item in names if _text(item))
    candidates.update({"agent_team_delegate", "delegate_worker"})
    candidates.discard("")
    return candidates


def _tool_names_for(
    *,
    effective_capabilities: set[str],
    allowed_subagent_ids: set[str],
    team_member_ids: set[str],
    delegation_enabled: bool,
    capability_ceiling: set[str],
    tool_ids: list[str] | None,
) -> tuple[str, ...]:
    always, _groups, _families, capability_ids_for_tool, permits, loaded = _tool_policy_parts()
    if not loaded or capability_ids_for_tool is None or permits is None:
        return ()
    visible: set[str] = set()
    normalized_ids = set(_values(tool_ids)) if tool_ids is not None else None
    for name in _tool_candidates():
        definition = SimpleNamespace(name=name, owner="")
        try:
            id_decision = permits(name, definition, tool_ids=normalized_ids)
        except Exception:
            id_decision = False
        if id_decision is False:
            continue
        if name == "agent_team_delegate":
            # Exposure must agree with dispatch: an allowed identity alone is
            # insufficient when every declared capability is outside the
            # revision ceiling.  Legacy delegation also requires the same
            # toggle/member boundary used by the migration projection.
            bounded_allowed = allowed_subagent_ids & team_member_ids
            delegation_surface = delegation_enabled
            if bounded_allowed and delegation_surface and effective_capabilities:
                visible.add(name)
            continue
        if name == "delegate_worker":
            capability_by_kind = {
                "codex": "aoi_tools",
                "antigravity": "aoi_tools",
                "claude": "aoi_tools",
                "grok": "aoi_tools",
                "muse": "remote_agent_delegate",
            }
            # The current P2 policy requires a concrete worker ID.  A legacy
            # NULL list is capability-only for ordinary tools, but it does not
            # authorize the worker dispatch operation itself.
            if tool_ids is not None and any(
                f"delegate_worker:{kind}" in normalized_ids
                and (
                    capability in effective_capabilities
                    or (kind == "muse" and capability in capability_ceiling)
                )
                for kind, capability in capability_by_kind.items()
            ):
                visible.add(name)
            continue
        if name in always:
            visible.add(name)
            continue
        try:
            required = set(capability_ids_for_tool(name, definition))
        except Exception:
            required = set()
        if required and required <= effective_capabilities:
            visible.add(name)
    return tuple(sorted(visible))


def _canonical_effective_tool_ids(
    *,
    effective_capabilities: set[str],
    allowed_subagent_ids: set[str],
    delegation_enabled: bool,
    tool_ids: list[str] | None,
) -> tuple[str, ...]:
    """Return IDs useful for an additive restore comparison.

    Legacy rows have no persisted ID list.  Capability IDs are the stable
    server-owned group IDs that the P2 matcher accepts; a few special groups
    are included only when the corresponding legacy surface could be exposed.
    """

    values = set(effective_capabilities)
    if allowed_subagent_ids and delegation_enabled:
        if effective_capabilities:
            values.add("delegate")
    if "browser_qa" in effective_capabilities:
        values.add("browser")
    if tool_ids is not None:
        # Keep explicit IDs in the projection, including concrete names and
        # worker IDs.  They are needed to make NULL versus [] observable.
        values.update(tool_ids)
    return tuple(sorted(values))


def _tool_id_covers_names(names: set[str], tool_ids: set[str]) -> set[str]:
    """Return names allowed by the same P2 ID/group matcher as runtime."""

    _always, _groups, _families, _capability_ids_for_tool, permits, loaded = _tool_policy_parts()
    if not loaded or permits is None:
        return set()
    covered: set[str] = set()
    for name in names:
        try:
            decision = permits(
                name,
                SimpleNamespace(name=name, owner=""),
                tool_ids=tool_ids,
            )
        except Exception:
            decision = False
        if decision is True:
            covered.add(name)
    return covered


def calculate_revision_authority(
    revision: Any,
    config: Any,
    *,
    mode: str = "legacy",
) -> RevisionAuthority:
    """Calculate the static Agent/Team/Subagent authority for one revision."""

    catalog = _catalog()
    teams, subagents = _known_topology(config)
    team_id = _text(_revision_value(revision, "agent_team_id", "team_id", default=""))
    team = teams.get(team_id) or {}
    team_enabled = team.get("enabled", True) is not False
    team_member_ids = set(_values(team.get("subagent_ids")))
    allowed = set(
        _values(
            _revision_value(
                revision,
                "allowed_subagent_ids",
                "allowed_subagents",
                default=(),
            )
        )
    )
    ceiling = set(
        item
        for item in _values(
            _revision_value(
                revision,
                "capability_ceiling_json",
                "capability_ceiling",
                default=(),
            )
        )
        if item in catalog
    )

    rows: list[SubagentAuthority] = []
    team_capabilities: set[str] = set()
    browser_subagent_ids = {
        "ui_qa_worker",
        "browser",
        "browser_agent",
        "browser_agent_dedicated",
    }
    for subagent_id in sorted(allowed):
        raw = subagents.get(subagent_id) or {}
        enabled = raw.get("enabled", True) is not False
        member = subagent_id in team_member_ids
        declared = set(item for item in _values(raw.get("capability_ids")) if item in catalog)
        effective = declared & ceiling if enabled and team_enabled and member else set()
        if enabled and "browser_qa" in ceiling and subagent_id in browser_subagent_ids:
            effective.add("browser_qa")
        team_capabilities.update(declared if enabled and team_enabled and member else ())
        backend, child_capabilities, child_tool_names = _child_surfaces(
            config,
            revision,
            subagent_id,
            raw,
            ceiling,
            active=bool(enabled and team_enabled and member),
        )
        rows.append(
            SubagentAuthority(
                subagent_id=subagent_id,
                enabled=enabled,
                team_member=member,
                declared_capabilities=tuple(sorted(declared)),
                effective_capabilities=tuple(sorted(effective)),
                workspace_access=_workspace_access(raw),
                backend=backend,
                child_capabilities=child_capabilities,
                child_tool_names=child_tool_names,
            )
        )

    effective_capabilities = ceiling & team_capabilities if team_enabled else set()
    stored_tool_ids = _revision_tool_ids(revision)
    # Browser QA is a parent-issued capability.  An explicitly allowed QA
    # Subagent can carry it even when the opt-in worker is not listed in a
    # default Team roster; the concrete QA guard still owns the parent token.
    if "browser_qa" in ceiling and allowed.intersection(browser_subagent_ids):
        effective_capabilities.add("browser_qa")
    normalized_mode = _text(mode).casefold() or "legacy"
    if normalized_mode == "v2" and stored_tool_ids is not None:
        # For an explicit P2 list the persisted IDs are the effective ID
        # surface.  In particular, ``[]`` must stay an empty deny-all list;
        # do not union capability IDs back into it for reporting.
        effective_tool_ids = tuple(sorted(stored_tool_ids))
    else:
        effective_tool_ids = _canonical_effective_tool_ids(
            effective_capabilities=effective_capabilities,
            allowed_subagent_ids=allowed,
            delegation_enabled=_delegation_enabled(config),
            tool_ids=None,
        )
    tool_names = _tool_names_for(
        effective_capabilities=effective_capabilities,
        allowed_subagent_ids=allowed,
        team_member_ids=team_member_ids,
        delegation_enabled=_delegation_enabled(config),
        capability_ceiling=ceiling,
        tool_ids=stored_tool_ids if normalized_mode == "v2" else None,
    )
    try:
        version = int(_revision_value(revision, "version", default=0) or 0)
    except (TypeError, ValueError):
        version = 0
    return RevisionAuthority(
        mode=normalized_mode,
        agent_id=_text(_revision_value(revision, "agent_id", default="")),
        revision_id=_text(_revision_value(revision, "id", "revision_id", default="")),
        version=version,
        team_id=team_id,
        team_enabled=team_enabled,
        team_member_ids=tuple(sorted(team_member_ids)),
        allowed_subagent_ids=tuple(sorted(allowed)),
        capability_ceiling=tuple(sorted(ceiling)),
        team_capabilities=tuple(sorted(team_capabilities)),
        effective_capabilities=tuple(sorted(effective_capabilities)),
        subagents=tuple(rows),
        tool_ids=tuple(stored_tool_ids) if normalized_mode == "v2" and stored_tool_ids is not None else None,
        effective_tool_ids=effective_tool_ids,
        tool_names=tool_names,
    )


def calculate_legacy_authority(revision: Any, config: Any) -> RevisionAuthority:
    return calculate_revision_authority(revision, config, mode="legacy")


def calculate_v2_authority(revision: Any, config: Any) -> RevisionAuthority:
    return calculate_revision_authority(revision, config, mode="v2")


def compare_revision_authority(
    legacy_revision: Any,
    migrated_revision: Any,
    config: Any,
) -> AuthorityComparison:
    """Compare a legacy row with its P2 immutable successor."""

    before = calculate_legacy_authority(legacy_revision, config)
    after = calculate_v2_authority(migrated_revision, config)
    before_caps = set(before.effective_capabilities)
    after_caps = set(after.effective_capabilities)
    reduced_caps = before_caps - after_caps

    before_ids = set(before.effective_tool_ids)
    after_ids = set(after.tool_ids or ())
    reduced_ids = before_ids - after_ids if after.tool_ids is not None else set()
    before_names = set(before.tool_names)
    after_names = set(after.tool_names)
    reduced_names = before_names - after_names

    # Capabilities already present in the migrated ceiling need no capability
    # patch.  Only capabilities still declared by the current Team (or the
    # explicit parent-issued Browser surface) are safe additive repairs; a
    # Team topology change cannot be repaired by this bounded script.
    supported_after = set(after.team_capabilities)
    if "browser_qa" in after.effective_capabilities:
        supported_after.add("browser_qa")
    restore_caps = reduced_caps & supported_after - set(after.capability_ceiling)
    # A NULL migrated list is still capability-only and therefore has no ID
    # reduction.  Explicit lists use the same stable ID/group matcher as
    # runtime; a group such as ``workspace`` therefore covers the corresponding
    # capability ID without an unnecessary additive patch.
    restore_ids: set[str] = set()
    if after.tool_ids is not None:
        uncovered_names = before_names - _tool_id_covers_names(
            before_names,
            set(after.tool_ids),
        )
        for tool_id in before.effective_tool_ids:
            if tool_id in set(after.tool_ids):
                continue
            required_capability = (
                "browser_qa"
                if tool_id == "browser"
                else None
                if tool_id == "delegate"
                else tool_id
            )
            if required_capability is not None and required_capability not in (
                set(after.effective_capabilities) | set(restore_caps)
            ):
                continue
            if tool_id == "delegate" and not (
                set(after.effective_capabilities) | set(restore_caps)
            ):
                continue
            if uncovered_names and uncovered_names - _tool_id_covers_names(
                uncovered_names,
                set(after.tool_ids) | {tool_id},
            ) != uncovered_names:
                restore_ids.add(tool_id)
    return AuthorityComparison(
        before=before,
        after=after,
        reduced_capability_ids=tuple(sorted(reduced_caps)),
        reduced_tool_ids=tuple(sorted(reduced_ids)),
        reduced_tool_names=tuple(sorted(reduced_names)),
        restore_capability_ids=tuple(sorted(restore_caps)),
        restore_tool_ids=tuple(sorted(restore_ids)),
    )


# Friendly names for focused callers and migration tests.
build_authority_comparison = compare_revision_authority
revision_authority_snapshot = calculate_revision_authority
legacy_effective_permissions = calculate_legacy_authority
v2_effective_permissions = calculate_v2_authority


__all__ = [
    "AuthorityComparison",
    "RevisionAuthority",
    "SubagentAuthority",
    "build_authority_comparison",
    "calculate_legacy_authority",
    "calculate_revision_authority",
    "calculate_v2_authority",
    "compare_revision_authority",
    "legacy_effective_permissions",
    "revision_authority_snapshot",
    "v2_effective_permissions",
]
