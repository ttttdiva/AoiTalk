"""Authenticated owner-only finance entry, snapshot and receipt routes."""

from __future__ import annotations

import inspect
from typing import Any, Literal
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from ...services.personal_finance_attachment_source import FinanceAttachmentSourceError
from ...services.personal_finance_contracts import (
    FinanceActorNotAllowed,
    FinanceConflict,
    FinanceContractError,
    FinanceDigestMismatch,
    FinanceDuplicateRowRef,
    FinanceRow,
    FinanceSource,
    compute_confirmed_digest,
    finance_owner_actor,
)
from ...services.personal_finance_dataset import PersonalFinanceDatasetService
from ...services.personal_finance_receipt_service import (
    PersonalFinanceReceiptService,
    ReceiptProcessorRoute,
    ReceiptTextProcessor,
)
from ...services.personal_finance_service import PersonalFinanceService, SqlPersonalFinanceRepository
from ...services.personal_finance_summary import summarize_finance_snapshot
from ...services.personal_finance_summary import FinanceSummaryOverflowError
from ...services.search_sources import SearchScope, SearchSourceResolver
from ...services.turn_context import reset_turn_context, set_turn_context
from .personal_ledger_routes import require_interactive_owner

_KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class FinanceEntryPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    row: dict[str, Any]
    source: dict[str, Any] | None = None


class FinanceEntryPreviewResponse(BaseModel):
    row: dict[str, Any]
    source: dict[str, Any] | None
    confirmed_digest: str


class FinanceEntryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    row: dict[str, Any]
    source: dict[str, Any] | None = None
    confirmed_digest: str = Field(pattern=_SHA256_PATTERN, strict=True)
    owner_confirmed: Literal[True]
    idempotency_key: str = Field(pattern=_KEY_PATTERN, strict=True)


class FinanceEntryOut(BaseModel):
    row_ref: UUID
    occurred_on: str
    kind: str
    amount_minor: int
    currency: str
    account_alias: str
    category: str | None
    source: dict[str, Any] | None
    confirmed_digest: str
    confirmed_by_kind: Literal["user_action"]


class FinanceEntryPage(BaseModel):
    entries: list[FinanceEntryOut]


class FinanceEntryWriteResponse(BaseModel):
    entry: FinanceEntryOut
    replayed: bool


class FinanceDatasetAccept(BaseModel):
    model_config = ConfigDict(extra="forbid")
    row_refs: list[UUID] = Field(min_length=1, max_length=1000)
    owner_confirmed: Literal[True]
    idempotency_key: str = Field(pattern=_KEY_PATTERN, strict=True)


class FinanceDatasetOut(BaseModel):
    dataset_ref: UUID
    member_count: int
    complete: bool
    members_digest: str
    replayed: bool = False


class FinanceDatasetPage(BaseModel):
    datasets: list[FinanceDatasetOut]


class PersonalFinanceSummaryTotalOut(BaseModel):
    currency: str
    income_minor: int
    expense_minor: int
    record_count: int


class FinanceSummaryResponse(BaseModel):
    snapshot_digest: str
    totals: list[PersonalFinanceSummaryTotalOut]
    duplicate_candidates: list[UUID]
    needs_review: list[str]


class ReceiptSourceOut(BaseModel):
    source_ref: UUID
    source_sha256: str = Field(pattern=_SHA256_PATTERN)
    mime_type: str
    byte_size: int


class ReceiptEvidenceOut(BaseModel):
    source_ref: UUID
    source_sha256: str = Field(pattern=_SHA256_PATTERN)
    page: int | None
    span_start: int
    span_end: int


class ReceiptSourcePreviewOut(BaseModel):
    text: str
    evidence: list[ReceiptEvidenceOut]


class ReceiptAcceptedRowOut(BaseModel):
    row_ref: UUID
    occurred_on: str
    kind: str
    amount_minor: int
    currency: str
    account_alias: str
    category: str | None


class ReceiptCandidateOut(BaseModel):
    candidate_ref: UUID
    revision: int
    confirmed_digest: str = Field(pattern=_SHA256_PATTERN)
    source: ReceiptSourceOut
    fields: dict[str, Any]
    needs_review: list[str]
    status: Literal["pending", "accepted"]
    possible_duplicate: bool
    source_text: str
    parser_version: str
    accepted_row: ReceiptAcceptedRowOut | None


class ReceiptCandidatePage(BaseModel):
    candidates: list[ReceiptCandidateOut]


class ReceiptCandidateProposalOut(BaseModel):
    status: Literal[
        "candidate_needs_review", "processor_unavailable", "processor_failed", "no_text", "no_candidate"
    ]
    effective_ledger_change: Literal[False]
    candidate_ref: UUID | None = None
    needs_review: bool | None = None
    fields: dict[str, Any] | None = None
    review_reasons: list[str] | None = None
    source_ref: UUID | None = None
    source_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    replayed: bool | None = None


class ReceiptConfirmationPreviewOut(BaseModel):
    candidate_ref: UUID
    revision: int
    confirmed_digest: str = Field(pattern=_SHA256_PATTERN)
    row: ReceiptAcceptedRowOut
    source: ReceiptSourceOut
    possible_duplicate: bool
    duplicate_entry_refs: list[UUID]
    duplicate_scan_incomplete: bool


class ReceiptCandidateAcceptanceOut(BaseModel):
    candidate_ref: UUID
    revision: int
    confirmed_digest: str = Field(pattern=_SHA256_PATTERN)
    status: Literal["accepted"]
    entry_refs: list[UUID]
    replayed: bool


class ReceiptCandidateProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_ref: UUID
    source_sha256: str = Field(pattern=_SHA256_PATTERN, strict=True)
    idempotency_key: UUID


class ReceiptConfirmationPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1, le=1_000_000, strict=True)
    candidate_digest: str = Field(pattern=_SHA256_PATTERN, strict=True)
    row: dict[str, Any]


class ReceiptCandidateConfirmation(ReceiptConfirmationPreview):
    confirmed_digest: str = Field(pattern=_SHA256_PATTERN, strict=True)
    duplicate_confirmed: bool = False
    owner_confirmed: Literal[True]
    idempotency_key: UUID


def _entry_out(view: Any) -> FinanceEntryOut:
    return FinanceEntryOut(
        row_ref=view.row_ref,
        occurred_on=view.occurred_on,
        kind=view.kind,
        amount_minor=view.amount_minor,
        currency=view.currency,
        account_alias=view.account_alias,
        category=view.category,
        source=view.source,
        confirmed_digest=view.content_digest,
        confirmed_by_kind="user_action",
    )


def _dataset_out(view: Any) -> FinanceDatasetOut:
    return FinanceDatasetOut(
        dataset_ref=view.dataset_ref,
        member_count=view.member_count,
        complete=view.complete,
        members_digest=view.members_digest,
        replayed=view.replayed,
    )


def _finance_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, (FinanceConflict, FinanceDuplicateRowRef, FinanceDigestMismatch)):
        return HTTPException(409, "finance_conflict")
    if isinstance(exc, FinanceActorNotAllowed):
        return HTTPException(403, "owner_action_required")
    if isinstance(exc, FinanceContractError):
        return HTTPException(422, "finance_validation")
    return HTTPException(400, "finance_operation_failed")


def _receipt_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, (FinanceConflict, FinanceDuplicateRowRef)):
        return HTTPException(409, "finance_conflict")
    if isinstance(exc, FinanceActorNotAllowed):
        return HTTPException(403, "owner_action_required")
    if isinstance(exc, FinanceContractError):
        return HTTPException(422, "finance_validation")
    from ...services.personal_ledger_service import (
        LedgerConflictError,
        LedgerError,
        LedgerNotFoundError,
        LedgerPermissionError,
        LedgerValidationError,
    )

    if isinstance(exc, FinanceAttachmentSourceError):
        return HTTPException(409 if exc.code == "source_hash_mismatch" else 404, exc.code)
    if isinstance(exc, LedgerNotFoundError):
        return HTTPException(404, exc.code)
    if isinstance(exc, LedgerConflictError):
        return HTTPException(409, exc.code)
    if isinstance(exc, LedgerPermissionError):
        return HTTPException(403, exc.code)
    if isinstance(exc, LedgerValidationError):
        return HTTPException(422, exc.code)
    if isinstance(exc, LedgerError):
        return HTTPException(400, exc.code)
    return HTTPException(400, "receipt_operation_failed")


def register_personal_finance_routes(
    app: FastAPI,
    server,
    *,
    finance_service: PersonalFinanceService | None = None,
    dataset_service: PersonalFinanceDatasetService | None = None,
    receipt_service: PersonalFinanceReceiptService | None = None,
    receipt_runtime_provider=None,
    resolver_factory=None,
) -> None:
    """Register the source-level routes; server registration is owned by integration."""

    session_factory = lambda: server._db_manager.get_session()
    entries = finance_service or PersonalFinanceService(SqlPersonalFinanceRepository(session_factory))
    datasets = dataset_service or PersonalFinanceDatasetService(session_factory)
    receipts = receipt_service or PersonalFinanceReceiptService(session_factory, finance_service=entries)

    def no_store(response: Response) -> None:
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["Pragma"] = "no-cache"

    async def owner(request: Request, response: Response) -> str:
        no_store(response)
        owner_id = await require_interactive_owner(server, request)
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if media_type != "application/json" or request.headers.get("sec-fetch-site") == "cross-site":
                raise HTTPException(403, "same_origin_json_required")
        return owner_id

    async def guard_for(request: Request, owner_id: str):
        current = await require_interactive_owner(server, request)
        if current != owner_id:
            raise HTTPException(403, "owner_scope_changed")

    async def open_resolver(owner_id: str):
        if resolver_factory is not None:
            resolver = resolver_factory(UUID(owner_id))
            if inspect.isawaitable(resolver):
                resolver = await resolver
            return None, resolver
        session = server._db_manager.get_session()
        if inspect.isawaitable(session):
            session = await session
        resolver = SearchSourceResolver(
            session,
            SearchScope(
                user_id=UUID(owner_id),
                project_id=None,
                project_scope_restricted=True,
            ),
        )
        return session, resolver

    async def close_session(session) -> None:
        if session is not None:
            result = session.close()
            if inspect.isawaitable(result):
                await result

    def operation_guard(request: Request, owner_id: str):
        async def guard():
            await guard_for(request, owner_id)

        return guard

    @app.post("/api/personal-finance/entries/preview", response_model=FinanceEntryPreviewResponse)
    async def preview_entry(body: FinanceEntryPreviewRequest, request: Request, owner_id: str = Depends(owner)):
        guard = operation_guard(request, owner_id)
        try:
            parsed_row = FinanceRow.from_mapping(body.row)
            parsed_source = None if body.source is None else FinanceSource.from_mapping(body.source)
            await guard()
            return FinanceEntryPreviewResponse(
                row=parsed_row.to_canonical(),
                source=None if parsed_source is None else parsed_source.to_canonical(),
                confirmed_digest=compute_confirmed_digest(
                    owner_user_id=UUID(owner_id), row=parsed_row, source=parsed_source
                ),
            )
        except (FinanceActorNotAllowed, FinanceContractError) as exc:
            raise _finance_http_error(exc) from None

    @app.get("/api/personal-finance/entries", response_model=FinanceEntryPage)
    async def list_entries(
        request: Request,
        limit: int = Query(default=1000, ge=1, le=1000),
        owner_id: str = Depends(owner),
    ):
        try:
            rows = await entries.list_confirmed_entries(
                finance_owner_actor(UUID(owner_id)), limit=limit,
                guard=operation_guard(request, owner_id),
            )
            return FinanceEntryPage(entries=[_entry_out(row) for row in rows])
        except FinanceContractError as exc:
            raise _finance_http_error(exc) from None

    @app.post("/api/personal-finance/entries", response_model=FinanceEntryWriteResponse, status_code=201)
    async def add_entry(
        body: FinanceEntryCreate,
        request: Request,
        response: Response,
        owner_id: str = Depends(owner),
    ):
        try:
            view = await entries.append_confirmed_entry(
                finance_owner_actor(UUID(owner_id)), row=body.row, source=body.source,
                confirmed_digest=body.confirmed_digest, owner_confirmed=body.owner_confirmed,
                idempotency_key=body.idempotency_key, guard=operation_guard(request, owner_id),
            )
        except FinanceContractError as exc:
            raise _finance_http_error(exc) from None
        response.status_code = 200 if view.replayed else 201
        return FinanceEntryWriteResponse(entry=_entry_out(view), replayed=view.replayed)

    @app.get("/api/personal-finance/datasets", response_model=FinanceDatasetPage)
    async def list_datasets(
        request: Request,
        limit: int = Query(default=50, ge=1, le=100),
        owner_id: str = Depends(owner),
    ):
        try:
            found = await datasets.list_datasets(
                owner_user_id=UUID(owner_id), limit=limit,
                guard=operation_guard(request, owner_id),
            )
            return FinanceDatasetPage(datasets=[_dataset_out(item) for item in found])
        except FinanceContractError as exc:
            raise _finance_http_error(exc) from None

    @app.post("/api/personal-finance/datasets/accept", response_model=FinanceDatasetOut, status_code=201)
    async def accept_dataset(
        body: FinanceDatasetAccept,
        request: Request,
        response: Response,
        owner_id: str = Depends(owner),
    ):
        try:
            accepted = await datasets.accept_dataset(
                finance_owner_actor(UUID(owner_id)), row_refs=body.row_refs,
                owner_confirmed=body.owner_confirmed, idempotency_key=body.idempotency_key,
                guard=operation_guard(request, owner_id),
            )
        except FinanceContractError as exc:
            raise _finance_http_error(exc) from None
        response.status_code = 200 if accepted.replayed else 201
        return _dataset_out(accepted)

    @app.get("/api/personal-finance/datasets/{dataset_ref}/summary", response_model=FinanceSummaryResponse)
    async def dataset_summary(
        dataset_ref: UUID,
        request: Request,
        month: str = Query(pattern=r"^[0-9]{4}-(?:0[1-9]|1[0-2])$"),
        owner_id: str = Depends(owner),
    ):
        guard = operation_guard(request, owner_id)
        try:
            snapshot = await datasets.load_snapshot(
                owner_user_id=UUID(owner_id), dataset_ref=dataset_ref, guard=guard
            )
            await guard()
            return summarize_finance_snapshot(
                snapshot,
                owner_user_id=UUID(owner_id),
                accepted_dataset_ref=dataset_ref,
                month=month,
            )
        except FinanceContractError as exc:
            raise _finance_http_error(exc) from None
        except PermissionError:
            raise HTTPException(404, "dataset_not_found") from None
        except FinanceSummaryOverflowError:
            raise HTTPException(422, "finance_summary_overflow") from None
        except ValueError:
            raise HTTPException(422, "finance_summary_invalid") from None

    @app.get("/api/personal-finance/receipts/sources", response_model=list[ReceiptSourceOut])
    async def receipt_sources(request: Request, owner_id: str = Depends(owner)):
        session = None
        try:
            session, resolver = await open_resolver(owner_id)
            return await receipts.list_sources(
                owner_user_id=UUID(owner_id), resolver=resolver,
                guard=operation_guard(request, owner_id),
            )
        except Exception as exc:
            from ...services.personal_ledger_service import LedgerError
            if isinstance(exc, LedgerError):
                raise _receipt_http_error(exc) from None
            raise
        finally:
            await close_session(session)

    @app.get("/api/personal-finance/receipts/candidates", response_model=list[ReceiptCandidateOut])
    async def receipt_candidates(
        request: Request,
        source_ref: UUID | None = None,
        limit: int = Query(default=50, ge=1, le=50),
        owner_id: str = Depends(owner),
    ):
        try:
            return await receipts.list_candidates(
                owner_user_id=UUID(owner_id), source_ref=source_ref, limit=limit,
                guard=operation_guard(request, owner_id),
            )
        except Exception as exc:
            from ...services.personal_ledger_service import LedgerError
            if isinstance(exc, LedgerError):
                raise _receipt_http_error(exc) from None
            raise

    @app.post("/api/personal-finance/receipts/candidates", response_model=ReceiptCandidateProposalOut)
    async def propose_receipt_candidate(
        body: ReceiptCandidateProposal,
        request: Request,
        owner_id: str = Depends(owner),
    ):
        owner_uuid = UUID(owner_id)
        session_id = str(uuid4())
        token = set_turn_context(user_id=str(owner_uuid), session_id=session_id)
        session = None
        try:
            session, resolver = await open_resolver(owner_id)
            provider = receipt_runtime_provider or getattr(server, "personal_finance_receipt_runtime_provider", None)
            route: ReceiptProcessorRoute | None = None
            processor: ReceiptTextProcessor | None = None
            if callable(provider):
                runtime = provider(owner_uuid, session_id)
                if inspect.isawaitable(runtime):
                    runtime = await runtime
                if isinstance(runtime, tuple) and len(runtime) == 2:
                    route, processor = runtime
                elif isinstance(runtime, dict):
                    route, processor = runtime.get("route"), runtime.get("processor")
            return await receipts.propose_from_attachment(
                owner_user_id=owner_uuid, resolver=resolver,
                source_ref=body.source_ref, expected_sha256=body.source_sha256,
                idempotency_key=body.idempotency_key, route=route, processor=processor,
                guard=operation_guard(request, owner_id),
            )
        except Exception as exc:
            from ...services.personal_ledger_service import LedgerError
            if isinstance(exc, (LedgerError, FinanceAttachmentSourceError)):
                raise _receipt_http_error(exc) from None
            raise
        finally:
            await close_session(session)
            reset_turn_context(token)

    @app.get("/api/personal-finance/receipts/candidates/{candidate_ref}", response_model=ReceiptCandidateOut)
    async def get_receipt_candidate(
        candidate_ref: UUID, request: Request, owner_id: str = Depends(owner)
    ):
        try:
            return await receipts.get_candidate(
                owner_user_id=UUID(owner_id), candidate_ref=candidate_ref,
                guard=operation_guard(request, owner_id),
            )
        except Exception as exc:
            from ...services.personal_ledger_service import LedgerError
            if isinstance(exc, LedgerError):
                raise _receipt_http_error(exc) from None
            raise

    @app.get("/api/personal-finance/receipts/candidates/{candidate_ref}/preview", response_model=ReceiptSourcePreviewOut)
    async def receipt_candidate_preview(
        candidate_ref: UUID, request: Request, owner_id: str = Depends(owner)
    ):
        try:
            view = await receipts.get_candidate(
                owner_user_id=UUID(owner_id), candidate_ref=candidate_ref,
                guard=operation_guard(request, owner_id),
            )
        except Exception as exc:
            from ...services.personal_ledger_service import LedgerError
            if isinstance(exc, LedgerError):
                raise _receipt_http_error(exc) from None
            raise
        source = view["source"]
        evidence = []
        for item in view.get("fields", {}).values():
            span = item.get("span") if isinstance(item, dict) else None
            if isinstance(span, dict) and type(span.get("start")) is int and type(span.get("end")) is int:
                evidence.append({
                    "source_ref": source["source_ref"],
                    "source_sha256": source["source_sha256"],
                    "page": None,
                    "span_start": span["start"],
                    "span_end": span["end"],
                })
        return {"text": view["source_text"], "evidence": evidence[:20]}

    @app.post("/api/personal-finance/receipts/candidates/{candidate_ref}/confirmation-preview", response_model=ReceiptConfirmationPreviewOut)
    async def preview_receipt_confirmation(
        candidate_ref: UUID,
        body: ReceiptConfirmationPreview,
        request: Request,
        owner_id: str = Depends(owner),
    ):
        session = None
        try:
            session, resolver = await open_resolver(owner_id)
            return await receipts.preview_confirmation(
                owner_user_id=UUID(owner_id), resolver=resolver, candidate_ref=candidate_ref,
                revision=body.revision, candidate_digest=body.candidate_digest,
                row=body.row, guard=operation_guard(request, owner_id),
            )
        except Exception as exc:
            from ...services.personal_ledger_service import LedgerError
            if isinstance(exc, (LedgerError, FinanceAttachmentSourceError)):
                raise _receipt_http_error(exc) from None
            raise
        finally:
            await close_session(session)

    @app.post("/api/personal-finance/receipts/candidates/{candidate_ref}/confirm", response_model=ReceiptCandidateAcceptanceOut)
    async def confirm_receipt_candidate(
        candidate_ref: UUID,
        body: ReceiptCandidateConfirmation,
        request: Request,
        response: Response,
        owner_id: str = Depends(owner),
    ):
        session = None
        try:
            session, resolver = await open_resolver(owner_id)
            result = await receipts.confirm_candidate(
                owner_user_id=UUID(owner_id), resolver=resolver, candidate_ref=candidate_ref,
                revision=body.revision, candidate_digest=body.candidate_digest,
                row=body.row, confirmed_digest=body.confirmed_digest,
                duplicate_confirmed=body.duplicate_confirmed,
                owner_confirmed=body.owner_confirmed,
                idempotency_key=body.idempotency_key,
                guard=operation_guard(request, owner_id),
            )
            response.status_code = 200 if result["replayed"] else 201
            return result
        except Exception as exc:
            from ...services.personal_ledger_service import LedgerError
            if isinstance(exc, (LedgerError, FinanceAttachmentSourceError, FinanceContractError)):
                raise _receipt_http_error(exc) from None
            raise
        finally:
            await close_session(session)
