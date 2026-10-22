"""Explicit event-time windows for past-conversation retrieval.

SQL conversation timestamps are naive UTC. Calendar words use AOITALK_TIMEZONE
(Asia/Tokyo by default), not a fact's update time or the server OS timezone.
"""

from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True)
class ConversationSearchWindow:
    name: str
    timezone_name: str
    start: datetime | None = None
    end: datetime | None = None

    @property
    def bounded(self) -> bool:
        return self.start is not None

    def sql_bounds(self) -> dict[str, datetime]:
        if not self.bounded:
            return {}
        return {
            "created_after": self.start.astimezone(timezone.utc).replace(tzinfo=None),
            "created_before": self.end.astimezone(timezone.utc).replace(tzinfo=None),
        }

    def metadata(self) -> dict[str, str | None]:
        return {
            "time_range": self.name,
            "timezone": self.timezone_name,
            "timestamp_field": "conversation_message.created_at",
            "start_inclusive": self.start.isoformat() if self.start else None,
            "end_exclusive": self.end.isoformat() if self.end else None,
        }


def conversation_search_window(
    time_range: str = "all",
    *,
    now: datetime | None = None,
    timezone_name: str | None = None,
) -> ConversationSearchWindow:
    name = str(time_range or "all").strip().casefold()
    supported = {"all", "today", "recent", "week", "month", "this_week", "this_month"}
    if name not in supported:
        raise ValueError(
            "time_range must be all, today, recent, week, month, this_week, or this_month"
        )
    zone_name = str(
        timezone_name or os.getenv("AOITALK_TIMEZONE") or "Asia/Tokyo"
    ).strip()
    try:
        zone = ZoneInfo(zone_name)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError(
            "Invalid AOITALK_TIMEZONE; configure an IANA timezone such as Asia/Tokyo"
        ) from exc
    if name == "all":
        return ConversationSearchWindow(name, zone_name)
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    end = instant.astimezone(timezone.utc)
    local = end.astimezone(zone)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    if name == "today":
        start = midnight
    elif name == "this_week":
        start = midnight - timedelta(days=midnight.weekday())
    elif name == "this_month":
        start = midnight.replace(day=1)
    else:
        start = end - timedelta(days=30 if name == "month" else 7)
    return ConversationSearchWindow(
        name, zone_name, start.astimezone(timezone.utc), end
    )
