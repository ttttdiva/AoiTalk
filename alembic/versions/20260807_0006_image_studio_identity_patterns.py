"""Add versioned Image Studio identity packs and patterns.

Revision ID: 20260807_0006
Revises: 20260807_0005
"""
import importlib.util
from pathlib import Path

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260807_0006"
down_revision = "20260807_0005"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)

def _header(name: str, unique_name: str) -> None:
    op.create_table(name,
        sa.Column("id", UUID, primary_key=True),
        sa.Column("project_id", UUID, sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_by", UUID, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("name", sa.String(200), nullable=False), sa.Column("description", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False), sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("project_id", "name", name=unique_name))
    op.create_index(f"ix_{name}_project_id", name, ["project_id"])


def _purge_existing_image_studio_rows() -> None:
    """Re-purge Studio data before adding strict identity constraints."""
    path = Path(__file__).with_name("20260807_0004_runtime_purge.py")
    spec = importlib.util.spec_from_file_location("_aoi_image_studio_runtime_purge", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load Image Studio purge helper")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.purge_existing_rows()

def upgrade() -> None:
    _purge_existing_image_studio_rows()
    _header("image_studio_identity_packs", "uq_image_studio_identity_packs_project_name")
    op.create_table("image_studio_identity_pack_versions",
        sa.Column("id", UUID, primary_key=True), sa.Column("identity_pack_id", UUID, sa.ForeignKey("image_studio_identity_packs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False), sa.Column("status", sa.String(16), nullable=False), sa.Column("prompt", sa.Text()), sa.Column("negative_prompt", sa.Text()),
        sa.Column("traits", sa.JSON(), nullable=False), sa.Column("lora", sa.JSON(), nullable=False), sa.Column("strategy", sa.JSON(), nullable=False), sa.Column("calibration", sa.JSON(), nullable=False),
        sa.Column("published_at", sa.DateTime()), sa.Column("created_by", UUID, sa.ForeignKey("users.id", ondelete="SET NULL")), sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("identity_pack_id", "version", name="uq_image_studio_identity_pack_versions_number"), sa.CheckConstraint("version > 0", name="ck_image_studio_identity_pack_versions_positive"), sa.CheckConstraint("status IN ('draft','published')", name="ck_image_studio_identity_pack_versions_status"))
    op.create_index("ix_image_studio_identity_pack_versions_identity_pack_id", "image_studio_identity_pack_versions", ["identity_pack_id"])
    op.create_table("image_studio_identity_asset_bindings",
        sa.Column("id", UUID, primary_key=True), sa.Column("identity_pack_version_id", UUID, sa.ForeignKey("image_studio_identity_pack_versions.id", ondelete="CASCADE"), nullable=False), sa.Column("asset_id", UUID, sa.ForeignKey("image_studio_assets.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("role", sa.String(32), nullable=False), sa.Column("crop", sa.JSON(), nullable=False), sa.Column("mask", sa.JSON(), nullable=False), sa.Column("weight", sa.Float(), nullable=False), sa.Column("approval", sa.String(16), nullable=False), sa.Column("position", sa.Integer(), nullable=False), sa.Column("metadata", sa.JSON(), nullable=False),
        sa.UniqueConstraint("identity_pack_version_id", "position", name="uq_image_studio_identity_bindings_position"), sa.CheckConstraint("weight >= 0", name="ck_image_studio_identity_bindings_weight"), sa.CheckConstraint("approval IN ('approved','rejected','pending')", name="ck_image_studio_identity_bindings_approval"), sa.CheckConstraint("position >= 0", name="ck_image_studio_identity_bindings_position"))
    op.create_index("ix_image_studio_identity_asset_bindings_pack_version_id", "image_studio_identity_asset_bindings", ["identity_pack_version_id"])
    op.create_index("ix_image_studio_identity_asset_bindings_asset_id", "image_studio_identity_asset_bindings", ["asset_id"])
    _header("image_studio_patterns", "uq_image_studio_patterns_project_name")
    op.create_table("image_studio_pattern_versions",
        sa.Column("id", UUID, primary_key=True), sa.Column("pattern_id", UUID, sa.ForeignKey("image_studio_patterns.id", ondelete="CASCADE"), nullable=False), sa.Column("version", sa.Integer(), nullable=False), sa.Column("status", sa.String(16), nullable=False),
        sa.Column("slot", sa.String(64), nullable=False), sa.Column("pattern_type", sa.String(64), nullable=False), sa.Column("take", sa.JSON(), nullable=False), sa.Column("do_not_take", sa.JSON(), nullable=False), sa.Column("variation", sa.JSON(), nullable=False), sa.Column("compatibility", sa.JSON(), nullable=False), sa.Column("control", sa.JSON(), nullable=False),
        sa.Column("source_asset_id", UUID, sa.ForeignKey("image_studio_assets.id", ondelete="RESTRICT")), sa.Column("source_output_id", UUID, sa.ForeignKey("image_studio_outputs.id", ondelete="SET NULL")), sa.Column("published_at", sa.DateTime()), sa.Column("created_by", UUID, sa.ForeignKey("users.id", ondelete="SET NULL")), sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("pattern_id", "version", name="uq_image_studio_pattern_versions_number"), sa.CheckConstraint("version > 0", name="ck_image_studio_pattern_versions_positive"), sa.CheckConstraint("status IN ('draft','published')", name="ck_image_studio_pattern_versions_status"))
    for col in ("pattern_id", "source_asset_id", "source_output_id"): op.create_index(f"ix_image_studio_pattern_versions_{col}", "image_studio_pattern_versions", [col])
    _header("image_studio_pattern_sets", "uq_image_studio_pattern_sets_project_name")
    op.create_table("image_studio_pattern_set_versions",
        sa.Column("id", UUID, primary_key=True), sa.Column("pattern_set_id", UUID, sa.ForeignKey("image_studio_pattern_sets.id", ondelete="CASCADE"), nullable=False), sa.Column("version", sa.Integer(), nullable=False), sa.Column("status", sa.String(16), nullable=False), sa.Column("published_at", sa.DateTime()), sa.Column("created_by", UUID, sa.ForeignKey("users.id", ondelete="SET NULL")), sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("pattern_set_id", "version", name="uq_image_studio_pattern_set_versions_number"), sa.CheckConstraint("version > 0", name="ck_image_studio_pattern_set_versions_positive"), sa.CheckConstraint("status IN ('draft','published')", name="ck_image_studio_pattern_set_versions_status"))
    op.create_index("ix_image_studio_pattern_set_versions_pattern_set_id", "image_studio_pattern_set_versions", ["pattern_set_id"])
    op.create_table("image_studio_pattern_set_items",
        sa.Column("id", UUID, primary_key=True), sa.Column("pattern_set_version_id", UUID, sa.ForeignKey("image_studio_pattern_set_versions.id", ondelete="CASCADE"), nullable=False), sa.Column("pattern_version_id", UUID, sa.ForeignKey("image_studio_pattern_versions.id", ondelete="RESTRICT"), nullable=False), sa.Column("position", sa.Integer(), nullable=False),
        sa.UniqueConstraint("pattern_set_version_id", "position", name="uq_image_studio_pattern_set_items_position"), sa.CheckConstraint("position >= 0", name="ck_image_studio_pattern_set_items_position"))
    op.create_index("ix_image_studio_pattern_set_items_pattern_set_version_id", "image_studio_pattern_set_items", ["pattern_set_version_id"])
    op.create_index("ix_image_studio_pattern_set_items_pattern_version_id", "image_studio_pattern_set_items", ["pattern_version_id"])

def downgrade() -> None:
    for table in ("image_studio_pattern_set_items", "image_studio_pattern_set_versions", "image_studio_pattern_sets", "image_studio_pattern_versions", "image_studio_patterns", "image_studio_identity_asset_bindings", "image_studio_identity_pack_versions", "image_studio_identity_packs"):
        op.drop_table(table)
