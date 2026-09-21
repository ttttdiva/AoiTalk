"""Authenticated HTTP boundary for typed Media Operations."""

from __future__ import annotations

from collections.abc import Mapping
from inspect import isawaitable
from typing import Annotated, Any, Callable, Literal
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.exc import IntegrityError

from ..memory.models import MediaPlatform
from ..services import media_operations_service as service


ContentPillar = Annotated[
    str,
    Field(min_length=1, max_length=200),
]


class _MediaCommandModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )


class _MediaResponseModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )


class PersonaRevisionCreateRequest(_MediaCommandModel):
    display_name: str = Field(
        min_length=1,
        max_length=120,
    )
    summary: str | None = Field(
        default=None,
        max_length=4000,
    )
    voice: str | None = Field(
        default=None,
        max_length=4000,
    )
    audience: str | None = Field(
        default=None,
        max_length=4000,
    )
    platforms: list[MediaPlatform] = Field(
        default_factory=list,
        max_length=6,
    )
    content_pillars: list[ContentPillar] = Field(
        default_factory=list,
        max_length=20,
    )
    public_aliases: list[str] = Field(default_factory=list, max_length=20)
    niche: str | None = Field(default=None, max_length=1000)
    positioning: str | None = Field(default=None, max_length=2000)
    visual_identity: dict[str, Any] = Field(default_factory=dict)
    creative_direction: str | None = Field(default=None, max_length=4000)
    allowed_subjects: list[str] = Field(default_factory=list, max_length=40)
    prohibited_subjects: list[str] = Field(default_factory=list, max_length=40)
    adult_policy: str | None = Field(default=None, max_length=32)
    sensitive_policy: str | None = Field(default=None, max_length=32)
    ip_policy: str | None = Field(default=None, max_length=32)
    disclosure_policy: str | None = Field(default=None, max_length=32)
    monetization_policy: dict[str, Any] = Field(default_factory=dict)
    kpi_objectives: list[str] = Field(default_factory=list, max_length=20)
    default_language: str | None = Field(default=None, max_length=16)
    locale: str | None = Field(default=None, max_length=64)
    timezone: str | None = Field(default=None, max_length=64)
    research_policy: dict[str, Any] = Field(default_factory=dict)
    image_production_policy: dict[str, Any] = Field(default_factory=dict)
    video_production_policy: dict[str, Any] = Field(default_factory=dict)


class PersonaCreateRequest(
    PersonaRevisionCreateRequest
):
    intake_slot: int = Field(ge=1, le=9)
    project_id: str | None = None
    state: Literal["draft", "active", "paused", "archived"] = "draft"
    parent_brand_ref: str | None = Field(default=None, max_length=164)


class CharacterCreateRequest(PersonaRevisionCreateRequest):
    """Slot-free Character creation payload.

    Characters use the same immutable PersonaRevision fields as the legacy
    Persona API, but deliberately have no ``intake_slot`` field.
    """

    project_id: str | None = None
    state: Literal["draft", "active", "paused", "archived"] = "draft"
    parent_brand_ref: str | None = Field(default=None, max_length=164)


class MediaCharacterPatchRequest(_MediaCommandModel):
    """Optimistic partial Character revision update."""

    expected_revision_id: UUID
    expected_revision_version: int = Field(ge=1)
    expected_revision_content_hash: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )

    # Defaults are intentionally ``None`` while ``exclude_unset`` at the
    # route boundary preserves whether a caller omitted a field or explicitly
    # requested a clear operation.
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    summary: str | None = Field(default=None, max_length=4000)
    voice: str | None = Field(default=None, max_length=4000)
    audience: str | None = Field(default=None, max_length=4000)
    platforms: list[MediaPlatform] | None = Field(default=None, max_length=6)
    content_pillars: list[ContentPillar] | None = Field(default=None, max_length=20)
    public_aliases: list[str] | None = Field(default=None, max_length=20)
    niche: str | None = Field(default=None, max_length=1000)
    positioning: str | None = Field(default=None, max_length=2000)
    visual_identity: dict[str, Any] | None = Field(default=None)
    creative_direction: str | None = Field(default=None, max_length=4000)
    allowed_subjects: list[str] | None = Field(default=None, max_length=40)
    prohibited_subjects: list[str] | None = Field(default=None, max_length=40)
    adult_policy: str | None = Field(default=None, max_length=32)
    sensitive_policy: str | None = Field(default=None, max_length=32)
    ip_policy: str | None = Field(default=None, max_length=32)
    disclosure_policy: str | None = Field(default=None, max_length=32)
    monetization_policy: dict[str, Any] | None = Field(default=None)
    kpi_objectives: list[str] | None = Field(default=None, max_length=20)
    default_language: str | None = Field(default=None, max_length=16)
    locale: str | None = Field(default=None, max_length=64)
    timezone: str | None = Field(default=None, max_length=64)
    research_policy: dict[str, Any] | None = Field(default=None)
    image_production_policy: dict[str, Any] | None = Field(default=None)
    video_production_policy: dict[str, Any] | None = Field(default=None)

    @model_validator(mode="after")
    def _require_revision_change(self) -> "MediaCharacterPatchRequest":
        expected = {
            "expected_revision_id",
            "expected_revision_version",
            "expected_revision_content_hash",
        }
        if not (self.model_fields_set - expected):
            raise ValueError(
                "at least one character revision field is required"
            )
        return self


class CharacterUpdateRequest(MediaCharacterPatchRequest):
    """Legacy PUT payload using the same optimistic PATCH gate."""


class UrlPersonaResourceProvenance(
    _MediaCommandModel
):
    type: Literal["url"]
    url: str = Field(
        min_length=1,
        max_length=4000,
    )


class ArtifactPersonaResourceProvenance(
    _MediaCommandModel
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


class StoredArtifactPersonaResourceProvenance(
    _MediaCommandModel
):
    type: Literal["stored_artifact"]
    artifact_id: UUID


PersonaResourceProvenance = Annotated[
    UrlPersonaResourceProvenance
    | ArtifactPersonaResourceProvenance
    | StoredArtifactPersonaResourceProvenance,
    Field(discriminator="type"),
]


class PersonaResourceCreateRequest(
    _MediaCommandModel
):
    resource_kind: Literal[
        "profile",
        "reference",
        "asset",
        "persona_bible",
        "character_bible",
        "world_bible",
        "visual_style_reference",
        "reference_image",
        "posting_rule",
        "platform_rule",
        "sensitive_rule",
        "forbidden_content_rule",
        "ip_rights_rule",
        "monetization_rule",
        "kpi_definition",
        "experiment_policy",
        "topic_source",
        "idea_bank",
        "high_performing_content",
        "supporting_document",
    ]
    platform: MediaPlatform | None = None
    label: str | None = Field(
        default=None,
        max_length=255,
    )
    provenance: PersonaResourceProvenance


class PersonaRevisionResponse(
    _MediaResponseModel
):
    id: str
    persona_id: str
    owner_user_id: str
    project_id: str | None
    version: int = Field(ge=1)
    display_name: str
    summary: str | None
    voice: str | None
    audience: str | None
    niche: str | None
    positioning: str | None
    visual_identity: dict[str, Any]
    creative_direction: str | None
    allowed_subjects: list[str]
    prohibited_subjects: list[str]
    adult_policy: str | None
    sensitive_policy: str | None
    ip_policy: str | None
    disclosure_policy: str | None
    monetization_policy: dict[str, Any]
    kpi_objectives: list[str]
    default_language: str | None
    locale: str | None
    timezone: str | None
    research_policy: dict[str, Any]
    image_production_policy: dict[str, Any]
    video_production_policy: dict[str, Any]
    public_aliases: list[str]
    platforms: list[MediaPlatform] = Field(
        max_length=6
    )
    content_pillars: list[str] = Field(
        max_length=20
    )
    content_hash: str
    created_by: str | None
    created_at: str


class PersonaSummaryResponse(
    _MediaResponseModel
):
    id: str
    owner_user_id: str
    project_id: str | None
    state: Literal["draft", "active", "paused", "archived"]
    parent_brand_ref: str | None
    create_hash: str
    created_by: str | None
    created_at: str
    current_revision: PersonaRevisionResponse


class PersonaDetailResponse(
    PersonaSummaryResponse
):
    revisions: list[
        PersonaRevisionResponse
    ] = Field(max_length=100)
    revision_history_truncated: bool


class PersonaIntakeSlotResponse(
    _MediaResponseModel
):
    slot: int = Field(ge=1, le=9)
    persona_id: str | None
    persona: PersonaSummaryResponse | None


class PersonaIntakeResponse(
    _MediaResponseModel
):
    project_id: str | None
    slots: list[
        PersonaIntakeSlotResponse
    ] = Field(min_length=9, max_length=9)


class UrlPersonaResourceProvenanceResponse(
    _MediaResponseModel
):
    type: Literal["url"]
    url: str


class ArtifactPersonaResourceProvenanceResponse(
    _MediaResponseModel
):
    type: Literal["artifact"]
    sha256: str
    mime_type: str


PersonaResourceProvenanceResponse = Annotated[
    UrlPersonaResourceProvenanceResponse
    | ArtifactPersonaResourceProvenanceResponse,
    Field(discriminator="type"),
]


class PersonaResourceResponse(
    _MediaResponseModel
):
    id: str
    persona_id: str
    owner_user_id: str
    project_id: str | None
    resource_kind: Literal[
        "profile",
        "reference",
        "asset",
        "persona_bible",
        "character_bible",
        "world_bible",
        "visual_style_reference",
        "reference_image",
        "posting_rule",
        "platform_rule",
        "sensitive_rule",
        "forbidden_content_rule",
        "ip_rights_rule",
        "monetization_rule",
        "kpi_definition",
        "experiment_policy",
        "topic_source",
        "idea_bank",
        "high_performing_content",
        "supporting_document",
    ]
    platform: MediaPlatform | None
    label: str | None
    provenance: PersonaResourceProvenanceResponse
    resource_hash: str
    created_by: str | None
    created_at: str


PersonaListResponse = list[
    PersonaSummaryResponse
]
PersonaResourceListResponse = list[
    PersonaResourceResponse
]


async def _maybe_await(value: Any) -> Any:
    return (
        await value
        if isawaitable(value)
        else value
    )


async def _get_session(
    get_db_manager: Callable[[], Any],
) -> Any:
    manager = (
        get_db_manager()
        if callable(get_db_manager)
        else get_db_manager
    )
    manager = await _maybe_await(manager)
    if (
        manager is None
        or not callable(
            getattr(
                manager,
                "get_session",
                None,
            )
        )
    ):
        raise HTTPException(
            status_code=503,
            detail="database is unavailable",
        )
    try:
        session = await _maybe_await(
            manager.get_session()
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="database is unavailable",
        ) from exc
    if session is None:
        raise HTTPException(
            status_code=503,
            detail="database is unavailable",
        )
    return session


async def _close_session(session: Any) -> None:
    close = getattr(session, "close", None)
    if callable(close):
        try:
            await _maybe_await(close())
        except Exception:
            return


async def _actor(
    get_user_from_request: Callable[..., Any],
    request: Request,
) -> Any:
    try:
        user = await _maybe_await(
            get_user_from_request(request)
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=401,
            detail="Not authenticated",
        ) from exc
    if not user:
        raise HTTPException(
            status_code=401,
            detail="Not authenticated",
        )
    return user


def _raise_http_error(exc: Exception) -> None:
    if isinstance(exc, HTTPException):
        raise exc
    if isinstance(
        exc,
        service.MediaOperationsError,
    ):
        raise HTTPException(
            status_code=int(
                getattr(
                    exc,
                    "status_code",
                    400,
                )
                or 400
            ),
            detail=str(exc),
        ) from exc
    if isinstance(exc, IntegrityError):
        raise HTTPException(
            status_code=409,
            detail=(
                "media operation conflicts "
                "with an existing record"
            ),
        ) from exc
    if isinstance(exc, PermissionError):
        raise HTTPException(
            status_code=403,
            detail=(
                str(exc)
                or "media operation access denied"
            ),
        ) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(
            status_code=422,
            detail=str(exc),
        ) from exc
    raise exc


async def _invoke(
    method: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    try:
        return await _maybe_await(
            method(*args, **kwargs)
        )
    except Exception as exc:
        _raise_http_error(exc)
        raise AssertionError("unreachable")


async def _with_session(
    get_db_manager: Callable[[], Any],
    callback: Callable[[Any], Any],
) -> Any:
    session = await _get_session(
        get_db_manager
    )
    try:
        return await _maybe_await(
            callback(session)
        )
    finally:
        await _close_session(session)


def _principal_projection(
    principal: Any,
) -> dict[str, Any]:
    def field(
        name: str,
        default: Any = None,
    ) -> Any:
        if isinstance(principal, Mapping):
            return principal.get(
                name,
                default,
            )
        return getattr(
            principal,
            name,
            default,
        )

    # Authentication resolves the principal, but MediaOps also needs a
    # server-owned authority classification for human-only review actions.
    # A normal browser session is the only implicit human authority; bearer
    # and internal callers remain fail-closed unless they carry an explicit
    # non-human marker.  Never let a client-provided ``is_agent`` value be
    # hidden by an ``actor_type=human`` marker.
    raw_actor_type = field("actor_type", None)
    is_agent = bool(field("is_agent", False))
    authority_source = field("_authority_source", None)
    if is_agent:
        actor_type: Any = "agent"
    elif authority_source == "web_session":
        # A normal browser session is the only implicit human authority.  Do
        # not accept an actor_type supplied by a client/test principal as a
        # substitute for this server-owned authentication provenance.
        actor_type = "human"
    elif authority_source in {"bearer", "internal"}:
        # Non-browser callers stay non-human by default.  An internal,
        # server-stamped classification may be retained, but arbitrary
        # ``actor_type=human`` markers never cross this boundary.
        normalized = str(raw_actor_type or "unknown").strip().lower()
        actor_type = normalized if normalized in {"agent", "system", "unknown"} else "unknown"
    else:
        # Older resolver adapters returned the already-authenticated actor
        # classification without the newer ``_authority_source`` stamp.  The
        # value is still server-owned (the callback above resolves the user;
        # request fields are never read here), so preserve only the explicit
        # human/admin classifications for backwards compatibility.  Requests
        # carrying a bearer/internal source take the fail-closed branch above.
        normalized = str(raw_actor_type or "unknown").strip().lower()
        actor_type = normalized if normalized in {"human", "admin", "agent", "system", "unknown"} else "unknown"

    return {
        "id": field("id"),
        "user_id": field("user_id"),
        "role": field("role", ""),
        "actor_type": actor_type,
        "is_agent": is_agent,
    }


def create_media_operations_router(
    get_db_manager: Callable[[], Any],
    get_user_from_request: Callable[..., Any],
    require_auth_dependency: Callable[..., Any],
) -> APIRouter:
    router = APIRouter(
        prefix="/api/operations/media",
        tags=["operations-media"],
    )
    media = service.MediaOperationsService()

    async def current_actor(
        request: Request,
    ) -> dict[str, Any]:
        user = await _actor(
            get_user_from_request,
            request,
        )
        return _principal_projection(user)

    @router.get(
        "/characters",
        response_model=PersonaListResponse,
        operation_id="media_list_characters",
    )
    async def list_characters(
        request: Request,
        project_id: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        search: str | None = Query(default=None, max_length=200),
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                media.list_characters,
                session,
                actor,
                project_id=project_id,
                limit=limit,
                offset=offset,
                search=search,
            ),
        )

    @router.post(
        "/characters",
        response_model=PersonaDetailResponse,
        operation_id="media_create_character",
    )
    async def create_character(
        payload: CharacterCreateRequest,
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
                media.create_character,
                session,
                actor,
                **data,
                idempotency_key=idempotency_key,
            ),
        )

    @router.get(
        "/characters/{character_id}",
        response_model=PersonaDetailResponse,
        operation_id="media_get_character",
    )
    async def get_character(
        character_id: str,
        request: Request,
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                media.get_character,
                session,
                actor,
                character_id,
            ),
        )

    @router.patch(
        "/characters/{character_id}",
        response_model=PersonaDetailResponse,
        operation_id="media_patch_character",
    )
    async def patch_character(
        character_id: str,
        payload: MediaCharacterPatchRequest,
        request: Request,
        idempotency_key: Annotated[
            str,
            Header(alias="Idempotency-Key", min_length=1, max_length=255),
        ],
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        values = payload.model_dump(mode="json", exclude_unset=True)
        expected_revision_id = values.pop("expected_revision_id")
        expected_revision_version = values.pop("expected_revision_version")
        expected_revision_content_hash = values.pop(
            "expected_revision_content_hash"
        )
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                media.patch_character,
                session,
                actor,
                character_id,
                expected_revision_id=expected_revision_id,
                expected_revision_version=expected_revision_version,
                expected_revision_content_hash=expected_revision_content_hash,
                idempotency_key=idempotency_key,
                **values,
            ),
        )

    @router.put(
        "/characters/{character_id}",
        response_model=PersonaDetailResponse,
        operation_id="media_update_character",
    )
    async def update_character(
        character_id: str,
        payload: CharacterUpdateRequest,
        request: Request,
        idempotency_key: Annotated[
            str,
            Header(alias="Idempotency-Key", min_length=1, max_length=255),
        ],
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        values = payload.model_dump(mode="json", exclude_unset=True)
        expected_revision_id = values.pop("expected_revision_id")
        expected_revision_version = values.pop("expected_revision_version")
        expected_revision_content_hash = values.pop(
            "expected_revision_content_hash"
        )
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                media.patch_character,
                session,
                actor,
                character_id,
                expected_revision_id=expected_revision_id,
                expected_revision_version=expected_revision_version,
                expected_revision_content_hash=expected_revision_content_hash,
                idempotency_key=idempotency_key,
                **values,
            ),
        )

    @router.get(
        "/persona-intake",
        response_model=PersonaIntakeResponse,
        operation_id="media_get_persona_intake",
    )
    async def get_persona_intake(
        request: Request,
        project_id: str | None = Query(
            default=None
        ),
        _: Any = Depends(
            require_auth_dependency
        ),
    ) -> Any:
        actor = await current_actor(request)
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                media.get_persona_intake,
                session,
                actor,
                project_id=project_id,
            ),
        )

    @router.get(
        "/personas",
        response_model=PersonaListResponse,
        operation_id="media_list_personas",
    )
    async def list_personas(
        request: Request,
        project_id: str | None = Query(
            default=None
        ),
        limit: int = Query(
            default=100,
            ge=1,
            le=100,
        ),
        offset: int = Query(
            default=0,
            ge=0,
        ),
        _: Any = Depends(
            require_auth_dependency
        ),
    ) -> Any:
        actor = await current_actor(request)
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                media.list_personas,
                session,
                actor,
                project_id=project_id,
                limit=limit,
                offset=offset,
            ),
        )

    @router.post(
        "/personas",
        response_model=PersonaDetailResponse,
        operation_id="media_create_persona",
    )
    async def create_persona(
        payload: PersonaCreateRequest,
        request: Request,
        idempotency_key: Annotated[
            str,
            Header(
                alias="Idempotency-Key",
                min_length=1,
                max_length=255,
            ),
        ],
        _: Any = Depends(
            require_auth_dependency
        ),
    ) -> Any:
        actor = await current_actor(request)
        data = payload.model_dump(
            mode="json"
        )
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                media.create_persona,
                session,
                actor,
                **data,
                idempotency_key=(
                    idempotency_key
                ),
            ),
        )

    @router.get(
        "/personas/{persona_id}",
        response_model=PersonaDetailResponse,
        operation_id="media_get_persona",
    )
    async def get_persona(
        persona_id: str,
        request: Request,
        _: Any = Depends(
            require_auth_dependency
        ),
    ) -> Any:
        actor = await current_actor(request)
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                media.get_persona,
                session,
                actor,
                persona_id,
            ),
        )

    @router.post(
        "/personas/{persona_id}/revisions",
        response_model=PersonaRevisionResponse,
        operation_id=(
            "media_append_persona_revision"
        ),
    )
    async def append_persona_revision(
        persona_id: str,
        payload: PersonaRevisionCreateRequest,
        request: Request,
        idempotency_key: Annotated[
            str,
            Header(
                alias="Idempotency-Key",
                min_length=1,
                max_length=255,
            ),
        ],
        _: Any = Depends(
            require_auth_dependency
        ),
    ) -> Any:
        actor = await current_actor(request)
        data = payload.model_dump(
            mode="json"
        )
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                media.append_persona_revision,
                session,
                actor,
                persona_id,
                **data,
                idempotency_key=(
                    idempotency_key
                ),
            ),
        )

    @router.get(
        "/personas/{persona_id}/resources",
        response_model=(
            PersonaResourceListResponse
        ),
        operation_id=(
            "media_list_persona_resources"
        ),
    )
    async def list_persona_resources(
        persona_id: str,
        request: Request,
        limit: int = Query(
            default=100,
            ge=1,
            le=100,
        ),
        offset: int = Query(
            default=0,
            ge=0,
        ),
        _: Any = Depends(
            require_auth_dependency
        ),
    ) -> Any:
        actor = await current_actor(request)
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                media.list_persona_resources,
                session,
                actor,
                persona_id,
                limit=limit,
                offset=offset,
            ),
        )

    @router.post(
        "/personas/{persona_id}/resources",
        response_model=PersonaResourceResponse,
        operation_id=(
            "media_attach_persona_resource"
        ),
    )
    async def attach_persona_resource(
        persona_id: str,
        payload: PersonaResourceCreateRequest,
        request: Request,
        idempotency_key: Annotated[
            str,
            Header(
                alias="Idempotency-Key",
                min_length=1,
                max_length=255,
            ),
        ],
        _: Any = Depends(
            require_auth_dependency
        ),
    ) -> Any:
        actor = await current_actor(request)
        data = payload.model_dump(
            mode="json"
        )
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                media.attach_persona_resource,
                session,
                actor,
                persona_id,
                **data,
                idempotency_key=(
                    idempotency_key
                ),
            ),
        )

    return router
