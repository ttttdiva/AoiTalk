"""Add Discord source-message idempotency across session rotation.

Revision ``20260808_0014`` is already deployed in environments where the
Scoped Memory principal settings table exists, so the job-column change lives
in this follow-up revision.  The guarded DDL also tolerates a partially
applied 0014 from the development window.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260808_0016"
down_revision = "20260808_0015"
branch_labels = None
depends_on = None


def _is_offline() -> bool:
    """Return whether Alembic supplied its ``--sql`` mock connection."""

    try:
        sa.inspect(op.get_bind())
    except sa.exc.NoInspectionAvailable:
        return True
    return False


def _columns(table_name: str) -> set[str]:
    # A MockConnection has no schema to inspect.  The migration chain is
    # linear, so an empty set is the conservative answer for offline upgrade
    # generation and causes the required ADD COLUMN to be emitted.
    if _is_offline():
        return set()
    bind = op.get_bind()
    return {str(item["name"]) for item in sa.inspect(bind).get_columns(table_name)}


def _indexes(table_name: str) -> set[str]:
    if _is_offline():
        return set()
    bind = op.get_bind()
    return {str(item["name"]) for item in sa.inspect(bind).get_indexes(table_name)}


def upgrade() -> None:
    columns = _columns("scoped_memory_jobs")
    if "source_message_id" not in columns:
        op.add_column(
            "scoped_memory_jobs",
            sa.Column("source_message_id", sa.String(length=255), nullable=True),
        )

    indexes = _indexes("scoped_memory_jobs")
    if "ix_scoped_memory_jobs_source_message_id" not in indexes:
        op.create_index(
            "ix_scoped_memory_jobs_source_message_id",
            "scoped_memory_jobs",
            ["source_message_id"],
        )
    if "uq_scoped_memory_jobs_external_message" not in indexes:
        op.create_index(
            "uq_scoped_memory_jobs_external_message",
            "scoped_memory_jobs",
            ["user_id", "source_message_id"],
            unique=True,
            postgresql_where=sa.text(
                "user_id LIKE 'discord:%' AND source_message_id IS NOT NULL"
            ),
            sqlite_where=sa.text(
                "user_id LIKE 'discord:%' AND source_message_id IS NOT NULL"
            ),
        )


def downgrade() -> None:
    if _is_offline():
        # In offline mode no catalog queries are available, so emit the full
        # reverse DDL for the linear 0014 -> 0016 chain rather than silently
        # generating an empty migration.
        op.drop_index(
            "uq_scoped_memory_jobs_external_message",
            table_name="scoped_memory_jobs",
        )
        op.drop_index(
            "ix_scoped_memory_jobs_source_message_id",
            table_name="scoped_memory_jobs",
        )
        op.drop_column("scoped_memory_jobs", "source_message_id")
        return

    indexes = _indexes("scoped_memory_jobs")
    if "uq_scoped_memory_jobs_external_message" in indexes:
        op.drop_index(
            "uq_scoped_memory_jobs_external_message",
            table_name="scoped_memory_jobs",
        )
    if "ix_scoped_memory_jobs_source_message_id" in indexes:
        op.drop_index(
            "ix_scoped_memory_jobs_source_message_id",
            table_name="scoped_memory_jobs",
        )
    if "source_message_id" in _columns("scoped_memory_jobs"):
        op.drop_column("scoped_memory_jobs", "source_message_id")
