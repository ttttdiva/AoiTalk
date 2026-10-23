"""Datetime parsing shared by task and sync APIs."""

from __future__ import annotations

from datetime import datetime, timezone


def parse_utc_naive_datetime(value: str) -> datetime:
    """Parse an ISO timestamp as UTC-naive, preserving its full precision.

    Naive timestamps retain their legacy UTC meaning. Explicit offsets and
    ``Z`` identify an instant and are converted to UTC before timezone
    information is removed for the database's naive ``DateTime`` columns.
    """

    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed
