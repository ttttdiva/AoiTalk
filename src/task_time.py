"""Shared task time defaults and timer timestamp boundary helpers.

Task due dates and recurrence values intentionally keep their existing
wall-clock semantics.  The helpers in this module are for ``time_entries``
only: those rows use a naive database timestamp representing the deployment's
configured wall clock, while API responses carry an explicit RFC3339 offset.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULT_TASK_TIMEZONE = "Asia/Tokyo"

# ``AOITALK_TIMEZONE`` is deliberately separate from recurrence timezone
# normalization.  The latter is an existing product setting with legacy
# semantics and must not be changed as part of the timer boundary fix.
TIMER_TIMEZONE_ENV = "AOITALK_TIMEZONE"


def normalize_task_timezone(value: Any) -> str:
    timezone = str(value or "").strip()
    if not timezone or timezone.upper() == "UTC":
        return DEFAULT_TASK_TIMEZONE
    return timezone


def get_timer_timezone() -> ZoneInfo:
    """Return the deployment zone used by timer rows and API timestamps.

    The value is resolved on every call so tests and long-running processes
    that update their environment before a request observe the new setting.
    A malformed/unknown setting falls back to the documented default rather
    than making an otherwise healthy task API fail with a low-level
    ``ZoneInfoNotFoundError``.
    """

    configured = str(os.getenv(TIMER_TIMEZONE_ENV) or "").strip()
    name = configured or DEFAULT_TASK_TIMEZONE
    try:
        return ZoneInfo(name)
    except (KeyError, TypeError, ZoneInfoNotFoundError):
        return ZoneInfo(DEFAULT_TASK_TIMEZONE)


def timer_db_datetime(value: datetime | None) -> datetime | None:
    """Normalize a timer timestamp for the naive deployment-zone DB column.

    Naive values are legacy/manual wall-clock input and are intentionally
    preserved.  Offset-aware values identify an instant and are converted to
    the configured deployment zone before dropping their offset for storage.
    """

    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(get_timer_timezone()).replace(tzinfo=None)


def timer_api_datetime(value: datetime | None) -> str | None:
    """Serialize a timer timestamp as RFC3339 with an explicit zone offset."""

    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=get_timer_timezone())
    else:
        value = value.astimezone(get_timer_timezone())
    return value.isoformat()


def timer_api_datetime_value(value: Any) -> str | None:
    """Serialize a metadata timestamp, preserving malformed legacy values.

    ``entry_metadata`` is user-editable JSON and older rows may contain a
    string rather than a datetime object.  Valid datetime strings are routed
    through the same explicit-offset serializer as model columns; an invalid
    legacy value is returned unchanged so reading a row never destroys data.
    """

    if value is None:
        return None
    if isinstance(value, datetime):
        return timer_api_datetime(value)
    if isinstance(value, str):
        normalized = value.strip()
        try:
            if normalized.endswith("Z"):
                normalized = f"{normalized[:-1]}+00:00"
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return value
        return timer_api_datetime(parsed)
    return None


def timer_now_db() -> datetime:
    """Return current deployment-zone wall clock for a timer DB row."""

    return datetime.now(get_timer_timezone()).replace(tzinfo=None)


def timer_duration_seconds(
    started_at: datetime | None,
    ended_at: datetime | None,
    *,
    now: datetime | None = None,
) -> int:
    """Calculate timer duration using the deployment zone as an instant.

    The database representation is deliberately naive, so attaching the
    configured zone before subtraction keeps report/detail calculations
    aligned with the explicit-offset API representation (including zones with
    daylight-saving transitions).
    """

    started = timer_db_datetime(started_at)
    if started is None:
        return 0
    finished = timer_db_datetime(ended_at)
    if finished is None:
        finished = timer_db_datetime(now) if now is not None else timer_now_db()
    if finished is None:
        return 0
    zone = get_timer_timezone()
    elapsed = (
        finished.replace(tzinfo=zone).astimezone(timezone.utc)
        - started.replace(tzinfo=zone).astimezone(timezone.utc)
    ).total_seconds()
    return max(0, int(elapsed))
