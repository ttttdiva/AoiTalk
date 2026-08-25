"""Add Image Studio structured commands, external V2 targets, and repairs.

Revision ID: 20260808_0009
Revises: 20260807_0008
"""

import importlib.util
from pathlib import Path

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260808_0009"
down_revision = "20260807_0008"
branch_labels = None
depends_on = None


def _uuid() -> postgresql.UUID:
    return postgresql.UUID(as_uuid=True)


def _purge_existing_image_studio_rows() -> None:
    """Re-purge Studio data before adding strict V2 targets/repairs."""
    path = Path(__file__).with_name("20260807_0004_runtime_purge.py")
    spec = importlib.util.spec_from_file_location("_aoi_image_studio_runtime_purge", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load Image Studio purge helper")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.purge_existing_rows()


def upgrade() -> None:
    _purge_existing_image_studio_rows()
    uuid_type = _uuid()
    external = "image_studio_external_sessions"
    op.alter_column(external, "generation_id", nullable=True)
    op.add_column(external, sa.Column("target_type", sa.String(24), nullable=False, server_default="legacy_generation"))
    for name in ("batch_id", "run_id", "shot_id", "revision_id"):
        op.add_column(external, sa.Column(name, uuid_type, nullable=True))
    op.add_column(external, sa.Column("request_key_hash", sa.String(64), nullable=True))
    op.add_column(external, sa.Column("request_fingerprint", sa.String(64), nullable=True))
    op.execute(sa.text("""
        UPDATE image_studio_external_sessions
        SET request_key_hash = md5('external-key:' || id::text) || md5(project_id::text || ':' || id::text),
            request_fingerprint = md5('external-payload:' || id::text) || md5(id::text || ':' || project_id::text)
        WHERE request_key_hash IS NULL OR request_fingerprint IS NULL
    """))
    op.alter_column(external, "request_key_hash", nullable=False)
    op.alter_column(external, "request_fingerprint", nullable=False)
    targets = {
        "batch_id": ("image_studio_batches", "CASCADE"),
        "run_id": ("image_studio_runs", "CASCADE"),
        "shot_id": ("image_studio_shots", "SET NULL"),
        "revision_id": ("image_studio_shot_revisions", "SET NULL"),
    }
    for name, (table, ondelete) in targets.items():
        op.create_foreign_key(f"fk_is_external_{name}", external, table, [name], ["id"], ondelete=ondelete)
        op.create_index(f"ix_image_studio_external_sessions_{name}", external, [name])
    op.create_index("ix_image_studio_external_sessions_target_type", external, ["target_type"])
    op.create_unique_constraint("uq_image_studio_external_sessions_project_request_key", external, ["project_id", "request_key_hash"])
    op.create_check_constraint("ck_image_studio_external_sessions_target_type", external, "target_type IN ('legacy_generation','batch','run','shot','revision')")
    op.create_check_constraint(
        "ck_image_studio_external_sessions_unambiguous_target",
        external,
        "(target_type = 'legacy_generation' AND generation_id IS NOT NULL AND batch_id IS NULL AND run_id IS NULL AND shot_id IS NULL AND revision_id IS NULL) OR "
        "(target_type = 'batch' AND generation_id IS NULL AND batch_id IS NOT NULL AND run_id IS NULL AND shot_id IS NOT NULL AND revision_id IS NOT NULL) OR "
        "(target_type = 'run' AND generation_id IS NULL AND batch_id IS NOT NULL AND run_id IS NOT NULL AND shot_id IS NOT NULL AND revision_id IS NOT NULL) OR "
        "(target_type = 'shot' AND generation_id IS NULL AND batch_id IS NULL AND run_id IS NULL AND shot_id IS NOT NULL AND revision_id IS NULL) OR "
        "(target_type = 'revision' AND generation_id IS NULL AND batch_id IS NULL AND run_id IS NULL AND shot_id IS NOT NULL AND revision_id IS NOT NULL)",
    )

    op.create_table(
        "image_studio_structured_commands",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("project_id", uuid_type, sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("conversation_session_id", uuid_type, sa.ForeignKey("conversation_sessions.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_by", uuid_type, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("command_type", sa.String(24), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="previewed"),
        sa.Column("server_summary", sa.Text(), nullable=False),
        sa.Column("server_intent", sa.String(80), nullable=False),
        sa.Column("command_payload", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("preview_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("preview_hash", sa.String(64), nullable=False),
        sa.Column("request_key_hash", sa.String(64), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("target_action_descriptor", sa.JSON(), nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("state_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
        sa.Column("rejected_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("project_id", "request_key_hash", name="uq_is_structured_commands_project_request_key"),
        sa.CheckConstraint("command_type IN ('batch','handoff')", name="ck_is_structured_commands_type"),
        sa.CheckConstraint("status IN ('previewed','confirmed','rejected')", name="ck_is_structured_commands_status"),
        sa.CheckConstraint("state_version >= 0", name="ck_is_structured_commands_state_version"),
        sa.CheckConstraint("status <> 'confirmed' OR (target_action_descriptor IS NOT NULL AND confirmed_at IS NOT NULL)", name="ck_is_structured_commands_confirmed_action"),
        sa.CheckConstraint("status = 'confirmed' OR target_action_descriptor IS NULL", name="ck_is_structured_commands_action_only_when_confirmed"),
        sa.CheckConstraint("status <> 'rejected' OR rejected_at IS NOT NULL", name="ck_is_structured_commands_rejected_at"),
    )
    for name in ("project_id", "conversation_session_id", "created_by", "command_type", "status"):
        op.create_index(f"ix_image_studio_structured_commands_{name}", "image_studio_structured_commands", [name])
    op.create_index("ix_is_structured_commands_project_status_created", "image_studio_structured_commands", ["project_id", "status", "created_at"])
    op.create_index("ix_is_structured_commands_conversation_created", "image_studio_structured_commands", ["conversation_session_id", "created_at"])

    op.create_table(
        "image_studio_repair_stages",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("project_id", uuid_type, sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_output_id", uuid_type, sa.ForeignKey("image_studio_outputs.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("source_run_id", uuid_type, sa.ForeignKey("image_studio_runs.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("source_asset_id", uuid_type, sa.ForeignKey("image_studio_assets.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("mask_asset_id", uuid_type, sa.ForeignKey("image_studio_assets.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("workflow_id", uuid_type, sa.ForeignKey("image_studio_workflows.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_by", uuid_type, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("repair_type", sa.String(24), nullable=False, server_default="inpaint"),
        sa.Column("status", sa.String(24), nullable=False, server_default="previewed"),
        sa.Column("request_payload", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("repair_profile", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("plan_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("plan_hash", sa.String(64), nullable=False),
        sa.Column("capability_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("capability_hash", sa.String(64), nullable=False),
        sa.Column("preview_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("preview_hash", sa.String(64), nullable=False),
        sa.Column("request_key_hash", sa.String(64), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("preflight_error", sa.Text(), nullable=True),
        sa.Column("decision_action", sa.String(24), nullable=True),
        sa.Column("decision_key_hash", sa.String(64), nullable=True),
        sa.Column("decision_fingerprint", sa.String(64), nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("state_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("child_batch_id", uuid_type, sa.ForeignKey("image_studio_batches.id", ondelete="SET NULL"), nullable=True),
        sa.Column("child_manifest_id", uuid_type, sa.ForeignKey("image_studio_execution_manifests.id", ondelete="SET NULL"), nullable=True),
        sa.Column("child_run_id", uuid_type, sa.ForeignKey("image_studio_runs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("rejected_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("project_id", "request_key_hash", name="uq_image_studio_repair_project_request_key"),
        sa.CheckConstraint("repair_type = 'inpaint'", name="ck_image_studio_repair_type"),
        sa.CheckConstraint("status IN ('previewed','preflight_failed','approved','rejected')", name="ck_image_studio_repair_status"),
        sa.CheckConstraint("state_version >= 0", name="ck_image_studio_repair_state_version"),
        sa.CheckConstraint("source_asset_id <> mask_asset_id", name="ck_image_studio_repair_distinct_mask"),
        sa.CheckConstraint("status <> 'approved' OR (approved_at IS NOT NULL AND child_batch_id IS NOT NULL AND child_manifest_id IS NOT NULL AND child_run_id IS NOT NULL)", name="ck_image_studio_repair_approved_children"),
        sa.CheckConstraint("status = 'approved' OR (child_batch_id IS NULL AND child_manifest_id IS NULL AND child_run_id IS NULL)", name="ck_image_studio_repair_children_only_when_approved"),
        sa.CheckConstraint("status <> 'rejected' OR rejected_at IS NOT NULL", name="ck_image_studio_repair_rejected_at"),
        sa.CheckConstraint("status <> 'preflight_failed' OR preflight_error IS NOT NULL", name="ck_image_studio_repair_preflight_error"),
    )
    for name in ("project_id", "source_output_id", "source_run_id", "source_asset_id", "mask_asset_id", "workflow_id", "created_by", "status", "child_batch_id", "child_manifest_id", "child_run_id"):
        op.create_index(f"ix_image_studio_repair_stages_{name}", "image_studio_repair_stages", [name])
    op.create_index("ix_image_studio_repair_project_status_created", "image_studio_repair_stages", ["project_id", "status", "created_at"])


def downgrade() -> None:
    op.execute(sa.text("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM image_studio_structured_commands) THEN
            RAISE EXCEPTION 'cannot downgrade: structured command rows must be exported or removed first';
          END IF;
          IF EXISTS (SELECT 1 FROM image_studio_repair_stages) THEN
            RAISE EXCEPTION 'cannot downgrade: repair stage rows must be exported or removed first';
          END IF;
          IF EXISTS (SELECT 1 FROM image_studio_external_sessions WHERE target_type <> 'legacy_generation' OR generation_id IS NULL) THEN
            RAISE EXCEPTION 'cannot downgrade: V2 external handoff rows must be migrated first';
          END IF;
        END $$
    """))
    op.drop_table("image_studio_repair_stages")
    op.drop_table("image_studio_structured_commands")
    external = "image_studio_external_sessions"
    op.drop_constraint("ck_image_studio_external_sessions_unambiguous_target", external, type_="check")
    op.drop_constraint("ck_image_studio_external_sessions_target_type", external, type_="check")
    op.drop_constraint("uq_image_studio_external_sessions_project_request_key", external, type_="unique")
    op.drop_index("ix_image_studio_external_sessions_target_type", table_name=external)
    for name in ("revision_id", "shot_id", "run_id", "batch_id"):
        op.drop_index(f"ix_image_studio_external_sessions_{name}", table_name=external)
        op.drop_constraint(f"fk_is_external_{name}", external, type_="foreignkey")
    for name in ("request_fingerprint", "request_key_hash", "revision_id", "shot_id", "run_id", "batch_id", "target_type"):
        op.drop_column(external, name)
    op.alter_column(external, "generation_id", nullable=False)
