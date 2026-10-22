"""Orders between employees with immutable scope, revision and evidence pins."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from ..features import Features
from ..memory.models import AgentDelegation, AgentDelegationArtifact, AgentOrganizationProfile, AgentProjectGrant, AgentRun, AgentSpaceAssignment, AgentWorkEvent, AgentWorkItem, Organization, Persona, PersonaOperatorAssignment, Project
from ..security.field_crypto import decrypt_text, encrypt_text
from .agent_authority import AgentAuthorityResolver
from .agent_identity_service import AgentIdentityService
from .agent_organization_service import lock_reporting_graph

PHASES = ("work", "review", "return")
WORK_ROLES = ("researcher", "writer", "data_worker", "general_worker", "code_worker")
# Every phase consumes scoped instructions/evidence. Requesting an existing
# read capability activates all central permission and capability ceilings.
DELEGATION_REQUIRED_CAPABILITIES = ("workspace_read",)


class DelegationError(Exception):
    def __init__(self, code, status_code=422):
        super().__init__(code)
        self.code, self.status_code = code, status_code


def delegation_uuid(value):
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise DelegationError("delegation_invalid_id") from None


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def source_revision(order, phase):
    return f"plan:{order.plan_round}" if phase == "plan" else str(order.round)


def phase_sequence(order):
    return ("plan", *PHASES) if (order.planning_policy_json or {}).get("policy") else PHASES


async def phase_items(session, order):
    items = {}
    for phase in phase_sequence(order):
        if phase == "work" and order.linked_work_item_id:
            items[phase] = await session.get(AgentWorkItem, order.linked_work_item_id, populate_existing=True)
        else:
            items[phase] = await session.scalar(select(AgentWorkItem).where(
                AgentWorkItem.source_type == "employee_delegation", AgentWorkItem.source_id == str(order.id),
                AgentWorkItem.source_revision == source_revision(order, phase), AgentWorkItem.intent_key == phase))
    return items


async def adopted_artifact(session, item, order, phase):
    if item is None or item.state != "succeeded":
        return None
    result = item.result_summary_json or {}
    try:
        artifact = await session.get(AgentDelegationArtifact, delegation_uuid(result.get("artifact_id")))
    except DelegationError:
        return None
    if (artifact is None or artifact.delegation_id != order.id or artifact.work_item_id != item.id
            or artifact.phase != phase or artifact.sha256 != result.get("sha256")):
        return None
    run = await session.get(AgentRun, artifact.agent_run_id)
    expected_agent, expected_revision = phase_identity(order, phase)
    if run is None or run.status != "succeeded" or run.work_item_id != item.id or run.agent_id != expected_agent or run.agent_revision_id != expected_revision:
        return None
    return artifact


def phase_identity(order, phase):
    prefix = ((order.planning_policy_json or {}).get("actor") or "delegator") if phase == "plan" else {"work": "worker", "review": "reviewer", "return": "delegator"}[phase]
    return getattr(order, prefix + "_agent_id"), getattr(order, prefix + "_revision_id")


async def delegation_state(session, order, *, _depth=0):
    items = await phase_items(session, order)
    if order.cancelled_at:
        return "cancelled", items
    if order.planning_error_code:
        return "planning_blocked", items
    from .agent_delegation_planning import active_children
    children = await active_children(session, order)
    if children:
        if _depth >= 8:
            return "child_blocked", items
        states = [(await delegation_state(session, child, _depth=_depth + 1))[0] for child in children]
        if any(state != "returned" for state in states):
            blocked = any(state in {"cancelled", "changes_requested", "evidence_missing", "child_blocked"}
                or state.endswith((":blocked", ":failed", ":dead_letter", ":uncertain")) for state in states)
            return "child_blocked" if blocked else "awaiting_children", items
    for phase in phase_sequence(order):
        item = items[phase]
        if item is None:
            return {"plan": "awaiting_plan", "work": "queued", "review": "awaiting_review", "return": "awaiting_return"}[phase], items
        if item.state != "succeeded":
            return (f"{phase}:{item.state}"), items
        if phase == "work" and order.linked_work_item_id:
            continue
        artifact = await adopted_artifact(session, item, order, phase)
        if artifact is None:
            return "evidence_missing", items
        if phase == "review" and artifact.accepted != 1:
            return "changes_requested", items
    return "returned", items


def native_binding(item):
    return {key: str(getattr(item, key)) if getattr(item, key) is not None else None for key in (
        "source_type", "source_id", "source_revision", "execution_adapter", "assigned_agent_id", "agent_revision_id", "project_id", "persona_id")}


class AgentDelegationService:
    def __init__(self, db_manager, *, config=None):
        self.identity = AgentIdentityService(db_manager, config=config)
        self.authority = AgentAuthorityResolver(db_manager, config=config)

    @staticmethod
    def require_enabled():
        if not (Features.virtual_company() and Features.autonomous_agent_runtime()):
            raise DelegationError("employee_runtime_disabled", 404)

    async def can_manage_project_delegation(self, session, order, organization):
        """Existing scoped management ACL allows functional shared workers.

        Reporting metadata never creates a grant. Every ordinary scope and
        employment check still applies, and setup never issues this ACL.
        """
        from .project_permissions import normalize_project_member_permissions
        now = datetime.utcnow()
        grant = await session.scalar(select(AgentProjectGrant).where(
            AgentProjectGrant.agent_id == order.delegator_agent_id,
            AgentProjectGrant.project_id == order.project_id, AgentProjectGrant.state == "active"))
        if (grant is None or (grant.active_from and grant.active_from > now)
                or (grant.active_until and grant.active_until < now)
                or normalize_project_member_permissions(grant.permissions).get("manage_members") is not True):
            return False
        manager = await session.get(AgentOrganizationProfile, order.delegator_agent_id, populate_existing=True)
        project = await session.get(Project, order.project_id, populate_existing=True)
        policies = [organization.policy_json, manager.company_permission_ceiling_json if manager else None]
        if project and project.space_id:
            assignments = (await session.scalars(select(AgentSpaceAssignment).where(
                AgentSpaceAssignment.agent_id == order.delegator_agent_id,
                AgentSpaceAssignment.space_id == project.space_id, AgentSpaceAssignment.state == "active"))).all()
            policies.extend(assignment.policy_ceiling_json for assignment in assignments
                if not (assignment.active_from and assignment.active_from > now)
                and not (assignment.active_until and assignment.active_until < now))
        for policy in policies:
            if policy is None:
                continue
            if not isinstance(policy, dict):
                return False
            for key in ("permissions", "project_permissions", "space_permissions", "persona_permissions"):
                if key not in policy:
                    continue
                ceiling = policy[key]
                if isinstance(ceiling, dict):
                    if ceiling.get("manage_members") is not True:
                        return False
                elif isinstance(ceiling, (list, tuple, set, frozenset)):
                    if "manage_members" not in ceiling:
                        return False
                else:
                    return False
        return True

    async def validate_authority(self, session, order, *, _depth=0, _seen=None, _scope_checks=None):
        self.require_enabled()
        _seen = set() if _seen is None else _seen
        _scope_checks = set() if _scope_checks is None else _scope_checks
        if order.id in _seen:
            return
        _seen.add(order.id)
        if _depth >= 8:
            raise DelegationError("delegation_depth_exceeded", 403)
        await self.identity._assert_admin(session, order.created_by)
        organization = await session.scalar(select(Organization).where(Organization.singleton_key == "installation"))
        if organization is None or (organization.policy_json or {}).get("allow_agent_runtime") is not True:
            raise DelegationError("delegation_runtime_policy_denied", 403)
        scoped_manager = None
        if (order.planning_policy_json or {}).get("policy") or order.planning_root_id:
            project = await session.get(Project, order.project_id, populate_existing=True)
            if project is None or project.owner_id != order.created_by:
                raise DelegationError("delegation_planning_owner_required", 403)
            from .agent_delegation_planning import DelegationPlanner
            planner = DelegationPlanner(self)
            await planner.verify_brand_scopes(session, order)
            await planner.verified_inputs(session, order, _depth=_depth, _authority_seen=_seen, _scope_checks=_scope_checks)
        for phase in PHASES:
            agent_id, revision_id = phase_identity(order, phase)
            profile = await session.get(AgentOrganizationProfile, agent_id, populate_existing=True)
            if profile is None:
                raise DelegationError("delegation_employment_profile_missing", 403)
            if phase != "return" and profile.manager_agent_id != order.delegator_agent_id:
                if scoped_manager is None:
                    scoped_manager = await self.can_manage_project_delegation(session, order, organization)
                if not scoped_manager:
                    raise DelegationError("delegation_reporting_relation_revoked", 403)
            await self.validate_read_authority(session, agent_id, revision_id, order.project_id, order.persona_id, _checked=_scope_checks)
        if order.parent_delegation_id:
            parent = await session.get(AgentDelegation, order.parent_delegation_id, populate_existing=True)
            expected = phase_identity(parent, "plan")[0] if parent and order.parent_plan_artifact_id else parent.worker_agent_id if parent else None
            if parent is None or parent.cancelled_at or parent.planning_error_code or expected != order.delegator_agent_id:
                raise DelegationError("delegation_parent_unavailable", 403)
            if order.parent_plan_artifact_id:
                parent_items = await phase_items(session, parent)
                plan = await adopted_artifact(session, parent_items.get("plan"), parent, "plan")
                if (plan is None or plan.id != order.parent_plan_artifact_id or plan.sha256 != order.parent_plan_sha256
                        or parent.adopted_plan_artifact_id != plan.id or order.parent_round != parent.plan_round):
                    raise DelegationError("delegation_parent_plan_changed", 403)
            if parent.completed_at and (await delegation_state(session, order))[0] != "returned":
                raise DelegationError("delegation_parent_terminal", 403)
            await self.validate_authority(session, parent, _depth=_depth + 1, _seen=_seen, _scope_checks=_scope_checks)
        if order.linked_work_item_id:
            linked = await session.get(AgentWorkItem, order.linked_work_item_id, populate_existing=True)
            if linked is None or native_binding(linked) != order.linked_work_binding:
                raise DelegationError("delegation_native_work_binding_changed", 403)
            from ..memory.models.operations import EngagementOpportunity
            from .marketplace.client_development import development_source_revision
            opportunity = await session.get(EngagementOpportunity, delegation_uuid(linked.source_id), populate_existing=True)
            kind = linked.execution_adapter.removeprefix("crowdworks_development_")
            if (opportunity is None or opportunity.project_id != order.project_id
                    or opportunity.status not in {"contract_accepted", "ready_for_review"}
                    or (opportunity.metadata_json or {}).get("worker_kind") != kind
                    or development_source_revision(opportunity, kind, agent_revision_id=str(order.worker_revision_id)) != linked.source_revision):
                raise DelegationError("delegation_native_requirements_changed", 403)

    async def validate_read_authority(self, session, agent_id, revision_id, project_id, persona_id, *, _checked=None):
        scope_key = (agent_id, revision_id, project_id, persona_id)
        if _checked is not None and scope_key in _checked:
            return
        profile = await session.get(AgentOrganizationProfile, agent_id, populate_existing=True)
        if profile is None:
            raise DelegationError("delegation_employment_profile_missing", 403)
        decision = await self.authority.resolve(agent_id=agent_id, revision_id=revision_id,
            project_id=project_id, persona_id=persona_id, required_capability=DELEGATION_REQUIRED_CAPABILITIES[0])
        if not decision.allowed:
            raise DelegationError("delegation_authority_denied", 403)
        if persona_id:
            from .agent_team_v3 import AGENT_TEAM_CAPABILITY_CATALOG
            now = datetime.utcnow()
            assignments = (await session.scalars(select(PersonaOperatorAssignment).where(
                PersonaOperatorAssignment.agent_id == agent_id, PersonaOperatorAssignment.persona_id == persona_id,
                PersonaOperatorAssignment.state == "active"))).all()
            for assignment in assignments:
                if (assignment.active_from and assignment.active_from > now) or (assignment.active_until and assignment.active_until < now):
                    continue
                ceiling = assignment.capability_ceiling_json
                if (not isinstance(ceiling, list) or any(not isinstance(cap, str) or cap not in AGENT_TEAM_CAPABILITY_CATALOG for cap in ceiling)
                        or not set(DELEGATION_REQUIRED_CAPABILITIES).issubset(ceiling)):
                    raise DelegationError("delegation_persona_read_ceiling_denied", 403)
        if _checked is not None:
            _checked.add(scope_key)

    async def create(self, *, actor_user_id, **payload):
        self.require_enabled()
        session = await self.identity._session()
        try:
            actor = await self.identity._assert_admin(session, actor_user_id)
            await lock_reporting_graph(session)
            work_role = payload.get("work_role")
            if work_role not in WORK_ROLES:
                raise DelegationError("delegation_invalid_role")
            instruction = str(payload.get("instruction") or "").strip()
            title = str(payload.get("title") or "").strip()
            key = str(payload.get("idempotency_key") or "").strip()
            if not instruction or len(instruction) > 12000 or not title or len(title) > 160 or not key or len(key) > 255:
                raise DelegationError("delegation_invalid_input")
            # Credentials, personal claims and provider targets are never slots.
            identifiers = {name: delegation_uuid(payload[name]) for name in (
                "delegator_agent_id", "delegator_revision_id", "worker_agent_id", "worker_revision_id",
                "reviewer_agent_id", "reviewer_revision_id", "project_id")}
            for name in ("persona_id", "parent_delegation_id", "linked_work_item_id"):
                identifiers[name] = delegation_uuid(payload[name]) if payload.get(name) else None
            if len({identifiers[key] for key in ("delegator_agent_id", "worker_agent_id", "reviewer_agent_id")}) != 3:
                raise DelegationError("delegation_independent_employees_required")
            project = await session.get(Project, identifiers["project_id"])
            if project is None or project.deleted_at:
                raise DelegationError("delegation_project_missing", 404)
            if identifiers["persona_id"]:
                persona = await session.get(Persona, identifiers["persona_id"])
                if persona is None or persona.project_id != project.id:
                    raise DelegationError("delegation_persona_project_mismatch")
            if identifiers["parent_delegation_id"]:
                parent = await session.get(AgentDelegation, identifiers["parent_delegation_id"])
                if parent and ((parent.planning_policy_json or {}).get("policy") or parent.planning_root_id):
                    raise DelegationError("delegation_planned_parent_requires_adopted_plan", 409)
                if (parent is None or parent.project_id != project.id or parent.persona_id != identifiers["persona_id"]
                        or parent.worker_agent_id != identifiers["delegator_agent_id"]):
                    raise DelegationError("delegation_parent_scope_mismatch")
                parent_items = await phase_items(session, parent)
                if parent.cancelled_at or parent_items["review"] is not None:
                    raise DelegationError("delegation_parent_already_in_review", 409)
                cursor, depth = parent, 1
                while cursor.parent_delegation_id:
                    depth += 1
                    if depth >= 8:
                        raise DelegationError("delegation_depth_exceeded")
                    cursor = await session.get(AgentDelegation, cursor.parent_delegation_id)
                    if cursor is None:
                        raise DelegationError("delegation_parent_unavailable")
            if (work_role == "code_worker") != bool(identifiers["linked_work_item_id"]):
                raise DelegationError("delegation_code_requires_native_work")
            linked_binding = {}
            if identifiers["linked_work_item_id"]:
                linked = await session.get(AgentWorkItem, identifiers["linked_work_item_id"])
                if (linked is None or linked.source_type != "marketplace_crowdworks_fulfillment"
                        or linked.execution_adapter not in {"crowdworks_development_web_development", "crowdworks_development_app_development"}
                        or linked.assigned_agent_id != identifiers["worker_agent_id"] or linked.agent_revision_id != identifiers["worker_revision_id"]
                        or linked.project_id != project.id or linked.persona_id != identifiers["persona_id"]):
                    raise DelegationError("delegation_native_work_binding_invalid")
                linked_binding = native_binding(linked)
            digest_input = {**{name: str(value) if value else None for name, value in identifiers.items()},
                "title": title, "work_role": work_role, "instruction": instruction, "created_by": str(actor), "linked_work_binding": linked_binding}
            if payload.get("planning") is not None:
                from .agent_delegation_planning import DelegationPlanningPolicy
                digest_input["planning"] = DelegationPlanningPolicy.model_validate(payload["planning"]).model_dump(mode="json")
            digest = hashlib.sha256(canonical(digest_input).encode()).hexdigest()
            existing = await session.scalar(select(AgentDelegation).where(AgentDelegation.idempotency_key == key))
            if existing:
                if existing.content_hash != digest:
                    raise DelegationError("delegation_idempotency_conflict", 409)
                await self.validate_authority(session, existing)
                return await self.project(session, existing)
            order = AgentDelegation(id=uuid4(), **identifiers, title=title, work_role=work_role, content_hash=digest,
                idempotency_key=key, created_by=actor, round=1, plan_round=1, linked_work_binding=linked_binding,
                planning_policy_json={}, max_output_tokens=8192, max_attempts_per_phase=3,
                planning_reserved_output_tokens=0, planning_created_children=0)
            if payload.get("planning") is not None:
                from .agent_delegation_planning import DelegationPlanner
                await DelegationPlanner(self).prepare(session, order, payload["planning"])
            await self.validate_authority(session, order)
            order.instruction_ciphertext = encrypt_text(instruction, aad=f"delegation:{order.id}:instruction")
            session.add(order)
            await session.commit()
            return await self.project(session, order)
        except IntegrityError:
            await session.rollback()
            raise DelegationError("delegation_concurrent_create", 409) from None
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def cancel(self, delegation_id, *, actor_user_id):
        self.require_enabled()
        session = await self.identity._session()
        try:
            await self.identity._assert_admin(session, actor_user_id)
            order = await session.scalar(select(AgentDelegation).where(AgentDelegation.id == delegation_uuid(delegation_id)).with_for_update())
            if order is None:
                raise DelegationError("delegation_not_found", 404)
            order.cancelled_at = order.cancelled_at or datetime.utcnow()
            await session.commit()
            from .agent_work_runtime import AgentWorkCoordinator
            coordinator = AgentWorkCoordinator(self.identity._db_manager, config=self.identity.config)
            rows = (await session.scalars(select(AgentWorkItem).where(AgentWorkItem.source_type == "employee_delegation", AgentWorkItem.source_id == str(order.id)))).all()
            for item in rows:
                await coordinator.cancel(str(item.id), actor=str(actor_user_id))
            return await self.project(session, order)
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def project(self, session, order):
        state, items = await delegation_state(session, order)
        result = order.to_safe_dict()
        result.update(state=state, phases={phase: item.to_safe_dict() if item else None for phase, item in items.items()})
        from .agent_delegation_planning import DelegationPlanner, active_children, policy
        children = await active_children(session, order)
        result["children"] = [{"delegation_id": str(child.id), "state": (await delegation_state(session, child))[0]}
            for child in children]
        try:
            await self.validate_authority(session, order)
            result["readiness"] = {"ready": True, "error_code": None}
        except Exception as exc:
            result["readiness"] = {"ready": False, "error_code": exc.code if isinstance(exc, DelegationError) else "delegation_authority_denied"}
        if state in {"awaiting_children", "child_blocked", "planning_blocked"}:
            result["readiness"] = {"ready": False, "error_code": "delegation_" + state}
        root = await DelegationPlanner(self).root(session, order) if policy(order) or order.planning_root_id else None
        root_policy = policy(root) if root else None
        result["planning"] = {"enabled": bool(policy(order)), "root_id": str(root.id) if root else None,
            "adopted_plan_artifact_id": str(order.adopted_plan_artifact_id) if order.adopted_plan_artifact_id else None,
            "reserved_output_tokens": root.planning_reserved_output_tokens if root else 0,
            "max_total_output_tokens": root_policy.max_total_output_tokens if root_policy else None,
            "created_children": root.planning_created_children if root else 0,
            "error_code": order.planning_error_code}
        result["artifacts"] = {}
        for phase, item in items.items():
            artifact = await adopted_artifact(session, item, order, phase)
            if artifact:
                result["artifacts"][phase] = artifact.to_safe_dict()
        return result

    async def list(self, *, actor_user_id, agent_id=None, limit=100):
        self.require_enabled()
        session = await self.identity._session()
        try:
            await self.identity._assert_admin(session, actor_user_id)
            query = select(AgentDelegation)
            if agent_id:
                aid = delegation_uuid(agent_id)
                query = query.where((AgentDelegation.delegator_agent_id == aid) | (AgentDelegation.worker_agent_id == aid) | (AgentDelegation.reviewer_agent_id == aid))
            rows = (await session.scalars(query.order_by(AgentDelegation.created_at.desc(), AgentDelegation.id).limit(min(max(int(limit), 1), 200)))).all()
            return [await self.project(session, order) for order in rows]
        finally:
            await session.close()
    async def get(self, delegation_id, *, actor_user_id):
        self.require_enabled()
        session = await self.identity._session()
        try:
            await self.identity._assert_admin(session, actor_user_id)
            order = await session.get(AgentDelegation, delegation_uuid(delegation_id))
            if order is None:
                raise DelegationError("delegation_not_found", 404)
            return await self.project(session, order)
        finally:
            await session.close()

    async def retry(self, delegation_id, *, actor_user_id, expected_work_item_id):
        self.require_enabled()
        session = await self.identity._session()
        try:
            actor = await self.identity._assert_admin(session, actor_user_id)
            await lock_reporting_graph(session)
            order = await session.scalar(select(AgentDelegation).where(AgentDelegation.id == delegation_uuid(delegation_id)).with_for_update())
            if order is None:
                raise DelegationError("delegation_not_found", 404)
            await self.validate_authority(session, order)
            state, items = await delegation_state(session, order)
            expected = delegation_uuid(expected_work_item_id)
            if order.cancelled_at or state == "returned":
                raise DelegationError("delegation_retry_not_allowed", 409)
            from .agent_delegation_planning import DelegationPlanner, policy
            root = await DelegationPlanner(self).root(session, order) if policy(order) or order.planning_root_id else None
            approved = policy(root) if root else None
            if approved and root.planning_reserved_output_tokens + order.max_output_tokens > approved.max_total_output_tokens:
                raise DelegationError("delegation_plan_output_budget_exhausted", 409)
            if state == "planning_blocked":
                if items.get("plan") is None or items["plan"].id != expected:
                    raise DelegationError("delegation_retry_not_allowed", 409)
                order.planning_error_code = None
                await session.commit()
                return await self.project(session, order)
            if state == "changes_requested":
                if items["review"].id != expected or order.round >= 16 or order.linked_work_item_id:
                    raise DelegationError("delegation_retry_not_allowed", 409)
                if approved and order.round > approved.max_rework_rounds:
                    raise DelegationError("delegation_plan_rework_budget_exhausted", 409)
                order.round += 1
                order.rework_from_work_item_id = expected
            else:
                phase = state.split(":")[0]
                item = items.get(phase)
                if item is None or item.id != expected or item.state not in {"blocked", "failed", "dead_letter"} or (phase == "work" and order.linked_work_item_id):
                    raise DelegationError("delegation_retry_not_allowed", 409)
                if item.attempt_count >= 100:
                    raise DelegationError("delegation_retry_limit", 409)
                if approved and item.attempt_count >= order.max_attempts_per_phase:
                    raise DelegationError("delegation_plan_attempt_budget_exhausted", 409)
                # Requeue this exact internal work so the coordinator finds
                # its previous AgentRun and reuses the original route manifest.
                item = await session.scalar(select(AgentWorkItem).where(AgentWorkItem.id == expected).with_for_update())
                prior_state = item.state
                if prior_state not in {"blocked", "failed", "dead_letter"}:
                    raise DelegationError("delegation_retry_not_allowed", 409)
                sequence = (await session.scalar(select(func.max(AgentWorkEvent.sequence)).where(AgentWorkEvent.work_item_id == item.id)) or 0) + 1
                session.add(AgentWorkEvent(work_item_id=item.id, sequence=sequence, event_type="delegation.retry_requested",
                    from_state=prior_state, to_state="pending", actor_kind="human", actor_user_id=actor,
                    payload_json={"delegation_id": str(order.id), "phase": phase}))
                item.state = "pending"
                item.max_attempts = order.max_attempts_per_phase if approved else min(100, max(item.max_attempts, item.attempt_count + 3))
                item.not_before = item.next_attempt_at = item.completed_at = None
                item.safe_error_code = item.blocker_code = item.escalation_reason = None
                item.result_summary = None
                item.result_summary_json = {}
                item.updated_at = datetime.utcnow()
            order.completed_at = None
            await session.commit()
            return await self.project(session, order)
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def artifact(self, delegation_id, artifact_id, *, actor_user_id):
        self.require_enabled()
        session = await self.identity._session()
        try:
            await self.identity._assert_admin(session, actor_user_id)
            order = await session.get(AgentDelegation, delegation_uuid(delegation_id))
            if order is None:
                raise DelegationError("delegation_not_found", 404)
            await self.validate_authority(session, order)
            candidate = await session.get(AgentDelegationArtifact, delegation_uuid(artifact_id))
            items = {candidate.phase: await session.get(AgentWorkItem, candidate.work_item_id)} if candidate and candidate.delegation_id == order.id else {}
            for phase, item in items.items():
                artifact = await adopted_artifact(session, item, order, phase)
                if artifact and artifact.id == delegation_uuid(artifact_id):
                    raw = decrypt_text(artifact.body_ciphertext, aad=f"delegation-artifact:{artifact.id}")
                    if hashlib.sha256(raw.encode()).hexdigest() != artifact.sha256:
                        raise DelegationError("delegation_artifact_corrupt", 409)
                    envelope = json.loads(raw)
                    return {**artifact.to_safe_dict(), "content": envelope["output"],
                            "evidence": envelope["evidence"], "child_returns": envelope["child_returns"],
                            "context_sha256": envelope["context_sha256"]}
            raise DelegationError("delegation_artifact_not_adopted", 404)
        finally:
            await session.close()
