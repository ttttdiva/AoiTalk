"""Admission of a saved graph snapshot into an existing delegation plan."""
from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from sqlalchemy import select

from ..memory.models import Agent, AgentOrganizationProfile, AgentRevision
from .agent_identity_service import (
    AgentIdentityValidationError, _delegate_policy, _model_overrides, _tool_ids,
)
from .agent_model_routing import AgentModelRouteError, AgentRoutingPins, resolve_agent_role_route, resolve_native_code_route
from .agent_team_graph_compiler import CompiledGroup, GraphCompileError, PinnedAgent, compile_group, compile_media_group, compile_software_group
from .agent_team_graph_schema import GraphDefinition
from .agent_team_graph_service import AgentTeamGraphError, AgentTeamGraphService


class TeamGraphRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    graph_id: UUID
    revision_id: UUID
    group_key: str = Field(min_length=1, max_length=64)


def _ready_route(config, revision, role):
    """Apply the same route resolver and bounded executor restrictions as runtime."""
    try:
        route = resolve_agent_role_route(config, AgentRoutingPins.from_revision(revision), role)
    except AgentModelRouteError:
        return False
    return not route.acting_subagent_id and route.provider not in {
        "routing-profile", "claude-cli", "antigravity-cli", "grok-cli", "codex-cli",
    }


def _ready_native_code_route(config, revision, owner):
    """Check the exact route and deployment policy used by native execution."""
    from .codex_development_runtime import CodexDevelopmentRuntimeError, CodexNativeWorkerPolicy

    try:
        route = resolve_native_code_route(config, AgentRoutingPins.from_revision(revision))
        if route.acting_subagent_id or route.route_source == "inherit_chat" or route.provider != "codex-cli":
            return False
        native_policy = CodexNativeWorkerPolicy.from_deployment()
        if not native_policy.sealed:
            return False
        native_policy._check_context(owner_id=str(owner), provider=route.provider,
                                     model=route.model or "", effort=route.effort or "",
                                     role="code_worker", mode="mutate")
        return True
    except (AgentModelRouteError, CodexDevelopmentRuntimeError):
        return False


def _complete_revision(revision):
    # Validate raw values: the runtime readers intentionally coerce malformed
    # persisted shapes to empty projections and cannot establish completeness.
    values = (revision.model_overrides, revision.tool_ids, revision.delegate_policy)
    if not isinstance(values[0], dict) or not isinstance(values[1], list) or not isinstance(values[2], dict):
        return False
    try:
        _model_overrides(values[0])
        _tool_ids(values[1], allow_concrete=True)
        _delegate_policy(values[2])
    except (AgentIdentityValidationError, TypeError, ValueError):
        return False
    return True


async def admit_team_graph(session, *, ref, owner, project_id, leader_id, leader_revision_id, planning, config=None, authorization=None):
    """Resolve one historical snapshot and pin its selected group in this session.

    A future root delegate may pass the same typed ref to its selected leader;
    that leader must call this admission with its own current run identity.
    This helper does not dispatch or authorize cross-group execution.
    """
    from .agent_delegation_planning import DelegationPlanningPolicy
    from .agent_delegation_service import DelegationError

    ref = TeamGraphRef.model_validate(ref)
    try:
        envelope = await AgentTeamGraphService().get_in_session(
            session, ref.graph_id, actor_user_id=owner, revision_id=ref.revision_id)
        if envelope["project_id"] is not None and envelope["project_id"] != str(project_id):
            raise DelegationError("delegation_graph_project_mismatch", 403)
        if authorization is not None and (
                authorization.get("ref") != ref.model_dump(mode="json")
                or authorization.get("graph_hash") != envelope["content_hash"]
                or (authorization.get("target_project_id") is not None
                    and str(authorization["target_project_id"]) != str(project_id))):
            raise DelegationError("delegation_graph_authorization_mismatch", 403)
        definition = GraphDefinition.model_validate(envelope["definition"])
        group = next((g for g in definition.groups if g.key == ref.group_key), None)
        if group is None or not group.leader_key or not definition.root_key:
            raise GraphCompileError("group, leader or root missing")
        nodes = {node.key: node for node in definition.nodes}
        leader = nodes[group.leader_key]
        if leader.agent_id != leader_id or (leader.revision_id and leader.revision_id != leader_revision_id):
            raise DelegationError("delegation_graph_leader_mismatch", 403)
        pins, agents, revisions, loaded_revisions = {}, {}, {}, {}
        relevant = set(group.node_keys) | {definition.root_key}
        stored_pins = authorization.get("nodepins") if authorization is not None else None
        if ((authorization is not None and group.execution_kind == "software_code" and stored_pins is None)
                or (stored_pins is not None and (not isinstance(stored_pins, dict) or set(stored_pins) != relevant))):
            raise DelegationError("delegation_graph_authorization_mismatch", 403)
        for key in relevant:
            node = nodes[key]
            if node.agent_id not in agents:
                agents[node.agent_id] = await session.get(Agent, node.agent_id)
            agent = agents[node.agent_id]
            if agent is None or agent.created_by != owner or agent.state != "active":
                raise DelegationError("delegation_graph_agent_invalid", 403)
            rid = node.revision_id
            if stored_pins is not None:
                pin = stored_pins[key]
                try:
                    if not isinstance(pin, dict) or UUID(str(pin.get("agent_id"))) != node.agent_id:
                        raise ValueError("agent mismatch")
                    rid = UUID(str(pin.get("revision_id")))
                except (ValueError, TypeError, AttributeError):
                    raise DelegationError("delegation_graph_authorization_mismatch", 403) from None
                if (node.revision_id and rid != node.revision_id) or (key == leader.key and rid != leader_revision_id):
                    raise DelegationError("delegation_graph_authorization_mismatch", 403)
            elif key == leader.key:
                rid = leader_revision_id
            elif rid is None:
                if node.agent_id not in revisions:
                    revisions[node.agent_id] = await session.scalar(select(AgentRevision).where(
                        AgentRevision.agent_id == node.agent_id).order_by(AgentRevision.version.desc()).limit(1))
                revision = revisions[node.agent_id]
                rid = revision.id if revision else None
            if rid and rid not in loaded_revisions:
                loaded_revisions[rid] = await session.get(AgentRevision, rid)
            revision = loaded_revisions.get(rid)
            media_worker = group.execution_kind in {"media_image", "media_video"} and node.role == "worker"
            if (revision is None or revision.agent_id != node.agent_id
                    or (not media_worker and not _complete_revision(revision))):
                raise DelegationError("delegation_graph_revision_invalid", 403)
            pins[key] = PinnedAgent(node.agent_id, rid)
        if not isinstance(planning, dict) or "roster" in planning or "graph_constraints" in planning:
            raise DelegationError("delegation_graph_constraints_server_only", 403)
        if group.execution_kind == "software_code":
            from .agent_authority import AgentAuthorityResolver
            from .agent_delegate_policy import revision_allows_agent_delegate
            from .agent_revision_v2 import revision_tool_ids

            compiled = compile_software_group(envelope, ref.group_key, pins)
            worker_key, reviewer_key = compiled.allowed_pairs[0]
            if (len(compiled.roster) != 3 or
                    {node.key for node in compiled.roster} != {leader.key, worker_key, reviewer_key}):
                raise DelegationError("delegation_graph_invalid", 403)
            leader_revision = loaded_revisions[leader_revision_id]
            tools = revision_tool_ids(leader_revision)
            if tools is None or "delegate_plan" not in tools:
                raise DelegationError("delegation_graph_plan_tool_unavailable", 403)
            if not _ready_route(config, leader_revision, "planner"):
                raise DelegationError("delegation_graph_route_unavailable", 403)
            nodes_by_key = {node.key: node for node in compiled.roster}
            worker, reviewer = nodes_by_key[worker_key], nodes_by_key[reviewer_key]
            if not _ready_native_code_route(config, loaded_revisions[worker.revision_id], owner):
                raise DelegationError("delegation_graph_route_unavailable", 403)
            if not _ready_route(config, loaded_revisions[reviewer.revision_id], "reviewer"):
                raise DelegationError("delegation_graph_route_unavailable", 403)
            authority = AgentAuthorityResolver(config=config)
            for node, capabilities in ((worker, ("workspace_write", "command_execute")),
                                       (reviewer, ("workspace_read",))):
                agent = agents[node.agent_id]
                if not revision_allows_agent_delegate(leader_revision, tool_name="delegate_plan",
                        target_agent_id=agent.id, target_agent_owner_id=agent.created_by,
                        caller_agent_owner_id=agents[leader.agent_id].created_by, owner_user_id=owner):
                    raise DelegationError("delegate_policy_denied", 403)
                if await session.get(AgentOrganizationProfile, node.agent_id) is None:
                    raise DelegationError("delegation_employment_profile_missing", 403)
                for capability in capabilities:
                    decision = await authority.resolve(session=session, agent_id=node.agent_id,
                        revision_id=node.revision_id, project_id=project_id, persona_id=None,
                        required_capability=capability)
                    if not decision.allowed:
                        raise DelegationError("delegation_authority_denied", 403)
            requested = dict(planning)
            requested["roster"] = [{"key": node.key, "agent_id": str(node.agent_id),
                "agent_revision_id": str(node.revision_id),
                "allowed_work_roles": ["code_worker"] if node.role == "worker" else [],
                "scope_keys": ["current"], "can_review": node.role == "reviewer", "can_delegate": False}
                for node in (worker, reviewer)]
            requested["graph_constraints"] = compiled
            result = DelegationPlanningPolicy.model_validate(requested).model_dump(mode="json")
            return {**result, "execution_kind": "software_code"}
        if group.execution_kind in {"media_image", "media_video"}:
            from .agent_authority import AgentAuthorityResolver
            from .agent_delegate_policy import revision_allows_agent_delegate
            from .agent_revision_v2 import revision_tool_ids

            media = compile_media_group(envelope, ref.group_key, pins)
            compiled = media.graph
            if not _ready_route(config, loaded_revisions[leader_revision_id], "planner"):
                raise DelegationError("delegation_graph_route_unavailable", 403)
            nodes_by_key = {node.key: node for node in compiled.roster}
            worker_key, reviewer_key = compiled.allowed_pairs[0]
            leader_revision = loaded_revisions[leader_revision_id]
            leader_agent = agents[leader.agent_id]
            if (len(compiled.roster) != 3 or
                    {node.key for node in compiled.roster} != {leader.key, worker_key, reviewer_key}):
                raise DelegationError("delegation_graph_invalid", 403)
            tools = revision_tool_ids(leader_revision)
            if tools is not None and "delegate_media" not in tools:
                raise DelegationError("delegation_graph_media_tool_unavailable", 403)
            authority = AgentAuthorityResolver(config=config)
            for key, capability in ((worker_key, "media"), (reviewer_key, "workspace_read")):
                node = nodes_by_key[key]
                agent = agents[node.agent_id]
                if not revision_allows_agent_delegate(leader_revision, tool_name="delegate_media",
                        target_agent_id=agent.id, target_agent_owner_id=agent.created_by,
                        caller_agent_owner_id=leader_agent.created_by, owner_user_id=owner):
                    raise DelegationError("delegate_policy_denied", 403)
                decision = await authority.resolve(session=session, agent_id=node.agent_id,
                    revision_id=node.revision_id, project_id=project_id, persona_id=None,
                    required_capability=capability)
                # Media requires a Persona, which is not chosen at this boundary.
                # All other current authority denials remain effective here.
                if not decision.allowed and set(decision.deny_reasons) != {"persona_scope_required"}:
                    raise DelegationError("delegation_authority_denied", 403)
            reviewer = nodes_by_key[reviewer_key]
            if await session.get(AgentOrganizationProfile, reviewer.agent_id) is None:
                raise DelegationError("delegation_employment_profile_missing", 403)
            if not _ready_route(config, loaded_revisions[reviewer.revision_id], "reviewer"):
                raise DelegationError("delegation_graph_route_unavailable", 403)
            # Persona-specific authority is checked after the leader selects a recipe.
            return {"execution_kind": media.execution_kind,
                    "graph_constraints": TypeAdapter(CompiledGroup).dump_python(compiled, mode="json")}
        compiled = compile_group(envelope, ref.group_key, pins)
        if not _ready_route(config, loaded_revisions[leader_revision_id], "planner"):
            raise DelegationError("delegation_graph_route_unavailable", 403)
        requested = dict(planning)
        requested["roster"] = []
        for node in compiled.roster:
            if node.role not in {"worker", "reviewer"}:
                continue
            revision = loaded_revisions[node.revision_id]
            roles = [role for role in ("researcher", "writer", "data_worker", "general_worker")
                     if _ready_route(config, revision, role)] if node.role == "worker" else []
            if (node.role == "worker" and not roles) or (
                    node.role == "reviewer" and not _ready_route(config, revision, "reviewer")):
                raise DelegationError("delegation_graph_route_unavailable", 403)
            requested["roster"].append({"key": node.key, "agent_id": str(node.agent_id),
                "agent_revision_id": str(node.revision_id), "allowed_work_roles": roles,
                "scope_keys": ["current"], "can_review": node.role == "reviewer", "can_delegate": False})
        requested["graph_constraints"] = compiled
        try:
            return DelegationPlanningPolicy.model_validate(requested).model_dump(mode="json")
        except ValidationError as exc:
            error = DelegationError("delegation_planning_invalid")
            error.planning_validation_error = exc
            raise error from None
    except (AgentTeamGraphError, GraphCompileError) as exc:
        raise DelegationError("delegation_graph_invalid", 403) from exc
