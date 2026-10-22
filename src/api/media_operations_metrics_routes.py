"""Typed HTTP boundary for MediaOps WS7/WS8 evidence operations.

This is a direct, provider-neutral facade: callers can read/list immutable
observations, create snapshots/experiments/events and propose learning.  There
are intentionally no approval, execution, publication or reconciliation
routes here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Callable, Literal

from fastapi import APIRouter, Depends, File, Header, Query, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..services.media_operations_learning_service import MediaOperationsLearningService
from .media_operations_routes import _actor, _invoke, _principal_projection, _with_session


class _MetricsCommandModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class _MetricsResponseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class EvidenceRefRequest(_MetricsCommandModel):
    kind: Literal["url", "artifact", "file", "record", "snapshot", "experiment", "result", "revenue_event", "publication"]
    url: str | None = Field(default=None, max_length=4000)
    ref: str | None = Field(default=None, max_length=164)
    sha256: str | None = Field(default=None, min_length=64, max_length=64, pattern=r"^[0-9a-fA-F]{64}$")
    mime_type: str | None = Field(default=None, max_length=255)
    role: str | None = Field(default=None, max_length=64)
    label: str | None = Field(default=None, max_length=255)
    note: str | None = Field(default=None, max_length=2000)
    format_version: str | None = Field(default=None, max_length=64)
    row_index: int | None = Field(default=None, ge=0, le=1_000_000)


class VariantGroupRequest(_MetricsCommandModel):
    name: str = Field(min_length=1, max_length=64)
    variant_refs: list[str] = Field(default_factory=list, max_length=64)


class MetricSnapshotInputRequest(_MetricsCommandModel):
    """One expected-hash MetricSnapshot input for server-side analysis."""

    metric_snapshot_id: str | None = Field(default=None, max_length=164)
    snapshot_id: str | None = Field(default=None, max_length=164)
    metric_snapshot_hash: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-fA-F]{64}$",
    )
    expected_snapshot_hash: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-fA-F]{64}$",
    )
    expected_hash: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-fA-F]{64}$",
    )
    snapshot_hash: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-fA-F]{64}$",
    )
    group_name: str | None = Field(default=None, min_length=1, max_length=64)
    variant_group: str | None = Field(default=None, min_length=1, max_length=64)
    variant_ref: str | None = Field(default=None, max_length=164)

    @model_validator(mode="after")
    def _require_group(self) -> "MetricSnapshotInputRequest":
        if self.group_name is None and self.variant_group is None:
            raise ValueError("group_name is required")
        return self


class MetricSnapshotCreateRequest(_MetricsCommandModel):
    project_id: str | None = None
    persona_ref: str | None = Field(default=None, max_length=164)
    persona_id: str | None = Field(default=None, max_length=164)
    platform_account_ref: str | None = Field(default=None, max_length=164)
    account_id: str | None = Field(default=None, max_length=164)
    content_variant_ref: str | None = Field(default=None, max_length=164)
    content_variant_id: str | None = Field(default=None, max_length=164)
    publication_ref: str | None = Field(default=None, max_length=164)
    publication_id: str | None = Field(default=None, max_length=164)
    period_start: datetime | None = None
    period_end: datetime | None = None
    observed_at: datetime
    # Provider-authoritative ``source=api`` is intentionally absent from the
    # public command model.  Only a server-owned ingestion adapter can stamp
    # that value inside the service boundary.
    source: Literal["manual", "imported"] = "manual"
    provider: str | None = Field(default=None, max_length=128)
    normalized_metrics: dict[str, int | float] = Field(default_factory=dict, max_length=32)
    platform_metrics: dict[str, dict[str, int | float]] = Field(default_factory=dict, max_length=16)
    provenance: list[EvidenceRefRequest] = Field(default_factory=list, max_length=32)
    completeness: Literal["complete", "partial", "unknown"] = "unknown"
    ingestion_status: Literal["accepted", "rejected", "superseded"] = "accepted"
    import_status: Literal["accepted", "rejected", "superseded"] | None = None
    correction_of_id: str | None = None


class ExperimentCreateRequest(_MetricsCommandModel):
    project_id: str | None = None
    name: str = Field(min_length=1, max_length=255)
    hypothesis: str = Field(min_length=1, max_length=8000)
    persona_refs: list[str] = Field(default_factory=list, max_length=32)
    persona_ids: list[str] = Field(default_factory=list, max_length=32)
    account_refs: list[str] = Field(default_factory=list, max_length=32)
    account_ids: list[str] = Field(default_factory=list, max_length=32)
    variant_groups: list[VariantGroupRequest] = Field(min_length=2, max_length=12)
    primary_metric: str = Field(min_length=1, max_length=64)
    secondary_metrics: list[str] = Field(default_factory=list, max_length=16)
    window_start: datetime
    window_end: datetime
    minimum_sample_size: int = Field(default=1, ge=1, le=10_000_000)
    status: Literal["draft", "running"] = "draft"


class ExperimentResultCreateRequest(_MetricsCommandModel):
    sample_size: int | None = Field(default=None, ge=0, le=10_000_000)
    sample_sizes: dict[str, int] = Field(default_factory=dict, max_length=12)
    group_metrics: dict[str, dict[str, int | float]] | None = Field(default=None, max_length=12)
    metrics: dict[str, dict[str, int | float]] | None = Field(default=None, max_length=12)
    winner_variant_ref: str | None = None
    confidence: float = Field(ge=0, le=1)
    uncertainty: float = Field(ge=0, le=1)
    evidence_refs: list[EvidenceRefRequest] = Field(max_length=32)
    conclusion: str = Field(default="", max_length=8000)
    inputs: list[MetricSnapshotInputRequest] | None = Field(default=None, max_length=1000)
    analysis_method: str | None = Field(default=None, min_length=1, max_length=64)
    analysis_version: str | None = Field(default=None, min_length=1, max_length=32)
    analysis_design: Literal["controlled", "observational"] | None = None
    assignment_evidence: list[EvidenceRefRequest] | None = Field(default=None, max_length=32)
    exposure_evidence: list[EvidenceRefRequest] | None = Field(default=None, max_length=32)


class ExperimentAnalysisRequest(_MetricsCommandModel):
    """Read-only analysis input; no result/idempotency mutation is possible."""

    inputs: list[MetricSnapshotInputRequest] | None = Field(default=None, max_length=1000)
    sample_size: int | None = Field(default=None, ge=0, le=10_000_000)
    sample_sizes: dict[str, int] | None = Field(default=None, max_length=12)
    group_metrics: dict[str, dict[str, int | float]] | None = Field(default=None, max_length=12)
    metrics: dict[str, dict[str, int | float]] | None = Field(default=None, max_length=12)
    analysis_method: str | None = Field(default=None, min_length=1, max_length=64)
    analysis_version: str | None = Field(default=None, min_length=1, max_length=32)
    analysis_design: Literal["controlled", "observational"] | None = None
    assignment_evidence: list[EvidenceRefRequest] | None = Field(default=None, max_length=32)
    exposure_evidence: list[EvidenceRefRequest] | None = Field(default=None, max_length=32)


class RevenueEventCreateRequest(_MetricsCommandModel):
    project_id: str | None = None
    persona_ref: str | None = Field(default=None, max_length=164)
    platform_account_ref: str | None = Field(default=None, max_length=164)
    account_ref: str | None = Field(default=None, max_length=164)
    platform: str | None = Field(default=None, max_length=32)
    content_ref: str | None = Field(default=None, max_length=164)
    content_variant_ref: str | None = Field(default=None, max_length=164)
    publication_ref: str | None = Field(default=None, max_length=164)
    product_ref: str | None = Field(default=None, max_length=164)
    # Estimated/manual and imported observations are accepted here.  Settled
    # provider revenue uses the private server-owned ingestion boundary.
    source: Literal["manual", "imported"] = "manual"
    provider: str | None = Field(default=None, max_length=128)
    event_type: Literal["sale", "refund", "chargeback", "adjustment", "reversal"] = "sale"
    gross_amount: float
    net_amount: float
    currency: str = Field(min_length=3, max_length=3)
    event_at: datetime
    settlement_at: datetime | None = None
    evidence: list[EvidenceRefRequest] = Field(max_length=32)
    correction_of_id: str | None = None


class PatreonRevenueImportRequest(_MetricsCommandModel):
    """Bounded manual Patreon CSV/row import (never provider API authority)."""

    rows: list[dict[str, Any]] | None = Field(default=None, max_length=1000)
    csv_text: str | None = Field(default=None, max_length=2_000_000)

    @model_validator(mode="after")
    def _require_one_input(self) -> "PatreonRevenueImportRequest":
        if (self.rows is None) == (self.csv_text is None):
            raise ValueError("provide exactly one of rows or csv_text")
        return self


class RevenueCostRecordCreateRequest(_MetricsCommandModel):
    """Three cost categories allocated wholly to the selected revenue event."""

    currency: str = Field(min_length=3, max_length=3, pattern=r"^[A-Za-z]{3}$")
    source: Literal["manual", "imported"] = "manual"
    basis: Literal["estimated", "settled"]
    fee_amount: float | None = Field(default=None, strict=True, allow_inf_nan=False, ge=-1e12, le=1e12)
    production_cost_amount: float | None = Field(default=None, strict=True, allow_inf_nan=False, ge=-1e12, le=1e12)
    human_cost_amount: float | None = Field(default=None, strict=True, allow_inf_nan=False, ge=-1e12, le=1e12)
    human_minutes: float | None = Field(default=None, strict=True, allow_inf_nan=False, ge=0, le=1e12)
    missing_reasons: dict[str, str] = Field(default_factory=dict, max_length=4)
    coverage_complete: bool = Field(default=False, strict=True)
    coverage_note: str = Field(min_length=1, max_length=2000)
    evidence: list[EvidenceRefRequest] = Field(min_length=1, max_length=32)
    correction_of_id: str | None = None


class CostEvidenceRefResponse(EvidenceRefRequest):
    # Normalized URL evidence uses ref, unlike the command's url spelling.
    ref: str | None = Field(default=None, max_length=4000)
    evidence_hash: str


class RevenueCostRecordResponse(RevenueCostRecordCreateRequest):
    id: str
    revenue_event_id: str
    owner_user_id: str
    project_id: str | None
    revenue_event_hash: str
    cost_entry_id: str | None
    revision: int
    attribution_method: Literal["direct_event"]
    window_start: datetime
    window_end: datetime
    cost_hash: str
    idempotency_key: str
    created_by: str | None
    created_at: datetime
    cost_authority: str
    profit_amount: float | None
    profit_status: str
    evidence: list[CostEvidenceRefResponse]


class RevenueCostRecordHistoryResponse(_MetricsResponseModel):
    items: list[RevenueCostRecordResponse]
    effective: RevenueCostRecordResponse | None
    has_more: bool


class MediaCostEntryCreateRequest(RevenueCostRecordCreateRequest):
    project_id: str | None = None
    persona_ref: str = Field(min_length=1, max_length=164)
    content_ref: str | None = Field(default=None, max_length=164)
    window_start: datetime
    window_end: datetime


class MediaCostEntryResponse(MediaCostEntryCreateRequest):
    id: str
    entry_id: str
    revision: int
    owner_user_id: str
    entry_hash: str
    idempotency_key: str
    created_by: str | None
    created_at: datetime
    evidence: list[CostEvidenceRefResponse]
    allocation_status: Literal["unallocated", "allocated"]
    allocated_revenue_event_id: str | None
    unallocated_fee_amount: float | None
    unallocated_production_cost_amount: float | None
    unallocated_human_cost_amount: float | None
    unallocated_human_minutes: float | None


class MediaCostEntryHistoryResponse(_MetricsResponseModel):
    items: list[MediaCostEntryResponse]
    effective: MediaCostEntryResponse | None
    has_more: bool


class MediaCostEntryAllocateRequest(_MetricsCommandModel):
    correction_of_id: str | None = None


class LearningProposalCreateRequest(_MetricsCommandModel):
    project_id: str | None = None
    subject_type: Literal["persona", "account", "content", "publication", "experiment", "general"]
    subject_ref: str = Field(min_length=1, max_length=164)
    proposal_type: Literal["learning", "content", "timing", "audience", "pricing"] = "learning"
    title: str = Field(min_length=1, max_length=255)
    summary: str = Field(min_length=1, max_length=4000)
    recommendation: str = Field(min_length=1, max_length=8000)
    evidence_refs: list[EvidenceRefRequest] = Field(max_length=32)
    human_decision_refs: list[str] = Field(default_factory=list, max_length=64)
    target_fields: list[str] = Field(default_factory=list, max_length=32)
    proposed_before: dict[str, Any] = Field(default_factory=dict, max_length=32)
    proposed_after: dict[str, Any] = Field(default_factory=dict, max_length=32)
    expected_persona_revision_id: str | None = None
    expected_persona_revision_version: int | None = Field(default=None, ge=1)
    expected_persona_revision_hash: str | None = Field(default=None, min_length=64, max_length=64, pattern=r"^[0-9a-fA-F]{64}$")
    window_start: datetime
    window_end: datetime
    confidence: float = Field(ge=0, le=1)
    uncertainty: float = Field(ge=0, le=1)


ResponseObject = dict[str, Any]


def _dump(payload: BaseModel) -> dict[str, Any]:
    """Strip optional ``None`` fields before strict service normalization."""

    return payload.model_dump(mode="json", exclude_none=True)


def create_media_operations_metrics_router(
    get_db_manager: Callable[[], Any],
    get_user_from_request: Callable[..., Any],
    require_auth_dependency: Callable[..., Any],
) -> APIRouter:
    router = APIRouter(prefix="/api/operations/media", tags=["operations-media"])
    media = MediaOperationsLearningService()

    async def current_actor(request: Request) -> dict[str, Any]:
        user = await _actor(get_user_from_request, request)
        return _principal_projection(user)

    def key_header() -> Any:
        return Header(alias="Idempotency-Key", min_length=1, max_length=255)

    @router.get("/metrics", response_model=list[ResponseObject], operation_id="media_list_metric_snapshots")
    async def list_metrics(request: Request, project_id: str | None = Query(default=None), limit: int = Query(default=100, ge=1, le=100), offset: int = Query(default=0, ge=0), _: Any = Depends(require_auth_dependency)) -> Any:
        actor = await current_actor(request)
        return await _with_session(get_db_manager, lambda session: _invoke(media.list_metric_snapshots, session, actor, project_id=project_id, limit=limit, offset=offset))

    @router.get("/metrics/ingestion-runs", response_model=list[ResponseObject], operation_id="media_list_metric_ingestion_runs")
    async def list_metric_ingestion_runs(request: Request, project_id: str | None = Query(default=None), limit: int = Query(default=100, ge=1, le=100), offset: int = Query(default=0, ge=0), _: Any = Depends(require_auth_dependency)) -> Any:
        actor = await current_actor(request)
        return await _with_session(get_db_manager, lambda session: _invoke(media.list_metric_ingestion_runs, session, actor, project_id=project_id, limit=limit, offset=offset))

    @router.get("/metrics/ingestion-runs/{run_id}", response_model=ResponseObject, operation_id="media_get_metric_ingestion_run")
    async def get_metric_ingestion_run(run_id: str, request: Request, _: Any = Depends(require_auth_dependency)) -> Any:
        actor = await current_actor(request)
        return await _with_session(get_db_manager, lambda session: _invoke(media.get_metric_ingestion_run, session, actor, run_id))

    @router.post("/metrics", response_model=ResponseObject, operation_id="media_create_metric_snapshot")
    async def create_metric(payload: MetricSnapshotCreateRequest, request: Request, idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)], _: Any = Depends(require_auth_dependency)) -> Any:
        actor = await current_actor(request)
        return await _with_session(get_db_manager, lambda session: _invoke(media.create_metric_snapshot, session, actor, **_dump(payload), idempotency_key=idempotency_key))

    @router.get("/metrics/{snapshot_id}", response_model=ResponseObject, operation_id="media_get_metric_snapshot")
    async def get_metric(snapshot_id: str, request: Request, _: Any = Depends(require_auth_dependency)) -> Any:
        actor = await current_actor(request)
        return await _with_session(get_db_manager, lambda session: _invoke(media.get_metric_snapshot, session, actor, snapshot_id))

    @router.get("/metrics/{snapshot_id}/effective", response_model=ResponseObject, operation_id="media_get_effective_metric_snapshot")
    async def get_effective_metric(snapshot_id: str, request: Request, _: Any = Depends(require_auth_dependency)) -> Any:
        actor = await current_actor(request)
        return await _with_session(get_db_manager, lambda session: _invoke(media.get_effective_metric_snapshot, session, actor, snapshot_id))

    @router.get("/experiments", response_model=list[ResponseObject], operation_id="media_list_experiments")
    async def list_experiments(request: Request, project_id: str | None = Query(default=None), limit: int = Query(default=100, ge=1, le=100), offset: int = Query(default=0, ge=0), _: Any = Depends(require_auth_dependency)) -> Any:
        actor = await current_actor(request)
        return await _with_session(get_db_manager, lambda session: _invoke(media.list_experiments, session, actor, project_id=project_id, limit=limit, offset=offset))

    @router.post("/experiments", response_model=ResponseObject, operation_id="media_create_experiment")
    async def create_experiment(payload: ExperimentCreateRequest, request: Request, idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)], _: Any = Depends(require_auth_dependency)) -> Any:
        actor = await current_actor(request)
        return await _with_session(get_db_manager, lambda session: _invoke(media.create_experiment, session, actor, **_dump(payload), idempotency_key=idempotency_key))

    @router.get("/experiments/{experiment_id}", response_model=ResponseObject, operation_id="media_get_experiment")
    async def get_experiment(experiment_id: str, request: Request, _: Any = Depends(require_auth_dependency)) -> Any:
        actor = await current_actor(request)
        return await _with_session(get_db_manager, lambda session: _invoke(media.get_experiment, session, actor, experiment_id))

    @router.get("/experiments/{experiment_id}/analysis", response_model=ResponseObject, operation_id="media_analyze_experiment")
    async def analyze_experiment(experiment_id: str, request: Request, _: Any = Depends(require_auth_dependency)) -> Any:
        """Return the latest server-calculated result without mutating evidence."""

        actor = await current_actor(request)
        return await _with_session(get_db_manager, lambda session: _invoke(media.analyze_experiment, session, actor, experiment_id))

    @router.post("/experiments/{experiment_id}/analysis", response_model=ResponseObject, operation_id="media_analyze_experiment_inputs")
    async def analyze_experiment_inputs(experiment_id: str, payload: ExperimentAnalysisRequest, request: Request, _: Any = Depends(require_auth_dependency)) -> Any:
        actor = await current_actor(request)
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                media.analyze_experiment,
                session,
                actor,
                experiment_id,
                **_dump(payload),
            ),
        )

    @router.post("/experiments/{experiment_id}/results", response_model=ResponseObject, operation_id="media_record_experiment_result")
    async def record_result(experiment_id: str, payload: ExperimentResultCreateRequest, request: Request, idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)], _: Any = Depends(require_auth_dependency)) -> Any:
        actor = await current_actor(request)
        return await _with_session(get_db_manager, lambda session: _invoke(media.record_experiment_result, session, actor, experiment_id, **_dump(payload), idempotency_key=idempotency_key))

    @router.get("/experiments/{experiment_id}/results", response_model=list[ResponseObject], operation_id="media_list_experiment_results")
    async def list_results(experiment_id: str, request: Request, limit: int = Query(default=100, ge=1, le=100), offset: int = Query(default=0, ge=0), _: Any = Depends(require_auth_dependency)) -> Any:
        actor = await current_actor(request)
        return await _with_session(get_db_manager, lambda session: _invoke(media.list_experiment_results, session, actor, experiment_id, limit=limit, offset=offset))

    @router.get("/revenue-events", response_model=list[ResponseObject], operation_id="media_list_revenue_events")
    async def list_revenue(request: Request, project_id: str | None = Query(default=None), limit: int = Query(default=100, ge=1, le=100), offset: int = Query(default=0, ge=0), _: Any = Depends(require_auth_dependency)) -> Any:
        actor = await current_actor(request)
        return await _with_session(get_db_manager, lambda session: _invoke(media.list_revenue_events, session, actor, project_id=project_id, limit=limit, offset=offset))

    @router.post("/revenue-events", response_model=ResponseObject, operation_id="media_create_revenue_event")
    async def create_revenue(payload: RevenueEventCreateRequest, request: Request, idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)], _: Any = Depends(require_auth_dependency)) -> Any:
        actor = await current_actor(request)
        return await _with_session(get_db_manager, lambda session: _invoke(media.create_revenue_event, session, actor, **_dump(payload), idempotency_key=idempotency_key))

    @router.post("/revenue-events/import/patreon", response_model=ResponseObject, operation_id="media_import_patreon_csv_revenue")
    async def import_patreon_revenue(payload: PatreonRevenueImportRequest, request: Request, idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)], _: Any = Depends(require_auth_dependency)) -> Any:
        actor = await current_actor(request)
        values = _dump(payload)
        return await _with_session(get_db_manager, lambda session: _invoke(media.import_patreon_csv_revenue, session, actor, **values, idempotency_key=idempotency_key))

    @router.post(
        "/revenue-events/import/patreon-csv",
        response_model=ResponseObject,
        operation_id="media_import_patreon_csv_file",
    )
    async def import_patreon_csv_file(
        request: Request,
        idempotency_key: Annotated[
            str,
            Header(alias="Idempotency-Key", min_length=1, max_length=255),
        ],
        file: UploadFile = File(...),
        project_id: str | None = Query(default=None),
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        """Import exact uploaded Patreon bytes with durable file provenance."""

        actor = await current_actor(request)
        try:
            # Read at most one byte over the service limit so a malformed or
            # oversized upload cannot force an unbounded in-memory buffer.
            raw_bytes = await file.read(2 * 1024 * 1024 + 1)
        finally:
            close = getattr(file, "close", None)
            if callable(close):
                await close()
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                media.import_patreon_csv_revenue,
                session,
                actor,
                csv_bytes=raw_bytes,
                project_id=project_id,
                idempotency_key=idempotency_key,
            ),
        )

    @router.get("/revenue-events/{event_id}", response_model=ResponseObject, operation_id="media_get_revenue_event")
    async def get_revenue(event_id: str, request: Request, _: Any = Depends(require_auth_dependency)) -> Any:
        actor = await current_actor(request)
        return await _with_session(get_db_manager, lambda session: _invoke(media.get_revenue_event, session, actor, event_id))

    @router.get("/revenue-events/{event_id}/cost-records", response_model=RevenueCostRecordHistoryResponse, operation_id="media_list_revenue_cost_records")
    async def list_revenue_costs(event_id: str, request: Request, limit: int = Query(default=100, ge=1, le=100), offset: int = Query(default=0, ge=0), _: Any = Depends(require_auth_dependency)) -> Any:
        actor = await current_actor(request)
        return await _with_session(get_db_manager, lambda session: _invoke(media.list_revenue_cost_records, session, actor, event_id, limit=limit, offset=offset))

    @router.post("/revenue-events/{event_id}/cost-records", response_model=RevenueCostRecordResponse, operation_id="media_create_revenue_cost_record")
    async def create_revenue_cost(event_id: str, payload: RevenueCostRecordCreateRequest, request: Request, idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)], _: Any = Depends(require_auth_dependency)) -> Any:
        actor = await current_actor(request)
        return await _with_session(get_db_manager, lambda session: _invoke(media.create_revenue_cost_record, session, actor, event_id, **_dump(payload), idempotency_key=idempotency_key))

    @router.get("/cost-entries", response_model=list[MediaCostEntryResponse], operation_id="media_list_cost_entries")
    async def list_cost_entries(request: Request, persona_ref: str = Query(min_length=1, max_length=164), project_id: str | None = Query(default=None), limit: int = Query(default=100, ge=1, le=100), offset: int = Query(default=0, ge=0), _: Any = Depends(require_auth_dependency)) -> Any:
        actor = await current_actor(request)
        return await _with_session(get_db_manager, lambda session: _invoke(media.list_cost_entries, session, actor, persona_ref=persona_ref, project_id=project_id, limit=limit, offset=offset))

    @router.post("/cost-entries", response_model=MediaCostEntryResponse, operation_id="media_create_cost_entry")
    async def create_cost_entry(payload: MediaCostEntryCreateRequest, request: Request, idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)], _: Any = Depends(require_auth_dependency)) -> Any:
        actor = await current_actor(request)
        return await _with_session(get_db_manager, lambda session: _invoke(media.create_cost_entry, session, actor, **_dump(payload), idempotency_key=idempotency_key))

    @router.get("/cost-entries/{entry_id}/history", response_model=MediaCostEntryHistoryResponse, operation_id="media_get_cost_entry_history")
    async def cost_entry_history(entry_id: str, request: Request, limit: int = Query(default=100, ge=1, le=100), offset: int = Query(default=0, ge=0), _: Any = Depends(require_auth_dependency)) -> Any:
        actor = await current_actor(request)
        return await _with_session(get_db_manager, lambda session: _invoke(media.get_cost_entry_history, session, actor, entry_id, limit=limit, offset=offset))

    @router.post("/revenue-events/{event_id}/cost-entries/{cost_entry_id}/allocate", response_model=RevenueCostRecordResponse, operation_id="media_allocate_cost_entry")
    async def allocate_cost_entry(event_id: str, cost_entry_id: str, payload: MediaCostEntryAllocateRequest, request: Request, idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)], _: Any = Depends(require_auth_dependency)) -> Any:
        actor = await current_actor(request)
        return await _with_session(get_db_manager, lambda session: _invoke(media.allocate_cost_entry, session, actor, event_id, cost_entry_id, **_dump(payload), idempotency_key=idempotency_key))

    @router.get("/learning-proposals", response_model=list[ResponseObject], operation_id="media_list_learning_proposals")
    async def list_learning(request: Request, project_id: str | None = Query(default=None), limit: int = Query(default=100, ge=1, le=100), offset: int = Query(default=0, ge=0), _: Any = Depends(require_auth_dependency)) -> Any:
        actor = await current_actor(request)
        return await _with_session(get_db_manager, lambda session: _invoke(media.list_learning_proposals, session, actor, project_id=project_id, limit=limit, offset=offset))

    @router.post("/learning-proposals", response_model=ResponseObject, operation_id="media_propose_learning")
    async def propose_learning(payload: LearningProposalCreateRequest, request: Request, idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)], _: Any = Depends(require_auth_dependency)) -> Any:
        actor = await current_actor(request)
        return await _with_session(get_db_manager, lambda session: _invoke(media.create_learning_proposal, session, actor, **_dump(payload), idempotency_key=idempotency_key))

    @router.get("/learning-proposals/{proposal_id}", response_model=ResponseObject, operation_id="media_get_learning_proposal")
    async def get_learning(proposal_id: str, request: Request, _: Any = Depends(require_auth_dependency)) -> Any:
        actor = await current_actor(request)
        return await _with_session(get_db_manager, lambda session: _invoke(media.get_learning_proposal, session, actor, proposal_id))

    return router


__all__ = [
    "EvidenceRefRequest",
    "VariantGroupRequest",
    "MetricSnapshotInputRequest",
    "MetricSnapshotCreateRequest",
    "ExperimentCreateRequest",
    "ExperimentResultCreateRequest",
    "ExperimentAnalysisRequest",
    "RevenueEventCreateRequest",
    "RevenueCostRecordCreateRequest",
    "RevenueCostRecordResponse",
    "RevenueCostRecordHistoryResponse",
    "MediaCostEntryCreateRequest",
    "MediaCostEntryResponse",
    "MediaCostEntryHistoryResponse",
    "MediaCostEntryAllocateRequest",
    "PatreonRevenueImportRequest",
    "LearningProposalCreateRequest",
    "create_media_operations_metrics_router",
]
