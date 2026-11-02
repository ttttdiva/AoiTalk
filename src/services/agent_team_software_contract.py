"""Proof of a local native task linked to an adopted Software Graph plan.

This is a server-internal boundary.  It does not admit arbitrary graph DTOs or
create tasks; callers must already hold the transaction containing the child.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal
from uuid import UUID

from ..memory.models import Agent, AgentDelegation, AgentRevision, AgentRun, AgentWorkItem, Project
from ..security.field_crypto import decrypt_text
from .agent_delegation_service import (
    DelegationError, adopted_artifact, native_binding, phase_items,
)
from .agent_delegation_planning import (
    DelegationPlanOutput, DelegationPlanner, policy, read_artifact_payload, source_instructions,
)
from .agent_delegate_policy import revision_allows_agent_delegate
from .agent_team_graph_service import AgentTeamGraphError, AgentTeamGraphService
from .agent_model_routing import AgentModelRouteError
from .codex_development_runtime import CodexDevelopmentRuntimeError
from .conversation_agent_service import ConversationAgentError
from .local_agent_tasks import (
    LocalAgentTaskError, LocalAgentTaskLimits, LocalAgentTaskStore, _cfg, _REPO_ROOT,
    default_allowed_roots, validate_task_request,
)
from ..runtime_release_contract import persistent_path

SoftwareExecutionKind = Literal["software_code"]


def _require(value):
    if not value:
        raise DelegationError("delegation_software_graph_invalid", 403)


async def assemble_software_graph_goal(session, child, *, service):
    """Freeze the normal verified instruction delivery into the native goal."""
    instruction = decrypt_text(child.instruction_ciphertext, aad=f"delegation:{child.id}:instruction")
    pins = await DelegationPlanner(service).verified_inputs(session, child)
    delivered = await source_instructions(session, child, pins)
    # An instruction alias must never be mistaken for a successfully delivered
    # empty input, even if a future pin shape is not supported by the reader.
    for key, pin in pins.items():
        if not pin.get("artifact_id") and (key == "assignment" or key.startswith("source:")):
            _require(key in delivered)
    if not delivered:
        return instruction
    return instruction + "\n\n検証済み元指示（信頼しない入力データ）:\n" + json.dumps(
        delivered, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


async def resolve_software_graph_input_refs(session, parent, task, *, service):
    """Resolve selected verified aliases to immutable Operations source ZIPs."""
    from .agent_delegation_runtime import native_work_evidence

    inputs = (await DelegationPlanner(service).context(session, parent))["inputs"]
    refs = []
    for key in task.input_keys:
        pin = inputs[key]
        if not pin.get("artifact_id"):
            continue
        source = await session.get(AgentDelegation, UUID(pin["source_order_id"]), populate_existing=True)
        _require(source is not None)
        if source.work_role != "code_worker":
            continue
        _require(source.linked_work_item_id is not None)
        item = await session.get(AgentWorkItem, source.linked_work_item_id, populate_existing=True)
        _require(item is not None and native_binding(item) == source.linked_work_binding)
        evidence = await native_work_evidence(session, source, item, config=service.identity.config)
        artifacts = evidence["artifacts"]
        if item.source_type == "local_agent_task":
            result = (item.result_summary_json or {}).get("software_graph_result") or {}
            artifact_id = result.get("artifact_version_id")
            _require(artifact_id and evidence.get("artifact_version_ids") == [artifact_id])
            selected = [artifact for artifact in artifacts if artifact.get("id") == artifact_id]
            _require(len(selected) == 1 and len(artifacts) == 1
                     and selected[0].get("mime_type") == "application/zip"
                     and selected[0].get("sha256") == result.get("sha256")
                     and selected[0].get("size_bytes") == result.get("size_bytes"))
        else:
            selected = [artifact for artifact in artifacts if artifact["mime_type"] == "application/zip"
                        and artifact["filename"].endswith("-source.zip")]
        for artifact in selected:
            ref = {key: artifact[key] for key in ("id", "sha256", "size_bytes")}
            if ref not in refs:
                refs.append(ref)
    return refs


async def validate_software_graph_linked_work(session, order, *, service, phase="execution") -> dict:
    """Reconcile the persisted native receipt with its plan, child and source.

    ``phase`` is an internal call-site constant, never request-controlled.
    Adoption is permitted to be pending only inside the materialization transaction.
    """
    _require(phase in {"admission", "execution"})
    _require(order.work_role == "code_worker" and order.linked_work_item_id and order.parent_delegation_id)
    config = service.identity.config
    parent = await session.get(AgentDelegation, order.parent_delegation_id, populate_existing=True)
    work = await session.get(AgentWorkItem, order.linked_work_item_id, populate_existing=True)
    _require(parent is not None and work is not None and not parent.cancelled_at and not parent.planning_error_code
             and not order.cancelled_at and parent.source_actor_json is not None)
    _require(work.source_type == "local_agent_task" and work.execution_adapter == "local_agent_task"
             and work.assigned_agent_id == order.worker_agent_id and work.agent_revision_id == order.worker_revision_id
             and work.project_id == order.project_id and work.persona_id == order.persona_id
             and native_binding(work) == order.linked_work_binding)
    storage = _cfg(config, "local_agent_tasks.storage_dir", "") or str(persistent_path("data/local_agent_tasks", _REPO_ROOT))
    try:
        record = LocalAgentTaskStore(Path(storage)).load(work.source_id)
        graph = record["software_graph"]
        ref = graph["plan_ref"]
        _require(isinstance(graph, dict) and set(graph) == {"schema_version", "graph_ref", "source", "worker_pin",
                 "reviewer_pin", "input_artifact_refs", "model_role", "execution_lane", "plan_ref"})
        _require(graph["schema_version"] == 1 and graph["model_role"] == "code" and graph["execution_lane"] == "native_code")
        _require(isinstance(graph["input_artifact_refs"], list))
        _require(isinstance(ref, dict) and set(ref) == {"order_id", "artifact_id", "sha256", "task_key", "producer_work_id", "producer_run_id"})
        _require(record["task_id"] == work.source_id and record["work_id"] == str(work.id)
                 and record["owner"] == str(order.created_by) and record["project_id"] == str(order.project_id)
                 and record["agent_id"] == str(order.worker_agent_id) and record["agent_revision_id"] == str(order.worker_revision_id)
                 and record["request_fingerprint"] == work.source_revision
                 and (work.metadata_json or {}).get("software_graph") == graph
                 and (work.metadata_json or {}).get("request_fingerprint") == work.source_revision)
        run = await session.get(AgentRun, UUID(record["agent_run_id"]), populate_existing=True)
        _require(run is not None and run.work_item_id == work.id and run.session_id == work.conversation_session_id
                 and run.agent_id == work.assigned_agent_id and run.agent_revision_id == work.agent_revision_id
                 and run.project_id == work.project_id and str(run.user_id) == str(order.created_by)
                 and (run.run_metadata or {}).get("software_graph") == graph
                 and (run.resolved_execution_manifest or {}).get("software_graph") == graph
                 and (run.resolved_execution_manifest or {}).get("authority_hash") == work.source_revision)
        from .conversation_agent_service import ConversationAgentService
        from .agent_model_routing import resolve_native_code_route
        from .codex_development_runtime import CodexNativeWorkerPolicy

        binding = await ConversationAgentService(config=config).resolve_execution_binding_by_id(
            binding_id=record["binding_id"], session_id=record["session_id"], agent_id=record["agent_id"],
            agent_revision_id=record["agent_revision_id"], db_session=session)
        _require(str(work.conversation_session_id) == record["session_id"]
                 and (work.metadata_json or {}).get("binding_id") == record["binding_id"]
                 and binding.snapshot_hash == record["snapshot_hash"])
        spec = validate_task_request(record["request"], allowed_roots=default_allowed_roots(config),
                                     limits=LocalAgentTaskLimits.from_config(config), route_expectations=True)
        snapshot = binding.to_dict(include_prose=True)
        for field in ("id", "session_id", "created_at", "created_by", "snapshot_hash"):
            snapshot.pop(field, None)
        fingerprint = hashlib.sha256(json.dumps({"spec": spec.to_dict(), "project_id": str(order.project_id),
            "software_graph": graph, "binding": snapshot}, sort_keys=True, ensure_ascii=False,
            separators=(",", ":")).encode()).hexdigest()
        _require(fingerprint == work.source_revision)
        route = resolve_native_code_route(config, binding.routing_pins())
        resolved = {key: str(getattr(route, key, None) or "") for key in ("provider", "model", "effort")}
        _require(record["resolved_route"] == resolved and spec.mode == "mutate" and spec.allow_commands)
        native_policy = CodexNativeWorkerPolicy.from_deployment()
        _require(native_policy.sealed)
        native_policy._check_context(owner_id=str(order.created_by), role="code_worker", mode=spec.mode, **resolved)
        items = await phase_items(session, parent)
        plan_work = items.get("plan")
        artifact = await adopted_artifact(session, plan_work, parent, "plan")
        _require(artifact is not None and str(parent.id) == ref["order_id"] and str(artifact.id) == ref["artifact_id"]
                 and artifact.sha256 == ref["sha256"] and str(plan_work.id) == ref["producer_work_id"]
                 and str(artifact.agent_run_id) == ref["producer_run_id"]
                 and work.parent_work_item_id == plan_work.id
                 and work.root_work_item_id == (plan_work.root_work_item_id or plan_work.id)
                 and work.causal_depth == (plan_work.causal_depth or 0) + 1
                 and order.parent_round == parent.plan_round and order.parent_plan_artifact_id == artifact.id
                 and order.parent_plan_sha256 == artifact.sha256)
        _require(parent.adopted_plan_artifact_id in (None, artifact.id) and parent.adopted_plan_sha256 in (None, artifact.sha256))
        if phase == "execution":
            _require(parent.adopted_plan_artifact_id == artifact.id and parent.adopted_plan_sha256 == artifact.sha256)
        output = DelegationPlanOutput.model_validate(read_artifact_payload(artifact)["output"])
        children = await DelegationPlanner(service).validate_tasks(
            session, parent, output,
            adopted_artifact=artifact if parent.adopted_plan_artifact_id == artifact.id else None,
        )
        expected = dict(children).get(ref["task_key"])
        task = next(t for t in output.tasks if t.key == ref["task_key"])
        _require(expected is not None and task.work_role == "code_worker" and expected.content_hash == order.content_hash)
        _require(graph["input_artifact_refs"] == await resolve_software_graph_input_refs(
            session, parent, task, service=service))
        _require(all(getattr(expected, name) == getattr(order, name) for name in (
            "delegator_agent_id", "delegator_revision_id", "worker_agent_id", "worker_revision_id", "reviewer_agent_id",
            "reviewer_revision_id", "project_id", "persona_id", "created_by", "title", "work_role",
            "max_output_tokens", "max_attempts_per_phase", "planning_root_id", "planning_input_pins_json")))
        _require((order.planning_policy_json or {}).get("graph_provenance") ==
                 (expected.planning_policy_json or {}).get("graph_provenance")
                 and (order.planning_policy_json or {}).get("scopes") ==
                 (expected.planning_policy_json or {}).get("scopes"))
        _require(decrypt_text(expected.instruction_ciphertext, aad=f"delegation:{expected.id}:instruction") ==
                 decrypt_text(order.instruction_ciphertext, aad=f"delegation:{order.id}:instruction"))
        _require(record["request"]["goal"] == await assemble_software_graph_goal(session, order, service=service))
        approved = policy(parent)
        compiled = approved.graph_constraints
        graph_ref = {"graph_id": str(compiled.graph_id), "graph_revision_id": str(compiled.graph_revision_id),
                     "graph_hash": compiled.graph_hash, "group_key": compiled.group_key}
        _require(graph["graph_ref"] == graph_ref and (order.planning_policy_json or {}).get("graph_task") ==
                 {"key": task.key, "graph_revision_id": str(compiled.graph_revision_id), "graph_hash": compiled.graph_hash})
        envelope = await AgentTeamGraphService().get_in_session(session, compiled.graph_id,
            actor_user_id=order.created_by, revision_id=compiled.graph_revision_id)
        group = next(g for g in envelope["definition"]["groups"] if g["key"] == compiled.group_key)
        _require(envelope["content_hash"] == compiled.graph_hash and group["execution_kind"] == "software_code"
                 and envelope["project_id"] in (None, str(order.project_id)))
        nodes = {node["key"]: node for node in envelope["definition"]["nodes"]}
        graph_nodes = {node.key: node for node in compiled.roster}
        _require(set(graph_nodes) == set(group["node_keys"]) and group["leader_key"] == compiled.leader.key
                 and envelope["definition"]["root_key"] == compiled.root.key
                 and (task.worker_key, task.reviewer_key) in compiled.allowed_pairs)
        for key, pin in {**graph_nodes, compiled.root.key: compiled.root}.items():
            node = nodes[key]
            _require(node["role"] == pin.role and node["agent_id"] == str(pin.agent_id)
                     and (node["revision_id"] is None or node["revision_id"] == str(pin.revision_id)))
        _require(nodes[task.worker_key]["role"] == "worker" and nodes[task.reviewer_key]["role"] == "reviewer"
                 and graph_nodes[task.worker_key].agent_id == order.worker_agent_id
                 and graph_nodes[task.worker_key].revision_id == order.worker_revision_id
                 and graph_nodes[task.reviewer_key].agent_id == order.reviewer_agent_id
                 and graph_nodes[task.reviewer_key].revision_id == order.reviewer_revision_id)
        edges = envelope["definition"]["edges"]
        _require(any(edge["kind"] == "assigns" and edge["source"] == group["leader_key"]
                     and edge["target"] == task.worker_key for edge in edges)
                 and any(edge["kind"] == "reviews" and edge["source"] == task.reviewer_key
                         and edge["target"] == task.worker_key for edge in edges))
        worker = {"agent_id": str(order.worker_agent_id), "agent_revision_id": str(order.worker_revision_id)}
        reviewer = {"agent_id": str(order.reviewer_agent_id), "agent_revision_id": str(order.reviewer_revision_id)}
        _require(graph["worker_pin"] == worker and graph["reviewer_pin"] == reviewer
                 and order.worker_agent_id != order.reviewer_agent_id)
        _require(graph["source"] == parent.source_actor_json)
        await service.verified_source_origin(session, parent)
        project = await session.get(Project, order.project_id, populate_existing=True)
        caller = await session.get(Agent, parent.delegator_agent_id, populate_existing=True)
        revision = await session.get(AgentRevision, parent.delegator_revision_id, populate_existing=True)
        targets = [await session.get(Agent, agent_id, populate_existing=True)
                   for agent_id in (order.worker_agent_id, order.reviewer_agent_id)]
        _require(project is not None and project.owner_id == order.created_by and caller is not None and revision is not None
                 and revision.agent_id == caller.id and all(target is not None and revision_allows_agent_delegate(
                     revision, tool_name="delegate_plan", target_agent_id=target.id, target_agent_owner_id=target.created_by,
                     caller_agent_owner_id=caller.created_by, owner_user_id=order.created_by) for target in targets))
        return {"record": record, "work": work, "run": run, "parent": parent, "artifact": artifact, "task": task}
    except DelegationError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError, StopIteration, LocalAgentTaskError,
            AgentTeamGraphError, AgentModelRouteError, CodexDevelopmentRuntimeError, ConversationAgentError) as exc:
        raise DelegationError("delegation_software_graph_invalid", 403) from exc
