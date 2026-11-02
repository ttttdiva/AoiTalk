"""Resolve cross-group inputs from persisted, validated Dialogue returns."""

from uuid import UUID

from ..memory.models import AgentDelegation, AgentDialogue
from .agent_delegation_service import DelegationError, adopted_artifact, phase_items
from .agent_team_case_readiness import resolve_case_readiness
from .agent_team_graph_schema import GraphDefinition
from .agent_team_handoff_binder import Edge, HandoffBindingError, VerifiedOutput, bind_verified_handoffs


async def resolve_group_handoff_inputs(session, *, service, owner_user_id, root_session_id, case_key, graph, target_group):
    return await _resolve_group_handoff_inputs(session, service=service, owner_user_id=owner_user_id,
        root_session_id=root_session_id, case_key=case_key, graph=graph, target_group=target_group, active=False)


async def _resolve_group_handoff_inputs(session, *, service, owner_user_id, root_session_id, case_key, graph,
                                        target_group, active):
    definition = GraphDefinition.model_validate(graph["definition"])
    project_id = UUID(str(graph["case_project_id"]))
    readiness = await resolve_case_readiness(session, service=service, owner_user_id=owner_user_id,
        root_session_id=root_session_id, case_key=case_key, graph=graph)
    allowed = readiness["running"] if active else readiness["ready"]
    if target_group not in allowed:
        raise DelegationError("team_graph_group_not_ready", 409)
    memberships = {}
    for group in definition.groups:
        for key in group.node_keys:
            memberships.setdefault(key, []).append(group.key)
    edges = []
    for edge in definition.edges:
        if edge.kind != "handoff":
            continue
        source, target = memberships.get(edge.source, []), memberships.get(edge.target, [])
        if target_group not in target:
            continue
        if len(source) != 1 or len(target) != 1:
            raise DelegationError("team_graph_handoff_ambiguous", 403)
        if source[0] != target[0]:
            edges.append(Edge(edge.key, source[0], edge.source, target[0], edge.output_key))
    nodes = {node.key: node for node in definition.nodes}
    outputs = []
    seen_sources = set()
    for edge in edges:
        source_key = (edge.source_group, edge.source_node)
        if source_key in seen_sources:
            continue
        seen_sources.add(source_key)
        refs = readiness["group_dialogue_refs"].get(edge.source_group, [])
        if len(refs) != 1:
            raise DelegationError("team_graph_handoff_invalid", 403)
        dialogue = await session.get(AgentDialogue, UUID(refs[0]))
        auth = dialogue.authorization_json["team_graph"]
        pin = auth.get("nodepins", {}).get(edge.source_node, {})
        node = nodes[edge.source_node]
        if (dialogue.project_id != project_id or pin.get("agent_id") != str(node.agent_id)
                or not pin.get("revision_id") or (node.revision_id and pin["revision_id"] != str(node.revision_id))):
            raise DelegationError("team_graph_handoff_invalid", 403)
        for proof in readiness["accepted_outputs"].get(edge.source_group, []):
            order = await session.get(AgentDelegation, UUID(proof["order_id"]))
            if (order is None or order.created_by != owner_user_id or order.project_id != project_id
                    or str(order.worker_agent_id) != pin["agent_id"]
                    or str(order.worker_revision_id) != pin["revision_id"]):
                continue
            items = await phase_items(session, order)
            work = await adopted_artifact(session, items.get("work"), order, "work")
            review = await adopted_artifact(session, items.get("review"), order, "review")
            if work is None or review is None or review.accepted != 1:
                raise DelegationError("team_graph_handoff_invalid", 403)
            provenance = {"return_proof": proof, "review_artifact_id": str(review.id),
                "review_sha256": review.sha256, "review_work_item_id": str(review.work_item_id),
                "review_agent_run_id": str(review.agent_run_id), "reviewer_agent_id": str(order.reviewer_agent_id),
                "reviewer_revision_id": str(order.reviewer_revision_id), "producer_agent_id": str(order.worker_agent_id),
                "producer_revision_id": str(order.worker_revision_id), "source_dialogue_id": str(dialogue.id),
                "case_key": case_key, "graph_revision_id": str(graph["revision_id"]), "graph_hash": graph["content_hash"],
                "persona_id": str(order.persona_id) if order.persona_id else None}
            outputs.append(VerifiedOutput(edge.source_group, edge.source_node, work.id, work.sha256,
                work.work_item_id, work.agent_run_id, order.worker_agent_id, order.worker_revision_id,
                review.id, order.reviewer_agent_id, case_key, UUID(str(graph["revision_id"])), owner_user_id,
                project_id, provenance))
    try:
        bound = bind_verified_handoffs(target_group=target_group, case_key=case_key,
            graph_revision_id=UUID(str(graph["revision_id"])), owner_id=owner_user_id, project_id=project_id,
            edges=edges, outputs=outputs)
    except HandoffBindingError as exc:
        raise DelegationError(exc.code, 409) from exc
    return {key: {"artifact_id": str(value.artifact_id), "sha256": value.sha256,
        "work_item_id": str(value.work_id), "agent_run_id": str(value.run_id),
        "source_order_id": value.provenance["return_proof"]["order_id"], "project_id": str(project_id),
        "persona_id": value.provenance["persona_id"],
        "handoff_provenance": value.provenance} for key, value in bound.items()}
