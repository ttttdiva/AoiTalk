"""Orders between employees with immutable scope, revision and evidence pins."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from dataclasses import dataclass
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .agent_delegation_media_inputs import MediaInputError, MediaInputPin, resolve_media_input
from .media_operations_service import MediaOperationsError
from .storage_io import StorageError

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from ..features import Features
from ..memory.models import Agent, AgentRevision, ConversationSession, AgentDialogue, AgentDialogueDelivery, AgentDelegation, AgentDelegationArtifact, AgentOrganizationProfile, AgentProjectGrant, AgentRun, AgentSpaceAssignment, AgentWorkEvent, AgentWorkItem, Organization, Persona, PersonaOperatorAssignment, Project
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


class MediaInputPinDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset_revision_id: UUID
    output_id: UUID
    bytes_sha256: str = Field(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64)
    revision_hash: str = Field(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64)
    work_id: UUID
    agent_run_id: UUID
    plan_id: UUID
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64)
    generation_run_id: UUID
    generation_run_hash: str = Field(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64)
    output_provenance_hash: str = Field(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64)


class OperationsSourceRefDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    opportunity_id: UUID
    source_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64)


def normalize_operations_source_ref(value):
    if value is None:
        return None
    try:
        return OperationsSourceRefDTO.model_validate(value).model_dump(mode="json")
    except (ValidationError, TypeError, ValueError):
        raise DelegationError("delegation_operations_source_invalid") from None


def get_operations_source_ref(order) -> dict | None:
    stored = order.planning_input_pins_json or {}
    if not isinstance(stored, dict):
        raise DelegationError("delegation_operations_source_invalid", 403)
    value = stored.get("operations_source_ref")
    # Historical artifact pins could use this formerly unreserved key.
    if isinstance(value, dict) and "source_order_id" in value:
        return None
    try:
        return normalize_operations_source_ref(value)
    except DelegationError:
        raise DelegationError("delegation_operations_source_invalid", 403) from None


def normalize_media_input_pins(value):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 8:
        raise DelegationError("delegation_media_input_invalid")
    if any(not isinstance(pin, (dict, MediaInputPinDTO)) for pin in value):
        raise DelegationError("delegation_media_input_invalid")
    if any(isinstance(v, (bytes, bytearray)) for pin in value if isinstance(pin, dict) for v in pin.values()):
        raise DelegationError("delegation_media_input_invalid")
    try:
        return [MediaInputPinDTO.model_validate(pin).model_dump(mode="json") for pin in value]
    except (ValidationError, TypeError, ValueError):
        raise DelegationError("delegation_media_input_invalid") from None


def get_media_input_pins(order) -> tuple[MediaInputPin, ...]:
    stored = order.planning_input_pins_json
    if stored is None:
        return ()
    if not isinstance(stored, dict):
        raise DelegationError("delegation_media_input_invalid", 403)
    try:
        value = stored.get("media_inputs", [])
        # Historical planning artifacts may use this same arbitrary key.
        if (isinstance(value, dict) and {"source_order_id", "project_id", "persona_id"}.issubset(value)
                and ("content_hash" in value or {"artifact_id", "sha256", "work_item_id", "agent_run_id"}.issubset(value))):
            return ()
        if not isinstance(value, list):
            raise DelegationError("delegation_media_input_invalid")
        refs = normalize_media_input_pins(value)
        return tuple(MediaInputPin(**MediaInputPinDTO.model_validate(ref).model_dump()) for ref in refs)
    except (DelegationError, ValidationError, TypeError, ValueError):
        raise DelegationError("delegation_media_input_invalid", 403) from None


def canonical(value):
    """Stable JSON form retained for historical artifact projections."""

    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def delegation_uuid(value):
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise DelegationError("delegation_invalid_id") from None


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
            blocked = any(state in {"cancelled", "changes_requested", "evidence_missing", "child_blocked", "planning_blocked"}
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


@dataclass(frozen=True)
class _CapturedSourceActor:
    """Server-bound admission evidence; never constructed from a request."""

    actor: object
    source: dict
    run: AgentRun
    work: AgentWorkItem
    caller: Agent
    revision: AgentRevision
    project_id: UUID


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

    async def validate_authority(self, session, order, *, _depth=0, _seen=None, _scope_checks=None, _linked_work_phase="execution", _terminal_result_proof=False):
        self.require_enabled()
        _seen = set() if _seen is None else _seen
        _scope_checks = set() if _scope_checks is None else _scope_checks
        if order.id in _seen:
            return
        _seen.add(order.id)
        if _depth >= 8:
            raise DelegationError("delegation_depth_exceeded", 403)
        await self.identity._assert_admin(session, order.created_by)
        if order.source_actor_json is not None:
            await self.verified_source_origin(session, order)
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
        software_linked = False
        if order.linked_work_item_id:
            linked = await session.get(AgentWorkItem, order.linked_work_item_id, populate_existing=True)
            if linked is None or native_binding(linked) != order.linked_work_binding:
                raise DelegationError("delegation_native_work_binding_changed", 403)
            if linked.source_type == "local_agent_task" and linked.execution_adapter == "local_agent_task":
                from .agent_team_software_contract import validate_software_graph_linked_work
                await validate_software_graph_linked_work(session, order, phase=_linked_work_phase, service=self)
                software_linked = True
        if order.parent_delegation_id:
            parent = await session.get(AgentDelegation, order.parent_delegation_id, populate_existing=True)
            expected = phase_identity(parent, "plan")[0] if parent and order.parent_plan_artifact_id else parent.worker_agent_id if parent else None
            if parent is None or parent.cancelled_at or parent.planning_error_code or expected != order.delegator_agent_id:
                raise DelegationError("delegation_parent_unavailable", 403)
            if order.parent_plan_artifact_id:
                parent_items = await phase_items(session, parent)
                plan = await adopted_artifact(session, parent_items.get("plan"), parent, "plan")
                if (plan is None or plan.id != order.parent_plan_artifact_id or plan.sha256 != order.parent_plan_sha256
                        or (not (software_linked and _linked_work_phase == "admission") and parent.adopted_plan_artifact_id != plan.id) or order.parent_round != parent.plan_round):
                    raise DelegationError("delegation_parent_plan_changed", 403)
            if parent.completed_at and not _terminal_result_proof and (await delegation_state(session, order))[0] != "returned":
                raise DelegationError("delegation_parent_terminal", 403)
            await self.validate_authority(session, parent, _depth=_depth + 1, _seen=_seen, _scope_checks=_scope_checks,
                                          _terminal_result_proof=_terminal_result_proof)
        if order.linked_work_item_id and not software_linked:
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
        decision = await self.authority.resolve(session=session, agent_id=agent_id, revision_id=revision_id,
            project_id=project_id, persona_id=persona_id, required_capability=DELEGATION_REQUIRED_CAPABILITIES[0])
        if not decision.allowed:
            raise DelegationError("delegation_authority_denied", 403)
        if persona_id:
            from .agent_capabilities import AGENT_CAPABILITY_CATALOG as AGENT_TEAM_CAPABILITY_CATALOG
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
            return await self._create_in_session(session, actor, payload, None)
        except IntegrityError:
            await session.rollback()
            raise DelegationError("delegation_concurrent_create", 409) from None
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def verified_source_origin(self, session, order):
        """Resolve an order's immutable Agent origin without requiring its old lease."""
        return await self.verified_source_origin_fields(
            session, source=order.source_actor_json, project_id=order.project_id,
            persona_id=order.persona_id, delegator_agent_id=order.delegator_agent_id,
            delegator_revision_id=order.delegator_revision_id, created_by=order.created_by)

    async def verified_source_origin_fields(
        self, session, *, source, project_id, persona_id, delegator_agent_id,
        delegator_revision_id, created_by):
        """Resolve a typed immutable Agent origin without requiring its old lease."""
        keys = ("source_run_id", "source_work_item_id", "source_session_id", "source_agent_id",
                "source_revision_id", "inherited_user_id")
        if not isinstance(source, dict) or source.get("version") != 1 or set(source) != {"version", *keys}:
            raise DelegationError("delegation_source_invalid", 403)
        try:
            ids = {key: delegation_uuid(source[key]) for key in keys}
        except (KeyError, DelegationError):
            raise DelegationError("delegation_source_invalid", 403) from None
        run = await session.get(AgentRun, ids["source_run_id"], populate_existing=True)
        work = await session.get(AgentWorkItem, ids["source_work_item_id"], populate_existing=True)
        conversation = await session.get(ConversationSession, ids["source_session_id"], populate_existing=True)
        revision = await session.get(AgentRevision, ids["source_revision_id"], populate_existing=True)
        agent = await session.get(Agent, ids["source_agent_id"], populate_existing=True)
        if (run is None or work is None or conversation is None or revision is None or agent is None
                or run.work_item_id != work.id or run.session_id != conversation.id
                or run.agent_id != ids["source_agent_id"] or run.agent_revision_id != revision.id
                or revision.agent_id != run.agent_id or work.assigned_agent_id != run.agent_id
                or work.agent_revision_id != revision.id or work.conversation_session_id != conversation.id
                or str(run.user_id) != str(ids["inherited_user_id"])
                or str(conversation.user_id) != str(ids["inherited_user_id"])
                or conversation.agent_id != run.agent_id or conversation.deleted_at is not None
                or run.project_id != project_id or work.project_id != project_id
                or conversation.project_id != project_id or work.persona_id != persona_id
                or delegator_agent_id != run.agent_id or delegator_revision_id != revision.id
                or created_by != ids["inherited_user_id"]):
            raise DelegationError("delegation_source_invalid", 403)
        # Follow the same canonical Human anchor used by Dialogue admission;
        # metadata is only a pointer and never grants authority by itself.
        from .agent_run_service import AgentRunService
        dialogue = await session.scalar(select(AgentDialogue).where(AgentDialogue.worker_session_id == conversation.id))
        if dialogue is not None and (dialogue.worker_agent_id != run.agent_id
                or dialogue.worker_agent_revision_id != run.agent_revision_id
                or dialogue.owner_user_id != ids["inherited_user_id"] or dialogue.project_id != project_id):
            raise DelegationError("delegation_source_invalid", 403)
        if dialogue is not None:
            metadata = (run.run_metadata or {}).get("agent_dialogue") if isinstance(run.run_metadata, dict) else None
            delivery = await session.scalar(select(AgentDialogueDelivery).where(
                AgentDialogueDelivery.dialogue_id == dialogue.id,
                AgentDialogueDelivery.receiver_run_id == run.id,
                AgentDialogueDelivery.receiver_work_item_id == work.id,
                AgentDialogueDelivery.kind.in_(("request", "control"))))
            parent = await session.get(AgentRun, delivery.source_run_id) if delivery else None
            parent_work = await session.get(AgentWorkItem, delivery.source_work_item_id) if delivery and delivery.source_work_item_id else None
            root_session = await session.get(ConversationSession, dialogue.root_session_id, populate_existing=True)
            authorization = dialogue.authorization_json
            projectless = (root_session is not None and parent is not None and parent_work is not None
                and root_session.project_id is None and parent.project_id is None and parent_work.project_id is None
                and isinstance(authorization, dict)
                and (str(authorization.get("context_project_hint_id")) == str(project_id)
                     or (isinstance(authorization.get("team_graph"), dict)
                         and str(authorization["team_graph"].get("target_project_id")) == str(project_id)))
                and dialogue.authorization_hash == hashlib.sha256(canonical(authorization).encode("utf-8")).hexdigest())
            projectful = (root_session is not None and parent is not None and parent_work is not None
                and root_session.project_id == parent.project_id == parent_work.project_id == project_id)
            from ..memory.models.conversations import ConversationMessage
            source_message = await session.get(ConversationMessage, delivery.source_message_id) if delivery and delivery.source_message_id else None
            relay = source_message.message_metadata if source_message and isinstance(source_message.message_metadata, dict) else {}
            if (not isinstance(metadata, dict) or str(metadata.get("dialogue_id")) != str(dialogue.id)
                    or str(metadata.get("receiver_role" if delivery and delivery.kind == "control" else "role", "")).casefold() != "worker" or delivery is None or parent is None
                    or str(metadata.get("delivery_id")) != str(delivery.id)
                    or str(metadata.get("source_run_id")) != str(delivery.source_run_id)
                    or metadata.get("expected_control_revision" if delivery.kind == "control" else "control_revision", 0) != (dialogue.control_revision or 0)
                    or (delivery.kind == "control" and (parent_work is None or source_message is None or source_message.deleted_at is not None
                        or source_message.session_id != dialogue.root_session_id or source_message.role != "assistant"
                        or source_message.sender_type != "agent" or str(source_message.sender_id) != str(dialogue.root_agent_id)
                        or str(relay.get("agent_run_id")) != str(parent.id)
                        or str(relay.get("source_work_item_id")) != str(parent_work.id)
                        or str(relay.get("agent_dialogue_id")) != str(dialogue.id)
                        or relay.get("control_revision") != dialogue.control_revision
                        or metadata.get("delivery_kind") != "control"
                        or str(metadata.get("source_work_item_id")) != str(delivery.source_work_item_id)
                        or str(metadata.get("source_message_id")) != str(delivery.source_message_id)
                        or str(metadata.get("receiver_session_id")) != str(delivery.receiver_session_id)
                        or str(metadata.get("receiver_agent_id")) != str(delivery.receiver_agent_id)
                        or str(metadata.get("receiver_agent_revision_id")) != str(delivery.receiver_agent_revision_id)))
                    or int(delivery.expected_control_revision or 0) != int(dialogue.control_revision or 0)
                    or parent_work is None or parent.work_item_id != parent_work.id
                    or not (projectless or projectful)
                    or parent_work.conversation_session_id != dialogue.root_session_id
                    or parent_work.assigned_agent_id != dialogue.root_agent_id
                    or parent_work.agent_revision_id != dialogue.root_agent_revision_id
                    or delivery.receiver_session_id != conversation.id or delivery.receiver_agent_id != run.agent_id
                    or delivery.receiver_agent_revision_id != run.agent_revision_id
                    or delivery.source_session_id != dialogue.root_session_id
                    or delivery.source_agent_id != dialogue.root_agent_id
                    or delivery.source_agent_revision_id != dialogue.root_agent_revision_id
                    or parent.session_id != dialogue.root_session_id or parent.agent_id != dialogue.root_agent_id
                    or parent.agent_revision_id != dialogue.root_agent_revision_id
                    or str(parent.user_id) != str(ids["inherited_user_id"])
                    or (delivery.kind == "control" and (
                        delivery.source_work_item_id is None
                        or delivery.source_work_item_id != parent.work_item_id
                        or str(metadata.get("source_work_item_id")) != str(delivery.source_work_item_id)
                        or metadata.get("delivery_kind") != "control"
                        or metadata.get("expected_control_revision") != delivery.expected_control_revision))):
                raise DelegationError("delegation_source_invalid", 403)
        from ..memory.project_repository import ProjectRepository
        if not await ProjectRepository.has_permission(session, project_id, ids["inherited_user_id"], "manage_members"):
            raise DelegationError("delegation_project_management_denied", 403)
        try:
            anchor = await AgentRunService(self.identity._db_manager, config=self.identity.config)._build_dialogue_human_anchor(
                session, source_run=run, source_work=work, dialogue=dialogue, include_body=False)
            if delegation_uuid(anchor["refs"]["owner_user_id"]) != ids["inherited_user_id"]:
                raise DelegationError("delegation_source_invalid", 403)
        except Exception as exc:
            raise DelegationError("delegation_source_invalid", 403) from exc
        return dict(source)

    async def capture_current_source_actor(self, session, *, project_id):
        """Capture the current turn after locking and checking its live run and lease."""
        from .agent_run_service import get_current_agent_run_id
        from .turn_context import get_turn_context
        from ..memory.project_repository import ProjectRepository

        context = get_turn_context()
        user_id = delegation_uuid(context.user_id)
        agent_id = delegation_uuid(context.agent_id)
        revision_id = delegation_uuid(context.agent_revision_id)
        session_id = delegation_uuid(context.session_id)
        run_id = delegation_uuid(get_current_agent_run_id())
        actor = await self.identity._assert_admin(session, user_id)
        run = await session.scalar(select(AgentRun).where(AgentRun.id == run_id).with_for_update())
        work = await session.scalar(select(AgentWorkItem).where(AgentWorkItem.id == run.work_item_id).with_for_update()) if run and run.work_item_id else None
        if (run is None or work is None or run.status not in {"queued", "running"}
                or run.session_id != session_id or run.agent_id != agent_id or run.agent_revision_id != revision_id
                or work.active_agent_run_id != run.id or work.state not in {"claimed", "running"}
                or (run.work_item_attempt != work.attempt_count and not (
                    run.work_item_attempt is None and work.source_type == "chat_turn"
                    and work.source_id == str(run.id) and work.max_attempts == 1))
                or not work.lease_owner or not work.lease_token or work.lease_expires_at is None
                or work.lease_expires_at <= datetime.utcnow()):
            raise DelegationError("delegation_source_inactive", 403)
        project_id = delegation_uuid(project_id)
        if delegation_uuid(context.project_id) != project_id:
            raise DelegationError("delegation_source_scope_mismatch", 403)
        if not await ProjectRepository.has_permission(session, project_id, user_id, "manage_members"):
            raise DelegationError("delegation_project_management_denied", 403)
        revision = await session.get(AgentRevision, revision_id)
        caller = await session.get(Agent, agent_id)
        if revision is None or caller is None or revision.agent_id != caller.id:
            raise DelegationError("delegation_source_invalid", 403)
        if "aoi_tools" not in (revision.capability_ceiling_json or []):
            raise DelegationError("delegate_policy_denied", 403)
        source = {"version": 1, "source_run_id": str(run_id), "source_work_item_id": str(work.id),
                  "source_session_id": str(session_id), "source_agent_id": str(agent_id),
                  "source_revision_id": str(revision_id), "inherited_user_id": str(user_id)}
        return _CapturedSourceActor(actor, source, run, work, caller, revision, project_id)

    async def create_from_agent(self, **payload):
        """Admit a bounded delegation from the current server-bound Agent turn."""
        self.require_enabled()
        forbidden = {"delegator_agent_id", "delegator_revision_id", "parent_delegation_id",
                     "linked_work_item_id", "actor_user_id", "created_by", "source_actor_json"}
        if forbidden.intersection(payload):
            raise DelegationError("delegation_source_override_denied", 403)
        session = await self.identity._session()
        try:
            captured = await self.capture_current_source_actor(session, project_id=payload.get("project_id"))
            actor, source, run, work = captured.actor, captured.source, captured.run, captured.work
            user_id, agent_id = delegation_uuid(source["inherited_user_id"]), run.agent_id
            revision_id, project_id = run.agent_revision_id, captured.project_id
            payload = {**payload, "delegator_agent_id": agent_id, "delegator_revision_id": revision_id}
            graph_authorization = await self._inherited_team_graph(session, run, work, user_id, project_id)
            if graph_authorization is not None:
                if graph_authorization.get("execution_kind", "generic") not in {"generic", "software_code"}:
                    raise DelegationError("delegation_graph_kind_mismatch", 403)
                from .agent_team_graph_admission import TeamGraphRef
                inherited_ref = TeamGraphRef.model_validate(graph_authorization["ref"]).model_dump(mode="json")
                if payload.get("team_graph_ref") is not None:
                    explicit_ref = TeamGraphRef.model_validate(payload["team_graph_ref"]).model_dump(mode="json")
                    if explicit_ref != inherited_ref:
                        raise DelegationError("delegation_graph_ref_mismatch", 403)
                payload = {**payload, "team_graph_ref": inherited_ref}
                handoffs = graph_authorization.get("handoff_inputs", {})
                if not isinstance(handoffs, dict):
                    raise DelegationError("delegation_graph_authorization_mismatch", 403)
                if handoffs:
                    planning = dict(payload.get("planning") or {})
                    inputs = dict(planning.get("input_artifacts") or {})
                    if set(inputs) & set(handoffs):
                        raise DelegationError("delegation_graph_input_conflict", 403)
                    inputs.update({key: pin["artifact_id"] for key, pin in handoffs.items()})
                    planning["input_artifacts"] = inputs
                    payload = {**payload, "planning": planning}
            return await self._create_in_session(session, actor, payload, source, graph_authorization)
        except IntegrityError:
            await session.rollback()
            raise DelegationError("delegation_concurrent_create", 409) from None
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def _inherited_team_graph(self, session, run, work, owner, project_id):
        """Read graph authority only from the bound Dialogue and delivery ledger."""
        dialogue = await session.scalar(select(AgentDialogue).where(
            AgentDialogue.worker_session_id == run.session_id).with_for_update())
        if dialogue is None:
            return None
        authorization = dialogue.authorization_json
        if not isinstance(authorization, dict) or "team_graph" not in authorization:
            return None
        graph = authorization["team_graph"]
        delivery = await session.scalar(select(AgentDialogueDelivery).where(
            AgentDialogueDelivery.dialogue_id == dialogue.id,
            AgentDialogueDelivery.receiver_run_id == run.id,
            AgentDialogueDelivery.receiver_work_item_id == work.id,
            AgentDialogueDelivery.kind.in_(("request", "control"))))
        if (not isinstance(graph, dict) or not isinstance(graph.get("ref"), dict)
                or not isinstance(graph.get("nodepins"), dict)
                or dialogue.authorization_hash != hashlib.sha256(canonical(authorization).encode("utf-8")).hexdigest()
                or dialogue.owner_user_id != owner or dialogue.project_id != project_id
                or str(dialogue.state or "").casefold() not in {"queued", "running", "waiting"}
                or (dialogue.deadline is not None and dialogue.deadline <= datetime.utcnow())
                or dialogue.worker_agent_id != run.agent_id
                or dialogue.worker_agent_revision_id != run.agent_revision_id
                or str(run.user_id) != str(owner) or run.project_id != project_id
                or work.assigned_agent_id != run.agent_id or work.agent_revision_id != run.agent_revision_id
                or work.conversation_session_id != run.session_id or work.project_id != project_id
                or delivery is None or delivery.receiver_session_id != run.session_id
                or int(delivery.expected_control_revision or 0) != int(dialogue.control_revision or 0)
                or str(delivery.state or "").casefold() in {"failed", "cancelled", "expired"}
                or delivery.receiver_agent_id != run.agent_id
                or delivery.receiver_agent_revision_id != run.agent_revision_id):
            raise DelegationError("delegation_source_invalid", 403)
        from .agent_team_graph_schema import GraphDefinition
        from .agent_team_graph_service import AgentTeamGraphService
        ref = graph["ref"]
        envelope = await AgentTeamGraphService().get_in_session(session, delegation_uuid(ref["graph_id"]),
            actor_user_id=owner, revision_id=delegation_uuid(ref["revision_id"]))
        definition = GraphDefinition.model_validate(envelope["definition"])
        group = next((item for item in definition.groups if item.key == ref.get("group_key")), None)
        expected_kind = group.execution_kind if group and group.execution_kind else "generic"
        if (group is None or expected_kind not in {"generic", "media_image", "media_video", "software_code"}
                or graph.get("execution_kind", "generic") != expected_kind
                or envelope["content_hash"] != graph.get("graph_hash")
                or (envelope["project_id"] is not None and envelope["project_id"] != str(project_id))
                or (expected_kind != "generic" and graph.get("target_project_id") != str(project_id))
                or (graph.get("target_project_id") is not None and graph["target_project_id"] != str(project_id))):
            raise DelegationError("delegation_graph_authorization_mismatch", 403)
        if graph.get("handoff_inputs"):
            from .agent_team_handoff_inputs import _resolve_group_handoff_inputs
            current = await _resolve_group_handoff_inputs(session, service=self, owner_user_id=owner,
                root_session_id=dialogue.root_session_id, case_key=graph["case_key"],
                graph={**envelope, "case_project_id": str(project_id)}, target_group=ref["group_key"], active=True)
            if current != graph["handoff_inputs"]:
                raise DelegationError("delegation_graph_input_changed", 403)
        return graph

    async def _validate_graph_source_targets(self, session, source, worker_id, reviewer_id):
        if source is None:
            return
        from .agent_delegate_policy import revision_allows_agent_delegate
        revision = await session.get(AgentRevision, delegation_uuid(source["source_revision_id"]))
        caller = await session.get(Agent, delegation_uuid(source["source_agent_id"]))
        for target_id in (worker_id, reviewer_id):
            target = await session.get(Agent, target_id)
            if revision is None or caller is None or target is None or not revision_allows_agent_delegate(
                    revision, tool_name="delegate_plan", target_agent_id=target.id,
                    target_agent_owner_id=target.created_by, caller_agent_owner_id=caller.created_by,
                    owner_user_id=delegation_uuid(source["inherited_user_id"])):
                raise DelegationError("delegate_policy_denied", 403)

    async def _create_in_session(self, session, actor, payload, source, graph_authorization=None):
        await lock_reporting_graph(session)
        work_role = payload.get("work_role")
        if work_role not in WORK_ROLES:
            raise DelegationError("delegation_invalid_role")
        instruction = str(payload.get("instruction") or "").strip()
        title = str(payload.get("title") or "").strip()
        key = str(payload.get("idempotency_key") or "").strip()
        if not instruction or len(instruction) > 12000 or not title or len(title) > 160 or not key or len(key) > 255:
            raise DelegationError("delegation_invalid_input")
        graph_ref = payload.get("team_graph_ref")
        names = ("delegator_agent_id", "delegator_revision_id", "worker_agent_id", "worker_revision_id",
                 "reviewer_agent_id", "reviewer_revision_id", "project_id")
        identifiers = {name: delegation_uuid(payload.get(name)) if payload.get(name) else None for name in names}
        for name in ("persona_id", "parent_delegation_id", "linked_work_item_id"):
            identifiers[name] = delegation_uuid(payload[name]) if payload.get(name) else None
        if any(identifiers[name] is None for name in ("delegator_agent_id", "delegator_revision_id", "project_id")):
            raise DelegationError("delegation_invalid_input")
        if not graph_ref and any(identifiers[name] is None for name in names[2:6]):
            raise DelegationError("delegation_invalid_input")
        planning = payload.get("planning")
        if planning is not None and "graph_constraints" in planning:
            raise DelegationError("delegation_graph_constraints_server_only", 403)
        existing = await session.scalar(select(AgentDelegation).where(AgentDelegation.idempotency_key == key))
        graph_request_hash = None
        if graph_ref is not None:
            from .agent_team_graph_admission import TeamGraphRef, admit_team_graph
            if planning is None or "roster" in planning or work_role == "code_worker":
                raise DelegationError("delegation_graph_planning_required")
            graph_ref = TeamGraphRef.model_validate(graph_ref).model_dump(mode="json")
            request_input = {"identifiers": {name: str(value) if value else None for name, value in identifiers.items()},
                "title": title, "work_role": work_role, "instruction": instruction, "created_by": str(actor),
                "source_actor": source, "planning": planning, "team_graph_ref": graph_ref}
            graph_request_hash = hashlib.sha256(canonical(request_input).encode()).hexdigest()
            if existing:
                if (existing.planning_policy_json or {}).get("graph_request_hash") != graph_request_hash:
                    raise DelegationError("delegation_idempotency_conflict", 409)
                await self._validate_graph_source_targets(session, source, existing.worker_agent_id, existing.reviewer_agent_id)
                await self.validate_authority(session, existing)
                return await self.project(session, existing)
            normalized = await admit_team_graph(session, ref=graph_ref, owner=actor,
                project_id=identifiers["project_id"], leader_id=identifiers["delegator_agent_id"],
                leader_revision_id=identifiers["delegator_revision_id"], planning=planning, config=self.identity.config,
                authorization=graph_authorization)
            if normalized.get("execution_kind", "generic") not in {"generic", "software_code"}:
                raise DelegationError("delegation_graph_kind_mismatch", 403)
            if normalized.get("execution_kind") == "software_code" and graph_authorization is None:
                raise DelegationError("delegation_graph_authorization_mismatch", 403)
            normalized.pop("execution_kind", None)
            graph = normalized["graph_constraints"]
            nodes = {node["key"]: node for node in graph["roster"]}
            pairs = []
            roster = {entry["key"]: entry for entry in normalized["roster"]}
            for worker, reviewer in graph["allowed_pairs"]:
                if (graph.get("execution_kind") != "software_code"
                        and work_role not in roster[worker]["allowed_work_roles"]):
                    continue
                candidate = {"worker_agent_id": nodes[worker]["agent_id"], "worker_revision_id": nodes[worker]["revision_id"],
                    "reviewer_agent_id": nodes[reviewer]["agent_id"], "reviewer_revision_id": nodes[reviewer]["revision_id"]}
                if all(identifiers[name] is None or str(identifiers[name]) == value for name, value in candidate.items()):
                    pairs.append(candidate)
            if not pairs:
                raise DelegationError("delegation_graph_pair_invalid", 403)
            identifiers.update({name: delegation_uuid(value) for name, value in pairs[0].items()})
            payload = {**payload, "planning": normalized}
        if len({identifiers[name] for name in ("delegator_agent_id", "worker_agent_id", "reviewer_agent_id")}) != 3:
            raise DelegationError("delegation_independent_employees_required")
        await self._validate_graph_source_targets(session, source, identifiers["worker_agent_id"], identifiers["reviewer_agent_id"])
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
            if (linked is None or not (
                    (linked.source_type == "marketplace_crowdworks_fulfillment" and linked.execution_adapter in {"crowdworks_development_web_development", "crowdworks_development_app_development"})
                    or (linked.source_type == "local_agent_task" and linked.execution_adapter == "local_agent_task"))
                    or linked.assigned_agent_id != identifiers["worker_agent_id"] or linked.agent_revision_id != identifiers["worker_revision_id"]
                    or linked.project_id != project.id or linked.persona_id != identifiers["persona_id"]):
                raise DelegationError("delegation_native_work_binding_invalid")
            linked_binding = native_binding(linked)
        media_refs = normalize_media_input_pins(payload.get("media_input_pins"))
        operations_ref = normalize_operations_source_ref(payload.get("operations_source_ref"))
        digest_input = {**{name: str(value) if value else None for name, value in identifiers.items()},
            "title": title, "work_role": work_role, "instruction": instruction, "created_by": str(actor), "linked_work_binding": linked_binding}
        if media_refs:
            digest_input["media_input_pins"] = media_refs
        if operations_ref is not None:
            digest_input["operations_source_ref"] = operations_ref
        if source is not None:
            digest_input["source_actor"] = source
        if payload.get("planning") is not None:
            from .agent_delegation_planning import DelegationPlanningPolicy
            digest_input["planning"] = DelegationPlanningPolicy.model_validate(payload["planning"]).model_dump(mode="json")
        digest = hashlib.sha256(canonical(digest_input).encode()).hexdigest()
        if existing:
            if existing.content_hash != digest:
                raise DelegationError("delegation_idempotency_conflict", 409)
            await self.validate_authority(session, existing)
            return await self.project(session, existing)
        order = AgentDelegation(id=uuid4(), **identifiers, title=title, work_role=work_role, content_hash=digest,
            idempotency_key=key, created_by=actor, round=1, plan_round=1, linked_work_binding=linked_binding,
            source_actor_json=source, planning_policy_json={}, max_output_tokens=8192, max_attempts_per_phase=3,
            planning_reserved_output_tokens=0, planning_created_children=0)
        if payload.get("planning") is not None:
            from .agent_delegation_planning import DelegationPlanner
            await DelegationPlanner(self).prepare(session, order, payload["planning"])
            if graph_authorization is not None:
                handoffs = graph_authorization.get("handoff_inputs", {})
                stored = order.planning_policy_json["input_pins"]
                for key, pin in handoffs.items():
                    if any(stored.get(key, {}).get(name) != pin.get(name) for name in
                           ("artifact_id", "sha256", "work_item_id", "agent_run_id", "source_order_id", "project_id", "persona_id")):
                        raise DelegationError("delegation_graph_input_changed", 403)
                    stored[key] = dict(pin)
                order.planning_policy_json = {**order.planning_policy_json, "input_pins": stored}
            if graph_request_hash:
                compiled_graph = order.planning_policy_json["policy"]["graph_constraints"]
                provenance = {key: compiled_graph[key] for key in
                    ("graph_id", "graph_revision_id", "graph_hash", "group_key", "root", "leader")}
                if graph_authorization is not None:
                    provenance["case_key"] = graph_authorization.get("case_key")
                order.planning_policy_json = {**order.planning_policy_json, "graph_request_hash": graph_request_hash,
                    "team_graph_ref": graph_ref, "graph_provenance": provenance}
        await self.validate_authority(session, order, _linked_work_phase=(
            "admission" if order.linked_work_item_id and linked.source_type == "local_agent_task" else "execution"))
        if operations_ref is not None:
            if "operations_source_ref" in ((order.planning_policy_json or {}).get("input_pins") or {}):
                raise DelegationError("delegation_operations_source_invalid")
            from .agent_delegation_operations_inputs import validate_operations_source_ref
            operations_ref = await validate_operations_source_ref(
                session, owner_user_id=order.created_by, project_id=order.project_id, source_ref=operations_ref)
            order.planning_input_pins_json = {**(order.planning_input_pins_json or {}), "operations_source_ref": operations_ref}
        if media_refs:
            order.planning_input_pins_json = {**(order.planning_input_pins_json or {}), "media_inputs": media_refs}
            try:
                for pin in get_media_input_pins(order):
                    await resolve_media_input(session, order, reviewer_agent_id=order.reviewer_agent_id,
                        reviewer_revision_id=order.reviewer_revision_id, pin=pin, authority_resolver=self.authority)
            except (MediaInputError, MediaOperationsError, StorageError, OSError):
                raise DelegationError("delegation_media_input_denied", 403) from None
        order.instruction_ciphertext = encrypt_text(instruction, aad=f"delegation:{order.id}:instruction")
        session.add(order)
        await session.commit()
        return await self.project(session, order)

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
        result["operations_source_ref"] = get_operations_source_ref(order)
        result["media_input_pins"] = [MediaInputPinDTO.model_validate({name: getattr(pin, name) for name in MediaInputPinDTO.model_fields}).model_dump(mode="json") for pin in get_media_input_pins(order)]
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
        graph_ref = (order.planning_policy_json or {}).get("team_graph_ref")
        if graph_ref:
            graph = order.planning_policy_json["policy"]["graph_constraints"]
            result["planning"]["team_graph_ref"] = graph_ref
            result["planning"]["graph_provenance"] = {key: graph[key] for key in
                ("graph_id", "graph_revision_id", "graph_hash", "group_key", "root", "leader", "roster", "allowed_pairs")}
        result["artifacts"] = {}
        for phase, item in items.items():
            artifact = await adopted_artifact(session, item, order, phase)
            if artifact:
                result["artifacts"][phase] = artifact.to_safe_dict()
        return result

    async def list(self, *, actor_user_id, agent_id=None, limit=100):
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
        session = await self.identity._session()
        try:
            await self.identity._assert_admin(session, actor_user_id)
            order = await session.get(AgentDelegation, delegation_uuid(delegation_id))
            if order is None:
                raise DelegationError("delegation_not_found", 404)
            # Historical artifacts remain readable after the employee runtime
            # retirement.  Current authority is diagnostic metadata here;
            # mutation endpoints are removed from the HTTP surface.
            try:
                await self.validate_authority(session, order)
            except DelegationError:
                pass
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
