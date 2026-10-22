"""Private in-app reminder drafts for verified, incomplete deadlines (Stage 1: draft only).

The reminder time is never calculated. A human records an explicit selection (server
timestamped, IANA zone + offset validated); the agent-facing ``propose`` only accepts a
request that matches such a selection. Nothing is scheduled or delivered here: the result
is always ``awaiting_user_review`` / ``owner_private_in_app`` / ``scheduled: false``.
Same owner advisory lock serializes every mutation before any read.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select, text

from ..memory.models.personal_reminder import PersonalReminderEvent as Event
from .personal_draft_service import digest
from .personal_ledger_service import (
    LedgerActor, LedgerConflictError, LedgerNotFoundError, LedgerPermissionError,
    LedgerValidationError, MAX_STATE_ENTRIES, PersonalLedgerService, derive_state, _uuid,
)

_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_ZONE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_+-]*(/[A-Za-z0-9_+-]+){0,2}$")
MAX_LEAD = timedelta(days=366 * 5)
_RESULT = {"status": "awaiting_user_review", "delivery": "owner_private_in_app", "scheduled": False}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def parse_remind_at(remind_at, time_zone, *, now: datetime) -> tuple[datetime, str]:
    """Return (UTC instant, canonical zone). Offset must match the zone at that instant."""
    if not isinstance(time_zone, str) or not 1 <= len(time_zone) <= 80 or not _ZONE_RE.fullmatch(time_zone):
        raise LedgerValidationError("invalid time_zone")
    try:
        zone = ZoneInfo(time_zone)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise LedgerValidationError("unknown IANA time_zone") from None
    if not isinstance(remind_at, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?(Z|[+-]\d{2}:\d{2})", remind_at):
        raise LedgerValidationError("remind_at must be an RFC 3339 date-time with an explicit offset")
    try:
        moment = datetime.fromisoformat(remind_at.replace("Z", "+00:00"))
    except ValueError:
        raise LedgerValidationError("invalid remind_at") from None
    if moment.utcoffset() != moment.astimezone(zone).utcoffset():
        raise LedgerValidationError("remind_at offset does not match time_zone")
    utc = moment.astimezone(timezone.utc)
    if utc <= now:
        raise LedgerValidationError("remind_at must be in the future")
    if utc - now > MAX_LEAD:
        raise LedgerValidationError("remind_at is too far in the future")
    return utc, time_zone


def _key(operation: str, value) -> str:
    if not isinstance(value, str) or not _KEY_RE.fullmatch(value):
        raise LedgerValidationError("invalid idempotency_key")
    return f"{operation}:{value}"


def _confirmed(value) -> str:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
        raise LedgerValidationError("invalid confirmed_digest")
    return value


class PersonalReminderService:
    def __init__(self, session_factory, *, clock=_now):
        self.ledger = PersonalLedgerService(session_factory)
        self._clock = clock

    @staticmethod
    async def _lock(session, owner):
        key = int.from_bytes(hashlib.sha256(b"reminder" + owner.bytes).digest()[:4], "big", signed=True)
        await session.execute(text("SELECT pg_advisory_xact_lock(1346651221, :key)"), {"key": key})

    @staticmethod
    async def _by_key(session, owner, key):
        return (await session.execute(select(Event).where(
            Event.owner_user_id == owner, Event.operation_key == key))).scalar_one_or_none()

    @staticmethod
    def _replay(row, request):
        if row.payload["request_digest"] != digest(request):
            raise LedgerConflictError("operation key has different content")
        return row.payload["result"]

    async def _require_eligible(self, session, owner, candidate_id, confirmed_digest, guard=None):
        entries = await self.ledger._select_entries(
            session, owner, ledger="deadline", subject_key=None, limit=MAX_STATE_ENTRIES + 1, guard=guard)
        if len(entries) > MAX_STATE_ENTRIES:
            raise LedgerValidationError("ledger too large to derive completely")
        if not any(e.id == candidate_id for e in entries):
            raise LedgerNotFoundError()  # absent or another owner's: indistinguishable
        current = next((d for d in derive_state(entries).deadlines if d.candidate_entry_id == candidate_id), None)
        if current is None:
            raise LedgerConflictError("stale candidate")  # corrected or cancelled
        if current.needs_review:
            raise LedgerConflictError("candidate needs review")
        if current.status != "verified":
            raise LedgerConflictError("candidate is not verified")
        if current.completed_on is not None:
            raise LedgerConflictError("deadline is already completed")
        if current.confirm_digest != confirmed_digest:
            raise LedgerConflictError("confirmed digest does not match the current candidate")

    async def record_selection(self, *, actor: LedgerActor, candidate_entry_id, confirmed_digest,
                               remind_at, time_zone, idempotency_key):
        """Human-only: the owner picked this time on screen. Server records the timestamp."""
        if not isinstance(actor, LedgerActor) or actor.kind != "user_action":
            raise LedgerPermissionError("interactive owner action required")
        candidate = _uuid(candidate_entry_id, "candidate_entry_id")
        confirmed = _confirmed(confirmed_digest)
        utc, zone = parse_remind_at(remind_at, time_zone, now=self._clock())
        key = _key("reminder_select", idempotency_key)
        selection = {"remind_at_utc": utc.isoformat(), "time_zone": zone}
        request = {"candidate_entry_id": str(candidate), "confirmed_digest": confirmed, **selection}
        session = await self.ledger._open()
        try:
            async with session.begin():
                await self._lock(session, actor.user_id)
                existing = await self._by_key(session, actor.user_id, key)
                if existing:
                    return self._replay(existing, request)
                await self._require_eligible(session, actor.user_id, candidate, confirmed)
                selection_id = uuid4()
                result = {"selection_ref": str(selection_id)}
                row = Event(id=selection_id, owner_user_id=actor.user_id, event_type="selection",
                            candidate_entry_id=candidate, operation_key=key)
                row.payload = {"request_digest": digest(request), "result": result, **selection}
                session.add(row)
                await session.flush()
                return result
        finally:
            await session.close()

    async def propose(self, *, actor: LedgerActor, candidate_entry_id, confirmed_digest, remind_at,
                      time_zone, idempotency_key, guard=None):
        if not isinstance(actor, LedgerActor) or actor.kind not in {"agent", "user_action"}:
            raise LedgerPermissionError("invalid actor")
        candidate = _uuid(candidate_entry_id, "candidate_entry_id")
        confirmed = _confirmed(confirmed_digest)
        utc, zone = parse_remind_at(remind_at, time_zone, now=self._clock())
        key = _key("reminder_propose", idempotency_key)
        request = {"candidate_entry_id": str(candidate), "confirmed_digest": confirmed,
                   "remind_at_utc": utc.isoformat(), "time_zone": zone}
        session = await self.ledger._open()
        try:
            if guard is not None:
                guard()
            async with session.begin():
                await self._lock(session, actor.user_id)
                if guard is not None:
                    guard()
                existing = await self._by_key(session, actor.user_id, key)
                if guard is not None:
                    guard()
                if existing:
                    return self._replay(existing, request)
                await self._require_eligible(session, actor.user_id, candidate, confirmed, guard)
                selections = (await session.execute(select(Event).where(
                    Event.owner_user_id == actor.user_id, Event.event_type == "selection",
                    Event.candidate_entry_id == candidate).order_by(Event.recorded_at, Event.id))).scalars().all()
                if guard is not None:
                    guard()
                match = next((s for s in selections if s.payload["remind_at_utc"] == request["remind_at_utc"]
                              and s.payload["time_zone"] == zone), None)
                if match is None:
                    raise LedgerPermissionError("remind_at was not selected by the owner")
                taken = (await session.execute(select(Event.id).where(
                    Event.owner_user_id == actor.user_id, Event.selection_ref == match.id))).first()
                if guard is not None:
                    guard()
                if taken:
                    raise LedgerConflictError("selection already has a draft")
                draft_id = uuid4()
                result = {"draft_ref": str(draft_id), **_RESULT}
                row = Event(id=draft_id, owner_user_id=actor.user_id, event_type="proposed",
                            candidate_entry_id=candidate, selection_ref=match.id, operation_key=key)
                row.payload = {"request_digest": digest(request), "result": result,
                               "proposed_by": actor.kind}
                session.add(row)
                await session.flush()
                if guard is not None:
                    guard()  # raising here rolls back before COMMIT
                return result
        finally:
            await session.close()
