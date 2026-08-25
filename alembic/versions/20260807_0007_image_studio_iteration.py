"""Add Image Studio evaluation, reroll, reference, and calibration domain.

Revision ID: 20260807_0007
Revises: 20260807_0006
"""
import importlib.util
from pathlib import Path

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260807_0007"
down_revision = "20260807_0006"
branch_labels = None
depends_on = None
UUID = postgresql.UUID(as_uuid=True)


def _purge_existing_image_studio_rows() -> None:
    """Re-purge Studio data before adding strict iteration constraints."""
    path = Path(__file__).with_name("20260807_0004_runtime_purge.py")
    spec = importlib.util.spec_from_file_location("_aoi_image_studio_runtime_purge", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load Image Studio purge helper")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.purge_existing_rows()


def upgrade() -> None:
    _purge_existing_image_studio_rows()
    op.create_table("image_studio_output_evaluations",
        sa.Column("id", UUID, primary_key=True), sa.Column("project_id", UUID, sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("output_id", UUID, sa.ForeignKey("image_studio_outputs.id", ondelete="RESTRICT"), nullable=False), sa.Column("run_id", UUID, sa.ForeignKey("image_studio_runs.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("manifest_id", UUID, sa.ForeignKey("image_studio_execution_manifests.id", ondelete="RESTRICT"), nullable=False), sa.Column("evaluator_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("evaluator_kind", sa.String(16), nullable=False), sa.Column("decision", sa.String(24), nullable=False), sa.Column("reason_tags", sa.JSON(), nullable=False), sa.Column("dimension_scores", sa.JSON(), nullable=False),
        sa.Column("notes", sa.Text()), sa.Column("snapshot", sa.JSON(), nullable=False), sa.Column("idempotency_key", sa.String(160), nullable=False), sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("project_id", "idempotency_key", name="uq_is_output_evaluations_project_key"), sa.CheckConstraint("decision IN ('good','maybe','reject','needs_revision')", name="ck_is_output_evaluations_decision"), sa.CheckConstraint("evaluator_kind IN ('human','system','model')", name="ck_is_output_evaluations_evaluator"))
    op.create_index("ix_image_studio_output_evaluations_project_id", "image_studio_output_evaluations", ["project_id"]); op.create_index("ix_is_output_evaluations_output_created", "image_studio_output_evaluations", ["output_id", "created_at"]); op.create_index("ix_image_studio_output_evaluations_run_id", "image_studio_output_evaluations", ["run_id"])
    op.create_table("image_studio_reroll_requests",
        sa.Column("id", UUID, primary_key=True), sa.Column("project_id", UUID, sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False), sa.Column("parent_run_id", UUID, sa.ForeignKey("image_studio_runs.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("parent_manifest_id", UUID, sa.ForeignKey("image_studio_execution_manifests.id", ondelete="RESTRICT"), nullable=False), sa.Column("child_run_id", UUID, sa.ForeignKey("image_studio_runs.id", ondelete="RESTRICT"), nullable=False, unique=True), sa.Column("child_manifest_id", UUID, sa.ForeignKey("image_studio_execution_manifests.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("source_output_id", UUID, sa.ForeignKey("image_studio_outputs.id", ondelete="RESTRICT")), sa.Column("patch_kind", sa.String(24), nullable=False), sa.Column("patch", sa.JSON(), nullable=False), sa.Column("manifest_diff", sa.JSON(), nullable=False), sa.Column("idempotency_key", sa.String(160), nullable=False), sa.Column("created_by", UUID, sa.ForeignKey("users.id", ondelete="SET NULL")), sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("project_id", "idempotency_key", name="uq_is_rerolls_project_key"), sa.CheckConstraint("patch_kind IN ('seed_only','custom')", name="ck_is_rerolls_patch_kind"), sa.CheckConstraint("parent_run_id <> child_run_id", name="ck_is_rerolls_distinct_runs"))
    for column in ("project_id", "parent_run_id"): op.create_index(f"ix_image_studio_reroll_requests_{column}", "image_studio_reroll_requests", [column])
    op.create_table("image_studio_reference_bindings",
        sa.Column("id", UUID, primary_key=True), sa.Column("project_id", UUID, sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False), sa.Column("binding_type", sa.String(24), nullable=False), sa.Column("source_kind", sa.String(24), nullable=False),
        sa.Column("source_asset_id", UUID, sa.ForeignKey("image_studio_assets.id", ondelete="RESTRICT")), sa.Column("source_output_id", UUID, sa.ForeignKey("image_studio_outputs.id", ondelete="RESTRICT")), sa.Column("identity_pack_version_id", UUID, sa.ForeignKey("image_studio_identity_pack_versions.id", ondelete="RESTRICT")), sa.Column("pattern_version_id", UUID, sa.ForeignKey("image_studio_pattern_versions.id", ondelete="RESTRICT")),
        sa.Column("weight", sa.Float(), nullable=False), sa.Column("snapshot", sa.JSON(), nullable=False), sa.Column("idempotency_key", sa.String(160), nullable=False), sa.Column("created_by", UUID, sa.ForeignKey("users.id", ondelete="SET NULL")), sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("project_id", "idempotency_key", name="uq_is_reference_bindings_project_key"), sa.CheckConstraint("binding_type IN ('identity','pattern','pose','style','composition')", name="ck_is_reference_bindings_type"), sa.CheckConstraint("source_kind IN ('asset','output','identity_version','pattern_version')", name="ck_is_reference_bindings_source_kind"), sa.CheckConstraint("(source_kind = 'asset' AND source_asset_id IS NOT NULL AND source_output_id IS NULL AND identity_pack_version_id IS NULL AND pattern_version_id IS NULL) OR (source_kind = 'output' AND source_asset_id IS NULL AND source_output_id IS NOT NULL AND identity_pack_version_id IS NULL AND pattern_version_id IS NULL) OR (source_kind = 'identity_version' AND source_asset_id IS NULL AND source_output_id IS NULL AND identity_pack_version_id IS NOT NULL AND pattern_version_id IS NULL) OR (source_kind = 'pattern_version' AND source_asset_id IS NULL AND source_output_id IS NULL AND identity_pack_version_id IS NULL AND pattern_version_id IS NOT NULL)", name="ck_is_reference_bindings_typed_source"), sa.CheckConstraint("weight >= 0 AND weight <= 2", name="ck_is_reference_bindings_weight"))
    op.create_index("ix_image_studio_reference_bindings_project_id", "image_studio_reference_bindings", ["project_id"])
    op.create_table("image_studio_calibrations",
        sa.Column("id", UUID, primary_key=True), sa.Column("project_id", UUID, sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False), sa.Column("subject_kind", sa.String(24), nullable=False), sa.Column("identity_pack_version_id", UUID, sa.ForeignKey("image_studio_identity_pack_versions.id", ondelete="RESTRICT")), sa.Column("pattern_version_id", UUID, sa.ForeignKey("image_studio_pattern_versions.id", ondelete="RESTRICT")),
        sa.Column("version", sa.Integer(), nullable=False), sa.Column("predecessor_id", UUID, sa.ForeignKey("image_studio_calibrations.id", ondelete="SET NULL")), sa.Column("status", sa.String(16), nullable=False), sa.Column("score", sa.Float()), sa.Column("approved", sa.Boolean(), nullable=False), sa.Column("notes", sa.Text()), sa.Column("model_profile", sa.JSON(), nullable=False), sa.Column("workflow_profile", sa.JSON(), nullable=False), sa.Column("evidence_output_ids", sa.JSON(), nullable=False), sa.Column("created_by", UUID, sa.ForeignKey("users.id", ondelete="SET NULL")), sa.Column("published_at", sa.DateTime()), sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("subject_kind IN ('identity','pattern')", name="ck_is_calibrations_subject_kind"), sa.CheckConstraint("(subject_kind = 'identity' AND identity_pack_version_id IS NOT NULL AND pattern_version_id IS NULL) OR (subject_kind = 'pattern' AND identity_pack_version_id IS NULL AND pattern_version_id IS NOT NULL)", name="ck_is_calibrations_typed_subject"), sa.CheckConstraint("status IN ('draft','published')", name="ck_is_calibrations_status"), sa.CheckConstraint("version > 0", name="ck_is_calibrations_version"), sa.CheckConstraint("score IS NULL OR (score >= 0 AND score <= 1)", name="ck_is_calibrations_score"), sa.UniqueConstraint("identity_pack_version_id", "version", name="uq_is_calibrations_identity_version"), sa.UniqueConstraint("pattern_version_id", "version", name="uq_is_calibrations_pattern_version"))
    op.create_index("ix_image_studio_calibrations_project_id", "image_studio_calibrations", ["project_id"])


def downgrade() -> None:
    for table in ("image_studio_calibrations", "image_studio_reference_bindings", "image_studio_reroll_requests", "image_studio_output_evaluations"):
        op.drop_table(table)
