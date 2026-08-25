"""Scenario Studio の story_* 正本テーブルを作成する。

旧 scenario / Docs テーブルは削除しない。データ移行と旧テーブルの drop は
別手順で行う。
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260802_0004"
down_revision = "20260802_0003"
branch_labels = None
depends_on = None


def _uuid() -> postgresql.UUID:
    return postgresql.UUID(as_uuid=True)


def _jsonb(name: str, default: str) -> sa.Column:
    return sa.Column(
        name,
        postgresql.JSONB(),
        nullable=False,
        server_default=sa.text(default),
    )


def upgrade() -> None:
    op.create_table(
        "story_works",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("user_id", _uuid(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("synopsis", sa.Text(), nullable=True),
        sa.Column("plot", sa.Text(), nullable=True),
        sa.Column("style_guide", sa.Text(), nullable=True),
        sa.Column("kind", sa.String(length=20), nullable=False, server_default="novel"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="planning"),
        sa.Column("target_episode_chars", sa.Integer(), nullable=False, server_default="6000"),
        sa.Column("planned_episode_count", sa.Integer(), nullable=True),
        sa.Column("start_episode_id", _uuid(), nullable=True),
        _jsonb("ui_state", "'{}'::jsonb"),
        _jsonb("model_override", "'{}'::jsonb"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_story_works_user_id", "story_works", ["user_id"])
    op.create_index("ix_story_works_user_status", "story_works", ["user_id", "status"])

    op.create_table(
        "story_episodes",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("work_id", _uuid(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("plot", sa.Text(), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("body_etag", sa.String(length=71), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("summary_locked", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("premise_note", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="unwritten"),
        sa.Column("target_chars", sa.Integer(), nullable=True),
        sa.Column("char_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("map_x", sa.Float(), nullable=True),
        sa.Column("map_y", sa.Float(), nullable=True),
        sa.Column("sort_hint", sa.Float(), nullable=False, server_default="0"),
        sa.Column("current_rev_no", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["work_id"], ["story_works.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_story_episodes_work_id", "story_episodes", ["work_id"])
    op.create_index("ix_story_episodes_work_updated", "story_episodes", ["work_id", "updated_at"])
    op.create_index("ix_story_episodes_work_sort", "story_episodes", ["work_id", "sort_hint"])
    op.create_foreign_key(
        "fk_story_works_start_episode_id",
        "story_works",
        "story_episodes",
        ["start_episode_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "story_links",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("work_id", _uuid(), nullable=False),
        sa.Column("from_episode_id", _uuid(), nullable=False),
        sa.Column("to_episode_id", _uuid(), nullable=False),
        sa.Column("choice_label", sa.Text(), nullable=True),
        sa.Column("position", sa.Float(), nullable=False, server_default="0"),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["work_id"], ["story_works.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["from_episode_id"], ["story_episodes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["to_episode_id"], ["story_episodes.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("from_episode_id", "to_episode_id", name="uq_story_links_from_to"),
        sa.CheckConstraint("from_episode_id <> to_episode_id", name="ck_story_links_no_self_loop"),
    )
    op.create_index("ix_story_links_work_id", "story_links", ["work_id"])
    op.create_index("ix_story_links_work_from", "story_links", ["work_id", "from_episode_id"])
    op.create_index("ix_story_links_work_to", "story_links", ["work_id", "to_episode_id"])

    op.create_table(
        "story_characters",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("user_id", _uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        _jsonb("aliases", "'[]'::jsonb"),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("ai_mode", sa.String(length=20), nullable=False, server_default="keyword"),
        _jsonb("keywords", "'[]'::jsonb"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_story_characters_user_id", "story_characters", ["user_id"])

    op.create_table(
        "story_work_characters",
        sa.Column("work_id", _uuid(), primary_key=True),
        sa.Column("character_id", _uuid(), primary_key=True),
        sa.Column("role_note", sa.Text(), nullable=True),
        sa.Column("position", sa.Float(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["work_id"], ["story_works.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["character_id"], ["story_characters.id"], ondelete="CASCADE"),
    )

    op.create_table(
        "story_rulebooks",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("user_id", _uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_story_rulebooks_user_id", "story_rulebooks", ["user_id"])

    op.create_table(
        "story_work_rulebooks",
        sa.Column("work_id", _uuid(), primary_key=True),
        sa.Column("rulebook_id", _uuid(), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("position", sa.Float(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["work_id"], ["story_works.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["rulebook_id"], ["story_rulebooks.id"], ondelete="CASCADE"),
    )

    op.create_table(
        "story_notes",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("work_id", _uuid(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("ai_mode", sa.String(length=20), nullable=False, server_default="keyword"),
        _jsonb("keywords", "'[]'::jsonb"),
        sa.Column("position", sa.Float(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["work_id"], ["story_works.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_story_notes_work_id", "story_notes", ["work_id"])

    op.create_table(
        "story_episode_revisions",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("episode_id", _uuid(), nullable=False),
        sa.Column("rev_no", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("plot", sa.Text(), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("origin", sa.String(length=30), nullable=False),
        sa.Column("body_sha256", sa.String(length=64), nullable=False),
        sa.Column("char_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_by", sa.String(length=20), nullable=False, server_default="user"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["episode_id"], ["story_episodes.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("episode_id", "rev_no", name="uq_story_episode_revisions_episode_rev"),
    )
    op.create_index("ix_story_episode_revisions_episode_id", "story_episode_revisions", ["episode_id"])
    # §5.6: 履歴一覧は rev_no 降順で引くため、索引も降順で作る。
    op.create_index(
        "ix_story_episode_revisions_episode_rev_desc",
        "story_episode_revisions",
        ["episode_id", sa.text("rev_no DESC")],
    )

    op.create_table(
        "story_search_index",
        sa.Column("episode_id", _uuid(), primary_key=True),
        sa.Column("work_id", _uuid(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body_plain", sa.Text(), nullable=False, server_default=""),
        sa.ForeignKeyConstraint(["episode_id"], ["story_episodes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["work_id"], ["story_works.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_story_search_index_work_id", "story_search_index", ["work_id"])

    op.create_table(
        "story_generation_jobs",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("work_id", _uuid(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        _jsonb("payload", "'{}'::jsonb"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="queued"),
        _jsonb("progress", "'{}'::jsonb"),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["work_id"], ["story_works.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_story_generation_jobs_work_id", "story_generation_jobs", ["work_id"])
    op.create_index("ix_story_generation_jobs_work_status", "story_generation_jobs", ["work_id", "status"])

    op.create_table(
        "story_writing_sessions",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("work_id", _uuid(), nullable=False),
        sa.Column("episode_id", _uuid(), nullable=True),
        sa.Column("conversation_session_id", _uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["work_id"], ["story_works.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["episode_id"], ["story_episodes.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["conversation_session_id"], ["conversation_sessions.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_story_writing_sessions_work_id", "story_writing_sessions", ["work_id"])
    op.create_index("ix_story_writing_sessions_episode_id", "story_writing_sessions", ["episode_id"])
    op.create_index(
        "ix_story_writing_sessions_conversation_session_id",
        "story_writing_sessions",
        ["conversation_session_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_story_writing_sessions_conversation_session_id", table_name="story_writing_sessions")
    op.drop_index("ix_story_writing_sessions_episode_id", table_name="story_writing_sessions")
    op.drop_index("ix_story_writing_sessions_work_id", table_name="story_writing_sessions")
    op.drop_table("story_writing_sessions")

    op.drop_index("ix_story_generation_jobs_work_status", table_name="story_generation_jobs")
    op.drop_index("ix_story_generation_jobs_work_id", table_name="story_generation_jobs")
    op.drop_table("story_generation_jobs")

    op.drop_index("ix_story_search_index_work_id", table_name="story_search_index")
    op.drop_table("story_search_index")

    op.drop_index("ix_story_episode_revisions_episode_rev_desc", table_name="story_episode_revisions")
    op.drop_index("ix_story_episode_revisions_episode_id", table_name="story_episode_revisions")
    op.drop_table("story_episode_revisions")

    op.drop_index("ix_story_notes_work_id", table_name="story_notes")
    op.drop_table("story_notes")
    op.drop_table("story_work_rulebooks")
    op.drop_index("ix_story_rulebooks_user_id", table_name="story_rulebooks")
    op.drop_table("story_rulebooks")
    op.drop_table("story_work_characters")
    op.drop_index("ix_story_characters_user_id", table_name="story_characters")
    op.drop_table("story_characters")

    op.drop_index("ix_story_links_work_to", table_name="story_links")
    op.drop_index("ix_story_links_work_from", table_name="story_links")
    op.drop_index("ix_story_links_work_id", table_name="story_links")
    op.drop_table("story_links")

    op.drop_constraint("fk_story_works_start_episode_id", "story_works", type_="foreignkey")
    op.drop_index("ix_story_episodes_work_sort", table_name="story_episodes")
    op.drop_index("ix_story_episodes_work_updated", table_name="story_episodes")
    op.drop_index("ix_story_episodes_work_id", table_name="story_episodes")
    op.drop_table("story_episodes")

    op.drop_index("ix_story_works_user_status", table_name="story_works")
    op.drop_index("ix_story_works_user_id", table_name="story_works")
    op.drop_table("story_works")
