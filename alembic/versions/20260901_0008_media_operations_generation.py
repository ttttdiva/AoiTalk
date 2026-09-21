"""Create the MediaOps Generation Studio semantic provenance ledger."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260901_0008"
down_revision = "20260901_0007"
branch_labels = None
depends_on = None


def _uuid():
    return postgresql.UUID(as_uuid=True)


def _fk(cols, refs, *, ondelete=None):
    return sa.ForeignKeyConstraint(cols, refs, ondelete=ondelete)


def upgrade() -> None:
    op.create_table(
        "media_generation_workspaces",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("owner_user_id", _uuid(), nullable=False),
        sa.Column("project_id", _uuid(), nullable=True),
        sa.Column("provider", sa.String(64), nullable=False, server_default="comfyui_workbench"),
        sa.Column("external_workspace_id", sa.String(164), nullable=False),
        sa.Column("external_project_id", sa.String(164), nullable=True),
        sa.Column("base_url", sa.String(512), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="configured"),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("created_by", _uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("provider = 'comfyui_workbench'", name="ck_media_generation_workspaces_provider"),
        sa.CheckConstraint("external_workspace_id LIKE 'wsp_%'", name="ck_media_generation_workspaces_workspace_ref"),
        sa.CheckConstraint("external_project_id IS NULL OR external_project_id LIKE 'prj_%'", name="ck_media_generation_workspaces_project_ref"),
        sa.CheckConstraint("status IN ('configured', 'unavailable', 'verified')", name="ck_media_generation_workspaces_status"),
        sa.CheckConstraint("length(config_hash) = 64", name="ck_media_generation_workspaces_hash"),
        _fk(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        _fk(["project_id"], ["projects.id"], ondelete="CASCADE"),
        _fk(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_media_generation_workspaces_owner_project", "media_generation_workspaces", ["owner_user_id", "project_id"])
    op.create_index("ix_media_generation_workspaces_external_workspace_id", "media_generation_workspaces", ["external_workspace_id"])
    op.create_index("ix_media_generation_workspaces_external_project_id", "media_generation_workspaces", ["external_project_id"])
    op.create_index("ix_media_generation_workspaces_status", "media_generation_workspaces", ["status"])
    op.create_index("ix_media_generation_workspaces_config_hash", "media_generation_workspaces", ["config_hash"])
    op.create_index("ix_media_generation_workspaces_created_at", "media_generation_workspaces", ["created_at"])
    op.create_index("uq_media_generation_workspaces_personal_identity", "media_generation_workspaces", ["owner_user_id", "external_workspace_id"], unique=True, postgresql_where=sa.text("project_id IS NULL"), sqlite_where=sa.text("project_id IS NULL"))
    op.create_index("uq_media_generation_workspaces_project_identity", "media_generation_workspaces", ["project_id", "external_workspace_id"], unique=True, postgresql_where=sa.text("project_id IS NOT NULL"), sqlite_where=sa.text("project_id IS NOT NULL"))
    op.create_index("uq_media_generation_workspaces_personal_idempotency", "media_generation_workspaces", ["owner_user_id", "idempotency_key"], unique=True, postgresql_where=sa.text("project_id IS NULL"), sqlite_where=sa.text("project_id IS NULL"))
    op.create_index("uq_media_generation_workspaces_project_idempotency", "media_generation_workspaces", ["project_id", "idempotency_key"], unique=True, postgresql_where=sa.text("project_id IS NOT NULL"), sqlite_where=sa.text("project_id IS NOT NULL"))

    op.create_table(
        "media_creative_recipes",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("owner_user_id", _uuid(), nullable=False),
        sa.Column("project_id", _uuid(), nullable=True),
        sa.Column("persona_id", _uuid(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False, server_default="Untitled recipe"),
        sa.Column("create_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("created_by", _uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("length(create_hash) = 64", name="ck_media_creative_recipes_create_hash"),
        _fk(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        _fk(["project_id"], ["projects.id"], ondelete="CASCADE"),
        _fk(["persona_id"], ["media_personas.id"], ondelete="CASCADE"),
        _fk(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_media_creative_recipes_owner_project", "media_creative_recipes", ["owner_user_id", "project_id"])
    op.create_index("ix_media_creative_recipes_persona_id", "media_creative_recipes", ["persona_id"])
    op.create_index("ix_media_creative_recipes_create_hash", "media_creative_recipes", ["create_hash"])
    op.create_index("ix_media_creative_recipes_created_at", "media_creative_recipes", ["created_at"])
    op.create_index("uq_media_creative_recipes_personal_idempotency", "media_creative_recipes", ["owner_user_id", "idempotency_key"], unique=True, postgresql_where=sa.text("project_id IS NULL"), sqlite_where=sa.text("project_id IS NULL"))
    op.create_index("uq_media_creative_recipes_project_idempotency", "media_creative_recipes", ["project_id", "idempotency_key"], unique=True, postgresql_where=sa.text("project_id IS NOT NULL"), sqlite_where=sa.text("project_id IS NOT NULL"))

    op.create_table(
        "media_creative_recipe_revisions",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("creative_recipe_id", _uuid(), nullable=False),
        sa.Column("owner_user_id", _uuid(), nullable=False),
        sa.Column("project_id", _uuid(), nullable=True),
        sa.Column("persona_id", _uuid(), nullable=False),
        sa.Column("persona_revision_id", _uuid(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("recipe_type", sa.String(24), nullable=False),
        sa.Column("workflow_id", sa.String(255), nullable=True),
        sa.Column("model_selection_id", sa.String(255), nullable=True),
        sa.Column("identity_pack_id", sa.String(255), nullable=True),
        sa.Column("prompt_schema_id", sa.String(255), nullable=True),
        sa.Column("prompt_template", sa.Text(), nullable=False),
        sa.Column("negative_requirements", sa.Text(), nullable=True),
        sa.Column("reference_asset_ids", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("aspect_ratio", sa.String(32), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("frame_count", sa.Integer(), nullable=True),
        sa.Column("storyboard_json", sa.JSON(), nullable=True),
        sa.Column("candidate_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("cost_policy", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("provenance_retention_policy", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("human_review_policy", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=True),
        sa.Column("created_by", _uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("version > 0", name="ck_media_creative_recipe_revisions_version"),
        sa.CheckConstraint("recipe_type IN ('image', 'image_set', 'comic', 'video', 'thumbnail')", name="ck_media_creative_recipe_revisions_type"),
        sa.CheckConstraint("candidate_count >= 1 AND candidate_count <= 20", name="ck_media_creative_recipe_revisions_candidates"),
        sa.CheckConstraint("width IS NULL OR (width > 0 AND width <= 16384)", name="ck_media_creative_recipe_revisions_width"),
        sa.CheckConstraint("height IS NULL OR (height > 0 AND height <= 16384)", name="ck_media_creative_recipe_revisions_height"),
        sa.CheckConstraint("duration_seconds IS NULL OR (duration_seconds > 0 AND duration_seconds <= 86400)", name="ck_media_creative_recipe_revisions_duration"),
        sa.CheckConstraint("frame_count IS NULL OR (frame_count > 0 AND frame_count <= 1000000)", name="ck_media_creative_recipe_revisions_frames"),
        sa.CheckConstraint("length(content_hash) = 64", name="ck_media_creative_recipe_revisions_hash"),
        _fk(["creative_recipe_id"], ["media_creative_recipes.id"], ondelete="CASCADE"),
        _fk(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        _fk(["project_id"], ["projects.id"], ondelete="CASCADE"),
        _fk(["persona_id"], ["media_personas.id"], ondelete="CASCADE"),
        _fk(["persona_revision_id"], ["media_persona_revisions.id"], ondelete="CASCADE"),
        _fk(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("creative_recipe_id", "version", name="uq_media_creative_recipe_revisions_version"),
        sa.UniqueConstraint("creative_recipe_id", "idempotency_key", name="uq_media_creative_recipe_revisions_idempotency"),
    )
    for name, cols in {
        "owner_project": ["owner_user_id", "project_id"], "creative_recipe_id": ["creative_recipe_id"], "persona_id": ["persona_id"], "persona_revision_id": ["persona_revision_id"], "recipe_type": ["recipe_type"], "content_hash": ["content_hash"], "project_id": ["project_id"], "owner_user_id": ["owner_user_id"], "created_at": ["created_at"],
    }.items():
        op.create_index(f"ix_media_creative_recipe_revisions_{name}", "media_creative_recipe_revisions", cols)

    op.create_table(
        "media_generation_plans",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("owner_user_id", _uuid(), nullable=False),
        sa.Column("project_id", _uuid(), nullable=True),
        sa.Column("persona_revision_id", _uuid(), nullable=False),
        sa.Column("persona_revision_hash", sa.String(64), nullable=False),
        sa.Column("content_item_id", _uuid(), nullable=True),
        sa.Column("content_variant_id", _uuid(), nullable=True),
        sa.Column("creative_recipe_revision_id", _uuid(), nullable=False),
        sa.Column("workspace_id", _uuid(), nullable=False),
        sa.Column("requested_outputs", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("request_spec", sa.JSON(), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("plan_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("created_by", _uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("length(persona_revision_hash) = 64", name="ck_media_generation_plans_persona_hash"),
        sa.CheckConstraint("requested_outputs >= 1 AND requested_outputs <= 20", name="ck_media_generation_plans_requested_outputs"),
        sa.CheckConstraint("length(plan_hash) = 64", name="ck_media_generation_plans_hash"),
        sa.CheckConstraint("status IN ('draft', 'submitted', 'unavailable')", name="ck_media_generation_plans_status"),
        _fk(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        _fk(["project_id"], ["projects.id"], ondelete="CASCADE"),
        _fk(["persona_revision_id"], ["media_persona_revisions.id"], ondelete="CASCADE"),
        _fk(["content_item_id"], ["media_content_items.id"], ondelete="SET NULL"),
        _fk(["creative_recipe_revision_id"], ["media_creative_recipe_revisions.id"], ondelete="CASCADE"),
        _fk(["workspace_id"], ["media_generation_workspaces.id"], ondelete="CASCADE"),
        _fk(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_media_generation_plans_owner_project", "media_generation_plans", ["owner_user_id", "project_id"])
    for name in ["persona_revision_id", "content_item_id", "content_variant_id", "creative_recipe_revision_id", "workspace_id", "status", "plan_hash", "project_id", "owner_user_id", "created_at"]:
        op.create_index(f"ix_media_generation_plans_{name}", "media_generation_plans", [name])
    op.create_index("uq_media_generation_plans_personal_idempotency", "media_generation_plans", ["owner_user_id", "idempotency_key"], unique=True, postgresql_where=sa.text("project_id IS NULL"), sqlite_where=sa.text("project_id IS NULL"))
    op.create_index("uq_media_generation_plans_project_idempotency", "media_generation_plans", ["project_id", "idempotency_key"], unique=True, postgresql_where=sa.text("project_id IS NOT NULL"), sqlite_where=sa.text("project_id IS NOT NULL"))

    op.create_table(
        "media_generation_run_intents",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("plan_id", _uuid(), nullable=False),
        sa.Column("owner_user_id", _uuid(), nullable=False),
        sa.Column("project_id", _uuid(), nullable=True),
        sa.Column("external_idempotency_key", sa.String(64), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("external_run_id", sa.String(164), nullable=True),
        sa.Column("adapter_release", sa.String(128), nullable=True),
        sa.Column("error_code", sa.String(128), nullable=True),
        sa.Column("response_hash", sa.String(64), nullable=True),
        sa.Column("created_by", _uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("length(external_idempotency_key) = 64", name="ck_media_generation_run_intents_idempotency"),
        sa.CheckConstraint("length(request_hash) = 64", name="ck_media_generation_run_intents_request_hash"),
        sa.CheckConstraint("response_hash IS NULL OR length(response_hash) = 64", name="ck_media_generation_run_intents_response_hash"),
        sa.CheckConstraint("status IN ('pending', 'submitted', 'unavailable', 'uncertain', 'failed')", name="ck_media_generation_run_intents_status"),
        _fk(["plan_id"], ["media_generation_plans.id"], ondelete="CASCADE"),
        _fk(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        _fk(["project_id"], ["projects.id"], ondelete="CASCADE"),
        _fk(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("plan_id", name="uq_media_generation_run_intents_plan"),
        sa.UniqueConstraint("external_idempotency_key", name="uq_media_generation_run_intents_external_key"),
    )
    for name, cols in {"plan_id":["plan_id"],"external_idempotency_key":["external_idempotency_key"],"owner_user_id":["owner_user_id"],"project_id":["project_id"],"request_hash":["request_hash"],"response_hash":["response_hash"],"created_at":["created_at"],"owner_project":["owner_user_id","project_id"]}.items():
        op.create_index(f"ix_media_generation_run_intents_{name}", "media_generation_run_intents", cols)

    op.create_table(
        "media_generation_runs",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("plan_id", _uuid(), nullable=False),
        sa.Column("intent_id", _uuid(), nullable=False),
        sa.Column("owner_user_id", _uuid(), nullable=False),
        sa.Column("project_id", _uuid(), nullable=True),
        sa.Column("workspace_id", _uuid(), nullable=False),
        sa.Column("external_run_id", sa.String(164), nullable=False),
        sa.Column("external_workspace_id", sa.String(164), nullable=False),
        sa.Column("external_project_id", sa.String(164), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("run_hash", sa.String(64), nullable=False),
        sa.Column("adapter_release", sa.String(128), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("cost_summary", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("error_code", sa.String(128), nullable=True),
        sa.Column("result_deep_link", sa.String(2000), nullable=True),
        sa.Column("created_by", _uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("external_run_id LIKE 'run_%'", name="ck_media_generation_runs_run_ref"),
        sa.CheckConstraint("external_workspace_id LIKE 'wsp_%'", name="ck_media_generation_runs_workspace_ref"),
        sa.CheckConstraint("external_project_id LIKE 'prj_%'", name="ck_media_generation_runs_project_ref"),
        sa.CheckConstraint("status IN ('draft', 'queued', 'claimed', 'running', 'output_pending', 'succeeded', 'failed', 'cancelled', 'quarantined', 'uncertain', 'unavailable')", name="ck_media_generation_runs_status"),
        sa.CheckConstraint("length(run_hash) = 64", name="ck_media_generation_runs_hash"),
        _fk(["plan_id"], ["media_generation_plans.id"], ondelete="CASCADE"),
        _fk(["intent_id"], ["media_generation_run_intents.id"], ondelete="CASCADE"),
        _fk(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        _fk(["project_id"], ["projects.id"], ondelete="CASCADE"),
        _fk(["workspace_id"], ["media_generation_workspaces.id"], ondelete="CASCADE"),
        _fk(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "external_run_id", name="uq_media_generation_runs_workspace_external"),
    )
    for name, cols in {"plan_id":["plan_id"],"intent_id":["intent_id"],"workspace_id":["workspace_id"],"external_run_id":["external_run_id"],"status":["status"],"run_hash":["run_hash"],"project_id":["project_id"],"owner_user_id":["owner_user_id"],"created_at":["created_at"],"owner_project":["owner_user_id","project_id"]}.items():
        op.create_index(f"ix_media_generation_runs_{name}", "media_generation_runs", cols)

    op.create_table(
        "media_generation_run_observations",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("generation_run_id", _uuid(), nullable=False),
        sa.Column("owner_user_id", _uuid(), nullable=False),
        sa.Column("project_id", _uuid(), nullable=True),
        sa.Column("external_run_id", sa.String(164), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("observation_hash", sa.String(64), nullable=False),
        sa.Column("adapter_release", sa.String(128), nullable=True),
        sa.Column("cost_summary", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("error_code", sa.String(128), nullable=True),
        sa.Column("result_deep_link", sa.String(2000), nullable=True),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("external_run_id LIKE 'run_%'", name="ck_media_generation_run_observations_run_ref"),
        sa.CheckConstraint("status IN ('draft', 'queued', 'claimed', 'running', 'output_pending', 'succeeded', 'failed', 'cancelled', 'quarantined', 'uncertain', 'unavailable')", name="ck_media_generation_run_observations_status"),
        sa.CheckConstraint("length(observation_hash) = 64", name="ck_media_generation_run_observations_hash"),
        _fk(["generation_run_id"], ["media_generation_runs.id"], ondelete="CASCADE"),
        _fk(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        _fk(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("generation_run_id", "observation_hash", name="uq_media_generation_run_observations_hash"),
    )
    for name, cols in {"generation_run_id":["generation_run_id"],"external_run_id":["external_run_id"],"observation_hash":["observation_hash"],"owner_user_id":["owner_user_id"],"project_id":["project_id"],"observed_at":["observed_at"],"owner_project":["owner_user_id","project_id"]}.items():
        op.create_index(f"ix_media_generation_run_observations_{name}", "media_generation_run_observations", cols)

    op.create_table(
        "media_generation_outputs",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("generation_run_id", _uuid(), nullable=False),
        sa.Column("owner_user_id", _uuid(), nullable=False),
        sa.Column("project_id", _uuid(), nullable=True),
        sa.Column("external_asset_id", sa.String(164), nullable=False),
        sa.Column("external_output_version", sa.String(165), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("mime_type", sa.String(255), nullable=False),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("deep_link", sa.String(2000), nullable=True),
        sa.Column("provenance_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("external_asset_id LIKE 'ast_%'", name="ck_media_generation_outputs_asset_ref"),
        sa.CheckConstraint("external_output_version LIKE 'outv_%'", name="ck_media_generation_outputs_version_ref"),
        sa.CheckConstraint("length(sha256) = 64", name="ck_media_generation_outputs_sha256"),
        sa.CheckConstraint("length(provenance_hash) = 64", name="ck_media_generation_outputs_provenance_hash"),
        sa.CheckConstraint("width IS NULL OR (width > 0 AND width <= 16384)", name="ck_media_generation_outputs_width"),
        sa.CheckConstraint("height IS NULL OR (height > 0 AND height <= 16384)", name="ck_media_generation_outputs_height"),
        _fk(["generation_run_id"], ["media_generation_runs.id"], ondelete="CASCADE"),
        _fk(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        _fk(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("generation_run_id", "external_asset_id", "external_output_version", name="uq_media_generation_outputs_external"),
    )
    for name, cols in {"generation_run_id":["generation_run_id"],"external_asset_id":["external_asset_id"],"external_output_version":["external_output_version"],"sha256":["sha256"],"provenance_hash":["provenance_hash"],"owner_user_id":["owner_user_id"],"project_id":["project_id"],"created_at":["created_at"],"owner_project":["owner_user_id","project_id"]}.items():
        op.create_index(f"ix_media_generation_outputs_{name}", "media_generation_outputs", cols)

    op.create_table(
        "media_generation_output_selections",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("generation_run_id", _uuid(), nullable=False),
        sa.Column("generation_output_id", _uuid(), nullable=False),
        sa.Column("owner_user_id", _uuid(), nullable=False),
        sa.Column("project_id", _uuid(), nullable=True),
        sa.Column("selection_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("created_by", _uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("length(selection_hash) = 64", name="ck_media_generation_output_selections_hash"),
        _fk(["generation_run_id"], ["media_generation_runs.id"], ondelete="CASCADE"),
        _fk(["generation_output_id"], ["media_generation_outputs.id"], ondelete="CASCADE"),
        _fk(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        _fk(["project_id"], ["projects.id"], ondelete="CASCADE"),
        _fk(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("generation_run_id", "idempotency_key", name="uq_media_generation_output_selections_idempotency"),
    )
    for name, cols in {"generation_run_id":["generation_run_id"],"generation_output_id":["generation_output_id"],"selection_hash":["selection_hash"],"owner_user_id":["owner_user_id"],"project_id":["project_id"],"created_at":["created_at"],"owner_project":["owner_user_id","project_id"]}.items():
        op.create_index(f"ix_media_generation_output_selections_{name}", "media_generation_output_selections", cols)


def downgrade() -> None:
    for table in [
        "media_generation_output_selections",
        "media_generation_outputs",
        "media_generation_run_observations",
        "media_generation_runs",
        "media_generation_run_intents",
        "media_generation_plans",
        "media_creative_recipe_revisions",
        "media_creative_recipes",
        "media_generation_workspaces",
    ]:
        op.drop_table(table)
