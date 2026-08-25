"""Add the project-scoped Image Studio catalog tables."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260806_0002"
down_revision = "20260806_0001"
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


def _timestamps() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )


def upgrade() -> None:
    op.create_table(
        "image_studio_characters",
        _uuid("id", primary_key=True),
        _uuid("project_id"),
        _uuid("created_by", nullable=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("slug", sa.String(length=160), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("concept_tags", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        *_timestamps(),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("project_id", "slug", name="uq_image_studio_characters_project_slug"),
    )
    op.create_index("ix_image_studio_characters_project_id", "image_studio_characters", ["project_id"])
    op.create_index("ix_image_studio_characters_created_by", "image_studio_characters", ["created_by"])
    op.create_index("ix_image_studio_characters_project_created", "image_studio_characters", ["project_id", "created_at"])

    op.create_table(
        "image_studio_assets",
        _uuid("id", primary_key=True),
        _uuid("project_id"),
        _uuid("created_by", nullable=True),
        _uuid("generation_id", nullable=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("asset_type", sa.String(length=32), nullable=False, server_default="image"),
        sa.Column("storage_path", sa.String(length=1024), nullable=False),
        sa.Column("mime_type", sa.String(length=160), nullable=True),
        sa.Column("byte_size", sa.BigInteger(), nullable=True),
        sa.Column("checksum_sha256", sa.String(length=64), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("concept_tags", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        *_timestamps(),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["generation_id"], ["image_studio_generations.id"], ondelete="SET NULL"),
        sa.CheckConstraint("byte_size IS NULL OR byte_size >= 0", name="ck_image_studio_assets_byte_size"),
    )
    op.create_index("ix_image_studio_assets_project_id", "image_studio_assets", ["project_id"])
    op.create_index("ix_image_studio_assets_created_by", "image_studio_assets", ["created_by"])
    op.create_index("ix_image_studio_assets_generation_id", "image_studio_assets", ["generation_id"])
    op.create_index("ix_image_studio_assets_project_created", "image_studio_assets", ["project_id", "created_at"])
    op.create_index("ix_image_studio_assets_project_path", "image_studio_assets", ["project_id", "storage_path"])

    op.create_table(
        "image_studio_references",
        _uuid("id", primary_key=True),
        _uuid("project_id"),
        _uuid("created_by", nullable=True),
        _uuid("asset_id", nullable=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("reference_type", sa.String(length=32), nullable=False, server_default="image"),
        sa.Column("storage_path", sa.String(length=1024), nullable=True),
        sa.Column("source_url", sa.String(length=2048), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("concept_tags", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        *_timestamps(),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["asset_id"], ["image_studio_assets.id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            "storage_path IS NOT NULL OR source_url IS NOT NULL OR asset_id IS NOT NULL",
            name="ck_image_studio_references_target",
        ),
    )
    op.create_index("ix_image_studio_references_project_id", "image_studio_references", ["project_id"])
    op.create_index("ix_image_studio_references_created_by", "image_studio_references", ["created_by"])
    op.create_index("ix_image_studio_references_asset_id", "image_studio_references", ["asset_id"])
    op.create_index("ix_image_studio_references_project_created", "image_studio_references", ["project_id", "created_at"])

    op.create_table(
        "image_studio_concepts",
        _uuid("id", primary_key=True),
        _uuid("project_id"),
        _uuid("created_by", nullable=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("slug", sa.String(length=160), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("tags", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column("metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("concept_tags", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        *_timestamps(),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("project_id", "slug", name="uq_image_studio_concepts_project_slug"),
    )
    op.create_index("ix_image_studio_concepts_project_id", "image_studio_concepts", ["project_id"])
    op.create_index("ix_image_studio_concepts_created_by", "image_studio_concepts", ["created_by"])
    op.create_index("ix_image_studio_concepts_project_created", "image_studio_concepts", ["project_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_image_studio_concepts_project_created", table_name="image_studio_concepts")
    op.drop_index("ix_image_studio_concepts_created_by", table_name="image_studio_concepts")
    op.drop_index("ix_image_studio_concepts_project_id", table_name="image_studio_concepts")
    op.drop_table("image_studio_concepts")
    op.drop_index("ix_image_studio_references_project_created", table_name="image_studio_references")
    op.drop_index("ix_image_studio_references_asset_id", table_name="image_studio_references")
    op.drop_index("ix_image_studio_references_created_by", table_name="image_studio_references")
    op.drop_index("ix_image_studio_references_project_id", table_name="image_studio_references")
    op.drop_table("image_studio_references")
    op.drop_index("ix_image_studio_assets_project_path", table_name="image_studio_assets")
    op.drop_index("ix_image_studio_assets_project_created", table_name="image_studio_assets")
    op.drop_index("ix_image_studio_assets_generation_id", table_name="image_studio_assets")
    op.drop_index("ix_image_studio_assets_created_by", table_name="image_studio_assets")
    op.drop_index("ix_image_studio_assets_project_id", table_name="image_studio_assets")
    op.drop_table("image_studio_assets")
    op.drop_index("ix_image_studio_characters_project_created", table_name="image_studio_characters")
    op.drop_index("ix_image_studio_characters_created_by", table_name="image_studio_characters")
    op.drop_index("ix_image_studio_characters_project_id", table_name="image_studio_characters")
    op.drop_table("image_studio_characters")
