"""Add Image Studio evaluations, presets and retrieval cases."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260806_0005"
down_revision = "20260806_0004"
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
        "image_studio_evaluations",
        _uuid("id", primary_key=True),
        _uuid("project_id"),
        _uuid("generation_id"),
        sa.Column("output_id", sa.String(length=160), nullable=True),
        _uuid("asset_id", nullable=True),
        _uuid("created_by", nullable=True),
        sa.Column("decision", sa.String(length=24), nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("correction_reason", sa.Text(), nullable=True),
        _json("metadata", "{}"),
        _json("concept_tags", "[]"),
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
        sa.CheckConstraint(
            "decision IN ('accepted','rejected','needs_revision')",
            name="ck_image_studio_evaluations_decision",
        ),
        sa.CheckConstraint(
            "score IS NULL OR (score >= 0 AND score <= 1)",
            name="ck_image_studio_evaluations_score",
        ),
    )
    op.create_index("ix_image_studio_evaluations_project_id", "image_studio_evaluations", ["project_id"])
    op.create_index("ix_image_studio_evaluations_generation_id", "image_studio_evaluations", ["generation_id"])
    op.create_index("ix_image_studio_evaluations_output_id", "image_studio_evaluations", ["output_id"])
    op.create_index("ix_image_studio_evaluations_asset_id", "image_studio_evaluations", ["asset_id"])
    op.create_index("ix_image_studio_evaluations_created_by", "image_studio_evaluations", ["created_by"])
    op.create_index("ix_image_studio_evaluations_project_created", "image_studio_evaluations", ["project_id", "created_at"])

    op.create_table(
        "image_studio_presets",
        _uuid("id", primary_key=True),
        _uuid("project_id"),
        _uuid("created_by", nullable=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("slug", sa.String(length=160), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("prompt", sa.Text(), nullable=False, server_default=""),
        sa.Column("negative_prompt", sa.Text(), nullable=False, server_default=""),
        sa.Column("workflow_key", sa.String(length=160), nullable=True),
        sa.Column("workflow_version", sa.String(length=80), nullable=True),
        _json("parameters", "{}"),
        _json("metadata", "{}"),
        _json("concept_tags", "[]"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint(
            "project_id",
            "slug",
            name="uq_image_studio_presets_project_slug",
        ),
    )
    op.create_index("ix_image_studio_presets_project_id", "image_studio_presets", ["project_id"])
    op.create_index("ix_image_studio_presets_created_by", "image_studio_presets", ["created_by"])
    op.create_index("ix_image_studio_presets_project_created", "image_studio_presets", ["project_id", "created_at"])
    op.create_index("ix_image_studio_presets_project_active", "image_studio_presets", ["project_id", "is_active"])

    op.create_table(
        "image_studio_cases",
        _uuid("id", primary_key=True),
        _uuid("project_id"),
        _uuid("evaluation_id", nullable=True),
        _uuid("generation_id", nullable=True),
        sa.Column("output_id", sa.String(length=160), nullable=True),
        _uuid("asset_id", nullable=True),
        _uuid("created_by", nullable=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        _json("snapshot", "{}"),
        _json("metadata", "{}"),
        _json("concept_tags", "[]"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["evaluation_id"],
            ["image_studio_evaluations.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["generation_id"],
            ["image_studio_generations.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["image_studio_assets.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            "outcome IN ('success','failure')",
            name="ck_image_studio_cases_outcome",
        ),
        sa.CheckConstraint(
            "score IS NULL OR (score >= 0 AND score <= 1)",
            name="ck_image_studio_cases_score",
        ),
    )
    op.create_index("ix_image_studio_cases_project_id", "image_studio_cases", ["project_id"])
    op.create_index("ix_image_studio_cases_evaluation_id", "image_studio_cases", ["evaluation_id"])
    op.create_index("ix_image_studio_cases_generation_id", "image_studio_cases", ["generation_id"])
    op.create_index("ix_image_studio_cases_output_id", "image_studio_cases", ["output_id"])
    op.create_index("ix_image_studio_cases_asset_id", "image_studio_cases", ["asset_id"])
    op.create_index("ix_image_studio_cases_created_by", "image_studio_cases", ["created_by"])
    op.create_index("ix_image_studio_cases_project_created", "image_studio_cases", ["project_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_image_studio_cases_project_created", table_name="image_studio_cases")
    op.drop_index("ix_image_studio_cases_created_by", table_name="image_studio_cases")
    op.drop_index("ix_image_studio_cases_asset_id", table_name="image_studio_cases")
    op.drop_index("ix_image_studio_cases_output_id", table_name="image_studio_cases")
    op.drop_index("ix_image_studio_cases_generation_id", table_name="image_studio_cases")
    op.drop_index("ix_image_studio_cases_evaluation_id", table_name="image_studio_cases")
    op.drop_index("ix_image_studio_cases_project_id", table_name="image_studio_cases")
    op.drop_table("image_studio_cases")

    op.drop_index("ix_image_studio_presets_project_active", table_name="image_studio_presets")
    op.drop_index("ix_image_studio_presets_project_created", table_name="image_studio_presets")
    op.drop_index("ix_image_studio_presets_created_by", table_name="image_studio_presets")
    op.drop_index("ix_image_studio_presets_project_id", table_name="image_studio_presets")
    op.drop_table("image_studio_presets")

    op.drop_index("ix_image_studio_evaluations_project_created", table_name="image_studio_evaluations")
    op.drop_index("ix_image_studio_evaluations_created_by", table_name="image_studio_evaluations")
    op.drop_index("ix_image_studio_evaluations_asset_id", table_name="image_studio_evaluations")
    op.drop_index("ix_image_studio_evaluations_output_id", table_name="image_studio_evaluations")
    op.drop_index("ix_image_studio_evaluations_generation_id", table_name="image_studio_evaluations")
    op.drop_index("ix_image_studio_evaluations_project_id", table_name="image_studio_evaluations")
    op.drop_table("image_studio_evaluations")
