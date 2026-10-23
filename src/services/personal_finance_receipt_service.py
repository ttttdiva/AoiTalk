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
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..memory.models.personal_finance_receipt import PersonalFinanceReceiptCandidate as Candidate
from ..memory.models.workspace_files import WorkspaceFileEntry
from .personal_draft_service import digest
from .personal_finance_attachment_source import (
    FinanceAttachmentSourceError,
    resolve_finance_attachment_source,
)
from .personal_finance_contracts import (
    FinanceConflict,
    FinanceContractError,
    FinanceRow,
    FinanceSource,
    compute_confirmed_digest,
    finance_owner_actor,
)
from .personal_finance_receipt_parser import parse_receipt_text
from .personal_finance_service import PersonalFinanceService, SqlPersonalFinanceRepository
from .personal_finance_image_types import SUPPORTED_MIME_TYPES
from .personal_async_guard import run_personal_guard
from .personal_ledger_service import LedgerConflictError, LedgerPermissionError, LedgerValidationError
from .search_sources import SourceRef, SourceUnavailable
from .turn_context import TurnContext, get_turn_context

MAX_PROCESSOR_TEXT = 16_384
STATUS_CANDIDATE = "candidate_needs_review"
STATUS_UNAVAILABLE = "processor_unavailable"
STATUS_FAILED = "processor_failed"
STATUS_NO_TEXT = "no_text"
STATUS_NO_CANDIDATE = "no_candidate"
MAX_RECEIPT_CANDIDATES = 50
MAX_DUPLICATE_SCAN = 1000
_RECEIPT_ENTRY_NAMESPACE = "personal-finance-receipt-entry-v1"


def _entry_ref(candidate_ref: UUID) -> UUID:
    return uuid5(candidate_ref, _RECEIPT_ENTRY_NAMESPACE)


def _normalized_review_row(candidate_ref: UUID, row: Any) -> FinanceRow:
    if not isinstance(row, dict) or set(row) != {
        "occurred_on", "kind", "amount_minor", "currency", "account_alias", "category"
    }:
        raise LedgerValidationError("receipt row must contain the reviewed finance fields")
    candidate_row = {"row_ref": str(_entry_ref(candidate_ref)), **row}
    try:
        normalized_row = FinanceRow.from_mapping(candidate_row)
    except FinanceContractError:
        raise LedgerValidationError("receipt row is invalid") from None
    return normalized_row


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
    def __init__(
        self,
        session_factory: Callable[[], Any],
        *,
        finance_service: PersonalFinanceService | None = None,
    ):
        self._session_factory = session_factory
        self._finance = finance_service or PersonalFinanceService(
            SqlPersonalFinanceRepository(session_factory)
        )

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
        guard: Callable[[], Any] | None = None,
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
            await run_personal_guard(guard)
            return _result(STATUS_FAILED)
        await run_personal_guard(guard)  # right after the processor await, before parsing or storage
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
            await run_personal_guard(guard)
            async with session.begin():
                result = await session.execute(select(Candidate).where(
                    Candidate.owner_user_id == owner_user_id, Candidate.operation_key == key,
                ))
                await run_personal_guard(guard)
                existing = result.scalar_one_or_none()
                if existing is not None:
                    stored = existing.payload
                    if stored["request_digest"] != request_digest:
                        raise LedgerConflictError("operation key has different content")
                    result_view = _candidate_view(existing.id, stored, replayed=True)
                else:
                    row = Candidate(owner_user_id=owner_user_id, source_ref=source_ref,
                                    source_sha256=evidence.source_sha256, operation_key=key,
                                    status="needs_review")
                    row.payload = payload
                    session.add(row)
                    try:
                        await session.flush()
                    except IntegrityError:
                        raise LedgerConflictError("concurrent receipt candidate") from None
                    await run_personal_guard(guard)  # raising rolls the flushed row back before COMMIT
                    result_view = _candidate_view(row.id, payload, replayed=False)
            await run_personal_guard(guard)
            return result_view
        finally:
            await session.close()

    async def list_sources(
        self,
        *,
        owner_user_id: UUID,
        resolver: Any,
        limit: int = 50,
        guard: Callable[[], Any] | None = None,
    ) -> tuple[dict[str, Any], ...]:
        """List only indexed personal image attachments verified by the resolver."""

        from .personal_finance_attachment_source import _scope_is_personal

        if not isinstance(owner_user_id, UUID) or type(limit) is not int or not 1 <= limit <= 50:
            raise LedgerValidationError("invalid finance source list request")
        if not _scope_is_personal(resolver, owner_user_id):
            raise LedgerPermissionError("personal owner resolver required")
        result = await resolver.session.execute(
            select(WorkspaceFileEntry)
            .where(
                WorkspaceFileEntry.scope == "user",
                WorkspaceFileEntry.owner_id == owner_user_id,
                WorkspaceFileEntry.status == "indexable",
                WorkspaceFileEntry.deleted_at.is_(None),
                WorkspaceFileEntry.content_hash.is_not(None),
                WorkspaceFileEntry.mime.in_(tuple(sorted(SUPPORTED_MIME_TYPES))),
                WorkspaceFileEntry.size_bytes > 0,
                WorkspaceFileEntry.size_bytes <= 10_485_760,
            )
            .order_by(WorkspaceFileEntry.last_seen_at.desc(), WorkspaceFileEntry.id.desc())
            .limit(limit)
        )
        await run_personal_guard(guard)
        rows = result.scalars().all()
        sources: list[dict[str, Any]] = []
        for row in rows:
            try:
                resolved = await resolver.resolve(SourceRef("workspace_file", str(row.id)), open_file=False)
            except SourceUnavailable:
                await run_personal_guard(guard)
                continue
            await run_personal_guard(guard)
            metadata = resolved.metadata if isinstance(resolved.metadata, dict) else {}
            if (
                metadata.get("files_scope") != "user"
                or metadata.get("files_owner_id") != str(owner_user_id)
                or metadata.get("content_hash") != row.content_hash
                or metadata.get("mime_type") not in SUPPORTED_MIME_TYPES
            ):
                continue
            sources.append({
                "source_ref": str(row.id),
                "source_sha256": str(row.content_hash),
                "mime_type": str(metadata["mime_type"]),
                "byte_size": int(row.size_bytes),
            })
        return tuple(sources)

    async def list_candidates(
        self,
        *,
        owner_user_id: UUID,
        source_ref: UUID | None = None,
        limit: int = 50,
        guard: Callable[[], Any] | None = None,
    ) -> tuple[dict[str, Any], ...]:
        if (
            not isinstance(owner_user_id, UUID)
            or (source_ref is not None and not isinstance(source_ref, UUID))
            or type(limit) is not int
            or not 1 <= limit <= MAX_RECEIPT_CANDIDATES
        ):
            raise LedgerValidationError("invalid receipt candidate list request")
        statement = select(Candidate).where(Candidate.owner_user_id == owner_user_id)
        if source_ref is not None:
            statement = statement.where(Candidate.source_ref == source_ref)
        result = await self._execute_candidates(statement, limit, guard)
        views: list[dict[str, Any]] = []
        for candidate in result:
            view = await self._candidate_view(owner_user_id, candidate, guard=guard)
            views.append(view)
        return tuple(views)

    async def get_candidate(
        self,
        *,
        owner_user_id: UUID,
        candidate_ref: UUID,
        guard: Callable[[], Any] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(owner_user_id, UUID) or not isinstance(candidate_ref, UUID):
            raise LedgerValidationError("invalid receipt candidate reference")
        from sqlalchemy import select

        session = await self._open()
        try:
            await run_personal_guard(guard)
            result = await session.execute(select(Candidate).where(
                Candidate.owner_user_id == owner_user_id,
                Candidate.id == candidate_ref,
            ))
            await run_personal_guard(guard)
            candidate = result.scalar_one_or_none()
            if candidate is None:
                from .personal_ledger_service import LedgerNotFoundError
                raise LedgerNotFoundError("receipt_candidate_not_found")
            return await self._candidate_view(owner_user_id, candidate, guard=guard)
        finally:
            await session.close()

    async def preview_confirmation(
        self,
        *,
        owner_user_id: UUID,
        resolver: Any,
        candidate_ref: UUID,
        revision: int,
        candidate_digest: str,
        row: dict[str, Any],
        guard: Callable[[], Any] | None = None,
    ) -> dict[str, Any]:
        candidate, payload = await self._load_candidate_payload(
            owner_user_id, candidate_ref, guard=guard
        )
        if revision != 1 or candidate_digest != payload.get("request_digest"):
            raise LedgerConflictError("receipt_candidate_changed")
        if payload.get("proposal", {}).get("review_reasons"):
            raise LedgerValidationError("receipt_candidate_requires_manual_review")
        normalized_row = _normalized_review_row(candidate_ref, row)
        evidence = payload.get("evidence") or {}
        try:
            source = FinanceSource.from_mapping({
                "source_ref": evidence["source_ref"],
                "source_sha256": evidence["source_sha256"],
                "mime_type": evidence["mime_type"],
                "byte_size": evidence["byte_size"],
            })
        except (KeyError, FinanceContractError):
            raise LedgerValidationError("receipt evidence is invalid") from None
        try:
            await resolve_finance_attachment_source(
                resolver,
                owner_user_id=owner_user_id,
                source_ref=candidate.source_ref,
                expected_sha256=candidate.source_sha256,
                guard=guard,
            )
        except FinanceAttachmentSourceError as exc:
            raise LedgerConflictError("receipt_source_changed") from exc

        entries = await self._finance.list_confirmed_entries(
            finance_owner_actor(owner_user_id), limit=MAX_DUPLICATE_SCAN, guard=guard
        )
        canonical = normalized_row.to_canonical()
        compare = {key: value for key, value in canonical.items() if key != "row_ref"}
        duplicates = [
            str(entry.row_ref)
            for entry in entries
            if entry.row_ref != normalized_row.row_ref
            and {
                "occurred_on": entry.occurred_on,
                "kind": entry.kind,
                "amount_minor": entry.amount_minor,
                "currency": entry.currency,
                "account_alias": entry.account_alias,
                "category": entry.category,
            } == compare
        ]
        scan_incomplete = len(entries) == MAX_DUPLICATE_SCAN
        possible_duplicate = bool(duplicates) or scan_incomplete
        confirmed_digest = digest({
            "owner_user_id": str(owner_user_id),
            "candidate_ref": str(candidate_ref),
            "revision": revision,
            "candidate_digest": candidate_digest,
            "row": canonical,
            "source": source.to_canonical(),
        })
        return {
            "candidate_ref": str(candidate_ref),
            "revision": revision,
            "confirmed_digest": confirmed_digest,
            "row": canonical,
            "source": source.to_canonical(),
            "possible_duplicate": possible_duplicate,
            "duplicate_entry_refs": duplicates[:20],
            "duplicate_scan_incomplete": scan_incomplete,
        }

    async def confirm_candidate(
        self,
        *,
        owner_user_id: UUID,
        resolver: Any,
        candidate_ref: UUID,
        revision: int,
        candidate_digest: str,
        row: dict[str, Any],
        confirmed_digest: str,
        duplicate_confirmed: bool,
        owner_confirmed: bool,
        idempotency_key: UUID,
        guard: Callable[[], Any] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(idempotency_key, UUID) or owner_confirmed is not True:
            raise LedgerPermissionError("owner confirmation required")
        preview = await self.preview_confirmation(
            owner_user_id=owner_user_id,
            resolver=resolver,
            candidate_ref=candidate_ref,
            revision=revision,
            candidate_digest=candidate_digest,
            row=row,
            guard=guard,
        )
        if confirmed_digest != preview["confirmed_digest"]:
            raise LedgerConflictError("receipt_confirmation_digest_mismatch")
        if preview["possible_duplicate"] and duplicate_confirmed is not True:
            raise LedgerConflictError("duplicate_confirmation_required")
        if not preview["possible_duplicate"] and duplicate_confirmed is True:
            raise LedgerValidationError("unexpected_duplicate_confirmation")
        await run_personal_guard(guard)
        normalized_row = FinanceRow.from_mapping(preview["row"])
        source = FinanceSource.from_mapping(preview["source"])
        entry_digest = compute_confirmed_digest(
            owner_user_id=owner_user_id, row=normalized_row, source=source
        )
        try:
            entry = await self._finance.append_confirmed_entry(
                finance_owner_actor(owner_user_id),
                row=normalized_row.to_canonical(),
                source=source.to_canonical(),
                confirmed_digest=entry_digest,
                owner_confirmed=True,
                idempotency_key=f"receipt:{candidate_ref}:{idempotency_key}",
                guard=guard,
            )
        except FinanceConflict:
            raise LedgerConflictError("receipt_idempotency_payload_mismatch") from None
        except FinanceContractError:
            raise LedgerConflictError("receipt_entry_conflict") from None
        await run_personal_guard(guard)
        return {
            "candidate_ref": str(candidate_ref),
            "revision": revision,
            "confirmed_digest": confirmed_digest,
            "status": "accepted",
            "entry_refs": [str(entry.row_ref)],
            "replayed": bool(entry.replayed),
        }

    async def _execute_candidates(self, statement, limit: int, guard):
        statement = statement.order_by(Candidate.recorded_at.desc(), Candidate.id.desc()).limit(limit)
        session = await self._open()
        try:
            await run_personal_guard(guard)
            result = await session.execute(statement)
            await run_personal_guard(guard)
            return list(result.scalars().all())
        finally:
            await session.close()

    async def _load_candidate_payload(self, owner_user_id: UUID, candidate_ref: UUID, *, guard=None):
        from sqlalchemy import select
        from .personal_ledger_service import LedgerNotFoundError

        session = await self._open()
        try:
            await run_personal_guard(guard)
            result = await session.execute(select(Candidate).where(
                Candidate.owner_user_id == owner_user_id,
                Candidate.id == candidate_ref,
            ))
            await run_personal_guard(guard)
            candidate = result.scalar_one_or_none()
            if candidate is None:
                raise LedgerNotFoundError("receipt_candidate_not_found")
            payload = candidate.payload
            if not isinstance(payload, dict) or payload.get("schema") != 1:
                raise LedgerValidationError("receipt_candidate_payload_invalid")
            return candidate, payload
        finally:
            await session.close()

    async def _candidate_view(self, owner_user_id: UUID, candidate: Candidate, *, guard=None):
        await run_personal_guard(guard)
        payload = candidate.payload
        if not isinstance(payload, dict) or payload.get("schema") != 1:
            raise LedgerValidationError("receipt_candidate_payload_invalid")
        evidence = payload.get("evidence") or {}
        proposal = payload.get("proposal") or {}
        source = {
            "source_ref": str(candidate.source_ref),
            "source_sha256": candidate.source_sha256,
            "mime_type": evidence.get("mime_type"),
            "byte_size": evidence.get("byte_size"),
        }
        fields = proposal.get("fields") if isinstance(proposal.get("fields"), dict) else {}
        review_reasons = proposal.get("review_reasons")
        candidate_entry = await self._finance.get_confirmed_entry(
            finance_owner_actor(owner_user_id), row_ref=_entry_ref(candidate.id), guard=guard
        )
        await run_personal_guard(guard)
        candidate_status = "accepted" if candidate_entry is not None else "pending"
        return {
            "candidate_ref": str(candidate.id),
            "revision": 1,
            "confirmed_digest": str(payload.get("request_digest") or ""),
            "source": source,
            "fields": fields,
            "needs_review": list(review_reasons) if isinstance(review_reasons, list) else ["candidate_review_required"],
            "status": candidate_status,
            "possible_duplicate": False,
            "source_text": str(evidence.get("text") or ""),
            "parser_version": str(evidence.get("parser_version") or ""),
            "accepted_row": None if candidate_entry is None else {
                "row_ref": str(candidate_entry.row_ref),
                "occurred_on": candidate_entry.occurred_on,
                "kind": candidate_entry.kind,
                "amount_minor": candidate_entry.amount_minor,
                "currency": candidate_entry.currency,
                "account_alias": candidate_entry.account_alias,
                "category": candidate_entry.category,
            },
        }
