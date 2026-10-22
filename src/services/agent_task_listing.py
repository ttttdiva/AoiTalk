"""Owner-scoped, read-only listing of persisted canonical Agent task invocations.

Rows come from ``AgentWorkItem`` (source ``api_agent_task``) only. The projection is
an explicit allowlist of identifiers, state, route and timestamps; prompts,
objectives, results, failure text and credentials are never read into it.
"""

from __future__ import annotations

import base64
import binascii
import inspect
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import and_, or_, select

from .agent_task_invocation import AGENT_TASK_ADAPTER_KEY, AGENT_TASK_SOURCE_TYPE
from .agent_work_runtime import WORK_TERMINAL_STATES

DEFAULT_LIMIT = 20
MAX_LIMIT = 100

# Exact WorkItem state -> public status. Anything absent is reported as
# "unknown"; no fuzzy or prefix matching decides terminality.
STATE_STATUS: Mapping[str, str] = {
    "pending": "queued",
    "claimed": "running",
    "running": "running",
    "retry_wait": "waiting",
    "awaiting_approval": "waiting_approval",
    "succeeded": "completed",
    "failed": "failed",
    "dead_letter": "failed",
    "blocked": "failed",
    "cancelled": "cancelled",
    "uncertain": "interrupted",
}
LIST_STATUSES = frozenset(set(STATE_STATUS.values()) | {"unknown"})


class AgentTaskListingError(ValueError):
    def __init__(self, code: str, message: str, http_status: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


def encode_cursor(created_at: datetime, row_id: uuid.UUID) -> str:
    raw = f"{created_at.isoformat()}|{row_id}"
    return base64.urlsafe_b64encode(raw.encode("ascii")).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        if len(cursor) > 128:
            raise ValueError
        padded = cursor + "=" * (-len(cursor) % 4)
        text = base64.urlsafe_b64decode(padded.encode("ascii")).decode("ascii")
        stamp, ident = text.split("|", 1)
        moment = datetime.fromisoformat(stamp)
        if moment.tzinfo is not None or not 2000 <= moment.year <= 2999:
            raise ValueError
        return moment, uuid.UUID(ident)
    except (ValueError, UnicodeError, binascii.Error):
        raise AgentTaskListingError("invalid_cursor", "cursor is invalid") from None


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def project_row(row: Any) -> dict[str, Any]:
    metadata = row.metadata_json if isinstance(row.metadata_json, Mapping) else {}
    state = str(row.state or "pending")
    status = STATE_STATUS.get(state, "unknown")
    if state == "pending" and metadata.get("admission_state") != "ready":
        status = "interrupted"
    route = metadata.get("route") if isinstance(metadata.get("route"), Mapping) else {}
    return {
        "task_id": str(row.source_id),
        "work_item_id": str(row.id),
        "agent_run_id": str(metadata.get("agent_run_id") or row.active_agent_run_id or "") or None,
        "agent_id": str(row.assigned_agent_id or "") or None,
        "agent_revision_id": str(row.agent_revision_id or "") or None,
        "state": state,
        "status": status,
        # Exact canonical raw states only; recoverable "blocked" is not terminal.
        "terminal": state in WORK_TERMINAL_STATES,
        "route": {key: str(route.get(key) or "") for key in ("provider", "model", "effort")},
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
        "started_at": _iso(row.started_at),
        "completed_at": _iso(row.completed_at),
        "cancelled_at": _iso(row.cancelled_at),
    }


async def list_agent_tasks(
    db_manager: Any,
    *,
    owner: str,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    from ..memory.models import AgentWorkItem

    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_LIMIT:
        raise AgentTaskListingError("invalid_limit", f"limit must be between 1 and {MAX_LIMIT}")
    if status is not None and status not in LIST_STATUSES:
        raise AgentTaskListingError("invalid_status", "status is not a known task status")
    try:
        owner_id = str(uuid.UUID(str(owner)))
    except (TypeError, ValueError):
        raise AgentTaskListingError("invalid_owner", "owner is invalid", 403) from None
    after = decode_cursor(cursor) if cursor else None

    stmt = select(AgentWorkItem).where(
        AgentWorkItem.source_type == AGENT_TASK_SOURCE_TYPE,
        AgentWorkItem.execution_adapter == AGENT_TASK_ADAPTER_KEY,
        AgentWorkItem.metadata_json["owner_user_id"].as_string() == owner_id,
    )
    if status is not None:
        admitted = AgentWorkItem.metadata_json["admission_state"].as_string() == "ready"
        pending = AgentWorkItem.state == "pending"
        states = [name for name, value in STATE_STATUS.items() if value == status and name != "pending"]
        clauses = [AgentWorkItem.state.in_(states)] if states else []
        if status == "queued":
            clauses.append(and_(pending, admitted))
        elif status == "interrupted":
            clauses.append(and_(pending, or_(AgentWorkItem.metadata_json["admission_state"].as_string().is_(None), ~admitted)))
        elif status == "unknown":
            clauses = [~AgentWorkItem.state.in_(list(STATE_STATUS))]
        stmt = stmt.where(or_(*clauses)) if clauses else stmt.where(AgentWorkItem.id.is_(None))
    if after is not None:
        created, ident = after
        stmt = stmt.where(or_(
            AgentWorkItem.created_at < created,
            and_(AgentWorkItem.created_at == created, AgentWorkItem.id < ident),
        ))
    stmt = stmt.order_by(AgentWorkItem.created_at.desc(), AgentWorkItem.id.desc()).limit(limit + 1)

    value = db_manager.get_session()
    session = await value if inspect.isawaitable(value) else value
    try:
        rows = list((await session.execute(stmt)).scalars().all())
    finally:
        await session.close()
    page, more = rows[:limit], len(rows) > limit
    items = [project_row(row) for row in page]
    return {
        "items": items,
        "limit": limit,
        "next_cursor": encode_cursor(page[-1].created_at, page[-1].id) if more and page else None,
    }
