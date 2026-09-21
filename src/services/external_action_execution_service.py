"""Server-owned execution. A committed attempt always precedes submission."""
from __future__ import annotations

import asyncio
import re
from datetime import datetime
from uuid import uuid4

from sqlalchemy import String, and_, cast, exists, func, or_, select, update

from ..memory.models import AgentRun, AgentWorkItem
from ..memory.models.operations import ExternalAction, ExternalActionAttempt, ExternalActionReceipt, OperationEvent, sanitize_source_url
from .agent_action_policy_service import uid, naive, record_action_event
from .agent_work_runtime import ExecutionOutcome
from .integration_action_registry import ActionPolicyError, DefinitelyNotSent, IntegrationActionResult
from .procurement_action import validate_quote


def action_source_revision(action):
    from .agent_identity_service import sha256_json
    return sha256_json({"action_version": action.action_version, "payload_hash": action.payload_hash,
        "authorization_mode": action.authorization_mode, "policy": str(action.action_policy_revision_id),
        "policy_hash": action.action_policy_hash, "connection": action.source_snapshot_hash,
        "credential": action.credential_state_hash, "adapter": action.adapter_key, "adapter_version": action.adapter_version})


def safe_ref(value, *, limit=255):
    return value if isinstance(value, str) and len(value) <= limit and re.fullmatch(r"[A-Za-z0-9_.:/-]+", value) else None


async def validate_service_attempt_reconciliation(session, action, attempt):
    """Validate historical evidence without requiring a revoked policy to run.

    Reconciliation is an authorized human observation, never another provider
    submission. Current execution availability must not trap historical rows.
    """
    from ..memory.models.agent_automation import AgentActionPolicyRevision
    from .agent_action_policy_service import policy_content
    from .agent_identity_service import sha256_json
    from .operations_service import OperationsConflictError, payload_hash
    from .integration_action_registry import IntegrationActionRegistry
    definition = IntegrationActionRegistry().get(action.action_type)
    revision = await session.get(AgentActionPolicyRevision, action.action_policy_revision_id)
    intent = await session.scalar(select(OperationEvent).where(OperationEvent.entity_id == action.id,
        OperationEvent.event_type == "action.attempt.started",
        OperationEvent.payload_json["attempt_id"].as_string() == str(attempt.id)).limit(1))
    evidence = intent.payload_json if intent else {}
    valid = (definition and definition.category != "legacy" and revision
             and not getattr(action, "legacy_evidence_incomplete", False)
             and not getattr(attempt, "legacy_evidence_incomplete", False))
    if valid:
        try:
            valid = (revision.policy_id == action.action_policy_id and revision.connection_id == action.connection_id
                and revision.content_hash == action.action_policy_hash == sha256_json(policy_content(revision))
                and revision.action_type == action.action_type and action.payload_hash == payload_hash(action.payload_json)
                and definition.normalize_payload(action.payload_json, revision.constraints_json) == action.payload_json
                and evidence.get("connection_id", str(revision.connection_id)) == str(action.connection_id)
                and evidence.get("connection_binding_hash", action.source_snapshot_hash) == action.source_snapshot_hash
                and evidence.get("payload_hash") == action.payload_hash
                and evidence.get("action_policy_hash") == action.action_policy_hash
                and evidence.get("action_version") == action.action_version
                and attempt.action_id == action.id and attempt.owner_user_id == action.owner_user_id
                and attempt.action_version == action.action_version and attempt.executor_type == "service"
                and attempt.execution_mode == action.execution_mode == "provider"
                and attempt.provider_key in definition.connection_provider_keys
                and attempt.provider_adapter_key == action.adapter_key == revision.registry_adapter_key
                and attempt.provider_adapter_version == action.adapter_version == revision.registry_adapter_version
                and attempt.registry_schema_version == action.registry_schema_version == definition.schema_version
                and attempt.credential_state_hash == action.credential_state_hash
                and attempt.execution_key == action.execution_key)
        except (ValueError, TypeError):
            valid = False
    if not valid:
        raise OperationsConflictError("service attempt integrity changed before reconcile")


async def recover_interrupted_action(session, action, *, work_item, now=None):
    """Classify a committed submission whose executing lease no longer lives.

    This repairs Action/Attempt evidence before any current execution gate.
    It neither grants authority nor mutates/claims/settles the WorkItem queue;
    the caller owns the transaction and the coordinator owns work recovery.
    """
    if action.status not in {"attempting", "uncertain"} or getattr(action, "legacy_evidence_incomplete", False):
        return False
    now = now or datetime.utcnow()
    # Serialize with provider settlement and other recovery callers, including
    # SQLite where FOR UPDATE alone does not establish a write fence.
    await session.execute(update(ExternalAction).where(ExternalAction.id == action.id).values(
        version=ExternalAction.version, updated_at=ExternalAction.updated_at))
    await session.refresh(action)
    if action.status not in {"attempting", "uncertain"}:
        return False
    attempt = await session.scalar(select(ExternalActionAttempt).where(ExternalActionAttempt.action_id == action.id,
        ExternalActionAttempt.status.in_(["running", "uncertain"]), ExternalActionAttempt.executor_type == "service")
        .order_by(ExternalActionAttempt.started_at.desc()).limit(1).execution_options(populate_existing=True))
    if not attempt or getattr(attempt, "legacy_evidence_incomplete", False):
        return False
    intent = await session.scalar(select(OperationEvent).where(OperationEvent.entity_id == action.id,
        OperationEvent.event_type == "action.attempt.started",
        OperationEvent.payload_json["attempt_id"].as_string() == str(attempt.id)).limit(1))
    evidence = intent.payload_json if intent else {}
    if (work_item.source_type != "external_action" or work_item.source_id != str(action.id)
            or work_item.source_revision != action_source_revision(action)
            or work_item.intent_key != "external-action.execute"):
        return False
    execution_work_id = evidence.get("execution_work_item_id")
    if execution_work_id:
        if str(work_item.id) != execution_work_id:
            return False
        work = await session.get(AgentWorkItem, uid(execution_work_id), populate_existing=True)
        run_id = evidence.get("execution_run_id")
        run = await session.get(AgentRun, uid(run_id), populate_existing=True) if run_id else None
        if not run or run.work_item_id != work_item.id or run.agent_id != work_item.assigned_agent_id or run.agent_revision_id != work_item.agent_revision_id:
            raise RuntimeError("action_recovery_origin_invalid")
        live = (work and run and work.source_type == "external_action" and work.source_id == str(action.id)
            and work.state == "running" and work.lease_expires_at and work.lease_expires_at > now
            and work.active_agent_run_id == run.id and run.work_item_id == work.id
            and run.work_item_attempt == work.attempt_count and run.status == "running")
        if live:
            return False
    elif action.origin_work_item_id is not None:
        # Older attempts lack explicit execution IDs. Conservatively retain
        # a live matching claim; otherwise their committed submit is unknown.
        live = work_item.state == "running" and work_item.lease_expires_at and work_item.lease_expires_at > now
        if live:
            return False
    else:
        # Realtime calls have their own persisted interruption recovery and
        # no queue lease. Do not mistake a live REFER for abandoned work.
        return False
    await validate_service_attempt_reconciliation(session, action, attempt)
    if action.status == "uncertain" and attempt.status == "uncertain":
        return True
    attempt.status = "uncertain"
    attempt.finished_at = now
    attempt.error_message = "provider_submission_interrupted"
    action.status = "uncertain"
    action.version += 1
    record_action_event(session, action, "action.attempt.uncertain", attempt_id=attempt.id)
    await session.flush()
    return True


class ExternalActionExecutionService:
    def __init__(self, *, policy_service):
        self.policies = policy_service

    async def _claim(self, session, action, claim, agent_run_id):
        if claim is None:
            if action.action_type != "telephony.transfer_call" or action.origin_work_item_id is not None or uid(agent_run_id) != action.origin_agent_run_id:
                raise ActionPolicyError("action_execution_claim_required", 403)
            return
        row = await session.get(AgentWorkItem, uid(claim.work_item_id), populate_existing=True)
        run = await session.get(AgentRun, uid(agent_run_id), populate_existing=True)
        if not row or not run or row.state != "running" or row.lease_token != claim.lease_token or row.lease_owner != claim.lease_owner or not row.lease_expires_at or row.lease_expires_at <= datetime.utcnow():
            raise ActionPolicyError("action_execution_lease_lost", 409)
        if row.source_type != "external_action" or row.source_id != str(action.id) or row.source_revision != action_source_revision(action) or row.execution_adapter != "external_action" or row.active_agent_run_id != run.id or run.work_item_id != row.id or run.agent_id != action.origin_agent_id or run.agent_revision_id != row.agent_revision_id:
            raise ActionPolicyError("action_execution_origin_invalid", 403)
        origin = await session.get(AgentRun, action.origin_agent_run_id)
        if not origin or origin.agent_revision_id != run.agent_revision_id:
            raise ActionPolicyError("action_execution_revision_invalid", 403)

    @staticmethod
    def _outcome(action, classification, code=None):
        return ExecutionOutcome(classification=classification, error_code=code,
                                domain_ref=str(action.id), domain_status=action.status)

    async def execute_action(self, session, action_id, *, claim=None, agent_run_id=None):
        action = await session.get(ExternalAction, uid(action_id))
        if action is None:
            return ExecutionOutcome(classification="blocked", error_code="action_not_found")
        if getattr(action, "legacy_evidence_incomplete", False):
            return ExecutionOutcome(classification="blocked", error_code="legacy_provider_evidence_incomplete")
        try:
            await self.policies.serialize(session, action.connection_id, action.action_policy_id)
            await session.refresh(action)
            await self._claim(session, action, claim, agent_run_id)
            if action.status == "succeeded":
                receipt = await session.scalar(select(ExternalActionReceipt).where(ExternalActionReceipt.action_id == action.id))
                return self._outcome(action, "succeeded" if receipt else "uncertain")
            if action.status == "uncertain":
                return self._outcome(action, "uncertain", "provider_result_uncertain")
            old = await session.scalar(select(ExternalActionAttempt).where(ExternalActionAttempt.action_id == action.id,
                ExternalActionAttempt.status.in_(["running", "uncertain", "succeeded"])).order_by(ExternalActionAttempt.started_at.desc()).limit(1))
            if old:
                if getattr(old, "legacy_evidence_incomplete", False):
                    raise ActionPolicyError("legacy_provider_evidence_incomplete", 409)
                # Restart or overlapping invocation cannot prove whether the
                # committed request was submitted. Never issue it again.
                action.status = "uncertain"
                if old.status == "running":
                    old.status = "uncertain"
                    old.finished_at = datetime.utcnow()
                await session.commit()
                return self._outcome(action, "uncertain", "provider_result_uncertain")
            if action.status not in {"proposed", "approved", "failed"}:
                raise ActionPolicyError("action_not_executable", 409)
            if action.status == "failed":
                previous = await session.scalar(select(ExternalActionAttempt).where(ExternalActionAttempt.action_id == action.id).order_by(ExternalActionAttempt.started_at.desc()).limit(1))
                if not previous or previous.error_message != "provider_definitely_not_sent" or action.action_type == "telephony.transfer_call":
                    raise ActionPolicyError("action_not_retryable", 409)
            revision, definition, connection, ready, _, _ = await self.policies.authorize_action(session, action, allow_unapproved=True)
            capability = "telephony.control" if action.action_type == "telephony.transfer_call" else action.action_type
            credential = await self.policies._vault().resolve_for_execution(session, connection.id,
                owner_user_id=connection.owner_user_id, project_id=connection.project_id,
                expected_revision=action.credential_revision, expected_state_hash=action.credential_state_hash,
                required_capabilities=(capability,))
            adapter = definition.adapter_factory()
            if (adapter.adapter_key, adapter.adapter_version) != (action.adapter_key, action.adapter_version):
                raise ActionPolicyError("action_registry_changed", 409)
            binding = {"id": str(connection.id), "provider_key": connection.provider_key,
                       "remote_account_ref": connection.remote_account_ref,
                       "policy_constraints": revision.constraints_json}
            payload = {k: v for k, v in action.payload_json.items() if k != "quote"}
            # Quote lookup is read-only. Release DB locks around network then
            # reacquire/recheck every mutable gate before persisting Attempt.
            await session.commit()
            quote = None
            if action.action_type == "procurement.place_order":
                try:
                    quote = validate_quote(await adapter.prepare(payload=payload, connection=binding, credential=credential),
                                           payload, revision.constraints_json, enforce_ceiling=False)
                except ValueError:
                    return self._outcome(action, "blocked", "procurement_quote_invalid")
                except Exception:
                    return self._outcome(action, "transient", "procurement_quote_unavailable")
            await self.policies.serialize(session, action.connection_id, action.action_policy_id)
            await session.refresh(action)
            await self._claim(session, action, claim, agent_run_id)
            await self.policies.authorize_action(session, action, allow_unapproved=True)
            # The first transaction may have raced another request during its
            # quote lookup; only one caller may install the attempt fence.
            count = await session.scalar(select(ExternalActionAttempt.id).where(ExternalActionAttempt.action_id == action.id,
                ExternalActionAttempt.status.in_(["running", "uncertain", "succeeded"])).limit(1))
            if count or action.status not in {"proposed", "approved", "failed"}:
                raise ActionPolicyError("action_attempt_already_exists", 409)
            if quote is not None:
                from .operations_service import payload_hash
                validate_quote(quote, payload, revision.constraints_json, enforce_ceiling=False)
                snapshot = quote.model_dump(mode="json")
                over_budget = quote.total_minor > revision.constraints_json["max_order_total_minor"]
                if over_budget and action.authorization_mode == "bounded_policy" and revision.fallback_behavior == "block":
                    await session.rollback()
                    return ExecutionOutcome(classification="blocked", error_code="procurement_quote_exceeds_policy", domain_ref=str(action_id))
                needs_human = action.authorization_mode == "human_approval" or over_budget
                approved_quote = action.payload_json.get("quote") or {}
                material_quote = {key: value for key, value in snapshot.items() if key != "observed_at"}
                approved_material = {key: value for key, value in approved_quote.items() if key != "observed_at"}
                if needs_human and approved_material != material_quote:
                    if claim is not None:
                        work_row = await session.get(AgentWorkItem, uid(claim.work_item_id))
                        work_row.metadata_json = {**(work_row.metadata_json or {}),
                            "approval_transition_from": claim.source_revision, "approval_action_id": str(action.id)}
                    action.payload_json = {**payload, "quote": snapshot}
                    action.payload_hash = payload_hash(action.payload_json)
                    action.action_version += 1
                    action.version += 1
                    action.authorization_mode = "human_approval"
                    action.status = "proposed"
                    record_action_event(session, action, "action.quote_approval_required")
                    await session.commit()
                    return self._outcome(action, "awaiting_approval", "human_approval_required")
            await self.policies.authorize_action(session, action)
            attempt = ExternalActionAttempt(id=uuid4(), action_id=action.id, owner_user_id=action.owner_user_id,
                action_version=action.action_version, executor_type="service", execution_mode="provider",
                provider_key=connection.provider_key, provider_adapter_key=action.adapter_key,
                provider_adapter_version=action.adapter_version, registry_schema_version=action.registry_schema_version,
                credential_state_hash=action.credential_state_hash, execution_key=action.execution_key,
                status="running", created_by=None, evidence_artifact_ids=[])
            session.add(attempt)
            action.status = "attempting"
            action.version += 1
            record_action_event(session, action, "action.attempt.started", attempt_id=attempt.id,
                execution_work_item_id=claim.work_item_id if claim else None, execution_run_id=agent_run_id)
            await session.commit()
        except ActionPolicyError as exc:
            await session.rollback()
            return ExecutionOutcome(classification="awaiting_approval" if exc.code == "human_approval_required" else "blocked",
                                    error_code=exc.code, domain_ref=str(action_id))

        cancelled = False
        try:
            result = await adapter.execute(action_id=str(action.id), attempt_id=str(attempt.id),
                execution_key=attempt.execution_key, payload=payload, connection=binding, credential=credential, quote=quote)
            if not isinstance(result, IntegrationActionResult):
                result = IntegrationActionResult("uncertain")
            # Generic transient reports are not proof of no submission.
            if result.status not in {"succeeded", "failed", "uncertain"}:
                result = IntegrationActionResult("uncertain")
        except DefinitelyNotSent:
            result = IntegrationActionResult("failed", safe_error_code="provider_definitely_not_sent")
        except asyncio.CancelledError:
            cancelled = True
            result = IntegrationActionResult("uncertain")
        except Exception:
            result = IntegrationActionResult("uncertain")
        finally:
            credential = None

        await self.policies.serialize(session, action.connection_id, action.action_policy_id)
        await session.refresh(action)
        await session.refresh(attempt)
        if attempt.status != "running" or action.status != "attempting" or attempt.action_version != action.action_version:
            await session.rollback()
            return ExecutionOutcome(classification="uncertain", error_code="action_attempt_fence_changed", domain_ref=str(action_id))
        strong = (result.status == "succeeded" and isinstance(result.observed_at, datetime)
                  and safe_ref(result.remote_status, limit=64)
                  and (safe_ref(result.provider_receipt_ref) or safe_ref(result.remote_resource_id)))
        if result.status == "succeeded" and not strong:
            result = IntegrationActionResult("uncertain")
        attempt.status = action.status = result.status
        attempt.finished_at = datetime.utcnow()
        attempt.provider_attempt_ref = safe_ref(result.provider_attempt_ref)
        attempt.error_message = "provider_definitely_not_sent" if result.safe_error_code == "provider_definitely_not_sent" else None
        action.version += 1
        receipt = None
        if strong:
            receipt = ExternalActionReceipt(id=uuid4(), action_id=action.id, attempt_id=attempt.id,
                owner_user_id=action.owner_user_id, action_version=action.action_version,
                provider_receipt_ref=safe_ref(result.provider_receipt_ref), remote_resource_id=safe_ref(result.remote_resource_id),
                remote_status=safe_ref(result.remote_status, limit=64), remote_url=sanitize_source_url(result.remote_url),
                provider_observed_at=naive(result.observed_at), confirmation_level="provider_confirmed", evidence_artifact_ids=[])
            session.add(receipt)
        record_action_event(session, action, "action.attempt." + result.status, attempt_id=attempt.id,
                            receipt_id=receipt.id if receipt else None)
        await session.commit()
        classification = ("transient" if attempt.error_message == "provider_definitely_not_sent" and action.action_type != "telephony.transfer_call"
                          else "permanent" if result.status == "failed" else result.status)
        if cancelled:
            raise asyncio.CancelledError
        return self._outcome(action, classification, "provider_result_uncertain" if result.status == "uncertain" else attempt.error_message)


class ExternalActionExecutionAdapter:
    adapter_key = "external_action"

    def __init__(self, *, execution_service):
        self.service = execution_service

    def required_capabilities(self, claim):
        return claim.required_capabilities

    def recovery_condition(self):
        # Match the committed submission's execution work BEFORE the
        # coordinator applies its page limit. An older quote-version work
        # item for the same Action is not an execution-recovery candidate.
        legacy_work = AgentWorkItem.__table__.alias("legacy_action_recovery_work")
        legacy_work_count = select(func.count()).select_from(legacy_work).where(
            legacy_work.c.source_type == "external_action",
            func.replace(legacy_work.c.source_id, "-", "") == func.replace(cast(ExternalAction.id, String), "-", ""),
        ).correlate(ExternalAction).scalar_subquery()
        execution_work_id = OperationEvent.payload_json["execution_work_item_id"].as_string()
        return exists(select(ExternalActionAttempt.id).join(ExternalAction,
            ExternalAction.id == ExternalActionAttempt.action_id).join(OperationEvent, and_(
                OperationEvent.entity_id == ExternalAction.id,
                OperationEvent.entity_type == "action",
                OperationEvent.event_type == "action.attempt.started",
                func.replace(OperationEvent.payload_json["attempt_id"].as_string(), "-", "") ==
                    func.replace(cast(ExternalActionAttempt.id, String), "-", ""),
            )).where(
                func.replace(cast(ExternalAction.id, String), "-", "") == func.replace(AgentWorkItem.source_id, "-", ""),
                AgentWorkItem.source_type == "external_action",
                AgentWorkItem.intent_key == "external-action.execute",
                ExternalActionAttempt.action_version == ExternalAction.action_version,
                OperationEvent.payload_json["action_version"].as_integer() == ExternalActionAttempt.action_version,
                OperationEvent.payload_json["payload_hash"].as_string() == ExternalAction.payload_hash,
                or_(
                    func.replace(execution_work_id, "-", "") == func.replace(cast(AgentWorkItem.id, String), "-", ""),
                    # Old start audits may omit execution IDs. Only a single
                    # durable work identity is unambiguous. Missing audit or
                    # multiple source versions never occupy background pages;
                    # do not guess which work submitted a historical request.
                    and_(execution_work_id.is_(None), legacy_work_count == 1),
                ),
                ExternalAction.status.in_(["attempting", "uncertain"]),
                ExternalAction.legacy_evidence_incomplete.is_(False),
                ExternalActionAttempt.status.in_(["running", "uncertain"]),
                ExternalActionAttempt.legacy_evidence_incomplete.is_(False),
                ExternalActionAttempt.executor_type == "service"))

    async def recover_stale(self, session, work_item, *, coordinator, now):
        action = await session.get(ExternalAction, uid(work_item.source_id), populate_existing=True)
        if not action or not await recover_interrupted_action(session, action, work_item=work_item, now=now):
            return False
        await coordinator._transition_locked(session, work_item, "uncertain", token=work_item.lease_token,
            error_code="provider_submission_interrupted", error_message="Committed provider submission requires reconciliation")
        work_item.not_before = work_item.next_attempt_at = None
        if work_item.budget_reservation_json:
            work_item.budget_reservation_json = coordinator._settle_budget(dict(work_item.budget_reservation_json),
                ExecutionOutcome(classification="uncertain"), state="uncertain")
        return True

    async def execute(self, claim, *, coordinator, session=None, actor=None, run=None, run_id=None):
        return await self.service.execute_action(session, claim.source_id, claim=claim, agent_run_id=run_id)
