"""Read-only Calendar and Results routes for the MediaOps workspace."""

from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from ..services.media_operations_overview_service import MediaOperationsOverviewService
from .media_operations_routes import (
    _actor,
    _invoke,
    _principal_projection,
    PersonaDetailResponse,
    _with_session,
)


class _OverviewModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class CalendarReference(_OverviewModel):
    type: str
    id: str


class MediaCalendarEvent(_OverviewModel):
    id: str
    source: str
    kind: str
    title: str
    starts_at: str
    ends_at: str | None = None
    status: str
    platform: str | None = None
    persona_label: str | None = None
    character: str | None = None
    target_account: dict[str, Any] | None = None
    content_summary: str | None = None
    media_readiness: dict[str, Any] = Field(default_factory=dict)
    qa_status: str | None = None
    rights_status: str | None = None
    approval_status: str | None = None
    execution_status: str | None = None
    receipt_status: str | None = None
    receipt: dict[str, Any] | None = None
    requires_human_action: bool = False
    reference: CalendarReference


class MediaCalendarResponse(_OverviewModel):
    start: str
    end: str
    items: list[MediaCalendarEvent] = Field(default_factory=list, max_length=100)
    total: int = Field(ge=0)
    has_more: bool = False


class MetricPlatformSummary(_OverviewModel):
    platform: str
    snapshot_count: int = Field(ge=1)
    metrics: dict[str, int | float] = Field(default_factory=dict)
    last_observed_at: str | None = None


class MetricPersonaSummary(_OverviewModel):
    persona_ref: str
    persona_label: str | None = None
    snapshot_count: int = Field(ge=1)
    metrics: dict[str, int | float] = Field(default_factory=dict)


class MetricAccountSummary(_OverviewModel):
    account_ref: str
    account_label: str | None = None
    snapshot_count: int = Field(ge=1)
    metrics: dict[str, int | float] = Field(default_factory=dict)


class MetricContentSummary(_OverviewModel):
    content_ref: str
    content_label: str | None = None
    snapshot_count: int = Field(ge=1)
    metrics: dict[str, int | float] = Field(default_factory=dict)


class MetricSummary(_OverviewModel):
    count: int = Field(ge=0)
    by_platform: list[MetricPlatformSummary] = Field(default_factory=list)
    by_persona: list[MetricPersonaSummary] = Field(default_factory=list)
    by_account: list[MetricAccountSummary] = Field(default_factory=list)
    by_content: list[MetricContentSummary] = Field(default_factory=list)


class RevenueCurrencySummary(_OverviewModel):
    currency: str
    event_count: int = Field(ge=1)
    gross: float
    net: float


class RevenuePlatformSummary(_OverviewModel):
    platform: str
    event_count: int = Field(ge=1)
    gross: float
    net: float


class RevenueAccountSummary(_OverviewModel):
    account_ref: str
    event_count: int = Field(ge=1)
    gross: float
    net: float


class RevenueContentSummary(_OverviewModel):
    content_ref: str
    event_count: int = Field(ge=1)
    gross: float
    net: float


class RevenueProductSummary(_OverviewModel):
    product_ref: str
    event_count: int = Field(ge=1)
    gross: float
    net: float


class RevenueSummary(_OverviewModel):
    event_count: int = Field(ge=0)
    by_currency: list[RevenueCurrencySummary] = Field(default_factory=list)
    by_platform: list[RevenuePlatformSummary] = Field(default_factory=list)
    by_account: list[RevenueAccountSummary] = Field(default_factory=list)
    by_content: list[RevenueContentSummary] = Field(default_factory=list)
    by_product: list[RevenueProductSummary] = Field(default_factory=list)


class ExperimentSummary(_OverviewModel):
    count: int = Field(ge=0)
    by_status: dict[str, int] = Field(default_factory=dict)
    result_count: int = Field(ge=0)


class LearningSummary(_OverviewModel):
    count: int = Field(ge=0)
    by_status: dict[str, int] = Field(default_factory=dict)
    pending_review_count: int = Field(ge=0)


class MediaResultsResponse(_OverviewModel):
    start: str
    end: str
    metric_snapshots: MetricSummary
    revenue: RevenueSummary
    experiments: ExperimentSummary
    learning: LearningSummary
    evidence_count: int = Field(ge=0)


class CharacterDashboardAccount(_OverviewModel):
    id: str
    platform: str
    account_ref: str | None = None
    status: str
    connection_status: str | None = None
    capability_status: str | None = None
    capabilities: dict[str, str | None] = Field(default_factory=dict)
    observed_capabilities: dict[str, str | None] = Field(default_factory=dict)
    adapter_ready: bool = False
    account_revision_id: str | None = None
    account_revision_version: int | None = None


class CharacterDashboardResearchCandidate(_OverviewModel):
    id: str
    title: str
    summary: str
    status: str
    review_state: str
    reason: str | None = None
    candidate_hash: str
    decision_version: int = Field(ge=0)
    content_item_id: str | None = None
    latest_decision: dict[str, Any] | None = None
    discovered_at: str | None = None
    expires_at: str | None = None


class CharacterDashboardRecipe(_OverviewModel):
    id: str
    name: str
    created_at: str | None = None


class CharacterDashboardGenerationRun(_OverviewModel):
    id: str
    status: str
    started_at: str | None = None
    finished_at: str | None = None


class CharacterDashboardGeneration(_OverviewModel):
    recipes: list[CharacterDashboardRecipe] = Field(default_factory=list)
    runs: list[CharacterDashboardGenerationRun] = Field(default_factory=list)
    plans: list[dict[str, Any]] = Field(default_factory=list)
    recipes_page: Optional["CharacterDashboardPage"] = None
    plans_page: Optional["CharacterDashboardPage"] = None
    runs_page: Optional["CharacterDashboardPage"] = None


class CharacterDashboardCalendarItem(_OverviewModel):
    id: str
    kind: str
    title: str
    starts_at: str | None = None
    status: str
    platform: str | None = None
    character: str | None = None
    target_account: dict[str, Any] | None = None
    content_summary: str | None = None
    media_readiness: dict[str, Any] = Field(default_factory=dict)
    qa_status: str | None = None
    rights_status: str | None = None
    approval_status: str | None = None
    execution_status: str | None = None
    receipt_status: str | None = None
    receipt: dict[str, Any] | None = None


class CharacterDashboardResults(_OverviewModel):
    snapshot_count: int = Field(ge=0)
    metrics: dict[str, int | float] = Field(default_factory=dict)
    last_observed_at: str | None = None


class CharacterDashboardLearningItem(_OverviewModel):
    id: str
    title: str
    proposal_type: str
    status: str
    created_at: str | None = None


class CharacterDashboardLearning(_OverviewModel):
    count: int = Field(ge=0)
    pending_review_count: int = Field(ge=0)
    items: list[CharacterDashboardLearningItem] = Field(default_factory=list)
    page: Optional["CharacterDashboardPage"] = None


class CharacterDashboardPage(_OverviewModel):
    """Independent child collection page.

    The page envelope is closed and rejects unknown fields.  Items remain
    open JSON values because the projection combines several append-only
    ledgers while preserving the legacy response shape.
    """

    items: list[Any] = Field(default_factory=list)
    count: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)
    has_more: bool = False


class CharacterDashboardPagination(_OverviewModel):
    accounts: CharacterDashboardPage
    research: CharacterDashboardPage
    content: CharacterDashboardPage
    variants: CharacterDashboardPage
    qa: CharacterDashboardPage
    rights: CharacterDashboardPage
    recipes: CharacterDashboardPage
    plans: CharacterDashboardPage
    runs: CharacterDashboardPage
    publications: CharacterDashboardPage
    metrics: CharacterDashboardPage
    revenue: CharacterDashboardPage
    experiments: CharacterDashboardPage
    learning: CharacterDashboardPage
    calendar: CharacterDashboardPage


class CharacterDashboardResponse(_OverviewModel):
    character: PersonaDetailResponse
    connected_accounts: list[CharacterDashboardAccount] = Field(default_factory=list)
    research_candidates: list[CharacterDashboardResearchCandidate] = Field(default_factory=list)
    generation: CharacterDashboardGeneration
    calendar: list[CharacterDashboardCalendarItem] = Field(default_factory=list)
    results: CharacterDashboardResults
    learning: CharacterDashboardLearning
    content: CharacterDashboardPage = Field(
        default_factory=lambda: CharacterDashboardPage(count=0, limit=100, offset=0)
    )
    variants: CharacterDashboardPage = Field(
        default_factory=lambda: CharacterDashboardPage(count=0, limit=100, offset=0)
    )
    qa: CharacterDashboardPage = Field(
        default_factory=lambda: CharacterDashboardPage(count=0, limit=100, offset=0)
    )
    rights: CharacterDashboardPage = Field(
        default_factory=lambda: CharacterDashboardPage(count=0, limit=100, offset=0)
    )
    publications: CharacterDashboardPage = Field(
        default_factory=lambda: CharacterDashboardPage(count=0, limit=100, offset=0)
    )
    metrics: CharacterDashboardPage = Field(
        default_factory=lambda: CharacterDashboardPage(count=0, limit=100, offset=0)
    )
    revenue: CharacterDashboardPage = Field(
        default_factory=lambda: CharacterDashboardPage(count=0, limit=100, offset=0)
    )
    experiments: CharacterDashboardPage = Field(
        default_factory=lambda: CharacterDashboardPage(count=0, limit=100, offset=0)
    )
    revenue_summary: RevenueSummary | None = None
    accounts_page: CharacterDashboardPage = Field(
        default_factory=lambda: CharacterDashboardPage(count=0, limit=100, offset=0)
    )
    research_page: CharacterDashboardPage = Field(
        default_factory=lambda: CharacterDashboardPage(count=0, limit=100, offset=0)
    )
    content_page: CharacterDashboardPage = Field(
        default_factory=lambda: CharacterDashboardPage(count=0, limit=100, offset=0)
    )
    variants_page: CharacterDashboardPage = Field(
        default_factory=lambda: CharacterDashboardPage(count=0, limit=100, offset=0)
    )
    qa_page: CharacterDashboardPage = Field(
        default_factory=lambda: CharacterDashboardPage(count=0, limit=100, offset=0)
    )
    rights_page: CharacterDashboardPage = Field(
        default_factory=lambda: CharacterDashboardPage(count=0, limit=100, offset=0)
    )
    recipes_page: CharacterDashboardPage = Field(
        default_factory=lambda: CharacterDashboardPage(count=0, limit=100, offset=0)
    )
    plans_page: CharacterDashboardPage = Field(
        default_factory=lambda: CharacterDashboardPage(count=0, limit=100, offset=0)
    )
    runs_page: CharacterDashboardPage = Field(
        default_factory=lambda: CharacterDashboardPage(count=0, limit=100, offset=0)
    )
    publications_page: CharacterDashboardPage = Field(
        default_factory=lambda: CharacterDashboardPage(count=0, limit=100, offset=0)
    )
    metrics_page: CharacterDashboardPage = Field(
        default_factory=lambda: CharacterDashboardPage(count=0, limit=100, offset=0)
    )
    revenue_page: CharacterDashboardPage = Field(
        default_factory=lambda: CharacterDashboardPage(count=0, limit=100, offset=0)
    )
    experiments_page: CharacterDashboardPage = Field(
        default_factory=lambda: CharacterDashboardPage(count=0, limit=100, offset=0)
    )
    learning_page: CharacterDashboardPage = Field(
        default_factory=lambda: CharacterDashboardPage(count=0, limit=100, offset=0)
    )
    calendar_page: CharacterDashboardPage = Field(
        default_factory=lambda: CharacterDashboardPage(count=0, limit=100, offset=0)
    )
    pagination: CharacterDashboardPagination | None = None


def create_media_operations_overview_router(
    get_db_manager: Callable[[], Any],
    get_user_from_request: Callable[..., Any],
    require_auth_dependency: Callable[..., Any],
) -> APIRouter:
    router = APIRouter(prefix="/api/operations/media", tags=["operations-media"])
    overview = MediaOperationsOverviewService()

    async def current_actor(request: Request) -> dict[str, Any]:
        user = await _actor(get_user_from_request, request)
        return _principal_projection(user)

    @router.get(
        "/calendar",
        response_model=MediaCalendarResponse,
        operation_id="media_get_calendar",
    )
    async def get_calendar(
        request: Request,
        project_id: str | None = Query(default=None),
        start: str | None = Query(default=None, max_length=64),
        end: str | None = Query(default=None, max_length=64),
        limit: int = Query(default=100, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                overview.get_calendar,
                session,
                actor,
                project_id=project_id,
                start=start,
                end=end,
                limit=limit,
                offset=offset,
            ),
        )

    @router.get(
        "/results",
        response_model=MediaResultsResponse,
        operation_id="media_get_results",
    )
    async def get_results(
        request: Request,
        project_id: str | None = Query(default=None),
        start: str | None = Query(default=None, max_length=64),
        end: str | None = Query(default=None, max_length=64),
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                overview.get_results,
                session,
                actor,
                project_id=project_id,
                start=start,
                end=end,
            ),
        )

    @router.get(
        "/characters/{character_id}/dashboard",
        response_model=CharacterDashboardResponse,
        operation_id="media_get_character_dashboard",
    )
    async def get_character_dashboard(
        character_id: str,
        request: Request,
        limit: int = Query(default=100, ge=1, le=100),
        accounts_offset: int = Query(default=0, ge=0),
        research_offset: int = Query(default=0, ge=0),
        content_offset: int = Query(default=0, ge=0),
        variants_offset: int = Query(default=0, ge=0),
        recipes_offset: int = Query(default=0, ge=0),
        plans_offset: int = Query(default=0, ge=0),
        runs_offset: int = Query(default=0, ge=0),
        qa_offset: int = Query(default=0, ge=0),
        rights_offset: int = Query(default=0, ge=0),
        publications_offset: int = Query(default=0, ge=0),
        metrics_offset: int = Query(default=0, ge=0),
        revenue_offset: int = Query(default=0, ge=0),
        experiments_offset: int = Query(default=0, ge=0),
        learning_offset: int = Query(default=0, ge=0),
        calendar_offset: int = Query(default=0, ge=0),
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                overview.get_character_dashboard,
                session,
                actor,
                character_id,
                limit=limit,
                accounts_offset=accounts_offset,
                research_offset=research_offset,
                content_offset=content_offset,
                variants_offset=variants_offset,
                recipes_offset=recipes_offset,
                plans_offset=plans_offset,
                runs_offset=runs_offset,
                qa_offset=qa_offset,
                rights_offset=rights_offset,
                publications_offset=publications_offset,
                metrics_offset=metrics_offset,
                revenue_offset=revenue_offset,
                experiments_offset=experiments_offset,
                learning_offset=learning_offset,
                calendar_offset=calendar_offset,
            ),
        )

    return router


__all__ = [
    "CalendarReference",
    "MetricAccountSummary",
    "MetricContentSummary",
    "RevenueAccountSummary",
    "RevenueContentSummary",
    "RevenuePlatformSummary",
    "RevenueProductSummary",
    "MediaCalendarEvent",
    "MediaCalendarResponse",
    "MediaResultsResponse",
    "CharacterDashboardPage",
    "CharacterDashboardPagination",
    "CharacterDashboardResponse",
    "create_media_operations_overview_router",
]
