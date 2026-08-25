"""Make AgentRun tool-call audit retries idempotent.

Duplicate provider call IDs are normalized to NULL during upgrade.  That data
change is irreversible: downgrade can remove the constraint, but it cannot
restore the discarded IDs.

Revision ID: 20260804_0001
Revises: 20260803_0005
Create Date: 2026-08-04
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260804_0001"
down_revision = "20260803_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Keep concurrent INSERT/UPDATE/DELETE statements out until the unique
    # constraint exists. PostgreSQL holds this lock for the migration
    # transaction, including the cleanup and constraint creation below.
    op.execute(
        sa.text(
            "LOCK TABLE agent_run_tool_calls IN SHARE ROW EXCLUSIVE MODE"
        )
    )

    # Preserve historical audit rows while making the first recorded provider
    # call ID canonical.  NULL remains intentionally repeatable.
    op.execute(
        sa.text(
            """
            WITH ranked_tool_calls AS (
                SELECT
                    id,
                    ROW_NUMBER() OVER (
                        PARTITION BY run_id, tool_call_id
                        ORDER BY created_at, id
                    ) AS duplicate_number
                FROM agent_run_tool_calls
                WHERE tool_call_id IS NOT NULL
            )
            UPDATE agent_run_tool_calls AS tool_call
            SET tool_call_id = NULL
            FROM ranked_tool_calls AS ranked
            WHERE tool_call.id = ranked.id
              AND ranked.duplicate_number > 1
            """
        )
    )
    op.create_unique_constraint(
        "uq_agent_run_tool_calls_run_tool_call_id",
        "agent_run_tool_calls",
        ["run_id", "tool_call_id"],
    )


def downgrade() -> None:
    # The upgrade's duplicate-ID-to-NULL normalization is irreversible;
    # dropping the constraint cannot reconstruct the discarded provider IDs.
    op.drop_constraint(
        "uq_agent_run_tool_calls_run_tool_call_id",
        "agent_run_tool_calls",
        type_="unique",
    )
