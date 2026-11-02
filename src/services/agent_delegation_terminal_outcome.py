"""Read-only proof of terminal delegation outcomes for a future transport."""

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json

from sqlalchemy import select

from ..memory.models import AgentDelegation, AgentDialogue, AgentDialogueDelivery, AgentRun, AgentWorkItem
from ..memory.models.agent_work import AgentWorkEvent
from ..memory.models.conversations import ConversationMessage
from .agent_delegation_dialogue_return import (
    _invalid, _verify_phase_binding, original_source_run_work, resolve_delegation_return,
)
from .agent_delegation_planning import active_children, policy
from .agent_delegation_service import (
    DelegationError, adopted_artifact, canonical, delegation_state, delegation_uuid, phase_identity,
)


@dataclass(frozen=True)
class DelegationTerminalOutcome:
    body: str

    @property
    def sha256(self):
        return hashlib.sha256(self.body.encode("utf-8")).hexdigest()

    def as_dict(self):
        return json.loads(self.body)


async def _origin(session, service, order, *, allow_acked_replay=False):
    await service.verified_source_origin(session, order)
    await service.validate_authority(session, order, _terminal_result_proof=True)
    run, work = await original_source_run_work(session, order)
    dialogue = await session.scalar(select(AgentDialogue).where(AgentDialogue.worker_session_id == run.session_id))
    if (dialogue is None or dialogue.state not in ({"queued", "running", "waiting", "failed", "cancelled", "blocked"} if allow_acked_replay else {"queued", "running", "waiting"})
            or (not allow_acked_replay and dialogue.deadline is not None and dialogue.deadline <= datetime.utcnow())):
        raise _invalid()
    metadata = (run.run_metadata or {}).get("agent_dialogue") or {}
    try:
        incoming = await session.get(AgentDialogueDelivery, delegation_uuid(metadata["delivery_id"]), populate_existing=True)
    except (KeyError, TypeError, DelegationError):
        raise _invalid() from None
    if (incoming is None or incoming.dialogue_id != dialogue.id
            or incoming.receiver_run_id != run.id or incoming.receiver_work_item_id != work.id
            or incoming.kind not in {"request", "control"}
            or incoming.expected_control_revision != dialogue.control_revision):
        raise _invalid()
    return dialogue, incoming


async def _work_evidence(session, service, order, phase, work, *, artifact=None):
    if work is None or work.project_id != order.project_id or work.persona_id != order.persona_id:
        raise _invalid()
    actor, revision = phase_identity(order, phase)
    if work.assigned_agent_id != actor or work.agent_revision_id != revision:
        raise _invalid()
    if artifact is not None:
        run = await session.get(AgentRun, artifact.agent_run_id, populate_existing=True)
    else:
        event = await session.scalar(select(AgentWorkEvent).where(
            AgentWorkEvent.work_item_id == work.id).order_by(AgentWorkEvent.sequence.desc()).limit(1))
        if (event is None or event.to_state != work.state or event.event_type != f"work.{work.state}"
                or event.safe_error_code != work.safe_error_code
                or (work.active_agent_run_id is not None and work.active_agent_run_id != event.agent_run_id)):
            raise _invalid()
        run = await session.get(AgentRun, event.agent_run_id, populate_existing=True) if event.agent_run_id else None
        if event.agent_run_id and run is None:
            raise _invalid()
        if run is not None and run.work_item_attempt != work.attempt_count:
            raise _invalid()
        if run is None:
            if event.from_state not in {"pending", "claimed"}:
                raise _invalid()
            existing = await session.scalar(select(AgentRun.id).where(
                AgentRun.work_item_id == work.id, AgentRun.work_item_attempt == work.attempt_count).limit(1))
            if existing is not None:
                raise _invalid()
    if run is not None:
        if (run.work_item_id != work.id or run.project_id != order.project_id
                or run.agent_id != actor or run.agent_revision_id != revision
                or (artifact is None and run.status not in {"failed", "cancelled"})):
            raise _invalid()
        if (phase == "work" and order.linked_work_item_id == work.id
                and work.source_type == "local_agent_task" and work.execution_adapter == "local_agent_task"):
            from .agent_team_software_contract import validate_software_graph_linked_work

            proof = await validate_software_graph_linked_work(session, order, service=service, phase="execution")
            if proof["work"].id != work.id or proof["run"].id != run.id:
                raise _invalid()
        else:
            await _verify_phase_binding(session, service, order, work, run)
    return {"phase": phase, "work_item_id": str(work.id), "work_state": work.state,
            "run_id": str(run.id) if run else None, "run_status": run.status if run else None,
            "error_code": work.safe_error_code, "blocker_code": work.blocker_code}


async def _leaves(session, service, order, depth=0):
    if depth > 3:
        raise _invalid()
    state, items = await delegation_state(session, order)
    if state == "child_blocked":
        candidates = []
        for child in sorted(await active_children(session, order), key=lambda row: str(row.id)):
            if child.parent_delegation_id != order.id or child.created_by != order.created_by:
                raise _invalid()
            await service.validate_authority(session, child, _terminal_result_proof=True)
            candidates.extend(await _leaves(session, service, child, depth + 1))
        return candidates
    if state == "evidence_missing":
        raise _invalid()
    if state == "changes_requested":
        planning_root = await session.get(AgentDelegation, order.planning_root_id or order.id, populate_existing=True)
        approved = policy(planning_root) if planning_root else None
        if approved and order.round <= approved.max_rework_rounds:
            return []
        artifact = await adopted_artifact(session, items.get("review"), order, "review")
        if artifact is None or artifact.accepted != 0:
            raise _invalid()
        evidence = await _work_evidence(session, service, order, "review", items["review"], artifact=artifact)
        evidence["review_artifact"] = artifact.to_safe_dict()
        return [(order, "changes_requested", evidence)]
    if state == "planning_blocked":
        artifact = await adopted_artifact(session, items.get("plan"), order, "plan")
        if artifact is None:
            raise _invalid()
        evidence = await _work_evidence(session, service, order, "plan", items["plan"], artifact=artifact)
        evidence["plan_artifact"] = artifact.to_safe_dict()
        evidence["planning_error_code"] = order.planning_error_code
        evidence["adopted_plan_artifact_id"] = (
            str(order.adopted_plan_artifact_id) if order.adopted_plan_artifact_id else None)
        evidence["adopted_plan_sha256"] = order.adopted_plan_sha256
        return [(order, state, evidence)]
    if ":" in state:
        phase, status = state.split(":", 1)
        if status in {"blocked", "failed", "dead_letter", "cancelled", "uncertain"}:
            evidence = await _work_evidence(session, service, order, phase, items.get(phase))
            return [(order, status, evidence)]
    if state == "cancelled":
        return [(order, state, None)]
    return []


async def _resolve(session, service, order, *, allow_acked_replay=False):
    dialogue, incoming = await _origin(session, service, order, allow_acked_replay=allow_acked_replay)
    state, _ = await delegation_state(session, order)
    if state == "returned":
        result = await resolve_delegation_return(session, service, order)
        return DelegationTerminalOutcome(result.body)
    leaves = await _leaves(session, service, order)
    if not leaves:
        return None
    causal_order, outcome, evidence = leaves[0]
    causes = [{"causal_order_id": str(cause.id), "causal_round": cause.round,
               "outcome": kind, "evidence": facts} for cause, kind, facts in leaves]
    envelope = {"version": 1, "order_id": str(order.id), "round": order.round,
                "dialogue_id": str(dialogue.id), "control_revision": dialogue.control_revision,
                "origin": dict(order.source_actor_json), "incoming_delivery_id": str(incoming.id),
                "outcome": outcome, "causal_order_id": str(causal_order.id),
                "causal_round": causal_order.round, "evidence": evidence, "causes": causes}
    return DelegationTerminalOutcome(canonical(envelope))


async def delegation_descendants_settled(session, service, order, depth=0):
    """Require every accepted descendant to have a settled, proved outcome."""
    if depth > 3:
        raise _invalid()
    for child in await active_children(session, order):
        if child.parent_delegation_id != order.id or child.created_by != order.created_by:
            raise _invalid()
        await service.validate_authority(session, child, _terminal_result_proof=True)
        if not await delegation_descendants_settled(session, service, child, depth + 1):
            return False
        state, _ = await delegation_state(session, child)
        if state != "returned" and not await _leaves(session, service, child):
            return False
    return True


async def resolve_delegation_terminal_outcome(session, service, order, *, allow_acked_replay=False) -> DelegationTerminalOutcome | None:
    # Queries must not flush unrelated pending ORM changes.
    with session.no_autoflush:
        return await _resolve(session, service, order, allow_acked_replay=allow_acked_replay)


async def validate_delegation_outcome_message(session, service, dialogue, delivery, message, *, allow_acked_replay=False):
    """Reconstruct the negative proof and bind it to its host delivery."""
    dialogue = await session.get(AgentDialogue, dialogue.id, populate_existing=True)
    delivery = await session.get(AgentDialogueDelivery, delivery.id, populate_existing=True)
    message = await session.get(ConversationMessage, message.id, populate_existing=True)
    if dialogue is None or delivery is None or message is None:
        raise _invalid()
    metadata = message.message_metadata if isinstance(message.message_metadata, dict) else {}
    marker = metadata.get("agent_delegation_outcome")
    if (not isinstance(marker, dict) or set(marker) != {"version", "order_id", "round"}
            or marker.get("version") != 1):
        raise _invalid()
    try:
        order = await session.get(AgentDelegation, delegation_uuid(marker["order_id"]), populate_existing=True)
    except DelegationError:
        raise _invalid() from None
    if order is None or marker["round"] != order.round:
        raise _invalid()
    if not await delegation_descendants_settled(session, service, order):
        raise _invalid()
    replay = allow_acked_replay and delivery.state == "acked"
    projection = await resolve_delegation_terminal_outcome(session, service, order, allow_acked_replay=replay)
    if projection is None:
        raise _invalid()
    envelope = projection.as_dict()
    origin_run, origin_work = await original_source_run_work(session, order)
    if ("outcome" not in envelope or str(dialogue.id) != envelope["dialogue_id"]
            or delivery.dialogue_id != dialogue.id
            or dialogue.worker_agent_id != order.delegator_agent_id
            or dialogue.worker_agent_revision_id != order.delegator_revision_id
            or message.deleted_at is not None or message.role != "system" or message.sender_type != "system"
            or message.session_id != dialogue.worker_session_id or delivery.source_message_id != message.id
            or delivery.source_session_id != dialogue.worker_session_id
            or delivery.source_agent_id != dialogue.worker_agent_id
            or delivery.source_agent_revision_id != dialogue.worker_agent_revision_id
            or delivery.source_run_id != origin_run.id or delivery.source_work_item_id != origin_work.id
            or delivery.receiver_session_id != dialogue.root_session_id
            or delivery.receiver_agent_id != dialogue.root_agent_id
            or delivery.receiver_agent_revision_id != dialogue.root_agent_revision_id
            or delivery.kind not in {"response", "event"}
            or str(delivery.causation_id) != envelope["incoming_delivery_id"]
            or delivery.expected_control_revision != dialogue.control_revision
            or delivery.request_digest != projection.sha256 or message.content != projection.body):
        raise _invalid()
    return projection
