"""Add Image Studio shot, branch and revision tables."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260806_0004"
down_revision = "20260806_0003"
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


def upgrade() -> None:
    op.create_table(
        "image_studio_shots",
        _uuid("id", primary_key=True),
        _uuid("project_id"),
        _uuid("created_by", nullable=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="draft"),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("concept_tags", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            "status IN ('draft','active','archived')",
            name="ck_image_studio_shots_status",
        ),
    )
    op.create_index("ix_image_studio_shots_project_id", "image_studio_shots", ["project_id"])
    op.create_index("ix_image_studio_shots_created_by", "image_studio_shots", ["created_by"])
    op.create_index("ix_image_studio_shots_status", "image_studio_shots", ["status"])
    op.create_index(
        "ix_image_studio_shots_project_created",
        "image_studio_shots",
        ["project_id", "created_at"],
    )

    op.create_table(
        "image_studio_shot_branches",
        _uuid("id", primary_key=True),
        _uuid("project_id"),
        _uuid("shot_id"),
        _uuid("parent_branch_id", nullable=True),
        _uuid("created_by", nullable=True),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("slug", sa.String(length=160), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="draft"),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["shot_id"], ["image_studio_shots.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["parent_branch_id"],
            ["image_studio_shot_branches.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint(
            "shot_id",
            "slug",
            name="uq_image_studio_shot_branches_shot_slug",
        ),
        sa.CheckConstraint(
            "status IN ('draft','active','archived')",
            name="ck_image_studio_shot_branches_status",
        ),
        sa.CheckConstraint(
            "parent_branch_id IS NULL OR parent_branch_id <> id",
            name="ck_image_studio_shot_branches_no_self_parent",
        ),
    )
    op.create_index("ix_image_studio_shot_branches_project_id", "image_studio_shot_branches", ["project_id"])
    op.create_index("ix_image_studio_shot_branches_shot_id", "image_studio_shot_branches", ["shot_id"])
    op.create_index("ix_image_studio_shot_branches_parent_branch_id", "image_studio_shot_branches", ["parent_branch_id"])
    op.create_index("ix_image_studio_shot_branches_created_by", "image_studio_shot_branches", ["created_by"])
    op.create_index("ix_image_studio_shot_branches_status", "image_studio_shot_branches", ["status"])
    op.create_index(
        "ix_image_studio_shot_branches_project_created",
        "image_studio_shot_branches",
        ["project_id", "created_at"],
    )

    op.create_table(
        "image_studio_shot_revisions",
        _uuid("id", primary_key=True),
        _uuid("project_id"),
        _uuid("shot_id"),
        _uuid("branch_id"),
        _uuid("parent_revision_id", nullable=True),
        _uuid("created_by", nullable=True),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["shot_id"], ["image_studio_shots.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["branch_id"], ["image_studio_shot_branches.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["parent_revision_id"],
            ["image_studio_shot_revisions.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint(
            "branch_id",
            "revision_no",
            name="uq_image_studio_shot_revisions_branch_no",
        ),
        sa.CheckConstraint(
            "revision_no >= 1",
            name="ck_image_studio_shot_revisions_revision_no",
        ),
    )
    op.create_index("ix_image_studio_shot_revisions_project_id", "image_studio_shot_revisions", ["project_id"])
    op.create_index("ix_image_studio_shot_revisions_shot_id", "image_studio_shot_revisions", ["shot_id"])
    op.create_index("ix_image_studio_shot_revisions_branch_id", "image_studio_shot_revisions", ["branch_id"])
    op.create_index("ix_image_studio_shot_revisions_parent_revision_id", "image_studio_shot_revisions", ["parent_revision_id"])
    op.create_index("ix_image_studio_shot_revisions_created_by", "image_studio_shot_revisions", ["created_by"])
    op.create_index(
        "ix_image_studio_shot_revisions_project_created",
        "image_studio_shot_revisions",
        ["project_id", "created_at"],
    )
    op.create_index(
        "ix_image_studio_shot_revisions_branch_revision",
        "image_studio_shot_revisions",
        ["branch_id", "revision_no"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_image_studio_shot_revisions_branch_revision",
        table_name="image_studio_shot_revisions",
    )
    op.drop_index(
        "ix_image_studio_shot_revisions_project_created",
        table_name="image_studio_shot_revisions",
    )
    op.drop_index(
        "ix_image_studio_shot_revisions_created_by",
        table_name="image_studio_shot_revisions",
    )
    op.drop_index(
        "ix_image_studio_shot_revisions_parent_revision_id",
        table_name="image_studio_shot_revisions",
    )
    op.drop_index(
        "ix_image_studio_shot_revisions_branch_id",
        table_name="image_studio_shot_revisions",
    )
    op.drop_index(
        "ix_image_studio_shot_revisions_shot_id",
        table_name="image_studio_shot_revisions",
    )
    op.drop_index(
        "ix_image_studio_shot_revisions_project_id",
        table_name="image_studio_shot_revisions",
    )
    op.drop_table("image_studio_shot_revisions")

    op.drop_index(
        "ix_image_studio_shot_branches_project_created",
        table_name="image_studio_shot_branches",
    )
    op.drop_index(
        "ix_image_studio_shot_branches_status",
        table_name="image_studio_shot_branches",
    )
    op.drop_index(
        "ix_image_studio_shot_branches_created_by",
        table_name="image_studio_shot_branches",
    )
    op.drop_index(
        "ix_image_studio_shot_branches_parent_branch_id",
        table_name="image_studio_shot_branches",
    )
    op.drop_index(
        "ix_image_studio_shot_branches_shot_id",
        table_name="image_studio_shot_branches",
    )
    op.drop_index(
        "ix_image_studio_shot_branches_project_id",
        table_name="image_studio_shot_branches",
    )
    op.drop_table("image_studio_shot_branches")

    op.drop_index(
        "ix_image_studio_shots_project_created",
        table_name="image_studio_shots",
    )
    op.drop_index("ix_image_studio_shots_status", table_name="image_studio_shots")
    op.drop_index("ix_image_studio_shots_created_by", table_name="image_studio_shots")
    op.drop_index("ix_image_studio_shots_project_id", table_name="image_studio_shots")
    op.drop_table("image_studio_shots")
