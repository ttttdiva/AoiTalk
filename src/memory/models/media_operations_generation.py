"""MediaOps WS4 Generation Studio persistence.

The models in this module deliberately store only the semantic generation
request and the opaque receipt returned by Generation Studio.  Provider
workflow graphs, credentials, local paths, and raw provider responses are not
part of the persistence contract.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Dict

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID

from .base import Base


class GenerationWorkspaceStatus(str, Enum):
    CONFIGURED = "configured"
    UNAVAILABLE = "unavailable"
    VERIFIED = "verified"


GENERATION_WORKSPACE_STATUS_VALUES = tuple(
    value.value for value in GenerationWorkspaceStatus
)


class CreativeRecipeType(str, Enum):
    IMAGE = "image"
    IMAGE_SET = "image_set"
    COMIC = "comic"
    VIDEO = "video"
    THUMBNAIL = "thumbnail"


CREATIVE_RECIPE_TYPE_VALUES = tuple(value.value for value in CreativeRecipeType)


class GenerationPlanStatus(str, Enum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    UNAVAILABLE = "unavailable"


GENERATION_PLAN_STATUS_VALUES = tuple(value.value for value in GenerationPlanStatus)


class GenerationRunIntentStatus(str, Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    UNAVAILABLE = "unavailable"
    UNCERTAIN = "uncertain"
    FAILED = "failed"


GENERATION_RUN_INTENT_STATUS_VALUES = tuple(
    value.value for value in GenerationRunIntentStatus
)


class GenerationRunStatus(str, Enum):
    DRAFT = "draft"
    QUEUED = "queued"
    CLAIMED = "claimed"
    RUNNING = "running"
    OUTPUT_PENDING = "output_pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    QUARANTINED = "quarantined"
    UNCERTAIN = "uncertain"
    UNAVAILABLE = "unavailable"


GENERATION_RUN_STATUS_VALUES = tuple(value.value for value in GenerationRunStatus)


def _uuid(value: Any) -> str | None:
    return str(value) if value is not None else None


def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


class GenerationWorkspace(Base):
    """Stable, safe reference to one Generation Studio workspace."""

    __tablename__ = "media_generation_workspaces"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    provider = Column(
        String(64),
        nullable=False,
        default="comfyui_workbench",
        server_default="comfyui_workbench",
    )
    external_workspace_id = Column(String(164), nullable=False, index=True)
    external_project_id = Column(String(164), nullable=True, index=True)
    # This is origin-only (scheme + host + optional port).  No credentials,
    # query, fragment, or filesystem path is persisted.
    base_url = Column(String(512), nullable=False)
    status = Column(
        String(16),
        nullable=False,
        default=GenerationWorkspaceStatus.CONFIGURED.value,
        server_default=GenerationWorkspaceStatus.CONFIGURED.value,
        index=True,
    )
    config_hash = Column(String(64), nullable=False, index=True)
    idempotency_key = Column(String(255), nullable=False)
    created_by = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    __table_args__ = (
        CheckConstraint(
            "provider = 'comfyui_workbench'",
            name="ck_media_generation_workspaces_provider",
        ),
        CheckConstraint(
            "external_workspace_id LIKE 'wsp_%'",
            name="ck_media_generation_workspaces_workspace_ref",
        ),
        CheckConstraint(
            "external_project_id IS NULL OR external_project_id LIKE 'prj_%'",
            name="ck_media_generation_workspaces_project_ref",
        ),
        CheckConstraint(
            "status IN ('configured', 'unavailable', 'verified')",
            name="ck_media_generation_workspaces_status",
        ),
        CheckConstraint(
            "length(config_hash) = 64",
            name="ck_media_generation_workspaces_hash",
        ),
        Index(
            "ix_media_generation_workspaces_owner_project",
            "owner_user_id",
            "project_id",
        ),
        Index(
            "uq_media_generation_workspaces_personal_identity",
            "owner_user_id",
            "external_workspace_id",
            unique=True,
            postgresql_where=text("project_id IS NULL"),
            sqlite_where=text("project_id IS NULL"),
        ),
        Index(
            "uq_media_generation_workspaces_project_identity",
            "project_id",
            "external_workspace_id",
            unique=True,
            postgresql_where=text("project_id IS NOT NULL"),
            sqlite_where=text("project_id IS NOT NULL"),
        ),
        Index(
            "uq_media_generation_workspaces_personal_idempotency",
            "owner_user_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("project_id IS NULL"),
            sqlite_where=text("project_id IS NULL"),
        ),
        Index(
            "uq_media_generation_workspaces_project_idempotency",
            "project_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("project_id IS NOT NULL"),
            sqlite_where=text("project_id IS NOT NULL"),
        ),
    )

    @property
    def owner_id(self):
        return self.owner_user_id

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "id": _uuid(self.id),
            "owner_user_id": _uuid(self.owner_user_id),
            "project_id": _uuid(self.project_id),
            "provider": self.provider,
            "external_workspace_id": self.external_workspace_id,
            "external_project_id": self.external_project_id,
            "base_url": self.base_url,
            "status": self.status,
            "config_hash": self.config_hash,
            "created_by": _uuid(self.created_by),
            "created_at": _dt(self.created_at),
        }

    to_dict = to_safe_dict


class CreativeRecipe(Base):
    """Stable recipe identity associated with one Persona."""

    __tablename__ = "media_creative_recipes"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    persona_id = Column(
        UUID(as_uuid=True),
        ForeignKey("media_personas.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name = Column(String(255), nullable=False, default="Untitled recipe", server_default="Untitled recipe")
    create_hash = Column(String(64), nullable=False, index=True)
    idempotency_key = Column(String(255), nullable=False)
    created_by = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    __table_args__ = (
        CheckConstraint(
            "length(create_hash) = 64",
            name="ck_media_creative_recipes_create_hash",
        ),
        Index(
            "ix_media_creative_recipes_owner_project",
            "owner_user_id",
            "project_id",
        ),
        Index(
            "uq_media_creative_recipes_personal_idempotency",
            "owner_user_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("project_id IS NULL"),
            sqlite_where=text("project_id IS NULL"),
        ),
        Index(
            "uq_media_creative_recipes_project_idempotency",
            "project_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("project_id IS NOT NULL"),
            sqlite_where=text("project_id IS NOT NULL"),
        ),
    )

    @property
    def owner_id(self):
        return self.owner_user_id

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "id": _uuid(self.id),
            "owner_user_id": _uuid(self.owner_user_id),
            "project_id": _uuid(self.project_id),
            "persona_id": _uuid(self.persona_id),
            "name": self.name,
            "create_hash": self.create_hash,
            "created_by": _uuid(self.created_by),
            "created_at": _dt(self.created_at),
        }

    to_dict = to_safe_dict


class CreativeRecipeRevision(Base):
    """Immutable semantic recipe revision."""

    __tablename__ = "media_creative_recipe_revisions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    creative_recipe_id = Column(
        UUID(as_uuid=True),
        ForeignKey("media_creative_recipes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    owner_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    persona_id = Column(
        UUID(as_uuid=True),
        ForeignKey("media_personas.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    persona_revision_id = Column(
        UUID(as_uuid=True),
        ForeignKey("media_persona_revisions.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    version = Column(Integer, nullable=False)
    recipe_type = Column(String(24), nullable=False, index=True)
    workflow_id = Column(String(255), nullable=True)
    model_selection_id = Column(String(255), nullable=True)
    identity_pack_id = Column(String(255), nullable=True)
    prompt_schema_id = Column(String(255), nullable=True)
    prompt_template = Column(Text, nullable=False)
    negative_requirements = Column(Text, nullable=True)
    reference_asset_ids = Column(JSON, nullable=False, default=list, server_default="[]")
    aspect_ratio = Column(String(32), nullable=True)
    width = Column(Integer, nullable=True)
    height = Column(Integer, nullable=True)
    duration_seconds = Column(Float, nullable=True)
    frame_count = Column(Integer, nullable=True)
    storyboard_json = Column(JSON, nullable=True)
    candidate_count = Column(Integer, nullable=False, default=1, server_default="1")
    cost_policy = Column(JSON, nullable=False, default=dict, server_default="{}")
    provenance_retention_policy = Column(
        JSON,
        nullable=False,
        default=dict,
        server_default="{}",
    )
    human_review_policy = Column(JSON, nullable=False, default=dict, server_default="{}")
    content_hash = Column(String(64), nullable=False, index=True)
    idempotency_key = Column(String(255), nullable=True)
    created_by = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    __table_args__ = (
        CheckConstraint(
            "version > 0",
            name="ck_media_creative_recipe_revisions_version",
        ),
        CheckConstraint(
            "recipe_type IN ('image', 'image_set', 'comic', 'video', 'thumbnail')",
            name="ck_media_creative_recipe_revisions_type",
        ),
        CheckConstraint(
            "width IS NULL OR (width > 0 AND width <= 16384)",
            name="ck_media_creative_recipe_revisions_width",
        ),
        CheckConstraint(
            "height IS NULL OR (height > 0 AND height <= 16384)",
            name="ck_media_creative_recipe_revisions_height",
        ),
        CheckConstraint(
            "duration_seconds IS NULL OR (duration_seconds > 0 AND duration_seconds <= 86400)",
            name="ck_media_creative_recipe_revisions_duration",
        ),
        CheckConstraint(
            "frame_count IS NULL OR (frame_count > 0 AND frame_count <= 1000000)",
            name="ck_media_creative_recipe_revisions_frames",
        ),
        CheckConstraint(
            "candidate_count >= 1 AND candidate_count <= 20",
            name="ck_media_creative_recipe_revisions_candidates",
        ),
        CheckConstraint(
            "length(content_hash) = 64",
            name="ck_media_creative_recipe_revisions_hash",
        ),
        UniqueConstraint(
            "creative_recipe_id",
            "version",
            name="uq_media_creative_recipe_revisions_version",
        ),
        UniqueConstraint(
            "creative_recipe_id",
            "idempotency_key",
            name="uq_media_creative_recipe_revisions_idempotency",
        ),
        Index(
            "ix_media_creative_recipe_revisions_owner_project",
            "owner_user_id",
            "project_id",
        ),
    )

    @property
    def owner_id(self):
        return self.owner_user_id

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "id": _uuid(self.id),
            "creative_recipe_id": _uuid(self.creative_recipe_id),
            "owner_user_id": _uuid(self.owner_user_id),
            "project_id": _uuid(self.project_id),
            "persona_id": _uuid(self.persona_id),
            "persona_revision_id": _uuid(self.persona_revision_id),
            "version": int(self.version),
            "recipe_type": self.recipe_type,
            "workflow_id": self.workflow_id,
            "model_selection_id": self.model_selection_id,
            "identity_pack_id": self.identity_pack_id,
            "prompt_schema_id": self.prompt_schema_id,
            "prompt_template": self.prompt_template,
            "negative_requirements": self.negative_requirements,
            "reference_asset_ids": list(self.reference_asset_ids or []),
            "aspect_ratio": self.aspect_ratio,
            "width": self.width,
            "height": self.height,
            "duration_seconds": self.duration_seconds,
            "frame_count": self.frame_count,
            "storyboard": self.storyboard_json,
            "candidate_count": int(self.candidate_count),
            "cost_policy": self.cost_policy or {},
            "provenance_retention_policy": self.provenance_retention_policy or {},
            "human_review_policy": self.human_review_policy or {},
            "content_hash": self.content_hash,
            "created_by": _uuid(self.created_by),
            "created_at": _dt(self.created_at),
        }

    to_dict = to_safe_dict


class GenerationPlan(Base):
    """Immutable semantic generation intent (status is lifecycle metadata)."""

    __tablename__ = "media_generation_plans"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    persona_revision_id = Column(
        UUID(as_uuid=True),
        ForeignKey("media_persona_revisions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    persona_revision_hash = Column(String(64), nullable=False)
    content_item_id = Column(
        UUID(as_uuid=True),
        ForeignKey("media_content_items.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # ContentVariant is intentionally not a WS4 table yet; this opaque UUID
    # allows a later variant slice to bind without storing provider state.
    content_variant_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    creative_recipe_revision_id = Column(
        UUID(as_uuid=True),
        ForeignKey("media_creative_recipe_revisions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    workspace_id = Column(
        UUID(as_uuid=True),
        ForeignKey("media_generation_workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    requested_outputs = Column(Integer, nullable=False, default=1, server_default="1")
    request_spec = Column(JSON, nullable=False)
    idempotency_key = Column(String(255), nullable=False)
    plan_hash = Column(String(64), nullable=False, index=True)
    status = Column(
        String(16),
        nullable=False,
        default=GenerationPlanStatus.DRAFT.value,
        server_default=GenerationPlanStatus.DRAFT.value,
        index=True,
    )
    created_by = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    __table_args__ = (
        CheckConstraint(
            "length(persona_revision_hash) = 64",
            name="ck_media_generation_plans_persona_hash",
        ),
        CheckConstraint(
            "requested_outputs >= 1 AND requested_outputs <= 20",
            name="ck_media_generation_plans_requested_outputs",
        ),
        CheckConstraint(
            "length(plan_hash) = 64",
            name="ck_media_generation_plans_hash",
        ),
        CheckConstraint(
            "status IN ('draft', 'submitted', 'unavailable')",
            name="ck_media_generation_plans_status",
        ),
        Index(
            "ix_media_generation_plans_owner_project",
            "owner_user_id",
            "project_id",
        ),
        Index(
            "uq_media_generation_plans_personal_idempotency",
            "owner_user_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("project_id IS NULL"),
            sqlite_where=text("project_id IS NULL"),
        ),
        Index(
            "uq_media_generation_plans_project_idempotency",
            "project_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("project_id IS NOT NULL"),
            sqlite_where=text("project_id IS NOT NULL"),
        ),
    )

    @property
    def owner_id(self):
        return self.owner_user_id

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "id": _uuid(self.id),
            "owner_user_id": _uuid(self.owner_user_id),
            "project_id": _uuid(self.project_id),
            "persona_revision_id": _uuid(self.persona_revision_id),
            "persona_revision_hash": self.persona_revision_hash,
            "content_item_id": _uuid(self.content_item_id),
            "content_variant_id": _uuid(self.content_variant_id),
            "creative_recipe_revision_id": _uuid(self.creative_recipe_revision_id),
            "workspace_id": _uuid(self.workspace_id),
            "requested_outputs": int(self.requested_outputs),
            "request_spec": self.request_spec or {},
            "idempotency_key": self.idempotency_key,
            "plan_hash": self.plan_hash,
            "status": self.status,
            "created_by": _uuid(self.created_by),
            "created_at": _dt(self.created_at),
        }

    to_dict = to_safe_dict


class GenerationRunIntent(Base):
    """Durable pre-submit intent used to fence duplicate external calls."""

    __tablename__ = "media_generation_run_intents"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    plan_id = Column(
        UUID(as_uuid=True),
        ForeignKey("media_generation_plans.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    owner_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    external_idempotency_key = Column(String(64), nullable=False, unique=True, index=True)
    request_hash = Column(String(64), nullable=False, index=True)
    status = Column(String(16), nullable=False, default="pending", server_default="pending")
    external_run_id = Column(String(164), nullable=True)
    adapter_release = Column(String(128), nullable=True)
    error_code = Column(String(128), nullable=True)
    response_hash = Column(String(64), nullable=True, index=True)
    created_by = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        CheckConstraint(
            "length(external_idempotency_key) = 64",
            name="ck_media_generation_run_intents_idempotency",
        ),
        CheckConstraint(
            "length(request_hash) = 64",
            name="ck_media_generation_run_intents_request_hash",
        ),
        CheckConstraint(
            "status IN ('pending', 'submitted', 'unavailable', 'uncertain', 'failed')",
            name="ck_media_generation_run_intents_status",
        ),
        CheckConstraint(
            "response_hash IS NULL OR length(response_hash) = 64",
            name="ck_media_generation_run_intents_response_hash",
        ),
        Index(
            "ix_media_generation_run_intents_owner_project",
            "owner_user_id",
            "project_id",
        ),
    )

    @property
    def owner_id(self):
        return self.owner_user_id

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "id": _uuid(self.id),
            "plan_id": _uuid(self.plan_id),
            "owner_user_id": _uuid(self.owner_user_id),
            "project_id": _uuid(self.project_id),
            "external_idempotency_key": self.external_idempotency_key,
            "request_hash": self.request_hash,
            "status": self.status,
            "external_run_id": self.external_run_id,
            "adapter_release": self.adapter_release,
            "error_code": self.error_code,
            "response_hash": self.response_hash,
            "created_by": _uuid(self.created_by),
            "created_at": _dt(self.created_at),
            "updated_at": _dt(self.updated_at),
        }

    to_dict = to_safe_dict


class GenerationRun(Base):
    """Immutable receipt for an external Generation Studio run."""

    __tablename__ = "media_generation_runs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    plan_id = Column(
        UUID(as_uuid=True),
        ForeignKey("media_generation_plans.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    intent_id = Column(
        UUID(as_uuid=True),
        ForeignKey("media_generation_run_intents.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    owner_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    workspace_id = Column(
        UUID(as_uuid=True),
        ForeignKey("media_generation_workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    external_run_id = Column(String(164), nullable=False, index=True)
    external_workspace_id = Column(String(164), nullable=False)
    external_project_id = Column(String(164), nullable=False)
    status = Column(String(16), nullable=False, index=True)
    run_hash = Column(String(64), nullable=False, index=True)
    adapter_release = Column(String(128), nullable=True)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    cost_summary = Column(JSON, nullable=False, default=dict, server_default="{}")
    error_code = Column(String(128), nullable=True)
    result_deep_link = Column(String(2000), nullable=True)
    created_by = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    __table_args__ = (
        CheckConstraint(
            "external_run_id LIKE 'run_%'",
            name="ck_media_generation_runs_run_ref",
        ),
        CheckConstraint(
            "external_workspace_id LIKE 'wsp_%'",
            name="ck_media_generation_runs_workspace_ref",
        ),
        CheckConstraint(
            "external_project_id LIKE 'prj_%'",
            name="ck_media_generation_runs_project_ref",
        ),
        CheckConstraint(
            "status IN ('draft', 'queued', 'claimed', 'running', 'output_pending', 'succeeded', 'failed', 'cancelled', 'quarantined', 'uncertain', 'unavailable')",
            name="ck_media_generation_runs_status",
        ),
        CheckConstraint(
            "length(run_hash) = 64",
            name="ck_media_generation_runs_hash",
        ),
        UniqueConstraint(
            "workspace_id",
            "external_run_id",
            name="uq_media_generation_runs_workspace_external",
        ),
        Index(
            "ix_media_generation_runs_owner_project",
            "owner_user_id",
            "project_id",
        ),
    )

    @property
    def owner_id(self):
        return self.owner_user_id

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "id": _uuid(self.id),
            "plan_id": _uuid(self.plan_id),
            "intent_id": _uuid(self.intent_id),
            "owner_user_id": _uuid(self.owner_user_id),
            "project_id": _uuid(self.project_id),
            "workspace_id": _uuid(self.workspace_id),
            "external_run_id": self.external_run_id,
            "external_workspace_id": self.external_workspace_id,
            "external_project_id": self.external_project_id,
            "status": self.status,
            "run_hash": self.run_hash,
            "adapter_release": self.adapter_release,
            "started_at": _dt(self.started_at),
            "finished_at": _dt(self.finished_at),
            "cost_summary": self.cost_summary or {},
            "error_code": self.error_code,
            "result_deep_link": self.result_deep_link,
            "created_by": _uuid(self.created_by),
            "created_at": _dt(self.created_at),
        }

    to_dict = to_safe_dict


class GenerationRunObservation(Base):
    """Append-only normalized observation of a run."""

    __tablename__ = "media_generation_run_observations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    generation_run_id = Column(
        UUID(as_uuid=True),
        ForeignKey("media_generation_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    owner_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    external_run_id = Column(String(164), nullable=False)
    status = Column(String(16), nullable=False)
    observation_hash = Column(String(64), nullable=False, index=True)
    adapter_release = Column(String(128), nullable=True)
    cost_summary = Column(JSON, nullable=False, default=dict, server_default="{}")
    error_code = Column(String(128), nullable=True)
    result_deep_link = Column(String(2000), nullable=True)
    observed_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    __table_args__ = (
        CheckConstraint(
            "external_run_id LIKE 'run_%'",
            name="ck_media_generation_run_observations_run_ref",
        ),
        CheckConstraint(
            "status IN ('draft', 'queued', 'claimed', 'running', 'output_pending', 'succeeded', 'failed', 'cancelled', 'quarantined', 'uncertain', 'unavailable')",
            name="ck_media_generation_run_observations_status",
        ),
        CheckConstraint(
            "length(observation_hash) = 64",
            name="ck_media_generation_run_observations_hash",
        ),
        UniqueConstraint(
            "generation_run_id",
            "observation_hash",
            name="uq_media_generation_run_observations_hash",
        ),
        Index(
            "ix_media_generation_run_observations_owner_project",
            "owner_user_id",
            "project_id",
        ),
    )

    @property
    def owner_id(self):
        return self.owner_user_id

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "id": _uuid(self.id),
            "generation_run_id": _uuid(self.generation_run_id),
            "owner_user_id": _uuid(self.owner_user_id),
            "project_id": _uuid(self.project_id),
            "external_run_id": self.external_run_id,
            "status": self.status,
            "observation_hash": self.observation_hash,
            "adapter_release": self.adapter_release,
            "cost_summary": self.cost_summary or {},
            "error_code": self.error_code,
            "result_deep_link": self.result_deep_link,
            "observed_at": _dt(self.observed_at),
        }

    to_dict = to_safe_dict


class GenerationOutput(Base):
    """Immutable opaque output reference returned by a Generation run."""

    __tablename__ = "media_generation_outputs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    generation_run_id = Column(
        UUID(as_uuid=True),
        ForeignKey("media_generation_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    owner_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    external_asset_id = Column(String(164), nullable=False)
    external_output_version = Column(String(165), nullable=False)
    sha256 = Column(String(64), nullable=False, index=True)
    mime_type = Column(String(255), nullable=False)
    width = Column(Integer, nullable=True)
    height = Column(Integer, nullable=True)
    deep_link = Column(String(2000), nullable=True)
    provenance_hash = Column(String(64), nullable=False, index=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    __table_args__ = (
        CheckConstraint(
            "external_asset_id LIKE 'ast_%'",
            name="ck_media_generation_outputs_asset_ref",
        ),
        CheckConstraint(
            "external_output_version LIKE 'outv_%'",
            name="ck_media_generation_outputs_version_ref",
        ),
        CheckConstraint(
            "length(sha256) = 64",
            name="ck_media_generation_outputs_sha256",
        ),
        CheckConstraint(
            "width IS NULL OR (width > 0 AND width <= 16384)",
            name="ck_media_generation_outputs_width",
        ),
        CheckConstraint(
            "height IS NULL OR (height > 0 AND height <= 16384)",
            name="ck_media_generation_outputs_height",
        ),
        CheckConstraint(
            "length(provenance_hash) = 64",
            name="ck_media_generation_outputs_provenance_hash",
        ),
        UniqueConstraint(
            "generation_run_id",
            "external_asset_id",
            "external_output_version",
            name="uq_media_generation_outputs_external",
        ),
        Index(
            "ix_media_generation_outputs_owner_project",
            "owner_user_id",
            "project_id",
        ),
    )

    @property
    def owner_id(self):
        return self.owner_user_id

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "id": _uuid(self.id),
            "generation_run_id": _uuid(self.generation_run_id),
            "owner_user_id": _uuid(self.owner_user_id),
            "project_id": _uuid(self.project_id),
            "external_asset_id": self.external_asset_id,
            "external_output_version": self.external_output_version,
            "sha256": self.sha256,
            "mime_type": self.mime_type,
            "width": self.width,
            "height": self.height,
            "deep_link": self.deep_link,
            "provenance_hash": self.provenance_hash,
            "created_at": _dt(self.created_at),
        }

    to_dict = to_safe_dict


class GenerationOutputSelection(Base):
    """Immutable human selection event; output rows are never overwritten."""

    __tablename__ = "media_generation_output_selections"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    generation_run_id = Column(
        UUID(as_uuid=True),
        ForeignKey("media_generation_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    generation_output_id = Column(
        UUID(as_uuid=True),
        ForeignKey("media_generation_outputs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    owner_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    selection_hash = Column(String(64), nullable=False, index=True)
    idempotency_key = Column(String(255), nullable=False)
    created_by = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    __table_args__ = (
        CheckConstraint(
            "length(selection_hash) = 64",
            name="ck_media_generation_output_selections_hash",
        ),
        UniqueConstraint(
            "generation_run_id",
            "idempotency_key",
            name="uq_media_generation_output_selections_idempotency",
        ),
        Index(
            "ix_media_generation_output_selections_owner_project",
            "owner_user_id",
            "project_id",
        ),
    )

    @property
    def owner_id(self):
        return self.owner_user_id

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "id": _uuid(self.id),
            "generation_run_id": _uuid(self.generation_run_id),
            "generation_output_id": _uuid(self.generation_output_id),
            "owner_user_id": _uuid(self.owner_user_id),
            "project_id": _uuid(self.project_id),
            "selection_hash": self.selection_hash,
            "idempotency_key": self.idempotency_key,
            "created_by": _uuid(self.created_by),
            "created_at": _dt(self.created_at),
        }

    to_dict = to_safe_dict
