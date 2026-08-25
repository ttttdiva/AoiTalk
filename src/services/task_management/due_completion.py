"""期限到達時のタスク自動完了。

自動完了は通知ワーカーから呼び出すサーバ側の補助処理であり、通常タスクの
``tasks`` 行と、繰り返しタスクの ``task_occurrences`` 行を別々に更新する。
DB に保存している日時は既存のタスク機能と同じ「タイムゾーン無しの壁時計」
なので、現在時刻と終日の締切は Asia/Tokyo に正規化してから比較する。
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any, Optional
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ...memory.models import (
    Task,
    TaskAssignee,
    TaskOccurrence,
)
from ...task_time import DEFAULT_TASK_TIMEZONE
from ._shared import normalize_task_status


_CLOSED_STATUSES = {"closed", "done", "cancelled", "canceled"}
_TASK_TIMEZONE = ZoneInfo(DEFAULT_TASK_TIMEZONE)


def _is_closed_status(status: Any) -> bool:
    raw = str(status or "").strip().lower()
    if raw in _CLOSED_STATUSES:
        return True
    try:
        return normalize_task_status(raw) == "closed"
    except Exception:
        # Unknown legacy values are incomplete rather than silently treated as
        # completed, which is the safe behavior for parent auto-close.
        return False


def _task_due_at(
    *,
    start_at: Optional[datetime],
    end_at: Optional[datetime],
    all_day: bool,
) -> Optional[datetime]:
    """Return the effective due instant for a task/occurrence.

    All-day schedules use an inclusive calendar day.  New clients normally
    persist ``[day 00:00, next-day 00:00)`` while older rows may use equal
    midnight endpoints, therefore both shapes map to ``23:59:59`` of the
    intended day.  Timed schedules retain their explicit ``end_at`` value.
    """

    # Task rows use naive wall-clock timestamps, but sync/import callers may
    # still hand us aware values.  Normalize both endpoints before taking the
    # calendar date or comparing against the worker's naive local ``now``.
    start_at = _local_wall_clock(start_at)
    end_at = _local_wall_clock(end_at)
    # A missing end is not a due date.  In particular, an all-day row with
    # only ``start_at`` is an open-ended plan and must never be auto-closed.
    if end_at is None:
        return None
    if not all_day:
        return end_at

    if start_at is not None and end_at is not None and end_at > start_at:
        # Subtract a microsecond rather than a full second so an end value such
        # as 00:00:00.500 still belongs to the previous local calendar day.
        due_date = (end_at - timedelta(microseconds=1)).date()
    elif start_at is not None:
        due_date = start_at.date()
    else:
        due_date = end_at.date()
    return datetime.combine(due_date, time(23, 59, 59))


def _auto_close_now(now: Optional[datetime] = None) -> datetime:
    if now is None:
        return datetime.now(_TASK_TIMEZONE).replace(tzinfo=None)
    if now.tzinfo is not None:
        return now.astimezone(_TASK_TIMEZONE).replace(tzinfo=None)
    return now


def _local_wall_clock(value: Optional[datetime]) -> Optional[datetime]:
    """Convert an optional datetime to the task DB's naive Asia/Tokyo clock."""

    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(_TASK_TIMEZONE).replace(tzinfo=None)
    return value


def _all_rows(result: Any) -> list[Any]:
    """Read scalar rows from both SQLAlchemy and the small test doubles."""

    scalars = result.scalars()
    unique = getattr(scalars, "unique", None)
    if callable(unique):
        scalars = unique()
    rows = getattr(scalars, "all", None)
    if callable(rows):
        return list(rows())
    return list(scalars)


class DueCompletionMixin:
    """期限に到達した opt-in タスクを一度だけ完了へ遷移させる。"""

    async def _incomplete_direct_children(
        self,
        session: AsyncSession,
        *,
        task_id: UUID,
    ) -> list[Task]:
        # Parent and child rows are locked in the same transaction.  This is
        # important when a user closes a child concurrently with the worker:
        # whichever transaction obtains the row lock first provides the
        # authoritative completion state.
        result = await session.execute(
            select(Task)
            .where(
                Task.parent_task_id == task_id,
                Task.deleted_at.is_(None),
            )
            .with_for_update()
        )
        return [
            child
            for child in _all_rows(result)
            if not _is_closed_status(getattr(child, "status", "todo"))
        ]

    @staticmethod
    def _is_due(
        item: Any,
        *,
        now: datetime,
        task: Optional[Any] = None,
    ) -> tuple[bool, Optional[datetime]]:
        due_at = _task_due_at(
            start_at=getattr(item, "start_at", None),
            end_at=getattr(item, "end_at", None),
            all_day=bool(
                getattr(item, "all_day", False)
                or (getattr(task, "all_day", False) if task is not None else False)
            ),
        )
        return due_at is not None and due_at <= now, due_at

    @staticmethod
    def _task_broadcast_payload(task: Any) -> dict[str, Any]:
        try:
            return task.to_dict()
        except Exception:
            return {
                "id": str(getattr(task, "id", "")),
                "status": getattr(task, "status", None),
                "completed_at": (
                    getattr(task, "completed_at", None).isoformat()
                    if getattr(task, "completed_at", None)
                    else None
                ),
                "auto_close_on_due": bool(
                    getattr(task, "auto_close_on_due", False)
                ),
            }

    @staticmethod
    def _occurrence_broadcast_payload(occurrence: Any) -> dict[str, Any]:
        try:
            return occurrence.to_dict()
        except Exception:
            return {
                "id": str(getattr(occurrence, "id", "")),
                "task_id": str(getattr(occurrence, "task_id", "")),
                "status": getattr(occurrence, "status", None),
                "start_at": (
                    getattr(occurrence, "start_at", None).isoformat()
                    if getattr(occurrence, "start_at", None)
                    else None
                ),
                "end_at": (
                    getattr(occurrence, "end_at", None).isoformat()
                    if getattr(occurrence, "end_at", None)
                    else None
                ),
            }

    async def auto_close_due_tasks(
        self,
        session: AsyncSession,
        *,
        now: Optional[datetime] = None,
    ) -> dict[str, int]:
        """Close due opt-in tasks/occurrences under row locks.

        Returns counters suitable for the notification worker.  Re-running the
        method is idempotent: closed rows are filtered before locking and are
        checked again after the lock, so no duplicate activity or broadcast is
        emitted.
        """

        current_time = _auto_close_now(now)
        # A one-day look-ahead is required for the canonical all-day shape,
        # whose end_at is next-day midnight while its effective due is today
        # 23:59:59.  The Python check below remains authoritative for timed rows.
        candidate_until = current_time + timedelta(days=1)
        open_statuses = tuple(_CLOSED_STATUSES)
        task_result = await session.execute(
            select(Task)
            .options(
                selectinload(Task.project),
                selectinload(Task.assignees).selectinload(TaskAssignee.user),
                selectinload(Task.recurrence_rule),
            )
            .where(
                Task.auto_close_on_due.is_(True),
                Task.deleted_at.is_(None),
                Task.archived_at.is_(None),
                Task.end_at <= candidate_until,
                ~Task.status.in_(open_statuses),
                # Recurring parents stay open; their materialized occurrences
                # are the completion units handled by the second query.
                ~Task.recurrence_rule.has(),
            )
            .with_for_update(skip_locked=True)
        )
        task_candidates = _all_rows(task_result)

        occurrence_result = await session.execute(
            select(TaskOccurrence)
            .join(Task, Task.id == TaskOccurrence.task_id)
            .options(
                selectinload(TaskOccurrence.task).selectinload(Task.project),
                selectinload(TaskOccurrence.task)
                .selectinload(Task.assignees)
                .selectinload(TaskAssignee.user),
            )
            .where(
                Task.auto_close_on_due.is_(True),
                Task.deleted_at.is_(None),
                Task.archived_at.is_(None),
                TaskOccurrence.deleted_at.is_(None),
                TaskOccurrence.end_at <= candidate_until,
                ~TaskOccurrence.status.in_(open_statuses),
            )
            .with_for_update(skip_locked=True)
        )
        occurrence_candidates = _all_rows(occurrence_result)

        stats = {
            "auto_closed": 0,
            "tasks_closed": 0,
            "occurrences_closed": 0,
            "skipped_incomplete_subtasks": 0,
        }
        changed_tasks: list[Task] = []
        changed_occurrences: list[TaskOccurrence] = []
        child_cache: dict[UUID, bool] = {}

        for task in task_candidates:
            # Defensive status check handles legacy values (for example
            # ``done``) that are not covered by the SQL predicate.
            if (
                _is_closed_status(getattr(task, "status", ""))
                or getattr(task, "archived_at", None) is not None
                or getattr(task, "deleted_at", None) is not None
            ):
                continue
            due, due_at = self._is_due(task, now=current_time)
            if not due:
                continue
            task_id = task.id
            incomplete = await self._incomplete_direct_children(
                session, task_id=task_id
            )
            if incomplete:
                stats["skipped_incomplete_subtasks"] += 1
                continue
            completion_time = current_time
            task.status = "closed"
            task.completed_at = completion_time
            task.updated_at = completion_time
            await self._record_activity(
                session,
                task_id=task_id,
                activity_type="task_auto_closed_on_due",
                user_id=None,
                payload={
                    "due_at": due_at.isoformat() if due_at else None,
                    "auto_close_on_due": True,
                },
            )
            changed_tasks.append(task)
            stats["auto_closed"] += 1
            stats["tasks_closed"] += 1

        for occurrence in occurrence_candidates:
            task = getattr(occurrence, "task", None)
            if task is None:
                continue
            if getattr(occurrence, "source_kind", None) == "recurrence_skip":
                continue
            if (
                _is_closed_status(getattr(task, "status", ""))
                or getattr(task, "archived_at", None) is not None
                or getattr(task, "deleted_at", None) is not None
            ):
                continue
            if (
                _is_closed_status(getattr(occurrence, "status", ""))
                or getattr(occurrence, "archived_at", None) is not None
                or getattr(occurrence, "deleted_at", None) is not None
            ):
                continue
            due, due_at = self._is_due(occurrence, now=current_time, task=task)
            if not due:
                continue
            task_id = task.id
            if task_id not in child_cache:
                child_cache[task_id] = bool(
                    await self._incomplete_direct_children(
                        session, task_id=task_id
                    )
                )
            if child_cache[task_id]:
                stats["skipped_incomplete_subtasks"] += 1
                continue
            occurrence.status = "closed"
            occurrence.updated_at = current_time
            await self._record_activity(
                session,
                task_id=task_id,
                activity_type="occurrence_auto_closed_on_due",
                user_id=None,
                payload={
                    "occurrence_id": str(occurrence.id),
                    "due_at": due_at.isoformat() if due_at else None,
                    "auto_close_on_due": True,
                },
            )
            changed_occurrences.append(occurrence)
            stats["auto_closed"] += 1
            stats["occurrences_closed"] += 1

        if changed_tasks or changed_occurrences:
            await session.commit()
            for task in changed_tasks:
                await self._broadcast(
                    "task_updated", self._task_broadcast_payload(task)
                )
            for occurrence in changed_occurrences:
                await self._broadcast(
                    "task_occurrence_updated",
                    self._occurrence_broadcast_payload(occurrence),
                )

        return stats

    # Compatibility aliases keep the worker/service seam easy to exercise from
    # integrations that use a private helper naming convention.
    async def _auto_close_due_tasks(
        self,
        session: AsyncSession,
        *,
        now: Optional[datetime] = None,
    ) -> dict[str, int]:
        return await self.auto_close_due_tasks(session, now=now)

    async def close_due_tasks(
        self,
        session: AsyncSession,
        *,
        now: Optional[datetime] = None,
    ) -> dict[str, int]:
        return await self.auto_close_due_tasks(session, now=now)


__all__ = ["DueCompletionMixin", "_auto_close_now", "_task_due_at"]
