"""Add scenario kind and TRPG character sheet fields.

Revision ID: 20260429_0015
Revises: 20260429_0014
Create Date: 2026-04-29
"""

from alembic import op
import sqlalchemy as sa


revision = "20260429_0015"
down_revision = "20260429_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "scenarios",
        sa.Column("scenario_kind", sa.String(length=20), nullable=False, server_default="writing"),
    )
    op.add_column(
        "scenarios",
        sa.Column("ruleset", sa.String(length=50), nullable=False, server_default=""),
    )
    op.add_column(
        "scenario_characters",
        sa.Column("trpg_ruleset", sa.String(length=50), nullable=False, server_default=""),
    )
    op.add_column(
        "scenario_characters",
        sa.Column("trpg_pc_state", sa.JSON(), nullable=False, server_default="{}"),
    )
    op.create_index("ix_scenarios_scenario_kind", "scenarios", ["scenario_kind"])
    op.create_index("ix_scenarios_ruleset", "scenarios", ["ruleset"])
    op.execute(
        """
        UPDATE scenarios
        SET scenario_kind = 'trpg',
            ruleset = CASE
                WHEN lower(coalesce(genre, '')) = 'coc7'
                    OR tags::text ILIKE '%coc7%'
                THEN 'coc7'
                WHEN lower(coalesce(genre, '')) IN ('coc', 'coc6', 'call_of_cthulhu')
                    OR tags::text ILIKE '%coc%'
                    OR tags::text ILIKE '%cthulhu%'
                    OR EXISTS (
                        SELECT 1 FROM trpg_scenario_documents d
                        WHERE d.scenario_id = scenarios.id
                    )
                THEN 'coc6'
                ELSE ruleset
            END
        WHERE lower(coalesce(genre, '')) IN ('coc', 'coc6', 'coc7', 'call_of_cthulhu')
           OR tags::text ILIKE '%trpg%'
           OR tags::text ILIKE '%coc%'
           OR tags::text ILIKE '%cthulhu%'
           OR EXISTS (
                SELECT 1 FROM trpg_scenario_documents d
                WHERE d.scenario_id = scenarios.id
           )
        """
    )


def downgrade() -> None:
    op.drop_index("ix_scenarios_ruleset", table_name="scenarios")
    op.drop_index("ix_scenarios_scenario_kind", table_name="scenarios")
    op.drop_column("scenario_characters", "trpg_pc_state")
    op.drop_column("scenario_characters", "trpg_ruleset")
    op.drop_column("scenarios", "ruleset")
    op.drop_column("scenarios", "scenario_kind")
