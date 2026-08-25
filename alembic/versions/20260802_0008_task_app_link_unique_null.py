"""Prevent duplicate Task/App links without a Target.

The existing composite UNIQUE constraint treats NULL target_id values as
distinct.  This partial unique index makes the no-Target form idempotent while
leaving Target-specific relations independent.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260802_0008"
down_revision = "20260802_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Older deployments may already contain duplicate NULL-target links because
    # a normal composite UNIQUE constraint treats NULLs as distinct.  Keep the
    # earliest-created row in each logical group (UUID is only the deterministic
    # tie-breaker) before adding the partial unique index.  This preserves the
    # original link and its audit fields rather than choosing an arbitrary UUID.
    op.execute(sa.text(
        """
        DELETE FROM task_app_links
        WHERE id IN (
            SELECT duplicate.id
            FROM task_app_links AS duplicate
            JOIN task_app_links AS keeper
              ON keeper.task_id = duplicate.task_id
             AND keeper.app_id = duplicate.app_id
             AND keeper.relation_type = duplicate.relation_type
             AND keeper.target_id IS NULL
             AND duplicate.target_id IS NULL
             AND (
                   keeper.created_at < duplicate.created_at
                OR (keeper.created_at = duplicate.created_at AND keeper.id < duplicate.id)
             )
        )
        """
    ))
    op.create_index(
        "uq_task_app_links_no_target",
        "task_app_links",
        ["task_id", "app_id", "relation_type"],
        unique=True,
        postgresql_where=sa.text("target_id IS NULL"),
        sqlite_where=sa.text("target_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_task_app_links_no_target", table_name="task_app_links")
