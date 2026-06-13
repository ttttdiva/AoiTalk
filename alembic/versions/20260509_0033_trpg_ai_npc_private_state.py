"""TRPG AI NPC private state

Revision ID: 20260509_0033
Revises: 20260509_0032
Create Date: 2026-05-09
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "20260509_0033"
down_revision = "20260509_0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "scenario_participants",
        sa.Column(
            "private_state",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
    )
    op.add_column(
        "scenario_participants",
        sa.Column("last_observed_log_id", UUID(as_uuid=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("scenario_participants", "last_observed_log_id")
    op.drop_column("scenario_participants", "private_state")
