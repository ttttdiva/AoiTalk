"""Strict HTTP boundary for MediaOps platform content variants.

The content-variant API is deliberately a typed boundary.  Platform payloads
are discriminated by ``type`` and are never accepted as an arbitrary JSON
blob.  The service owns ACL, immutable revision, QA and rights invariants;
these routes only authenticate, validate and forward opaque references.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Callable, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..memory.models import MediaPlatform
from ..services import media_operations_content_service as service
from .media_operations_routes import (
    _actor,
    _invoke,
    _principal_projection,
    _with_session,
)


# UUIDs are used by the existing MediaOps rows, while external systems use
# opaque prefixed identifiers.  This intentionally excludes URLs, paths,
# whitespace and credential-like values from ordinary DTOs.
_REF_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,254}$"
_SHA256_PATTERN = r"^[0-9a-fA-F]{64}$"

OpaqueRef = Annotated[
    str,
    Field(min_length=1, max_length=255, pattern=_REF_PATTERN),
]
Sha256 = Annotated[
    str,
    Field(min_length=64, max_length=64, pattern=_SHA256_PATTERN),
]


def _safe_public_url(value: str) -> str:
    """Reject credentials and sensitive query parameters from public URLs."""

    text = str(value or "").strip()
    parsed = urlsplit(text)
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("URL must be a credential-free public HTTP(S) URL") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or any(ch.isspace() for ch in text)
    ):
        raise ValueError("URL must be a credential-free public HTTP(S) URL")
    sensitive = {"token", "access_token", "refresh_token", "password", "passwd", "secret", "api_key", "apikey", "cookie", "credential", "authorization", "auth"}
    if any(part.split("=", 1)[0].strip().lower() in sensitive for part in parsed.query.replace(";", "&").split("&") if part):
        raise ValueError("URL must not contain sensitive query parameters")
    return text


class _ContentCommandModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class _ContentResponseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class UrlEvidenceRequest(_ContentCommandModel):
    type: Literal["url"]
    url: str = Field(min_length=1, max_length=4000)
    label: str | None = Field(default=None, max_length=255)
    note: str | None = Field(default=None, max_length=2000)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return _safe_public_url(value)


class ArtifactEvidenceRequest(_ContentCommandModel):
    type: Literal["artifact"]
    sha256: Sha256
    mime_type: str = Field(min_length=1, max_length=255)
    label: str | None = Field(default=None, max_length=255)
    note: str | None = Field(default=None, max_length=2000)


EvidenceRequest = Annotated[
    UrlEvidenceRequest | ArtifactEvidenceRequest,
    Field(discriminator="type"),
]


class UrlEvidenceResponse(_ContentResponseModel):
    type: Literal["url"]
    url: str
    label: str | None = None
    note: str | None = None
    ordinal: int | None = Field(default=None, ge=1)
    evidence_hash: str | None = None


class ArtifactEvidenceResponse(_ContentResponseModel):
    type: Literal["artifact"]
    sha256: str
    mime_type: str
    label: str | None = None
    note: str | None = None
    ordinal: int | None = Field(default=None, ge=1)
    evidence_hash: str | None = None


EvidenceResponse = Annotated[
    UrlEvidenceResponse | ArtifactEvidenceResponse,
    Field(discriminator="type"),
]


# ---------------------------------------------------------------------------
# Typed platform payloads


class XThreadPost(_ContentCommandModel):
    text: str = Field(min_length=1, max_length=12_000)
    media: list[OpaqueRef] = Field(default_factory=list, max_length=4)
    alt_text: str | None = Field(default=None, max_length=2_000)


class XPostContentPayload(_ContentCommandModel):
    type: Literal["x_post"]
    text: str = Field(min_length=1, max_length=12_000)
    media: list[OpaqueRef] = Field(default_factory=list, max_length=4)
    alt_text: str | None = Field(default=None, max_length=2_000)
    links: list[str] = Field(default_factory=list, max_length=10)
    hashtags: list[str] = Field(default_factory=list, max_length=50)
    sensitive_content: bool = False
    reply_to: OpaqueRef | None = None
    scheduled_at: str | None = Field(default=None, max_length=64)

    @field_validator("links")
    @classmethod
    def validate_links(cls, values: list[str]) -> list[str]:
        return [_safe_public_url(value) for value in values]


class XThreadContentPayload(_ContentCommandModel):
    type: Literal["x_thread"]
    posts: list[XThreadPost] = Field(min_length=1, max_length=20)
    scheduled_at: str | None = Field(default=None, max_length=64)
    reply_to: OpaqueRef | None = None


class PixivContentPayload(_ContentCommandModel):
    type: Literal["pixiv_work"]
    title: str = Field(min_length=1, max_length=255)
    caption: str = Field(default="", max_length=25_000)
    tags: list[str] = Field(default_factory=list, max_length=50)
    media: list[OpaqueRef] = Field(min_length=1, max_length=100)
    ai_generated: bool | None = None
    rating: str | None = Field(default=None, max_length=32)
    r18: bool | None = None
    r18g: bool | None = None
    series_id: OpaqueRef | None = None


class DlsiteSalesConfiguration(_ContentCommandModel):
    currency: str = Field(min_length=3, max_length=8, pattern=r"^[A-Za-z]{3,8}$")
    tax_included: bool
    distribution: str = Field(min_length=1, max_length=80)


class DlsiteRightsChecklistItem(_ContentCommandModel):
    code: str = Field(min_length=1, max_length=120)
    status: Literal["passed", "failed", "not_run", "review_required"]
    note: str | None = Field(default=None, max_length=2_000)


class DlsiteContentPayload(_ContentCommandModel):
    type: Literal["dlsite_release"]
    title: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1, max_length=25_000)
    category: str = Field(min_length=1, max_length=120)
    age_rating: str = Field(min_length=1, max_length=32)
    price: float = Field(ge=0, le=10_000_000)
    sales: DlsiteSalesConfiguration
    preview_assets: list[OpaqueRef] = Field(min_length=1, max_length=20)
    deliverable_package_ref: OpaqueRef
    rights_checklist: list[DlsiteRightsChecklistItem] = Field(min_length=1, max_length=50)
    thumbnail_assets: list[OpaqueRef] = Field(default_factory=list, max_length=20)


class PatreonContentPayload(_ContentCommandModel):
    type: Literal["patreon_post"]
    audience: Literal["public", "paid", "tier"]
    title: str = Field(min_length=1, max_length=255)
    body: str = Field(min_length=1, max_length=25_000)
    public_preview: str | None = Field(default=None, max_length=10_000)
    attachments: list[OpaqueRef] = Field(default_factory=list, max_length=20)
    tier_refs: list[str] = Field(default_factory=list, max_length=20)
    scheduled_at: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def validate_tier_audience(self) -> "PatreonContentPayload":
        if self.audience == "tier" and not self.tier_refs:
            raise ValueError("tier audience requires tier_refs")
        return self


class YouTubeContentPayload(_ContentCommandModel):
    type: Literal["youtube_video", "youtube_short"]
    title: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=25_000)
    tags: list[str] = Field(default_factory=list, max_length=50)
    media_asset: list[OpaqueRef] = Field(min_length=1, max_length=1)
    visibility: str = Field(min_length=1, max_length=32)
    thumbnail: list[OpaqueRef] = Field(default_factory=list, max_length=1)
    captions: list[str] = Field(default_factory=list, max_length=20)
    scheduled_at: str | None = Field(default=None, max_length=64)
    audience: str | None = Field(default=None, max_length=64)
    disclosure: str | None = Field(default=None, max_length=64)


class InstagramContentPayload(_ContentCommandModel):
    type: Literal["instagram_feed", "instagram_carousel", "instagram_reel"]
    caption: str = Field(default="", max_length=2_200)
    media: list[OpaqueRef] = Field(min_length=1, max_length=10)
    alt_text: str | None = Field(default=None, max_length=1_000)
    scheduled_at: str | None = Field(default=None, max_length=64)
    cover: list[OpaqueRef] = Field(default_factory=list, max_length=1)

    @model_validator(mode="after")
    def validate_media_count(self) -> "InstagramContentPayload":
        if self.type == "instagram_carousel" and len(self.media) < 2:
            raise ValueError("instagram_carousel requires at least two media references")
        if self.type != "instagram_carousel" and len(self.media) != 1:
            raise ValueError("instagram_feed and instagram_reel require one media reference")
        if self.type != "instagram_reel" and self.cover:
            raise ValueError("cover is only valid for instagram_reel")
        return self


PlatformPayload = Annotated[
    XPostContentPayload
    | XThreadContentPayload
    | PixivContentPayload
    | DlsiteContentPayload
    | PatreonContentPayload
    | YouTubeContentPayload
    | InstagramContentPayload,
    Field(discriminator="type"),
]

_PAYLOAD_PLATFORM = {
    "x_post": "x",
    "x_thread": "x",
    "pixiv_work": "pixiv",
    "dlsite_release": "dlsite",
    "patreon_post": "patreon",
    "youtube_video": "youtube",
    "youtube_short": "youtube",
    "instagram_feed": "instagram",
    "instagram_carousel": "instagram",
    "instagram_reel": "instagram",
}

# Backwards-compatible semantic names for callers that do not need to
# distinguish a single post from a thread at import time.
XContentPayload = XPostContentPayload


# ---------------------------------------------------------------------------
# Command/response models


class ContentVariantCreateFields(_ContentCommandModel):
    platform: MediaPlatform
    persona_revision_id: OpaqueRef
    platform_account_id: OpaqueRef | None = None
    platform_account_revision_id: OpaqueRef | None = None
    payload: PlatformPayload
    generation_output_refs: list["GenerationOutputRef"] = Field(default_factory=list, max_length=50)
    source_evidence: list[EvidenceRequest] = Field(default_factory=list, max_length=50)
    expected_content_item_hash: Sha256 | None = None

    @model_validator(mode="after")
    def validate_payload_platform(self) -> "ContentVariantCreateFields":
        if _PAYLOAD_PLATFORM.get(self.payload.type) != self.platform.value:
            raise ValueError("payload platform does not match variant platform")
        return self


class ContentVariantCreateRequest(ContentVariantCreateFields):
    content_item_id: OpaqueRef


class ContentVariantRevisionCreateRequest(_ContentCommandModel):
    persona_revision_id: OpaqueRef
    platform_account_id: OpaqueRef | None = None
    platform_account_revision_id: OpaqueRef | None = None
    payload: PlatformPayload
    generation_output_refs: list[GenerationOutputRef] = Field(default_factory=list, max_length=50)
    source_evidence: list[EvidenceRequest] = Field(default_factory=list, max_length=50)
    expected_content_item_hash: Sha256 | None = None
    expected_version: int = Field(ge=1)


class ContentVariantSummaryResponse(_ContentResponseModel):
    id: str
    owner_user_id: str
    project_id: str | None = None
    content_item_id: str
    content_item_hash: str | None = None
    platform: MediaPlatform
    create_hash: str | None = None
    idempotency_key: str | None = None
    persona_revision_id: str | None = None
    persona_revision_hash: str | None = None
    platform_account_id: str | None = None
    platform_account_revision_id: str | None = None
    platform_account_revision_hash: str | None = None
    status: Literal["draft", "ready", "blocked", "publishable"] = "draft"
    current_revision: ContentVariantRevisionResponse | None = None
    readiness: ContentVariantReadinessResponse | None = None
    created_by: str | None = None
    created_at: str
    updated_at: str | None = None


class ContentVariantRevisionResponse(_ContentResponseModel):
    id: str
    content_variant_id: str
    variant_id: str | None = None
    owner_user_id: str
    project_id: str | None = None
    version: int = Field(ge=1)
    content_item_id: str
    content_item_hash: str
    platform: MediaPlatform
    persona_revision_id: str
    persona_revision_hash: str | None = None
    platform_account_id: str | None = None
    platform_account_revision_id: str | None = None
    platform_account_revision_hash: str | None = None
    payload: PlatformPayload
    generation_output_refs: list[GenerationOutputRefResponse] = Field(default_factory=list, max_length=50)
    source_evidence: list[EvidenceResponse] = Field(default_factory=list, max_length=50)
    content_hash: str
    idempotency_key: str | None = None
    created_by: str | None = None
    created_at: str


class ContentVariantDetailResponse(ContentVariantSummaryResponse):
    revisions: list[ContentVariantRevisionResponse] = Field(default_factory=list, max_length=100)
    revision_history_truncated: bool = False


ContentVariantListResponse = list[ContentVariantDetailResponse]
ContentVariantRevisionListResponse = list[ContentVariantRevisionResponse]


class GenerationOutputRefRequest(_ContentCommandModel):
    generation_output_id: OpaqueRef
    sha256: Sha256 | None = None


GenerationOutputRef = GenerationOutputRefRequest | OpaqueRef


class GenerationOutputRefResponse(_ContentResponseModel):
    ordinal: int = Field(ge=1)
    generation_output_id: str
    sha256: str | None = None


class AssessmentCheckRequest(_ContentCommandModel):
    code: str = Field(min_length=1, max_length=120)
    status: Literal["passed", "failed", "not_run"]
    mandatory: bool = False
    message: str | None = Field(default=None, max_length=2_000)


class AssessmentFindingRequest(_ContentCommandModel):
    code: str = Field(min_length=1, max_length=120)
    severity: Literal["info", "warning", "error"]
    message: str = Field(min_length=1, max_length=2_000)


class QaResultCreateRequest(_ContentCommandModel):
    result: Literal["passed", "failed", "review_required"]
    checks: list[AssessmentCheckRequest] = Field(default_factory=list, max_length=100)
    findings: list[AssessmentFindingRequest] = Field(default_factory=list, max_length=100)
    evidence: list[EvidenceRequest] = Field(default_factory=list, max_length=50)
    policy_revision_id: OpaqueRef | None = None
    policy_revision_hash: Sha256 | None = None

    @model_validator(mode="after")
    def block_unproven_pass(self) -> "QaResultCreateRequest":
        if self.result == "passed" and (
            not self.checks
            or any(check.status != "passed" for check in self.checks)
        ):
            raise ValueError("a passing QA result requires all checks to pass")
        return self


class QaResultResponse(_ContentResponseModel):
    id: str
    content_variant_id: str | None = None
    content_variant_revision_id: str
    variant_revision_id: str | None = None
    owner_user_id: str | None = None
    project_id: str | None = None
    revision_hash: str | None = None
    result: Literal["passed", "failed", "review_required"]
    checks: list[AssessmentCheckRequest] = Field(default_factory=list, max_length=100)
    findings: list[AssessmentFindingRequest] = Field(default_factory=list, max_length=100)
    evidence: list[EvidenceResponse] = Field(default_factory=list, max_length=50)
    policy_revision_id: str | None = None
    policy_revision_hash: str | None = None
    assessment_hash: str | None = None
    content_hash: str | None = None
    idempotency_key: str | None = None
    created_by: str | None = None
    created_at: str


class RightsCheckRequest(_ContentCommandModel):
    code: str = Field(min_length=1, max_length=120)
    status: Literal["passed", "failed", "not_run"]
    mandatory: bool = False
    message: str | None = Field(default=None, max_length=2_000)


class RightsRecordCreateRequest(_ContentCommandModel):
    result: Literal["cleared", "blocked", "review_required"]
    checks: list[RightsCheckRequest] = Field(default_factory=list, max_length=100)
    findings: list[AssessmentFindingRequest] = Field(default_factory=list, max_length=100)
    evidence: list[EvidenceRequest] = Field(default_factory=list, max_length=50)
    policy_revision_id: OpaqueRef | None = None
    policy_revision_hash: Sha256 | None = None
    note: str | None = Field(default=None, max_length=2_000)

    @model_validator(mode="after")
    def block_unproven_clearance(self) -> "RightsRecordCreateRequest":
        if self.result == "cleared" and (
            not self.checks
            or any(check.status != "passed" for check in self.checks)
        ):
            raise ValueError("cleared rights require all checks to pass")
        return self


class RightsRecordResponse(_ContentResponseModel):
    id: str
    content_variant_id: str | None = None
    content_variant_revision_id: str
    variant_revision_id: str | None = None
    owner_user_id: str | None = None
    project_id: str | None = None
    revision_hash: str | None = None
    result: Literal["cleared", "blocked", "review_required"]
    checks: list[AssessmentCheckRequest] = Field(default_factory=list, max_length=100)
    findings: list[AssessmentFindingRequest] = Field(default_factory=list, max_length=100)
    evidence: list[EvidenceResponse] = Field(default_factory=list, max_length=50)
    policy_revision_id: str | None = None
    policy_revision_hash: str | None = None
    note: str | None = None
    assessment_hash: str | None = None
    content_hash: str | None = None
    idempotency_key: str | None = None
    created_by: str | None = None
    created_at: str


QaResultListResponse = list[QaResultResponse]
RightsRecordListResponse = list[RightsRecordResponse]


class ContentVariantReadinessResponse(_ContentResponseModel):
    content_variant_id: str
    variant_id: str | None = None
    revision_id: str | None = None
    revision_hash: str | None = None
    ready: bool
    publication_allowed: bool = False
    status: Literal["draft", "ready", "blocked", "publishable"] = "blocked"
    qa_result: Literal["passed", "failed", "review_required"] | None = None
    rights_result: Literal["cleared", "blocked", "review_required"] | None = None
    blockers: list[str] = Field(default_factory=list, max_length=100)
    blocking_reasons: list[str] = Field(default_factory=list, max_length=100)
    qa: AssessmentProjection | None = None
    rights: AssessmentProjection | None = None


class AssessmentProjection(_ContentResponseModel):
    id: str
    content_variant_id: str
    content_variant_revision_id: str
    owner_user_id: str
    project_id: str | None = None
    revision_hash: str
    policy_revision_id: str | None = None
    policy_revision_hash: str
    result: str
    checks: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    findings: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    evidence: list[EvidenceResponse] = Field(default_factory=list, max_length=50)
    assessment_hash: str
    idempotency_key: str | None = None
    created_by: str | None = None
    created_at: str


ContentVariantSummaryResponse.model_rebuild()
ContentVariantDetailResponse.model_rebuild()
ContentVariantRevisionResponse.model_rebuild()
ContentVariantReadinessResponse.model_rebuild()


def _method(target: Any, *names: str) -> Callable[..., Any]:
    """Resolve a service method while allowing compatible service aliases."""

    for name in names:
        candidate = getattr(target, name, None)
        if callable(candidate):
            return candidate
    raise RuntimeError("MediaOps content service operation is unavailable")


_SERVICE_ALIASES: dict[str, tuple[str, ...]] = {
    "list_content_variants": ("list_content_variants", "list_variants"),
    "create_variant": ("create_variant", "create_content_variant"),
    "get_variant": ("get_variant", "get_content_variant"),
    "append_variant_revision": (
        "append_variant_revision",
        "append_content_variant_revision",
    ),
    "list_variant_revisions": (
        "list_variant_revisions",
        "list_content_variant_revisions",
    ),
    "get_variant_revision": (
        "get_variant_revision",
        "get_revision",
        "get_content_variant_revision",
    ),
    "record_qa": ("record_qa", "record_qa_assessment"),
    "record_rights": ("record_rights", "record_rights_assessment"),
    "get_readiness": ("get_readiness", "get_variant_readiness"),
}


def _payload_data(payload: ContentVariantCreateFields) -> dict[str, Any]:
    data = payload.model_dump(mode="json")
    def strip_nulls(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: strip_nulls(item)
                for key, item in value.items()
                if item is not None or key == "note"
            }
        if isinstance(value, list):
            return [strip_nulls(item) for item in value]
        return value

    # The service's typed normalizer distinguishes an omitted optional field
    # from JSON null (for example, X alt text or YouTube captions).
    if isinstance(data.get("payload"), dict):
        data["payload"] = strip_nulls(data["payload"])
        if (
            data["payload"].get("type") != "instagram_reel"
            and "cover" in data["payload"]
        ):
            data["payload"].pop("cover", None)
    # The service receives the discriminated payload as a plain JSON mapping;
    # no opaque/provider fields are reconstructed at this boundary.
    return data


def create_media_operations_content_router(
    get_db_manager: Callable[[], Any],
    get_user_from_request: Callable[..., Any],
    require_auth_dependency: Callable[..., Any],
) -> APIRouter:
    router = APIRouter(prefix="/api/operations/media", tags=["operations-media"])
    content = service.MediaOperationsContentService()

    async def current_actor(request: Request) -> dict[str, Any]:
        user = await _actor(get_user_from_request, request)
        # ``_principal_projection`` stamps browser-session human authority
        # server-side while keeping bearer/internal/agent callers fail-closed.
        return _principal_projection(user)

    async def call(name: str, *args: Any, **kwargs: Any) -> Any:
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                _method(content, *_SERVICE_ALIASES.get(name, (name,))),
                session,
                *args,
                **kwargs,
            ),
        )

    @router.get(
        "/content-variants",
        response_model=ContentVariantListResponse,
        operation_id="media_list_content_variants",
    )
    async def list_content_variants(
        request: Request,
        content_item_id: str | None = Query(default=None),
        project_id: str | None = Query(default=None),
        platform: MediaPlatform | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        return await call(
            "list_content_variants",
            actor,
            content_item_id=content_item_id,
            project_id=project_id,
            platform=platform.value if platform else None,
            limit=limit,
            offset=offset,
        )

    @router.post(
        "/content-variants",
        response_model=ContentVariantDetailResponse,
        operation_id="media_create_content_variant",
    )
    async def create_content_variant(
        payload: ContentVariantCreateRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        data = _payload_data(payload)
        content_item_id = data.pop("content_item_id")
        return await call(
            "create_variant",
            actor,
            content_item_id=content_item_id,
            idempotency_key=idempotency_key,
            **data,
        )

    @router.post(
        "/content-items/{content_item_id}/variants",
        response_model=ContentVariantDetailResponse,
        operation_id="media_create_content_item_variant",
    )
    async def create_content_item_variant(
        content_item_id: str,
        payload: ContentVariantCreateFields,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        data = _payload_data(payload)
        return await call(
            "create_variant",
            actor,
            content_item_id=content_item_id,
            idempotency_key=idempotency_key,
            **data,
        )

    @router.get(
        "/content-variants/{variant_id}",
        response_model=ContentVariantDetailResponse,
        operation_id="media_get_content_variant",
    )
    async def get_content_variant(
        variant_id: str,
        request: Request,
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        return await call("get_variant", actor, variant_id)

    @router.post(
        "/content-variants/{variant_id}/revisions",
        response_model=ContentVariantRevisionResponse,
        operation_id="media_append_content_variant_revision",
    )
    async def append_content_variant_revision(
        variant_id: str,
        payload: ContentVariantRevisionCreateRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        data = _payload_data(payload)
        expected_version = data.pop("expected_version")
        # The stable variant determines platform and content-item snapshot;
        # forwarding a second platform value would invite divergent records.
        data.pop("platform", None)
        return await call(
            "append_variant_revision",
            actor,
            variant_id,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            **data,
        )

    @router.get(
        "/content-variants/{variant_id}/revisions",
        response_model=ContentVariantRevisionListResponse,
        operation_id="media_list_content_variant_revisions",
    )
    async def list_content_variant_revisions(
        variant_id: str,
        request: Request,
        limit: int = Query(default=100, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        detail = await call("get_variant", actor, variant_id)
        revisions = detail.get("revisions", []) if isinstance(detail, dict) else []
        return revisions[offset : offset + limit]

    @router.get(
        "/content-variant-revisions/{revision_id}",
        response_model=ContentVariantRevisionResponse,
        operation_id="media_get_content_variant_revision",
    )
    async def get_content_variant_revision(
        revision_id: str,
        request: Request,
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        return await call("get_variant_revision", actor, revision_id)

    @router.post(
        "/content-variant-revisions/{revision_id}/qa",
        response_model=QaResultResponse,
        operation_id="media_record_content_variant_qa",
    )
    async def record_content_variant_qa(
        revision_id: str,
        payload: QaResultCreateRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        if (
            payload.result == "passed"
            and str(actor.get("actor_type", "human")).lower()
            in {"agent", "assistant", "automation", "system", "bot"}
        ):
            raise HTTPException(status_code=403, detail="only an authorized human may record a passed QA assessment")
        return await call(
            "record_qa",
            actor,
            content_variant_revision_id=revision_id,
            idempotency_key=idempotency_key,
            **payload.model_dump(mode="json"),
        )

    @router.get(
        "/content-variant-revisions/{revision_id}/qa",
        response_model=QaResultListResponse,
        operation_id="media_list_content_variant_qa",
    )
    async def list_content_variant_qa(
        revision_id: str,
        request: Request,
        limit: int = Query(default=100, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        return await call(
            "list_qa_assessments",
            actor,
            content_variant_revision_id=revision_id,
            limit=limit,
            offset=offset,
        )

    @router.post(
        "/content-variant-revisions/{revision_id}/rights",
        response_model=RightsRecordResponse,
        operation_id="media_record_content_variant_rights",
    )
    async def record_content_variant_rights(
        revision_id: str,
        payload: RightsRecordCreateRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        if (
            payload.result == "cleared"
            and str(actor.get("actor_type", "human")).lower()
            in {"agent", "assistant", "automation", "system", "bot"}
        ):
            raise HTTPException(status_code=403, detail="only an authorized human may record cleared rights")
        data = payload.model_dump(mode="json")
        # The append-only assessment ledger has no free-form note column;
        # findings carry any auditable explanation.
        data.pop("note", None)
        return await call(
            "record_rights",
            actor,
            content_variant_revision_id=revision_id,
            idempotency_key=idempotency_key,
            **data,
        )

    @router.get(
        "/content-variant-revisions/{revision_id}/rights",
        response_model=RightsRecordListResponse,
        operation_id="media_list_content_variant_rights",
    )
    async def list_content_variant_rights(
        revision_id: str,
        request: Request,
        limit: int = Query(default=100, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        return await call(
            "list_rights_assessments",
            actor,
            content_variant_revision_id=revision_id,
            limit=limit,
            offset=offset,
        )

    @router.get(
        "/content-variants/{variant_id}/readiness",
        response_model=ContentVariantReadinessResponse,
        operation_id="media_get_content_variant_readiness",
    )
    async def get_content_variant_readiness(
        variant_id: str,
        request: Request,
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        return await call("get_readiness", actor, variant_id)

    return router


__all__ = [
    "ArtifactEvidenceRequest",
    "ArtifactEvidenceResponse",
    "AssessmentCheckRequest",
    "AssessmentFindingRequest",
    "ContentVariantCreateFields",
    "ContentVariantCreateRequest",
    "ContentVariantDetailResponse",
    "ContentVariantListResponse",
    "ContentVariantReadinessResponse",
    "ContentVariantRevisionCreateRequest",
    "ContentVariantRevisionListResponse",
    "ContentVariantRevisionResponse",
    "ContentVariantSummaryResponse",
    "DlsiteContentPayload",
    "EvidenceRequest",
    "EvidenceResponse",
    "GenerationOutputRef",
    "GenerationOutputRefRequest",
    "GenerationOutputRefResponse",
    "InstagramContentPayload",
    "PatreonContentPayload",
    "PixivContentPayload",
    "PlatformPayload",
    "QaResultCreateRequest",
    "QaResultListResponse",
    "QaResultResponse",
    "RightsRecordCreateRequest",
    "RightsRecordListResponse",
    "RightsRecordResponse",
    "XContentPayload",
    "XPostContentPayload",
    "XThreadContentPayload",
    "YouTubeContentPayload",
    "create_media_operations_content_router",
]
