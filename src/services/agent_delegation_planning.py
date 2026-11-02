"""Bounded manager plans adopted by the server, never by model authority."""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import inspect, or_, select, update
from sqlalchemy.exc import DBAPIError, TimeoutError as SQLAlchemyTimeoutError

from ..memory.models import AgentDelegation, AgentDelegationArtifact, AgentDelegationBudgetReservation, AgentRevision, AgentRun, AgentWorkItem, Project
from ..security.field_crypto import decrypt_text, encrypt_text
from .handoff_graph import analyze_handoffs
from .agent_team_graph_compiler import CompiledGroup
from .agent_delegation_service import DelegationError, adopted_artifact, canonical, delegation_uuid, phase_identity, phase_items

KEY_PATTERN = r"^[a-z0-9][a-z0-9_.:-]*$"
PlanRole = Literal["researcher", "writer", "data_worker", "general_worker", "code_worker"]


class PlanningRosterEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=80, pattern=KEY_PATTERN)
    agent_id: UUID
    agent_revision_id: UUID
    allowed_work_roles: list[PlanRole] = Field(default_factory=lambda: ["general_worker"], max_length=4)
    scope_keys: list[str] = Field(default_factory=lambda: ["current"], min_length=1, max_length=9)
    can_review: bool = False
    can_delegate: bool = False


class DelegationPlanningPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    roster: list[PlanningRosterEntry] = Field(min_length=2, max_length=32)
    graph_constraints: CompiledGroup | None = None
    brand_keys: list[str] = Field(default_factory=list, max_length=8)
    input_artifacts: dict[str, UUID] = Field(default_factory=dict, max_length=16)
    max_children: int = Field(default=2, ge=1, le=8)
    max_total_children: int = Field(default=16, ge=1, le=64)
    max_depth: int = Field(default=3, ge=1, le=3)
    max_output_tokens_per_call: int = Field(default=2048, ge=256, le=8192)
    max_total_output_tokens: int = Field(default=65536, ge=256, le=262144)
    max_attempts_per_phase: int = Field(default=2, ge=1, le=3)
    max_rework_rounds: int = Field(default=1, ge=0, le=2)

    @model_validator(mode="after")
    def unique_keys(self):
        if len({entry.key for entry in self.roster}) != len(self.roster) or len({entry.agent_id for entry in self.roster}) != len(self.roster):
            raise ValueError("roster keys and employees must be unique")
        if len(set(self.brand_keys)) != len(self.brand_keys) or any(
                len(key) > 80 or re.fullmatch(KEY_PATTERN, key) is None or key == "assignment"
                or key.startswith(("origin:", "child:", "source:")) for key in self.input_artifacts):
            raise ValueError("invalid planning keys")
        software = self.graph_constraints is not None and self.graph_constraints.execution_kind == "software_code"
        if any("code_worker" in entry.allowed_work_roles for entry in self.roster) and not software:
            raise ValueError("code worker requires a software graph")
        if software:
            nodes_by_key = {node.key: node for node in self.graph_constraints.roster if node.role in {"worker", "reviewer"}}
            if set(nodes_by_key) != {entry.key for entry in self.roster}:
                raise ValueError("software graph roster mismatch")
            for entry in self.roster:
                node = nodes_by_key[entry.key]
                if (entry.allowed_work_roles != (["code_worker"] if node.role == "worker" else [])
                        or entry.can_review != (node.role == "reviewer") or entry.can_delegate):
                    raise ValueError("software graph role mismatch")
        if self.graph_constraints:
            nodes = {node.key: node for node in self.graph_constraints.roster}
            for entry in self.roster:
                node = nodes.get(entry.key)
                if (node is None or node.agent_id != entry.agent_id
                        or node.revision_id != entry.agent_revision_id
                        or (entry.can_review and node.role != "reviewer")):
                    raise ValueError("graph roster pin mismatch")
        return self


class PlannedAssignment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    key: str = Field(min_length=1, max_length=80, pattern=KEY_PATTERN)
    worker_key: str = Field(min_length=1, max_length=80)
    reviewer_key: str = Field(min_length=1, max_length=80)
    scope_key: str = Field(default="current", min_length=1, max_length=80)
    work_role: PlanRole
    title: str = Field(min_length=1, max_length=160)
    instruction: str = Field(min_length=1, max_length=6000)
    input_keys: list[str] = Field(default_factory=lambda: ["assignment"], min_length=1, max_length=17)
    depends_on: list[str] = Field(default_factory=list, max_length=8)
    result_inputs: dict[str, str] = Field(default_factory=dict, max_length=16)
    max_output_tokens: int = Field(default=512, ge=256, le=8192)
    max_attempts: int = Field(default=1, ge=1, le=3)


class DelegationPlanOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    summary: str = Field(min_length=1, max_length=2000)
    body: str = Field(min_length=1, max_length=20000)
    accepted: None = None
    tasks: list[PlannedAssignment] = Field(min_length=1, max_length=8)


def graph_child_provenance(graph, worker_key, reviewer_key):
    """Capture the server-compiled graph and selected node identities for a child."""
    nodes = {node.key: node for node in graph.roster}

    def pin(node):
        return {"key": node.key, "agent_id": str(node.agent_id), "revision_id": str(node.revision_id)}

    return {"graph_id": str(graph.graph_id), "graph_revision_id": str(graph.graph_revision_id),
            "graph_hash": graph.graph_hash, "group_key": graph.group_key,
            "root": pin(graph.root), "leader": pin(graph.leader),
            "worker": pin(nodes[worker_key]), "reviewer": pin(nodes[reviewer_key])}


_RESERVATION_STAGES = frozenset({"root_lock", "reservation_lookup", "work_lock", "budget_update", "commit"})
_RESERVATION_ERRORS = frozenset({"builtins.TimeoutError", "sqlalchemy.exc.TimeoutError", "sqlalchemy.exc.DBAPIError", "other"})


def safe_reservation_diagnostic(value):
    if type(value) is not dict:
        return {}
    stage, kind, elapsed = value.get("stage"), value.get("exception_type"), value.get("elapsed_ms")
    if (type(stage) is not str or stage not in _RESERVATION_STAGES
            or type(kind) is not str or kind not in _RESERVATION_ERRORS
            or type(elapsed) is not int or not 0 <= elapsed <= 3600000):
        return {}
    return {"stage": stage, "exception_type": kind, "elapsed_ms": elapsed}


async def _reservation_await(stage, awaitable):
    started = time.monotonic()
    try:
        return await awaitable
    except Exception as exc:
        try:
            if isinstance(exc, SQLAlchemyTimeoutError):
                kind = "sqlalchemy.exc.TimeoutError"
            elif isinstance(exc, DBAPIError):
                kind = "sqlalchemy.exc.DBAPIError"
            elif isinstance(exc, TimeoutError):
                kind = "builtins.TimeoutError"
            else:
                kind = "other"
            exc.reservation_diagnostic = {"stage": stage, "exception_type": kind,
                "elapsed_ms": min(3600000, max(0, int((time.monotonic() - started) * 1000)))}
        except Exception:
            pass
        raise


def policy(order):
    value = order.planning_policy_json or {}
    return DelegationPlanningPolicy.model_validate(value["policy"]) if value.get("policy") else None


async def active_children(session, order):
    return list((await session.scalars(select(AgentDelegation).where(
        AgentDelegation.parent_delegation_id == order.id,
        or_(AgentDelegation.parent_plan_artifact_id.is_(None),
            (AgentDelegation.parent_plan_artifact_id == order.adopted_plan_artifact_id)
            & (AgentDelegation.parent_round == order.plan_round)))
        .order_by(AgentDelegation.created_at, AgentDelegation.id))).all())


def read_artifact_payload(artifact):
    raw = decrypt_text(artifact.body_ciphertext, aad=f"delegation-artifact:{artifact.id}")
    if hashlib.sha256(raw.encode()).hexdigest() != artifact.sha256:
        raise DelegationError("delegation_artifact_corrupt", 409)
    return json.loads(raw)


async def source_instructions(session, order, pins):
    """Deliver verified ancestor instructions (e.g. a brief) as untrusted input data.

    Instruction pins are hash pins, so a child otherwise receives only the
    planner's assignment prose. Only the parent ("assignment") and planning
    ancestors ("source:*") are delivered; remote origins stay pin-only.
    """
    result = {}
    for key, pin in pins.items():
        if pin.get("artifact_id") or not (key == "assignment" or key.startswith("source:")):
            continue
        source = await session.get(AgentDelegation, delegation_uuid(pin["source_order_id"]))
        if source is None or source.id == order.id or source.content_hash != pin.get("content_hash"):
            raise DelegationError("delegation_planning_input_changed", 403)
        text = decrypt_text(source.instruction_ciphertext, aad=f"delegation:{source.id}:instruction")
        if not text or len(text) > 12000:
            raise DelegationError("delegation_plan_input_too_large")
        result[key] = {"source_order_id": str(source.id), "title": source.title, "content_hash": source.content_hash,
            "trust": "untrusted_input_data", "content": text}
    return result


class DelegationPlanner:
    def __init__(self, service):
        self.service = service

    async def prepare(self, session, order, requested):
        approved = DelegationPlanningPolicy.model_validate(requested)
        root_project = await session.get(Project, order.project_id)
        if root_project is None or root_project.owner_id != order.created_by:
            raise DelegationError("delegation_planning_owner_required", 403)
        scopes = {"current": {"project_id": str(order.project_id), "persona_id": str(order.persona_id) if order.persona_id else None}}
        if approved.brand_keys:
            from .media_brand_portfolio_service import MediaBrandPortfolioService
            portfolio = await MediaBrandPortfolioService().readback(session, {"id": str(order.created_by), "role": "admin", "actor_type": "human"})
            if portfolio["status"] != "ready" or portfolio["headquarters_project_id"] != str(order.project_id):
                raise DelegationError("delegation_planning_headquarters_required", 403)
            for key in approved.brand_keys:
                brand = next((row for row in portfolio["brands"] if row["definition"]["brand_key"] == key), None)
                if brand is None:
                    raise DelegationError("delegation_planning_brand_unknown")
                scopes[key] = {"project_id": brand["project_id"], "persona_id": brand["persona"]["id"],
                    "persona_revision_id": brand["persona"]["current_revision"]["id"], "source_sha256": portfolio["source"]["sha256"]}
        for entry in approved.roster:
            if entry.agent_id == order.delegator_agent_id or not set(entry.scope_keys).issubset(scopes):
                raise DelegationError("delegation_planning_roster_invalid")
            for key in entry.scope_keys:
                scope = scopes[key]
                await self.service.validate_read_authority(session, entry.agent_id, entry.agent_revision_id,
                    delegation_uuid(scope["project_id"]), delegation_uuid(scope["persona_id"]) if scope["persona_id"] else None)
        # Every eventual recipient of aggregated results must be authorized at
        # their source scope, including HQ worker/reviewer, before planning.
        for scope in scopes.values():
            for phase in ("work", "review", "return"):
                aid, rid = phase_identity(order, phase)
                await self.service.validate_read_authority(session, aid, rid,
                    delegation_uuid(scope["project_id"]), delegation_uuid(scope["persona_id"]) if scope["persona_id"] else None)
        value = {"policy": approved.model_dump(mode="json"), "scopes": scopes, "actor": "delegator", "input_pins": {}}
        for key, artifact_id in approved.input_artifacts.items():
            artifact = await session.get(AgentDelegationArtifact, artifact_id)
            if artifact is None:
                raise DelegationError("delegation_planning_input_missing", 404)
            source = await session.get(AgentDelegation, artifact.delegation_id)
            item = await session.get(AgentWorkItem, artifact.work_item_id)
            adopted = await adopted_artifact(session, item, source, artifact.phase) if source else None
            if source is None or source.created_by != order.created_by or adopted is None or adopted.id != artifact.id:
                raise DelegationError("delegation_planning_input_not_adopted", 403)
            await self.service.validate_authority(session, source)
            read_artifact_payload(artifact)
            value["input_pins"][key] = {"artifact_id": str(artifact.id), "sha256": artifact.sha256,
                "work_item_id": str(artifact.work_item_id), "agent_run_id": str(artifact.agent_run_id),
                "source_order_id": str(source.id), "project_id": str(source.project_id),
                "persona_id": str(source.persona_id) if source.persona_id else None}
        order.planning_policy_json = value
        order.max_output_tokens = approved.max_output_tokens_per_call
        order.max_attempts_per_phase = approved.max_attempts_per_phase
        await self.verified_inputs(session, order)

    async def verify_brand_scopes(self, session, order):
        scopes = (order.planning_policy_json or {}).get("scopes", {})
        pinned = {key: scope for key, scope in scopes.items() if scope.get("source_sha256")}
        if not pinned:
            return
        from .media_brand_portfolio_service import MediaBrandPortfolioService
        from .media_operations_service import MediaOperationsConflictError
        try:
            portfolio = await MediaBrandPortfolioService().readback(session, {"id": str(order.created_by), "role": "admin", "actor_type": "human"})
        except MediaOperationsConflictError as exc:
            raise DelegationError("delegation_planning_brand_changed", 403) from exc
        root = await self.root(session, order) if order.planning_root_id else order
        if portfolio["status"] != "ready" or portfolio["headquarters_project_id"] != str(root.project_id):
            raise DelegationError("delegation_planning_brand_changed", 403)
        for key, scope in pinned.items():
            brand = next((row for row in portfolio["brands"] if row["definition"]["brand_key"] == key), None)
            if (brand is None or brand["project_id"] != scope["project_id"] or brand["persona"]["id"] != scope["persona_id"]
                    or brand["persona"]["current_revision"]["id"] != scope["persona_revision_id"]
                    or portfolio["source"]["sha256"] != scope["source_sha256"]):
                raise DelegationError("delegation_planning_brand_changed", 403)

    async def verified_inputs(self, session, order, *, _depth=0, extra_pins=None, _memo=None, _authority_seen=None, _scope_checks=None):
        """Recheck every source influencing instructions, even after planning."""
        if _depth >= 8:
            raise DelegationError("delegation_depth_exceeded", 403)
        _memo = {} if _memo is None else _memo
        _authority_seen = set() if _authority_seen is None else _authority_seen
        _scope_checks = set() if _scope_checks is None else _scope_checks
        pins = {**((order.planning_policy_json or {}).get("input_pins") or {}),
                **{key: value for key, value in (order.planning_input_pins_json or {}).items() if (key != "media_inputs" or isinstance(value, dict)) and (key != "operations_source_ref" or (isinstance(value, dict) and "source_order_id" in value))}, **(extra_pins or {})}
        result = {}
        for key, pin in pins.items():
            if key == "operations_source_ref" and isinstance(pin, dict) and "source_order_id" not in pin:
                continue
            identity = canonical(pin)
            if identity in _memo:
                content, origins = _memo[identity]
                result[key] = {**pin, **({"content": content} if content is not None else {})}
                result.update(origins)
                continue
            source = await session.get(AgentDelegation, delegation_uuid(pin["source_order_id"]))
            if source is None or source.created_by != order.created_by or (source.id == order.id and pin.get("artifact_id")):
                raise DelegationError("delegation_planning_input_changed", 403)
            if str(source.project_id) != pin["project_id"] or (str(source.persona_id) if source.persona_id else None) != pin["persona_id"]:
                raise DelegationError("delegation_planning_input_changed", 403)
            if pin.get("artifact_id"):
                artifact = await session.get(AgentDelegationArtifact, delegation_uuid(pin["artifact_id"]))
                item = await session.get(AgentWorkItem, artifact.work_item_id) if artifact else None
                adopted = await adopted_artifact(session, item, source, artifact.phase) if artifact else None
                if (artifact is None or artifact.delegation_id != source.id or artifact.sha256 != pin["sha256"]
                        or str(artifact.work_item_id) != pin["work_item_id"] or str(artifact.agent_run_id) != pin["agent_run_id"]
                        or adopted is None or adopted.id != artifact.id):
                    raise DelegationError("delegation_planning_input_changed", 403)
                payload = read_artifact_payload(artifact)
                content = payload["output"]
                historical = dict(payload.get("input_pins") or {})
                for child in payload.get("child_returns", []):
                    historical[f"child:{child['delegation_id']}"] = {
                        "source_order_id": child["delegation_id"], "artifact_id": child["id"],
                        "work_item_id": child["work_item_id"], "agent_run_id": child["agent_run_id"], "sha256": child["sha256"],
                        "project_id": child["project_id"], "persona_id": child["persona_id"]}
            else:
                if source.content_hash != pin["content_hash"]:
                    raise DelegationError("delegation_planning_input_changed", 403)
                content = None
                historical = {}
            if source.id == order.id:
                # An aggregate retains its original instruction scope through
                # child returns. It is a hash pin, never a self-artifact edge.
                origins = {}
            else:
                await self.service.validate_authority(session, source, _depth=_depth + 1, _seen=_authority_seen, _scope_checks=_scope_checks)
                origins = await self.verified_inputs(session, source, _depth=_depth + 1, extra_pins=historical,
                    _memo=_memo, _authority_seen=_authority_seen, _scope_checks=_scope_checks)
            result[key] = {**pin, **({"content": content} if content is not None else {})}
            flattened = {("origin:artifact:" + origin["artifact_id"] if origin.get("artifact_id")
                else "origin:instruction:" + origin["source_order_id"]): origin for origin in origins.values()}
            result.update(flattened)
            _memo[identity] = (content, flattened)
            if len(result) > 64:
                raise DelegationError("delegation_plan_input_too_large")
        scopes = {(delegation_uuid(pin["project_id"]), delegation_uuid(pin["persona_id"]) if pin["persona_id"] else None) for pin in result.values()}
        for phase in ("work", "review", "return"):
            aid, rid = phase_identity(order, phase)
            for project_id, persona_id in scopes:
                await self.service.validate_read_authority(session, aid, rid, project_id, persona_id, _checked=_scope_checks)
        return result

    async def root(self, session, order, *, lock=False):
        key = order.planning_root_id or order.id
        statement = select(AgentDelegation).where(AgentDelegation.id == key).execution_options(populate_existing=True)
        return await session.scalar(statement.with_for_update() if lock else statement)

    async def context(self, session, order):
        approved = policy(order)
        if approved is None:
            raise DelegationError("delegation_planning_policy_missing")
        await self.verify_brand_scopes(session, order)
        actor_id, actor_revision = phase_identity(order, "plan")
        for entry in approved.roster:
            for key in entry.scope_keys:
                scope = order.planning_policy_json["scopes"].get(key)
                if scope is None:
                    continue
                await self.service.validate_read_authority(session, entry.agent_id, entry.agent_revision_id,
                    delegation_uuid(scope["project_id"]), delegation_uuid(scope["persona_id"]) if scope["persona_id"] else None)
        inputs = {"assignment": {"kind": "instruction", "source_order_id": str(order.id), "content_hash": order.content_hash,
            "project_id": str(order.project_id), "persona_id": str(order.persona_id) if order.persona_id else None}}
        for key, pin in (await self.verified_inputs(session, order)).items():
            await self.service.validate_read_authority(session, actor_id, actor_revision,
                delegation_uuid(pin["project_id"]), delegation_uuid(pin["persona_id"]) if pin["persona_id"] else None)
            inputs[f"source:{pin['source_order_id']}" if key == "assignment" else key] = pin
        return {"policy": approved.model_dump(mode="json"), "scopes": order.planning_policy_json["scopes"], "inputs": inputs}

    async def validate_tasks(self, session, order, output, *, adopted_artifact=None):
        approved = policy(order)
        if approved is None or len(output.tasks) > approved.max_children or len({task.key for task in output.tasks}) != len(output.tasks):
            raise DelegationError("delegation_plan_fanout_invalid")
        roster = {entry.key: entry for entry in approved.roster}
        scopes = order.planning_policy_json["scopes"]
        inputs = (await self.context(session, order))["inputs"]
        worker_ids, reviewer_ids = set(), set()
        graph = approved.graph_constraints
        if graph:
            pairs = set(graph.allowed_pairs)
            for task in output.tasks:
                if (task.worker_key, task.reviewer_key) not in pairs:
                    raise DelegationError("delegation_plan_graph_assignment_invalid")
            tasks = {task.key: task for task in output.tasks}
            try:
                edges = [(upstream, task.key) for task in output.tasks for upstream in task.depends_on]
                analysis = analyze_handoffs(list(tasks), edges)
                if analysis.cycles:
                    raise ValueError("cycle")
                for task in output.tasks:
                    if (len(set(task.depends_on)) != len(task.depends_on)
                            or not set(task.result_inputs.values()).issubset(task.depends_on)
                            or set(task.result_inputs.values()) != set(task.depends_on)
                            or any(len(alias) > 80 or re.fullmatch(KEY_PATTERN, alias) is None or alias == "assignment"
                                   or alias.startswith(("origin:", "child:", "source:"))
                                   or alias in inputs for alias in task.result_inputs)):
                        raise ValueError("invalid dependency inputs")
                for edge in graph.edges:
                    if edge.kind != "handoff":
                        continue
                    producers = [task for task in output.tasks if task.worker_key == edge.source]
                    consumers = [task for task in output.tasks if task.worker_key == edge.target]
                    for consumer in consumers:
                        if not producers:
                            raise ValueError("missing producer")
                        for producer in producers:
                            if (producer.key not in consumer.depends_on
                                    or consumer.result_inputs.get(edge.output_key if len(producers) == 1 else f"{edge.output_key}:{producer.key}") != producer.key):
                                raise ValueError("missing handoff")
            except ValueError as exc:
                raise DelegationError("delegation_plan_graph_dependency_invalid") from exc
        elif any(task.depends_on or task.result_inputs for task in output.tasks):
            raise DelegationError("delegation_plan_graph_dependency_invalid")
        children = []
        actor_id, actor_revision = phase_identity(order, "plan")
        for task in output.tasks:
            if (task.work_role == "code_worker") != bool(graph and graph.execution_kind == "software_code"):
                raise DelegationError("delegation_plan_assignment_invalid")
            worker, reviewer = roster.get(task.worker_key), roster.get(task.reviewer_key)
            scope = scopes.get(task.scope_key)
            if (worker is None or reviewer is None or scope is None or not reviewer.can_review
                    or task.work_role not in worker.allowed_work_roles
                    or task.scope_key not in worker.scope_keys or task.scope_key not in reviewer.scope_keys
                    or not set(task.input_keys).issubset(inputs)
                    or task.max_output_tokens > min(approved.max_output_tokens_per_call, order.max_output_tokens)
                    or task.max_attempts > min(approved.max_attempts_per_phase, order.max_attempts_per_phase)
                    or len({actor_id, worker.agent_id, reviewer.agent_id}) != 3):
                raise DelegationError("delegation_plan_assignment_invalid")
            worker_ids.add(worker.agent_id)
            reviewer_ids.add(reviewer.agent_id)
            target_project = await session.get(Project, delegation_uuid(scope["project_id"]))
            if target_project is None or target_project.owner_id != order.created_by:
                raise DelegationError("delegation_planning_owner_required", 403)
            for entry in (worker, reviewer):
                # Planner prose can reflect every supplied input. Conservatively
                # retain all source scopes rather than trusting model citations.
                for pin in inputs.values():
                    await self.service.validate_read_authority(session, entry.agent_id, entry.agent_revision_id,
                        delegation_uuid(pin["project_id"]), delegation_uuid(pin["persona_id"]) if pin["persona_id"] else None)
            instruction = task.instruction + "\n\n許可済み入力:\n" + canonical({key: inputs[key] for key in task.input_keys})
            if len(instruction) > 12000:
                raise DelegationError("delegation_plan_input_too_large")
            child = AgentDelegation(id=uuid4(), delegator_agent_id=actor_id, delegator_revision_id=actor_revision,
                worker_agent_id=worker.agent_id, worker_revision_id=worker.agent_revision_id,
                reviewer_agent_id=reviewer.agent_id, reviewer_revision_id=reviewer.agent_revision_id,
                project_id=target_project.id, persona_id=delegation_uuid(scope["persona_id"]) if scope["persona_id"] else None,
                title=task.title, work_role=task.work_role, created_by=order.created_by, round=1, plan_round=1,
                linked_work_binding={}, planning_policy_json={"scopes": {task.scope_key: scope}}, planning_root_id=order.planning_root_id or order.id,
                planning_input_pins_json={key: {k: v for k, v in pin.items() if k != "content"} for key, pin in inputs.items()},
                max_output_tokens=task.max_output_tokens, max_attempts_per_phase=task.max_attempts,
                planning_reserved_output_tokens=0, planning_created_children=0)
            if graph:
                provenance = graph_child_provenance(graph, task.worker_key, task.reviewer_key)
                parent_provenance = (order.planning_policy_json or {}).get("graph_provenance") or {}
                if parent_provenance.get("case_key"):
                    provenance["case_key"] = parent_provenance["case_key"]
                child.planning_policy_json = {**child.planning_policy_json, "graph_provenance": provenance}
            # Parent attachment is added only after the adopted-plan proof.
            await self.service.validate_authority(session, child)
            child.instruction_ciphertext = encrypt_text(instruction, aad=f"delegation:{child.id}:instruction")
            child.content_hash = hashlib.sha256(canonical({"task": task.model_dump(), "scope": scope,
                "inputs": {key: inputs[key] for key in task.input_keys}, "actor": str(actor_id), "actor_revision": str(actor_revision)}).encode()).hexdigest()
            if worker.can_delegate:
                inherited = approved.model_dump(mode="json")
                # The compiled group describes this coordinator, not a child.
                # Nested graph dispatch needs a separately compiled policy.
                if graph:
                    raise DelegationError("delegation_plan_graph_nested_unsupported")
                inherited["brand_keys"] = []
                inherited["input_artifacts"] = {}
                inherited["max_output_tokens_per_call"] = task.max_output_tokens
                inherited["max_attempts_per_phase"] = task.max_attempts
                inherited["roster"] = [entry.model_dump(mode="json") for entry in approved.roster
                    if entry.agent_id not in {actor_id, worker.agent_id} and task.scope_key in entry.scope_keys]
                for entry in inherited["roster"]:
                    entry["scope_keys"] = [task.scope_key]
                if len(inherited["roster"]) < 2:
                    raise DelegationError("delegation_plan_nested_roster_invalid")
                child.planning_policy_json = {"policy": DelegationPlanningPolicy.model_validate(inherited).model_dump(mode="json"),
                    "scopes": {task.scope_key: scope}, "actor": "worker", "input_pins": {}}
            children.append((task.key, child))
        if not graph and (len(worker_ids) != len(output.tasks) or worker_ids & reviewer_ids):
            raise DelegationError("delegation_plan_independent_employees_required")
        depth, cursor = 0, order
        while cursor.parent_delegation_id:
            cursor = await session.get(AgentDelegation, cursor.parent_delegation_id)
            depth += 1
            if cursor is None or depth >= approved.max_depth:
                raise DelegationError("delegation_plan_depth_exceeded")
        if any(policy(child) for _, child in children) and depth + 1 >= approved.max_depth:
            raise DelegationError("delegation_plan_depth_exceeded")
        root = await self.root(session, order)
        root_policy = policy(root) if root else None
        if root is None or root_policy is None:
            raise DelegationError("delegation_plan_children_budget_exhausted")
        if adopted_artifact is not None:
            saved = (await session.execute(select(AgentDelegation).where(
                AgentDelegation.parent_delegation_id == order.id,
                AgentDelegation.parent_round == order.plan_round))).scalars().all()
            by_key = {(child.planning_policy_json or {}).get("graph_task", {}).get("key"): child for child in saved}
            if (graph is None or order.adopted_plan_artifact_id != adopted_artifact.id
                    or order.adopted_plan_sha256 != adopted_artifact.sha256
                    or len(saved) != len(children) or len(by_key) != len(children)):
                raise DelegationError("delegation_plan_adoption_changed")
            for key, expected in children:
                child = by_key.get(key)
                graph_task = (child.planning_policy_json or {}).get("graph_task", {}) if child else {}
                if (child is None or child.parent_round != order.plan_round
                        or child.parent_plan_artifact_id != adopted_artifact.id
                        or child.parent_plan_sha256 != adopted_artifact.sha256
                        or child.content_hash != expected.content_hash
                        or child.idempotency_key != f"delegation-plan:{order.id}:{order.plan_round}:{adopted_artifact.sha256}:{key}"
                        or any(getattr(child, field) != getattr(expected, field) for field in (
                            "delegator_agent_id", "delegator_revision_id", "worker_agent_id", "worker_revision_id",
                            "reviewer_agent_id", "reviewer_revision_id", "project_id", "persona_id", "created_by",
                            "max_output_tokens", "max_attempts_per_phase", "planning_input_pins_json"))
                        or graph_task.get("graph_revision_id") != str(graph.graph_revision_id)
                        or graph_task.get("graph_hash") != graph.graph_hash):
                    raise DelegationError("delegation_plan_adoption_changed")
        elif root.planning_created_children + len(children) > root_policy.max_total_children:
            raise DelegationError("delegation_plan_children_budget_exhausted")
        return children

    async def materialize(self, session, order):
        # Preserve active guards deliberately changed in this transaction. A
        # stale but unchanged ORM value must not override the fresh DB read.
        if order in session.dirty:
            state = inspect(order)
            if state.attrs.planning_policy_json.history.has_changes() and not policy(order):
                return []
            if any(state.attrs[name].history.has_changes() and getattr(order, name)
                   for name in ("cancelled_at", "planning_error_code")):
                return []
        # Read scalar columns rather than a possibly stale identity-map object.
        # Do not autoflush discovery's last_discovered_at before the no-op:
        # that update would itself take the root row lock.
        with session.no_autoflush:
            fresh = (await session.execute(select(
                AgentDelegation.cancelled_at, AgentDelegation.planning_error_code,
                AgentDelegation.planning_policy_json, AgentDelegation.plan_round,
                AgentDelegation.adopted_plan_artifact_id, AgentDelegation.adopted_plan_sha256,
                AgentDelegation.delegator_agent_id, AgentDelegation.delegator_revision_id,
                AgentDelegation.worker_agent_id, AgentDelegation.worker_revision_id,
                AgentDelegation.reviewer_agent_id, AgentDelegation.reviewer_revision_id,
                AgentDelegationArtifact.id, AgentDelegationArtifact.delegation_id,
                AgentDelegationArtifact.work_item_id, AgentDelegationArtifact.phase,
                AgentDelegationArtifact.sha256,
                AgentWorkItem.id, AgentWorkItem.state, AgentWorkItem.source_type,
                AgentWorkItem.source_id, AgentWorkItem.source_revision, AgentWorkItem.intent_key,
                AgentWorkItem.result_summary_json,
                AgentRun.id, AgentRun.status, AgentRun.work_item_id, AgentRun.agent_id, AgentRun.agent_revision_id,
            ).outerjoin(AgentDelegationArtifact, AgentDelegationArtifact.id == AgentDelegation.adopted_plan_artifact_id)
             .outerjoin(AgentWorkItem, AgentWorkItem.id == AgentDelegationArtifact.work_item_id)
             .outerjoin(AgentRun, AgentRun.id == AgentDelegationArtifact.agent_run_id)
             .where(AgentDelegation.id == order.id))).first()
        if fresh is None:
            return []
        (cancelled, error, stored, plan_round, adopted_id, adopted_sha,
         delegator, delegator_revision, worker, worker_revision, reviewer, reviewer_revision,
         artifact_id, artifact_order, artifact_work, artifact_phase, artifact_sha,
         work_id, work_state, work_source_type, work_source_id, work_revision, work_phase,
         work_result, run_id, run_status, run_work, run_agent, run_revision) = fresh
        if cancelled or error:
            return []
        if stored is None or (isinstance(stored, dict) and not stored.get("policy")):
            return []
        try:
            approved = DelegationPlanningPolicy.model_validate(stored["policy"])
        except (TypeError, KeyError, ValueError):
            approved = None
        actor = (stored.get("actor") or "delegator") if isinstance(stored, dict) else None
        # The current Plan Work is identified from the fresh plan round, not
        # from the already-adopted Artifact (there may be no adoption yet).
        with session.no_autoflush:
            current_work = (await session.execute(select(
                AgentWorkItem.id, AgentWorkItem.state, AgentWorkItem.result_summary_json,
            ).where(AgentWorkItem.source_type == "employee_delegation",
                    AgentWorkItem.source_id == str(order.id),
                    AgentWorkItem.source_revision == f"plan:{plan_round}",
                    AgentWorkItem.intent_key == "plan"))).first()
        current_work_id = current_work[0] if current_work else None
        actors = {"delegator": (delegator, delegator_revision), "worker": (worker, worker_revision),
                  "reviewer": (reviewer, reviewer_revision)}
        expected = actors.get(actor) if isinstance(actor, str) else None
        # A pending in-session change to the proof is unknown until flushed.
        # Discovery's timestamp alone is intentionally excluded from this test.
        try:
            pending = session.new | session.dirty | session.deleted
            order_fields = ("planning_policy_json", "plan_round", "adopted_plan_artifact_id",
                "adopted_plan_sha256", "cancelled_at", "planning_error_code", "delegator_agent_id",
                "delegator_revision_id", "worker_agent_id", "worker_revision_id", "reviewer_agent_id",
                "reviewer_revision_id")
            pending_proof = any(
                (isinstance(obj, AgentDelegation) and obj.id == order.id
                 and (obj in session.new or obj in session.deleted or any(
                     inspect(obj).attrs[name].history.has_changes() for name in order_fields)))
                or (isinstance(obj, AgentDelegationArtifact) and (adopted_id is None or obj.id == artifact_id))
                or (isinstance(obj, AgentWorkItem) and (obj.id in (work_id, current_work_id)
                    or obj.source_id == str(order.id)
                    or str(order.id) in inspect(obj).attrs.source_id.history.deleted))
                or (isinstance(obj, AgentRun) and (adopted_id is None or obj.id == run_id))
                for obj in pending)
        except Exception:
            pending_proof = True
        if (not pending_proof and approved is not None and expected is not None
                and adopted_id is None and adopted_sha is None):
            # Match phase_items/adopted_artifact from fresh scalar values.
            # Any malformed or inconsistent evidence falls through to locks.
            not_ready = current_work is None or current_work.state != "succeeded"
            if current_work is not None and current_work.state == "succeeded":
                result = current_work.result_summary_json
                if result is None:
                    result = {}
                if isinstance(result, dict):
                    raw_id = result.get("artifact_id")
                    if raw_id is None:
                        not_ready = True
                    else:
                        try:
                            candidate_id = delegation_uuid(raw_id)
                        except DelegationError:
                            candidate_id = None
                        if candidate_id is not None:
                            with session.no_autoflush:
                                candidate = (await session.execute(select(
                                    AgentDelegationArtifact.delegation_id, AgentDelegationArtifact.work_item_id,
                                    AgentDelegationArtifact.phase, AgentDelegationArtifact.sha256,
                                    AgentRun.status, AgentRun.work_item_id, AgentRun.agent_id, AgentRun.agent_revision_id,
                                ).outerjoin(AgentRun, AgentRun.id == AgentDelegationArtifact.agent_run_id)
                                 .where(AgentDelegationArtifact.id == candidate_id))).first()
                            if candidate is None:
                                not_ready = True
                            elif (candidate[0] == order.id and candidate[1] == current_work_id
                                  and candidate[2] == "plan" and candidate[3] == result.get("sha256")):
                                # A missing or unfinished *matching* Run cannot yet
                                # be adopted; inconsistent Run proof takes locks.
                                if candidate[4] is None:
                                    not_ready = True
                                elif candidate[5] == current_work_id and (candidate[6], candidate[7]) == expected:
                                    not_ready = candidate[4] != "succeeded"
            if not_ready:
                return []
        if (not pending_proof and approved is not None and adopted_id is not None and isinstance(adopted_sha, str) and adopted_sha
                and artifact_id == adopted_id and artifact_order == order.id
                and artifact_phase == "plan" and artifact_sha == adopted_sha
                and artifact_work == work_id and work_state == "succeeded"
                and work_source_type == "employee_delegation" and work_source_id == str(order.id)
                and work_revision == f"plan:{plan_round}" and work_phase == "plan"
                and isinstance(work_result, dict) and work_result.get("artifact_id") == str(adopted_id)
                and work_result.get("sha256") == adopted_sha
                and run_status == "succeeded" and run_work == work_id
                and expected is not None and (run_agent, run_revision) == expected):
            return []
        # A consistent root -> order lock order serializes subtree adoption.
        root = await self.root(session, order, lock=True)
        order = await session.scalar(select(AgentDelegation).where(AgentDelegation.id == order.id)
            .execution_options(populate_existing=True).with_for_update())
        if not policy(order) or order.cancelled_at or order.planning_error_code:
            return []
        items = await phase_items(session, order)
        plan_item = items["plan"]
        # phase_items/session.get may return a stale identity-map object. Keep
        # pending edits intact, otherwise refresh the locked-path evidence.
        if plan_item is not None and plan_item not in session.dirty and plan_item not in session.new and plan_item not in session.deleted:
            await session.refresh(plan_item)
        if plan_item is not None and isinstance(plan_item.result_summary_json, dict):
            try:
                plan_artifact_id = delegation_uuid(plan_item.result_summary_json.get("artifact_id"))
            except DelegationError:
                plan_artifact_id = None
            if plan_artifact_id is not None:
                plan_artifact = await session.get(AgentDelegationArtifact, plan_artifact_id)
                if plan_artifact is not None and plan_artifact not in session.dirty and plan_artifact not in session.new and plan_artifact not in session.deleted:
                    await session.refresh(plan_artifact)
                if plan_artifact is not None:
                    plan_run = await session.get(AgentRun, plan_artifact.agent_run_id)
                    if plan_run is not None and plan_run not in session.dirty and plan_run not in session.new and plan_run not in session.deleted:
                        await session.refresh(plan_run)
        artifact = await adopted_artifact(session, plan_item, order, "plan")
        if artifact is None:
            return []
        if order.adopted_plan_artifact_id:
            if order.adopted_plan_artifact_id != artifact.id or order.adopted_plan_sha256 != artifact.sha256:
                order.planning_error_code = "delegation_plan_adoption_changed"
            return []
        try:
            await self.service.validate_authority(session, order)
            output = DelegationPlanOutput.model_validate(read_artifact_payload(artifact)["output"])
            children = await self.validate_tasks(session, order, output)
            root_policy = policy(root)
            if root.planning_created_children + len(children) > root_policy.max_total_children:
                raise DelegationError("delegation_plan_children_budget_exhausted")
            child_by_key = dict(children)
            task_by_key = {task.key: task for task in output.tasks}
            for key, child in children:
                task = task_by_key[key]
                if policy(order).graph_constraints:
                    child.planning_policy_json = {**child.planning_policy_json,
                        "graph_task": {"key": key, "graph_revision_id": str(policy(order).graph_constraints.graph_revision_id),
                                       "graph_hash": policy(order).graph_constraints.graph_hash},
                        "dependencies": {alias: {"task_key": upstream, "order_id": str(child_by_key[upstream].id),
                                                   "plan_sha256": artifact.sha256, "content_hash": child_by_key[upstream].content_hash,
                                                   "project_id": str(child_by_key[upstream].project_id),
                                                   "persona_id": str(child_by_key[upstream].persona_id) if child_by_key[upstream].persona_id else None}
                                         for alias, upstream in task.result_inputs.items()}}
                child.parent_delegation_id, child.parent_round = order.id, order.plan_round
                child.parent_plan_artifact_id, child.parent_plan_sha256 = artifact.id, artifact.sha256
                child.idempotency_key = f"delegation-plan:{order.id}:{order.plan_round}:{artifact.sha256}:{key}"
            if policy(order).graph_constraints and policy(order).graph_constraints.execution_kind == "software_code":
                # A real write starts SQLite's outer transaction before SAVEPOINT;
                # otherwise releasing the first savepoint can commit on its own.
                await session.execute(update(AgentDelegation).where(AgentDelegation.id == root.id).values(
                    planning_created_children=AgentDelegation.planning_created_children))
                async with session.begin_nested():
                    from .software_graph_materialization import prepare_native_children
                    await prepare_native_children(session, self, order, output, children)
                    for _, child in children:
                        session.add(child)
                    await session.flush()
            else:
                for _, child in children:
                    session.add(child)
            root.planning_created_children += len(children)
            order.adopted_plan_artifact_id, order.adopted_plan_sha256 = artifact.id, artifact.sha256
            order.planning_error_code = None
            await session.flush()
            return [child for _, child in children]
        except DelegationError as exc:
            order.planning_error_code = exc.code
            return []
        except (ValueError, TypeError, KeyError):
            order.planning_error_code = "delegation_plan_invalid"
            return []

    async def reserve_attempt(self, session, order, claim, run_id):
        if not order.planning_root_id and not policy(order):
            return
        root = await _reservation_await("root_lock", self.root(session, order, lock=True))
        approved = policy(root) if root else None
        if approved is None or root.cancelled_at:
            raise DelegationError("delegation_planning_root_unavailable", 403)
        run_uuid = delegation_uuid(run_id)
        if await _reservation_await("reservation_lookup", session.get(AgentDelegationBudgetReservation, run_uuid)):
            raise DelegationError("delegation_attempt_already_reserved", 409)
        item = await _reservation_await("work_lock", session.scalar(select(AgentWorkItem).where(AgentWorkItem.id == delegation_uuid(claim.work_item_id))
            .execution_options(populate_existing=True).with_for_update()))
        if (item is None or item.state != "running" or item.lease_token != claim.lease_token or item.lease_owner != claim.lease_owner
                or item.active_agent_run_id != run_uuid or item.lease_expires_at is None or item.lease_expires_at <= datetime.utcnow()):
            raise DelegationError("delegation_lease_lost", 409)
        # Use the canonical database value for both the ceiling and increment.
        # FOR UPDATE serializes PostgreSQL, but SQLite ignores it; writing an
        # ORM snapshot here would lose concurrent sibling reservations.
        reserved = await _reservation_await("budget_update", session.execute(update(AgentDelegation).where(
            AgentDelegation.id == root.id,
            AgentDelegation.cancelled_at.is_(None),
            AgentDelegation.planning_reserved_output_tokens + order.max_output_tokens
                <= approved.max_total_output_tokens,
        ).values(planning_reserved_output_tokens=
            AgentDelegation.planning_reserved_output_tokens + order.max_output_tokens
        ).execution_options(synchronize_session=False)))
        if reserved.rowcount != 1:
            raise DelegationError("delegation_plan_output_budget_exhausted")
        session.add(AgentDelegationBudgetReservation(agent_run_id=run_uuid, planning_root_id=root.id,
            delegation_id=order.id, work_item_id=item.id, output_tokens=order.max_output_tokens))
        await _reservation_await("commit", session.commit())
