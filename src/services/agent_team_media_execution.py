"""Atomic admission of a media Team's first generation work item."""
from __future__ import annotations

from dataclasses import replace
from uuid import UUID

from sqlalchemy import select

from ..features import Features
from ..memory.models import Agent, AgentRevision, AgentWorkItem, GenerationPlan, PersonaRevision, Project
from .agent_delegate_policy import revision_allows_agent_delegate
from .agent_delegation_service import AgentDelegationService, DelegationError
from .agent_team_graph_admission import TeamGraphRef
from .agent_team_graph_compiler import PinnedAgent, compile_media_group
from .agent_team_graph_service import AgentTeamGraphService
from .agent_work_runtime import AgentWorkCoordinator, _safe_json
from .media_operations_generation_service import MediaOperationsGenerationService
from .media_operations_service import _idempotency_key
from .media_work_sources import MediaGenerationWorkSource


def _deny(code="team_media_invalid", status=403):
    raise DelegationError(code, status)


async def _compile(session, captured, service, ref):
    auth = await service._inherited_team_graph(session, captured.run, captured.work,
                                                captured.actor, captured.project_id)
    if not auth or TeamGraphRef.model_validate(ref).model_dump(mode="json") != auth.get("ref"):
        _deny("delegation_graph_ref_mismatch")
    graph_ref = TeamGraphRef.model_validate(auth["ref"])
    envelope = await AgentTeamGraphService().get_in_session(
        session, graph_ref.graph_id, actor_user_id=captured.actor, revision_id=graph_ref.revision_id)
    if (envelope["content_hash"] != auth.get("graph_hash") or
            envelope["project_id"] not in (None, str(captured.project_id))):
        _deny("delegation_graph_authorization_mismatch")
    try:
        pins = {key: PinnedAgent(UUID(str(pin["agent_id"])), UUID(str(pin["revision_id"])))
                for key, pin in auth["nodepins"].items()}
        compiled = compile_media_group(envelope, graph_ref.group_key, pins)
        relevant = {compiled.graph.root.key, *(node.key for node in compiled.graph.roster)}
        if set(pins) != relevant:
            raise ValueError("pin set")
    except (KeyError, TypeError, ValueError) as exc:
        raise DelegationError("delegation_graph_authorization_mismatch", 403) from exc
    return auth, compiled


async def admit_team_media_from_current_agent(*, project_id, team_graph_ref, media_request,
                                               idempotency_key, db_manager=None, config=None):
    if db_manager is None:
        from ..tools.agent_dialogue import _service
        dialogue = await _service()
        db_manager = dialogue._manager()
        if config is None:
            config = dialogue.config
    service = AgentDelegationService(db_manager, config=config)
    service.require_enabled()
    session = await service.identity._session()
    try:
        captured = await service.capture_current_source_actor(session, project_id=project_id)
        _, compiled = await _compile(session, captured, service, team_graph_ref)
        result = await admit_team_media(session, delegation_service=service, source_actor=captured,
                                        compiled=compiled, request=media_request,
                                        idempotency_key=idempotency_key)
        await session.commit()
        return result
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def admit_team_media(session, *, delegation_service, source_actor, compiled, request,
                           idempotency_key, coordinator=None, work_source=None):
    service = delegation_service
    service.require_enabled()
    captured = await service.capture_current_source_actor(session, project_id=source_actor.project_id)
    if captured.source != source_actor.source or captured.project_id != source_actor.project_id:
        _deny("delegation_source_invalid")
    graph = compiled.graph
    ref = {"graph_id": str(graph.graph_id), "revision_id": str(graph.graph_revision_id),
           "group_key": graph.group_key}
    auth, actual = await _compile(session, captured, service, ref)
    if compiled != actual:
        _deny("delegation_graph_authorization_mismatch")
    expected_kind = {"media_image": "image", "media_video": "video"}.get(compiled.execution_kind)
    if expected_kind is None:
        _deny("team_media_kind_unsupported", 422)
    if (graph.leader.agent_id != captured.run.agent_id or
            graph.leader.revision_id != captured.run.agent_revision_id):
        _deny("delegation_graph_leader_mismatch")
    nodes = {node.key: node for node in graph.roster}
    worker_key, reviewer_key = graph.allowed_pairs[0]
    worker, reviewer = nodes[worker_key], nodes[reviewer_key]
    for node in (graph.root, *graph.roster):
        agent = await session.get(Agent, node.agent_id, populate_existing=True)
        revision = await session.get(AgentRevision, node.revision_id, populate_existing=True)
        if (agent is None or agent.created_by != captured.actor or agent.state != "active" or
                revision is None or revision.agent_id != agent.id):
            _deny("delegation_graph_agent_invalid")
        if node in (worker, reviewer) and not revision_allows_agent_delegate(
                captured.revision, tool_name="delegate_media", target_agent_id=agent.id,
                target_agent_owner_id=agent.created_by, caller_agent_owner_id=captured.caller.created_by,
                owner_user_id=captured.actor):
            _deny("delegate_policy_denied")
    allowed = {"persona_revision_id", "creative_recipe_revision_id", "workspace_id", "requested_outputs",
               "request_spec", "content_item_id", "content_variant_id"}
    if not isinstance(request, dict) or set(request) - allowed:
        _deny("team_media_invalid_request", 422)
    try:
        persona = await session.get(PersonaRevision, UUID(str(request["persona_revision_id"])))
    except (ValueError, KeyError, TypeError):
        _deny("team_media_invalid_request", 422)
    if persona is None or persona.project_id != captured.project_id:
        _deny("team_media_project_mismatch")
    await service.validate_read_authority(session, reviewer.agent_id, reviewer.revision_id,
                                          captured.project_id, persona.persona_id)
    for node, capability in ((worker, "media"),):
        decision = await service.authority.resolve(session=session, agent_id=node.agent_id,
            revision_id=node.revision_id, project_id=captured.project_id, persona_id=persona.persona_id,
            required_capability=capability)
        if not decision.allowed:
            _deny("delegation_authority_denied")
    generation = MediaOperationsGenerationService()
    # The capture returns the validated human UUID; Media services consume a
    # human actor object, as in the ordinary Media discovery registration.
    media_actor = {"id": str(captured.actor), "user_id": str(captured.actor),
                   "actor_type": "human", "is_agent": False}
    # There is no Plan row to lock on the first request. Serialize admissions
    # for the project before checking its idempotency key, so another source
    # cannot race an uncommitted initial Plan and its provenance-bearing Work.
    project = await session.scalar(select(Project).where(Project.id == captured.project_id).with_for_update())
    if project is None:
        _deny("team_media_project_mismatch")
    previous_plan = await session.scalar(select(GenerationPlan).where(
        GenerationPlan.owner_user_id == captured.actor, GenerationPlan.project_id == captured.project_id,
        GenerationPlan.idempotency_key == _idempotency_key(idempotency_key)).with_for_update())
    detail = await generation.create_generation_plan(session, media_actor, **request,
                                                       idempotency_key=idempotency_key, commit=False)
    if detail.get("generation_kind") != expected_kind or str(detail.get("project_id")) != str(captured.project_id):
        _deny("team_media_project_mismatch")
    plan_id, plan_hash = str(detail["id"]), detail["plan_hash"]
    existing = await session.scalar(select(AgentWorkItem).where(
        AgentWorkItem.source_type == "media.generation.plan", AgentWorkItem.source_id == plan_id).with_for_update())
    origin = {"version": 1, **captured.source, "graph_id": str(graph.graph_id),
              "graph_revision_id": str(graph.graph_revision_id), "graph_group_key": graph.group_key,
              "graph_hash": graph.graph_hash, "case_key": auth.get("case_key"),
              "worker_agent_id": str(worker.agent_id), "worker_revision_id": str(worker.revision_id),
              "reviewer_agent_id": str(reviewer.agent_id), "reviewer_revision_id": str(reviewer.revision_id),
              "plan_id": plan_id, "plan_hash": plan_hash}
    if not isinstance(origin["case_key"], str) or _safe_json(origin, depth=1) != origin:
        _deny("team_media_origin_invalid")
    if existing:
        prior = (existing.metadata_json or {}).get("team_media_origin")
        stable = set(origin) - {"source_run_id", "source_work_item_id", "source_session_id"}
        parent = await session.get(AgentWorkItem, existing.parent_work_item_id) if existing.parent_work_item_id else None
        if (not isinstance(prior, dict) or any(prior.get(k) != origin[k] for k in stable) or
                parent is None or str(parent.id) != prior.get("source_work_item_id") or
                parent.project_id != captured.project_id or existing.project_id != captured.project_id or
                existing.assigned_agent_id != worker.agent_id or existing.agent_revision_id != worker.revision_id or
                existing.source_revision != plan_hash or existing.root_work_item_id != (parent.root_work_item_id or parent.id) or
                existing.causal_depth != parent.causal_depth + 1 or existing.causation_id != str(parent.id)):
            _deny("team_media_idempotency_conflict", 409)
        return {"plan_id": plan_id, "plan_hash": plan_hash, "producer_work_item_id": str(existing.id),
                "producer_agent_revision_id": str(worker.revision_id), "graph_case": prior["case_key"],
                "source": {k: prior[k] for k in captured.source}}
    # Existing manual plans cannot be retrofitted with provenance.
    if previous_plan is not None:
        _deny("team_media_idempotency_conflict", 409)
    # Use the same service, authority resolver, actor and feature gate as
    # ordinary Media discovery.  Without the resolver the source fails closed.
    source = work_source or MediaGenerationWorkSource(
        generation, authority_resolver=service.authority.resolve,
        actor=media_actor, feature_checker=Features.media_operations_autonomy,
    )
    candidate = await source.candidate_for_plan(session, media_actor, detail)
    if (candidate is None or candidate.source_type != "media.generation.plan" or
            str(candidate.source_id) != plan_id or candidate.source_revision != plan_hash or
            str(candidate.project_id) != str(captured.project_id) or
            str(candidate.persona_id) != str(persona.persona_id) or
            str(candidate.assigned_agent_id) != str(worker.agent_id) or
            str(candidate.agent_revision_id) != str(worker.revision_id) or
            candidate.execution_adapter != "media.generation" or
            "media" not in candidate.required_capabilities or
            (candidate.metadata.get("payload") or {}).get("plan_hash") != plan_hash):
        _deny("team_media_candidate_invalid")
    parent = captured.work
    root = await session.get(AgentWorkItem, parent.root_work_item_id or parent.id)
    if parent.project_id != captured.project_id or root is None or root.project_id != captured.project_id:
        _deny("delegation_source_scope_mismatch")
    # Convert the media bridge before adding provenance: its legacy serializer
    # deliberately drops session-shaped keys. The runtime serializer validates it.
    candidate = candidate.normalized()
    candidate = replace(candidate, root_work_item_id=str(parent.root_work_item_id or parent.id),
                        parent_work_item_id=str(parent.id), causation_id=str(parent.id),
                        causal_depth=parent.causal_depth + 1,
                        metadata={**candidate.metadata, "team_media_origin": origin})
    coordinator = coordinator or AgentWorkCoordinator(service.identity._db_manager, config=service.identity.config)
    result = await coordinator.materialize(candidate, session=session)
    if not result:
        _deny("team_media_materialization_failed", 409)
    return {"plan_id": plan_id, "plan_hash": plan_hash, "producer_work_item_id": str(result["id"]),
            "producer_agent_revision_id": str(worker.revision_id), "graph_case": origin["case_key"],
            "source": captured.source}
