"""Receipt PNG -> grounded, unconfirmed finance candidate (dormant backend).

Reuses the owner/hash/10MiB attachment verification, runs a *local-only*
text processor injected by the trusted caller, parses with the strict
minor-unit parser, and stores encrypted evidence + proposal as one append-only
row.  Nothing here touches ``personal_finance_entries``: a candidate is never
booked automatically.  No processor / route => explicit status, no read, no write.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import inspect
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..memory.models.personal_finance_receipt import PersonalFinanceReceiptCandidate as Candidate
from .personal_draft_service import digest
from .personal_finance_attachment_source import resolve_finance_attachment_source
from .personal_finance_receipt_parser import parse_receipt_text
from .personal_ledger_service import LedgerConflictError, LedgerPermissionError, LedgerValidationError
from .turn_context import TurnContext, get_turn_context

MAX_PROCESSOR_TEXT = 16_384
STATUS_CANDIDATE = "candidate_needs_review"
STATUS_UNAVAILABLE = "processor_unavailable"
STATUS_FAILED = "processor_failed"
STATUS_NO_TEXT = "no_text"
STATUS_NO_CANDIDATE = "no_candidate"


class ReceiptTextProcessor(Protocol):
    """Local text extractor for one PNG; ``name``/``version`` are recorded as evidence."""

    name: str
    version: str

    async def extract_text(self, png: bytes) -> str: ...


@dataclass(frozen=True)
class ReceiptProcessorRoute:
    """Server-resolved route; never built from model/client input."""

    gateway: Any
    provider: str
    base_url: str | None
    allow_external_fallback: bool


def _check_route(route: object, owner_user_id: UUID) -> None:
    from .outbound_privacy_service import OutboundPrivacyGateway, PrivacyError, provider_classification

    turn = get_turn_context()
    if type(turn) is not TurnContext or turn.project_id or turn.user_id != str(owner_user_id) or not turn.session_id:
        raise LedgerPermissionError("trusted authenticated personal turn required")
    if not isinstance(route, ReceiptProcessorRoute) or route.allow_external_fallback is not False:
        raise LedgerPermissionError("local route with disabled fallback required")
    gateway = route.gateway
    if not isinstance(gateway, OutboundPrivacyGateway) or gateway.settings.mode != "local_only":
        raise LedgerPermissionError("explicit local-only gateway required")
    if gateway.user_id != turn.user_id or gateway.session_id != turn.session_id:
        raise LedgerPermissionError("gateway does not match authenticated turn")
    if not route.base_url or provider_classification(route.provider, base_url=route.base_url) != "local":
        raise LedgerPermissionError("resolved loopback route required")
    try:
        if gateway.ensure_provider_allowed(route.provider, base_url=route.base_url) != "local":
            raise LedgerPermissionError("external route denied")
    except PrivacyError:
        raise LedgerPermissionError("external route denied") from None


def _result(status: str, **extra: Any) -> dict:
    return {"status": status, "effective_ledger_change": False, **extra}


def _candidate_view(candidate_id: UUID, payload: dict, *, replayed: bool) -> dict:
    evidence = payload["evidence"]
    proposal = payload["proposal"]
    return _result(
        STATUS_CANDIDATE, candidate_ref=str(candidate_id), needs_review=True,
        fields=proposal["fields"], review_reasons=proposal["review_reasons"],
        source_ref=evidence["source_ref"], source_sha256=evidence["source_sha256"],
        replayed=replayed,
    )


class PersonalFinanceReceiptService:
    def __init__(self, session_factory: Callable[[], Any]):
        self._session_factory = session_factory

    async def _open(self):
        session = self._session_factory()
        if inspect.isawaitable(session):
            session = await session
        return session

    async def propose_from_attachment(
        self,
        *,
        owner_user_id: UUID,
        resolver: Any,
        source_ref: UUID,
        expected_sha256: str,
        idempotency_key: UUID,
        route: ReceiptProcessorRoute | None,
        processor: ReceiptTextProcessor | None,
        guard: Callable[[], None] | None = None,
    ) -> dict:
        if not isinstance(owner_user_id, UUID) or not isinstance(idempotency_key, UUID):
            raise LedgerValidationError("owner_user_id and idempotency_key must be UUIDs")
        if processor is None or route is None:
            # Nothing is read, sent or stored when no local processor is configured.
            return _result(STATUS_UNAVAILABLE)
        _check_route(route, owner_user_id)

        evidence = await resolve_finance_attachment_source(
            resolver, owner_user_id=owner_user_id, source_ref=source_ref,
            expected_sha256=expected_sha256, guard=guard,
        )
        try:
            text = await processor.extract_text(evidence.content)
        except Exception:  # noqa: BLE001 - processor detail may carry receipt content
            if guard is not None:
                guard()
            return _result(STATUS_FAILED)
        if guard is not None:
            guard()  # right after the processor await, before parsing or storage
        _check_route(route, owner_user_id)
        if not isinstance(text, str) or not text.strip():
            return _result(STATUS_NO_TEXT)
        if len(text) > MAX_PROCESSOR_TEXT:
            return _result(STATUS_FAILED)

        parsed = parse_receipt_text(text)
        if not parsed["fields"]:
            return _result(STATUS_NO_CANDIDATE, review_reasons=parsed["review_reasons"])

        processor_id = {"name": str(processor.name), "version": str(processor.version)}
        payload = {
            "schema": 1,
            "evidence": {
                "source_ref": str(evidence.source_ref), "source_sha256": evidence.source_sha256,
                "mime_type": evidence.mime_type, "byte_size": evidence.byte_size,
                "processor": processor_id, "parser_version": parsed["parser_version"],
                "text": parsed["text"],
            },
            "proposal": {
                "kind": "expense", "fields": parsed["fields"],
                "review_reasons": parsed["review_reasons"], "needs_review": True,
            },
        }
        request_digest = digest({"source_ref": str(source_ref), "source_sha256": evidence.source_sha256,
                                 "processor": processor_id, "text": parsed["text"]})
        payload["request_digest"] = request_digest
        key = f"receipt:{idempotency_key}"

        session = await self._open()
        try:
            async with session.begin():
                existing = (await session.execute(select(Candidate).where(
                    Candidate.owner_user_id == owner_user_id, Candidate.operation_key == key,
                ))).scalar_one_or_none()
                if guard is not None:
                    guard()
                if existing is not None:
                    stored = existing.payload
                    if stored["request_digest"] != request_digest:
                        raise LedgerConflictError("operation key has different content")
                    return _candidate_view(existing.id, stored, replayed=True)
                row = Candidate(owner_user_id=owner_user_id, source_ref=source_ref,
                                source_sha256=evidence.source_sha256, operation_key=key,
                                status="needs_review")
                row.payload = payload
                session.add(row)
                try:
                    await session.flush()
                except IntegrityError:
                    raise LedgerConflictError("concurrent receipt candidate") from None
                if guard is not None:
                    guard()  # raising rolls the flushed row back before COMMIT
                return _candidate_view(row.id, payload, replayed=False)
        finally:
            await session.close()
