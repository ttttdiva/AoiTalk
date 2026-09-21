"""Extend MediaOps research/editorial records with durable recurrence state.

The preceding WS3 migration intentionally kept the first implementation
manual-only.  This revision adds the durable schedule/policy fields and the
bounded ResearchCandidate ledger without introducing a second scraper or
publication queue.  Existing rows receive conservative manual/draft defaults.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260901_0012"
down_revision = "20260901_0011"
branch_labels = None
depends_on = None


def _uuid() -> postgresql.UUID:
    return postgresql.UUID(as_uuid=True)


def _is_sqlite() -> bool:
    bind = op.get_bind()
    return str(getattr(getattr(bind, "dialect", None), "name", "")).lower() == "sqlite"


def _add_columns(table: str, columns: list[sa.Column]) -> None:
    if _is_sqlite():
        with op.batch_alter_table(table, recreate="always") as batch:
            for column in columns:
                batch.add_column(column)
    else:
        for column in columns:
            op.add_column(table, column)


def _create_index(name: str, table: str, columns: list[str]) -> None:
    op.create_index(name, table, columns)


def upgrade() -> None:
    _add_columns(
        "media_platform_accounts",
        [
            sa.Column(
                "persona_id",
                _uuid(),
                sa.ForeignKey(
                    "media_personas.id",
                    name="fk_media_platform_accounts_persona_id",
                    ondelete="SET NULL",
                ),
                nullable=True,
            )
        ],
    )
    _create_index(
        "ix_media_platform_accounts_persona_id",
        "media_platform_accounts",
        ["persona_id"],
    )

    _add_columns(
        "media_research_routines",
        [
            sa.Column(
                "persona_id",
                _uuid(),
                sa.ForeignKey(
                    "media_personas.id",
                    name="fk_media_research_routines_persona_id",
                    ondelete="SET NULL",
                ),
                nullable=True,
            ),
            sa.Column("state", sa.String(16), nullable=False, server_default="draft"),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column(
                "platform_account_id",
                _uuid(),
                sa.ForeignKey(
                    "media_platform_accounts.id",
                    name="fk_media_research_routines_platform_account_id",
                    ondelete="SET NULL",
                ),
                nullable=True,
            ),
            sa.Column("last_due_at", sa.DateTime(), nullable=True),
            sa.Column("next_due_at", sa.DateTime(), nullable=True),
        ],
    )
    for name, columns in {
        "ix_media_research_routines_persona_id": ["persona_id"],
        "ix_media_research_routines_state": ["state"],
        "ix_media_research_routines_enabled": ["enabled"],
        "ix_media_research_routines_platform_account_id": ["platform_account_id"],
        "ix_media_research_routines_last_due_at": ["last_due_at"],
        "ix_media_research_routines_next_due_at": ["next_due_at"],
    }.items():
        _create_index(name, "media_research_routines", columns)

    _add_columns(
        "media_research_routine_revisions",
        [
            sa.Column("cadence", sa.String(32), nullable=False, server_default="manual"),
            sa.Column("timezone", sa.String(64), nullable=False, server_default="UTC"),
            sa.Column("schedule", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("source_types", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
            sa.Column("search_queries", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
            sa.Column("domains", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
            sa.Column("follow_accounts", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
            sa.Column("follow_tags", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
            sa.Column("exclusions", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
            sa.Column("freshness_hours", sa.Integer(), nullable=False, server_default="168"),
            sa.Column("max_candidates", sa.Integer(), nullable=False, server_default="20"),
            sa.Column("review_policy", sa.String(64), nullable=False, server_default="human_review"),
        ],
    )

    _add_columns(
        "media_research_runs",
        [
            sa.Column("status", sa.String(16), nullable=False, server_default="recorded"),
            sa.Column("started_at", sa.DateTime(), nullable=True),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.Column("source_refs", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
            sa.Column("omissions", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        ],
    )
    for name, columns in {
        "ix_media_research_runs_status": ["status"],
        "ix_media_research_runs_started_at": ["started_at"],
        "ix_media_research_runs_finished_at": ["finished_at"],
    }.items():
        _create_index(name, "media_research_runs", columns)

    _add_columns(
        "media_editorial_programs",
        [
            sa.Column("state", sa.String(16), nullable=False, server_default="draft"),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("last_due_at", sa.DateTime(), nullable=True),
            sa.Column("next_due_at", sa.DateTime(), nullable=True),
        ],
    )
    for name, columns in {
        "ix_media_editorial_programs_state": ["state"],
        "ix_media_editorial_programs_enabled": ["enabled"],
        "ix_media_editorial_programs_last_due_at": ["last_due_at"],
        "ix_media_editorial_programs_next_due_at": ["next_due_at"],
    }.items():
        _create_index(name, "media_editorial_programs", columns)

    _add_columns(
        "media_editorial_program_revisions",
        [
            sa.Column("content_type", sa.String(64), nullable=False, server_default="article"),
            sa.Column("cadence", sa.String(32), nullable=False, server_default="manual"),
            sa.Column("target_platforms", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
            sa.Column("target_account_refs", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
            sa.Column("content_pillar", sa.String(200), nullable=True),
            sa.Column("required_resources", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
            sa.Column("default_creative_recipe_ref", sa.String(164), nullable=True),
            sa.Column("default_qa_policy_ref", sa.String(164), nullable=True),
            sa.Column("experiment_ref", sa.String(164), nullable=True),
            sa.Column("draft_generation_policy", sa.String(64), nullable=False, server_default="human_review"),
        ],
    )

    _add_columns(
        "media_content_items",
        [
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
            sa.Column(
                "persona_revision_id",
                _uuid(),
                sa.ForeignKey(
                    "media_persona_revisions.id",
                    name="fk_media_content_items_persona_revision_id",
                    ondelete="SET NULL",
                ),
                nullable=True,
            ),
            sa.Column("objective", sa.Text(), nullable=True),
            sa.Column("content_type", sa.String(64), nullable=False, server_default="article"),
            sa.Column("content_pillar", sa.String(200), nullable=True),
            sa.Column("intended_audience", sa.Text(), nullable=True),
            sa.Column("source_refs", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
            sa.Column("candidate_refs", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
            sa.Column("desired_assets", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
            sa.Column("monetization_ref", sa.String(164), nullable=True),
            sa.Column("experiment_ref", sa.String(164), nullable=True),
            sa.Column("scheduled_at", sa.DateTime(), nullable=True),
        ],
    )
    for name, columns in {
        "ix_media_content_items_status": ["status"],
        "ix_media_content_items_persona_revision_id": ["persona_revision_id"],
        "ix_media_content_items_scheduled_at": ["scheduled_at"],
    }.items():
        _create_index(name, "media_content_items", columns)

    op.create_table(
        "media_research_candidates",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("owner_user_id", _uuid(), nullable=False),
        sa.Column("project_id", _uuid(), nullable=True),
        sa.Column("research_routine_id", _uuid(), nullable=False),
        sa.Column("research_run_id", _uuid(), nullable=False),
        sa.Column("routine_revision_id", _uuid(), nullable=False),
        sa.Column("candidate_key", sa.String(64), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("source_published_at", sa.DateTime(), nullable=True),
        sa.Column("discovered_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("relevance_score", sa.Float(), nullable=True),
        sa.Column("freshness_score", sa.Float(), nullable=True),
        sa.Column("evidence", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="discovered"),
        sa.Column("content_item_id", _uuid(), nullable=True),
        sa.Column("candidate_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("created_by", _uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('discovered', 'triaged', 'accepted', 'rejected', 'expired', 'promoted')",
            name="ck_media_research_candidates_status",
        ),
        sa.CheckConstraint("candidate_key <> ''", name="ck_media_research_candidates_key"),
        sa.CheckConstraint("length(candidate_hash) = 64", name="ck_media_research_candidates_hash"),
        sa.CheckConstraint(
            "relevance_score IS NULL OR (relevance_score >= 0 AND relevance_score <= 1)",
            name="ck_media_research_candidates_relevance",
        ),
        sa.CheckConstraint(
            "freshness_score IS NULL OR (freshness_score >= 0 AND freshness_score <= 1)",
            name="ck_media_research_candidates_freshness",
        ),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["research_routine_id"], ["media_research_routines.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["research_run_id"], ["media_research_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["routine_revision_id"], ["media_research_routine_revisions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["content_item_id"], ["media_content_items.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("research_routine_id", "candidate_key", name="uq_media_research_candidates_identity"),
        sa.UniqueConstraint("research_run_id", "idempotency_key", name="uq_media_research_candidates_idempotency"),
    )
    for name, columns in {
        "ix_media_research_candidates_owner_user_id": ["owner_user_id"],
        "ix_media_research_candidates_project_id": ["project_id"],
        "ix_media_research_candidates_routine_id": ["research_routine_id"],
        "ix_media_research_candidates_run_id": ["research_run_id"],
        "ix_media_research_candidates_revision_id": ["routine_revision_id"],
        "ix_media_research_candidates_key": ["candidate_key"],
        "ix_media_research_candidates_discovered_at": ["discovered_at"],
        "ix_media_research_candidates_expires_at": ["expires_at"],
        "ix_media_research_candidates_status": ["status"],
        "ix_media_research_candidates_content_item_id": ["content_item_id"],
        "ix_media_research_candidates_hash": ["candidate_hash"],
        "ix_media_research_candidates_created_at": ["created_at"],
        "ix_media_research_candidates_owner_project": ["owner_user_id", "project_id"],
    }.items():
        _create_index(name, "media_research_candidates", columns)


def downgrade() -> None:
    op.drop_table("media_research_candidates")
    for table, indexes in {
        "media_platform_accounts": ["ix_media_platform_accounts_persona_id"],
        "media_content_items": [
            "ix_media_content_items_scheduled_at",
            "ix_media_content_items_persona_revision_id",
            "ix_media_content_items_status",
        ],
        "media_editorial_programs": [
            "ix_media_editorial_programs_next_due_at",
            "ix_media_editorial_programs_last_due_at",
            "ix_media_editorial_programs_enabled",
            "ix_media_editorial_programs_state",
        ],
        "media_research_runs": [
            "ix_media_research_runs_finished_at",
            "ix_media_research_runs_started_at",
            "ix_media_research_runs_status",
        ],
        "media_research_routines": [
            "ix_media_research_routines_next_due_at",
            "ix_media_research_routines_last_due_at",
            "ix_media_research_routines_platform_account_id",
            "ix_media_research_routines_enabled",
            "ix_media_research_routines_state",
            "ix_media_research_routines_persona_id",
        ],
    }.items():
        for index in indexes:
            op.drop_index(index, table_name=table)

    # The remaining columns are nullable/defaulted extensions.  Dropping via
    # batch mode keeps SQLite development databases usable; PostgreSQL follows
    # the direct ALTER path.
    column_map = {
        "media_content_items": [
            "scheduled_at", "experiment_ref", "monetization_ref", "desired_assets",
            "candidate_refs", "source_refs", "intended_audience", "content_pillar",
            "content_type", "objective", "persona_revision_id", "status", "version",
        ],
        "media_editorial_program_revisions": [
            "draft_generation_policy", "experiment_ref", "default_qa_policy_ref",
            "default_creative_recipe_ref", "required_resources", "content_pillar",
            "target_account_refs", "target_platforms", "cadence", "content_type",
        ],
        "media_editorial_programs": ["next_due_at", "last_due_at", "enabled", "state"],
        "media_research_runs": ["omissions", "source_refs", "finished_at", "started_at", "status"],
        "media_research_routine_revisions": [
            "review_policy", "max_candidates", "freshness_hours", "exclusions",
            "follow_tags", "follow_accounts", "search_queries", "domains", "source_types",
            "schedule", "timezone", "cadence",
        ],
        "media_research_routines": ["next_due_at", "last_due_at", "platform_account_id", "enabled", "state", "persona_id"],
        "media_platform_accounts": ["persona_id"],
    }
    if _is_sqlite():
        for table, columns in column_map.items():
            with op.batch_alter_table(table, recreate="always") as batch:
                for column in columns:
                    batch.drop_column(column)
    else:
        for table, columns in column_map.items():
            for column in columns:
                op.drop_column(table, column)
