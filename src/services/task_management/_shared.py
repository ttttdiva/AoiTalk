"""Task management shared constants, pure helpers, and domain types.

TaskManagementService から切り出したモジュールレベルの定数・純粋関数・
データクラス・例外。振る舞い保存のため本体ロジックは一切変更していない。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional
from uuid import UUID, uuid4

from dateutil.rrule import rrulestr

from ...memory.models import (
    TaskOccurrence,
    Task,
    User,
)
from ...task_time import DEFAULT_TASK_TIMEZONE, normalize_task_timezone

VALID_TASK_STATUSES = {
    "todo",
    "open",
    "in_progress",
    "blocked",
    "on_hold",
    "review",
    "closed",
    "cancelled",
}
VALID_PRIORITIES = {"low", "medium", "high", "urgent"}
DEFAULT_USER_NOTIFICATION_MINUTES = 5
DISALLOWED_PLACEHOLDER_TITLES = {"無題のタスク", "Untitled task"}
DEFAULT_MEMBER_PERMISSIONS = {
    "owner": {
        "read": True,
        "write": True,
        "delete": True,
        "manage_members": True,
        "manage_settings": True,
    },
    "admin": {
        "read": True,
        "write": True,
        "delete": True,
        "manage_members": True,
        "manage_settings": False,
    },
    "member": {
        "read": True,
        "write": True,
        "delete": False,
        "manage_members": False,
        "manage_settings": False,
    },
    "viewer": {
        "read": True,
        "write": False,
        "delete": False,
        "manage_members": False,
        "manage_settings": False,
    },
}
LEGACY_STATUS_MAP = {
    "todo": "todo",
    "open": "open",
    "in_progress": "in_progress",
    # Docs #Task の状態フィールド（todo/doing/done）からの連携値
    "doing": "in_progress",
    "paused": "on_hold",
    "blocked": "blocked",
    "on_hold": "on_hold",
    "review": "review",
    "done": "closed",
    "closed": "closed",
}

logger = logging.getLogger(__name__)


class TaskManagementError(Exception):
    """Task management domain error with HTTP-like status information."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass
class ScheduledOccurrence:
    """Pure representation of an occurrence window."""

    start_at: datetime
    end_at: datetime
    is_generated: bool
    source_kind: str


def normalize_task_status(status: str) -> str:
    """Normalize legacy and public task statuses into the current enum."""
    normalized = (status or "").strip().lower()
    mapped = LEGACY_STATUS_MAP.get(normalized, normalized)
    if mapped not in VALID_TASK_STATUSES:
        raise TaskManagementError(f"Invalid status: {status}", status_code=400)
    return mapped


def normalize_priority(priority: Optional[str]) -> str:
    """Normalize priority values."""
    normalized = (priority or "medium").strip().lower()
    if normalized == "normal":
        normalized = "medium"
    if normalized not in VALID_PRIORITIES:
        raise TaskManagementError(f"Invalid priority: {priority}", status_code=400)
    return normalized


def _ensure_reminder_offsets(
    reminder_offsets: Optional[Iterable[Any]],
    *,
    default: Optional[Iterable[int]] = None,
) -> list[int]:
    if reminder_offsets is None:
        return list(default or [])

    normalized: list[int] = []
    for offset in reminder_offsets:
        try:
            value = int(offset)
        except (TypeError, ValueError) as exc:
            raise TaskManagementError(f"Invalid reminder offset: {offset}") from exc
        if value < 0:
            raise TaskManagementError("Reminder offsets must be >= 0")
        normalized.append(value)

    unique_sorted = sorted(set(normalized))
    return unique_sorted or list(default or [])


def _normalize_task_title(title: Optional[str]) -> str:
    normalized = (title or "").strip()
    if not normalized:
        raise TaskManagementError("title is required", status_code=400)
    if normalized in DISALLOWED_PLACEHOLDER_TITLES:
        raise TaskManagementError(
            "placeholder task titles are not allowed", status_code=400
        )
    return normalized


def _strip_google_calendar_metadata(
    metadata: Optional[dict[str, Any]],
) -> dict[str, Any]:
    cleaned = dict(metadata or {})
    cleaned.pop("google_calendar", None)
    return cleaned


def _get_user_notification_minutes(user: Optional[User]) -> int:
    raw = None
    if user is not None:
        raw = (user.user_settings or {}).get("task_notification_minutes_before")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_USER_NOTIFICATION_MINUTES
    return value if value >= 0 else DEFAULT_USER_NOTIFICATION_MINUTES


def _get_user_task_notifications_default_enabled(user: Optional[User]) -> bool:
    if user is None:
        return True
    raw = (user.user_settings or {}).get("task_notifications_default_enabled")
    return raw if isinstance(raw, bool) else True


def _is_midnight(value: Optional[datetime]) -> bool:
    return (
        value is not None
        and value.hour == 0
        and value.minute == 0
        and value.second == 0
        and value.microsecond == 0
    )


def _is_date_only_occurrence(occurrence: TaskOccurrence, task: Task) -> bool:
    if occurrence.all_day or task.all_day:
        return True
    if occurrence.start_at and occurrence.end_at:
        return _is_midnight(occurrence.start_at) and _is_midnight(occurrence.end_at)
    return _is_midnight(occurrence.start_at or occurrence.end_at)


def _normalize_member_permissions(role: str, permissions: Any) -> dict[str, bool]:
    if isinstance(permissions, dict):
        return permissions
    if isinstance(permissions, str):
        try:
            parsed = json.loads(permissions)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed
    return DEFAULT_MEMBER_PERMISSIONS.get(
        role, DEFAULT_MEMBER_PERMISSIONS["member"]
    ).copy()


def build_occurrence_schedule(
    *,
    start_at: Optional[datetime],
    end_at: Optional[datetime],
    recurrence_rrule: Optional[str],
    horizon_days: int = 90,
    base_now: Optional[datetime] = None,
) -> list[ScheduledOccurrence]:
    """Pure helper that expands a task schedule into materialized occurrences."""
    if not start_at or not end_at:
        return []
    if end_at <= start_at:
        raise TaskManagementError("end_at must be after start_at", status_code=400)

    if not recurrence_rrule:
        return [
            ScheduledOccurrence(
                start_at=start_at,
                end_at=end_at,
                is_generated=False,
                source_kind="task_schedule",
            )
        ]

    duration = end_at - start_at
    now = base_now or datetime.utcnow()
    window_start = min(start_at, now - duration)
    window_end = now + timedelta(days=horizon_days)

    try:
        rule = rrulestr(recurrence_rrule, dtstart=start_at)
    except Exception as exc:
        raise TaskManagementError(f"Invalid recurrence rule: {exc}") from exc

    starts = list(rule.between(window_start, window_end, inc=True))
    if start_at not in starts and start_at <= window_end:
        starts.insert(0, start_at)

    unique_starts = sorted(set(starts))
    return [
        ScheduledOccurrence(
            start_at=occurrence_start,
            end_at=occurrence_start + duration,
            is_generated=True,
            source_kind="recurrence",
        )
        for occurrence_start in unique_starts
    ]


_TIMER_UTC_SKEW_MIN = timedelta(hours=8)
_TIMER_UTC_SKEW_MAX = timedelta(hours=10)


def correct_likely_timer_started_at(
    started_at: Optional[datetime],
    created_at: Optional[datetime],
    source: Optional[str],
) -> Optional[datetime]:
    """timer 起動時に UTC が混入した started_at を created_at で補正する。

    Web BFF（frontend/src/lib/server/db-time.ts の correctLikelyTimerStartedAt）と
    同じ発見的補正。DB はローカル壁時計時刻で保存する規約のため、
    started_at と created_at の差が 8〜10 時間ある timer エントリは
    UTC で書かれた可能性が高く、created_at を開始時刻として扱う。
    """
    if (
        source == "timer"
        and started_at is not None
        and created_at is not None
        and _TIMER_UTC_SKEW_MIN <= (created_at - started_at) <= _TIMER_UTC_SKEW_MAX
    ):
        return created_at
    return started_at


def build_time_report(entries: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Pure aggregation helper used by reports and tests."""
    summary = {
        "total_seconds": 0,
        "entry_count": 0,
        "active_entries": 0,
    }
    by_project: dict[str, dict[str, Any]] = {}
    by_day: dict[str, dict[str, Any]] = {}
    by_user: dict[str, dict[str, Any]] = {}
    by_task: dict[str, dict[str, Any]] = {}

    def _bucket(
        target: dict[str, dict[str, Any]],
        key: str,
        label: str,
        extra: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        if key not in target:
            target[key] = {
                "key": key,
                "label": label,
                "seconds": 0,
                "entries": 0,
                **(extra or {}),
            }
        return target[key]

    # DB はローカル壁時計時刻で保存しているため、実行中エントリの集計も
    # ローカル現在時刻で行う（utcnow だと JST 環境で 9 時間ずれる）。
    now = datetime.now()
    for entry in entries:
        started_at = entry.get("started_at")
        ended_at = entry.get("ended_at")
        if not isinstance(started_at, datetime):
            continue

        effective_end = ended_at if isinstance(ended_at, datetime) else now
        duration_seconds = max(0, int((effective_end - started_at).total_seconds()))
        summary["total_seconds"] += duration_seconds
        summary["entry_count"] += 1
        if ended_at is None:
            summary["active_entries"] += 1

        project_key = entry.get("project_id") or "unknown"
        project_label = entry.get("project_name") or "Unknown project"
        project_bucket = _bucket(
            by_project,
            project_key,
            project_label,
            extra={"project_id": project_key, "project_name": project_label},
        )
        project_bucket["seconds"] += duration_seconds
        project_bucket["entries"] += 1

        task_key = entry.get("task_id") or "unknown"
        task_label = entry.get("task_title") or "Unknown task"
        task_bucket = _bucket(
            by_task,
            task_key,
            task_label,
            extra={
                "project_id": entry.get("project_id"),
                "project_name": entry.get("project_name"),
            },
        )
        task_bucket["seconds"] += duration_seconds
        task_bucket["entries"] += 1

        user_key = entry.get("user_id") or "unknown"
        user_label = (
            entry.get("display_name") or entry.get("username") or "Unknown user"
        )
        user_bucket = _bucket(by_user, user_key, user_label)
        user_bucket["seconds"] += duration_seconds
        user_bucket["entries"] += 1

        day_key = started_at.date().isoformat()
        day_bucket = _bucket(by_day, day_key, day_key)
        day_bucket["seconds"] += duration_seconds
        day_bucket["entries"] += 1

    return {
        "summary": summary,
        "by_project": sorted(
            by_project.values(), key=lambda item: item["seconds"], reverse=True
        ),
        "by_day": sorted(by_day.values(), key=lambda item: item["key"]),
        "by_user": sorted(
            by_user.values(), key=lambda item: item["seconds"], reverse=True
        ),
        "by_task": sorted(
            by_task.values(), key=lambda item: item["seconds"], reverse=True
        ),
    }

