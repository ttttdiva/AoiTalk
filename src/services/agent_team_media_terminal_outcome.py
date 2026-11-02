"""Database-bound terminal Team media status; contains no media or exception text."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from uuid import UUID

from sqlalchemy import select

from ..memory.models import AgentDialogue, AgentDialogueDelivery, AgentRun, AgentWorkItem, ConversationMessage
from ..memory.models.agent_work import AgentWorkEvent
from .agent_delegation_service import canonical
from .agent_team_media_dialogue_return import TeamMediaResult, _invalid, _uuid
from .agent_team_media_review import (
    TeamMediaReviewInvalid, review_causality_matches, team_media_review_key,
    validate_team_media_producer,
)


async def _failure(session, work, *, owner):
    states = {"failed": {"permanent"}, "dead_letter": {"transient", "permanent"},
              "cancelled": {"cancelled"}, "blocked": {"blocked"}, "uncertain": {"uncertain"}}
    if work.state not in states:
        return None
    if (work.outcome_classification not in states[work.state] or work.attempt_count < 0
            or (work.attempt_count == 0 and work.state not in {"cancelled", "blocked"})):
        raise _invalid()
    event = await session.scalar(select(AgentWorkEvent).where(
        AgentWorkEvent.work_item_id == work.id).order_by(AgentWorkEvent.sequence.desc()).limit(1))
    if (event is None or event.event_type != f"work.{work.state}" or event.to_state != work.state
            or (event.safe_error_code != work.safe_error_code
                and not (work.state == "cancelled" and work.safe_error_code == "cancelled"
                         and event.safe_error_code is None))
            or (work.active_agent_run_id is not None and work.active_agent_run_id != event.agent_run_id)):
        raise _invalid()
    payload = event.payload_json
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise _invalid()
    if (work.attempt_count == 0 and work.state == "blocked"
            and (event.from_state != "pending" or event.agent_run_id is not None
                 or work.safe_error_code not in {"deadline_expired", "budget_exhausted"}
                 or payload.get("error_code") != work.safe_error_code)):
        raise _invalid()
    if payload.get("classification") not in (None, work.outcome_classification):
        raise _invalid()
    if payload.get("attempt") is not None and payload["attempt"] != work.attempt_count:
        raise _invalid()
    runs = (await session.scalars(select(AgentRun).where(AgentRun.work_item_id == work.id,
        AgentRun.work_item_attempt == work.attempt_count))).all()
    run_id = event.agent_run_id
    if (run_id is None and work.state == "dead_letter" and work.safe_error_code == "max_attempts_exhausted"
            and event.from_state == "retry_wait"):
        # Claim-time exhaustion occurs after the last attempt was settled.
        # The transition itself has no active Run, so prove that attempt from
        # the immediately preceding retry receipt instead.
        previous = await session.scalar(select(AgentWorkEvent).where(
            AgentWorkEvent.work_item_id == work.id, AgentWorkEvent.sequence < event.sequence
            ).order_by(AgentWorkEvent.sequence.desc()).limit(1))
        facts = previous.payload_json if previous and isinstance(previous.payload_json, dict) else {}
        if (previous is None or previous.event_type != "work.retry_wait" or previous.to_state != "retry_wait"
                or facts.get("classification") != "transient" or facts.get("attempt") != work.attempt_count):
            raise _invalid()
        run_id = previous.agent_run_id
        if run_id is None and (runs or previous.from_state not in {"pending", "claimed"}):
            raise _invalid()
    if run_id:
        if len(runs) != 1 or runs[0].id != run_id:
            raise _invalid()
        run = runs[0]
        if (run.status not in {"failed", "cancelled"} or run.project_id != work.project_id
                or run.agent_id != work.assigned_agent_id or run.agent_revision_id != work.agent_revision_id
                or (run.user_id is not None and str(run.user_id) != owner)):
            raise _invalid()
    else:
        if runs or (event.from_state not in {"pending", "claimed"}
                and not (event.from_state == "running" and work.state in {"blocked", "cancelled"}) and not (
                work.state == "dead_letter" and event.from_state == "retry_wait"
                and work.safe_error_code == "max_attempts_exhausted")):
            raise _invalid()
        run = None
    return {"work_item_id": str(work.id), "attempt": work.attempt_count,
            "agent_run_id": str(run.id) if run else None,
            "run_status": run.status if run else None, "status": work.state,
            "code": {"failed": "execution_failed", "dead_letter": "attempts_exhausted",
                     "cancelled": "cancelled", "blocked": "blocked", "uncertain": "uncertain"}[work.state]}


async def _read_status(session, producer_id, config):
    from .agent_task_invocation import _admission_revision, _request_fingerprint, _task_id_for
    producer = await session.get(AgentWorkItem, producer_id, populate_existing=True)
    if producer is None:
        raise _invalid()
    base = await validate_team_media_producer(session, producer_work_item_id=producer_id, config=config, _base_only=True)
    failure = await _failure(session, producer, owner=base["owner_user_id"])
    phase = "producer"
    if failure is None:
        if producer.state != "succeeded":
            return None
        proof = await validate_team_media_producer(session, producer_work_item_id=producer_id, config=config, _status_only=True)
        task_id = _task_id_for(proof["owner_user_id"], team_media_review_key(proof))
        works = (await session.scalars(select(AgentWorkItem).where(
            AgentWorkItem.source_type == "api_agent_task", AgentWorkItem.source_id == task_id))).all()
        if not works:
            return None
        if len(works) != 1:
            raise _invalid()
        work = works[0]
        metadata = work.metadata_json or {}
        if (not review_causality_matches(work, producer) or work.execution_adapter != "api_agent_task"
                or metadata.get("team_media_review_origin_digest") != _request_fingerprint(proof)
                or metadata.get("team_media_review_origin") != proof
                or metadata.get("image_review") != proof["image_review"] or metadata.get("task_id") != task_id
                or metadata.get("owner_user_id") != proof["owner_user_id"]
                or work.source_revision != metadata.get("admission_revision")
                or work.source_revision != _admission_revision(task_id)
                or work.intent_key != "invoke" or work.domain != "api_agent_task"
                or str(work.project_id) != proof["project_id"] or str(work.persona_id) != proof["persona_id"]
                or str(work.assigned_agent_id) != proof["reviewer_agent_id"]
                or str(work.agent_revision_id) != proof["reviewer_revision_id"]):
            raise _invalid()
        failure = await _failure(session, work, owner=base["owner_user_id"])
        phase = "review"
    if failure is None:
        return None
    return {**base, "phase": phase, "outcome": failure["status"], "evidence": failure}


async def resolve_team_media_terminal_outcome(session, service, *, producer_work_item_id, config=None,
                                              allow_acked_replay=False):
    with session.no_autoflush:
        try:
            result = await _read_status(session, _uuid(str(producer_work_item_id)), config)
        except TeamMediaReviewInvalid as exc:
            raise _invalid() from exc
        if result is None:
            return None
        source = result["source"]
        run = await session.get(AgentRun, _uuid(source["source_run_id"]), populate_existing=True)
        work = await session.get(AgentWorkItem, _uuid(source["source_work_item_id"]), populate_existing=True)
        if run is None or work is None or run.work_item_id != work.id:
            raise _invalid()
        dialogue = await session.scalar(select(AgentDialogue).where(AgentDialogue.worker_session_id == run.session_id))
        terminal = {"succeeded", "failed", "cancelled", "blocked"}
        if (dialogue is None or dialogue.state not in ({"queued", "running", "waiting"} | (terminal if allow_acked_replay else set()))
                or (dialogue.state not in terminal and dialogue.deadline is not None and dialogue.deadline <= datetime.utcnow())):
            raise _invalid()
        metadata = (run.run_metadata or {}).get("agent_dialogue") or {}
        try:
            incoming = await session.get(AgentDialogueDelivery, _uuid(metadata["delivery_id"]), populate_existing=True)
        except (KeyError, TypeError):
            raise _invalid() from None
        if (incoming is None or incoming.dialogue_id != dialogue.id or incoming.receiver_run_id != run.id
                or incoming.receiver_work_item_id != work.id or incoming.kind not in {"request", "control"}
                or incoming.expected_control_revision != dialogue.control_revision
                or incoming.state in {"failed", "cancelled", "expired"}):
            raise _invalid()
        auth = dialogue.authorization_json
        if (not isinstance(auth, dict) or dialogue.authorization_hash != hashlib.sha256(canonical(auth).encode()).hexdigest()):
            raise _invalid()
        graph = auth.get("team_graph")
        ref = {"graph_id": result["graph_id"], "revision_id": result["graph_revision_id"], "group_key": result["graph_group_key"]}
        case = hashlib.sha256(json.dumps({"owner": str(dialogue.owner_user_id), "human_message_id": str(dialogue.human_message_id),
            "project_id": str(dialogue.project_id), "graph_revision_id": result["graph_revision_id"]},
            sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if (not isinstance(graph, dict) or graph.get("ref") != ref or graph.get("graph_hash") != result["graph_hash"]
                or graph.get("case_key") != result["graph_case"] or case != result["graph_case"]
                or dialogue.project_id != work.project_id or str(dialogue.owner_user_id) != source["inherited_user_id"]):
            raise _invalid()
        if dialogue.state not in terminal and await service._inherited_team_graph(session, run, work, dialogue.owner_user_id, work.project_id) != graph:
            raise _invalid()
        projection = TeamMediaResult(canonical({"version": 1, "dialogue_id": str(dialogue.id),
            "control_revision": dialogue.control_revision, "incoming_delivery_id": str(incoming.id),
            "source": source, "outcome": result["outcome"], "result": result}))
        if dialogue.state in terminal:
            acked = await session.scalar(select(AgentDialogueDelivery.id).where(
                AgentDialogueDelivery.dialogue_id == dialogue.id,
                AgentDialogueDelivery.kind.in_(("response", "event")), AgentDialogueDelivery.state == "acked",
                AgentDialogueDelivery.causation_id == str(incoming.id),
                AgentDialogueDelivery.expected_control_revision == dialogue.control_revision,
                AgentDialogueDelivery.request_digest == projection.sha256,
                AgentDialogueDelivery.source_session_id == dialogue.worker_session_id,
                AgentDialogueDelivery.source_agent_id == dialogue.worker_agent_id,
                AgentDialogueDelivery.source_agent_revision_id == dialogue.worker_agent_revision_id,
                AgentDialogueDelivery.receiver_session_id == dialogue.root_session_id,
                AgentDialogueDelivery.receiver_agent_id == dialogue.root_agent_id,
                AgentDialogueDelivery.receiver_agent_revision_id == dialogue.root_agent_revision_id))
            if acked is None:
                raise _invalid()
        return projection


async def validate_team_media_outcome_message(session, service, dialogue, delivery, message, *, config=None,
                                              allow_acked_replay=False):
    dialogue = await session.get(AgentDialogue, dialogue.id, populate_existing=True)
    delivery = await session.get(AgentDialogueDelivery, delivery.id, populate_existing=True)
    message = await session.get(ConversationMessage, message.id, populate_existing=True)
    if dialogue is None or delivery is None or message is None:
        raise _invalid()
    marker = (message.message_metadata or {}).get("agent_team_media_outcome")
    if (not isinstance(marker, dict) or set(marker) != {"version", "producer_work_item_id"}
            or type(marker.get("version")) is not int or marker["version"] != 1):
        raise _invalid()
    producer_id = _uuid(marker["producer_work_item_id"])
    proof = await resolve_team_media_terminal_outcome(session, service, producer_work_item_id=str(producer_id),
        config=config, allow_acked_replay=allow_acked_replay and delivery.state == "acked")
    if proof is None:
        raise _invalid()
    envelope = proof.as_dict()
    source, evidence = envelope["source"], envelope["result"]["evidence"]
    incoming = await session.get(AgentDialogueDelivery, _uuid(envelope["incoming_delivery_id"]), populate_existing=True)
    key = f"team-media-outcome:{producer_id}:{evidence['work_item_id']}:{evidence['attempt']}:{proof.sha256}:{dialogue.control_revision}"
    if (incoming is None or str(dialogue.id) != envelope["dialogue_id"] or delivery.dialogue_id != dialogue.id
            or message.deleted_at is not None or message.role != "system" or message.sender_type != "system"
            or message.session_id != dialogue.worker_session_id or delivery.source_message_id != message.id
            or delivery.source_session_id != dialogue.worker_session_id or delivery.source_agent_id != dialogue.worker_agent_id
            or delivery.source_agent_revision_id != dialogue.worker_agent_revision_id
            or delivery.receiver_session_id != dialogue.root_session_id or delivery.receiver_agent_id != dialogue.root_agent_id
            or delivery.receiver_agent_revision_id != dialogue.root_agent_revision_id or delivery.kind not in {"response", "event"}
            or str(delivery.causation_id) != envelope["incoming_delivery_id"] or delivery.expected_control_revision != dialogue.control_revision
            or str(delivery.source_run_id) != source["source_run_id"] or str(delivery.source_work_item_id) != source["source_work_item_id"]
            or int(delivery.depth or 0) != int(incoming.depth or 0) + 1 or int(delivery.sequence or 0) <= int(incoming.sequence or 0)
            or delivery.deadline != dialogue.deadline or delivery.idempotency_key != key
            or delivery.request_digest != proof.sha256 or message.content != proof.body):
        raise _invalid()
    return proof
