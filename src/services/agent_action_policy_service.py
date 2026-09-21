"""Immutable human-authored policies and serialized, provenance-bound proposals."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select, update

from ..features import Features
from ..memory.models import Agent, AgentRevision, AgentRun, AgentWorkItem, Organization, User
from ..memory.models.agent_automation import (
    AgentActionPolicy, AgentActionPolicyRevision, AgentAutomationEvent,
    AgentAutomationRule, AgentAutomationRuleAction, AgentAutomationRuleRevision,
)
from ..memory.models.operations import ExternalAction, ExternalActionApproval, ExternalActionAttempt, ExternalConnection, OperationEvent
from .agent_authority import AgentAuthorityResolver
from .agent_identity_service import sha256_json
from .integration_action_registry import ActionPolicyError, IntegrationActionRegistry
from .operations_service import payload_hash


def uid(value):
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise ActionPolicyError("invalid_identity") from None


def naive(value):
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise ActionPolicyError("invalid_date")
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def window(row, now=None):
    now = now or datetime.utcnow()
    return not (row.active_from and naive(row.active_from) > now) and not (row.active_until and naive(row.active_until) <= now)


def connection_hash(row):
    return sha256_json({key: str(getattr(row, key)) if getattr(row, key) is not None else None
                        for key in ("id", "owner_user_id", "project_id", "provider_key", "remote_account_ref", "credential_ref", "version")})


def record_action_event(session, action, event_type, *, attempt_id=None, receipt_id=None,
                        execution_work_item_id=None, execution_run_id=None):
    session.add(OperationEvent(owner_user_id=action.owner_user_id, project_id=action.project_id,
        entity_type="action", entity_id=action.id, event_type=event_type, actor_id=None, actor_type="system",
        payload_json={"authorization_mode": action.authorization_mode, "action_version": action.action_version,
            "payload_hash": action.payload_hash, "action_policy_revision_id": str(action.action_policy_revision_id),
            "connection_id": str(action.connection_id), "connection_binding_hash": action.source_snapshot_hash,
            "action_policy_hash": action.action_policy_hash, "origin_agent_id": str(action.origin_agent_id),
            "origin_agent_run_id": str(action.origin_agent_run_id),
            "origin_work_item_id": str(action.origin_work_item_id) if action.origin_work_item_id else None,
            "attempt_id": str(attempt_id) if attempt_id else None, "receipt_id": str(receipt_id) if receipt_id else None,
            "execution_work_item_id": str(execution_work_item_id) if execution_work_item_id else None,
            "execution_run_id": str(execution_run_id) if execution_run_id else None}))


def policy_content(revision):
    return {key: (str(value) if isinstance(value, UUID) else naive(value).isoformat() if isinstance(value, datetime) else value)
            for key, value in (("policy_id", revision.policy_id), ("action_type", revision.action_type),
            ("connection_id", revision.connection_id), ("authorization_mode", revision.authorization_mode),
            ("constraints", revision.constraints_json), ("rate_limit", revision.rate_limit_json),
            ("dedupe_window_seconds", revision.dedupe_window_seconds), ("fallback_behavior", revision.fallback_behavior),
            ("active_from", revision.active_from), ("active_until", revision.active_until),
            ("registry_schema_version", revision.registry_schema_version),
            ("registry_adapter_key", revision.registry_adapter_key), ("registry_adapter_version", revision.registry_adapter_version))}


class AgentActionPolicyService:
    def __init__(self, db_manager=None, *, registry=None, authority_resolver=None, credential_vault=None, config=None):
        self.db_manager, self.config = db_manager, config
        self.registry = registry or IntegrationActionRegistry()
        self.authority = authority_resolver or AgentAuthorityResolver(db_manager, config=config)
        self.vault = credential_vault

    def _vault(self):
        if self.vault is None:
            from .integration_credential_vault_service import IntegrationCredentialVaultService
            self.vault = IntegrationCredentialVaultService()
        return self.vault

    @staticmethod
    def feature_gate():
        if not Features.virtual_company() or not Features.autonomous_agent_runtime():
            raise ActionPolicyError("employee_features_disabled", 404)

    async def admin(self, session, actor_user_id):
        self.feature_gate()
        actor = uid(actor_user_id)
        user = await session.get(User, actor)
        if await session.get(Agent, actor) or user is None or not user.is_active or user.role != "admin":
            raise ActionPolicyError("administrator_human_required", 403)
        return actor

    async def _organization(self, session, *, bounded=False):
        self.feature_gate()
        org = await session.scalar(select(Organization).where(Organization.singleton_key == "installation").execution_options(populate_existing=True))
        if org is None or org.autonomy_level == "disabled":
            raise ActionPolicyError("organization_runtime_disabled", 403)
        policy = org.policy_json or {}
        if policy.get("allow_agent_runtime") is not True or policy.get("allow_external_actions") is not True:
            raise ActionPolicyError("organization_external_actions_denied", 403)
        if bounded and (policy.get("require_human_approval") is not False or org.autonomy_level not in {"bounded", "autonomous"}):
            raise ActionPolicyError("human_approval_required", 403)

    async def _policy(self, session, policy_id, *, lock=False):
        policy_id = uid(policy_id)
        if lock:
            # This no-op write is also a SQLite write-transaction fence. It
            # serializes cross-process callers, unlike asyncio locks.
            await session.execute(update(AgentActionPolicy).where(AgentActionPolicy.id == policy_id).values(version=AgentActionPolicy.version, updated_at=AgentActionPolicy.updated_at))
        row = await session.scalar(select(AgentActionPolicy).where(AgentActionPolicy.id == policy_id).execution_options(populate_existing=True))
        if row is None:
            raise ActionPolicyError("action_policy_not_found", 404)
        return row

    async def current(self, session, policy_id):
        return await session.scalar(select(AgentActionPolicyRevision).where(AgentActionPolicyRevision.policy_id == uid(policy_id)).order_by(AgentActionPolicyRevision.version.desc()).limit(1))

    async def project(self, session, policy):
        revision = await self.current(session, policy.id)
        return {**policy.to_safe_dict(), "current_revision": revision.to_safe_dict() if revision else None}

    async def list_policies(self, session, *, actor_user_id, agent_id):
        await self.admin(session, actor_user_id)
        rows = (await session.scalars(select(AgentActionPolicy).where(AgentActionPolicy.agent_id == uid(agent_id)).order_by(AgentActionPolicy.created_at).limit(200))).all()
        return [await self.project(session, row) for row in rows]

    async def get_policy(self, session, policy_id, *, actor_user_id):
        await self.admin(session, actor_user_id)
        return await self.project(session, await self._policy(session, policy_id))

    async def create_policy(self, session, *, actor_user_id, agent_id, display_name, idempotency_key):
        actor = await self.admin(session, actor_user_id)
        agent_id = uid(agent_id)
        await session.execute(update(Agent).where(Agent.id == agent_id).values(state=Agent.state, updated_at=Agent.updated_at))
        if await session.get(Agent, agent_id) is None:
            raise ActionPolicyError("agent_not_found", 404)
        if not isinstance(display_name, str) or not 1 <= len(display_name.strip()) <= 160:
            raise ActionPolicyError("invalid_display_name")
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 255:
            raise ActionPolicyError("invalid_idempotency_key")
        row = await session.scalar(select(AgentActionPolicy).where(AgentActionPolicy.agent_id == agent_id, AgentActionPolicy.idempotency_key == idempotency_key))
        if row:
            if row.display_name != display_name.strip():
                raise ActionPolicyError("idempotency_conflict", 409)
            return await self.project(session, row)
        row = AgentActionPolicy(id=uuid4(), agent_id=agent_id, display_name=display_name.strip(),
                                created_by=actor, state="draft", version=1, idempotency_key=idempotency_key)
        session.add(row)
        await session.flush()
        return await self.project(session, row)

    async def create_revision(self, session, policy_id, *, actor_user_id, expected_version, idempotency_key,
                              action_type, connection_id, authorization_mode, constraints, rate_limit,
                              dedupe_window_seconds, fallback_behavior="block", active_from=None, active_until=None):
        actor = await self.admin(session, actor_user_id)
        policy = await self._policy(session, policy_id, lock=True)
        definition = self.registry.require(action_type)
        try:
            constraints = definition.normalize_policy(constraints)
        except (ValueError, TypeError):
            raise ActionPolicyError("action_policy_constraint_failed") from None
        if authorization_mode not in {"human_approval", "bounded_auto"} or fallback_behavior not in {"block", "require_approval"}:
            raise ActionPolicyError("invalid_authorization_mode")
        if authorization_mode == "bounded_auto":
            await self._organization(session, bounded=True)
        if not isinstance(rate_limit, dict) or set(rate_limit) != {"window_seconds", "max_actions"}:
            raise ActionPolicyError("invalid_rate_limit")
        for key, maximum in (("window_seconds", 31536000), ("max_actions", 10000)):
            if type(rate_limit[key]) is not int or not 1 <= rate_limit[key] <= maximum:
                raise ActionPolicyError("invalid_rate_limit")
        if type(dedupe_window_seconds) is not int or not 0 <= dedupe_window_seconds <= 31536000:
            raise ActionPolicyError("invalid_dedupe_window")
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 255:
            raise ActionPolicyError("invalid_idempotency_key")
        connection = await session.get(ExternalConnection, uid(connection_id))
        if connection is None or connection.provider_key not in definition.connection_provider_keys:
            raise ActionPolicyError("integration_provider_mismatch")
        await self._human_owner(session, connection.owner_user_id)
        start, end = naive(active_from), naive(active_until)
        if start and end and end <= start:
            raise ActionPolicyError("invalid_date_window")
        previous = await self.current(session, policy.id)
        row = AgentActionPolicyRevision(id=uuid4(), policy_id=policy.id, version=(previous.version + 1 if previous else 1),
            created_by=actor, idempotency_key=idempotency_key, action_type=action_type, connection_id=connection.id,
            authorization_mode=authorization_mode, constraints_json=constraints, rate_limit_json=rate_limit,
            dedupe_window_seconds=dedupe_window_seconds, fallback_behavior=fallback_behavior, active_from=start, active_until=end,
            registry_schema_version=definition.schema_version, registry_adapter_key=definition.adapter_key,
            registry_adapter_version=definition.adapter_version)
        row.content_hash = sha256_json(policy_content(row))
        replay = await session.scalar(select(AgentActionPolicyRevision).where(AgentActionPolicyRevision.policy_id == policy.id, AgentActionPolicyRevision.idempotency_key == idempotency_key))
        if replay:
            if replay.content_hash != row.content_hash:
                raise ActionPolicyError("idempotency_conflict", 409)
            return await self.project(session, policy)
        if policy.version != expected_version:
            raise ActionPolicyError("stale_version", 409)
        if policy.state == "retired":
            raise ActionPolicyError("action_policy_retired", 409)
        session.add(row)
        policy.version += 1
        # Revising an active policy revokes old actions immediately; new
        # execution also requires current provider/credential readiness.
        await session.flush()
        return await self.project(session, policy)

    async def update_policy(self, session, policy_id, *, actor_user_id, expected_version, display_name=None, state=None):
        await self.admin(session, actor_user_id)
        policy = await self._policy(session, policy_id, lock=True)
        if policy.version != expected_version:
            raise ActionPolicyError("stale_version", 409)
        if policy.state == "retired":
            raise ActionPolicyError("action_policy_retired", 409)
        if state is not None:
            if state not in {"draft", "active", "paused", "retired"}:
                raise ActionPolicyError("invalid_policy_state")
            if state == "active":
                revision = await self.current(session, policy.id)
                if revision is None:
                    raise ActionPolicyError("action_policy_revision_missing", 409)
                await self._revision_integrity(session, revision, bounded=revision.authorization_mode == "bounded_auto")
                self.registry.require_executable(revision.action_type)
                await self._ready(session, revision)
            policy.state = state
        if display_name is not None:
            if not isinstance(display_name, str) or not 1 <= len(display_name.strip()) <= 160:
                raise ActionPolicyError("invalid_display_name")
            policy.display_name = display_name.strip()
        policy.version += 1
        await session.flush()
        return await self.project(session, policy)

    async def _human_owner(self, session, owner_id):
        owner = await session.get(User, owner_id)
        if owner is None or not owner.is_active or await session.get(Agent, owner_id) is not None:
            raise ActionPolicyError("human_account_owner_required", 403)
        return owner

    async def _revision_integrity(self, session, revision, *, bounded):
        if revision is None or sha256_json(policy_content(revision)) != revision.content_hash:
            raise ActionPolicyError("action_policy_integrity_failed", 409)
        definition = self.registry.require(revision.action_type)
        if (revision.registry_schema_version, revision.registry_adapter_key, revision.registry_adapter_version) != (definition.schema_version, definition.adapter_key, definition.adapter_version):
            raise ActionPolicyError("action_registry_changed", 409)
        try:
            valid_constraints = definition.normalize_policy(revision.constraints_json) == revision.constraints_json
        except (ValueError, TypeError):
            valid_constraints = False
        if not valid_constraints:
            raise ActionPolicyError("action_policy_constraint_failed", 409)
        if not window(revision):
            raise ActionPolicyError("action_policy_outside_window", 409)
        await self._organization(session, bounded=bounded)
        return definition

    async def _ready(self, session, revision):
        connection = await session.scalar(select(ExternalConnection).where(ExternalConnection.id == revision.connection_id).execution_options(populate_existing=True))
        definition = self.registry.require(revision.action_type)
        if connection is None or connection.provider_key not in definition.connection_provider_keys:
            raise ActionPolicyError("integration_provider_mismatch", 409)
        await self._human_owner(session, connection.owner_user_id)
        capability = "telephony.control" if revision.action_type == "telephony.transfer_call" else revision.action_type
        ready = await self._vault().readiness_for_connection(session, connection.id, owner_user_id=connection.owner_user_id,
            project_id=connection.project_id, required_capabilities=(capability,))
        if ready.get("ready") is not True or not ready.get("revision") or not ready.get("state_hash"):
            raise ActionPolicyError("integration_credential_unverified", 409)
        return connection, ready

    async def _provenance(self, session, *, policy, revision, agent_id, run_id, work_id,
                          rule_revision_id, event_id, position, proposing):
        agent_id, run_id = uid(agent_id), uid(run_id)
        run = await session.get(AgentRun, run_id, populate_existing=True)
        if agent_id != policy.agent_id or run is None or run.agent_id != agent_id or not run.agent_revision_id:
            raise ActionPolicyError("action_origin_invalid", 403)
        agent_revision = await session.get(AgentRevision, run.agent_revision_id)
        if agent_revision is None or agent_revision.agent_id != agent_id:
            raise ActionPolicyError("action_origin_invalid", 403)
        work = await session.get(AgentWorkItem, uid(work_id), populate_existing=True) if work_id else None
        if work_id and (work is None or run.work_item_id != work.id or
                        work.assigned_agent_id != agent_id or work.agent_revision_id != run.agent_revision_id):
            raise ActionPolicyError("action_origin_work_invalid", 403)
        if work and work.state == "running" and work.active_agent_run_id != run.id:
            raise ActionPolicyError("action_origin_work_invalid", 403)
        if work and work.state == "succeeded":
            latest_run = await session.scalar(select(AgentRun.id).where(AgentRun.work_item_id == work.id)
                .order_by(AgentRun.work_item_attempt.desc(), AgentRun.created_at.desc()).limit(1))
            if latest_run != run.id or run.work_item_attempt != work.attempt_count:
                raise ActionPolicyError("action_origin_work_invalid", 403)
        if proposing and (run.status != "running" or (work and (work.state != "running" or not work.lease_expires_at or work.lease_expires_at <= datetime.utcnow()))):
            raise ActionPolicyError("action_origin_not_running", 403)
        if not proposing and (run.status not in {"running", "succeeded", "completed"} or (work and work.state not in {"running", "succeeded"})):
            raise ActionPolicyError("action_origin_not_valid", 403)
        project_id = work.project_id if work else getattr(run, "project_id", None)
        space_id = work.space_id if work else None
        if rule_revision_id:
            rr = await session.get(AgentAutomationRuleRevision, uid(rule_revision_id))
            rule = await session.get(AgentAutomationRule, rr.rule_id) if rr else None
            event = await session.get(AgentAutomationEvent, uid(event_id))
            binding = await session.scalar(select(AgentAutomationRuleAction).where(AgentAutomationRuleAction.rule_revision_id == uid(rule_revision_id), AgentAutomationRuleAction.position == position))
            if not work or not rr or not rule or rule.state != "active" or rule.agent_id != agent_id or rr.agent_revision_id != run.agent_revision_id or not window(rr):
                raise ActionPolicyError("automation_rule_inactive", 409)
            latest = await session.scalar(select(func.max(AgentAutomationRuleRevision.version)).where(AgentAutomationRuleRevision.rule_id == rule.id))
            if latest != rr.version or not binding or binding.action_policy_revision_id != revision.id:
                raise ActionPolicyError("automation_rule_stale", 409)
            from .agent_automation_service import assert_rule_revision_integrity
            await assert_rule_revision_integrity(session, rr)
            if not event or work.source_type != "automation_event" or work.source_id != str(event.id) or work.source_revision != event.event_hash or work.intent_key != f"automation-rule:{rr.id}" or event.project_id != project_id or event.space_id != space_id:
                raise ActionPolicyError("action_event_origin_invalid", 403)
            from .agent_automation_runtime import AutomationRuleWorkSource
            from .agent_automation_service import AutomationError
            try:
                await AutomationRuleWorkSource(self.db_manager, config=self.config, authority_resolver=self.authority).load_source(
                    session, rule, rr, event=event)
            except AutomationError as exc:
                raise ActionPolicyError("action_source_unavailable", 403) from exc
        elif revision.action_type != "telephony.transfer_call" or work_id or event_id:
            raise ActionPolicyError("action_rule_origin_required", 403)
        for capability in self.registry.require(revision.action_type).required_capabilities:
            decision = await self.authority.resolve(agent_id=agent_id, revision_id=run.agent_revision_id,
                                                    project_id=project_id, space_id=space_id, required_capability=capability)
            if not decision.allowed:
                raise ActionPolicyError("action_authority_denied", 403)
        return run, work, project_id

    async def _phone_binding(self, session, revision, payload, run):
        if revision.action_type != "telephony.transfer_call":
            return
        from ..memory.models.telephony import TelephonyCall, TelephonyRoute
        call = await session.get(TelephonyCall, uid(payload["telephony_call_id"]))
        route = await session.get(TelephonyRoute, uid(revision.constraints_json["route_id"]))
        if not call or not route or call.route_id != route.id or route.connection_id != revision.connection_id or call.agent_run_id != run.id or call.agent_id != run.agent_id or call.agent_revision_id != run.agent_revision_id or call.conversation_session_id != run.session_id:
            raise ActionPolicyError("telephony_action_origin_invalid", 403)

    async def serialize(self, session, connection_id, policy_id):
        # One connection fence covers same-target cross-policy dedupe and
        # every policy's rate counter, on both PostgreSQL and SQLite.
        await session.execute(update(ExternalConnection).where(ExternalConnection.id == uid(connection_id)).values(version=ExternalConnection.version, updated_at=ExternalConnection.updated_at))
        return await self._policy(session, policy_id, lock=True)

    async def _duplicate(self, session, revision, key, *, excluding=None):
        submitted_at = select(func.max(ExternalActionAttempt.started_at)).where(
            ExternalActionAttempt.action_id == ExternalAction.id,
            ExternalActionAttempt.status.in_(["running", "uncertain", "succeeded"])).correlate(ExternalAction).scalar_subquery()
        conditions = [ExternalAction.connection_id == revision.connection_id, ExternalAction.dedupe_key == key,
                      or_(ExternalAction.status.in_(["proposed", "approved", "uncertain", "attempting", "running"]),
                          (func.coalesce(submitted_at, ExternalAction.created_at) >= datetime.utcnow() - timedelta(seconds=revision.dedupe_window_seconds)) &
                          (ExternalAction.status == "succeeded"))]
        if excluding:
            conditions.append(ExternalAction.id != excluding)
        return await session.scalar(select(ExternalAction).where(*conditions).order_by(ExternalAction.created_at).limit(1))

    async def _rate(self, session, revision, *, excluding=None):
        submitted_at = select(func.max(ExternalActionAttempt.started_at)).where(
            ExternalActionAttempt.action_id == ExternalAction.id,
            ExternalActionAttempt.status.in_(["running", "uncertain", "succeeded"])).correlate(ExternalAction).scalar_subquery()
        conditions = [ExternalAction.action_policy_id == revision.policy_id,
                      or_(ExternalAction.status.in_(["proposed", "approved", "attempting", "running", "uncertain"]),
                          (ExternalAction.status == "succeeded") &
                          (func.coalesce(submitted_at, ExternalAction.created_at) >= datetime.utcnow() - timedelta(seconds=revision.rate_limit_json["window_seconds"])))]
        if excluding:
            conditions.append(ExternalAction.id != excluding)
        count = await session.scalar(select(func.count()).select_from(ExternalAction).where(*conditions))
        if count >= revision.rate_limit_json["max_actions"]:
            raise ActionPolicyError("action_rate_limited", 409)

    async def propose_action(self, session, *, policy_revision_id, automation_rule_revision_id,
                              origin_agent_id, origin_agent_run_id, origin_work_item_id,
                              source_event_id, action_position, payload):
        if not isinstance(payload, dict) or "quote" in payload or type(action_position) is not int or action_position < 0:
            raise ActionPolicyError("action_policy_constraint_failed")
        revision = await session.get(AgentActionPolicyRevision, uid(policy_revision_id))
        if revision is None:
            raise ActionPolicyError("action_policy_not_found", 404)
        policy = await self.serialize(session, revision.connection_id, revision.policy_id)
        if policy.state != "active" or (await self.current(session, policy.id)).id != revision.id:
            raise ActionPolicyError("action_policy_stale", 409)
        definition = await self._revision_integrity(session, revision, bounded=revision.authorization_mode == "bounded_auto")
        try:
            normalized = definition.normalize_payload(payload, revision.constraints_json)
        except (ValueError, TypeError):
            raise ActionPolicyError("action_policy_constraint_failed") from None
        run, work, project_id = await self._provenance(session, policy=policy, revision=revision, agent_id=origin_agent_id,
            run_id=origin_agent_run_id, work_id=origin_work_item_id, rule_revision_id=automation_rule_revision_id,
            event_id=source_event_id, position=action_position, proposing=True)
        await self._phone_binding(session, revision, normalized, run)
        connection, readiness = await self._ready(session, revision)
        if connection.project_id != project_id:
            raise ActionPolicyError("connection_scope_mismatch", 403)
        key = (f"automation:{uid(automation_rule_revision_id)}:{uid(source_event_id)}:{action_position}" if automation_rule_revision_id
               else "realtime:" + sha256_json({"run": str(run.id), "policy": str(revision.id), "payload": normalized}))
        replay = await session.scalar(select(ExternalAction).where(ExternalAction.idempotency_key == key,
            ExternalAction.owner_user_id == connection.owner_user_id, ExternalAction.project_id == project_id))
        if replay:
            replay_input = {k: v for k, v in replay.payload_json.items() if k != "quote"}
            if payload_hash(replay_input) != payload_hash(normalized) or replay.action_policy_revision_id != revision.id:
                raise ActionPolicyError("idempotency_conflict", 409)
            return replay
        dedupe = definition.dedupe_key(normalized, {"connection_id": connection.id})
        duplicate = await self._duplicate(session, revision, dedupe)
        if duplicate:
            return duplicate
        await self._rate(session, revision)
        action = ExternalAction(id=uuid4(), owner_user_id=connection.owner_user_id, project_id=project_id,
            connection_id=connection.id, action_type=revision.action_type, idempotency_key=key,
            payload_json=normalized, payload_hash=payload_hash(normalized), artifact_hashes=[], status="proposed",
            created_by=None, origin_agent_id=policy.agent_id, origin_agent_run_id=run.id,
            origin_work_item_id=work.id if work else None, authorization_mode="bounded_policy" if revision.authorization_mode == "bounded_auto" else "human_approval",
            action_policy_id=policy.id, action_policy_revision_id=revision.id, action_policy_hash=revision.content_hash,
            automation_rule_revision_id=uid(automation_rule_revision_id) if automation_rule_revision_id else None,
            source_event_id=uid(source_event_id) if source_event_id else None, action_position=action_position,
            dedupe_key=dedupe, registry_schema_version=definition.schema_version, adapter_key=definition.adapter_key,
            adapter_version=definition.adapter_version, credential_state_hash=readiness["state_hash"], credential_revision=readiness["revision"],
            source_snapshot_hash=connection_hash(connection), execution_mode="provider", execution_key="action:" + sha256_json({"key":key,"connection":str(connection.id)}))
        session.add(action)
        await session.flush()
        record_action_event(session, action, "action.proposed")
        await session.flush()
        return action

    async def authorize_action(self, session, action, *, check_limits=True, allow_unapproved=False):
        if getattr(action, "legacy_evidence_incomplete", False):
            raise ActionPolicyError("legacy_provider_evidence_incomplete", 409)
        revision = await session.get(AgentActionPolicyRevision, action.action_policy_revision_id)
        if revision is None or revision.policy_id != action.action_policy_id or revision.content_hash != action.action_policy_hash:
            raise ActionPolicyError("action_policy_integrity_failed", 409)
        policy = await self._policy(session, revision.policy_id)
        if policy.state != "active":
            raise ActionPolicyError("action_policy_inactive", 409)
        bounded = action.authorization_mode == "bounded_policy"
        if action.authorization_mode not in {"human_approval", "bounded_policy"}:
            raise ActionPolicyError("invalid_authorization_mode", 409)
        if bounded and ((await self.current(session, policy.id)).id != revision.id or revision.authorization_mode != "bounded_auto"):
            raise ActionPolicyError("action_policy_stale", 409)
        definition = await self._revision_integrity(session, revision, bounded=bounded)
        self.registry.require_executable(action.action_type)
        if action.action_type != revision.action_type or action.action_policy_hash != revision.content_hash:
            raise ActionPolicyError("action_policy_integrity_failed", 409)
        try:
            valid_payload = action.payload_hash == payload_hash(action.payload_json) and definition.normalize_payload(action.payload_json, revision.constraints_json) == action.payload_json
        except (ValueError, TypeError):
            valid_payload = False
        if not valid_payload:
            raise ActionPolicyError("action_payload_integrity_failed", 409)
        if action.artifact_hashes or (action.registry_schema_version, action.adapter_key, action.adapter_version) != (definition.schema_version, definition.adapter_key, definition.adapter_version):
            raise ActionPolicyError("action_registry_changed", 409)
        if not bounded and not allow_unapproved:
            approval = await session.scalar(select(ExternalActionApproval).where(ExternalActionApproval.action_id == action.id).order_by(ExternalActionApproval.created_at.desc()).limit(1))
            if not approval or approval.decision != "approved" or approval.action_version != action.action_version or approval.payload_hash != action.payload_hash or approval.artifact_hashes != action.artifact_hashes or not approval.decided_by:
                raise ActionPolicyError("human_approval_required", 409)
            await self._human_owner(session, approval.decided_by)
            if action.action_type == "procurement.place_order" and "quote" not in action.payload_json:
                raise ActionPolicyError("human_approval_quote_required", 409)
        run, work, project_id = await self._provenance(session, policy=policy, revision=revision,
            agent_id=action.origin_agent_id, run_id=action.origin_agent_run_id, work_id=action.origin_work_item_id,
            rule_revision_id=action.automation_rule_revision_id, event_id=action.source_event_id,
            position=action.action_position, proposing=False)
        await self._phone_binding(session, revision, action.payload_json, run)
        connection, readiness = await self._ready(session, revision)
        if action.connection_id != connection.id or action.owner_user_id != connection.owner_user_id or project_id != connection.project_id or action.project_id != project_id or action.source_snapshot_hash != connection_hash(connection):
            raise ActionPolicyError("connection_binding_changed", 409)
        if action.credential_revision != readiness["revision"] or action.credential_state_hash != readiness["state_hash"]:
            raise ActionPolicyError("integration_credential_changed", 409)
        if action.dedupe_key != definition.dedupe_key(action.payload_json, {"connection_id": connection.id}):
            raise ActionPolicyError("action_dedupe_integrity_failed", 409)
        if check_limits:
            if await self._duplicate(session, revision, action.dedupe_key, excluding=action.id):
                raise ActionPolicyError("action_dedupe_suppressed", 409)
            await self._rate(session, revision, excluding=action.id)
        return revision, definition, connection, readiness, run, work

    authorize_bounded_action = authorize_action
