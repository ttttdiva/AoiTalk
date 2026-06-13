"""Cascade scenario play sessions when deleting scenarios.

Revision ID: 20260504_0024
Revises: 20260502_0023
Create Date: 2026-05-04
"""

from __future__ import annotations

from alembic import op

revision = "20260504_0024"
down_revision = "20260502_0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "scenario_play_sessions_scenario_id_fkey",
        "scenario_play_sessions",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "scenario_play_sessions_scenario_id_fkey",
        "scenario_play_sessions",
        "scenarios",
        ["scenario_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint(
        "scenario_play_sessions_scenario_id_fkey",
        "scenario_play_sessions",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "scenario_play_sessions_scenario_id_fkey",
        "scenario_play_sessions",
        "scenarios",
        ["scenario_id"],
        ["id"],
    )
