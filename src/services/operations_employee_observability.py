"""Private read-only employee projections. Never serialize configuration JSON."""

from datetime import datetime
import re
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError

from ..memory.models import (
    AgentActionPolicy, AgentActionPolicyRevision, AgentAutomationEvent,
    AgentAutomationRule, AgentRun, AgentWorkItem, ExternalAction,
    ExternalActionApproval, ExternalActionAttempt, ExternalActionReceipt,
    ExternalConnection, IntegrationCredential, TelephonyCall, TelephonyRoute,
)


async def optional_rows(session, statement):
    """A missing optional table must not expire previously loaded ORM rows.

    None means unavailable, whereas [] means the ledger was read and is empty.
    The savepoint also clears PostgreSQL's failed-transaction state locally.
    """
    try:
        async with session.begin_nested():
            return list((await session.execute(statement)).scalars().all())
    except DBAPIError:
        return None


def identity(value):
    try:
        return str(UUID(str(value))) if value is not None else None
    except (ValueError, TypeError, AttributeError):
        return None


def fields(row, *names):
    """Closed metadata only; IDs are parsed, not trusted opaque text."""
    result = {}
    for name in names:
        value = getattr(row, name, None)
        if name == "id" or name.endswith("_id"):
            value = identity(value)
        elif isinstance(value, datetime):
            value = value.isoformat()
        elif isinstance(value, str) and not re.fullmatch(r"[A-Za-z][A-Za-z_.:-]{0,79}", value):
            # Only closed lifecycle/event codes belong in these DTOs. Even a
            # corrupted code column must not become a plaintext/PII channel.
            value = None
        if value is not None:
            result[name] = value
    return result


def scoped(statement, column, projects):
    return statement if projects is None else statement.where(column.in_(projects or {UUID(int=0)}))


def work_metadata(row):
    return fields(row, "id", "assigned_agent_id", "agent_revision_id", "project_id",
                  "space_id", "state", "outcome_classification", "created_at", "updated_at",
                  "completed_at", "attempt_count")


def call_metadata(row, *, admin):
    result = fields(row, "id", "agent_id", "agent_revision_id", "agent_run_id", "state",
                    "received_at", "accepted_at", "ended_at", "created_at", "updated_at")
    if admin:
        result.update(fields(row, "route_id", "route_version"))
    # No provider IDs, destination keys, raw/masked stored numbers or transcript.
    return result


async def employee_summary(session, agent_id, *, admin, projects, registry):
    result = {
        "management_visible": admin,
        "rule_count": None, "active_rule_count": None, "action_policy_count": None,
        "integration_readiness": None, "last_automation_trigger": None,
        "last_automation_result": None, "uncertain_action_count": None,
        "phone": {"route_count": None, "routes": [], "recent_calls": []},
    }
    if admin:
        rules = await optional_rows(session, select(func.count()).select_from(AgentAutomationRule).where(AgentAutomationRule.agent_id == agent_id))
        active = await optional_rows(session, select(func.count()).select_from(AgentAutomationRule).where(AgentAutomationRule.agent_id == agent_id, AgentAutomationRule.state == "active"))
        policies = await optional_rows(session, select(func.count()).select_from(AgentActionPolicy).where(AgentActionPolicy.agent_id == agent_id))
        result.update(rule_count=rules[0] if rules else None,
                      active_rule_count=active[0] if active else None,
                      action_policy_count=policies[0] if policies else None)
        latest = select(func.max(AgentActionPolicyRevision.version)).where(
            AgentActionPolicyRevision.policy_id == AgentActionPolicy.id
        ).correlate(AgentActionPolicy).scalar_subquery()
        revisions = await optional_rows(session, select(AgentActionPolicyRevision).join(
            AgentActionPolicy, AgentActionPolicy.id == AgentActionPolicyRevision.policy_id
        ).where(AgentActionPolicy.agent_id == agent_id, AgentActionPolicyRevision.version == latest).limit(501))
        result["integration_readiness"] = await integration_readiness(session, revisions, registry)
        count = await optional_rows(session, select(func.count()).select_from(TelephonyRoute).where(TelephonyRoute.agent_id == agent_id))
        result["phone"]["route_count"] = count[0] if count else None
        routes = await optional_rows(session, select(TelephonyRoute).where(TelephonyRoute.agent_id == agent_id).order_by(TelephonyRoute.updated_at.desc()).limit(10))
        result["phone"]["routes"] = [dict(fields(row, "id", "agent_revision_id", "state", "version", "updated_at"),
                                                   readiness="unknown", executable=False)
                                             for row in routes or []]

    uncertain = await optional_rows(session, scoped(select(func.count()).select_from(ExternalAction).where(
        ExternalAction.origin_agent_id == agent_id, ExternalAction.status == "uncertain"
    ), ExternalAction.project_id, projects))
    result["uncertain_action_count"] = uncertain[0] if uncertain else None
    work_stmt = scoped(select(AgentWorkItem).where(AgentWorkItem.assigned_agent_id == agent_id,
                       AgentWorkItem.source_type == "automation_event"), AgentWorkItem.project_id, projects)
    # Join source facts before LIMIT so unrelated/deleted/inaccessible events
    # cannot masquerade as this employee's most recent trigger.
    event_match = func.replace(AgentWorkItem.source_id, "-", "") == func.replace(
        AgentAutomationEvent.id.cast(AgentWorkItem.source_id.type), "-", "")
    recent = await optional_rows(session, scoped(work_stmt.join(AgentAutomationEvent, event_match).where(
        AgentAutomationEvent.project_id == AgentWorkItem.project_id
    ), AgentAutomationEvent.project_id, projects).order_by(AgentAutomationEvent.occurred_at.desc()).limit(1))
    if recent:
        work = recent[0]
        event = await optional_rows(session, select(AgentAutomationEvent).where(AgentAutomationEvent.id == UUID(identity(work.source_id))))
        if event:
            trigger = fields(event[0], "event_type", "source_type", "source_id", "occurred_at")
            trigger.update(event_id=identity(event[0].id), work_item_id=identity(work.id))
            result["last_automation_trigger"] = trigger
    latest_result = await optional_rows(session, work_stmt.order_by(AgentWorkItem.updated_at.desc()).limit(1))
    if latest_result:
        work = latest_result[0]
        status = fields(work, "state", "outcome_classification", "updated_at", "completed_at")
        status["work_item_id"] = identity(work.id)
        outcome = work.result_summary_json if isinstance(work.result_summary_json, dict) else {}
        if outcome.get("reason_code") in {"not_matched", "matched", "action_dedupe_suppressed"}:
            status["reason_code"] = outcome["reason_code"]
        for name in ("matched", "suppressed"):
            if type(outcome.get(name)) is bool:
                status[name] = outcome[name]
        runs = await optional_rows(session, scoped(select(AgentRun).where(AgentRun.work_item_id == work.id,
            AgentRun.agent_id == agent_id), AgentRun.project_id, projects).order_by(AgentRun.created_at.desc()).limit(1))
        if runs:
            status["run_id"] = identity(runs[0].id)
        result["last_automation_result"] = status
    calls_stmt = select(TelephonyCall).where(TelephonyCall.agent_id == agent_id)
    if projects is not None:
        calls_stmt = calls_stmt.join(AgentRun, AgentRun.id == TelephonyCall.agent_run_id).where(
            AgentRun.agent_id == agent_id, AgentRun.project_id.in_(projects or {UUID(int=0)}))
    calls = await optional_rows(session, calls_stmt.order_by(TelephonyCall.received_at.desc()).limit(10))
    result["phone"]["recent_calls"] = [call_metadata(row, admin=admin) for row in calls or []]
    return result


async def integration_readiness(session, revisions, registry):
    result = {"status": "unknown", "executable": False, "connections": []}
    if revisions is None:
        return result
    if not revisions:
        result["status"] = "not_configured"
        return result
    # Inspect only fixed metadata. Never decrypt or invoke adapters/verifiers.
    ids = {row.connection_id for row in revisions[:500] if row.connection_id}
    connections = await optional_rows(session, select(ExternalConnection).where(ExternalConnection.id.in_(ids))) if ids else []
    credentials = await optional_rows(session, select(IntegrationCredential).where(IntegrationCredential.connection_id.in_(ids))) if ids else []
    by_connection = {row.id: row for row in connections or []}
    by_credential = {row.connection_id: row for row in credentials or []}
    states = []
    for revision in revisions[:500]:
        connection = by_connection.get(revision.connection_id)
        credential = by_credential.get(revision.connection_id)
        definition = registry.get(revision.action_type) if registry is not None else None
        provider_state = definition.status if definition else "unknown"
        credential_state = credential.status if credential else ("unknown" if credentials is None else "missing")
        adapter_available = bool(definition and definition.status == "automatable" and definition.adapter_factory)
        if definition and not adapter_available:
            status = "unavailable"
        elif connections is None or credentials is None or definition is None:
            status = "unknown"
        elif (connection is None or credential is None or credential_state != "verified"
              or connection.auth_status != "verified"
              or connection.provider_key not in definition.connection_provider_keys
              or credential.project_id != connection.project_id
              or credential.owner_user_id != connection.owner_user_id):
            status = "blocked"
        else:
            # Physical adapter registered + stored verification is still not
            # current authority/vault/policy validation at execution time.
            status = "ready"
        states.append(status)
        result["connections"].append({
            "policy_revision_id": identity(revision.id), "connection_id": identity(revision.connection_id),
            "provider_status": provider_state, "credential_status": credential_state,
            "adapter_available": adapter_available, "status": status, "executable": False,
            "execution_check_required": True,
        })
    result["status"] = next((value for value in ("unavailable", "blocked", "unknown") if value in states), "ready")
    if len(revisions) > 500:
        result.update(status="unknown", truncated=True)
    return result


async def causal_activity(session, work_rows, actions, *, projects, admin, limit):
    """Explicit links, without embedding rule/policy/connection/provider data."""
    rows = []
    work_ids = {row.id for row in work_rows}
    runs = await optional_rows(session, scoped(select(AgentRun).where(AgentRun.work_item_id.in_(work_ids)), AgentRun.project_id, projects)) if work_ids else []
    run_ids = {row.id for row in runs or []}
    event_ids = {UUID(value) for work in work_rows if work.source_type == "automation_event"
                 and (value := identity(work.source_id))}
    event_ids.update(row.source_event_id for row in actions if row.source_event_id)
    events = await optional_rows(session, scoped(select(AgentAutomationEvent).where(AgentAutomationEvent.id.in_(event_ids)), AgentAutomationEvent.project_id, projects)) if event_ids else []
    visible_events = {row.id: row for row in events or []}
    for event in events or []:
        rows.append(dict(fields(event, "id", "project_id", "source_type", "source_id", "event_type", "actor_kind", "occurred_at", "created_at"), kind="automation_trigger"))
    for work in work_rows:
        if work.source_type not in {"automation_event", "external_action"}:
            continue
        row = dict(work_metadata(work), kind="work_item", work_item_id=identity(work.id))
        event_id = identity(work.source_id) if work.source_type == "automation_event" else None
        event = visible_events.get(UUID(event_id)) if event_id else None
        if event is not None and event.project_id == work.project_id:
            row["event_id"] = event_id
        rows.append(row)
    for run in runs or []:
        rows.append(dict(fields(run, "id", "agent_id", "agent_revision_id", "project_id", "work_item_id", "status", "created_at", "started_at", "ended_at"), kind="agent_run", run_id=identity(run.id)))
    action_ids = {row.id for row in actions}
    if not action_ids:
        return rows
    attempts = await optional_rows(session, select(ExternalActionAttempt).where(ExternalActionAttempt.action_id.in_(action_ids)).order_by(ExternalActionAttempt.started_at.desc()).limit(limit))
    receipts = await optional_rows(session, select(ExternalActionReceipt).where(ExternalActionReceipt.action_id.in_(action_ids)).order_by(ExternalActionReceipt.created_at.desc()).limit(limit))
    approvals = await optional_rows(session, select(ExternalActionApproval).where(ExternalActionApproval.action_id.in_(action_ids)).order_by(ExternalActionApproval.created_at.desc()).limit(limit))
    for action in actions:
        row = dict(fields(action, "id", "project_id", "origin_agent_id", "status", "action_version", "created_at", "updated_at"), kind="external_action", action_id=identity(action.id))
        if action.origin_work_item_id in work_ids:
            row["work_item_id"] = identity(action.origin_work_item_id)
        if action.origin_agent_run_id in run_ids:
            row["run_id"] = identity(action.origin_agent_run_id)
        if action.source_event_id in visible_events and visible_events[action.source_event_id].project_id == action.project_id:
            row["event_id"] = identity(action.source_event_id)
        row["authorization_mode"] = action.authorization_mode
        if admin:
            row.update(fields(action, "action_policy_revision_id", "automation_rule_revision_id"))
        rows.append(row)
        # A proposal's mode is not evidence of successful authorization. The
        # trusted executor records an Attempt only after revalidation.
        if action.authorization_mode == "bounded_policy":
            for attempt in attempts or []:
                if attempt.action_id == action.id:
                    rows.append(dict(row, id=identity(attempt.id), kind="action_authorization",
                                     attempt_id=identity(attempt.id), created_at=attempt.started_at.isoformat(),
                                     action_version=attempt.action_version,
                                     authorization_mode="bounded_policy"))
    for approval in approvals or []:
        rows.append(dict(fields(approval, "id", "action_id", "action_version", "decision", "created_at"),
                         kind="action_authorization", authorization_mode="human_approval"))
    for attempt in attempts or []:
        rows.append(dict(fields(attempt, "id", "action_id", "action_version", "status", "started_at", "finished_at"), kind="action_attempt", attempt_id=identity(attempt.id)))
    for receipt in receipts or []:
        rows.append(dict(fields(receipt, "id", "action_id", "attempt_id", "action_version", "confirmation_level", "created_at", "provider_observed_at"), kind="action_receipt", receipt_id=identity(receipt.id)))
    return rows
