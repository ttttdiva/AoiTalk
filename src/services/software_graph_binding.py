"""Fixed, project-scoped binding preparation for a future Software Graph adapter.

The caller owns the transaction and supplies a freshly created conversation row.
This function neither creates Work nor starts execution.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from .agent_delegation_service import _CapturedSourceActor
from .agent_delegate_policy import revision_allows_agent_delegate
from .agent_authority import AgentAuthorityResolver
from .conversation_agent_service import ConversationAgentService
from .local_agent_tasks import LocalAgentTaskError


@dataclass(frozen=True)
class SoftwareGraphBinding:
    binding: Any
    project_id: UUID
    owner_user_id: UUID
    source: dict
    role: str = "code_worker"
    plan_ref: "SoftwareGraphPlanRef | None" = None


async def prepare_software_graph_binding(
    session: Any, *, config: Any, captured: _CapturedSourceActor,
    worker_agent_id: UUID, worker_revision_id: UUID, target_project_id: UUID,
    conversation: Any,
) -> SoftwareGraphBinding:
    """Validate server-captured provenance and persist the exact worker snapshot.

    Inputs identifying the worker must come from an already authorized Graph;
    this seam does not itself authorize a Graph or allocate a conversation.
    The caller must commit or roll back the encompassing transaction.
    """
    from ..memory.models import Agent, AgentRevision, ConversationSession
    from ..memory.project_repository import ProjectRepository

    def deny(code: str) -> None:
        raise LocalAgentTaskError(code, "Software Graph binding authority denied", 403)

    if not isinstance(captured, _CapturedSourceActor):
        deny("source_invalid")
    try:
        project = UUID(str(target_project_id))
        worker = UUID(str(worker_agent_id))
        revision_id = UUID(str(worker_revision_id))
        owner = UUID(str(captured.source["inherited_user_id"]))
    except (ValueError, TypeError, KeyError, AttributeError):
        deny("source_invalid")
    source = captured.source
    run, work = captured.run, captured.work
    if (captured.project_id != project or run.project_id != project or work.project_id != project
            or str(run.user_id) != str(owner) or str(captured.actor.id) != str(owner)
            or str(run.agent_id) != str(captured.caller.id)
            or str(run.agent_revision_id) != str(captured.revision.id)
            or str(captured.revision.agent_id) != str(captured.caller.id)
            or str(source.get("source_run_id")) != str(run.id)
            or str(source.get("source_work_item_id")) != str(work.id)
            or str(source.get("source_session_id")) != str(run.session_id)
            or str(source.get("source_agent_id")) != str(captured.caller.id)
            or str(source.get("source_revision_id")) != str(captured.revision.id)):
        deny("source_scope_mismatch")
    if not await ProjectRepository.has_permission(session, project, owner, "manage_members"):
        deny("source_project_denied")
    # Reload policy and ownership so stale captured ORM state cannot authorize a target.
    caller = await session.get(Agent, captured.caller.id, populate_existing=True)
    caller_revision = await session.get(AgentRevision, captured.revision.id, populate_existing=True)
    target = await session.get(Agent, worker, populate_existing=True)
    if (caller is None or caller_revision is None or target is None
            or caller_revision.agent_id != caller.id
            or not revision_allows_agent_delegate(
                caller_revision, tool_name="delegate_plan", target_agent_id=worker,
                target_agent_owner_id=target.created_by, caller_agent_owner_id=caller.created_by,
                owner_user_id=owner)):
        deny("delegate_policy_denied")
    row = await session.get(ConversationSession, conversation.id, populate_existing=True)
    if (row is None or row.project_id != project or str(row.user_id) != str(owner)
            or row.context):
        deny("binding_session_mismatch")
    agents = ConversationAgentService(config=config)
    agent, revision = await agents._agent_for_selection(
        session, agent_id=worker, actor_id=owner, actor_role=None,
        project_id=project, revision_id=revision_id)
    decision = await AgentAuthorityResolver(config=config).resolve(
        session=session, agent_id=worker, revision_id=revision_id,
        project_id=project, required_capability="workspace_read")
    if not decision.allowed:
        deny("target_authority_denied")
    binding = await agents._persist_snapshot(
        session, row=row, agent=agent, revision=revision, actor_id=owner, config=config)
    return SoftwareGraphBinding(binding=binding, project_id=project, owner_user_id=owner,
                                source=dict(source))


@dataclass(frozen=True)
class SoftwareGraphPlanRef:
    order_id: str
    artifact_id: str
    sha256: str
    task_key: str
    producer_work_id: str
    producer_run_id: str
    root_work_id: str
    causal_depth: int
    graph_ref: dict
    worker_pin: dict
    reviewer_pin: dict
    input_artifact_refs: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {key: getattr(self, key) for key in ("order_id", "artifact_id", "sha256",
            "task_key", "producer_work_id", "producer_run_id")}


async def prepare_host_software_graph_binding(
    session: Any, *, config: Any, delegation_service: Any, order: Any,
    task_key: str, conversation: Any, target_project_id: UUID,
) -> SoftwareGraphBinding:
    """Bind a materialized plan task without reacquiring the source Run lease.

    The caller owns the transaction; order and conversation must be persisted.
    Graph and Agent pins are derived only from the saved, adopted plan.
    """
    from sqlalchemy import select
    from ..memory.models import AgentDelegation, ConversationSession, Project
    from .agent_delegation_service import AgentDelegationService, adopted_artifact, phase_items
    from .agent_delegation_planning import DelegationPlanOutput, DelegationPlanner, policy, read_artifact_payload

    def deny():
        raise LocalAgentTaskError("plan_binding_denied", "Adopted plan binding authority denied", 403)

    if not isinstance(delegation_service, AgentDelegationService) or not isinstance(order, AgentDelegation):
        deny()
    order = (await session.execute(select(AgentDelegation).where(
        AgentDelegation.id == order.id).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if order is None or order.cancelled_at or order.planning_error_code:
        deny()
    items = await phase_items(session, order)
    work = items.get("plan")
    artifact = await adopted_artifact(session, work, order, "plan")
    if (artifact is None
            or (order.adopted_plan_artifact_id is not None and order.adopted_plan_artifact_id != artifact.id)
            or (order.adopted_plan_sha256 is not None and order.adopted_plan_sha256 != artifact.sha256)):
        deny()
    try:
        approved = policy(order)
        output = DelegationPlanOutput.model_validate(read_artifact_payload(artifact)["output"])
        await DelegationPlanner(delegation_service).validate_tasks(
            session, order, output, adopted_artifact=artifact if order.adopted_plan_artifact_id else None)
        task = next(task for task in output.tasks if task.key == task_key)
        graph = approved.graph_constraints
        roster = {entry.key: entry for entry in approved.roster}
        worker, reviewer = roster[task.worker_key], roster[task.reviewer_key]
        scope = order.planning_policy_json["scopes"][task.scope_key]
        project = UUID(str(target_project_id))
        if (graph is None or (task.worker_key, task.reviewer_key) not in graph.allowed_pairs
                or UUID(str(scope["project_id"])) != project or not reviewer.can_review):
            deny()
        nodes = {node.key: node for node in graph.roster}
        for entry in (worker, reviewer):
            node = nodes[entry.key]
            if node.agent_id != entry.agent_id or node.revision_id != entry.agent_revision_id:
                deny()
    except (ValueError, TypeError, KeyError, AttributeError, StopIteration):
        deny()
    project_row = await session.get(Project, project, populate_existing=True)
    if project_row is None or project_row.owner_id != order.created_by:
        deny()
    await delegation_service.validate_authority(session, order)
    source = await delegation_service.verified_source_origin(session, order)
    owner = order.created_by
    row = await session.get(ConversationSession, conversation.id, populate_existing=True)
    if row is None or row.project_id != project or str(row.user_id) != str(owner) or row.context:
        deny()
    agents = ConversationAgentService(config=config)
    selected = []
    for entry in (worker, reviewer):
        agent, revision = await agents._agent_for_selection(session, agent_id=entry.agent_id,
            actor_id=owner, actor_role=None, project_id=project, revision_id=entry.agent_revision_id)
        decision = await AgentAuthorityResolver(config=config).resolve(session=session,
            agent_id=entry.agent_id, revision_id=entry.agent_revision_id, project_id=project,
            required_capability="workspace_read")
        if not decision.allowed:
            deny()
        selected.append((agent, revision))
    binding = await agents._persist_snapshot(session, row=row, agent=selected[0][0],
        revision=selected[0][1], actor_id=owner, config=config)
    from .agent_team_software_contract import resolve_software_graph_input_refs
    input_refs = await resolve_software_graph_input_refs(session, order, task, service=delegation_service)
    ref = SoftwareGraphPlanRef(order_id=str(order.id), artifact_id=str(artifact.id),
        sha256=artifact.sha256, task_key=task.key, producer_work_id=str(work.id),
        producer_run_id=str(artifact.agent_run_id), root_work_id=str(work.root_work_item_id or work.id),
        causal_depth=int(work.causal_depth or 0) + 1,
        graph_ref={"graph_id": str(graph.graph_id), "graph_revision_id": str(graph.graph_revision_id),
                   "graph_hash": graph.graph_hash, "group_key": graph.group_key},
        worker_pin={"agent_id": str(worker.agent_id), "agent_revision_id": str(worker.agent_revision_id)},
        reviewer_pin={"agent_id": str(reviewer.agent_id), "agent_revision_id": str(reviewer.agent_revision_id)},
        input_artifact_refs=input_refs)
    return SoftwareGraphBinding(binding=binding, project_id=project, owner_user_id=owner,
                                source=source, plan_ref=ref)
