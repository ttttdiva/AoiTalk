"""Add default Google Calendar event reminder setting.

Revision ID: 20260424_0010
Revises: 20260422_0009
Create Date: 2026-04-24 00:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260424_0010"
down_revision = "20260422_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "google_calendar_connections",
        sa.Column(
            "default_event_reminder_minutes",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("10"),
        ),
    )


def downgrade() -> None:
    op.drop_column(
        "google_calendar_connections",
        "default_event_reminder_minutes",
    )
