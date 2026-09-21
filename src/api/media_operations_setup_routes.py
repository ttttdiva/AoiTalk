"""HTTP boundary for MediaOps WS2 setup operations."""

from __future__ import annotations

from typing import (
    Annotated,
    Any,
    Callable,
    Literal,
)

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)
from starlette.formparsers import MultiPartException, MultiPartParser

from ..memory.models import (
    DraftFactState,
    MediaPlatform,
    PlatformCapabilityStatus,
    PlatformCredentialStatus,
)
from ..services.media_credential_package import MAX_CREDENTIAL_PACKAGE_BYTES
from ..services.media_credential_vault_service import MediaCredentialVaultService
from ..services.media_operations_setup_service import (
    MediaOperationsSetupService,
)
from .media_operations_routes import (
    PersonaIntakeResponse,
    _actor,
    _invoke,
    _principal_projection,
    _with_session,
)


ContentPillar = Annotated[
    str,
    Field(
        min_length=1,
        max_length=200,
    ),
]


class _SetupCommandModel(
    BaseModel
):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )


class _SetupResponseModel(
    BaseModel
):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )


class PersonaDraftNameFact(
    _SetupCommandModel
):
    state: DraftFactState
    value: str | None = Field(
        default=None,
        max_length=120,
    )
    evidence: str | None = Field(
        default=None,
        max_length=1000,
    )


class PersonaDraftTextFact(
    _SetupCommandModel
):
    state: DraftFactState
    value: str | None = Field(
        default=None,
        max_length=4000,
    )
    evidence: str | None = Field(
        default=None,
        max_length=1000,
    )


class PersonaDraftPlatformsFact(
    _SetupCommandModel
):
    state: DraftFactState
    value: list[
        MediaPlatform
    ] | None = Field(
        default=None,
        max_length=6,
    )
    evidence: str | None = Field(
        default=None,
        max_length=1000,
    )


class PersonaDraftPillarsFact(
    _SetupCommandModel
):
    state: DraftFactState
    value: list[
        ContentPillar
    ] | None = Field(
        default=None,
        max_length=20,
    )
    evidence: str | None = Field(
        default=None,
        max_length=1000,
    )


class PersonaBulkDraftSlotWire(
    _SetupCommandModel
):
    slot: int = Field(
        ge=1,
        le=9,
    )
    display_name: PersonaDraftNameFact
    summary: PersonaDraftTextFact
    voice: PersonaDraftTextFact
    audience: PersonaDraftTextFact
    platforms: PersonaDraftPlatformsFact
    content_pillars: PersonaDraftPillarsFact


class PersonaBulkDraftImportRequest(
    _SetupCommandModel
):
    project_id: str | None = None
    slots: list[PersonaBulkDraftSlotWire] | None = Field(
        default=None,
        max_length=9,
    )
    # ``source`` is a bounded free-form/Markdown/JSON/YAML import path.  The
    # service extracts only recognized Persona fields and leaves the rest
    # unknown; it never evaluates imported text as instructions.
    source: str | None = Field(default=None, max_length=256_000)
    source_format: Literal["auto", "json", "yaml", "markdown", "text"] = "auto"

    @model_validator(mode="after")
    def require_one_import_shape(self) -> "PersonaBulkDraftImportRequest":
        if self.slots is None and not (self.source or "").strip():
            raise ValueError("slots or source is required")
        if self.slots is not None and self.source not in (None, ""):
            raise ValueError("provide either slots or source, not both")
        return self


class PersonaBulkDraftCorrectionRequest(
    _SetupCommandModel
):
    expected_version: int = Field(
        ge=1
    )
    slots: list[
        PersonaBulkDraftSlotWire
    ] = Field(
        min_length=9,
        max_length=9,
    )


class PersonaBulkDraftApplyRequest(
    _SetupCommandModel
):
    expected_version: int = Field(
        ge=1
    )


class PersonaBulkDraftIssueResponse(
    _SetupResponseModel
):
    code: str
    slot: int = Field(
        ge=1,
        le=9,
    )
    field: str
    message: str


class PersonaBulkDraftPreviewResponse(
    _SetupResponseModel
):
    id: str
    owner_user_id: str
    project_id: str | None
    source_hash: str
    draft_hash: str
    version: int = Field(
        ge=1
    )
    status: str
    applyable: bool
    issues: list[
        PersonaBulkDraftIssueResponse
    ]
    slots: list[
        PersonaBulkDraftSlotWire
    ] = Field(
        min_length=9,
        max_length=9,
    )
    applied_at: str | None
    created_by: str | None
    created_at: str
    updated_at: str


class PersonaBulkDraftApplyResponse(
    _SetupResponseModel
):
    draft_id: str
    draft_hash: str
    applied_at: str
    created_persona_ids: list[
        str
    ] = Field(
        max_length=9
    )
    intake: PersonaIntakeResponse


class PlatformAccountStateRequest(
    _SetupCommandModel
):
    account_type: str = Field(
        default="profile",
        min_length=1,
        max_length=32,
    )
    display_name: str = Field(
        min_length=1,
        max_length=255,
    )
    remote_url: str | None = Field(
        default=None,
        max_length=2000,
    )
    locale: str | None = Field(
        default=None,
        max_length=64,
    )
    timezone: str | None = Field(
        default=None,
        max_length=64,
    )
    supported_content_modes: list[str] = Field(
        default_factory=list,
        max_length=20,
    )
    disclosure_defaults: dict[str, str | int | float | bool | None] = Field(
        default_factory=dict,
        max_length=20,
    )
    rating_defaults: dict[str, str | int | float | bool | None] = Field(
        default_factory=dict,
        max_length=20,
    )
    adapter_ref: str | None = Field(
        default=None,
        max_length=164,
    )
    connection_id: str | None = None
    status: Literal["active", "paused"] = "active"
class PlatformAccountCreateRequest(
    PlatformAccountStateRequest
):
    platform: MediaPlatform
    account_ref: str = Field(
        min_length=1,
        max_length=255,
    )
    project_id: str | None = None
    persona_id: str | None = None


class PlatformAccountRevisionCreateRequest(
    PlatformAccountStateRequest
):
    expected_version: int = Field(
        ge=1
    )


class PlatformAccountRevisionResponse(
    _SetupResponseModel
):
    id: str
    platform_account_id: str
    owner_user_id: str
    project_id: str | None
    version: int = Field(
        ge=1
    )
    display_name: str
    publish_capability: (
        PlatformCapabilityStatus
    )
    media_capability: (
        PlatformCapabilityStatus
    )
    analytics_capability: (
        PlatformCapabilityStatus
    )
    credential_status: (
        PlatformCredentialStatus
    )
    remote_url: str | None
    locale: str | None
    timezone: str | None
    supported_content_modes: list[str]
    disclosure_defaults: dict[str, str | int | float | bool | None]
    rating_defaults: dict[str, str | int | float | bool | None]
    adapter_ref: str | None
    content_hash: str
    created_by: str | None
    created_at: str


class PlatformAccountSummaryResponse(
    _SetupResponseModel
):
    id: str
    owner_user_id: str
    project_id: str | None
    persona_id: str | None
    connection_id: str | None
    account_type: str
    remote_url: str | None
    status: Literal["active", "paused"]
    platform: MediaPlatform
    account_ref: str
    create_hash: str
    created_by: str | None
    created_at: str
    current_revision: (
        PlatformAccountRevisionResponse
    )


class PlatformAccountDetailResponse(
    PlatformAccountSummaryResponse
):
    revisions: list[
        PlatformAccountRevisionResponse
    ] = Field(
        max_length=100
    )
    revision_history_truncated: bool


PlatformAccountListResponse = list[
    PlatformAccountSummaryResponse
]


class MediaCredentialCapabilitiesResponse(_SetupResponseModel):
    identity: str
    publish: str
    media: str
    analytics: str


class MediaCredentialResponse(_SetupResponseModel):
    id: str
    platform_account_id: str
    connection_id: str
    owner_user_id: str
    project_id: str | None
    platform: str | None = None
    account_ref: str | None = None
    connection_type: Literal["cookie_export", "api_token", "oauth"]
    revision: int = Field(ge=1)
    status: Literal[
        "verification_pending",
        "verified",
        "invalid",
        "unsupported",
        "disabled",
        "key_unavailable",
    ]
    capabilities: MediaCredentialCapabilitiesResponse
    verification_code: str | None
    verification_started_at: str | None
    verification_completed_at: str | None
    last_verified_at: str | None
    disabled_at: str | None
    created_by: str | None
    created_at: str | None
    updated_at: str | None


class MediaCredentialCommandResponse(_SetupResponseModel):
    platform_account: PlatformAccountDetailResponse
    credential: MediaCredentialResponse


class MediaCredentialAuditResponse(_SetupResponseModel):
    id: str
    credential_id: str
    platform_account_id: str
    owner_user_id: str
    project_id: str | None
    event_type: Literal["add", "rotate", "verify", "disable", "rekey"]
    actor_id: str | None
    actor_type: str
    sequence: int = Field(ge=1)
    snapshot: dict[str, Any]
    status: str | None = None
    revision: int | None = None
    verification_code: str | None = None
    created_at: str | None


class MediaCredentialRevisionRequest(_SetupCommandModel):
    expected_revision: int = Field(ge=1)


class MediaCredentialJsonRequest(_SetupCommandModel):
    connection_type: Literal["cookie_export", "api_token", "oauth"]
    package: dict[str, Any] | list[Any] | str


class _BoundedCredentialMultipartParser(MultiPartParser):
    """Multipart parser that keeps the credential file in memory only.

    Starlette's default parser rolls files to a disk-backed temporary file at
    1 MiB.  Credential uploads must never leave plaintext on disk, so the
    spool threshold is raised to the hard 2 MiB package limit and each file
    part is counted before it is queued for writing.  The route still closes
    every returned UploadFile in a ``finally`` block.
    """

    spool_max_size = MAX_CREDENTIAL_PACKAGE_BYTES

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._credential_file_size = 0

    def on_part_begin(self) -> None:
        super().on_part_begin()
        self._credential_file_size = 0

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        chunk = data[start:end]
        if getattr(self._current_part, "file", None) is not None:
            self._credential_file_size += len(chunk)
            if self._credential_file_size > MAX_CREDENTIAL_PACKAGE_BYTES:
                raise MultiPartException("credential package is too large")
        super().on_part_data(data, start, end)


async def _bounded_request_stream(request: Request):
    """Yield request bytes with a bounded multipart/JSON envelope."""

    # Allow a modest envelope for multipart boundaries and command fields,
    # while ensuring chunked requests cannot bypass the package limit.
    envelope_limit = MAX_CREDENTIAL_PACKAGE_BYTES + 128 * 1024
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > envelope_limit:
            raise HTTPException(status_code=413, detail="credential package is too large")
        yield chunk


async def _read_credential_multipart(request: Request, *, require_fields: set[str]) -> dict[str, Any]:
    """Read a bounded upload without retaining filename/path metadata."""

    content_type = request.headers.get("content-type", "").lower()
    if content_type.startswith("multipart/"):
        declared = request.headers.get("content-length")
        if declared:
            try:
                if int(declared) > MAX_CREDENTIAL_PACKAGE_BYTES + 64 * 1024:
                    raise HTTPException(status_code=413, detail="credential package is too large")
            except ValueError:
                raise HTTPException(status_code=400, detail="invalid content-length") from None
        parser: _BoundedCredentialMultipartParser | None = None
        parsed = False
        try:
            parser = _BoundedCredentialMultipartParser(
                request.headers,
                _bounded_request_stream(request),
                max_files=1,
                max_fields=16,
                max_part_size=32 * 1024,
            )
            form = await parser.parse()
            parsed = True
        except HTTPException:
            raise
        except MultiPartException as exc:
            if "too large" in str(exc).casefold() or "maximum size" in str(exc).casefold():
                raise HTTPException(status_code=413, detail="credential package is too large") from None
            raise HTTPException(status_code=422, detail="invalid credential multipart payload") from None
        except Exception:
            raise HTTPException(status_code=422, detail="invalid credential multipart payload") from None
        finally:
            # Starlette only closes its pending file handles for
            # MultiPartException/OSError.  Our bounded request stream raises
            # HTTPException for an oversized chunk, so explicitly close any
            # files that were created before that envelope error.  The normal
            # success path keeps the returned UploadFile alive until the
            # bounded read below closes it.
            if parser is not None and not parsed:
                for pending in getattr(parser, "_files_to_close_on_error", ()):
                    try:
                        pending.close()
                    except Exception:
                        pass
        upload_items = [
            (str(key), value)
            for key, value in form.multi_items()
            if callable(getattr(value, "read", None))
        ]
        if len(upload_items) != 1 or upload_items[0][0] not in {"file", "package"}:
            for _, candidate in upload_items:
                try:
                    await candidate.close()
                except Exception:
                    pass
            raise HTTPException(status_code=422, detail="credential file is required")
        values = {str(key): value for key, value in form.multi_items() if not callable(getattr(value, "read", None))}
        upload = upload_items[0][1]
        try:
            raw = await upload.read(MAX_CREDENTIAL_PACKAGE_BYTES + 1)
            if len(raw) > MAX_CREDENTIAL_PACKAGE_BYTES:
                raise HTTPException(status_code=413, detail="credential package is too large")
        finally:
            for _, candidate in upload_items:
                try:
                    await candidate.close()
                except Exception:
                    pass
        values["package"] = raw
        missing = [field for field in require_fields if field not in values or values[field] in (None, "")]
        if missing:
            raise HTTPException(status_code=422, detail=f"{missing[0]} is required")
        return values
    try:
        chunks: list[bytes] = []
        async for chunk in _bounded_request_stream(request):
            chunks.append(bytes(chunk))
        raw = b"".join(chunks)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=422, detail="invalid credential payload") from None
    try:
        import json

        decoded = json.loads(raw.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=422, detail="invalid credential JSON payload") from None
    if not isinstance(decoded, dict):
        raise HTTPException(status_code=422, detail="credential payload must be an object")
    if "package" in decoded and isinstance(decoded["package"], (dict, list)):
        import json

        try:
            decoded["package"] = json.dumps(
                decoded["package"],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError, OverflowError, UnicodeError, RecursionError, MemoryError):
            # Never let malformed Unicode or pathological nesting turn a
            # client payload into a 500 response (or expose serializer detail).
            raise HTTPException(status_code=422, detail="invalid credential JSON payload") from None
    missing = [field for field in require_fields if field not in decoded or decoded[field] in (None, "")]
    if missing:
        raise HTTPException(status_code=422, detail=f"{missing[0]} is required")
    return decoded


def _credential_upload_openapi(
    *,
    multipart_required: list[str],
    multipart_properties: dict[str, Any],
    json_required: list[str] | None = None,
    json_properties: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an exact per-operation upload schema.

    A single permissive schema was previously shared by add/create/rotate.
    That made generated clients believe fields such as ``platform`` or
    ``expected_revision`` were accepted everywhere.  Keep each operation's
    contract honest while the runtime parser remains strict as well.
    """

    json_required = json_required or ["connection_type", "package"]
    json_properties = json_properties or {
        "connection_type": {"type": "string", "enum": ["cookie_export", "api_token", "oauth"]},
        "package": {"type": ["object", "array", "string"]},
    }
    return {
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": multipart_required,
                        "properties": multipart_properties,
                    }
                },
                "application/json": {
                    "schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": json_required,
                        "properties": json_properties,
                    }
                },
            },
        }
    }


_CHARACTER_CONNECTION_OPENAPI = _credential_upload_openapi(
    multipart_required=["platform", "account_ref", "display_name", "connection_type", "file"],
    multipart_properties={
        "platform": {"type": "string", "enum": ["x", "pixiv", "patreon", "youtube", "instagram", "dlsite"]},
        "account_ref": {"type": "string", "minLength": 1, "maxLength": 255},
        "display_name": {"type": "string", "minLength": 1, "maxLength": 255},
        "connection_type": {"type": "string", "enum": ["cookie_export", "api_token", "oauth"]},
        "project_id": {"type": ["string", "null"]},
        "file": {"type": "string", "format": "binary"},
    },
    json_required=["platform", "account_ref", "display_name", "connection_type", "package"],
    json_properties={
        "platform": {"type": "string", "enum": ["x", "pixiv", "patreon", "youtube", "instagram", "dlsite"]},
        "account_ref": {"type": "string", "minLength": 1, "maxLength": 255},
        "display_name": {"type": "string", "minLength": 1, "maxLength": 255},
        "connection_type": {"type": "string", "enum": ["cookie_export", "api_token", "oauth"]},
        "project_id": {"type": ["string", "null"]},
        "package": {"type": ["object", "array", "string"]},
    },
)
_CREATE_CREDENTIAL_OPENAPI = _credential_upload_openapi(
    multipart_required=["connection_type", "file"],
    multipart_properties={
        "connection_type": {"type": "string", "enum": ["cookie_export", "api_token", "oauth"]},
        "file": {"type": "string", "format": "binary"},
    },
)
_ROTATE_CREDENTIAL_OPENAPI = _credential_upload_openapi(
    multipart_required=["connection_type", "expected_revision", "file"],
    multipart_properties={
        "connection_type": {"type": "string", "enum": ["cookie_export", "api_token", "oauth"]},
        "expected_revision": {"type": "integer", "minimum": 1},
        "file": {"type": "string", "format": "binary"},
    },
    json_required=["connection_type", "expected_revision", "package"],
    json_properties={
        "connection_type": {"type": "string", "enum": ["cookie_export", "api_token", "oauth"]},
        "expected_revision": {"type": "integer", "minimum": 1},
        "package": {"type": ["object", "array", "string"]},
    },
)


def create_media_operations_setup_router(
    get_db_manager: Callable[[], Any],
    get_user_from_request: Callable[..., Any],
    require_auth_dependency: Callable[..., Any],
) -> APIRouter:
    router = APIRouter(
        prefix="/api/operations/media",
        tags=["operations-media"],
    )
    setup = (
        MediaOperationsSetupService()
    )
    credential_vault = MediaCredentialVaultService()

    async def current_actor(
        request: Request,
    ) -> dict[str, Any]:
        user = await _actor(
            get_user_from_request,
            request,
        )
        return _principal_projection(
            user
        )

    @router.post(
        "/persona-drafts",
        response_model=(
            PersonaBulkDraftPreviewResponse
        ),
        operation_id=(
            "media_import_persona_bulk_draft"
        ),
    )
    async def import_persona_bulk_draft(
        payload: PersonaBulkDraftImportRequest,
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
        actor = await current_actor(
            request
        )
        data = payload.model_dump(
            mode="json"
        )
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                setup.import_persona_bulk_draft,
                session,
                actor,
                **data,
                idempotency_key=(
                    idempotency_key
                ),
            ),
        )

    @router.get(
        "/persona-drafts/{draft_id}",
        response_model=(
            PersonaBulkDraftPreviewResponse
        ),
        operation_id=(
            "media_get_persona_bulk_draft"
        ),
    )
    async def get_persona_bulk_draft(
        draft_id: str,
        request: Request,
        _: Any = Depends(
            require_auth_dependency
        ),
    ) -> Any:
        actor = await current_actor(
            request
        )
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                setup.get_persona_bulk_draft,
                session,
                actor,
                draft_id,
            ),
        )

    @router.put(
        "/persona-drafts/{draft_id}",
        response_model=(
            PersonaBulkDraftPreviewResponse
        ),
        operation_id=(
            "media_correct_persona_bulk_draft"
        ),
    )
    async def correct_persona_bulk_draft(
        draft_id: str,
        payload: (
            PersonaBulkDraftCorrectionRequest
        ),
        request: Request,
        _: Any = Depends(
            require_auth_dependency
        ),
    ) -> Any:
        actor = await current_actor(
            request
        )
        data = payload.model_dump(
            mode="json"
        )
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                setup.correct_persona_bulk_draft,
                session,
                actor,
                draft_id,
                **data,
            ),
        )

    @router.post(
        "/persona-drafts/{draft_id}/apply",
        response_model=(
            PersonaBulkDraftApplyResponse
        ),
        operation_id=(
            "media_apply_persona_bulk_draft"
        ),
    )
    async def apply_persona_bulk_draft(
        draft_id: str,
        payload: (
            PersonaBulkDraftApplyRequest
        ),
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
        actor = await current_actor(
            request
        )
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                setup.apply_persona_bulk_draft,
                session,
                actor,
                draft_id,
                expected_version=(
                    payload.expected_version
                ),
                idempotency_key=(
                    idempotency_key
                ),
            ),
        )

    @router.get(
        "/platform-accounts",
        response_model=(
            PlatformAccountListResponse
        ),
        operation_id=(
            "media_list_platform_accounts"
        ),
    )
    async def list_platform_accounts(
        request: Request,
        project_id: str | None = Query(
            default=None
        ),
        persona_id: str | None = Query(
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
        actor = await current_actor(
            request
        )
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                setup.list_platform_accounts,
                session,
                actor,
                project_id=project_id,
                persona_id=persona_id,
                limit=limit,
                offset=offset,
            ),
        )

    @router.post(
        "/platform-accounts",
        response_model=(
            PlatformAccountDetailResponse
        ),
        operation_id=(
            "media_create_platform_account"
        ),
    )
    async def create_platform_account(
        payload: PlatformAccountCreateRequest,
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
        actor = await current_actor(
            request
        )
        data = payload.model_dump(
            mode="json"
        )
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                setup.create_platform_account,
                session,
                actor,
                **data,
                idempotency_key=(
                    idempotency_key
                ),
            ),
        )

    @router.get(
        "/platform-accounts/{account_id}",
        response_model=(
            PlatformAccountDetailResponse
        ),
        operation_id=(
            "media_get_platform_account"
        ),
    )
    async def get_platform_account(
        account_id: str,
        request: Request,
        _: Any = Depends(
            require_auth_dependency
        ),
    ) -> Any:
        actor = await current_actor(
            request
        )
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                setup.get_platform_account,
                session,
                actor,
                account_id,
            ),
        )

    @router.post(
        "/platform-accounts/{account_id}/revisions",
        response_model=(
            PlatformAccountRevisionResponse
        ),
        operation_id=(
            "media_append_platform_account_revision"
        ),
    )
    async def append_platform_account_revision(
        account_id: str,
        payload: (
            PlatformAccountRevisionCreateRequest
        ),
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
        actor = await current_actor(
            request
        )
        data = payload.model_dump(
            mode="json"
        )
        # Account identity/status fields belong to PlatformAccount and are not
        # mutable through an immutable revision append.
        data.pop("account_type", None)
        data.pop("connection_id", None)
        data.pop("status", None)
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                setup.append_platform_account_revision,
                session,
                actor,
                account_id,
                **data,
                idempotency_key=(
                    idempotency_key
                ),
            ),
        )

    @router.post(
        "/characters/{character_id}/platform-connections",
        response_model=MediaCredentialCommandResponse,
        operation_id="media_add_character_platform_connection",
        openapi_extra=_CHARACTER_CONNECTION_OPENAPI,
    )
    async def add_character_platform_connection(
        character_id: str,
        request: Request,
        idempotency_key: Annotated[
            str,
            Header(alias="Idempotency-Key", min_length=1, max_length=255),
        ],
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        values = await _read_credential_multipart(
            request,
            require_fields={"platform", "account_ref", "display_name", "connection_type", "package"},
        )
        allowed = {"platform", "account_ref", "display_name", "connection_type", "package", "project_id"}
        unknown = set(values) - allowed
        if unknown:
            raise HTTPException(status_code=422, detail="credential command contains an unknown field")
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                credential_vault.add_platform_connection,
                session,
                actor,
                character_id=character_id,
                platform=values["platform"],
                account_ref=values["account_ref"],
                display_name=values["display_name"],
                connection_type=values["connection_type"],
                package=values["package"],
                project_id=values.get("project_id"),
                idempotency_key=idempotency_key,
            ),
        )

    @router.post(
        "/platform-accounts/{account_id}/credential",
        response_model=MediaCredentialResponse,
        operation_id="media_create_platform_credential",
        openapi_extra=_CREATE_CREDENTIAL_OPENAPI,
    )
    async def create_platform_credential(
        account_id: str,
        request: Request,
        idempotency_key: Annotated[
            str,
            Header(alias="Idempotency-Key", min_length=1, max_length=255),
        ],
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        values = await _read_credential_multipart(request, require_fields={"connection_type", "package"})
        allowed = {"connection_type", "package"}
        if set(values) - allowed:
            raise HTTPException(status_code=422, detail="credential command contains an unknown field")
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                credential_vault.create_credential,
                session,
                actor,
                account_id,
                package=values["package"],
                connection_type=values["connection_type"],
                idempotency_key=idempotency_key,
            ),
        )

    @router.get(
        "/platform-accounts/{account_id}/credential",
        response_model=MediaCredentialResponse,
        operation_id="media_get_platform_credential",
    )
    async def get_platform_credential(
        account_id: str,
        request: Request,
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(credential_vault.get_credential, session, actor, account_id),
        )

    @router.post(
        "/platform-accounts/{account_id}/credential/rotate",
        response_model=MediaCredentialResponse,
        operation_id="media_rotate_platform_credential",
        openapi_extra=_ROTATE_CREDENTIAL_OPENAPI,
    )
    async def rotate_platform_credential(
        account_id: str,
        request: Request,
        idempotency_key: Annotated[
            str,
            Header(alias="Idempotency-Key", min_length=1, max_length=255),
        ],
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        values = await _read_credential_multipart(request, require_fields={"connection_type", "expected_revision", "package"})
        allowed = {"connection_type", "expected_revision", "package"}
        if set(values) - allowed:
            raise HTTPException(status_code=422, detail="credential command contains an unknown field")
        try:
            expected_revision = int(values["expected_revision"])
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="expected_revision must be an integer") from None
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                credential_vault.rotate_credential,
                session,
                actor,
                account_id,
                expected_revision=expected_revision,
                package=values["package"],
                connection_type=values["connection_type"],
                idempotency_key=idempotency_key,
            ),
        )

    @router.post(
        "/platform-accounts/{account_id}/credential/verify",
        response_model=MediaCredentialResponse,
        operation_id="media_verify_platform_credential",
    )
    async def verify_platform_credential(
        account_id: str,
        payload: MediaCredentialRevisionRequest,
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
                credential_vault.verify_credential,
                session,
                actor,
                account_id,
                expected_revision=payload.expected_revision,
                idempotency_key=idempotency_key,
            ),
        )

    @router.post(
        "/platform-accounts/{account_id}/credential/disable",
        response_model=MediaCredentialResponse,
        operation_id="media_disable_platform_credential",
    )
    async def disable_platform_credential(
        account_id: str,
        payload: MediaCredentialRevisionRequest,
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
                credential_vault.disable_credential,
                session,
                actor,
                account_id,
                expected_revision=payload.expected_revision,
                idempotency_key=idempotency_key,
            ),
        )

    @router.get(
        "/platform-accounts/{account_id}/credential/audit",
        response_model=list[MediaCredentialAuditResponse],
        operation_id="media_list_platform_credential_audit",
    )
    async def list_platform_credential_audit(
        account_id: str,
        request: Request,
        limit: int = Query(default=100, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        return await _with_session(
            get_db_manager,
            lambda session: _invoke(
                credential_vault.list_credential_audit,
                session,
                actor,
                account_id,
                limit=limit,
                offset=offset,
            ),
        )

    return router
