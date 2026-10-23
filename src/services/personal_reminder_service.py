"""Owner-private in-app reminder lifecycle for verified, incomplete deadlines.

The reminder time is never calculated. A human records an explicit selection (server
timestamped, IANA zone + offset validated); the agent-facing ``propose`` only accepts a
request that matches such a selection and creates an unapproved draft. Schedule/cancel/ack
are human-only. Due delivery is a private inbox row represented by an encrypted append-only
event; no external notification transport is used. The owner advisory lock serializes
lifecycle operations, and the ledger subject lock fences candidate changes against the final
eligibility check.
"""

from __future__ import annotations

import hashlib
import inspect
import re
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select, text

from ..memory.models.personal_ledger import PersonalLedgerEntry
from ..memory.models.personal_reminder import PersonalReminderEvent as Event
from .personal_draft_service import digest
from .personal_async_guard import run_personal_guard
from .personal_ledger_service import (
    LedgerActor, LedgerConflictError, LedgerError, LedgerNotFoundError, LedgerPermissionError,
    LedgerValidationError, MAX_STATE_ENTRIES, PersonalLedgerService, derive_state, _uuid,
    _view as _ledger_view,
)

_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_ZONE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_+-]*(/[A-Za-z0-9_+-]+){0,2}$")
MAX_LEAD = timedelta(days=366 * 5)
_RESULT = {
    "status": "awaiting_user_review",
    "delivery": "owner_private_in_app",
    "scheduled": False,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def parse_remind_at(remind_at, time_zone, *, now: datetime, require_future: bool = True) -> tuple[datetime, str]:
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
    if require_future and utc <= now:
        raise LedgerValidationError("remind_at must be in the future")
    if require_future and utc - now > MAX_LEAD:
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


class PersonalReminderUnavailable(LedgerError):
    """The delivery worker cannot accept a new schedule at this moment."""

    code = "personal_reminder_unavailable"


class PersonalReminderService:
    def __init__(self, session_factory, *, clock=_now):
        self.ledger = PersonalLedgerService(session_factory)
        self._clock = clock

    async def schema_available(self) -> bool:
        """Check the staged lifecycle column without selecting or decrypting row data."""
        session = await self.ledger._open()
        try:
            await session.execute(text("SELECT draft_ref FROM personal_reminder_events LIMIT 0"))
            return True
        except Exception:
            # Missing table/0105 column/permission is unavailable, never a reason to
            # report a reminder as scheduled or delivered.
            return False
        finally:
            await session.close()

    @staticmethod
    async def _lock(session, owner):
        key = int.from_bytes(hashlib.sha256(b"reminder" + owner.bytes).digest()[:4], "big", signed=True)
        await session.execute(text("SELECT pg_advisory_xact_lock(1346651221, :key)"), {"key": key})

    @staticmethod
    async def _by_key(session, owner, key):
        return (await session.execute(select(Event).where(
            Event.owner_user_id == owner, Event.operation_key == key))).scalar_one_or_none()

    @staticmethod
    async def _owned_draft(session, owner, draft_ref):
        # Filter by authenticated owner and event type before decrypting the payload.
        row = (await session.execute(select(Event).where(
            Event.owner_user_id == owner, Event.id == draft_ref, Event.event_type == "proposed",
        ))).scalar_one_or_none()
        if row is None:
            raise LedgerNotFoundError()
        return row

    async def _lock_candidate_subject(self, session, owner, candidate_id):
        # Match PersonalLedgerService's subject lock so candidate correction/completion
        # cannot commit between the current-state check and schedule/delivery append.
        row = (await session.execute(select(
            PersonalLedgerEntry.ledger, PersonalLedgerEntry.entry_type, PersonalLedgerEntry.subject_key,
        ).where(
            PersonalLedgerEntry.owner_user_id == owner,
            PersonalLedgerEntry.id == candidate_id,
        ))).first()
        if row is None or row.ledger != "deadline" or row.entry_type != "deadline_candidate":
            raise LedgerNotFoundError()
        await self.ledger._lock_subject(session, owner, "deadline", row.subject_key)
        return row.subject_key

    @staticmethod
    async def _require_worker_ready(worker_ready) -> None:
        """Reject only a new schedule when the delivery worker is unavailable.

        This check belongs inside the owner transaction, after the operation-key
        lookup. A lost response can therefore replay a committed schedule even
        after the worker has gone down, while a fresh key cannot append a schedule.
        """
        if worker_ready is None:
            return
        try:
            ready = worker_ready() if callable(worker_ready) else worker_ready
            if inspect.isawaitable(ready):
                ready = await ready
        except Exception:
            ready = False
        if ready is not True:
            raise PersonalReminderUnavailable()

    @staticmethod
    async def _draft_events(session, owner, draft_ref):
        # Owner filter precedes payload decryption for every event in this lifecycle.
        return (await session.execute(select(Event).where(
            Event.owner_user_id == owner, Event.draft_ref == draft_ref,
        ).order_by(Event.recorded_at, Event.id))).scalars().all()

    @staticmethod
    async def _append_event(session, *, owner, event_type, candidate_id, draft_ref,
                            operation_key, payload, event_id=None):
        row = Event(id=event_id or uuid4(), owner_user_id=owner, event_type=event_type,
                    candidate_entry_id=candidate_id, draft_ref=draft_ref,
                    operation_key=operation_key)
        row.payload = payload
        session.add(row)
        await session.flush()
        return row

    @staticmethod
    def _state(events):
        schedules = [row for row in events if row.event_type == "scheduled"]
        terminals = [row for row in events if row.event_type in {"cancelled", "delivered", "invalidated"}]
        acknowledgements = [row for row in events if row.event_type == "acknowledged"]
        if len(schedules) > 1 or len(terminals) > 1 or len(acknowledgements) > 1:
            raise LedgerConflictError("duplicate reminder lifecycle transition")
        scheduled = schedules[0] if schedules else None
        terminal = terminals[0] if terminals else None
        acknowledged = acknowledgements[0] if acknowledgements else None
        if any(row.candidate_entry_id != events[0].candidate_entry_id for row in events):
            raise LedgerConflictError("reminder lifecycle candidate mismatch")
        if acknowledged is not None and (terminal is None or terminal.event_type != "delivered" or scheduled is None):
            raise LedgerConflictError("invalid reminder lifecycle")
        if terminal is not None and terminal.event_type == "delivered" and scheduled is None:
            raise LedgerConflictError("delivery requires a scheduled reminder")
        if terminal is not None:
            status = "acknowledged" if acknowledged is not None else terminal.event_type
            return status, scheduled, terminal, acknowledged
        if scheduled is not None:
            return "scheduled", scheduled, None, None
        if events:
            raise LedgerConflictError("invalid reminder lifecycle")
        return "awaiting_user_review", None, None, None

    @staticmethod
    def _draft_state(draft, events):
        if any(row.candidate_entry_id != draft.candidate_entry_id for row in events):
            raise LedgerConflictError("reminder lifecycle candidate mismatch")
        return PersonalReminderService._state(events)

    @staticmethod
    def _draft_confirmation_digest(payload):
        return digest({
            "candidate_entry_id": payload["candidate_entry_id"],
            "confirmed_digest": payload["confirmed_digest"],
            "remind_at_utc": payload["remind_at_utc"],
            "time_zone": payload["time_zone"],
        })

    @staticmethod
    def _draft_payload_complete(draft, payload):
        if not isinstance(payload, dict):
            return False
        if payload.get("candidate_entry_id") != str(draft.candidate_entry_id):
            return False
        if not isinstance(payload.get("confirmed_digest"), str) or not _DIGEST_RE.fullmatch(payload["confirmed_digest"]):
            return False
        if not isinstance(payload.get("subject_key"), str) or not isinstance(payload.get("due_date"), str):
            return False
        if not isinstance(payload.get("time_zone"), str) or not 1 <= len(payload["time_zone"]) <= 80:
            return False
        try:
            ZoneInfo(payload["time_zone"])
            when = datetime.fromisoformat(payload["remind_at_utc"])
        except (KeyError, TypeError, ValueError, ZoneInfoNotFoundError, OSError):
            return False
        return when.tzinfo is not None and when.utcoffset() is not None

    @staticmethod
    def _snapshot_matches(payload, current):
        return (
            payload.get("subject_key") == current.subject_key
            and payload.get("due_date") == current.due_date
            and payload.get("label") == current.label
        )

    async def _invalidate_draft(self, session, *, owner, draft, scheduled):
        result = {"draft_ref": str(draft.id), "status": "invalidated",
                  "delivery": "owner_private_in_app", "scheduled": False,
                  "was_scheduled": scheduled is not None}
        await self._append_event(session, owner=owner, event_type="invalidated",
            candidate_id=draft.candidate_entry_id, draft_ref=draft.id,
            operation_key=f"reminder_invalidate:{draft.id}",
            payload={"request_digest": digest({"draft_ref": str(draft.id),
                "reason": "candidate_changed"}), "result": result,
                "reason": "candidate_changed"})

    @staticmethod
    def _replay(row, request):
        payload = row.payload
        if not isinstance(payload, dict) or payload.get("request_digest") != digest(request):
            raise LedgerConflictError("operation key has different content")
        if "result" not in payload:
            raise LedgerConflictError("stored reminder operation result is invalid")
        return payload["result"]

    @staticmethod
    def _replay_proposal(row, request):
        """Replay the stable four-key ``private.reminder.propose`` result.

        Lifecycle listing uses ``was_scheduled`` as a view field.  It must never
        leak into the existing agent-facing proposal contract, including rows
        written by an earlier lifecycle build that persisted that field.
        """
        raw = PersonalReminderService._replay(row, request)
        if not isinstance(raw, dict):
            raise LedgerConflictError("stored reminder proposal result is invalid")
        try:
            draft_ref = UUID(raw["draft_ref"])
        except (KeyError, TypeError, ValueError, AttributeError):
            raise LedgerConflictError("stored reminder proposal result is invalid") from None
        if (raw.get("status") != "awaiting_user_review"
                or raw.get("delivery") != "owner_private_in_app"
                or raw.get("scheduled") is not False):
            raise LedgerConflictError("stored reminder proposal result is invalid")
        return {
            "draft_ref": str(draft_ref),
            "status": "awaiting_user_review",
            "delivery": "owner_private_in_app",
            "scheduled": False,
        }

    async def _require_eligible(self, session, owner, candidate_id, confirmed_digest, guard=None):
        rows = (await session.execute(select(PersonalLedgerEntry).where(
            PersonalLedgerEntry.owner_user_id == owner,
            PersonalLedgerEntry.ledger == "deadline",
        ).order_by(PersonalLedgerEntry.seq).limit(MAX_STATE_ENTRIES + 1))).scalars().all()
        if guard is not None:
            # Run immediately after the DB await and before any encrypted payload read.
            await run_personal_guard(guard)
        entries = [_ledger_view(row) for row in rows]
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
        return current

    @staticmethod
    def _is_stale_conflict(exc):
        return str(exc) in {
            "stale candidate", "candidate needs review", "candidate is not verified",
            "deadline is already completed", "confirmed digest does not match the current candidate",
        }

    async def record_selection(self, *, actor: LedgerActor, candidate_entry_id, confirmed_digest,
                               remind_at, time_zone, idempotency_key):
        """Human-only: the owner picked this time on screen. Server records the timestamp."""
        if not isinstance(actor, LedgerActor) or actor.kind != "user_action":
            raise LedgerPermissionError("interactive owner action required")
        candidate = _uuid(candidate_entry_id, "candidate_entry_id")
        confirmed = _confirmed(confirmed_digest)
        utc, zone = parse_remind_at(remind_at, time_zone, now=self._clock(), require_future=False)
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
                now = self._clock()
                if utc <= now or utc - now > MAX_LEAD:
                    raise LedgerValidationError("remind_at is outside the allowed future window")
                await self._lock_candidate_subject(session, actor.user_id, candidate)
                await self._require_eligible(session, actor.user_id, candidate, confirmed)
                selection_id = uuid4()
                result = {"selection_ref": str(selection_id)}
                row = Event(id=selection_id, owner_user_id=actor.user_id, event_type="selection",
                            candidate_entry_id=candidate, operation_key=key)
                row.payload = {"request_digest": digest(request), "result": result,
                               "confirmed_digest": confirmed, **selection}
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
        # Permit normalized replay after the chosen time has passed. New proposals below
        # still enforce the future window before any insert.
        utc, zone = parse_remind_at(remind_at, time_zone, now=self._clock(), require_future=False)
        key = _key("reminder_propose", idempotency_key)
        request = {"candidate_entry_id": str(candidate), "confirmed_digest": confirmed,
                   "remind_at_utc": utc.isoformat(), "time_zone": zone}
        session = await self.ledger._open()
        try:
            if guard is not None:
                await run_personal_guard(guard)
            async with session.begin():
                await self._lock(session, actor.user_id)
                if guard is not None:
                    await run_personal_guard(guard)
                existing = await self._by_key(session, actor.user_id, key)
                if guard is not None:
                    await run_personal_guard(guard)
                if existing:
                    return self._replay_proposal(existing, request)
                now = self._clock()
                if utc <= now or utc - now > MAX_LEAD:
                    raise LedgerValidationError("remind_at is outside the allowed future window")
                await self._lock_candidate_subject(session, actor.user_id, candidate)
                if guard is not None:
                    await run_personal_guard(guard)
                current = await self._require_eligible(session, actor.user_id, candidate, confirmed, guard)
                selections = (await session.execute(select(Event).where(
                    Event.owner_user_id == actor.user_id, Event.event_type == "selection",
                    Event.candidate_entry_id == candidate).order_by(Event.recorded_at, Event.id))).scalars().all()
                if guard is not None:
                    await run_personal_guard(guard)
                match = next((s for s in selections if s.payload["remind_at_utc"] == request["remind_at_utc"]
                              and s.payload["time_zone"] == zone
                              and s.payload.get("confirmed_digest") == confirmed), None)
                if match is None:
                    raise LedgerPermissionError("remind_at was not selected by the owner")
                taken = (await session.execute(select(Event.id).where(
                    Event.owner_user_id == actor.user_id, Event.selection_ref == match.id))).first()
                if guard is not None:
                    await run_personal_guard(guard)
                if taken:
                    raise LedgerConflictError("selection already has a draft")
                draft_id = uuid4()
                result = {"draft_ref": str(draft_id), **_RESULT}
                row = Event(id=draft_id, owner_user_id=actor.user_id, event_type="proposed",
                            candidate_entry_id=candidate, selection_ref=match.id, operation_key=key)
                row.payload = {"request_digest": digest(request), "result": result,
                               "proposed_by": actor.kind, "candidate_entry_id": str(candidate),
                               "confirmed_digest": confirmed, "remind_at_utc": utc.isoformat(),
                               "time_zone": zone, "subject_key": current.subject_key,
                               "due_date": current.due_date, "label": current.label}
                session.add(row)
                await session.flush()
                if guard is not None:
                    await run_personal_guard(guard)  # raising here rolls back before COMMIT
                return result
        finally:
            await session.close()

    async def schedule(self, *, actor: LedgerActor, draft_ref, confirmed_digest, idempotency_key,
                       worker_ready=None):
        """Human-only approval of the exact draft content shown in the private UI."""
        if not isinstance(actor, LedgerActor) or actor.kind != "user_action":
            raise LedgerPermissionError("interactive owner action required")
        draft_id = _uuid(draft_ref, "draft_ref")
        confirmed = _confirmed(confirmed_digest)
        key = _key("reminder_schedule", idempotency_key)
        request = {"draft_ref": str(draft_id), "confirmed_digest": confirmed}
        session = await self.ledger._open()
        try:
            async with session.begin():
                await self._lock(session, actor.user_id)
                existing = await self._by_key(session, actor.user_id, key)
                if existing:
                    return self._replay(existing, request)
                # Keep this gate under the owner lock and after the operation-key
                # lookup. Existing same-key rows were replayed above, so a worker
                # outage cannot turn a lost-response retry into a false 503.
                await self._require_worker_ready(worker_ready)
                draft = await self._owned_draft(session, actor.user_id, draft_id)
                payload = draft.payload
                if not self._draft_payload_complete(draft, payload):
                    raise LedgerConflictError("draft is no longer reviewable")
                if self._draft_confirmation_digest(payload) != confirmed:
                    raise LedgerConflictError("draft content changed after review")
                candidate = UUID(payload["candidate_entry_id"])
                await self._lock_candidate_subject(session, actor.user_id, candidate)
                current = await self._require_eligible(session, actor.user_id, candidate, payload["confirmed_digest"])
                if not self._snapshot_matches(payload, current):
                    raise LedgerConflictError("draft content changed after review")
                events = await self._draft_events(session, actor.user_id, draft_id)
                state, _scheduled, _terminal, _ack = self._draft_state(draft, events)
                if state != "awaiting_user_review":
                    raise LedgerConflictError("draft is no longer awaiting approval")
                selected = (await session.execute(select(Event).where(
                    Event.owner_user_id == actor.user_id, Event.id == draft.selection_ref,
                    Event.event_type == "selection",
                ))).scalar_one_or_none()
                if selected is None:
                    raise LedgerConflictError("owner time selection is unavailable")
                selected_payload = selected.payload
                if (selected_payload.get("remind_at_utc") != payload["remind_at_utc"]
                        or selected_payload.get("time_zone") != payload["time_zone"]
                        or selected_payload.get("confirmed_digest") != payload["confirmed_digest"]):
                    raise LedgerConflictError("owner time selection changed")
                remind_at = datetime.fromisoformat(payload["remind_at_utc"])
                if remind_at.tzinfo is None or remind_at <= self._clock():
                    raise LedgerConflictError("selected reminder time has passed")
                result = {"draft_ref": str(draft_id), "status": "scheduled",
                          "delivery": "owner_private_in_app", "scheduled": True,
                          "was_scheduled": True}
                await self._append_event(session, owner=actor.user_id, event_type="scheduled",
                    candidate_id=candidate, draft_ref=draft_id, operation_key=key,
                    payload={"request_digest": digest(request), "result": result,
                             "remind_at_utc": payload["remind_at_utc"], "time_zone": payload["time_zone"],
                             "confirmed_digest": payload["confirmed_digest"]})
                return result
        finally:
            await session.close()

    async def cancel(self, *, actor: LedgerActor, draft_ref, idempotency_key):
        """Human-only cancellation of a proposed or scheduled reminder."""
        if not isinstance(actor, LedgerActor) or actor.kind != "user_action":
            raise LedgerPermissionError("interactive owner action required")
        draft_id = _uuid(draft_ref, "draft_ref")
        key = _key("reminder_cancel", idempotency_key)
        request = {"draft_ref": str(draft_id)}
        session = await self.ledger._open()
        try:
            async with session.begin():
                await self._lock(session, actor.user_id)
                existing = await self._by_key(session, actor.user_id, key)
                if existing:
                    return self._replay(existing, request)
                draft = await self._owned_draft(session, actor.user_id, draft_id)
                await self._lock_candidate_subject(session, actor.user_id, draft.candidate_entry_id)
                events = await self._draft_events(session, actor.user_id, draft_id)
                state, scheduled, _terminal, _ack = self._draft_state(draft, events)
                if state not in {"awaiting_user_review", "scheduled"}:
                    raise LedgerConflictError("reminder cannot be cancelled in its current state")
                result = {"draft_ref": str(draft_id), "status": "cancelled",
                          "delivery": "owner_private_in_app", "scheduled": False,
                          "was_scheduled": scheduled is not None}
                await self._append_event(session, owner=actor.user_id, event_type="cancelled",
                    candidate_id=draft.candidate_entry_id, draft_ref=draft_id, operation_key=key,
                    payload={"request_digest": digest(request), "result": result})
                return result
        finally:
            await session.close()

    async def acknowledge(self, *, actor: LedgerActor, draft_ref, idempotency_key):
        """Human-only acknowledgement of an already materialized private inbox result."""
        if not isinstance(actor, LedgerActor) or actor.kind != "user_action":
            raise LedgerPermissionError("interactive owner action required")
        draft_id = _uuid(draft_ref, "draft_ref")
        key = _key("reminder_ack", idempotency_key)
        request = {"draft_ref": str(draft_id)}
        session = await self.ledger._open()
        try:
            async with session.begin():
                await self._lock(session, actor.user_id)
                existing = await self._by_key(session, actor.user_id, key)
                if existing:
                    return self._replay(existing, request)
                draft = await self._owned_draft(session, actor.user_id, draft_id)
                events = await self._draft_events(session, actor.user_id, draft_id)
                state, scheduled, terminal, _ack = self._draft_state(draft, events)
                if state != "delivered" or terminal is None or scheduled is None:
                    raise LedgerConflictError("only a delivered reminder can be acknowledged")
                result = {"draft_ref": str(draft_id), "status": "acknowledged",
                          "delivery": "owner_private_in_app", "scheduled": False,
                          "was_scheduled": scheduled is not None}
                await self._append_event(session, owner=actor.user_id, event_type="acknowledged",
                    candidate_id=draft.candidate_entry_id, draft_ref=draft_id, operation_key=key,
                    payload={"request_digest": digest(request), "result": result,
                             "delivery_ref": str(terminal.id)})
                return result
        finally:
            await session.close()

    async def _reconcile_locked(self, session, owner, *, limit, now, allow_delivery=True):
        stats = {"delivered": 0, "invalidated": 0}
        drafts = (await session.execute(select(Event).where(
            Event.owner_user_id == owner, Event.event_type == "proposed",
        ).order_by(Event.recorded_at, Event.id).limit(limit + 1))).scalars().all()
        if len(drafts) > limit:
            raise LedgerValidationError("too many reminder drafts to reconcile safely")
        for draft in drafts:
            events = await self._draft_events(session, owner, draft.id)
            state, scheduled, _terminal, _ack = self._draft_state(draft, events)
            if state in {"cancelled", "delivered", "invalidated", "acknowledged"}:
                continue
            payload = draft.payload
            if not self._draft_payload_complete(draft, payload):
                await self._invalidate_draft(session, owner=owner, draft=draft, scheduled=scheduled)
                stats["invalidated"] += 1
                continue
            candidate = UUID(payload["candidate_entry_id"])
            try:
                await self._lock_candidate_subject(session, owner, candidate)
                current = await self._require_eligible(session, owner, candidate, payload["confirmed_digest"])
            except LedgerNotFoundError:
                current = None
            except LedgerConflictError as exc:
                if not self._is_stale_conflict(exc):
                    raise
                current = None
            if current is not None and not self._snapshot_matches(payload, current):
                current = None
            if current is None:
                await self._invalidate_draft(session, owner=owner, draft=draft, scheduled=scheduled)
                stats["invalidated"] += 1
                continue
            if state != "scheduled" or scheduled is None:
                continue
            if not allow_delivery:
                continue
            scheduled_payload = scheduled.payload
            remind_at = datetime.fromisoformat(scheduled_payload["remind_at_utc"])
            if remind_at.tzinfo is None:
                raise LedgerConflictError("scheduled reminder has no timezone")
            if remind_at > now:
                continue
            delivery_id = uuid4()
            result = {"draft_ref": str(draft.id), "status": "delivered",
                      "delivery": "owner_private_in_app", "scheduled": False,
                      "was_scheduled": True}
            delivery_payload = {
                "request_digest": digest({"draft_ref": str(draft.id),
                    "scheduled_event_ref": str(scheduled.id), "delivery_ref": str(delivery_id)}),
                "result": result, "delivery_ref": str(delivery_id),
                "scheduled_event_ref": str(scheduled.id), "scheduled_for": remind_at.isoformat(),
                "time_zone": scheduled_payload["time_zone"],
                "title": current.label or "本人が設定した期限リマインダー",
                "due_date": current.due_date, "subject_key": current.subject_key,
                "candidate_entry_id": str(candidate), "delivery": "owner_private_in_app",
                "private_in_app": True,
            }
            await self._append_event(session, owner=owner, event_type="delivered",
                candidate_id=candidate, draft_ref=draft.id,
                operation_key=f"reminder_deliver:{draft.id}", payload=delivery_payload,
                event_id=delivery_id)
            stats["delivered"] += 1
        return stats

    async def deliver_due(self, *, limit_per_owner: int = MAX_STATE_ENTRIES):
        """Server-owned due sweep; only explicitly scheduled rows can be delivered.

        The method discovers owner UUIDs from reminder metadata, then re-queries and
        decrypts events only under each owner's SQL filter. It has no external transport;
        each committed ``delivered`` event is the durable private in-app result. Re-running
        after restart/reconnect sees the terminal event and does not create another result.
        Keep this worker dormant until the staged lifecycle migration and runtime wiring are
        deliberately deployed.
        """
        if isinstance(limit_per_owner, bool) or not isinstance(limit_per_owner, int) or not 1 <= limit_per_owner <= MAX_STATE_ENTRIES:
            raise LedgerValidationError("invalid reminder sweep limit")
        if not await self.schema_available():
            raise LedgerValidationError("personal reminder schema unavailable")
        session = await self.ledger._open()
        try:
            async with session.begin():
                owners = list((await session.execute(select(Event.owner_user_id).where(
                    Event.event_type == "proposed",
                ).distinct())).scalars().all())
        finally:
            await session.close()
        totals = {"owners_checked": 0, "delivered": 0, "invalidated": 0}
        for owner in owners:
            owner_session = await self.ledger._open()
            try:
                async with owner_session.begin():
                    await self._lock(owner_session, owner)
                    stats = await self._reconcile_locked(
                        owner_session, owner, limit=limit_per_owner, now=self._clock())
                totals["owners_checked"] += 1
                totals["delivered"] += stats["delivered"]
                totals["invalidated"] += stats["invalidated"]
            finally:
                await owner_session.close()
        return totals

    async def list_owner(self, *, actor: LedgerActor, limit: int = 50):
        """Reconcile and list this authenticated owner's draft and private inbox states."""
        if not isinstance(actor, LedgerActor) or actor.kind != "user_action":
            raise LedgerPermissionError("interactive owner action required")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise LedgerValidationError("invalid reminder page limit")
        session = await self.ledger._open()
        try:
            async with session.begin():
                await self._lock(session, actor.user_id)
                await self._reconcile_locked(session, actor.user_id, limit=MAX_STATE_ENTRIES,
                                             now=self._clock(), allow_delivery=False)
                drafts = (await session.execute(select(Event).where(
                    Event.owner_user_id == actor.user_id, Event.event_type == "proposed",
                ).order_by(Event.recorded_at.desc(), Event.id.desc()).limit(limit))).scalars().all()
                results = []
                for draft in drafts:
                    events = await self._draft_events(session, actor.user_id, draft.id)
                    results.append(self._owner_item(draft, events))
                return {"items": results}
        finally:
            await session.close()

    @staticmethod
    def _owner_item(draft, events):
        payload = draft.payload
        status, scheduled, terminal, acknowledged = PersonalReminderService._draft_state(draft, events)
        item = {
            "draft_ref": str(draft.id), "candidate_entry_id": str(draft.candidate_entry_id),
            "status": status, "delivery": "owner_private_in_app",
            "scheduled": status == "scheduled", "was_scheduled": scheduled is not None,
            "remind_at": payload.get("remind_at_utc"),
            "time_zone": payload.get("time_zone"),
            "confirmation_digest": (PersonalReminderService._draft_confirmation_digest(payload)
                                    if PersonalReminderService._draft_payload_complete(draft, payload) else None),
            "subject_key": payload.get("subject_key"), "due_date": payload.get("due_date"),
            "label": payload.get("label"),
        }
        if terminal is not None and terminal.event_type == "delivered":
            delivered = terminal.payload
            item.update({"delivery_ref": delivered["delivery_ref"],
                         "title": delivered["title"], "due_date": delivered["due_date"],
                         "subject_key": delivered["subject_key"],
                         "scheduled_for": delivered["scheduled_for"]})
        if acknowledged is not None:
            item["acknowledged_at"] = acknowledged.recorded_at.isoformat() if acknowledged.recorded_at else None
        return item

    async def get_owner(self, *, actor: LedgerActor, draft_ref):
        if not isinstance(actor, LedgerActor) or actor.kind != "user_action":
            raise LedgerPermissionError("interactive owner action required")
        draft_id = _uuid(draft_ref, "draft_ref")
        session = await self.ledger._open()
        try:
            async with session.begin():
                await self._lock(session, actor.user_id)
                await self._reconcile_locked(session, actor.user_id, limit=MAX_STATE_ENTRIES,
                                             now=self._clock(), allow_delivery=False)
                draft = await self._owned_draft(session, actor.user_id, draft_id)
                events = await self._draft_events(session, actor.user_id, draft_id)
                return self._owner_item(draft, events)
        finally:
            await session.close()
