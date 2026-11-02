"""Strict boundary between Team media result readers and Dialogue return code.

This module does not publish a Dialogue message.  In particular, a matching
result and Work are not by themselves proof that a Root may consume it.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime

from sqlalchemy import select
from dataclasses import dataclass
from uuid import UUID

from ..memory.models import Agent, AgentRevision, AgentDialogue, AgentDialogueDelivery, AgentRun, AgentWorkItem
from .agent_delegation_service import DelegationError


_RESULT_KEYS = frozenset({
    "source", "graph_case", "graph_id", "graph_revision_id", "graph_group_key", "graph_hash",
    "plan_id", "plan_hash", "producer_work_item_id", "producer_agent_run_id", "producer_agent_id",
    "producer_revision_id", "asset_revision_id", "output_id", "bytes_sha256", "revision_hash",
    "generation_run_id", "generation_run_hash", "output_provenance_hash", "qa_task_id",
    "qa_work_item_id", "qa_agent_run_id", "qa_agent_id", "qa_revision_id", "accepted",
    "feedback", "result_sha256", "media_request",
})
_SOURCE_KEYS = frozenset({"version", "source_run_id", "source_work_item_id", "source_session_id",
                          "source_agent_id", "source_revision_id", "inherited_user_id"})
_UUID_KEYS = frozenset({"graph_id", "graph_revision_id", "plan_id", "producer_work_item_id",
    "producer_agent_run_id", "producer_agent_id", "producer_revision_id", "asset_revision_id",
    "output_id", "generation_run_id", "qa_work_item_id", "qa_agent_run_id",
    "qa_agent_id", "qa_revision_id"})
_HASH_KEYS = frozenset({"graph_hash", "plan_hash", "bytes_sha256", "revision_hash",
                         "generation_run_hash", "output_provenance_hash", "result_sha256"})


def _invalid():
    return DelegationError("team_media_return_invalid", 403)


def _uuid(value):
    if not isinstance(value, str):
        raise _invalid()
    try:
        parsed = UUID(value)
    except (ValueError, TypeError, AttributeError):
        raise _invalid() from None
    if str(parsed) != value:
        raise _invalid()
    return parsed


@dataclass(frozen=True)
class TeamMediaResult:
    """Immutable, canonical snapshot; this is not an authorization proof."""
    body: str

    @property
    def sha256(self):
        return hashlib.sha256(self.body.encode("utf-8")).hexdigest()

    def as_dict(self):
        return json.loads(self.body)


async def read_bound_team_media_result(session, *, producer_work_item_id, resolver, config=None):
    """Read via the supplied verified reader and bind its result to persisted Work.

    The caller must separately prove source and Dialogue authority before using
    this snapshot.  An explicit resolver keeps an absent reader fail-closed.
    """
    producer_id = _uuid(str(producer_work_item_id))
    result = await resolver(session, producer_work_item_id=producer_id, config=config)
    if result is None:
        return None
    if not isinstance(result, dict) or set(result) not in (_RESULT_KEYS, _RESULT_KEYS | {"sampled_video"}):
        raise _invalid()
    source = result["source"]
    if (not isinstance(source, dict) or set(source) != _SOURCE_KEYS
            or type(source["version"]) is not int or source["version"] != 1):
        raise _invalid()
    for key in _SOURCE_KEYS - {"version"}:
        _uuid(source[key])
    for key in _UUID_KEYS:
        _uuid(result[key])
    if not isinstance(result["qa_task_id"], str) or not re.fullmatch(r"[0-9a-f]{32}", result["qa_task_id"]):
        raise _invalid()
    for key in _HASH_KEYS:
        if not isinstance(result[key], str) or not re.fullmatch(r"[0-9a-f]{64}", result[key]):
            raise _invalid()
    if (type(result["accepted"]) is not bool
            or not all(isinstance(result[k], str) and result[k] for k in ("graph_case", "graph_group_key"))
            or not isinstance(result["feedback"], str)
            or not isinstance(result["media_request"], dict)):
        raise _invalid()
    work = await session.get(AgentWorkItem, producer_id, populate_existing=True)
    origin = (work.metadata_json or {}).get("team_media_origin") if work and isinstance(work.metadata_json, dict) else None
    if not isinstance(origin, dict):
        raise _invalid()
    expected = {**source, "graph_id": result["graph_id"], "graph_revision_id": result["graph_revision_id"],
                "graph_group_key": result["graph_group_key"], "graph_hash": result["graph_hash"],
                "case_key": result["graph_case"], "worker_agent_id": result["producer_agent_id"],
                "worker_revision_id": result["producer_revision_id"], "reviewer_agent_id": result["qa_agent_id"],
                "reviewer_revision_id": result["qa_revision_id"], "plan_id": result["plan_id"],
                "plan_hash": result["plan_hash"]}
    if (origin != expected or str(work.id) != result["producer_work_item_id"]
            or work.source_type != "media.generation.plan" or work.source_id != result["plan_id"]
            or work.source_revision != result["plan_hash"]
            or str(work.assigned_agent_id) != result["producer_agent_id"]
            or str(work.agent_revision_id) != result["producer_revision_id"]
            or str(work.parent_work_item_id) != source["source_work_item_id"]):
        raise _invalid()
    if "sampled_video" in result:
        from .agent_task_invocation import _normalise_video_review
        sampled = result["sampled_video"]
        pin = {"asset_revision_id": result["asset_revision_id"], "output_id": result["output_id"],
               "bytes_sha256": result["bytes_sha256"], "revision_hash": result["revision_hash"],
               "work_id": result["producer_work_item_id"], "agent_run_id": result["producer_agent_run_id"],
               "plan_id": result["plan_id"], "plan_hash": result["plan_hash"],
               "generation_run_id": result["generation_run_id"], "generation_run_hash": result["generation_run_hash"],
               "output_provenance_hash": result["output_provenance_hash"]}
        try:
            if (not isinstance(sampled, dict) or set(sampled) != {"source_pin", "source_video_sha256", "frames", "scope"}
                    or sampled["source_pin"] != pin or sampled["source_video_sha256"] != pin["bytes_sha256"]
                    or sampled["scope"] != "sampled_visual_only" or not isinstance(sampled["frames"], list)):
                raise _invalid()
            frames = []
            for frame in sampled["frames"]:
                if (not isinstance(frame, dict) or set(frame) != {"source_pin", "source_video_sha256", "frame_sha256", "mime_type", "timestamp_seconds", "scope"}
                        or frame["source_pin"] != pin or frame["scope"] != "sampled_visual_only"):
                    raise _invalid()
                frames.append({**{k: frame[k] for k in ("source_video_sha256", "frame_sha256", "mime_type", "timestamp_seconds")},
                               "sampled_visual_only": True})
            _normalise_video_review({"project_id": str(work.project_id), "persona_id": str(work.persona_id),
                "expected_agent_revision_id": result["qa_revision_id"], "pin": pin, "frames": frames})
            receipt = result["media_request"]
            if (set(receipt) != {"delivery", "route", "image_count", "bytes_sha256"}
                    or receipt["delivery"] != "client_image_data_request" or type(receipt["image_count"]) is not int
                    or receipt["image_count"] != len(frames)
                    or receipt["bytes_sha256"] != [frame["frame_sha256"] for frame in frames]):
                raise _invalid()
        except (KeyError, TypeError, ValueError, RuntimeError):
            raise _invalid() from None
    try:
        body = json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise _invalid() from None
    return TeamMediaResult(body)


async def resolve_team_media_return(session, service, *, producer_work_item_id, config=None,
                                    allow_acked_replay=False, _result_resolver=None):
    """Prove the persisted Dialogue origin and produce an immutable return envelope.

    The injected reader is a test seam; production always imports the verified reader.
    This function does not deliver a message or prove a delivery has been acked.
    """
    if _result_resolver is None:
        from .agent_team_media_review import resolve_team_media_result
        _result_resolver = resolve_team_media_result
    snapshot = await read_bound_team_media_result(session, producer_work_item_id=producer_work_item_id,
                                                   resolver=_result_resolver, config=config)
    if snapshot is None:
        return None
    result = snapshot.as_dict()
    source = result["source"]
    work = await session.get(AgentWorkItem, _uuid(result["producer_work_item_id"]), populate_existing=True)
    origin_work = await session.get(AgentWorkItem, _uuid(source["source_work_item_id"]), populate_existing=True)
    origin_run = await session.get(AgentRun, _uuid(source["source_run_id"]), populate_existing=True)
    if origin_work is None or origin_run is None or work is None or origin_work.project_id is None:
        raise _invalid()
    root = await session.get(AgentWorkItem, origin_work.root_work_item_id or origin_work.id, populate_existing=True)
    if (origin_run.work_item_id != origin_work.id or work.project_id != origin_work.project_id
            or root is None or root.project_id != origin_work.project_id
            or work.root_work_item_id != root.id or work.causation_id != str(origin_work.id)
            or work.causal_depth != origin_work.causal_depth + 1):
        raise _invalid()
    # This method is supplied by the source-proof extraction; no synthetic order.
    await service.verified_source_origin_fields(session, source=source, project_id=origin_work.project_id,
        persona_id=origin_work.persona_id, delegator_agent_id=origin_run.agent_id,
        delegator_revision_id=origin_run.agent_revision_id, created_by=_uuid(str(origin_run.user_id)))
    dialogue = await session.scalar(select(AgentDialogue).where(
        AgentDialogue.worker_session_id == origin_run.session_id))
    terminal = {"succeeded", "failed", "cancelled", "blocked"}
    if (dialogue is None or dialogue.state not in ({"queued", "running", "waiting"} | (terminal if allow_acked_replay else set()))
            or (dialogue.state not in terminal and dialogue.deadline is not None and dialogue.deadline <= datetime.utcnow())):
        raise _invalid()
    metadata = (origin_run.run_metadata or {}).get("agent_dialogue") or {}
    try:
        incoming = await session.get(AgentDialogueDelivery, _uuid(metadata["delivery_id"]), populate_existing=True)
    except (KeyError, TypeError):
        raise _invalid() from None
    if (incoming is None or incoming.dialogue_id != dialogue.id or incoming.receiver_run_id != origin_run.id
            or incoming.receiver_work_item_id != origin_work.id or incoming.kind not in {"request", "control"}
            or incoming.expected_control_revision != dialogue.control_revision
            or incoming.state in {"failed", "cancelled", "expired"}):
        raise _invalid()
    authorization = dialogue.authorization_json
    if not isinstance(authorization, dict):
        raise _invalid()
    from .agent_delegation_service import canonical
    if dialogue.authorization_hash != hashlib.sha256(canonical(authorization).encode("utf-8")).hexdigest():
        raise _invalid()
    auth = authorization.get("team_graph")
    if not isinstance(auth, dict):
        raise _invalid()
    if dialogue.state not in terminal:
        if await service._inherited_team_graph(session, origin_run, origin_work, dialogue.owner_user_id, origin_work.project_id) != auth:
            raise _invalid()
    ref = {"graph_id": result["graph_id"], "revision_id": result["graph_revision_id"],
           "group_key": result["graph_group_key"]}
    case = hashlib.sha256(json.dumps({"owner": str(dialogue.owner_user_id),
        "human_message_id": str(dialogue.human_message_id), "project_id": str(dialogue.project_id),
        "graph_revision_id": result["graph_revision_id"]}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if (auth.get("ref") != ref or auth.get("graph_hash") != result["graph_hash"]
            or auth.get("case_key") != result["graph_case"] or case != result["graph_case"]
            or dialogue.project_id != origin_work.project_id or str(dialogue.owner_user_id) != str(origin_run.user_id)):
        raise _invalid()
    from .agent_team_graph_service import AgentTeamGraphService
    from .agent_team_graph_compiler import PinnedAgent, compile_media_group
    envelope = await AgentTeamGraphService().get_in_session(session, _uuid(ref["graph_id"]),
        actor_user_id=dialogue.owner_user_id, revision_id=_uuid(ref["revision_id"]))
    if envelope["content_hash"] != result["graph_hash"] or envelope["project_id"] not in (None, str(work.project_id)):
        raise _invalid()
    if dialogue.state in terminal and auth.get("handoff_inputs"):
        from .agent_team_handoff_inputs import _resolve_group_handoff_inputs
        current = await _resolve_group_handoff_inputs(session, service=service, owner_user_id=dialogue.owner_user_id,
            root_session_id=dialogue.root_session_id, case_key=auth["case_key"],
            graph={**envelope, "case_project_id": str(work.project_id)}, target_group=ref["group_key"], active=True)
        if current != auth["handoff_inputs"]:
            raise _invalid()
    try:
        pins = {key: PinnedAgent(_uuid(pin["agent_id"]), _uuid(pin["revision_id"]))
                for key, pin in auth["nodepins"].items()}
        compiled = compile_media_group(envelope, ref["group_key"], pins)
        graph = compiled.graph
        nodes = {node.key: node for node in graph.roster}
        worker_key, reviewer_key = graph.allowed_pairs[0]
        worker, reviewer = nodes[worker_key], nodes[reviewer_key]
        expected_kind = "media_video" if "sampled_video" in result else "media_image"
        if (compiled.execution_kind != expected_kind or set(pins) != {graph.root.key, *nodes}
                or auth.get("root_key") != graph.root.key or auth.get("leader_key") != graph.leader.key
                or graph.root.agent_id != dialogue.root_agent_id or graph.root.revision_id != dialogue.root_agent_revision_id
                or graph.leader.agent_id != origin_run.agent_id or graph.leader.revision_id != origin_run.agent_revision_id
                or str(worker.agent_id) != result["producer_agent_id"] or str(worker.revision_id) != result["producer_revision_id"]
                or str(reviewer.agent_id) != result["qa_agent_id"] or str(reviewer.revision_id) != result["qa_revision_id"]):
            raise _invalid()
    except (KeyError, TypeError, ValueError):
        raise _invalid() from None
    for node in (graph.root, *graph.roster):
        agent = await session.get(Agent, node.agent_id, populate_existing=True)
        revision = await session.get(AgentRevision, node.revision_id, populate_existing=True)
        if agent is None or agent.state != "active" or agent.created_by != dialogue.owner_user_id or revision is None or revision.agent_id != agent.id:
            raise _invalid()
    from .agent_delegate_policy import revision_allows_agent_delegate
    leader_agent = await session.get(Agent, graph.leader.agent_id, populate_existing=True)
    leader_revision = await session.get(AgentRevision, graph.leader.revision_id, populate_existing=True)
    for node in (worker, reviewer):
        if not revision_allows_agent_delegate(leader_revision, tool_name="delegate_media", target_agent_id=node.agent_id,
                target_agent_owner_id=dialogue.owner_user_id, caller_agent_owner_id=leader_agent.created_by,
                owner_user_id=dialogue.owner_user_id):
            raise _invalid()
    await service.validate_read_authority(session, reviewer.agent_id, reviewer.revision_id, work.project_id, work.persona_id)
    decision = await service.authority.resolve(session=session, agent_id=worker.agent_id, revision_id=worker.revision_id,
        project_id=work.project_id, persona_id=work.persona_id, required_capability="media")
    if not decision.allowed:
        raise _invalid()
    projection = TeamMediaResult(canonical({"version": 1, "dialogue_id": str(dialogue.id),
        "control_revision": dialogue.control_revision, "incoming_delivery_id": str(incoming.id),
        "source": source, "result": result}))
    if dialogue.state in terminal:
        acked = await session.scalar(select(AgentDialogueDelivery).where(
            AgentDialogueDelivery.dialogue_id == dialogue.id,
            AgentDialogueDelivery.kind.in_(("response", "event")),
            AgentDialogueDelivery.state == "acked",
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


async def validate_team_media_return_message(session, service, dialogue, delivery, message, *, allow_acked_replay=False, config=None):
    """Reconstruct the host message from the persisted media and Dialogue proof."""
    from ..memory.models import ConversationMessage

    dialogue = await session.get(AgentDialogue, dialogue.id, populate_existing=True)
    delivery = await session.get(AgentDialogueDelivery, delivery.id, populate_existing=True)
    message = await session.get(ConversationMessage, message.id, populate_existing=True)
    if dialogue is None or delivery is None or message is None:
        raise _invalid()
    metadata = message.message_metadata if isinstance(message.message_metadata, dict) else {}
    marker = metadata.get("agent_team_media_return")
    if (not isinstance(marker, dict) or set(marker) != {"version", "producer_work_item_id"}
            or type(marker.get("version")) is not int or marker["version"] != 1):
        raise _invalid()
    producer_id = _uuid(marker["producer_work_item_id"])
    projection = await resolve_team_media_return(session, service, producer_work_item_id=str(producer_id),
        allow_acked_replay=allow_acked_replay and delivery.state == "acked", config=config)
    if projection is None:
        raise _invalid()
    envelope = projection.as_dict()
    source = envelope["source"]
    incoming = await session.get(AgentDialogueDelivery, _uuid(envelope["incoming_delivery_id"]), populate_existing=True)
    if (incoming is None or str(dialogue.id) != envelope["dialogue_id"] or delivery.dialogue_id != dialogue.id
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
            or str(delivery.source_run_id) != source["source_run_id"]
            or str(delivery.source_work_item_id) != source["source_work_item_id"]
            or int(delivery.depth or 0) != int(incoming.depth or 0) + 1
            or int(delivery.sequence or 0) <= int(incoming.sequence or 0)
            or delivery.deadline != dialogue.deadline
            or delivery.idempotency_key != f"team-media-return:{producer_id}:{projection.sha256}:{dialogue.control_revision}"
            or delivery.request_digest != projection.sha256 or message.content != projection.body):
        raise _invalid()
    return projection
