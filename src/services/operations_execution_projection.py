"""Bounded, metadata-only Operations reads. This module performs no mutations.

Do not use AgentRun.to_dict or get_run(include_timeline=True) here: those
include private prompts, arguments, results and unbounded lazy relationships.
"""
from __future__ import annotations

import base64
import binascii
import json
import re
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import String, and_, case, cast, desc, func, or_, select

from ..memory.models import AgentRun, AgentRunEvent, AgentRunToolCall, AgentWorkItem, EngagementOpportunity
from .agent_run_operation_identity import EVENT_OPERATION_CONTAINERS, EVENT_OPERATION_KEYS
from .operations_employee_observability import optional_rows, scoped

ACTIVE = frozenset({"claimed", "running"})
FINISHED = frozenset({"succeeded", "cancelled"})
PHASES = frozenset({"planning", "thinking", "executing", "verifying", "waiting", "completed", "failed", "cancelled"})
_PHASE_EVENT_TYPES = frozenset({
    "run.queued", "run.started", "run.succeeded", "run.failed", "run.cancelled", "run.awaiting_approval",
    "stream.thinking", "stream.tool_start", "stream.tool_end", "director.needs_human",
})
_TOOL_END_EVENT_TYPES = frozenset({"stream.tool_end", "tool.end", "tool.failed"})
_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,127}\Z")
_SECRET = re.compile(r"bearer\s|(?:api[_-]?key|password|token|secret|credential|authorization)\s*[=:]|sk-[A-Za-z0-9]|https?://\S+@|(?:[A-Z]:\\|/(?:home|root|workspace|tmp)/)", re.I)


def code(value: Any, default: str | None = None) -> str | None:
    if not isinstance(value, str) or not _CODE.fullmatch(value) or _SECRET.search(value):
        return default
    return value


def uid(value: Any) -> str | None:
    try:
        return str(UUID(str(value))) if value is not None else None
    except (ValueError, TypeError, AttributeError):
        return None


def dt(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def iso(value: Any) -> str | None:
    parsed = dt(value)
    return parsed.isoformat() + "Z" if parsed else None


def elapsed(start: Any, end: Any, now: datetime) -> float | None:
    start, end = dt(start), dt(end)
    return max(0.0, ((end or now) - start).total_seconds()) if start else None


def phase(event: Any) -> str | None:
    explicit = getattr(event, "phase", None)
    if isinstance(explicit, str) and explicit in PHASES:
        return explicit
    kind = getattr(event, "event_type", "")
    return {
        "run.queued": "waiting", "run.awaiting_approval": "waiting", "run.started": "executing",
        "run.succeeded": "completed", "run.failed": "failed", "run.cancelled": "cancelled",
        "stream.thinking": "thinking", "stream.tool_start": "executing",
        "stream.tool_end": "executing", "director.needs_human": "waiting",
    }.get(kind)


def _same_project(column: Any, project_id: UUID | None):
    """Match a nullable project boundary without turning NULL into a wildcard."""

    return column.is_(None) if project_id is None else column == project_id


def attention_predicate(now: datetime):
    """Select blockers BEFORE LIMIT. Finished retries are not exhausted work."""
    return or_(
        AgentWorkItem.state.in_(("blocked", "uncertain", "retry_wait", "dead_letter", "awaiting_approval", "failed")),
        and_(AgentWorkItem.state.in_(ACTIVE), AgentWorkItem.lease_expires_at <= now),
        and_(AgentWorkItem.state.not_in(FINISHED | ACTIVE), AgentWorkItem.attempt_count >= AgentWorkItem.max_attempts),
        and_(AgentWorkItem.state.not_in(FINISHED), or_(AgentWorkItem.blocker_code.is_not(None), AgentWorkItem.safe_error_code.is_not(None), AgentWorkItem.escalation_reason.is_not(None))),
    )


def _opportunity_join():
    # IDs and project equality are both required. A source ID is not authority.
    return and_(
        AgentWorkItem.source_type.like("%crowdworks%"),
        func.replace(cast(EngagementOpportunity.id, String), "-", "") == func.replace(AgentWorkItem.source_id, "-", ""),
        EngagementOpportunity.project_id == AgentWorkItem.project_id,
    )


def review_predicate():
    """The current canonical opportunity decides whether review is still due."""
    status = EngagementOpportunity.metadata_json["development"]["status"].as_string()
    stage = EngagementOpportunity.metadata_json["workflow_stage"].as_string()
    work_id = EngagementOpportunity.metadata_json["review_work_item_id"].as_string()
    development_work = EngagementOpportunity.metadata_json["development"]["work_item_id"].as_string()
    return and_(
        _opportunity_join(),
        or_(work_id == cast(AgentWorkItem.id, String),
            func.replace(work_id, "-", "") == func.replace(cast(AgentWorkItem.id, String), "-", ""),
            func.replace(development_work, "-", "") == func.replace(cast(AgentWorkItem.id, String), "-", "")),
        or_(stage == "READY_FOR_REVIEW", status.in_(("ready_for_review", "verification_failed"))),
    )


async def opportunity_statuses(session, rows, projects):
    if not rows:
        return {}
    statement = select(
        AgentWorkItem.id,
        EngagementOpportunity.metadata_json["workflow_stage"].as_string().label("stage"),
        EngagementOpportunity.metadata_json["development"]["status"].as_string().label("development"),
    ).join(EngagementOpportunity, _opportunity_join()).where(
        AgentWorkItem.id.in_([row.id for row in rows]), review_predicate(),
    )
    statement = scoped(statement, EngagementOpportunity.project_id, projects)
    # Optional domain absence must be distinguishable from a healthy empty join.
    try:
        async with session.begin_nested():
            result = (await session.execute(statement)).all()
    except Exception as exc:
        from sqlalchemy.exc import DBAPIError
        if not isinstance(exc, DBAPIError):
            raise
        return None
    return {str(row.id): "ready_for_review" if row.stage == "READY_FOR_REVIEW" else row.development for row in result}


def summary(row, *, run=None, event=None, phase_event=None, current_tool=None, review=None, now=None, observation_status="available"):
    now = now or datetime.utcnow()
    state = code(getattr(row, "state", None), "unknown")
    attempt = max(0, int(getattr(row, "attempt_count", 0) or 0))
    maximum = max(1, int(getattr(row, "max_attempts", 1) or 1))
    expiry = dt(getattr(row, "lease_expires_at", None))
    health = "none" if expiry is None else "active" if expiry > now else "expired"
    blocker = code(getattr(row, "blocker_code", None))
    error = code(getattr(row, "safe_error_code", None))
    reasons = []
    if state in {"blocked", "uncertain", "retry_wait", "dead_letter", "awaiting_approval", "failed"}:
        reasons.append("retry_exhausted" if state == "dead_letter" else state)
    if state in ACTIVE and health == "expired":
        reasons.append("stale")
    if state not in FINISHED and attempt >= maximum and state not in ACTIVE:
        reasons.append("retry_exhausted")
    if review in {"ready_for_review", "verification_failed"}:
        reasons.append(review)
    if error and "verification_failed" in error:
        reasons.append("verification_failed")
    if state not in FINISHED and (getattr(row, "escalation_reason", None) or (blocker and any(key in blocker for key in ("human", "permission", "approval")))):
        reasons.append("human_action_required")
    if state not in FINISHED and (blocker or error) and not reasons:
        reasons.append("blocked")
    if event and getattr(event, "event_type", None) == "director.needs_human" and state not in FINISHED:
        reasons.append("human_action_required")
    times = []  # Lease heartbeats are liveness, not evidence of execution progress.
    times += [
        dt(getattr(run, "last_event_at", None)),
        dt(getattr(event, "created_at", None)),
        dt(getattr(phase_event, "created_at", None)),
    ]
    activity = max((value for value in times if value), default=None)
    # Stored summaries are arbitrary prose, including for old ledger rows.
    # This endpoint deliberately exposes status/error metadata, not that prose.
    result = None
    stop = getattr(row, "completed_at", None) or getattr(row, "cancelled_at", None)
    if not stop and state in FINISHED:
        stop = getattr(run, "ended_at", None) or getattr(row, "updated_at", None)
    start = getattr(row, "started_at", None) or getattr(row, "claimed_at", None)
    current_phase = phase(phase_event or event) if (phase_event or event) else None
    if state in FINISHED:
        current_phase = "completed" if state == "succeeded" else "cancelled"
    tool = code(current_tool) if state in ACTIVE else None
    if tool:
        current_phase = "executing"
    return dict(
        phase=current_phase, tool=tool, last_activity_at=iso(activity),
        elapsed_seconds=elapsed(start, stop, now), attempt_count=attempt, max_attempts=maximum,
        heartbeat_at=iso(getattr(row, "heartbeat_at", None)), lease_expires_at=iso(expiry),
        lease_health=health, blocker_code=blocker, safe_error_code=error,
        result_summary=result, attention_reasons=list(dict.fromkeys(reasons)), observation_status=observation_status,
    )


def _run_columns():
    return (AgentRun.id, AgentRun.work_item_id, AgentRun.project_id, AgentRun.parent_run_id,
        AgentRun.root_run_id, AgentRun.acting_subagent_id, AgentRun.work_item_attempt,
        AgentRun.status, AgentRun.started_at, AgentRun.ended_at, AgentRun.last_event_at,
        AgentRun.created_at, AgentRun.error.is_not(None).label("has_error"))


def _json_key_aliases(column):
    """Return producer-compatible aliases for a JSON identity container."""

    expressions = [
        func.nullif(func.trim(column[key].as_string()), "")
        for key in EVENT_OPERATION_KEYS
    ]
    for container in EVENT_OPERATION_CONTAINERS:
        expressions.extend(
            func.nullif(func.trim(column[container][key].as_string()), "")
            for key in EVENT_OPERATION_KEYS
        )
        if container in {"tool_call", "call"}:
            expressions.append(func.nullif(func.trim(column[container]["id"].as_string()), ""))
    return expressions


def _event_key_aliases(table):
    return _json_key_aliases(table.c.payload)


def _receipt_key_aliases(table):
    return _json_key_aliases(table.c.result_metadata)


def _top_level_aliases(column):
    return [
        func.nullif(func.trim(column[key].as_string()), "")
        for key in EVENT_OPERATION_KEYS
    ]


def _identity_match(left_key, left_aliases, right_key, right_aliases):
    # Keep the SQL expression bounded on SQLite. The canonical expression
    # includes nested producer containers; top-level aliases cover mixed-ID
    # receipts without generating a nested-alias cross product.
    return or_(
        left_key == right_key,
        *(left_alias == right_alias for left_alias in left_aliases for right_alias in right_aliases),
    )


def _event_columns():
    return (
        AgentRunEvent.id, AgentRunEvent.run_id, AgentRunEvent.sequence, AgentRunEvent.event_type,
        AgentRunEvent.status, AgentRunEvent.created_at,
        AgentRunEvent.payload["phase"].as_string().label("phase"),
        AgentRunEvent.payload["tool"].as_string().label("tool"),
        AgentRunEvent.payload["tool_name"].as_string().label("tool_name"),
        _event_key(AgentRunEvent.__table__).label("operation_key"),
    )


def _event_key(table):
    return func.coalesce(*_event_key_aliases(table))


async def current_tools(session, run_ids):
    """Find unmatched starts, independent of intervening progress events.

    Correlation is by run plus operation identity; tool name alone must not
    collapse concurrent calls. Legacy uncorrelated starts remain unknown.
    """
    if not run_ids:
        return {}
    starts = AgentRunEvent.__table__.alias("operation_starts")
    ends = AgentRunEvent.__table__.alias("operation_ends")
    receipts = AgentRunToolCall.__table__.alias("operation_receipts")
    start_key = _event_key(starts)
    end_key = _event_key(ends)
    start_aliases = _top_level_aliases(starts.c.payload)
    end_aliases = _top_level_aliases(ends.c.payload)
    receipt_key = func.coalesce(*_receipt_key_aliases(receipts))
    receipt_aliases = _top_level_aliases(receipts.c.result_metadata)
    ended = select(ends.c.id).where(
        ends.c.run_id == starts.c.run_id, ends.c.sequence > starts.c.sequence,
        ends.c.event_type.in_(_TOOL_END_EVENT_TYPES),
        _identity_match(end_key, end_aliases, start_key, start_aliases),
    ).exists()
    persisted_done = select(receipts.c.id).where(
        receipts.c.run_id == starts.c.run_id,
        or_(
            receipts.c.event_id == starts.c.id,
            receipts.c.tool_call_id == start_key,
            *(receipts.c.tool_call_id == alias for alias in start_aliases),
            receipt_key == start_key,
            *(receipt_alias == start_alias for receipt_alias in receipt_aliases for start_alias in start_aliases),
        ),
        or_(receipts.c.success.is_(True), receipts.c.ended_at.is_not(None)),
    ).exists()
    ranked = select(
        starts.c.run_id,
        func.coalesce(starts.c.payload["tool_name"].as_string(), starts.c.payload["tool"].as_string()).label("tool_name"),
        func.row_number().over(partition_by=starts.c.run_id, order_by=starts.c.sequence.desc()).label("rank"),
    ).where(starts.c.run_id.in_(run_ids), starts.c.event_type == "stream.tool_start",
            _event_key(starts).is_not(None), ~ended, ~persisted_done).subquery()
    rows = (await session.execute(select(ranked).where(ranked.c.rank == 1))).all()
    return {str(row.run_id): code(row.tool_name) for row in rows}


async def latest_phase_events(session, run_ids):
    """Get the latest phase-bearing event per run, independent of event pages."""

    if not run_ids:
        return {}
    phase_value = AgentRunEvent.payload["phase"].as_string()
    ranked = select(*_event_columns(), func.row_number().over(
        partition_by=AgentRunEvent.run_id, order_by=AgentRunEvent.sequence.desc(),
    ).label("rank")).where(
        AgentRunEvent.run_id.in_(run_ids),
        or_(phase_value.in_(PHASES), AgentRunEvent.event_type.in_(_PHASE_EVENT_TYPES)),
    ).subquery()
    rows = (await session.execute(select(ranked).where(ranked.c.rank == 1))).all()
    return {str(event.run_id): event for event in rows}


async def card_summaries(session, rows, projects, *, now=None):
    """Return one bounded, same-project summary for each work card.

    ``active_agent_run_id`` is the producer's current-run pointer.  A matching
    ``work_item_attempt`` is the fallback for older rows that predate that
    pointer.  Phase evidence is queried separately from the latest activity so
    a token/progress event cannot erase the latest recorded phase.
    """
    if not rows:
        return {}
    now = now or datetime.utcnow()
    work_ids = [row.id for row in rows]
    project_match = or_(
        AgentRun.project_id == AgentWorkItem.project_id,
        and_(AgentRun.project_id.is_(None), AgentWorkItem.project_id.is_(None)),
    )
    active_priority = case(
        (AgentRun.id == AgentWorkItem.active_agent_run_id, 0),
        (and_(AgentWorkItem.attempt_count.is_not(None),
              AgentRun.work_item_attempt == AgentWorkItem.attempt_count), 1),
        else_=2,
    )
    ranked = scoped(select(
        AgentRun.id, AgentRun.work_item_id,
        func.row_number().over(
            partition_by=AgentRun.work_item_id,
            order_by=(active_priority, AgentRun.created_at.desc(), AgentRun.id.desc()),
        ).label("rank"),
    ).join(AgentWorkItem, AgentWorkItem.id == AgentRun.work_item_id).where(
        AgentRun.work_item_id.in_(work_ids), project_match,
    ), AgentRun.project_id, projects).subquery()
    runs = (await session.execute(select(*_run_columns()).join(ranked, ranked.c.id == AgentRun.id).where(ranked.c.rank == 1))).all()
    by_work = {str(run.work_item_id): run for run in runs}
    by_run = {}
    by_phase_run = {}
    if runs:
        ranked_events = select(*_event_columns(), func.row_number().over(
            partition_by=AgentRunEvent.run_id, order_by=AgentRunEvent.sequence.desc(),
        ).label("rank")).where(AgentRunEvent.run_id.in_([run.id for run in runs])).subquery()
        events = (await session.execute(select(ranked_events).where(ranked_events.c.rank == 1))).all()
        by_run = {str(event.run_id): event for event in events}
        by_phase_run = await latest_phase_events(session, [run.id for run in runs])
    tools = await current_tools(session, [run.id for run in runs if run.status in {"running", "queued"}])
    reviews = await opportunity_statuses(session, rows, projects)
    return {str(row.id): summary(row, run=by_work.get(str(row.id)),
        event=by_run.get(str(by_work[str(row.id)].id)) if str(row.id) in by_work else None,
        phase_event=by_phase_run.get(str(by_work[str(row.id)].id)) if str(row.id) in by_work else None,
        current_tool=tools.get(str(by_work[str(row.id)].id)) if str(row.id) in by_work else None,
        review=(reviews or {}).get(str(row.id)), now=now,
        observation_status="partial" if reviews is None else "available") for row in rows}


def _cursor(value, *, kind, work_id):
    if not value:
        return None
    try:
        if len(value) > 1024:
            raise ValueError()
        raw = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
        if raw["kind"] != kind or raw["work"] != str(work_id):
            raise ValueError()
        priority = raw.get("priority")
        if kind == "runs" and priority is None:
            raise ValueError()
        if priority is not None and (
            isinstance(priority, bool) or not isinstance(priority, int) or priority < 0 or priority > 2
        ):
            raise ValueError()
        at, identity = dt(raw["at"]), uid(raw["id"])
        if at is None or identity is None:
            raise ValueError()
        return at, UUID(identity), priority
    except (binascii.Error, ValueError, TypeError, KeyError, UnicodeError, OverflowError) as exc:
        raise ValueError("invalid execution cursor") from exc


def _page(rows, limit, *, kind, work_id, priority=None):
    more = len(rows) > limit
    items = rows[:limit]
    cursor = None
    if more and items:
        last = items[-1]
        payload = {"kind": kind, "work": str(work_id),
            "at": iso(last.created_at), "id": str(last.id)}
        if priority is not None:
            payload["priority"] = int(priority(last))
        cursor = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    return items, dict(next_cursor=cursor, has_more=more, limit=limit)


def _paged(statement, model, cursor, limit, *, priority=None):
    if cursor:
        at, identity, cursor_priority = cursor
        if priority is None or cursor_priority is None:
            statement = statement.where(or_(model.created_at < at, and_(model.created_at == at, model.id < identity)))
        else:
            statement = statement.where(or_(
                priority > cursor_priority,
                and_(priority == cursor_priority, model.created_at < at),
                and_(priority == cursor_priority, model.created_at == at, model.id < identity),
            ))
    if priority is None:
        return statement.order_by(model.created_at.desc(), model.id.desc()).limit(limit + 1)
    return statement.order_by(priority.asc(), model.created_at.desc(), model.id.desc()).limit(limit + 1)


def source_href(work):
    task_id, source_id = uid(getattr(work, "task_id", None)), uid(getattr(work, "source_id", None))
    if task_id:
        return f"/tasks?detail={task_id}"
    source = str(getattr(work, "source_type", ""))
    if "crowdworks" in source and source_id:
        return f"/operations?tab=opportunities&opportunity_id={source_id}"
    if source == "external_action" and source_id:
        return f"/operations?tab=actions&action={source_id}"
    return None


def canonical_source_ref(work) -> dict[str, str]:
    """Return the typed UI source identity without guessing from source_id.

    Chat work points at its private ConversationSession, while autonomous
    work points at its assigned Agent when available.  The WorkItem itself is
    the final canonical fallback.  ``source_id`` remains for compatibility;
    consumers should use this typed reference for navigation.
    """

    session_id = uid(getattr(work, "conversation_session_id", None))
    if str(getattr(work, "source_type", "") or "") == "chat_turn" and session_id:
        return {"kind": "conversation", "id": session_id}
    agent_id = uid(getattr(work, "assigned_agent_id", None))
    if agent_id:
        return {"kind": "agent", "id": agent_id}
    work_id = uid(getattr(work, "id", None))
    return {"kind": "work", "id": work_id} if work_id else {}


def _run_priority(run, work):
    if run.id == getattr(work, "active_agent_run_id", None):
        return 0
    if (
        getattr(run, "work_item_attempt", None) is not None
        and getattr(run, "work_item_attempt", None) == getattr(work, "attempt_count", None)
    ):
        return 1
    return 2


async def related_work_ids(session, work, projects):
    """Bound parent/root traversal to 200 same-project work items and 16 levels."""
    identities, frontier = {work.id}, {work.id}
    for _depth in range(16):
        statement = scoped(select(AgentWorkItem.id).where(
            _same_project(AgentWorkItem.project_id, work.project_id),
            AgentWorkItem.id.not_in(identities),
            or_(AgentWorkItem.root_work_item_id == work.id,
                AgentWorkItem.parent_work_item_id.in_(frontier)),
        ), AgentWorkItem.project_id, projects).order_by(AgentWorkItem.id).limit(201 - len(identities))
        children = list((await session.execute(statement)).scalars().all())
        if not children:
            return identities, False
        remaining = 200 - len(identities)
        identities.update(children[:remaining])
        if len(children) > remaining:
            return identities, True
        frontier = set(children)
    return identities, True


async def related_run_ids(session, work, work_ids, projects, *, max_runs=200, max_depth=16):
    """Walk only same-project causal run descendants with bounded breadth/depth.

    Runs attached to a related WorkItem are direct members.  A run without a
    WorkItem may be included only when its parent/root is already in the
    discovered set; a run attached to another WorkItem cannot smuggle itself
    into this tree through a forged parent/root link.
    """

    project_match = _same_project(AgentRun.project_id, work.project_id)
    current_priority = case(
        (AgentRun.id == AgentWorkItem.active_agent_run_id, 0),
        (and_(AgentWorkItem.attempt_count.is_not(None),
              AgentRun.work_item_attempt == AgentWorkItem.attempt_count), 1),
        else_=2,
    )
    direct = scoped(select(AgentRun.id).join(
        AgentWorkItem, AgentWorkItem.id == AgentRun.work_item_id,
    ).where(
        project_match,
        AgentWorkItem.id.in_(work_ids),
    ), AgentRun.project_id, projects).order_by(
        current_priority, AgentRun.created_at, AgentRun.id,
    )
    direct_rows = list((await session.execute(direct.limit(max_runs + 1))).scalars().all())
    truncated = len(direct_rows) > max_runs
    identities = set(direct_rows[:max_runs])
    frontier = set(identities)
    for _depth in range(max_depth):
        if not frontier or len(identities) >= max_runs:
            break
        statement = scoped(select(AgentRun.id).where(
            project_match,
            AgentRun.id.not_in(identities),
            AgentRun.work_item_id.is_(None),
            or_(AgentRun.parent_run_id.in_(frontier), AgentRun.root_run_id.in_(frontier)),
        ), AgentRun.project_id, projects).order_by(AgentRun.created_at, AgentRun.id).limit(
            max_runs - len(identities) + 1
        )
        children = list((await session.execute(statement)).scalars().all())
        if not children:
            break
        remaining = max_runs - len(identities)
        accepted = children[:remaining]
        identities.update(accepted)
        frontier = set(accepted)
        if len(children) > remaining:
            truncated = True
            break
    if len(identities) >= max_runs:
        # A full page is not proof that no more causal runs exist.
        probe = scoped(select(AgentRun.id).where(
            project_match,
            AgentRun.id.not_in(identities),
            AgentRun.work_item_id.is_(None),
            or_(AgentRun.parent_run_id.in_(frontier), AgentRun.root_run_id.in_(frontier)),
        ), AgentRun.project_id, projects).limit(1)
        truncated = truncated or (await session.execute(probe)).scalars().first() is not None
    return identities, truncated


async def tool_lifecycles(session, keys):
    """Resolve start/end evidence for a bounded set of run/operation keys.

    The query deliberately selects only lifecycle columns.  It is independent
    of the event page currently being rendered, so a page boundary cannot make
    a completed tool appear running again.
    """

    pairs = {(run_id, str(operation_key)) for run_id, operation_key in keys if operation_key}
    if not pairs:
        return {}
    table = AgentRunEvent.__table__.alias("tool_lifecycle")
    key_expr = _event_key(table)
    aliases = [key_expr, *_top_level_aliases(table.c.payload)]
    pair_filter = or_(*(
        and_(table.c.run_id == run_id, alias == operation_key)
        for run_id, operation_key in pairs
        for alias in aliases
    ))
    alias_columns = [
        func.max(alias).label(f"operation_alias_{index}")
        for index, alias in enumerate(aliases)
    ]
    statement = select(
        table.c.run_id,
        key_expr.label("operation_key"),
        *alias_columns,
        func.max(case((table.c.event_type == "stream.tool_start", table.c.sequence), else_=None)).label("last_start"),
        func.max(case((table.c.event_type.in_(_TOOL_END_EVENT_TYPES), table.c.sequence), else_=None)).label("last_end"),
        func.max(case((table.c.event_type == "tool.failed", table.c.sequence), else_=None)).label("last_failed"),
    ).where(
        table.c.event_type.in_(_TOOL_END_EVENT_TYPES | {"stream.tool_start"}),
        pair_filter,
    ).group_by(table.c.run_id, key_expr)
    rows = (await session.execute(statement)).all()
    lifecycles = {}
    for row in rows:
        lifecycle = {
            "last_start": row.last_start,
            "last_end": row.last_end,
            "last_failed": row.last_failed,
        }
        identities = {row.operation_key}
        identities.update(
            getattr(row, f"operation_alias_{index}")
            for index in range(len(aliases))
        )
        for operation_key in identities:
            if operation_key:
                lifecycles[(str(row.run_id), str(operation_key))] = lifecycle
    return lifecycles


async def event_alias_values(session, event_ids):
    """Read only correlation aliases for a bounded event page."""

    if not event_ids:
        return {}
    table = AgentRunEvent.__table__
    aliases = _event_key_aliases(table)
    columns = [alias.label(f"operation_alias_{index}") for index, alias in enumerate(aliases)]
    rows = (await session.execute(select(table.c.id, *columns).where(table.c.id.in_(event_ids)))).all()
    return {
        row.id: {
            value for index in range(len(aliases))
            if (value := getattr(row, f"operation_alias_{index}"))
        }
        for row in rows
    }


def tool_status(tool, *, active_run_ids, lifecycles):
    if tool.ended_at is not None:
        return "succeeded" if tool.success else "failed"
    if tool.success:
        return "succeeded"
    lifecycle = lifecycles.get((str(tool.run_id), str(tool.tool_call_id))) if tool.tool_call_id else None
    if lifecycle and lifecycle["last_end"] is not None and (
        lifecycle["last_start"] is None or lifecycle["last_end"] >= lifecycle["last_start"]
    ):
        return "failed" if lifecycle["last_failed"] == lifecycle["last_end"] else "succeeded"
    if tool.started_at is not None and tool.run_id in active_run_ids and lifecycle and lifecycle["last_start"] is not None:
        return "running"
    # Older completed tool records did not always save ended_at. No end time
    # alone is not evidence of a failure or of a still-running operation.
    return "unknown"


async def read_execution(session, work_id, projects, *, limit=50, runs_cursor=None, events_cursor=None, tools_cursor=None):
    """Authorization is enforced on the work AND on every queried run."""
    now = datetime.utcnow()
    limit = max(1, min(int(limit), 100))
    identity = UUID(str(work_id))
    cursors = {kind: _cursor(value, kind=kind, work_id=identity) for kind, value in
               (("runs", runs_cursor), ("events", events_cursor), ("tools", tools_cursor))}
    work = (await session.execute(scoped(select(AgentWorkItem).where(AgentWorkItem.id == identity), AgentWorkItem.project_id, projects))).scalars().first()
    if work is None:
        return None
    work_ids, work_truncated = await related_work_ids(session, work, projects)
    run_ids, run_truncated = await related_run_ids(session, work, work_ids, projects)
    # The bounded traversal has already enforced Work, project and causal
    # boundaries. Reuse its ID set for every page and child query.
    visible_runs = scoped(
        select(AgentRun.id).where(AgentRun.id.in_(run_ids or {UUID(int=0)})),
        AgentRun.project_id,
        projects,
    )
    run_priority = case(
        (AgentRun.id == work.active_agent_run_id, 0),
        (and_(work.attempt_count is not None, AgentRun.work_item_attempt.is_not(None),
              AgentRun.work_item_attempt == work.attempt_count), 1),
        else_=2,
    )
    runs_raw = list((await session.execute(_paged(
        scoped(
            select(*_run_columns()).where(AgentRun.id.in_(run_ids or {UUID(int=0)})),
            AgentRun.project_id,
            projects,
        ),
        AgentRun,
        cursors["runs"],
        limit,
        priority=run_priority,
    ))).all())
    runs, runs_page = _page(
        runs_raw,
        limit,
        kind="runs",
        work_id=identity,
        priority=lambda run: _run_priority(run, work),
    )
    runs_page["truncated"] = run_truncated
    events_raw = list((await session.execute(_paged(select(*_event_columns()).where(AgentRunEvent.run_id.in_(visible_runs)), AgentRunEvent, cursors["events"], limit))).all())
    events, events_page = _page(events_raw, limit, kind="events", work_id=identity)
    tool_columns = (AgentRunToolCall.id, AgentRunToolCall.run_id, AgentRunToolCall.tool_name, AgentRunToolCall.tool_call_id, AgentRunToolCall.success,
        AgentRunToolCall.started_at, AgentRunToolCall.ended_at, AgentRunToolCall.duration_ms, AgentRunToolCall.created_at)
    tools_raw = list((await session.execute(_paged(select(*tool_columns).where(AgentRunToolCall.run_id.in_(visible_runs)), AgentRunToolCall, cursors["tools"], limit))).all())
    tool_rows, tools_page = _page(tools_raw, limit, kind="tools", work_id=identity)
    execution_summary = (await card_summaries(session, [work], projects, now=now))[str(work.id)]
    work_data = {name: uid(getattr(work, name, None)) for name in ("id", "parent_work_item_id", "root_work_item_id", "project_id", "task_id", "assigned_agent_id")}
    work_references = {value for value in (work.parent_work_item_id, work.root_work_item_id) if value}
    visible_work_references = set((await session.execute(scoped(select(AgentWorkItem.id).where(
        AgentWorkItem.id.in_(work_references), _same_project(AgentWorkItem.project_id, work.project_id),
    ), AgentWorkItem.project_id, projects))).scalars().all()) if work_references else set()
    for name in ("parent_work_item_id", "root_work_item_id"):
        if getattr(work, name, None) not in visible_work_references:
            work_data[name] = None
    typed_source_ref = canonical_source_ref(work)
    work_data.update(source_type=code(work.source_type), source_id=uid(work.source_id), domain=code(work.domain),
        state=code(work.state, "unknown"), created_at=iso(work.created_at), updated_at=iso(work.updated_at),
        execution_summary=execution_summary, source_href=source_href(work),
        canonical_ref=typed_source_ref, source_ref=typed_source_ref)
    if work_truncated or run_truncated:
        execution_summary["observation_status"] = "partial"
    references = {value for run in runs for value in (run.parent_run_id, run.root_run_id) if value}
    visible_references = set((await session.execute(visible_runs.where(AgentRun.id.in_(references)))).scalars().all()) if references else set()
    phase_by_run = await latest_phase_events(session, run_ids)
    # The page may contain only a subset of runs, but current metadata is
    # resolved against the bounded full tree before selecting those rows.
    tool_by_run = await current_tools(session, [run.id for run in runs if run.status in {"running", "queued"}])
    run_data = [dict(id=str(run.id), work_item_id=uid(run.work_item_id) if run.work_item_id in work_ids else None, parent_run_id=uid(run.parent_run_id) if run.parent_run_id in visible_references else None, root_run_id=uid(run.root_run_id) if run.root_run_id in visible_references else None,
        acting_subagent_id=code(run.acting_subagent_id), work_item_attempt=run.work_item_attempt,
        status=code(run.status, "unknown"),
        phase=("completed" if run.status == "succeeded" else "cancelled" if run.status == "cancelled"
               else "executing" if tool_by_run.get(str(run.id))
               else phase(phase_by_run.get(str(run.id)))),
        tool=tool_by_run.get(str(run.id)),
        started_at=iso(run.started_at), ended_at=iso(run.ended_at),
        last_event_at=iso(run.last_event_at), elapsed_seconds=elapsed(run.started_at, run.ended_at, now),
        error_code="run_failed" if run.has_error or run.status == "failed" else None) for run in runs]
    event_data = [dict(id=str(event.id), run_id=str(event.run_id), sequence=event.sequence,
        event_type=code(event.event_type, "unknown"), status=code(event.status), phase=phase(event),
        created_at=iso(event.created_at), error_code="execution_failed" if event.status == "failed" or event.event_type in {"run.failed", "tool.failed", "agent_team.instance_failed"} else None) for event in events]
    active_run_ids = set((await session.execute(visible_runs.where(
        AgentRun.status.in_(("running", "queued")),
        AgentRun.id.in_({tool.run_id for tool in tool_rows}),
    ))).scalars().all()) if tool_rows else set()
    lifecycle_keys = {
        (tool.run_id, tool.tool_call_id) for tool in tool_rows if tool.tool_call_id
    }
    lifecycle_keys.update(
        (event.run_id, event.operation_key) for event in events
        if event.event_type == "stream.tool_start" and event.operation_key
    )
    lifecycles = await tool_lifecycles(session, lifecycle_keys)
    tools_data = []
    for tool in tool_rows:
        status = tool_status(tool, active_run_ids=active_run_ids, lifecycles=lifecycles)
        tools_data.append(dict(id=str(tool.id), run_id=str(tool.run_id), tool_name=code(tool.tool_name, "tool"),
            status=status, started_at=iso(tool.started_at), ended_at=iso(tool.ended_at),
            duration_ms=tool.duration_ms, error_code="tool_failed" if status == "failed" else None))
    # Check persisted identity across all tool pages before synthesizing an
    # event-only row. This prevents a later tools page from duplicating the
    # same operation with an old event:<id> row.
    event_ids = {event.id for event in events}
    aliases_by_event = await event_alias_values(session, event_ids)
    event_keys = {(event.run_id, event.operation_key) for event in events if event.operation_key}
    all_event_aliases = {
        alias
        for aliases in aliases_by_event.values()
        for alias in aliases
    }
    persisted_keys = set()
    if event_keys or event_ids:
        persisted_statement = select(
            AgentRunToolCall.run_id, AgentRunToolCall.tool_call_id,
        ).where(
            AgentRunToolCall.run_id.in_(run_ids),
            or_(
                AgentRunToolCall.event_id.in_(event_ids) if event_ids else False,
                and_(
                    AgentRunToolCall.tool_call_id.is_not(None),
                    AgentRunToolCall.tool_call_id.in_(all_event_aliases or {""}),
                ),
            ),
        )
        persisted_keys = {(row.run_id, row.tool_call_id) for row in (await session.execute(persisted_statement)).all()}
    # Only lifecycle metadata is used for in-flight tool visibility. A page
    # boundary is incomplete evidence: expose unknown, never fabricate running.
    for event in events:
        if event.event_type != "stream.tool_start":
            continue
        lifecycle = lifecycles.get((str(event.run_id), str(event.operation_key))) if event.operation_key else None
        if lifecycle and lifecycle["last_end"] is not None and (
            lifecycle["last_start"] is None or lifecycle["last_end"] >= lifecycle["last_start"]
        ):
            continue
        name = code(event.tool_name) or code(event.tool) or "tool"
        if (event.run_id, event.operation_key) in persisted_keys:
            continue
        run = next((run for run in runs if run.id == event.run_id), None)
        status = "running" if run and run.status in {"running", "queued"} and lifecycle and lifecycle["last_start"] is not None else "unknown"
        tools_data.append(dict(id="event:" + str(event.id), run_id=str(event.run_id), tool_name=name,
            status=status, started_at=iso(event.created_at), ended_at=None, duration_ms=None, error_code=None))
    return dict(schema_version="operations-work-execution-v1", observed_at=iso(now), work=work_data,
        runs=run_data, events=event_data, tool_calls=tools_data, runs_page=runs_page, events_page=events_page, tools_page=tools_page)
