"""Add story_illustrations table and work image_settings."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260818_0003"
down_revision = "20260818_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "story_works",
        sa.Column(
            "image_settings",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
    )
    op.create_table(
        "story_illustrations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("work_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("episode_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("body_etag", sa.Text(), nullable=False),
        sa.Column("rev_no", sa.Integer(), nullable=True),
        sa.Column("anchor_kind", sa.String(length=32), nullable=True),
        sa.Column("anchor_quote", sa.Text(), nullable=False),
        sa.Column("offset_hint", sa.Integer(), nullable=True),
        sa.Column("ordering", sa.Integer(), nullable=False),
        sa.Column("scene_description", sa.Text(), nullable=True),
        sa.Column("visual_prompt", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("generated_media_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["episode_id"], ["story_episodes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["work_id"], ["story_works.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_story_illustrations_episode_ordering",
        "story_illustrations",
        ["episode_id", "ordering"],
        unique=False,
    )
    op.create_index(
        "ix_story_illustrations_work_id",
        "story_illustrations",
        ["work_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_story_illustrations_work_id", table_name="story_illustrations")
    op.drop_index("ix_story_illustrations_episode_ordering", table_name="story_illustrations")
    op.drop_table("story_illustrations")
    op.drop_column("story_works", "image_settings")
