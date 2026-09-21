"""Authenticated, opaque-only HTTP boundary for MediaOps Generation Studio.

The MediaOps API owns the intent and provenance of a generation request.  It
does not expose provider workflow graphs, local paths, credentials, or raw
adapter responses.  Generation is deliberately fail-closed when the Studio
adapter is unavailable; these routes only call the typed service contract.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from inspect import isawaitable, signature
from typing import Annotated, Any, Callable, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy.exc import IntegrityError

from ..services import media_operations_generation_service as service
from ..services import media_operations_service as base_service
from .media_operations_routes import (
    _actor,
    _principal_projection,
    _with_session,
)


_OPAQUE_ID_RE = r"^[A-Za-z][A-Za-z0-9._:-]{1,254}$"
_STUDIO_WORKSPACE_ID_RE = r"^wsp_[A-Za-z0-9_-]{8,160}$"
_STUDIO_PROJECT_ID_RE = r"^prj_[A-Za-z0-9_-]{8,160}$"
_STUDIO_RUN_ID_RE = r"^run_[A-Za-z0-9_-]{8,160}$"
_STUDIO_ASSET_ID_RE = r"^ast_[A-Za-z0-9_-]{8,160}$"
_STUDIO_OUTPUT_VERSION_RE = r"^outv_[A-Za-z0-9_-]{8,160}$"
_IMAGE_MODEL_SELECTION_RE = (
    r"^(?:ims|imd|ien)_[A-Za-z0-9_-]{8,160}$"
)
_VIDEO_MODEL_SELECTION_RE = r"^wsl_[A-Za-z0-9_-]{8,160}$"
_MODEL_SELECTION_RE = (
    r"^(?:(?:ims|imd|ien|wsl)_[A-Za-z0-9_-]{8,160})$"
)
_SHA256_RE = r"^[0-9a-fA-F]{64}$"

OpaqueId = Annotated[str, Field(min_length=2, max_length=255, pattern=_OPAQUE_ID_RE)]
StudioWorkspaceId = Annotated[
    str,
    Field(min_length=12, max_length=164, pattern=_STUDIO_WORKSPACE_ID_RE),
]
StudioProjectId = Annotated[
    str,
    Field(min_length=12, max_length=164, pattern=_STUDIO_PROJECT_ID_RE),
]
StudioRunId = Annotated[
    str,
    Field(min_length=12, max_length=164, pattern=_STUDIO_RUN_ID_RE),
]
StudioAssetId = Annotated[
    str,
    Field(min_length=12, max_length=164, pattern=_STUDIO_ASSET_ID_RE),
]
StudioOutputVersion = Annotated[
    str,
    Field(min_length=13, max_length=165, pattern=_STUDIO_OUTPUT_VERSION_RE),
]
ImageModelSelectionId = Annotated[
    str,
    Field(min_length=12, max_length=164, pattern=_IMAGE_MODEL_SELECTION_RE),
]
VideoModelSelectionId = Annotated[
    str,
    Field(min_length=12, max_length=164, pattern=_VIDEO_MODEL_SELECTION_RE),
]
ModelSelectionId = Annotated[
    str,
    Field(min_length=12, max_length=164, pattern=_MODEL_SELECTION_RE),
]
Sha256 = Annotated[str, Field(min_length=64, max_length=64, pattern=_SHA256_RE)]


class _GenerationCommandModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class _GenerationResponseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


def _validate_origin(value: str) -> str:
    """Accept an HTTP(S) origin only; never store client URL credentials/path."""

    text = str(value or "").strip()
    if not text or any(ch.isspace() for ch in text):
        raise ValueError("base_url must be an HTTP(S) origin")
    try:
        parsed = urlsplit(text)
        # Accessing port catches malformed values such as ``:abc``.
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("base_url must be an HTTP(S) origin") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("base_url must be an HTTP(S) origin without credentials")
    # urlsplit keeps the original case.  The adapter receives a canonical
    # origin without a trailing slash and without userinfo/query components.
    host = parsed.hostname.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    authority = host
    if parsed.port is not None:
        authority = f"{authority}:{parsed.port}"
    return f"{parsed.scheme.lower()}://{authority}"


class GenerationWorkspaceCreateRequest(_GenerationCommandModel):
    project_id: str | None = None
    provider: Literal["comfyui_workbench"] = "comfyui_workbench"
    external_workspace_id: StudioWorkspaceId
    external_project_id: StudioProjectId | None = None
    base_url: str
    status: Literal["configured", "verified", "unavailable"] = "configured"

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        return _validate_origin(value)


class GenerationWorkspaceResponse(_GenerationResponseModel):
    id: str
    owner_user_id: str
    project_id: str | None
    provider: Literal["comfyui_workbench"]
    external_workspace_id: StudioWorkspaceId
    external_project_id: StudioProjectId | None
    base_url: str
    status: Literal["configured", "verified", "unavailable"]
    config_hash: Sha256
    created_by: str | None = None
    created_at: str
    updated_at: str | None = None


class CreativeRecipeRevisionFields(_GenerationCommandModel):
    recipe_type: Literal["image", "image_set", "comic", "video", "thumbnail"]
    # ``image_model_selection_id`` is the legacy wire name.  Keep accepting
    # it while allowing the canonical video ``wsl_`` selection IDs as well.
    model_selection_id: ModelSelectionId | None = Field(
        default=None,
        alias="image_model_selection_id",
    )
    workflow_id: OpaqueId | None = None
    identity_pack_id: OpaqueId | None = None
    prompt_schema_id: OpaqueId | None = None
    prompt_template: str = Field(min_length=1, max_length=16_000)
    negative_requirements: str | None = Field(default=None, max_length=8_000)
    reference_asset_ids: list[OpaqueId] = Field(default_factory=list, max_length=32)
    aspect_ratio: str | None = Field(
        default=None,
        max_length=16,
        pattern=r"^\d{1,3}:\d{1,3}$",
    )
    width: int | None = Field(default=None, ge=1, le=16_384)
    height: int | None = Field(default=None, ge=1, le=16_384)
    duration_seconds: int | None = Field(default=None, ge=1, le=86_400)
    frame_count: int | None = Field(default=None, ge=1, le=100_000)
    storyboard: list[str] = Field(default_factory=list, max_length=100)
    candidate_count: int = Field(default=1, ge=1, le=20)
    cost_policy: dict[str, str | int | float | bool | None] = Field(
        default_factory=dict,
        max_length=32,
    )
    provenance_retention_policy: dict[str, str | int | float | bool | None] = Field(
        default_factory=dict,
        max_length=32,
    )
    human_review_policy: dict[str, str | int | float | bool | None] = Field(
        default_factory=dict,
        max_length=32,
    )


class CreativeRecipeCreateRequest(CreativeRecipeRevisionFields):
    persona_id: str
    persona_revision_id: str | None = None
    name: str = Field(min_length=1, max_length=255)
    project_id: str | None = None


class CreativeRecipeRevisionCreateRequest(CreativeRecipeRevisionFields):
    expected_version: int = Field(ge=1)


class CreativeRecipeRevisionResponse(_GenerationResponseModel):
    id: str
    creative_recipe_id: str
    owner_user_id: str
    project_id: str | None
    persona_id: str | None = None
    persona_revision_id: str | None = None
    version: int = Field(ge=1)
    recipe_type: Literal["image", "image_set", "comic", "video", "thumbnail"]
    model_selection_id: ModelSelectionId | None = None
    image_model_selection_id: ImageModelSelectionId | None = None
    workflow_id: OpaqueId | None = None
    identity_pack_id: OpaqueId | None = None
    prompt_schema_id: OpaqueId | None = None
    prompt_template: str
    negative_requirements: str | None
    reference_asset_ids: list[OpaqueId] = Field(max_length=32)
    aspect_ratio: str | None
    width: int | None
    height: int | None
    duration_seconds: int | None
    frame_count: int | None
    storyboard: list[str] = Field(max_length=100)
    candidate_count: int = Field(ge=1, le=20)
    cost_policy: dict[str, str | int | float | bool | None] = Field(max_length=32)
    provenance_retention_policy: dict[str, str | int | float | bool | None] = Field(
        default_factory=dict,
        max_length=32,
    )
    human_review_policy: dict[str, str | int | float | bool | None] = Field(
        default_factory=dict,
        max_length=32,
    )
    content_hash: Sha256
    created_by: str | None = None
    created_at: str


class CreativeRecipeSummaryResponse(_GenerationResponseModel):
    id: str
    owner_user_id: str
    project_id: str | None
    persona_id: str
    # CreativeRecipe is a stable Persona association.  Older rows do not
    # carry a display name, so the field remains optional at the boundary.
    name: str | None = None
    create_hash: Sha256
    created_by: str | None = None
    created_at: str
    current_revision: CreativeRecipeRevisionResponse


class CreativeRecipeDetailResponse(CreativeRecipeSummaryResponse):
    revisions: list[CreativeRecipeRevisionResponse] = Field(max_length=100)
    revision_history_truncated: bool


class GenerationSettings(_GenerationCommandModel):
    values: dict[str, str | int | float | bool | None] = Field(
        default_factory=dict,
        max_length=32,
    )

    @field_validator("values")
    @classmethod
    def validate_scalar_settings(
        cls,
        values: dict[str, str | int | float | bool | None],
    ) -> dict[str, str | int | float | bool | None]:
        forbidden = (
            "path",
            "secret",
            "token",
            "credential",
            "password",
            "graph",
            "workflow",
            "provider",
        )
        for key in values:
            normalized = str(key).strip().lower()
            if not normalized or any(part in normalized for part in forbidden):
                raise ValueError("generation_settings contains a forbidden key")
            if not normalized.replace("_", "").isalnum():
                raise ValueError("generation_settings keys must be simple names")
        return values


class GenerationRequestSpecRequest(_GenerationCommandModel):
    prompt: str = Field(min_length=1, max_length=16_000)
    negative_prompt: str = Field(default="", max_length=8_000)
    seed: int | None = Field(default=None, ge=0, le=2**63 - 1)
    # Canonical selector plus the pre-WS4 image alias.  A selector is required
    # after aliases are resolved; ``wsl_`` selects video and ``ims_``/``imd_``/
    # ``ien_`` selects image generation.
    model_selection_id: ModelSelectionId | None = None
    image_model_selection_id: ImageModelSelectionId | None = None
    generation_settings: dict[str, str | int | float | bool | None] = Field(
        default_factory=dict,
        max_length=32,
    )
    size_preset_id: Literal[
        "normal_square",
        "normal_landscape",
        "normal_portrait",
        "custom",
    ] | None = None
    width: int | None = Field(default=None, ge=64, le=2048)
    height: int | None = Field(default=None, ge=64, le=2048)
    accept_metered_generation: bool = False
    aspect_ratio: str | None = Field(
        default=None,
        max_length=16,
        pattern=r"^[1-9][0-9]{0,2}:[1-9][0-9]{0,2}$",
    )
    duration_seconds: int | None = Field(default=None, ge=1, le=86_400)
    frame_count: int | None = Field(default=None, ge=1, le=100_000)
    storyboard: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("generation_settings")
    @classmethod
    def validate_generation_settings(cls, value: dict[str, Any]) -> dict[str, Any]:
        return GenerationSettings(values=value).values

    @model_validator(mode="after")
    def validate_custom_size(self) -> "GenerationRequestSpecRequest":
        selector = self.model_selection_id or self.image_model_selection_id
        if selector is None:
            raise ValueError("model_selection_id is required")
        if (
            self.model_selection_id is not None
            and self.image_model_selection_id is not None
            and self.model_selection_id != self.image_model_selection_id
        ):
            raise ValueError("model_selection_id aliases must match")

        is_video = selector.startswith("wsl_")
        has_image_fields = any(
            value is not None
            for value in (
                self.size_preset_id,
                self.width,
                self.height,
                self.aspect_ratio,
            )
        )
        has_video_fields = (
            self.duration_seconds is not None
            or self.frame_count is not None
            or bool(self.storyboard)
        )
        if is_video:
            if has_image_fields:
                raise ValueError("image size fields are not valid for video generation")
        else:
            if has_video_fields:
                raise ValueError("video fields are not valid for image generation")
            if self.size_preset_id is None:
                self.size_preset_id = "normal_square"
            if self.size_preset_id == "custom":
                if self.width is None or self.height is None:
                    raise ValueError("custom size requires width and height")
                if self.width % 64 or self.height % 64:
                    raise ValueError("custom size dimensions must be multiples of 64")
            elif self.width is not None or self.height is not None:
                raise ValueError("width and height are only valid for custom size")
        return self


class GenerationPlanCreateRequest(_GenerationCommandModel):
    persona_revision_id: str
    creative_recipe_revision_id: str
    workspace_id: str
    content_item_id: str | None = None
    content_variant_id: str | None = None
    requested_outputs: int = Field(default=1, ge=1, le=20)
    request_spec: GenerationRequestSpecRequest


class GenerationPlanResponse(_GenerationResponseModel):
    id: str
    owner_user_id: str
    project_id: str | None
    persona_revision_id: str
    persona_revision_hash: Sha256
    creative_recipe_revision_id: str
    workspace_id: str
    content_item_id: str | None = None
    content_variant_id: str | None = None
    requested_outputs: int = Field(ge=1, le=20)
    request_spec: GenerationRequestSpecRequest
    # Derived solely from the opaque selection ID/request fields.  The client
    # cannot choose a conflicting kind or smuggle provider metadata here.
    generation_kind: Literal["image", "video"]
    plan_hash: Sha256
    status: Literal["draft", "submitted", "unavailable", "uncertain", "succeeded", "failed"]
    created_by: str | None = None
    created_at: str
    updated_at: str | None = None


class GenerationOutputResponse(_GenerationResponseModel):
    id: str
    external_asset_id: StudioAssetId
    external_output_version: StudioOutputVersion
    sha256: Sha256
    # Generation output is intentionally closed to media MIME types.  A raw
    # provider payload (``application/json``, local files, etc.) must never
    # become a user-visible output receipt.
    mime_type: str = Field(
        min_length=3,
        max_length=255,
        # Images accept the server's bounded image/* variants.  Video is
        # intentionally closed to the MIME values owned by the 73 semantic
        # contract; accepting arbitrary video/foo would allow an unverified
        # provider payload to masquerade as a durable receipt.
        pattern=r"^(?:image/[A-Za-z0-9][A-Za-z0-9.+-]*|video/(?:mp4|webm|quicktime|x-matroska|other))$",
    )
    width: int | None = Field(default=None, ge=1, le=100_000)
    height: int | None = Field(default=None, ge=1, le=100_000)
    deep_link: str | None = Field(default=None, max_length=4_000)
    provenance_hash: Sha256
    created_at: str | None = None


class GenerationCatalogItemResponse(_GenerationResponseModel):
    kind: Literal["image", "video"]
    model_selection_id: ModelSelectionId
    # Kept for clients that shipped before the generic catalog contract.
    image_model_selection_id: ImageModelSelectionId | None = None
    display_name: str | None = Field(default=None, max_length=255)
    status: Literal["available", "unavailable", "configured", "verified"] = "available"


GenerationCatalogResponse = list[GenerationCatalogItemResponse]


class GenerationRunResponse(_GenerationResponseModel):
    id: str
    owner_user_id: str
    project_id: str | None
    plan_id: str
    external_run_id: StudioRunId
    external_workspace_id: StudioWorkspaceId | None = None
    external_project_id: StudioProjectId | None = None
    status: Literal[
        "draft",
        "queued",
        "claimed",
        "running",
        "output_pending",
        "succeeded",
        "failed",
        "cancelled",
        "quarantined",
        "uncertain",
        "unavailable",
    ]
    adapter_release: str | None = None
    cost_summary: dict[str, str | int | float | bool | None] = Field(
        default_factory=dict,
        max_length=32,
    )
    error_code: str | None = None
    result_deep_link: str | None = Field(default=None, max_length=4_000)
    outputs: list[GenerationOutputResponse] = Field(default_factory=list, max_length=20)
    run_hash: Sha256
    created_by: str | None = None
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    updated_at: str | None = None


class GenerationRunIntentResponse(_GenerationResponseModel):
    id: str
    plan_id: str
    owner_user_id: str | None = None
    project_id: str | None = None
    external_idempotency_key: str
    request_hash: Sha256
    status: Literal["pending", "submitted", "unavailable", "uncertain", "failed"]
    external_run_id: StudioRunId | None = None
    adapter_release: str | None = None
    error_code: str | None = None
    response_hash: Sha256 | None = None
    created_by: str | None = None
    created_at: str
    updated_at: str | None = None


class GenerationSubmitResponse(_GenerationResponseModel):
    plan: GenerationPlanResponse
    intent: GenerationRunIntentResponse
    run: GenerationRunResponse | None = None
    outputs: list[GenerationOutputResponse] = Field(default_factory=list, max_length=20)
    status: Literal["pending", "submitted", "unavailable", "uncertain", "failed"]
    external_idempotency_key: str


class GenerationSubmitRequest(_GenerationCommandModel):
    """Immutable-plan guard; the request specification lives on the Plan."""

    expected_plan_hash: Sha256 | None = None
    acknowledge_metered_generation: bool = False


class GenerationReconcileRequest(_GenerationCommandModel):
    """Reconcile a durable generation intent without resubmitting it."""

    expected_plan_hash: Sha256
    expected_intent_request_hash: Sha256
    acknowledge_metered_generation: bool = False


class GenerationOutputSelectionRequest(_GenerationCommandModel):
    output_id: str


class GenerationOutputSelectionResponse(_GenerationResponseModel):
    id: str
    generation_run_id: str
    generation_output_id: str
    owner_user_id: str
    project_id: str | None
    selection_hash: Sha256
    created_by: str | None = None
    created_at: str
    output: GenerationOutputResponse


GenerationWorkspaceListResponse = list[GenerationWorkspaceResponse]
CreativeRecipeListResponse = list[CreativeRecipeSummaryResponse]
GenerationPlanListResponse = list[GenerationPlanResponse]
GenerationRunListResponse = list[GenerationRunResponse]


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _first(value: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        candidate = _field(value, name)
        if candidate is not None:
            return candidate
    return default


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _safe_public_url(value: Any, *, origin_only: bool = False) -> str | None:
    """Project only HTTP(S) URLs; provider paths and credentials are omitted."""

    if value is None:
        return None
    try:
        text = str(value).strip()
        parsed = urlsplit(text)
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or any(ch.isspace() for ch in text)
            or parsed.query
            or parsed.fragment
        ):
            return None
        if origin_only and parsed.path not in {"", "/"}:
            return None
        # Avoid returning an unbounded provider response value.
        if len(text) > 4000:
            return None
        return text
    except (TypeError, ValueError):
        return None


def _safe_settings(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    try:
        return GenerationSettings(values=dict(value)).values
    except ValueError:
        return {}


def _generation_kind(value: Any, explicit: Any = None) -> str:
    """Derive the semantic kind from the selection, never provider payload."""

    if isinstance(explicit, str) and explicit.strip().lower() in {"image", "video"}:
        return explicit.strip().lower()
    selector = _first(value, "model_selection_id", "image_model_selection_id")
    return "video" if str(selector or "").startswith("wsl_") else "image"


def _safe_request_spec(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        value = {}
    allowed = {
        "prompt",
        "negative_prompt",
        "seed",
        "model_selection_id",
        "image_model_selection_id",
        "generation_settings",
        "size_preset_id",
        "width",
        "height",
        "accept_metered_generation",
        "aspect_ratio",
        "duration_seconds",
        "frame_count",
        "storyboard",
    }
    payload = {key: value[key] for key in allowed if key in value}
    selector = _first(value, "model_selection_id", "image_model_selection_id")
    if selector is not None:
        # The canonical key is always present in new responses.  Preserve the
        # image alias as well for old clients, but never expose it for a video
        # selector where the name would be misleading.
        payload["model_selection_id"] = selector
        if str(selector).startswith(("ims_", "imd_", "ien_")):
            payload["image_model_selection_id"] = selector
        else:
            payload.pop("image_model_selection_id", None)
    if str(selector or "").startswith("wsl_"):
        for key in ("size_preset_id", "width", "height", "aspect_ratio"):
            payload.pop(key, None)
    else:
        for key in ("duration_seconds", "frame_count", "storyboard"):
            payload.pop(key, None)
        if "size_preset_id" not in payload:
            payload["size_preset_id"] = "normal_square"
    payload["generation_settings"] = _safe_settings(payload.get("generation_settings"))
    if not isinstance(payload.get("storyboard"), list):
        payload["storyboard"] = []
    return payload


def _project_workspace(value: Any) -> dict[str, Any]:
    return {
        "id": str(_field(value, "id")),
        "owner_user_id": str(_field(value, "owner_user_id")),
        "project_id": _field(value, "project_id"),
        "provider": _field(value, "provider", "comfyui_workbench"),
        "external_workspace_id": _first(value, "external_workspace_id", "workspace_id"),
        "external_project_id": _first(value, "external_project_id", "project_ref"),
        "base_url": _safe_public_url(_field(value, "base_url"), origin_only=True),
        "status": _field(value, "status", "unavailable"),
        "config_hash": _field(value, "config_hash"),
        "created_by": _field(value, "created_by"),
        "created_at": _iso(_field(value, "created_at")),
        "updated_at": _iso(_field(value, "updated_at")),
    }


def _project_recipe_revision(value: Any) -> dict[str, Any]:
    selector = _first(value, "model_selection_id", "image_model_selection_id")
    image_selector = (
        selector
        if isinstance(selector, str)
        and selector.startswith(("ims_", "imd_", "ien_"))
        else None
    )
    return {
        "id": str(_field(value, "id")),
        "creative_recipe_id": str(
            _first(value, "creative_recipe_id", "recipe_id")
        ),
        "owner_user_id": str(_field(value, "owner_user_id")),
        "project_id": _field(value, "project_id"),
        "persona_id": _field(value, "persona_id"),
        "persona_revision_id": _field(value, "persona_revision_id"),
        "version": int(_field(value, "version", 1)),
        "recipe_type": _field(value, "recipe_type"),
        "model_selection_id": selector,
        "image_model_selection_id": image_selector,
        "workflow_id": _field(value, "workflow_id"),
        "identity_pack_id": _field(value, "identity_pack_id"),
        "prompt_schema_id": _field(value, "prompt_schema_id"),
        "prompt_template": _field(value, "prompt_template", ""),
        "negative_requirements": _field(value, "negative_requirements"),
        "reference_asset_ids": list(_field(value, "reference_asset_ids", []) or []),
        "aspect_ratio": _field(value, "aspect_ratio"),
        "width": _field(value, "width"),
        "height": _field(value, "height"),
        "duration_seconds": _field(value, "duration_seconds"),
        "frame_count": _field(value, "frame_count"),
        "storyboard": list(_field(value, "storyboard", []) or []),
        "candidate_count": int(_field(value, "candidate_count", 1)),
        "cost_policy": _safe_settings(_field(value, "cost_policy")),
        "provenance_retention_policy": _safe_settings(
            _field(value, "provenance_retention_policy")
        ),
        "human_review_policy": _safe_settings(
            _field(value, "human_review_policy")
        ),
        "content_hash": _field(value, "content_hash"),
        "created_by": _field(value, "created_by"),
        "created_at": _iso(_field(value, "created_at")),
    }


def _project_recipe(value: Any, *, detail: bool) -> dict[str, Any]:
    current = _project_recipe_revision(
        _field(value, "current_revision", {})
    )
    result = {
        "id": str(_field(value, "id")),
        "owner_user_id": str(_field(value, "owner_user_id")),
        "project_id": _field(value, "project_id"),
        "persona_id": str(_field(value, "persona_id")),
        "name": _field(value, "name", ""),
        "create_hash": _field(value, "create_hash"),
        "created_by": _field(value, "created_by"),
        "created_at": _iso(_field(value, "created_at")),
        "current_revision": current,
    }
    if detail:
        revisions = _field(value, "revisions", []) or []
        result["revisions"] = [_project_recipe_revision(item) for item in revisions[:100]]
        result["revision_history_truncated"] = bool(
            _field(value, "revision_history_truncated", False)
        )
    return result


def _project_plan(value: Any) -> dict[str, Any]:
    request_spec = _safe_request_spec(_field(value, "request_spec"))
    generation_kind = _generation_kind(
        request_spec,
        _field(value, "generation_kind"),
    )
    return {
        "id": str(_field(value, "id")),
        "owner_user_id": str(_field(value, "owner_user_id")),
        "project_id": _field(value, "project_id"),
        "persona_revision_id": str(_field(value, "persona_revision_id")),
        "persona_revision_hash": _field(value, "persona_revision_hash"),
        "creative_recipe_revision_id": str(
            _field(value, "creative_recipe_revision_id")
        ),
        "workspace_id": str(_field(value, "workspace_id")),
        "content_item_id": _field(value, "content_item_id"),
        "content_variant_id": _field(value, "content_variant_id"),
        "requested_outputs": int(_field(value, "requested_outputs", 1)),
        "request_spec": request_spec,
        "generation_kind": generation_kind,
        "plan_hash": _field(value, "plan_hash"),
        "status": _field(value, "status", "draft"),
        "created_by": _field(value, "created_by"),
        "created_at": _iso(_field(value, "created_at")),
        "updated_at": _iso(_field(value, "updated_at")),
    }


def _project_output(value: Any) -> dict[str, Any]:
    return {
        "id": str(_field(value, "id")),
        "external_asset_id": _first(value, "external_asset_id", "asset_id"),
        "external_output_version": _first(
            value, "external_output_version", "output_version"
        ),
        "sha256": _field(value, "sha256"),
        "mime_type": _field(value, "mime_type", "application/octet-stream"),
        "width": _field(value, "width"),
        "height": _field(value, "height"),
        "deep_link": _safe_public_url(_first(value, "deep_link", "result_deep_link")),
        "provenance_hash": _field(value, "provenance_hash"),
        "created_at": _iso(_field(value, "created_at")),
    }


def _project_catalog_item(value: Any) -> dict[str, Any] | None:
    selection = _first(
        value,
        "model_selection_id",
        "image_model_selection_id",
        "video_model_selection_id",
        "id",
    )
    selection_text = str(selection) if selection is not None else ""
    # Provider catalog rows are untrusted.  Drop malformed/opaque-ineligible
    # rows instead of letting response validation leak or stringify them.
    if not selection_text or re.fullmatch(_MODEL_SELECTION_RE, selection_text) is None:
        return None
    raw_kind = str(_field(value, "kind", "") or "").strip().lower()
    kind = raw_kind if raw_kind in {"image", "video"} else (
        "video" if selection_text.startswith("wsl_") else "image"
    )
    return {
        "kind": kind,
        "model_selection_id": selection,
        "image_model_selection_id": (
            selection
            if kind == "image"
            and selection_text.startswith(("ims_", "imd_", "ien_"))
            else None
        ),
        "display_name": _first(value, "display_name", "name", "label"),
        "status": _field(value, "status", "available"),
    }


def _project_run(value: Any) -> dict[str, Any]:
    outputs = _field(value, "outputs", []) or []
    return {
        "id": str(_field(value, "id")),
        "owner_user_id": str(_field(value, "owner_user_id")),
        "project_id": _field(value, "project_id"),
        "plan_id": str(_field(value, "plan_id")),
        "external_run_id": _field(value, "external_run_id"),
        "external_workspace_id": _field(value, "external_workspace_id"),
        "external_project_id": _field(value, "external_project_id"),
        "status": _field(value, "status", "unavailable"),
        "adapter_release": _field(value, "adapter_release"),
        "cost_summary": _safe_settings(_field(value, "cost_summary")),
        "error_code": _field(value, "error_code"),
        "result_deep_link": _safe_public_url(_field(value, "result_deep_link")),
        "outputs": [_project_output(item) for item in outputs[:20]],
        "run_hash": _field(value, "run_hash"),
        "created_by": _field(value, "created_by"),
        "created_at": _iso(_field(value, "created_at")),
        "started_at": _iso(_field(value, "started_at")),
        "finished_at": _iso(_field(value, "finished_at")),
        "updated_at": _iso(_field(value, "updated_at")),
    }


def _project_intent(value: Any) -> dict[str, Any]:
    return {
        "id": str(_field(value, "id")),
        "plan_id": str(_field(value, "plan_id")),
        "owner_user_id": _field(value, "owner_user_id"),
        "project_id": _field(value, "project_id"),
        "external_idempotency_key": _field(value, "external_idempotency_key", ""),
        "request_hash": _field(value, "request_hash"),
        "status": _field(value, "status", "pending"),
        "external_run_id": _field(value, "external_run_id"),
        "adapter_release": _field(value, "adapter_release"),
        "error_code": _field(value, "error_code"),
        "response_hash": _field(value, "response_hash"),
        "created_by": _field(value, "created_by"),
        "created_at": _iso(_field(value, "created_at")),
        "updated_at": _iso(_field(value, "updated_at")),
    }


def _project_submit(value: Any) -> dict[str, Any]:
    # Submit is an intent receipt, not a claim that a provider run succeeded.
    # The service returns a bounded envelope; project each member explicitly.
    if not isinstance(value, Mapping):
        value = {}
    outputs = _field(value, "outputs", []) or []
    run = _field(value, "run")
    intent = _field(value, "intent", {})
    status = _field(value, "status") or _field(intent, "status", "unavailable")
    external_key = _field(value, "external_idempotency_key") or _field(
        intent,
        "external_idempotency_key",
        "",
    )
    return {
        "plan": _project_plan(_field(value, "plan", {})),
        "intent": _project_intent(intent),
        "run": _project_run(run) if run is not None else None,
        "outputs": [_project_output(item) for item in outputs[:20]],
        "status": status,
        "external_idempotency_key": external_key,
    }


def _project_selection(value: Any) -> dict[str, Any]:
    return {
        "id": str(_field(value, "id")),
        "generation_run_id": str(_field(value, "generation_run_id")),
        "generation_output_id": str(_field(value, "generation_output_id")),
        "owner_user_id": str(_field(value, "owner_user_id")),
        "project_id": _field(value, "project_id"),
        "selection_hash": _field(value, "selection_hash"),
        "created_by": _field(value, "created_by"),
        "created_at": _iso(_field(value, "created_at")),
        "output": _project_output(_field(value, "output", {})),
    }


async def _maybe_await(value: Any) -> Any:
    return await value if isawaitable(value) else value


def _raise_generation_http_error(exc: Exception) -> None:
    if isinstance(exc, HTTPException):
        raise exc
    error_types: list[type[BaseException]] = [IntegrityError, PermissionError, ValueError]
    for name in (
        "MediaOperationsError",
        "MediaOperationsConflictError",
        "MediaOperationsNotFoundError",
        "MediaOperationsValidationError",
        "MediaOperationsUnavailableError",
        "GenerationStudioUnavailableError",
    ):
        candidate = getattr(service, name, None)
        if isinstance(candidate, type) and issubclass(candidate, BaseException):
            error_types.append(candidate)
    candidate = getattr(base_service, "MediaOperationsError", None)
    if isinstance(candidate, type) and issubclass(candidate, BaseException):
        error_types.append(candidate)
    if isinstance(exc, tuple(error_types)):
        if isinstance(exc, IntegrityError):
            raise HTTPException(status_code=409, detail="generation operation conflicts") from exc
        if isinstance(exc, PermissionError):
            raise HTTPException(status_code=403, detail=str(exc) or "access denied") from exc
        if exc.__class__.__name__.lower().endswith("unavailableerror"):
            raise HTTPException(status_code=503, detail="generation studio unavailable") from exc
        if isinstance(exc, ValueError) and not hasattr(exc, "status_code"):
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        status = int(getattr(exc, "status_code", 400) or 400)
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    raise exc


async def _invoke_generation(method: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        return await _maybe_await(method(*args, **kwargs))
    except Exception as exc:  # pragma: no cover - mapped branches are tested
        _raise_generation_http_error(exc)
        raise AssertionError("unreachable")


def _accepted_kwargs(method: Callable[..., Any], kwargs: Mapping[str, Any]) -> dict[str, Any]:
    """Drop compatibility-only fields during rolling service deployments."""

    try:
        parameters = signature(method).parameters
    except (TypeError, ValueError):
        return dict(kwargs)
    if any(
        parameter.kind == parameter.VAR_KEYWORD
        for parameter in parameters.values()
    ):
        return dict(kwargs)
    return {
        key: value
        for key, value in kwargs.items()
        if key in parameters
    }


async def _invoke_generation_compatible(
    method: Callable[..., Any], *args: Any, **kwargs: Any
) -> Any:
    return await _invoke_generation(
        method,
        *args,
        **_accepted_kwargs(method, kwargs),
    )


def _items(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    if isinstance(value, Mapping):
        if isinstance(value.get("items"), Sequence) and not isinstance(
            value.get("items"), (str, bytes, bytearray)
        ):
            return list(value["items"])
        # Legacy ``list_generation_catalog`` returned flattened selector ID
        # arrays rather than catalog items.  Normalize those arrays into the
        # generic safe projection here.
        for key in ("model_selection_ids", "image_model_selection_ids", "video_model_selection_ids"):
            ids = value.get(key)
            if isinstance(ids, Sequence) and not isinstance(ids, (str, bytes, bytearray)):
                return [
                    {
                        "model_selection_id": item,
                        "kind": "video" if key.startswith("video") else "image",
                        "status": value.get("status", "available"),
                    }
                    for item in ids
                ]
    return []


def _human_or_admin(actor: Mapping[str, Any]) -> bool:
    """Return whether a server-authenticated principal may approve billing.

    ``actor_type`` is projected by the authentication boundary.  Never trust a
    client body/header marker here; agent principals remain non-human even if a
    caller supplies an ``admin``-looking field.
    """

    if bool(actor.get("is_agent")):
        return False
    actor_type = str(actor.get("actor_type") or "").strip().lower()
    role = str(actor.get("role") or "").strip().lower()
    return actor_type == "human" or role in {"admin", "owner"}


def _reconcile_kwargs(
    method: Callable[..., Any],
    *,
    expected_plan_hash: str,
    expected_intent_request_hash: str,
    acknowledge_metered_generation: bool,
    idempotency_key: str,
) -> dict[str, Any]:
    """Build reconcile kwargs across Lane-A rolling service signatures."""

    kwargs: dict[str, Any] = {
        "expected_plan_hash": expected_plan_hash,
        "expected_intent_request_hash": expected_intent_request_hash,
        "acknowledge_metered_generation": acknowledge_metered_generation,
        "idempotency_key": idempotency_key,
    }
    # A short-lived compatibility spelling existed in early service drafts.
    # Add it only when the callable explicitly advertises that parameter; do
    # not duplicate an external call merely because a signature is opaque.
    try:
        names = signature(method).parameters
    except (TypeError, ValueError):
        names = {}
    if (
        "expected_intent_request_hash" not in names
        and "expected_intent_hash" in names
    ):
        kwargs["expected_intent_hash"] = kwargs.pop("expected_intent_request_hash")
    return _accepted_kwargs(method, kwargs)


def create_media_operations_generation_router(
    get_db_manager: Callable[[], Any],
    get_user_from_request: Callable[..., Any],
    require_auth_dependency: Callable[..., Any],
    *,
    service_instance: Any | None = None,
) -> APIRouter:
    """Create the authenticated Generation Studio router.

    ``service_instance`` is intentionally injectable for contract tests and
    for deployments that provide a configured adapter.  The production server
    uses the default ``MediaOperationsGenerationService`` singleton.
    """

    router = APIRouter(prefix="/api/operations/media", tags=["operations-media"])
    generation = service_instance or service.MediaOperationsGenerationService()

    async def current_actor(request: Request) -> dict[str, Any]:
        user = await _actor(get_user_from_request, request)
        return _principal_projection(user)

    @router.get(
        "/generation-workspaces",
        response_model=GenerationWorkspaceListResponse,
        operation_id="media_list_generation_workspaces",
    )
    async def list_generation_workspaces(
        request: Request,
        project_id: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        result = await _with_session(
            get_db_manager,
            lambda session: _invoke_generation(
                generation.list_generation_workspaces,
                session,
                actor,
                project_id=project_id,
                limit=limit,
                offset=offset,
            ),
        )
        return [_project_workspace(item) for item in _items(result)]

    @router.post(
        "/generation-workspaces",
        response_model=GenerationWorkspaceResponse,
        operation_id="media_create_generation_workspace",
    )
    async def create_generation_workspace(
        payload: GenerationWorkspaceCreateRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        result = await _with_session(
            get_db_manager,
            lambda session: _invoke_generation(
                generation.create_generation_workspace,
                session,
                actor,
                **payload.model_dump(mode="json"),
                idempotency_key=idempotency_key,
            ),
        )
        return _project_workspace(result)

    @router.get(
        "/generation-workspaces/{workspace_id}",
        response_model=GenerationWorkspaceResponse,
        operation_id="media_get_generation_workspace",
    )
    async def get_generation_workspace(
        workspace_id: str,
        request: Request,
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        result = await _with_session(
            get_db_manager,
            lambda session: _invoke_generation(
                generation.get_generation_workspace, session, actor, workspace_id
            ),
        )
        return _project_workspace(result)

    @router.get(
        "/generation-workspaces/{workspace_id}/catalog",
        response_model=GenerationCatalogResponse,
        operation_id="media_get_generation_catalog",
    )
    async def get_generation_catalog(
        workspace_id: str,
        request: Request,
        kind: Literal["image", "video"] = Query(default="image"),
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        """Read a bounded catalog through the configured adapter only.

        Catalog support is optional in WS4.  Returning 503 when the adapter
        does not implement it is intentional and avoids exposing a local
        provider filesystem or pretending that an unavailable Studio is live.
        """

        actor = await current_actor(request)
        # Prefer the generic Lane-A service method.  Keep image/video-specific
        # spellings for rolling deployments and test doubles.
        method = getattr(generation, "list_generation_catalog", None)
        if not callable(method):
            method = (
                getattr(generation, "list_image_catalog", None)
                if kind == "image"
                else getattr(generation, "list_video_catalog", None)
            )
        if not callable(method) and kind == "video":
            # Some adapters expose a single image method with a ``kind``
            # keyword while they are being upgraded to video support.
            method = getattr(generation, "list_image_catalog", None)
        if not callable(method):
            raise HTTPException(
                status_code=503,
                detail="generation catalog unavailable",
            )
        result = await _with_session(
            get_db_manager,
            lambda session: _invoke_generation_compatible(
                method,
                session,
                actor,
                workspace_id=workspace_id,
                kind=kind,
            ),
        )
        projected = [
            item
            for raw_item in _items(result)
            if (item := _project_catalog_item(raw_item)) is not None
        ]
        # A generic catalog method may return both kinds; query filtering is a
        # contract boundary, not a hint to the provider.
        return [item for item in projected if item["kind"] == kind]

    @router.get(
        "/creative-recipes",
        response_model=CreativeRecipeListResponse,
        operation_id="media_list_creative_recipes",
    )
    async def list_creative_recipes(
        request: Request,
        project_id: str | None = Query(default=None),
        persona_id: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        result = await _with_session(
            get_db_manager,
            lambda session: _invoke_generation(
                generation.list_creative_recipes,
                session,
                actor,
                project_id=project_id,
                persona_id=persona_id,
                limit=limit,
                offset=offset,
            ),
        )
        return [_project_recipe(item, detail=False) for item in _items(result)]

    @router.post(
        "/creative-recipes",
        response_model=CreativeRecipeDetailResponse,
        operation_id="media_create_creative_recipe",
    )
    async def create_creative_recipe(
        payload: CreativeRecipeCreateRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        result = await _with_session(
            get_db_manager,
            lambda session: _invoke_generation_compatible(
                generation.create_creative_recipe,
                session,
                actor,
                **payload.model_dump(mode="json"),
                idempotency_key=idempotency_key,
            ),
        )
        return _project_recipe(result, detail=True)

    @router.get(
        "/creative-recipes/{recipe_id}",
        response_model=CreativeRecipeDetailResponse,
        operation_id="media_get_creative_recipe",
    )
    async def get_creative_recipe(
        recipe_id: str,
        request: Request,
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        result = await _with_session(
            get_db_manager,
            lambda session: _invoke_generation(
                generation.get_creative_recipe, session, actor, recipe_id
            ),
        )
        return _project_recipe(result, detail=True)

    @router.post(
        "/creative-recipes/{recipe_id}/revisions",
        response_model=CreativeRecipeRevisionResponse,
        operation_id="media_append_creative_recipe_revision",
    )
    async def append_creative_recipe_revision(
        recipe_id: str,
        payload: CreativeRecipeRevisionCreateRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        data = payload.model_dump(mode="json")
        expected_version = data.pop("expected_version")
        result = await _with_session(
            get_db_manager,
            lambda session: _invoke_generation(
                generation.append_creative_recipe_revision,
                session,
                actor,
                recipe_id,
                expected_version=expected_version,
                **data,
                idempotency_key=idempotency_key,
            ),
        )
        return _project_recipe_revision(result)

    @router.get(
        "/generation-plans",
        response_model=GenerationPlanListResponse,
        operation_id="media_list_generation_plans",
    )
    async def list_generation_plans(
        request: Request,
        project_id: str | None = Query(default=None),
        workspace_id: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        result = await _with_session(
            get_db_manager,
            lambda session: _invoke_generation(
                generation.list_generation_plans,
                session,
                actor,
                project_id=project_id,
                workspace_id=workspace_id,
                limit=limit,
                offset=offset,
            ),
        )
        return [_project_plan(item) for item in _items(result)]

    @router.post(
        "/generation-plans",
        response_model=GenerationPlanResponse,
        operation_id="media_create_generation_plan",
    )
    async def create_generation_plan(
        payload: GenerationPlanCreateRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        result = await _with_session(
            get_db_manager,
            lambda session: _invoke_generation(
                generation.create_generation_plan,
                session,
                actor,
                **payload.model_dump(mode="json"),
                idempotency_key=idempotency_key,
            ),
        )
        return _project_plan(result)

    @router.get(
        "/generation-plans/{plan_id}",
        response_model=GenerationPlanResponse,
        operation_id="media_get_generation_plan",
    )
    async def get_generation_plan(
        plan_id: str,
        request: Request,
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        result = await _with_session(
            get_db_manager,
            lambda session: _invoke_generation(
                generation.get_generation_plan, session, actor, plan_id
            ),
        )
        return _project_plan(result)

    @router.post(
        "/generation-plans/{plan_id}/submit",
        response_model=GenerationSubmitResponse,
        operation_id="media_submit_generation_plan",
    )
    async def submit_generation_plan(
        plan_id: str,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
        payload: GenerationSubmitRequest,
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        # Submission crosses the provider execution boundary.  Agents may
        # prepare a plan, but only a server-authenticated human/admin may
        # execute it; metered requests additionally require the explicit ack.
        if not _human_or_admin(actor):
            raise HTTPException(
                status_code=403,
                detail="generation submission requires a human or admin",
            )
        result = await _with_session(
            get_db_manager,
            lambda session: _invoke_generation_compatible(
                generation.submit_generation_plan,
                session,
                actor,
                plan_id,
                **payload.model_dump(mode="json"),
                idempotency_key=idempotency_key,
            ),
        )
        return _project_submit(result)

    @router.post(
        "/generation-plans/{plan_id}/reconcile",
        response_model=GenerationSubmitResponse,
        operation_id="media_reconcile_generation_plan",
    )
    async def reconcile_generation_plan(
        plan_id: str,
        payload: GenerationReconcileRequest,
        request: Request,
        idempotency_key: Annotated[
            str,
            Header(alias="Idempotency-Key", min_length=1, max_length=255),
        ],
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        """Reconcile an existing receipt without issuing another submit.

        The operation intentionally has its own idempotency key and both plan
        and intent request hashes.  A missing service capability is reported
        as explicit ``503`` unavailable rather than falling back to submit.
        """

        actor = await current_actor(request)
        if not _human_or_admin(actor):
            raise HTTPException(
                status_code=403,
                detail="generation reconciliation requires a human or admin",
            )
        method = getattr(generation, "reconcile_generation_plan", None)
        if not callable(method):
            method = getattr(generation, "reconcile_generation_run", None)
        if not callable(method):
            raise HTTPException(
                status_code=503,
                detail="generation reconciliation unavailable",
            )
        kwargs = _reconcile_kwargs(
            method,
            expected_plan_hash=payload.expected_plan_hash,
            expected_intent_request_hash=payload.expected_intent_request_hash,
            acknowledge_metered_generation=payload.acknowledge_metered_generation,
            idempotency_key=idempotency_key,
        )
        result = await _with_session(
            get_db_manager,
            lambda session: _invoke_generation(
                method,
                session,
                actor,
                plan_id,
                **kwargs,
            ),
        )
        return _project_submit(result)

    @router.get(
        "/generation-runs",
        response_model=GenerationRunListResponse,
        operation_id="media_list_generation_runs",
    )
    async def list_generation_runs(
        request: Request,
        project_id: str | None = Query(default=None),
        plan_id: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        result = await _with_session(
            get_db_manager,
            lambda session: _invoke_generation(
                generation.list_generation_runs,
                session,
                actor,
                project_id=project_id,
                plan_id=plan_id,
                limit=limit,
                offset=offset,
            ),
        )
        return [_project_run(item) for item in _items(result)]

    @router.get(
        "/generation-runs/{run_id}",
        response_model=GenerationRunResponse,
        operation_id="media_get_generation_run",
    )
    async def get_generation_run(
        run_id: str,
        request: Request,
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        result = await _with_session(
            get_db_manager,
            lambda session: _invoke_generation(
                generation.get_generation_run, session, actor, run_id
            ),
        )
        return _project_run(result)

    @router.post(
        "/generation-runs/{run_id}/refresh",
        response_model=GenerationRunResponse,
        operation_id="media_refresh_generation_run",
    )
    async def refresh_generation_run(
        run_id: str,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        result = await _with_session(
            get_db_manager,
            lambda session: _invoke_generation_compatible(
                generation.refresh_generation_run,
                session,
                actor,
                run_id,
                idempotency_key=idempotency_key,
            ),
        )
        return _project_run(result)

    @router.post(
        "/generation-runs/{run_id}/outputs/select",
        response_model=GenerationOutputSelectionResponse,
        operation_id="media_select_generation_output_body",
    )
    async def select_generation_output(
        run_id: str,
        payload: GenerationOutputSelectionRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        result = await _with_session(
            get_db_manager,
            lambda session: _invoke_generation(
                generation.select_generation_output,
                session,
                actor,
                run_id,
                payload.output_id,
                idempotency_key=idempotency_key,
            ),
        )
        return _project_selection(result)

    @router.post(
        "/generation-runs/{run_id}/outputs/{output_id}/select",
        response_model=GenerationOutputSelectionResponse,
        operation_id="media_select_generation_output",
    )
    async def select_generation_output_by_path(
        run_id: str,
        output_id: str,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
        _: Any = Depends(require_auth_dependency),
    ) -> Any:
        actor = await current_actor(request)
        result = await _with_session(
            get_db_manager,
            lambda session: _invoke_generation(
                generation.select_generation_output,
                session,
                actor,
                run_id,
                output_id,
                idempotency_key=idempotency_key,
            ),
        )
        return _project_selection(result)

    return router
