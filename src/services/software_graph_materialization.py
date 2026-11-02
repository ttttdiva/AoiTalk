"""Materialize host-owned Software Graph tasks in the adopting transaction."""
from __future__ import annotations

import hashlib
from uuid import uuid4

from ..memory.models import AgentWorkItem, ConversationSession
from .agent_delegation_service import DelegationError, delegation_uuid, native_binding
from .harness_execution_scope_service import _project_root, _protected_roots, _workspace_root
from .local_agent_tasks import LocalAgentTaskError, get_local_agent_task_manager
from .software_graph_binding import prepare_host_software_graph_binding


async def _source_refs(session, planner, order, task, canonical):
    from .agent_team_software_contract import resolve_software_graph_input_refs

    return await resolve_software_graph_input_refs(session, order, task, service=planner.service)


async def prepare_native_children(session, planner, order, output, children):
    """Prepare gated Work; the caller commits before background reconciliation."""
    manager = get_local_agent_task_manager(planner.service.identity.config)
    canonical = getattr(manager, "canonical", None)
    if canonical is None:
        raise DelegationError("delegation_native_runtime_unavailable", 409)
    if any(task.depends_on for task in output.tasks):
        # Dependencies must be settled before activation, not merely linked.
        raise DelegationError("delegation_native_dependencies_unsupported", 409)
    tasks = {task.key: task for task in output.tasks}
    for key, child in children:
        task = tasks[key]
        conversation = ConversationSession(id=uuid4(), user_id=str(order.created_by),
            project_id=child.project_id, character_name="Agent", title=child.title, context={})
        session.add(conversation)
        await session.flush()
        try:
            binding = await prepare_host_software_graph_binding(session, config=planner.service.identity.config,
                delegation_service=planner.service, order=order, task_key=key,
                conversation=conversation, target_project_id=child.project_id)
            root = _workspace_root(planner.service.identity.config)
            workspace = _project_root({"id": str(child.project_id)}, project_id=str(child.project_id),
                workspace=root, protected_roots=_protected_roots(planner.service.identity.config, root))
            refs = await _source_refs(session, planner, order, task, canonical)
            ref = binding.plan_ref
            from .agent_team_software_contract import assemble_software_graph_goal
            goal = await assemble_software_graph_goal(session, child, service=planner.service)
            result, _ = await canonical.submit_software_graph(binding=binding, graph_ref=ref.graph_ref,
                worker_pin=ref.worker_pin, reviewer_pin=ref.reviewer_pin, input_artifact_refs=refs,
                project_workspace=str(workspace),
                goal=goal,
                constraints=[], completion_criteria=[], idempotency_key="software-graph:" + hashlib.sha256(child.idempotency_key.encode()).hexdigest(),
                db_session=session)
            work = await session.get(AgentWorkItem, delegation_uuid(result["work_id"]))
            if work is None:
                raise DelegationError("delegation_native_work_missing", 409)
            child.linked_work_item_id = work.id
            child.linked_work_binding = native_binding(work)
        except LocalAgentTaskError as exc:
            raise DelegationError(exc.code, exc.http_status) from exc
