"""Bounded manual evidence → draft → human acceptance, with atomic ledger writes.

No extractor/model/network is called. Service actors are issued by the existing
HTTP / trusted turn boundaries; accepting a draft always requires user_action.
All cross-process mutations take the same owner advisory lock before any read.
"""

from __future__ import annotations

import hashlib
import json
import re
from uuid import UUID, uuid4

from sqlalchemy import select, text, tuple_

from ..memory.models.personal_draft import PersonalDraftEvent as Event
from .personal_ledger_service import (
    LedgerActor, LedgerConflictError, LedgerNotFoundError, LedgerPermissionError,
    LedgerValidationError, PersonalLedgerService, agent_actor, normalize_payload,
    _SLUG_RE, _uuid,
)

MAX_SOURCE_TEXT = 16_384


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def normalize_proposal(value) -> dict:
    if not isinstance(value, dict) or set(value) != {"kind", "data"}:
        raise LedgerValidationError("invalid proposal")
    kind, data = value["kind"], value["data"]
    if not isinstance(kind, str) or kind not in {"deadline", "inventory"} or not isinstance(data, dict):
        raise LedgerValidationError("unsupported proposal kind")
    subject = data.get("subject_key")
    if not isinstance(subject, str) or not _SLUG_RE.fullmatch(subject):
        raise LedgerValidationError("invalid neutral subject key")
    entry_type = "deadline_candidate" if kind == "deadline" else data.get("entry_type")
    if kind == "inventory" and (not isinstance(entry_type, str) or entry_type not in {"opening_balance", "purchase", "consumption"}):
        raise LedgerValidationError("unsupported inventory proposal")
    payload = {k: v for k, v in data.items() if k not in {"subject_key", "entry_type"}}
    if kind == "deadline" and "entry_type" in data:
        raise LedgerValidationError("unsupported proposal field")
    # schema is server-authored, never accepted from the model.
    if "schema" in payload:
        raise LedgerValidationError("unsupported proposal field")
    normalized = normalize_payload(entry_type, payload)
    normalized.pop("schema")
    return {"kind": kind, "data": {"subject_key": subject, **(
        {"entry_type": entry_type} if kind == "inventory" else {}
    ), **normalized}}


def _actor(actor, *, human=False):
    if not isinstance(actor, LedgerActor) or actor.kind not in {"agent", "user_action"}:
        raise LedgerPermissionError("invalid actor")
    if human and actor.kind != "user_action":
        raise LedgerPermissionError("interactive owner action required")


def _key(operation: str, value) -> str:
    # Opaque keys survive retries/restarts and cannot carry document/account IDs.
    return f"{operation}:{_uuid(value, 'idempotency_key')}"


def _manual_evidence(proposal: dict, source: str) -> dict:
    if not isinstance(source, str) or not source.strip() or len(source) > MAX_SOURCE_TEXT:
        raise LedgerValidationError("invalid bounded source text")
    spans = {}
    for field, value in proposal["data"].items():
        if field == "subject_key":
            continue  # An opaque neutral alias is assigned by the owner.
        token = str(value)
        # Exact explicit values only. No date arithmetic, OCR or semantic claims.
        match = re.search(r"(?<![\w.-])" + re.escape(token) + r"(?![\w.-])", source)
        if match is None:
            raise LedgerValidationError("evidence does not contain the explicit field value")
        spans[field] = {"start": match.start(), "end": match.end()}
    return {
        "schema": 1, "source_text": source,
        "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        "extractor_version": "owner_explicit_text_v1", "field_spans": spans,
        "proposal": proposal,
    }


class PersonalDraftService:
    def __init__(self, session_factory):
        self.ledger = PersonalLedgerService(session_factory)

    @staticmethod
    async def _lock(session, owner):
        # Separate two-key namespace from the existing ledger subject lock.
        key = int.from_bytes(hashlib.sha256(owner.bytes).digest()[:4], "big", signed=True)
        await session.execute(text("SELECT pg_advisory_xact_lock(1346651220, :key)"), {"key": key})

    @staticmethod
    async def _owned(session, owner, ref, event_type):
        row = (await session.execute(select(Event).where(
            Event.owner_user_id == owner, Event.id == ref, Event.event_type == event_type,
        ))).scalar_one_or_none()
        if row is None:
            raise LedgerNotFoundError()
        return row  # Owner WHERE runs before payload decryption.

    @staticmethod
    async def _by_key(session, owner, key):
        return (await session.execute(select(Event).where(
            Event.owner_user_id == owner, Event.operation_key == key,
        ))).scalar_one_or_none()

    @staticmethod
    def _replay(row, request):
        payload = row.payload
        if payload["request_digest"] != digest(request):
            raise LedgerConflictError("operation key has different content")
        return payload

    @staticmethod
    async def _insert(session, *, actor, event_type, operation_key, payload, **fields):
        row = Event(id=uuid4(), owner_user_id=actor.user_id, event_type=event_type,
                    operation_key=operation_key, **fields)
        row.payload = payload
        session.add(row)
        await session.flush()
        return row

    async def record_evidence(self, *, actor, proposal, source_text, idempotency_key):
        _actor(actor, human=True)
        proposal = normalize_proposal(proposal)
        evidence = _manual_evidence(proposal, source_text)
        key = _key("evidence", idempotency_key)
        request = {"evidence": evidence}
        session = await self.ledger._open()
        try:
            async with session.begin():
                await self._lock(session, actor.user_id)
                existing = await self._by_key(session, actor.user_id, key)
                if existing:
                    self._replay(existing, request)
                    return {"evidence_ref": str(existing.id)}
                row = await self._insert(session, actor=actor, event_type="evidence", operation_key=key,
                    payload={"request_digest": digest(request), "evidence": evidence})
                return {"evidence_ref": str(row.id)}
        finally:
            await session.close()

    async def _evidence(self, session, owner, ref, guard=None):
        row = await self._owned(session, owner, ref, "evidence")
        if guard is not None:
            guard()  # right after the DB await, before the payload is read/decrypted
        evidence = row.payload["evidence"]
        if evidence != _manual_evidence(evidence["proposal"], evidence["source_text"]):
            raise LedgerConflictError("source integrity mismatch")
        return evidence

    async def propose(self, *, actor, evidence_ref, proposal, idempotency_key, guard=None):
        _actor(actor)
        proposal = normalize_proposal(proposal)
        evidence_ref = _uuid(evidence_ref, "evidence_ref")
        key = _key("propose", idempotency_key)
        request = {"evidence_ref": str(evidence_ref), "proposal": proposal, "actor_kind": actor.kind}
        session = await self.ledger._open()
        try:
            if guard is not None:
                guard()  # after _open, before the first DB read
            async with session.begin():
                if guard is not None:
                    guard()  # after transaction enter
                await self._lock(session, actor.user_id)
                if guard is not None:
                    guard()  # after the advisory lock, before the next DB read
                existing = await self._by_key(session, actor.user_id, key)
                if guard is not None:
                    guard()
                if existing:
                    return self._replay(existing, request)["result"]
                evidence = await self._evidence(session, actor.user_id, evidence_ref, guard)
                if evidence["proposal"] != proposal:
                    raise LedgerValidationError("proposal does not match evidence")
                duplicate = (await session.execute(select(Event.id).where(
                    Event.owner_user_id == actor.user_id, Event.evidence_ref == evidence_ref,
                ))).first()
                if guard is not None:
                    guard()  # raising here rolls the transaction back before any write
                if duplicate:
                    raise LedgerConflictError("evidence already has a draft")
                draft_id = uuid4()
                snapshot = {"schema": 1, "revision": 1, "draft_ref": str(draft_id),
                            "proposal": proposal, "evidence_ref": str(evidence_ref), "evidence": evidence,
                            "proposed_by": actor.kind}
                result = {"draft_ref": str(draft_id), "confirmed_digest": digest(snapshot),
                          "status": "awaiting_user_review", "effective_ledger_change": False}
                row = Event(id=draft_id, owner_user_id=actor.user_id, event_type="proposed",
                            evidence_ref=evidence_ref, operation_key=key)
                row.payload = {"request_digest": digest(request), "result": result,
                               "snapshot_digest": digest(snapshot), "proposed_by": actor.kind,
                               "proposal": proposal, "evidence_digest": digest(evidence)}
                session.add(row)
                await session.flush()
                if guard is not None:
                    guard()  # raising here rolls back the flushed audit row before COMMIT
                return result
        finally:
            await session.close()

    @staticmethod
    async def _terminal(session, owner, ref):
        return (await session.execute(select(Event).where(
            Event.owner_user_id == owner, Event.draft_ref == ref,
        ))).scalar_one_or_none()

    async def _snapshot(self, session, owner, ref):
        row = await self._owned(session, owner, ref, "proposed")
        data = row.payload
        evidence = await self._evidence(session, owner, row.evidence_ref)
        snapshot = {"schema": 1, "revision": 1, "draft_ref": str(ref), "proposal": data["proposal"],
                    "evidence_ref": str(row.evidence_ref), "evidence": evidence, "proposed_by": data["proposed_by"]}
        if digest(evidence) != data["evidence_digest"] or digest(snapshot) != data["snapshot_digest"]:
            raise LedgerConflictError("draft integrity mismatch")
        return {**snapshot, "confirmed_digest": digest(snapshot)}

    async def get_draft(self, *, actor, draft_ref):
        _actor(actor, human=True)  # No generic draft/evidence retrieval agent tool.
        ref = _uuid(draft_ref, "draft_ref")
        session = await self.ledger._open()
        try:
            snapshot = await self._snapshot(session, actor.user_id, ref)
            terminal = await self._terminal(session, actor.user_id, ref)
            return {**snapshot, "status": terminal.payload["result"]["status"] if terminal else "awaiting_user_review",
                    "target_id": str(terminal.target_entry_id) if terminal and terminal.target_entry_id else None}
        finally:
            await session.close()

    async def list_drafts(self, *, actor, limit=50, cursor=None):
        _actor(actor, human=True)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise LedgerValidationError("invalid limit")
        cursor = _uuid(cursor, "cursor") if cursor is not None else None
        session = await self.ledger._open()
        try:
            query = select(Event.id).where(
                Event.owner_user_id == actor.user_id, Event.event_type == "proposed",
            )
            if cursor is not None:
                # The immutable proposal is the cursor: resolve its ordering key
                # only inside the authenticated owner's scope, before decryption.
                anchor = await self._owned(session, actor.user_id, cursor, "proposed")
                query = query.where(tuple_(Event.recorded_at, Event.id) <
                                    tuple_(anchor.recorded_at, anchor.id))
            refs = (await session.execute(query.order_by(
                Event.recorded_at.desc(), Event.id.desc()).limit(limit + 1))).scalars().all()
        finally:
            await session.close()
        return {"items": [await self.get_draft(actor=actor, draft_ref=ref) for ref in refs[:limit]],
                "next_cursor": str(refs[limit - 1]) if len(refs) > limit else None}

    async def accept(self, *, actor, draft_ref, confirmed_digest, idempotency_key):
        return await self._finish(actor=actor, draft_ref=draft_ref, confirmed_digest=confirmed_digest,
                                  idempotency_key=idempotency_key, operation="accepted")

    async def discard(self, *, actor, draft_ref, confirmed_digest, idempotency_key):
        return await self._finish(actor=actor, draft_ref=draft_ref, confirmed_digest=confirmed_digest,
                                  idempotency_key=idempotency_key, operation="discarded")

    async def _finish(self, *, actor, draft_ref, confirmed_digest, idempotency_key, operation):
        _actor(actor, human=True)
        ref = _uuid(draft_ref, "draft_ref")
        if not isinstance(confirmed_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", confirmed_digest):
            raise LedgerValidationError("invalid confirmation digest")
        # Accept and discard share the operation-key namespace.
        key = _key("finish", idempotency_key)
        request = {"draft_ref": str(ref), "confirmed_digest": confirmed_digest, "operation": operation}
        session = await self.ledger._open()
        try:
            async with session.begin():
                await self._lock(session, actor.user_id)
                existing = await self._by_key(session, actor.user_id, key)
                if existing:
                    return {**self._replay(existing, request)["result"], "replayed": True}
                snapshot = await self._snapshot(session, actor.user_id, ref)
                if confirmed_digest != snapshot["confirmed_digest"]:
                    raise LedgerConflictError("displayed draft has changed")
                terminal = await self._terminal(session, actor.user_id, ref)
                if terminal:
                    # Another key/tab/restart cannot apply the same draft twice.
                    if terminal.event_type != operation:
                        raise LedgerConflictError("draft already has a terminal event")
                    result = {**terminal.payload["result"], "replayed": True}
                    await self._insert(session, actor=actor, event_type="receipt", operation_key=key,
                        payload={"request_digest": digest(request), "result": result})
                    return result
                result = {"draft_id": str(ref), "status": "discarded", "target_kind": None,
                          "target_id": None, "replayed": False}
                target_id = None
                if operation == "accepted":
                    kind = snapshot["proposal"]["kind"]
                    data = snapshot["proposal"]["data"]
                    entry_type = "deadline_candidate" if kind == "deadline" else data["entry_type"]
                    # Inventory is a human action. Deadline stays an agent candidate,
                    # requiring the separate existing verify route after materializing.
                    material_actor = agent_actor(actor.user_id) if kind == "deadline" else actor
                    materialized = await self.ledger._append_entry_in_transaction(
                        session=session, actor=material_actor, ledger=kind, entry_type=entry_type,
                        subject_key=data["subject_key"],
                        payload={k: v for k, v in data.items() if k not in {"subject_key", "entry_type"}},
                        idempotency_key=f"personal-draft:{ref}",
                    )
                    target_id = materialized.entry.id
                    # A ledger replay without a terminal event is an integrity failure,
                    # never attach somebody else's direct API entry to this acceptance.
                    if materialized.replayed:
                        raise LedgerConflictError("unexpected existing materialization")
                    result.update(status="materialized_requires_verification" if kind == "deadline" else "accepted",
                                  target_kind=kind, target_id=str(target_id))
                await self._insert(session, actor=actor, event_type=operation, operation_key=key,
                    draft_ref=ref, target_entry_id=target_id,
                    payload={"request_digest": digest(request), "result": result,
                             "confirmed_digest": confirmed_digest})
                return result
        finally:
            # Any error, including final event insertion/commit failure, rolls BOTH
            # writes back. No nested append_entry transaction / two-write window.
            await session.close()
