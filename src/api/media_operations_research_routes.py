"""Strict HTTP boundary for MediaOps WS3 research/editorial records."""

from __future__ import annotations

from typing import Annotated, Any, Callable, Literal

from fastapi import (
    APIRouter,
    Depends,
    Header,
    Query,
    Request,
)
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
)

from ..memory.models import (
    MediaPlatform,
    ResearchFindingKind,
)
from ..services.media_operations_research_service import (
    MediaOperationsResearchService,
)
from .media_operations_routes import (
    _actor,
    _invoke,
    _principal_projection,
    _with_session,
)


Question = Annotated[
    str,
    Field(min_length=1, max_length=500),
]


class _ResearchCommandModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )


class _ResearchResponseModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )


class ResearchRoutineRevisionCreateRequest(
    _ResearchCommandModel
):
    expected_version: int = Field(ge=1)
    name: str = Field(
        min_length=1,
        max_length=255,
    )
    objective: str = Field(
        min_length=1,
        max_length=4000,
    )
    questions: list[Question] = Field(
        min_length=1,
        max_length=20,
    )
    target_platforms: list[MediaPlatform] | None = Field(default=None, max_length=6)
    cadence: Literal["manual", "hourly", "daily", "weekly", "monthly"] | None = None
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    schedule: dict[str, Any] | None = None
    source_types: list[str] | None = Field(default=None, max_length=12)
    search_queries: list[str] | None = Field(default=None, max_length=20)
    domains: list[str] | None = Field(default=None, max_length=50)
    follow_accounts: list[str] | None = Field(default=None, max_length=50)
    follow_tags: list[str] | None = Field(default=None, max_length=50)
    exclusions: list[str] | None = Field(default=None, max_length=50)
    freshness_hours: int | None = Field(default=None, ge=1, le=8760)
    max_candidates: int | None = Field(default=None, ge=1, le=500)
    review_policy: str | None = Field(default=None, min_length=1, max_length=64)


class ResearchRoutineCreateRequest(
    _ResearchCommandModel
):
    project_id: str | None = None
    persona_id: str | None = None
    platform_account_id: str | None = None
    name: str = Field(
        min_length=1,
        max_length=255,
    )
    objective: str = Field(
        min_length=1,
        max_length=4000,
    )
    questions: list[Question] = Field(
        min_length=1,
        max_length=20,
    )
    target_platforms: list[MediaPlatform] | None = Field(default=None, max_length=6)
    cadence: Literal["manual", "hourly", "daily", "weekly", "monthly"] | None = None
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    schedule: dict[str, Any] | None = None
    source_types: list[str] | None = Field(default=None, max_length=12)
    search_queries: list[str] | None = Field(default=None, max_length=20)
    domains: list[str] | None = Field(default=None, max_length=50)
    follow_accounts: list[str] | None = Field(default=None, max_length=50)
    follow_tags: list[str] | None = Field(default=None, max_length=50)
    exclusions: list[str] | None = Field(default=None, max_length=50)
    freshness_hours: int | None = Field(default=None, ge=1, le=8760)
    max_candidates: int | None = Field(default=None, ge=1, le=500)
    review_policy: str | None = Field(default=None, min_length=1, max_length=64)
    state: Literal["draft", "active", "paused", "archived"] = "draft"
    enabled: bool = False


class ResearchRoutineRevisionResponse(
    _ResearchResponseModel
):
    id: str
    research_routine_id: str
    owner_user_id: str
    project_id: str | None
    version: int
    name: str
    objective: str
    questions: list[str]
    target_platforms: list[MediaPlatform]
    cadence: Literal["manual", "hourly", "daily", "weekly", "monthly"]
    timezone: str
    schedule: dict[str, Any]
    source_types: list[str]
    search_queries: list[str]
    domains: list[str]
    follow_accounts: list[str]
    follow_tags: list[str]
    exclusions: list[str]
    freshness_hours: int
    max_candidates: int
    review_policy: str
    content_hash: str
    created_by: str | None
    created_at: str


class ResearchRoutineSummaryResponse(
    _ResearchResponseModel
):
    id: str
    owner_user_id: str
    project_id: str | None
    persona_id: str | None
    state: Literal["draft", "active", "paused", "archived"]
    enabled: bool
    platform_account_id: str | None
    last_due_at: str | None
    next_due_at: str | None
    create_hash: str
    created_by: str | None
    created_at: str
    current_revision: ResearchRoutineRevisionResponse


class ResearchRoutineDetailResponse(
    ResearchRoutineSummaryResponse
):
    revisions: list[
        ResearchRoutineRevisionResponse
    ] = Field(max_length=100)
    revision_history_truncated: bool


ResearchRoutineListResponse = list[
    ResearchRoutineSummaryResponse
]


class ResearchRunCreateRequest(
    _ResearchCommandModel
):
    research_routine_id: str
    routine_version: int = Field(ge=1)
    focus_note: str | None = Field(
        default=None,
        max_length=4000,
    )
    source_refs: list[str] | None = Field(default=None, max_length=100)
    omissions: list[str] | None = Field(default=None, max_length=100)
    status: Literal["queued", "running", "recorded", "partial", "succeeded", "failed"] = "recorded"


class UrlFindingEvidenceRequest(
    _ResearchCommandModel
):
    type: Literal["url"]
    url: str = Field(
        min_length=1,
        max_length=4000,
    )
    label: str | None = Field(
        default=None,
        max_length=255,
    )
    note: str | None = Field(
        default=None,
        max_length=2000,
    )


class ArtifactFindingEvidenceRequest(
    _ResearchCommandModel
):
    type: Literal["artifact"]
    sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-fA-F]{64}$",
    )
    mime_type: str = Field(
        min_length=1,
        max_length=255,
    )
    label: str | None = Field(
        default=None,
        max_length=255,
    )
    note: str | None = Field(
        default=None,
        max_length=2000,
    )


FindingEvidenceRequest = Annotated[
    UrlFindingEvidenceRequest
    | ArtifactFindingEvidenceRequest,
    Field(discriminator="type"),
]


class ResearchFindingCreateRequest(
    _ResearchCommandModel
):
    kind: ResearchFindingKind
    statement: str = Field(
        min_length=1,
        max_length=8000,
    )
    evidence: list[
        FindingEvidenceRequest
    ] = Field(
        min_length=1,
        max_length=20,
    )


class UrlFindingEvidenceResponse(
    _ResearchResponseModel
):
    type: Literal["url"]
    url: str


class ArtifactFindingEvidenceResponse(
    _ResearchResponseModel
):
    type: Literal["artifact"]
    sha256: str
    mime_type: str


FindingEvidenceProvenanceResponse = Annotated[
    UrlFindingEvidenceResponse
    | ArtifactFindingEvidenceResponse,
    Field(discriminator="type"),
]


class ResearchFindingEvidenceResponse(
    _ResearchResponseModel
):
    id: str
    finding_id: str
    owner_user_id: str
    project_id: str | None
    ordinal: int
    label: str | None
    note: str | None
    provenance: FindingEvidenceProvenanceResponse
    evidence_hash: str
    created_at: str


class ResearchFindingResponse(
    _ResearchResponseModel
):
    id: str
    research_run_id: str
    owner_user_id: str
    project_id: str | None
    kind: ResearchFindingKind
    statement: str
    finding_hash: str
    created_by: str | None
    created_at: str
    evidence: list[
        ResearchFindingEvidenceResponse
    ] = Field(
        min_length=1,
        max_length=20,
    )


ResearchFindingListResponse = list[
    ResearchFindingResponse
]


class ResearchRunSummaryResponse(
    _ResearchResponseModel
):
    id: str
    owner_user_id: str
    project_id: str | None
    research_routine_id: str
    research_routine_revision_id: str
    routine_content_hash: str
    focus_note: str | None
    status: Literal["queued", "running", "recorded", "partial", "succeeded", "failed"]
    started_at: str | None
    finished_at: str | None
    source_refs: list[str]
    omissions: list[str]
    run_hash: str
    created_by: str | None
    created_at: str


class ResearchRunDetailResponse(
    ResearchRunSummaryResponse
):
    routine_revision: (
        ResearchRoutineRevisionResponse
    )
    findings: list[
        ResearchFindingResponse
    ]


ResearchRunListResponse = list[
    ResearchRunSummaryResponse
]


class EditorialProgramRevisionCreateRequest(
    _ResearchCommandModel
):
    expected_version: int = Field(ge=1)
    name: str = Field(
        min_length=1,
        max_length=255,
    )
    objective: str = Field(
        min_length=1,
        max_length=4000,
    )
    content_type: str = Field(default="article", min_length=1, max_length=64)
    cadence: Literal["manual", "hourly", "daily", "weekly", "monthly"] = "manual"
    target_platforms: list[MediaPlatform] = Field(default_factory=list, max_length=6)
    target_account_refs: list[str] = Field(default_factory=list, max_length=32)
    content_pillar: str | None = Field(default=None, max_length=200)
    required_resources: list[str] = Field(default_factory=list, max_length=32)
    default_creative_recipe_ref: str | None = Field(default=None, max_length=164)
    default_qa_policy_ref: str | None = Field(default=None, max_length=164)
    experiment_ref: str | None = Field(default=None, max_length=164)
    draft_generation_policy: str = Field(default="human_review", min_length=1, max_length=64)


class EditorialProgramCreateRequest(
    _ResearchCommandModel
):
    persona_id: str
    name: str = Field(
        min_length=1,
        max_length=255,
    )
    objective: str = Field(
        min_length=1,
        max_length=4000,
    )
    content_type: str = Field(default="article", min_length=1, max_length=64)
    cadence: Literal["manual", "hourly", "daily", "weekly", "monthly"] = "manual"
    target_platforms: list[MediaPlatform] = Field(default_factory=list, max_length=6)
    target_account_refs: list[str] = Field(default_factory=list, max_length=32)
    content_pillar: str | None = Field(default=None, max_length=200)
    required_resources: list[str] = Field(default_factory=list, max_length=32)
    default_creative_recipe_ref: str | None = Field(default=None, max_length=164)
    default_qa_policy_ref: str | None = Field(default=None, max_length=164)
    experiment_ref: str | None = Field(default=None, max_length=164)
    draft_generation_policy: str = Field(default="human_review", min_length=1, max_length=64)
    state: Literal["draft", "active", "paused", "archived"] = "draft"
    enabled: bool = False


class EditorialProgramRevisionResponse(
    _ResearchResponseModel
):
    id: str
    editorial_program_id: str
    owner_user_id: str
    project_id: str | None
    version: int
    name: str
    objective: str
    content_type: str
    cadence: Literal["manual", "hourly", "daily", "weekly", "monthly"]
    target_platforms: list[MediaPlatform]
    target_account_refs: list[str]
    content_pillar: str | None
    required_resources: list[str]
    default_creative_recipe_ref: str | None
    default_qa_policy_ref: str | None
    experiment_ref: str | None
    draft_generation_policy: str
    content_hash: str
    created_by: str | None
    created_at: str


class EditorialProgramSummaryResponse(
    _ResearchResponseModel
):
    id: str
    owner_user_id: str
    project_id: str | None
    persona_id: str
    state: Literal["draft", "active", "paused", "archived"]
    enabled: bool
    last_due_at: str | None
    next_due_at: str | None
    create_hash: str
    created_by: str | None
    created_at: str
    current_revision: (
        EditorialProgramRevisionResponse
    )


class EditorialProgramDetailResponse(
    EditorialProgramSummaryResponse
):
    revisions: list[
        EditorialProgramRevisionResponse
    ] = Field(max_length=100)
    revision_history_truncated: bool


EditorialProgramListResponse = list[
    EditorialProgramSummaryResponse
]


class ContentItemCreateRequest(
    _ResearchCommandModel
):
    editorial_program_id: str
    title: str = Field(
        min_length=1,
        max_length=500,
    )
    brief: str = Field(
        min_length=1,
        max_length=8000,
    )
    finding_ids: list[str] = Field(default_factory=list, max_length=20)
    candidate_ids: list[str] = Field(default_factory=list, max_length=20)
    persona_revision_id: str | None = None
    objective: str | None = Field(default=None, max_length=4000)
    content_type: str = Field(default="article", min_length=1, max_length=64)
    content_pillar: str | None = Field(default=None, max_length=200)
    intended_audience: str | None = Field(default=None, max_length=4000)
    source_refs: list[str] = Field(default_factory=list, max_length=20)
    desired_assets: list[str] = Field(default_factory=list, max_length=20)
    monetization_ref: str | None = Field(default=None, max_length=164)
    experiment_ref: str | None = Field(default=None, max_length=164)
    scheduled_at: str | None = None


class ContentItemSummaryResponse(
    _ResearchResponseModel
):
    id: str
    editorial_program_id: str
    owner_user_id: str
    project_id: str | None
    title: str
    brief: str
    version: int
    status: Literal["draft", "ready", "in_progress", "published", "archived"]
    persona_revision_id: str | None
    objective: str | None
    content_type: str
    content_pillar: str | None
    intended_audience: str | None
    source_refs: list[str]
    candidate_refs: list[str]
    desired_assets: list[str]
    monetization_ref: str | None
    experiment_ref: str | None
    scheduled_at: str | None
    content_hash: str
    created_by: str | None
    created_at: str
    source_finding_ids: list[str] = Field(
        min_length=0,
        max_length=20,
    )
    source_candidate_ids: list[str] = Field(default_factory=list, max_length=20)


class ContentItemDetailResponse(
    ContentItemSummaryResponse
):
    source_findings: list[
        ResearchFindingResponse
    ] = Field(default_factory=list, max_length=20)
    source_candidates: list[ResearchCandidateResponse] = Field(default_factory=list, max_length=20)


ContentItemListResponse = list[
    ContentItemSummaryResponse
]


class ResearchCandidateCreateRequest(_ResearchCommandModel):
    research_run_id: str
    candidate_key: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=500)
    summary: str = Field(min_length=1, max_length=8000)
    source_url: str | None = Field(default=None, max_length=4000)
    source_published_at: str | None = None
    relevance_score: float | None = Field(default=None, ge=0, le=1)
    freshness_score: float | None = Field(default=None, ge=0, le=1)
    evidence: list[FindingEvidenceRequest] = Field(min_length=1, max_length=20)
    reason: str | None = Field(default=None, max_length=4000)


class ResearchCandidateResponse(_ResearchResponseModel):
    id: str
    owner_user_id: str
    project_id: str | None
    research_routine_id: str
    research_run_id: str
    routine_revision_id: str
    candidate_key: str
    title: str
    summary: str
    source_url: str | None
    source_published_at: str | None
    discovered_at: str
    expires_at: str | None
    relevance_score: float | None
    freshness_score: float | None
    evidence: list[dict[str, Any]]
    reason: str | None
    status: Literal["discovered", "triaged", "accepted", "rejected", "expired", "promoted"]
    content_item_id: str | None
    candidate_hash: str
    created_by: str | None
    created_at: str
    updated_at: str
    decision_version: int = Field(ge=0, default=0)
    review_state: Literal["pending", "accepted", "rejected", "expired"] = "pending"
    ranking_score: float | None = Field(default=None, ge=0, le=1)
    ranking_factors: dict[str, Any] = Field(default_factory=dict)
    ranking_rationale: str | None = None
    ranking_policy_revision_id: str | None = None
    ranking_policy_content_hash: str | None = None
    # The triage command returns the immutable decision that advanced this
    # projection.  List/detail responses leave it null for compatibility.
    decision: "ResearchCandidateDecisionResponse | None" = None


ResearchCandidateListResponse = list[ResearchCandidateResponse]


class ResearchCandidateDecisionResponse(_ResearchResponseModel):
    """Safe immutable candidate-review ledger projection."""

    id: str
    candidate_id: str
    sequence: int = Field(ge=1)
    event_type: Literal["triage", "accept", "reject", "expire", "promote"]
    from_status: str | None
    to_status: Literal["triaged", "accepted", "rejected", "expired", "promoted"]
    reason: str | None
    candidate_hash: str
    candidate_snapshot_hash: str
    request_hash: str
    actor_id: str | None
    actor_type: str
    content_item_id: str | None
    decision_hash: str
    prev_decision_hash: str | None
    decided_at: str
    # Backwards-compatible aliases retained for existing generated clients.
    prev_event_hash: str | None
    event_hash: str
    created_at: str


class ResearchCandidateDecisionPageResponse(_ResearchResponseModel):
    """Paginated, hash-only decision history for one candidate."""

    candidate_id: str
    current_status: Literal[
        "discovered",
        "triaged",
        "accepted",
        "rejected",
        "expired",
        "promoted",
    ]
    current_decision_version: int = Field(ge=0)
    candidate_hash: str
    items: list[ResearchCandidateDecisionResponse] = Field(
        default_factory=list,
        max_length=100,
    )
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)
    has_more: bool = False


# Kept as a named alias for generated-client compatibility with the original
# list-oriented implementation.  New callers should use the page envelope.
ResearchCandidateDecisionListResponse = ResearchCandidateDecisionPageResponse

# Resolve the forward reference used by the triage response after the
# immutable decision DTO has been declared.
ResearchCandidateResponse.model_rebuild()


class ResearchCandidateTriageRequest(_ResearchCommandModel):
    status: Literal["triaged", "accepted", "rejected"]
    reason: str | None = Field(default=None, max_length=4000)
    # ``accepted`` is retained for explicit human reaffirmation of a legacy
    # accepted projection that predates the decision ledger.
    expected_status: Literal["discovered", "triaged", "accepted"]
    expected_decision_version: int = Field(ge=0)
    expected_candidate_hash: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )


class ResearchCandidatePromoteRequest(_ResearchCommandModel):
    accepted_decision_id: str
    # Optional optimistic aliases retained for clients that pinned a decision
    # by sequence/hash before the accepted-decision ID became canonical.
    expected_decision_version: int | None = Field(default=None, ge=0)
    expected_decision_hash: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-fA-F]{64}$",
    )
    title: str | None = Field(default=None, max_length=500)
    brief: str | None = Field(default=None, max_length=8000)


# ``ContentItemDetailResponse`` is declared before the candidate response so
# the source-candidate field can use a forward reference while keeping the
# public schema grouped with the existing ContentItem types.
ContentItemDetailResponse.model_rebuild()


def create_media_operations_research_router(
    get_db_manager: Callable[[], Any],
    get_user_from_request: Callable[..., Any],
    require_auth_dependency: Callable[..., Any],
) -> APIRouter:
    router = APIRouter(
        prefix="/api/operations/media",
        tags=["operations-media"],
    )
    research = MediaOperationsResearchService()

    async def current_actor(
        request: Request,
    ) -> dict[str, Any]:
        user = await _actor(
            get_user_from_request,
            request,
        )
        return _principal_projection(user)

    @router.get(
        "/research-routines",
        response_model=ResearchRoutineListResponse,
        operation_id="media_list_research_routines",
    )
    async def list_research_routines(
        request: Request,
        project_id: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                research.list_research_routines,
                session,
                actor,
                project_id=project_id,
                limit=limit,
                offset=offset,
            ),
        )

    @router.get(
        "/research-routines/due",
        response_model=ResearchRoutineListResponse,
        operation_id="media_list_due_research_routines",
    )
    async def list_due_research_routines(
        request: Request,
        project_id: str | None = Query(default=None),
        as_of: str | None = Query(default=None, max_length=64),
        limit: int = Query(default=100, ge=1, le=100),
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                research.list_due_research_routines,
                session,
                actor,
                project_id=project_id,
                as_of=as_of,
                limit=limit,
            ),
        )

    @router.post(
        "/research-routines",
        response_model=ResearchRoutineDetailResponse,
        operation_id="media_create_research_routine",
    )
    async def create_research_routine(
        payload: ResearchRoutineCreateRequest,
        request: Request,
        idempotency_key: Annotated[
            str,
            Header(
                alias="Idempotency-Key",
                min_length=1,
                max_length=255,
            ),
        ],
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                research.create_research_routine,
                session,
                actor,
                **payload.model_dump(mode="json"),
                idempotency_key=idempotency_key,
            ),
        )

    @router.get(
        "/research-routines/{routine_id}",
        response_model=ResearchRoutineDetailResponse,
        operation_id="media_get_research_routine",
    )
    async def get_research_routine(
        routine_id: str,
        request: Request,
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                research.get_research_routine,
                session,
                actor,
                routine_id,
            ),
        )

    @router.post(
        "/research-routines/{routine_id}/revisions",
        response_model=ResearchRoutineRevisionResponse,
        operation_id="media_append_research_routine_revision",
    )
    async def append_research_routine_revision(
        routine_id: str,
        payload: ResearchRoutineRevisionCreateRequest,
        request: Request,
        idempotency_key: Annotated[
            str,
            Header(
                alias="Idempotency-Key",
                min_length=1,
                max_length=255,
            ),
        ],
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        data = payload.model_dump(mode="json")
        expected_version = data.pop("expected_version")

        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                research.append_research_routine_revision,
                session,
                actor,
                routine_id,
                expected_version=expected_version,
                **data,
                idempotency_key=idempotency_key,
            ),
        )

    @router.get(
        "/research-runs",
        response_model=ResearchRunListResponse,
        operation_id="media_list_research_runs",
    )
    async def list_research_runs(
        request: Request,
        project_id: str | None = Query(default=None),
        research_routine_id: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                research.list_research_runs,
                session,
                actor,
                project_id=project_id,
                research_routine_id=research_routine_id,
                limit=limit,
                offset=offset,
            ),
        )

    @router.post(
        "/research-runs",
        response_model=ResearchRunDetailResponse,
        operation_id="media_start_research_run",
    )
    async def start_research_run(
        payload: ResearchRunCreateRequest,
        request: Request,
        idempotency_key: Annotated[
            str,
            Header(
                alias="Idempotency-Key",
                min_length=1,
                max_length=255,
            ),
        ],
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                research.start_research_run,
                session,
                actor,
                **payload.model_dump(mode="json"),
                idempotency_key=idempotency_key,
            ),
        )

    @router.get(
        "/research-runs/{run_id}",
        response_model=ResearchRunDetailResponse,
        operation_id="media_get_research_run",
    )
    async def get_research_run(
        run_id: str,
        request: Request,
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                research.get_research_run,
                session,
                actor,
                run_id,
            ),
        )

    @router.get(
        "/research-runs/{run_id}/findings",
        response_model=ResearchFindingListResponse,
        operation_id="media_list_research_findings",
    )
    async def list_research_findings(
        run_id: str,
        request: Request,
        limit: int = Query(default=100, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                research.list_research_findings,
                session,
                actor,
                run_id,
                limit=limit,
                offset=offset,
            ),
        )

    @router.post(
        "/research-runs/{run_id}/findings",
        response_model=ResearchFindingResponse,
        operation_id="media_append_research_finding",
    )
    async def append_research_finding(
        run_id: str,
        payload: ResearchFindingCreateRequest,
        request: Request,
        idempotency_key: Annotated[
            str,
            Header(
                alias="Idempotency-Key",
                min_length=1,
                max_length=255,
            ),
        ],
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                research.append_research_finding,
                session,
                actor,
                run_id,
                **payload.model_dump(mode="json"),
                idempotency_key=idempotency_key,
            ),
        )

    @router.get(
        "/research-candidates",
        response_model=ResearchCandidateListResponse,
        operation_id="media_list_research_candidates",
    )
    async def list_research_candidates(
        request: Request,
        project_id: str | None = Query(default=None),
        research_run_id: str | None = Query(default=None),
        research_routine_id: str | None = Query(default=None),
        status: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                research.list_research_candidates,
                session,
                actor,
                project_id=project_id,
                research_run_id=research_run_id,
                research_routine_id=research_routine_id,
                status=status,
                limit=limit,
                offset=offset,
            ),
        )

    @router.post(
        "/research-candidates",
        response_model=ResearchCandidateResponse,
        operation_id="media_create_research_candidate",
    )
    async def create_research_candidate(
        payload: ResearchCandidateCreateRequest,
        request: Request,
        idempotency_key: Annotated[
            str,
            Header(alias="Idempotency-Key", min_length=1, max_length=255),
        ],
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        data = payload.model_dump(mode="json")
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                research.create_research_candidate,
                session,
                actor,
                **data,
                idempotency_key=idempotency_key,
            ),
        )

    @router.get(
        "/research-candidates/{candidate_id}",
        response_model=ResearchCandidateResponse,
        operation_id="media_get_research_candidate",
    )
    async def get_research_candidate(
        candidate_id: str,
        request: Request,
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                research.get_research_candidate,
                session,
                actor,
                candidate_id,
            ),
        )

    @router.get(
        "/research-candidates/{candidate_id}/decisions",
        response_model=ResearchCandidateDecisionPageResponse,
        operation_id="media_list_research_candidate_decisions",
    )
    async def list_research_candidate_decisions(
        candidate_id: str,
        request: Request,
        limit: int = Query(default=100, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                research.list_research_candidate_decisions,
                session,
                actor,
                candidate_id,
                limit=limit,
                offset=offset,
            ),
        )

    @router.post(
        "/research-candidates/{candidate_id}/triage",
        response_model=ResearchCandidateResponse,
        operation_id="media_triage_research_candidate",
    )
    async def triage_research_candidate(
        candidate_id: str,
        payload: ResearchCandidateTriageRequest,
        request: Request,
        idempotency_key: Annotated[
            str,
            Header(alias="Idempotency-Key", min_length=1, max_length=255),
        ],
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                research.triage_research_candidate,
                session,
                actor,
                candidate_id,
                **payload.model_dump(mode="json"),
                idempotency_key=idempotency_key,
            ),
        )

    @router.post(
        "/research-candidates/expire",
        operation_id="media_expire_research_candidates",
    )
    async def expire_research_candidates(
        request: Request,
        project_id: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=100),
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                research.expire_research_candidates,
                session,
                actor,
                project_id=project_id,
                limit=limit,
            ),
        )

    @router.post(
        "/research-candidates/{candidate_id}/promote",
        response_model=ContentItemDetailResponse,
        operation_id="media_promote_research_candidate",
    )
    async def promote_research_candidate(
        candidate_id: str,
        payload: ResearchCandidatePromoteRequest,
        request: Request,
        idempotency_key: Annotated[
            str,
            Header(alias="Idempotency-Key", min_length=1, max_length=255),
        ],
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                research.promote_research_candidate,
                session,
                actor,
                candidate_id,
                **payload.model_dump(mode="json"),
                idempotency_key=idempotency_key,
            ),
        )

    @router.get(
        "/editorial-programs",
        response_model=EditorialProgramListResponse,
        operation_id="media_list_editorial_programs",
    )
    async def list_editorial_programs(
        request: Request,
        project_id: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                research.list_editorial_programs,
                session,
                actor,
                project_id=project_id,
                limit=limit,
                offset=offset,
            ),
        )

    @router.get(
        "/editorial-programs/due",
        response_model=EditorialProgramListResponse,
        operation_id="media_list_due_editorial_programs",
    )
    async def list_due_editorial_programs(
        request: Request,
        project_id: str | None = Query(default=None),
        as_of: str | None = Query(default=None, max_length=64),
        limit: int = Query(default=100, ge=1, le=100),
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                research.list_due_editorial_programs,
                session,
                actor,
                project_id=project_id,
                as_of=as_of,
                limit=limit,
            ),
        )

    @router.post(
        "/editorial-programs",
        response_model=EditorialProgramDetailResponse,
        operation_id="media_create_editorial_program",
    )
    async def create_editorial_program(
        payload: EditorialProgramCreateRequest,
        request: Request,
        idempotency_key: Annotated[
            str,
            Header(
                alias="Idempotency-Key",
                min_length=1,
                max_length=255,
            ),
        ],
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                research.create_editorial_program,
                session,
                actor,
                **payload.model_dump(mode="json"),
                idempotency_key=idempotency_key,
            ),
        )

    @router.get(
        "/editorial-programs/{program_id}",
        response_model=EditorialProgramDetailResponse,
        operation_id="media_get_editorial_program",
    )
    async def get_editorial_program(
        program_id: str,
        request: Request,
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                research.get_editorial_program,
                session,
                actor,
                program_id,
            ),
        )

    @router.post(
        "/editorial-programs/{program_id}/revisions",
        response_model=EditorialProgramRevisionResponse,
        operation_id="media_append_editorial_program_revision",
    )
    async def append_editorial_program_revision(
        program_id: str,
        payload: EditorialProgramRevisionCreateRequest,
        request: Request,
        idempotency_key: Annotated[
            str,
            Header(
                alias="Idempotency-Key",
                min_length=1,
                max_length=255,
            ),
        ],
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        data = payload.model_dump(mode="json")
        expected_version = data.pop("expected_version")

        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                research.append_editorial_program_revision,
                session,
                actor,
                program_id,
                expected_version=expected_version,
                **data,
                idempotency_key=idempotency_key,
            ),
        )

    @router.get(
        "/content-items",
        response_model=ContentItemListResponse,
        operation_id="media_list_content_items",
    )
    async def list_content_items(
        request: Request,
        project_id: str | None = Query(default=None),
        editorial_program_id: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                research.list_content_items,
                session,
                actor,
                project_id=project_id,
                editorial_program_id=editorial_program_id,
                limit=limit,
                offset=offset,
            ),
        )

    @router.post(
        "/content-items",
        response_model=ContentItemDetailResponse,
        operation_id="media_create_content_item",
    )
    async def create_content_item(
        payload: ContentItemCreateRequest,
        request: Request,
        idempotency_key: Annotated[
            str,
            Header(
                alias="Idempotency-Key",
                min_length=1,
                max_length=255,
            ),
        ],
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                research.create_content_item,
                session,
                actor,
                **payload.model_dump(mode="json"),
                idempotency_key=idempotency_key,
            ),
        )

    @router.get(
        "/content-items/{content_item_id}",
        response_model=ContentItemDetailResponse,
        operation_id="media_get_content_item",
    )
    async def get_content_item(
        content_item_id: str,
        request: Request,
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                research.get_content_item,
                session,
                actor,
                content_item_id,
            ),
        )

    return router
