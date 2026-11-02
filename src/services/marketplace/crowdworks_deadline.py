"""Conservative deadline parsing for CrowdWorks listing snapshots."""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any


_TOKYO = ZoneInfo("Asia/Tokyo")
_LABEL = r"(?:応募|募集|掲載)?(?:期限|終了予定|終了|締切|〆切)(?:日時|日)?"
_LABELED_COUNTDOWN_WITH_DATE = re.compile(
    rf"{_LABEL}(?:まで)?\s*(?:あと|残り)\s*(?P<days>\d{{1,3}})\s*日[^()（）]{{0,40}}[（(]\s*"
    r"(?P<month>\d{1,2})月\s*(?P<day>\d{1,2})日(?:まで)?\s*[）)]"
)
_FULL_DATE = re.compile(
    rf"{_LABEL}[^\d年月日]{{0,24}}(?P<year>20\d{{2}})[年./-]"
    r"(?P<month>\d{1,2})[月./-](?P<day>\d{1,2})日?"
    r"(?:\s*(?P<hour>\d{1,2}):(?P<minute>\d{2}))?"
)
_MONTH_DAY = re.compile(
    rf"{_LABEL}[^\d年月日]{{0,24}}(?P<month>\d{{1,2}})月"
    r"(?P<day>\d{1,2})日(?:\s*(?P<hour>\d{1,2}):(?P<minute>\d{2}))?"
)


def _local(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(_TOKYO)
    if value.tzinfo is None:
        # Operations timestamps are stored as naive UTC datetimes.
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(_TOKYO)


def _end_of_day(year: int, month: int, day: int, hour: int | None = None,
                minute: int | None = None) -> datetime | None:
    try:
        if hour is None:
            return datetime.combine(date(year, month, day), time.max, tzinfo=_TOKYO)
        return datetime(year, month, day, hour, minute or 0, 59, 999999, tzinfo=_TOKYO)
    except ValueError:
        return None


def _yearless_date(
    month: int,
    day: int,
    *,
    captured_at: datetime,
    relative_days: int | None = None,
    hour: int | None = None,
    minute: int | None = None,
) -> datetime | None:
    anchor = _local(captured_at)
    if relative_days is not None:
        if relative_days > 366:
            return None
        expected = anchor.date() + timedelta(days=relative_days)
        if (expected.month, expected.day) == (month, day):
            return _end_of_day(expected.year, month, day, hour, minute)
        # Conflicting countdown and calendar date is ambiguous evidence. Do
        # not prefer the date that would make the listing appear active.
        return None

    candidate = _end_of_day(anchor.year, month, day, hour, minute)
    if candidate is None:
        return None
    # Without a year, dates close to the capture date are unambiguous enough
    # for this screening gate. A date more than six months in the past may be
    # a next-year deadline, so fail closed and require verification instead.
    if candidate < anchor - timedelta(days=183):
        return None
    return candidate


def _parse_text(text: str, *, captured_at: datetime) -> datetime | None:
    normalized = unicodedata.normalize("NFKC", text)
    countdown_date = _LABELED_COUNTDOWN_WITH_DATE.search(normalized)
    if countdown_date:
        return _yearless_date(
            int(countdown_date.group("month")),
            int(countdown_date.group("day")),
            captured_at=captured_at,
            relative_days=int(countdown_date.group("days")),
        )

    full = _FULL_DATE.search(normalized)
    if full:
        return _end_of_day(
            int(full.group("year")), int(full.group("month")), int(full.group("day")),
            int(full.group("hour")) if full.group("hour") else None,
            int(full.group("minute")) if full.group("minute") else None,
        )

    month_day = _MONTH_DAY.search(normalized)
    if month_day:
        return _yearless_date(
            int(month_day.group("month")), int(month_day.group("day")),
            captured_at=captured_at,
            hour=int(month_day.group("hour")) if month_day.group("hour") else None,
            minute=int(month_day.group("minute")) if month_day.group("minute") else None,
        )

    # A relative countdown is only deadline evidence when the deadline label
    # is adjacent. Other listing prose may mention unrelated delivery dates.
    countdown = re.search(
        rf"{_LABEL}(?:まで)?\s*(?:あと|残り)\s*(?P<days>\d{{1,3}})\s*日",
        normalized,
    )
    if countdown:
        days = int(countdown.group("days"))
        if days <= 366:
            due = _local(captured_at).date() + timedelta(days=days)
            return _end_of_day(due.year, due.month, due.day)

    return None


def crowdworks_deadline(
    *,
    source_snapshot: Any = None,
    source_text: Any = None,
    captured_at: datetime | None = None,
    now: datetime | None = None,
) -> tuple[str | None, str]:
    """Return (deadline ISO-8601, state) without changing the source record.

    States are ``open``, ``expired`` and ``unverified``. Unparseable or
    ambiguous deadlines are never treated as eligible.
    """

    snapshot = source_snapshot if isinstance(source_snapshot, dict) else {}
    anchor = _local(captured_at)
    deadline: datetime | None = None
    raw_deadline = snapshot.get("deadline_at")
    if isinstance(raw_deadline, str) and raw_deadline.strip():
        try:
            deadline = _local(datetime.fromisoformat(raw_deadline.strip()))
        except ValueError:
            deadline = None

    if deadline is None:
        fragments: list[str] = []
        for key in ("deadline", "deadline_text", "deadline_display", "context"):
            value = snapshot.get(key)
            if isinstance(value, str) and value.strip():
                fragments.append(value)
        if isinstance(source_text, str) and source_text.strip():
            fragments.append(source_text)
        deadline = next(
            (parsed for fragment in fragments if (parsed := _parse_text(fragment, captured_at=anchor))),
            None,
        )

    if deadline is None:
        return None, "unverified"
    normalized_now = _local(now)
    return deadline.isoformat(), "expired" if deadline <= normalized_now else "open"


__all__ = ["crowdworks_deadline"]


def _deadline_state(row) -> tuple[str | None, str]:
    snapshot = getattr(row, "source_snapshot_json", None)
    metadata = getattr(row, "metadata_json", None)
    if isinstance(metadata, dict) and isinstance(metadata.get("deadline_at"), str):
        snapshot = {**(snapshot if isinstance(snapshot, dict) else {}), "deadline_at": metadata["deadline_at"]}
    return crowdworks_deadline(
        source_snapshot=snapshot,
        source_text=getattr(row, "source_text", None),
        captured_at=getattr(row, "created_at", None),
    )
