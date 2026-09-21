"""Add provenance and optimistic lifecycle fields to Project Q&A.

Project Q&A predates the review queue and only carried the lossy
``created_by_agent`` boolean.  Keep that column for compatibility, but add an
explicit origin plus a monotonically increasing version token so review and
cleanup operations can distinguish inferred rows from accepted/manual facts
and reject stale browser mutations safely.

The backfill is deliberately conservative: only legacy agent rows that are
still in the candidate/rejected review states are classified as
``legacy_auto``.  Accepted (or otherwise terminal) rows are marked ``manual``
even if an old agent writer set ``created_by_agent``; cleanup therefore cannot
silently remove durable accepted knowledge.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260901_0001"
down_revision = "20260831_0008"
branch_labels = None
depends_on = None


def _table_exists(bind: sa.Connection, table_name: str) -> bool:
    return table_name in sa.inspect(bind).get_table_names()


def _columns(bind: sa.Connection, table_name: str) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(table_name)}


def _indexes(bind: sa.Connection, table_name: str) -> set[str]:
    return {index["name"] for index in sa.inspect(bind).get_indexes(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    if not _table_exists(bind, "project_qa_entries"):
        # Older installations may have been provisioned without Project
        # Information.  Leave the migration idempotent; the foundation
        # migration will create the table when it is present in that lineage.
        return

    columns = _columns(bind, "project_qa_entries")
    if "origin" not in columns:
        # Add nullable first so PostgreSQL can install the column on a large
        # table without requiring a full-table default rewrite.  The
        # application backfill below is deterministic and then tightens the
        # invariant to NOT NULL with a safe default for rolling writers.
        op.add_column(
            "project_qa_entries",
            sa.Column("origin", sa.String(length=32), nullable=True),
        )
    if "version" not in columns:
        op.add_column(
            "project_qa_entries",
            sa.Column("version", sa.Integer(), nullable=True),
        )

    # Do not infer provenance from question text or encrypted payloads.  The
    # only trusted legacy signal is the existing boolean + review state.
    bind.execute(
        sa.text(
            """
            UPDATE project_qa_entries
            SET origin = CASE
                WHEN COALESCE(created_by_agent, false) = true
                 AND LOWER(COALESCE(review_state, '')) IN ('candidate', 'rejected')
                    THEN 'legacy_auto'
                ELSE 'manual'
            END
            WHERE origin IS NULL
            """
        )
    )
    bind.execute(
        sa.text(
            """
            UPDATE project_qa_entries
            SET version = 1
            WHERE version IS NULL OR version < 1
            """
        )
    )

    # Tighten only after every existing row has a valid value.  Keep a server
    # default during rolling deploys so old writers that omit the new fields
    # continue to produce safe rows.
    op.alter_column(
        "project_qa_entries",
        "origin",
        existing_type=sa.String(length=32),
        nullable=False,
        server_default=sa.text("'manual'"),
    )
    op.alter_column(
        "project_qa_entries",
        "version",
        existing_type=sa.Integer(),
        nullable=False,
        server_default=sa.text("1"),
    )

    indexes = _indexes(bind, "project_qa_entries")
    if "ix_project_qa_entries_origin" not in indexes:
        op.create_index(
            "ix_project_qa_entries_origin",
            "project_qa_entries",
            ["origin"],
        )
    if "ix_project_qa_entries_project_origin_review" not in indexes:
        op.create_index(
            "ix_project_qa_entries_project_origin_review",
            "project_qa_entries",
            ["project_id", "origin", "review_state", "deleted_at"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    if not _table_exists(bind, "project_qa_entries"):
        return

    indexes = _indexes(bind, "project_qa_entries")
    if "ix_project_qa_entries_project_origin_review" in indexes:
        op.drop_index(
            "ix_project_qa_entries_project_origin_review",
            table_name="project_qa_entries",
        )
    if "ix_project_qa_entries_origin" in indexes:
        op.drop_index("ix_project_qa_entries_origin", table_name="project_qa_entries")

    columns = _columns(bind, "project_qa_entries")
    if "version" in columns:
        op.drop_column("project_qa_entries", "version")
    if "origin" in columns:
        op.drop_column("project_qa_entries", "origin")
