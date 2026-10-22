"""Bounded manager plans adopted by the server, never by model authority."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import or_, select, update

from ..memory.models import AgentDelegation, AgentDelegationArtifact, AgentDelegationBudgetReservation, AgentRevision, AgentWorkItem, Project
from ..security.field_crypto import decrypt_text, encrypt_text
from .agent_delegation_service import DelegationError, adopted_artifact, canonical, delegation_uuid, phase_identity, phase_items

KEY_PATTERN = r"^[a-z0-9][a-z0-9_.:-]*$"
PlanRole = Literal["researcher", "writer", "data_worker", "general_worker"]


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
    max_output_tokens: int = Field(default=512, ge=256, le=8192)
    max_attempts: int = Field(default=1, ge=1, le=3)


class DelegationPlanOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    summary: str = Field(min_length=1, max_length=2000)
    body: str = Field(min_length=1, max_length=20000)
    accepted: None = None
    tasks: list[PlannedAssignment] = Field(min_length=1, max_length=8)


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
                **(order.planning_input_pins_json or {}), **(extra_pins or {})}
        result = {}
        for key, pin in pins.items():
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

    async def validate_tasks(self, session, order, output):
        approved = policy(order)
        if approved is None or len(output.tasks) > approved.max_children or len({task.key for task in output.tasks}) != len(output.tasks):
            raise DelegationError("delegation_plan_fanout_invalid")
        roster = {entry.key: entry for entry in approved.roster}
        scopes = order.planning_policy_json["scopes"]
        inputs = (await self.context(session, order))["inputs"]
        worker_ids, reviewer_ids = set(), set()
        children = []
        actor_id, actor_revision = phase_identity(order, "plan")
        for task in output.tasks:
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
            # Parent attachment is added only after the adopted-plan proof.
            await self.service.validate_authority(session, child)
            child.instruction_ciphertext = encrypt_text(instruction, aad=f"delegation:{child.id}:instruction")
            child.content_hash = hashlib.sha256(canonical({"task": task.model_dump(), "scope": scope,
                "inputs": {key: inputs[key] for key in task.input_keys}, "actor": str(actor_id), "actor_revision": str(actor_revision)}).encode()).hexdigest()
            if worker.can_delegate:
                inherited = approved.model_dump(mode="json")
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
        if len(worker_ids) != len(output.tasks) or worker_ids & reviewer_ids:
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
        if root is None or root_policy is None or root.planning_created_children + len(children) > root_policy.max_total_children:
            raise DelegationError("delegation_plan_children_budget_exhausted")
        return children

    async def materialize(self, session, order):
        if not policy(order) or order.cancelled_at or order.planning_error_code:
            return []
        # A consistent root -> order lock order serializes subtree adoption.
        root = await self.root(session, order, lock=True)
        order = await session.scalar(select(AgentDelegation).where(AgentDelegation.id == order.id)
            .execution_options(populate_existing=True).with_for_update())
        if order.cancelled_at or order.planning_error_code:
            return []
        items = await phase_items(session, order)
        artifact = await adopted_artifact(session, items["plan"], order, "plan")
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
            for key, child in children:
                child.parent_delegation_id, child.parent_round = order.id, order.plan_round
                child.parent_plan_artifact_id, child.parent_plan_sha256 = artifact.id, artifact.sha256
                child.idempotency_key = f"delegation-plan:{order.id}:{order.plan_round}:{artifact.sha256}:{key}"
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
        root = await self.root(session, order, lock=True)
        approved = policy(root) if root else None
        if approved is None or root.cancelled_at:
            raise DelegationError("delegation_planning_root_unavailable", 403)
        run_uuid = delegation_uuid(run_id)
        if await session.get(AgentDelegationBudgetReservation, run_uuid):
            raise DelegationError("delegation_attempt_already_reserved", 409)
        item = await session.scalar(select(AgentWorkItem).where(AgentWorkItem.id == delegation_uuid(claim.work_item_id))
            .execution_options(populate_existing=True).with_for_update())
        if (item is None or item.state != "running" or item.lease_token != claim.lease_token or item.lease_owner != claim.lease_owner
                or item.active_agent_run_id != run_uuid or item.lease_expires_at is None or item.lease_expires_at <= datetime.utcnow()):
            raise DelegationError("delegation_lease_lost", 409)
        # Use the canonical database value for both the ceiling and increment.
        # FOR UPDATE serializes PostgreSQL, but SQLite ignores it; writing an
        # ORM snapshot here would lose concurrent sibling reservations.
        reserved = await session.execute(update(AgentDelegation).where(
            AgentDelegation.id == root.id,
            AgentDelegation.cancelled_at.is_(None),
            AgentDelegation.planning_reserved_output_tokens + order.max_output_tokens
                <= approved.max_total_output_tokens,
        ).values(planning_reserved_output_tokens=
            AgentDelegation.planning_reserved_output_tokens + order.max_output_tokens
        ).execution_options(synchronize_session=False))
        if reserved.rowcount != 1:
            raise DelegationError("delegation_plan_output_budget_exhausted")
        session.add(AgentDelegationBudgetReservation(agent_run_id=run_uuid, planning_root_id=root.id,
            delegation_id=order.id, work_item_id=item.id, output_tokens=order.max_output_tokens))
        await session.commit()
