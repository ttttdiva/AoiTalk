"""Separate TRPG player sheets from scenario characters.

Revision ID: 20260502_0021
Revises: 20260501_0019
Create Date: 2026-05-02
"""

from alembic import op


revision = "20260502_0021"
down_revision = "20260501_0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS trpg_player_character_sheets (
            id UUID PRIMARY KEY,
            scenario_id UUID NOT NULL REFERENCES scenarios(id) ON DELETE CASCADE,
            user_id UUID NOT NULL,
            ruleset VARCHAR(50) NOT NULL DEFAULT '',
            name VARCHAR(100) NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            trpg_pc_state JSON NOT NULL DEFAULT '{}',
            sheet_metadata JSON NOT NULL DEFAULT '{}',
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )
        """
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_trpg_player_sheets_scenario_user "
        "ON trpg_player_character_sheets (scenario_id, user_id)"
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_trpg_player_sheets_user "
        "ON trpg_player_character_sheets (user_id)"
    )
    bind.exec_driver_sql(
        """
        INSERT INTO trpg_player_character_sheets (
            id, scenario_id, user_id, ruleset, name, description,
            trpg_pc_state, sheet_metadata, created_at, updated_at
        )
        SELECT
            sc.id,
            sc.scenario_id,
            (owner_rel.rel->>'user_id')::uuid,
            COALESCE(NULLIF(sc.trpg_ruleset, ''), COALESCE(sc.trpg_pc_state->>'ruleset', 'coc6')),
            sc.name,
            COALESCE(sc.description, ''),
            COALESCE(sc.trpg_pc_state, '{}'::json),
            json_build_object('migrated_from', 'scenario_characters_owner_user'),
            NOW(),
            NOW()
        FROM scenario_characters sc
        JOIN LATERAL jsonb_array_elements(COALESCE(sc.relationships::jsonb, '[]'::jsonb)) AS owner_rel(rel) ON true
        WHERE owner_rel.rel->>'type' = 'owner_user'
          AND owner_rel.rel->>'user_id' ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        ON CONFLICT (id) DO NOTHING
        """
    )
    bind.exec_driver_sql(
        """
        DELETE FROM scenario_characters sc
        WHERE EXISTS (
            SELECT 1
            FROM jsonb_array_elements(COALESCE(sc.relationships::jsonb, '[]'::jsonb)) AS owner_rel(rel)
            WHERE owner_rel.rel->>'type' = 'owner_user'
        )
        """
    )


def downgrade() -> None:
    op.drop_index("ix_trpg_player_sheets_user", table_name="trpg_player_character_sheets")
    op.drop_index("ix_trpg_player_sheets_scenario_user", table_name="trpg_player_character_sheets")
    op.drop_table("trpg_player_character_sheets")
