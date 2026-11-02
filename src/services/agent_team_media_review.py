"""Validation primitives for the Team media review bridge."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from collections.abc import Mapping
from typing import Any

from .agent_task_invocation import _normalise_image_review


class TeamMediaReviewInvalid(ValueError):
    """The review evidence does not match its immutable expected identity."""


@dataclass(frozen=True, slots=True)
class ReviewVerdict:
    accepted: bool
    feedback: str
    result_sha256: str


def validate_review_result(value: Any, *, review: Mapping[str, Any], reviewer_agent_id: str) -> ReviewVerdict:
    """Validate result evidence only; callers must independently authenticate all links.

    In particular this function does not establish Work/Run success or authorize a
    caller and must not be used as a database-bound review reader on its own.
    """
    expected = _normalise_image_review(review)
    if not isinstance(value, Mapping):
        raise TeamMediaReviewInvalid("missing review result")
    accepted, feedback = value.get("accepted"), value.get("feedback")
    if type(accepted) is not bool or not isinstance(feedback, str) or len(feedback) > 2048:
        raise TeamMediaReviewInvalid("invalid verdict")
    canonical = json.dumps({"accepted": accepted, "feedback": feedback}, ensure_ascii=False,
                           sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    provenance = value.get("provenance")
    route, media = value.get("route"), value.get("media_request")
    if (value.get("result_sha256") != digest or value.get("reviewer_agent_id") != reviewer_agent_id
            or value.get("reviewer_revision_id") != expected["expected_agent_revision_id"]
            or not isinstance(provenance, Mapping)
            or any(provenance.get(k) != v for k, v in expected["pin"].items())
            or provenance.get("work_state") != "succeeded"
            or provenance.get("agent_run_status") != "succeeded"
            or provenance.get("generation_run_status") != "succeeded"
            or provenance.get("producer_binding") != "plan_association_only"
            or not isinstance(route, Mapping) or set(route) != {"provider", "model", "effort", "backend"}
            or not isinstance(media, Mapping) or set(media) != {"delivery", "route", "image_count", "bytes_sha256"}
            or media.get("delivery") != "client_image_data_request" or media.get("route") != route
            or type(media.get("image_count")) is not int or media["image_count"] != 1
            or media.get("bytes_sha256") != [expected["pin"]["bytes_sha256"]]):
        raise TeamMediaReviewInvalid("review evidence mismatch")
    return ReviewVerdict(accepted, feedback, digest)


# The key is derived from the complete immutable proof, including the normalized
# pin. Reject non-JSON values rather than allowing process-dependent encodings.
def team_media_review_key(proof: Mapping[str, Any]) -> str:
    if not isinstance(proof, Mapping):
        raise TeamMediaReviewInvalid("invalid producer proof")
    try:
        encoded = json.dumps(dict(proof), ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise TeamMediaReviewInvalid("invalid producer proof") from exc
    return "team-media-review-v1:" + hashlib.sha256(encoded).hexdigest()


def review_causal_fields(producer):
    """Normal Work lineage inherited from the persisted producer."""
    return {"parent_work_item_id": str(producer.id),
            "root_work_item_id": str(producer.root_work_item_id or producer.id),
            "causation_id": str(producer.id), "causal_depth": producer.causal_depth + 1}


def review_causality_matches(work, producer):
    fields = review_causal_fields(producer)
    return (str(work.parent_work_item_id or "") == fields["parent_work_item_id"]
            and str(work.root_work_item_id or "") == fields["root_work_item_id"]
            and work.causation_id == fields["causation_id"] and work.causal_depth == fields["causal_depth"])


async def validate_team_media_producer(session, *, producer_work_item_id, config=None, _base_only=False, _status_only=False) -> dict:
    """Rebuild the image review identity from persisted server records."""
    return await _validate_team_producer(session, producer_work_item_id=producer_work_item_id,
        config=config, kind="image", _base_only=_base_only, _status_only=_status_only)


async def validate_team_video_producer(session, *, producer_work_item_id, config=None) -> dict:
    """Authenticate the original video producer before any frame sampling."""
    return await _validate_team_producer(session, producer_work_item_id=producer_work_item_id,
        config=config, kind="video")


async def _validate_team_producer(session, *, producer_work_item_id, config, kind, _base_only=False, _status_only=False, _result_replay=False) -> dict:
    """Rebuild review identity from persisted records and the compiled kind."""
    from uuid import UUID
    from sqlalchemy import select
    from ..memory.models import Agent, AgentRevision, AgentRun, AgentWorkItem, AgentWorkEvent, GenerationPlan, GenerationRun, GenerationOutput, PersonaRevision
    from ..memory.models.media_assets import MediaAssetRevision
    from .agent_delegation_service import AgentDelegationService, _CapturedSourceActor
    from .agent_delegate_policy import revision_allows_agent_delegate
    from .agent_team_media_execution import _compile
    from .media_asset_service import _binding, MIME_EXTENSIONS, IMAGE_MAX_BYTES, VIDEO_MAX_BYTES
    from .media_operations_service import sha256_json

    def require(value):
        if not value:
            raise TeamMediaReviewInvalid("producer evidence mismatch")

    try:
        work = await session.get(AgentWorkItem, UUID(str(producer_work_item_id)), populate_existing=True)
        require(work is not None and (_base_only or (work.state == "succeeded" and work.active_agent_run_id is None))
                and work.source_type == "media.generation.plan"
                and work.execution_adapter == "media.generation" and (_base_only or work.attempt_count > 0))
        origin = (work.metadata_json or {}).get("team_media_origin")
        require(isinstance(origin, dict) and origin.get("version") == 1)
        source_keys = ("version", "source_run_id", "source_work_item_id", "source_session_id", "source_agent_id", "source_revision_id", "inherited_user_id")
        source = {k: origin[k] for k in source_keys}
        owner = UUID(source["inherited_user_id"])
        plan = await session.get(GenerationPlan, UUID(str(work.source_id)), populate_existing=True)
        require(plan is not None and plan.owner_user_id == owner and plan.project_id == work.project_id
                and work.source_revision == plan.plan_hash == origin.get("plan_hash")
                and str(plan.id) == origin.get("plan_id")
                and str(work.assigned_agent_id) == origin.get("worker_agent_id")
                and str(work.agent_revision_id) == origin.get("worker_revision_id"))
        if not _base_only:
            runs = (await session.scalars(select(AgentRun).where(AgentRun.work_item_id == work.id,
                AgentRun.work_item_attempt == work.attempt_count))).all()
            require(len(runs) == 1)
            run = runs[0]
            require(plan is not None and run is not None and run.status == "succeeded" and run.work_item_id == work.id
                    and run.agent_id == work.assigned_agent_id and run.agent_revision_id == work.agent_revision_id
                    and run.project_id == work.project_id and (run.user_id is None or str(run.user_id) == str(owner))
                    and plan.owner_user_id == owner and plan.project_id == work.project_id
                    and work.source_revision == plan.plan_hash == origin.get("plan_hash")
                    and str(plan.id) == origin.get("plan_id")
                    and str(work.assigned_agent_id) == origin.get("worker_agent_id")
                    and str(work.agent_revision_id) == origin.get("worker_revision_id"))
            events = (await session.scalars(select(AgentWorkEvent).where(
                AgentWorkEvent.work_item_id == work.id, AgentWorkEvent.event_type == "work.succeeded")
                .order_by(AgentWorkEvent.sequence.desc()))).all()
            require(bool(events) and events[0].agent_run_id == run.id and events[0].to_state == "succeeded"
                    and work.outcome_classification == "succeeded")
            receipt = events[0].payload_json or {}
            require(receipt.get("classification") == "succeeded" and
                    (receipt.get("attempt") == work.attempt_count or receipt.get("receipt_recovered") is True))
        persona = await session.get(PersonaRevision, plan.persona_revision_id, populate_existing=True)
        require(persona is not None and persona.persona_id == work.persona_id and persona.owner_user_id == owner
                and persona.project_id == work.project_id and persona.content_hash == plan.persona_revision_hash)
        parent = await session.get(AgentWorkItem, UUID(source["source_work_item_id"]), populate_existing=True)
        require(parent is not None and parent.project_id == work.project_id
                and work.parent_work_item_id == parent.id
                and work.root_work_item_id == (parent.root_work_item_id or parent.id)
                and work.causation_id == str(parent.id) and work.causal_depth == parent.causal_depth + 1)
        service = AgentDelegationService(None, config=config)
        await service.verified_source_origin_fields(session, source=source, project_id=work.project_id,
            persona_id=parent.persona_id, delegator_agent_id=UUID(source["source_agent_id"]),
            delegator_revision_id=UUID(source["source_revision_id"]), created_by=owner)
        source_run = await session.get(AgentRun, UUID(source["source_run_id"]), populate_existing=True)
        caller = await session.get(Agent, UUID(source["source_agent_id"]), populate_existing=True)
        revision = await session.get(AgentRevision, UUID(source["source_revision_id"]), populate_existing=True)
        require(parent is not None and work.parent_work_item_id == parent.id
                and work.root_work_item_id == (parent.root_work_item_id or parent.id)
                and work.causation_id == str(parent.id) and work.causal_depth == parent.causal_depth + 1)
        root = await session.get(AgentWorkItem, work.root_work_item_id, populate_existing=True)
        require(root is not None and root.project_id == work.project_id)
        from ..memory.models import AgentDialogueDelivery
        delivery = await session.scalar(select(AgentDialogueDelivery).where(
            AgentDialogueDelivery.receiver_run_id == source_run.id,
            AgentDialogueDelivery.receiver_work_item_id == parent.id,
            AgentDialogueDelivery.kind.in_(("request", "control"))))
        source_root = await session.get(AgentWorkItem, delivery.source_work_item_id, populate_existing=True) if delivery and delivery.source_work_item_id else None
        require(source_root is not None)
        for ancestor in (parent, root, source_root):
            require(ancestor.state != "cancelled" and not (ancestor.metadata_json or {}).get("cancellation_requested"))
        captured = _CapturedSourceActor(owner, source, source_run, parent, caller, revision, work.project_id)
        ref = {"graph_id": origin["graph_id"], "revision_id": origin["graph_revision_id"], "group_key": origin["graph_group_key"]}
        historical = False
        if _result_replay:
            from ..memory.models import AgentDialogue
            dialogue = await session.scalar(select(AgentDialogue).where(
                AgentDialogue.worker_session_id == source_run.session_id))
            if dialogue and dialogue.state in {"succeeded", "failed", "cancelled", "blocked"}:
                incoming_id = (source_run.run_metadata or {}).get("agent_dialogue", {}).get("delivery_id")
                incoming = await session.get(AgentDialogueDelivery, UUID(str(incoming_id)), populate_existing=True)
                require(dialogue.owner_user_id == owner and dialogue.project_id == work.project_id
                        and dialogue.worker_session_id == source_run.session_id
                        and dialogue.worker_agent_id == source_run.agent_id
                        and dialogue.worker_agent_revision_id == source_run.agent_revision_id
                        and incoming is not None and incoming.dialogue_id == dialogue.id
                        and incoming.kind in {"request", "control"} and incoming.state == "acked"
                        and incoming.receiver_run_id == source_run.id and incoming.receiver_work_item_id == parent.id
                        and incoming.receiver_session_id == source_run.session_id
                        and incoming.receiver_agent_id == source_run.agent_id
                        and incoming.receiver_agent_revision_id == source_run.agent_revision_id
                        and incoming.expected_control_revision == dialogue.control_revision)
                acked = (await session.scalars(select(AgentDialogueDelivery).where(
                    AgentDialogueDelivery.dialogue_id == dialogue.id,
                    AgentDialogueDelivery.kind.in_(("response", "event")),
                    AgentDialogueDelivery.state == "acked",
                    AgentDialogueDelivery.causation_id == str(incoming.id),
                    AgentDialogueDelivery.expected_control_revision == dialogue.control_revision,
                    AgentDialogueDelivery.source_session_id == dialogue.worker_session_id,
                    AgentDialogueDelivery.source_agent_id == dialogue.worker_agent_id,
                    AgentDialogueDelivery.source_agent_revision_id == dialogue.worker_agent_revision_id,
                    AgentDialogueDelivery.source_run_id == source_run.id,
                    AgentDialogueDelivery.source_work_item_id == parent.id,
                    AgentDialogueDelivery.receiver_session_id == dialogue.root_session_id,
                    AgentDialogueDelivery.receiver_agent_id == dialogue.root_agent_id,
                    AgentDialogueDelivery.receiver_agent_revision_id == dialogue.root_agent_revision_id,
                    AgentDialogueDelivery.idempotency_key.startswith(f"team-media-return:{work.id}:")))).all()
                require(len(acked) == 1 and acked[0].idempotency_key ==
                        f"team-media-return:{work.id}:{acked[0].request_digest}:{dialogue.control_revision}")
                historical = True
        if _base_only or _status_only or historical:
            # Status replay may outlive the active Dialogue. Reconstruct its pinned
            # graph without granting target execution authority.
            from ..memory.models import AgentDialogue
            from .agent_team_graph_compiler import PinnedAgent, compile_media_group
            from .agent_team_graph_service import AgentTeamGraphService
            dialogue = await session.scalar(select(AgentDialogue).where(
                AgentDialogue.worker_session_id == source_run.session_id))
            authorization = dialogue.authorization_json if dialogue else None
            from .agent_delegation_service import canonical
            require(isinstance(authorization, dict) and dialogue.authorization_hash ==
                    hashlib.sha256(canonical(authorization).encode("utf-8")).hexdigest())
            auth = authorization.get("team_graph")
            require(isinstance(auth, dict) and auth.get("ref") == ref)
            envelope = await AgentTeamGraphService().get_in_session(session, UUID(ref["graph_id"]),
                actor_user_id=owner, revision_id=UUID(ref["revision_id"]))
            require(envelope["content_hash"] == auth.get("graph_hash")
                    and envelope["project_id"] in (None, str(work.project_id)))
            if auth.get("handoff_inputs"):
                from .agent_team_handoff_inputs import _resolve_group_handoff_inputs
                current = await _resolve_group_handoff_inputs(session, service=service, owner_user_id=owner,
                    root_session_id=dialogue.root_session_id, case_key=auth["case_key"],
                    graph={**envelope, "case_project_id": str(work.project_id)}, target_group=ref["group_key"], active=True)
                require(current == auth["handoff_inputs"])
            pins = {key: PinnedAgent(UUID(str(pin["agent_id"])), UUID(str(pin["revision_id"])))
                    for key, pin in auth["nodepins"].items()}
            compiled = compile_media_group(envelope, ref["group_key"], pins)
            if historical:
                require(auth.get("execution_kind", "generic") == compiled.execution_kind
                        and auth.get("target_project_id") == str(work.project_id))
            require(set(pins) == {compiled.graph.root.key, *(node.key for node in compiled.graph.roster)}
                    and compiled.graph.root.agent_id == dialogue.root_agent_id
                    and compiled.graph.root.revision_id == dialogue.root_agent_revision_id
                    and auth.get("root_key") == compiled.graph.root.key
                    and auth.get("leader_key") == compiled.graph.leader.key)
        else:
            auth, compiled = await _compile(session, captured, service, ref)
        graph = compiled.graph
        if _base_only or _status_only:
            await service.validate_read_authority(session, graph.root.agent_id, graph.root.revision_id,
                                                  work.project_id, parent.persona_id)
        require(compiled.execution_kind == f"media_{kind}" and graph.graph_hash == origin["graph_hash"]
                and auth.get("case_key") == origin["case_key"] and graph.leader.agent_id == source_run.agent_id
                and graph.leader.revision_id == source_run.agent_revision_id)
        nodes = {node.key: node for node in graph.roster}
        worker_key, reviewer_key = graph.allowed_pairs[0]
        worker, reviewer = nodes[worker_key], nodes[reviewer_key]
        require(str(worker.agent_id) == origin["worker_agent_id"] and str(worker.revision_id) == origin["worker_revision_id"]
                and str(reviewer.agent_id) == origin["reviewer_agent_id"] and str(reviewer.revision_id) == origin["reviewer_revision_id"]
                and reviewer.agent_id != worker.agent_id)
        for node in (graph.root, *graph.roster):
            agent = await session.get(Agent, node.agent_id, populate_existing=True)
            rev = await session.get(AgentRevision, node.revision_id, populate_existing=True)
            require(agent is not None and agent.created_by == owner and rev is not None and rev.agent_id == agent.id
                    and (node == worker or ((_base_only or _status_only) and node == reviewer) or agent.state == "active"))
            if not (_base_only or _status_only) and node in (worker, reviewer):
                require(revision_allows_agent_delegate(revision, tool_name="delegate_media", target_agent_id=agent.id,
                    target_agent_owner_id=agent.created_by, caller_agent_owner_id=caller.created_by, owner_user_id=owner))
        latest = await session.scalar(select(AgentRevision.id).where(AgentRevision.agent_id == reviewer.agent_id).order_by(AgentRevision.version.desc()).limit(1))
        if not (_base_only or _status_only):
            require(latest == reviewer.revision_id)
        if not (_base_only or _status_only):
            await service.validate_read_authority(session, reviewer.agent_id, reviewer.revision_id, work.project_id, work.persona_id)
        if _base_only:
            require(origin == {**source, "graph_id": str(graph.graph_id),
                "graph_revision_id": str(graph.graph_revision_id), "graph_group_key": graph.group_key,
                "graph_hash": graph.graph_hash, "case_key": auth.get("case_key"),
                "worker_agent_id": str(worker.agent_id), "worker_revision_id": str(worker.revision_id),
                "reviewer_agent_id": str(reviewer.agent_id), "reviewer_revision_id": str(reviewer.revision_id),
                "plan_id": str(plan.id), "plan_hash": plan.plan_hash})
            return {"owner_user_id": str(owner), "project_id": str(work.project_id), "persona_id": str(work.persona_id),
                "source": source, "graph_id": str(graph.graph_id), "graph_revision_id": str(graph.graph_revision_id),
                "graph_group_key": graph.group_key, "graph_hash": graph.graph_hash, "graph_case": origin["case_key"],
                "plan_id": str(plan.id), "plan_hash": plan.plan_hash, "producer_work_item_id": str(work.id),
                "producer_agent_id": str(worker.agent_id), "producer_revision_id": str(worker.revision_id),
                "reviewer_agent_id": str(reviewer.agent_id), "reviewer_revision_id": str(reviewer.revision_id)}
        # An ambiguous set of outputs cannot identify one immutable review target.
        rows = (await session.execute(select(GenerationRun, GenerationOutput, MediaAssetRevision)
            .join(GenerationOutput, GenerationOutput.generation_run_id == GenerationRun.id)
            .join(MediaAssetRevision, MediaAssetRevision.generation_output_id == GenerationOutput.id)
            .where(GenerationRun.plan_id == plan.id, GenerationRun.status == "succeeded"))).all()
        require(len(rows) == 1)
        generation, output, asset = rows[0]
        if receipt.get("receipt_recovered") is True:
            # Recovery can establish the legacy image path, but an uncertain
            # video intent is not a normal producer receipt for sampled QA.
            require(kind != "video")
            saved = (work.metadata_json or {}).get("generation_receipt_recovered") or {}
            run_result = run.result or {}
            require(receipt.get("plan_id") == str(plan.id) and receipt.get("generation_run_id") == str(generation.id)
                    and str(output.id) in receipt.get("output_ids", []) and str(asset.id) in receipt.get("asset_revision_ids", [])
                    and saved.get("proof_hash") == receipt.get("proof_hash") == run_result.get("proof_hash")
                    and saved.get("generation_run_id") == str(generation.id) and run_result.get("generation_run_id") == str(generation.id)
                    and run_result.get("plan_id") == str(plan.id) and run_result.get("work_item_id") == str(work.id)
                    and run_result.get("agent_run_id") == str(run.id) and str(output.id) in run_result.get("output_ids", [])
                    and str(asset.id) in run_result.get("asset_revision_ids", []))
        else:
            run_result = run.result if isinstance(run.result, dict) else {}
            output_ids = run_result.get("output_ids")
            require(run_result.get("generation_run_id") == str(generation.id)
                    and isinstance(output_ids, list) and str(output.id) in output_ids)
        require(all((row.owner_user_id, row.project_id) == (owner, work.project_id) for row in (generation, output, asset)))
        require(asset.persona_id == work.persona_id and asset.source_kind == "generation_output"
                and asset.source_id == output.id and output.byte_binding_state == "managed"
                and asset.sha256 == output.sha256 and asset.revision_hash == sha256_json(_binding(asset))
                and asset.mime_type == output.mime_type and asset.mime_type in MIME_EXTENSIONS
                and asset.mime_type.startswith(f"{kind}/") and 0 < asset.size_bytes <= (IMAGE_MAX_BYTES if kind == "image" else VIDEO_MAX_BYTES))
        hashes = (asset.sha256, asset.revision_hash, plan.plan_hash, generation.run_hash, output.provenance_hash)
        require(all(isinstance(x, str) and len(x) == 64 and all(c in "0123456789abcdef" for c in x) for x in hashes))
        pin = {"asset_revision_id": str(asset.id), "output_id": str(output.id), "bytes_sha256": asset.sha256,
               "revision_hash": asset.revision_hash, "work_id": str(work.id), "agent_run_id": str(run.id),
               "plan_id": str(plan.id), "plan_hash": plan.plan_hash, "generation_run_id": str(generation.id),
               "generation_run_hash": generation.run_hash, "output_provenance_hash": output.provenance_hash}
        review = _normalise_image_review({"project_id": str(work.project_id), "persona_id": str(work.persona_id),
                  "expected_agent_revision_id": str(reviewer.revision_id), "pin": pin})
        return {"owner_user_id": str(owner), "project_id": str(work.project_id), "persona_id": str(work.persona_id),
            "source": source, "graph_id": str(graph.graph_id), "graph_revision_id": str(graph.graph_revision_id),
            "graph_group_key": graph.group_key, "graph_hash": graph.graph_hash, "graph_case": origin["case_key"],
            "plan_id": str(plan.id), "plan_hash": plan.plan_hash, "producer_work_item_id": str(work.id),
            "producer_agent_run_id": str(run.id), "producer_agent_id": str(worker.agent_id),
            "producer_revision_id": str(worker.revision_id), "reviewer_agent_id": str(reviewer.agent_id),
            "reviewer_revision_id": str(reviewer.revision_id), f"{kind}_review": review, **{k: v for k, v in pin.items() if k not in ("work_id", "agent_run_id", "plan_id", "plan_hash")}}
    except TeamMediaReviewInvalid:
        raise
    except Exception as exc:
        raise TeamMediaReviewInvalid("producer evidence mismatch") from exc


async def resolve_team_media_result(session, *, producer_work_item_id, config=None) -> dict | None:
    """Read a normal admitted review; never admit work or call a provider."""
    from uuid import UUID
    from sqlalchemy import select
    from ..memory.models import AgentRun, AgentWorkItem
    from .agent_task_invocation import _task_id_for, _request_fingerprint, _registered_route_backend, _admission_revision
    from .agent_work_runtime import _safe_json
    try:
        proof = await _validate_team_producer(session, producer_work_item_id=producer_work_item_id, config=config, kind="image", _result_replay=True)
        kind = "image"
    except TeamMediaReviewInvalid:
        proof = await _validate_team_producer(session, producer_work_item_id=producer_work_item_id, config=config, kind="video", _result_replay=True)
        kind = "video"
    task_id = _task_id_for(proof["owner_user_id"], team_media_review_key(proof))
    works = (await session.scalars(select(AgentWorkItem).where(AgentWorkItem.source_type == "api_agent_task", AgentWorkItem.source_id == task_id))).all()
    if not works:
        return None
    def require(value):
        if not value:
            raise TeamMediaReviewInvalid("review evidence mismatch")
    require(len(works) == 1)
    work = works[0]
    metadata = work.metadata_json or {}
    review_key = f"{kind}_review"
    review = metadata.get(review_key)
    if kind == "video":
        from .agent_task_invocation import _normalise_video_review
        try:
            review = _normalise_video_review(review)
        except Exception as exc:
            raise TeamMediaReviewInvalid("review evidence mismatch") from exc
        require({k: v for k, v in review.items() if k != "frames"} == proof[review_key])
        require(metadata.get("video_review_digest") == _request_fingerprint(review))
    else:
        require(review == proof[review_key])
    require(metadata.get("video_review" if kind == "image" else "image_review") is None)
    producer = await session.get(AgentWorkItem, UUID(str(proof["producer_work_item_id"])), populate_existing=True)
    require(producer is not None and review_causality_matches(work, producer))
    require(metadata.get("team_media_review_origin_digest") == _request_fingerprint(proof))
    require(work.execution_adapter == "api_agent_task" and metadata.get("team_media_review_origin") == proof
            and metadata.get("task_id") == task_id
            and metadata.get("owner_user_id") == proof["owner_user_id"]
            and work.source_revision == metadata.get("admission_revision") == _admission_revision(task_id)
            and work.intent_key == "invoke" and work.domain == "api_agent_task"
            and str(work.project_id) == proof["project_id"] and str(work.persona_id) == proof["persona_id"]
            and str(work.assigned_agent_id) == proof["reviewer_agent_id"] and str(work.agent_revision_id) == proof["reviewer_revision_id"])
    if work.state in ("pending", "claimed", "running", "retry_wait", "awaiting_approval"):
        return None
    require(work.state == "succeeded")
    if kind == "video":
        from .agent_delegation_media_inputs import MediaInputPin
        from .agent_team_video_review_inputs import resolve_video_review_frames
        uuid_keys = {"asset_revision_id", "output_id", "work_id", "agent_run_id", "plan_id", "generation_run_id"}
        pin = MediaInputPin(**{k: UUID(v) if k in uuid_keys else v for k, v in review["pin"].items()})
        try:
            frames = await resolve_video_review_frames(session, owner_user_id=UUID(proof["owner_user_id"]),
                project_id=UUID(proof["project_id"]), persona_id=UUID(proof["persona_id"]),
                reviewer_agent_id=UUID(proof["reviewer_agent_id"]), reviewer_revision_id=UUID(proof["reviewer_revision_id"]), pin=pin)
            frozen = [{"frame_sha256": f.frame_sha256, "source_video_sha256": f.source_video_sha256,
                "timestamp_seconds": f.timestamp_seconds, "mime_type": f.mime_type, "sampled_visual_only": True} for f in frames]
            require(frozen == review["frames"] and all(f.source_pin == pin and
                hashlib.sha256(f.data).hexdigest() == f.frame_sha256 for f in frames))
        except TeamMediaReviewInvalid:
            raise
        except Exception as exc:
            raise TeamMediaReviewInvalid("video evidence changed") from exc
    try:
        run_id = UUID(str(metadata.get("agent_run_id")))
    except (TypeError, ValueError):
        raise TeamMediaReviewInvalid("review evidence mismatch") from None
    run = await session.get(AgentRun, run_id, populate_existing=True)
    require(run is not None and run.status == "succeeded" and run.work_item_id == work.id
            and run.agent_id == work.assigned_agent_id and run.agent_revision_id == work.agent_revision_id
            and run.project_id == work.project_id and str(run.user_id) == proof["owner_user_id"]
            and run.work_item_attempt == work.attempt_count and work.attempt_count > 0
            and (work.active_agent_run_id is None or work.active_agent_run_id == run.id)
            and str(run.id) == str(metadata.get("agent_run_id")) and str(run.session_id) == str(metadata.get("session_id")))
    rm = run.run_metadata or {}
    task = rm.get("api_agent_task") or {}
    require(task.get("team_media_review_origin_digest") == _request_fingerprint(proof))
    require(task.get("task_id") == task_id and task.get("request_fingerprint") == metadata.get("request_fingerprint")
            and task.get(f"{review_key}_digest") == _request_fingerprint(review))
    result = (run.result or {}).get(review_key)
    require((run.result or {}).get("video_review" if kind == "image" else "image_review") is None)
    require(isinstance(result, dict) and (work.result_summary_json or {}).get(review_key) == _safe_json(result, depth=1))
    if kind == "image":
        verdict = validate_review_result(result, review=review, reviewer_agent_id=proof["reviewer_agent_id"])
    else:
        accepted, feedback = result.get("accepted"), result.get("feedback")
        require(type(accepted) is bool and isinstance(feedback, str) and len(feedback) <= 2048)
        digest = hashlib.sha256(json.dumps({"accepted": accepted, "feedback": feedback}, ensure_ascii=False,
            sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        route = result.get("route")
        frames = review["frames"]
        provenance = [{"source_pin": review["pin"], "source_video_sha256": f["source_video_sha256"],
            "frame_sha256": f["frame_sha256"], "mime_type": f["mime_type"],
            "timestamp_seconds": f["timestamp_seconds"], "scope": "sampled_visual_only"} for f in frames]
        require(result.get("result_sha256") == digest and result.get("reviewer_agent_id") == proof["reviewer_agent_id"]
            and result.get("reviewer_revision_id") == proof["reviewer_revision_id"]
            and isinstance(route, dict) and set(route) == {"provider", "model", "effort", "backend"}
            and result.get("sampled_video") == {"source_pin": review["pin"], "source_video_sha256": proof["bytes_sha256"],
                "frames": provenance, "scope": "sampled_visual_only"}
            and isinstance(result.get("media_request"), dict)
            and type(result["media_request"].get("image_count")) is int
            and result.get("media_request") == {"delivery": "client_image_data_request", "route": route,
                "image_count": len(frames), "bytes_sha256": [f["frame_sha256"] for f in frames]})
        verdict = ReviewVerdict(accepted, feedback, digest)
    route = result["route"]
    try:
        require(route.get("backend") == _registered_route_backend(route.get("provider")))
    except TeamMediaReviewInvalid:
        raise
    except Exception as exc:
        raise TeamMediaReviewInvalid("review route mismatch") from exc
    require(all(route.get(k) == (rm.get("resolved_route") or {}).get(k) == (metadata.get("route") or {}).get(k)
                for k in ("provider", "model", "effort")))
    keys = ("source", "graph_case", "graph_id", "graph_revision_id", "graph_group_key", "graph_hash", "plan_id", "plan_hash",
            "producer_work_item_id", "producer_agent_run_id", "producer_agent_id", "producer_revision_id", "asset_revision_id",
            "output_id", "bytes_sha256", "revision_hash", "generation_run_id", "generation_run_hash", "output_provenance_hash")
    resolved = {**{k: proof[k] for k in keys}, "qa_task_id": task_id, "qa_work_item_id": str(work.id),
            "qa_agent_run_id": str(run.id), "qa_agent_id": proof["reviewer_agent_id"], "qa_revision_id": proof["reviewer_revision_id"],
            "accepted": verdict.accepted, "feedback": verdict.feedback, "result_sha256": verdict.result_sha256,
            "media_request": result["media_request"],
            **({"sampled_video": result["sampled_video"]} if kind == "video" else {})}

    # Bind a historical read to the exact ACKed envelope after reconstructing
    # the result; the earlier ledger check permits this reconstruction only.
    from ..memory.models import AgentDialogue, AgentDialogueDelivery
    from .agent_delegation_service import canonical
    source = proof["source"]
    dialogue = await session.scalar(select(AgentDialogue).where(
        AgentDialogue.worker_session_id == UUID(source["source_session_id"])))
    if dialogue and dialogue.state in {"succeeded", "failed", "cancelled", "blocked"}:
        source_run = await session.get(AgentRun, UUID(source["source_run_id"]), populate_existing=True)
        incoming_id = (source_run.run_metadata or {}).get("agent_dialogue", {}).get("delivery_id")
        envelope = {"version": 1, "dialogue_id": str(dialogue.id),
            "control_revision": dialogue.control_revision, "incoming_delivery_id": incoming_id,
            "source": source, "result": resolved}
        digest = hashlib.sha256(canonical(envelope).encode("utf-8")).hexdigest()
        acked = await session.scalar(select(AgentDialogueDelivery.id).where(
            AgentDialogueDelivery.dialogue_id == dialogue.id, AgentDialogueDelivery.state == "acked",
            AgentDialogueDelivery.idempotency_key ==
                f"team-media-return:{producer_work_item_id}:{digest}:{dialogue.control_revision}",
            AgentDialogueDelivery.request_digest == digest))
        require(acked is not None)
    return resolved
