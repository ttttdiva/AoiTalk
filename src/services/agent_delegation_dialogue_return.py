"""Database-backed proof of a delegation return for a Dialogue host."""

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json

from sqlalchemy import select

from ..memory.models import AgentDelegation, AgentDialogue, AgentDialogueDelivery, AgentRun, AgentWorkItem
from ..memory.models.conversations import ConversationMessage
from .agent_delegation_planning import read_artifact_payload
from .agent_delegation_service import (
    DelegationError, adopted_artifact, canonical, delegation_state, delegation_uuid, phase_identity, phase_sequence,
)


def _invalid():
    return DelegationError("delegation_return_invalid", 403)


@dataclass(frozen=True)
class DelegationReturn:
    """A canonical immutable envelope, not a transport or an assistant message."""

    body: str

    @property
    def sha256(self):
        return hashlib.sha256(self.body.encode("utf-8")).hexdigest()

    def as_dict(self):
        return json.loads(self.body)


async def original_source_run_work(session, order):
    source = order.source_actor_json
    if not isinstance(source, dict):
        raise _invalid()
    try:
        run = await session.get(AgentRun, delegation_uuid(source["source_run_id"]), populate_existing=True)
        work = await session.get(AgentWorkItem, delegation_uuid(source["source_work_item_id"]), populate_existing=True)
    except (KeyError, DelegationError):
        raise _invalid() from None
    if run is None or work is None or run.work_item_id != work.id:
        raise _invalid()
    return run, work


async def _verify_phase_binding(session, service, order, work, run):
    """Prove the prepared owner binding, or the historical unbound background run."""
    work_metadata = work.metadata_json if isinstance(work.metadata_json, dict) else {}
    run_metadata = run.run_metadata if isinstance(run.run_metadata, dict) else {}
    if run.user_id is None:
        if (run.session_id is not None or work.conversation_session_id is not None
                or any(key in work_metadata or key in run_metadata
                       for key in ("agent_binding", "conversation_session_id", "owner_user_id"))):
            raise _invalid()
        return
    if str(run.user_id) != str(order.created_by) or run.work_item_id != work.id:
        raise _invalid()
    from types import SimpleNamespace
    from .agent_work_runtime import AgentWorkCoordinator
    claim = SimpleNamespace(**{key: getattr(work, key) for key in (
        "source_type", "source_id", "source_revision", "intent_key", "execution_adapter",
        "assigned_agent_id", "agent_revision_id", "project_id")})
    try:
        context = await AgentWorkCoordinator(service.identity._db_manager, config=service.identity.config)._work_binding_context(
            session, work, claim)
        if (work.conversation_session_id != run.session_id
                or str(run.session_id) != context["conversation_session_id"]
                or run.agent_id != work.assigned_agent_id or run.agent_revision_id != work.agent_revision_id
                or any(run_metadata.get(key) != context[key] for key in
                       ("owner_user_id", "conversation_session_id", "agent_binding"))):
            raise _invalid()
    except DelegationError:
        raise
    except (ValueError, TypeError, KeyError):
        raise _invalid() from None


async def resolve_delegation_return(session, service, order, *, allow_acked_replay=False):
    await service.verified_source_origin(session, order)
    await service.validate_authority(session, order)
    origin_run, origin_work = await original_source_run_work(session, order)
    dialogue = await session.scalar(select(AgentDialogue).where(AgentDialogue.worker_session_id == origin_run.session_id))
    if (dialogue is None or dialogue.state not in ({"queued", "running", "waiting", "succeeded", "failed", "cancelled", "blocked"} if allow_acked_replay else {"queued", "running", "waiting"})
            or (not allow_acked_replay and dialogue.deadline is not None and dialogue.deadline <= datetime.utcnow())):
        raise _invalid()
    metadata = (origin_run.run_metadata or {}).get("agent_dialogue") or {}
    try:
        incoming = await session.get(AgentDialogueDelivery, delegation_uuid(metadata["delivery_id"]), populate_existing=True)
    except (KeyError, TypeError, DelegationError):
        raise _invalid() from None
    if (incoming is None or incoming.dialogue_id != dialogue.id
            or incoming.receiver_run_id != origin_run.id or incoming.receiver_work_item_id != origin_work.id
            or incoming.kind not in {"request", "control"}
            or incoming.expected_control_revision != dialogue.control_revision):
        raise _invalid()
    state, items = await delegation_state(session, order)
    if state != "returned":
        raise _invalid()
    artifacts = {}
    native_evidence = None
    for phase in phase_sequence(order):
        if phase == "work" and order.linked_work_item_id:
            from .agent_delegation_runtime import native_work_evidence
            native_evidence = await native_work_evidence(session, order, items[phase], config=service.identity.config)
            continue
        artifact = await adopted_artifact(session, items.get(phase), order, phase)
        if artifact is None or (phase == "review" and artifact.accepted != 1):
            raise _invalid()
        phase_work = items[phase]
        phase_run = await session.get(AgentRun, artifact.agent_run_id, populate_existing=True)
        actor, revision = phase_identity(order, phase)
        if (phase_run is None
                or phase_run.project_id != order.project_id or phase_work.project_id != order.project_id
                or phase_work.persona_id != order.persona_id or phase_work.assigned_agent_id != actor
                or phase_work.agent_revision_id != revision):
            raise _invalid()
        await _verify_phase_binding(session, service, order, phase_work, phase_run)
        payload = read_artifact_payload(artifact)
        artifacts[phase] = (artifact, payload)
    artifact, payload = artifacts["return"]
    if not isinstance(payload, dict) or "output" not in payload:
        raise _invalid()
    output = payload["output"]
    if len(canonical(output).encode("utf-8")) > 65536:
        raise _invalid()
    run = await session.get(AgentRun, artifact.agent_run_id, populate_existing=True)
    envelope = {
        "version": 1, "order_id": str(order.id), "round": order.round,
        "dialogue_id": str(dialogue.id), "control_revision": dialogue.control_revision,
        "origin": dict(order.source_actor_json), "incoming_delivery_id": str(incoming.id),
        "return_artifact": {"phase": artifact.phase, "id": str(artifact.id), "sha256": artifact.sha256,
            "work_item_id": str(artifact.work_item_id), "run_id": str(run.id),
            "producer_agent_id": str(run.agent_id), "producer_revision_id": str(run.agent_revision_id)},
        "output": output,
    }
    if native_evidence is not None:
        envelope["native_work_evidence"] = native_evidence
    return DelegationReturn(canonical(envelope))


async def validate_delegation_return_message(session, service, dialogue, delivery, message):
    dialogue = await session.get(AgentDialogue, dialogue.id, populate_existing=True)
    delivery = await session.get(AgentDialogueDelivery, delivery.id, populate_existing=True)
    message = await session.get(ConversationMessage, message.id, populate_existing=True)
    if dialogue is None or delivery is None or message is None:
        raise _invalid()
    marker = (message.message_metadata or {}).get("agent_delegation_return") if isinstance(message.message_metadata, dict) else None
    if not isinstance(marker, dict) or set(marker) != {"version", "order_id"} or marker.get("version") != 1:
        raise _invalid()
    try:
        order = await session.get(AgentDelegation, delegation_uuid(marker["order_id"]), populate_existing=True)
    except DelegationError:
        raise _invalid() from None
    if order is None:
        raise _invalid()
    projection = await resolve_delegation_return(
        session, service, order, allow_acked_replay=(dialogue.state in {"succeeded", "failed", "cancelled", "blocked"} and delivery.state == "acked"))
    envelope = projection.as_dict()
    artifact = envelope["return_artifact"]
    if (str(dialogue.id) != envelope["dialogue_id"] or delivery.dialogue_id != dialogue.id
            or dialogue.worker_agent_id != order.delegator_agent_id
            or dialogue.worker_agent_revision_id != order.delegator_revision_id
            or message.deleted_at is not None or message.role != "system" or message.sender_type != "system"
            or message.session_id != dialogue.worker_session_id or delivery.source_message_id != message.id
            or delivery.source_session_id != dialogue.worker_session_id
            or delivery.source_agent_id != dialogue.worker_agent_id
            or delivery.source_agent_revision_id != dialogue.worker_agent_revision_id
            or delivery.receiver_session_id != dialogue.root_session_id
            or delivery.receiver_agent_id != dialogue.root_agent_id
            or delivery.receiver_agent_revision_id != dialogue.root_agent_revision_id
            or delivery.kind not in {"response", "event"}
            or str(delivery.causation_id) != envelope["incoming_delivery_id"]
            or delivery.expected_control_revision != dialogue.control_revision
            or str(delivery.source_run_id) != artifact["run_id"]
            or str(delivery.source_work_item_id) != artifact["work_item_id"]
            or delivery.request_digest != projection.sha256
            or message.content != projection.body):
        raise _invalid()
    return projection
