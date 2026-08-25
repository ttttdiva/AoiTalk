"""Add durable Image Studio output variants and provenance."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260806_0007"
down_revision = "20260806_0006"
branch_labels = None
depends_on = None


def _uuid(
    name: str,
    *,
    nullable: bool = False,
    primary_key: bool = False,
) -> sa.Column:
    return sa.Column(
        name,
        postgresql.UUID(as_uuid=True),
        nullable=nullable,
        primary_key=primary_key,
    )


def _json(name: str, default: str) -> sa.Column:
    return sa.Column(
        name,
        sa.JSON(),
        nullable=False,
        server_default=sa.text(f"'{default}'::json"),
    )


def upgrade() -> None:
    op.create_table(
        "image_studio_outputs",
        _uuid("id", primary_key=True),
        _uuid("project_id"),
        _uuid("generation_id"),
        _uuid("asset_id", nullable=True),
        _uuid("created_by", nullable=True),
        sa.Column("variant_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("storage_path", sa.String(length=1024), nullable=True),
        sa.Column("media_url", sa.String(length=2048), nullable=True),
        sa.Column("checksum_sha256", sa.String(length=64), nullable=True),
        sa.Column("mime_type", sa.String(length=160), nullable=True),
        sa.Column("byte_size", sa.BigInteger(), nullable=True),
        _json("metadata", "{}"),
        _json("provenance", "{}"),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["generation_id"],
            ["image_studio_generations.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["image_studio_assets.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint(
            "generation_id",
            "variant_index",
            name="uq_image_studio_outputs_generation_variant",
        ),
        sa.CheckConstraint(
            "variant_index >= 0",
            name="ck_image_studio_outputs_variant_index",
        ),
        sa.CheckConstraint(
            "byte_size IS NULL OR byte_size >= 0",
            name="ck_image_studio_outputs_byte_size",
        ),
    )
    op.create_index(
        "ix_image_studio_outputs_project_id",
        "image_studio_outputs",
        ["project_id"],
    )
    op.create_index(
        "ix_image_studio_outputs_generation_id",
        "image_studio_outputs",
        ["generation_id"],
    )
    op.create_index(
        "ix_image_studio_outputs_asset_id",
        "image_studio_outputs",
        ["asset_id"],
    )
    op.create_index(
        "ix_image_studio_outputs_created_by",
        "image_studio_outputs",
        ["created_by"],
    )
    op.create_index(
        "ix_image_studio_outputs_generation_variant",
        "image_studio_outputs",
        ["generation_id", "variant_index"],
    )
    op.create_index(
        "ix_image_studio_outputs_project_created",
        "image_studio_outputs",
        ["project_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_image_studio_outputs_project_created",
        table_name="image_studio_outputs",
    )
    op.drop_index(
        "ix_image_studio_outputs_generation_variant",
        table_name="image_studio_outputs",
    )
    op.drop_index(
        "ix_image_studio_outputs_created_by",
        table_name="image_studio_outputs",
    )
    op.drop_index(
        "ix_image_studio_outputs_asset_id",
        table_name="image_studio_outputs",
    )
    op.drop_index(
        "ix_image_studio_outputs_generation_id",
        table_name="image_studio_outputs",
    )
    op.drop_index(
        "ix_image_studio_outputs_project_id",
        table_name="image_studio_outputs",
    )
    op.drop_table("image_studio_outputs")
