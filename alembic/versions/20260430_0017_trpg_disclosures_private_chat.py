"""Add TRPG disclosures and private chat tables.

Revision ID: 20260430_0017
Revises: 20260430_0016
Create Date: 2026-04-30
"""

from alembic import op


revision = "20260430_0017"
down_revision = "20260430_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS trpg_room_disclosures (
            id UUID PRIMARY KEY,
            play_session_id UUID NOT NULL REFERENCES scenario_play_sessions(id) ON DELETE CASCADE,
            creator_participant_id UUID REFERENCES scenario_participants(id) ON DELETE SET NULL,
            disclosure_type VARCHAR(30) NOT NULL DEFAULT 'handout',
            visibility VARCHAR(20) NOT NULL DEFAULT 'public',
            target_participant_ids JSON NOT NULL DEFAULT '[]',
            title VARCHAR(200) NOT NULL,
            content TEXT NOT NULL DEFAULT '',
            image_url VARCHAR(1000) NOT NULL DEFAULT '',
            image_path VARCHAR(1000) NOT NULL DEFAULT '',
            tags JSON NOT NULL DEFAULT '[]',
            disclosure_metadata JSON NOT NULL DEFAULT '{}',
            is_pinned BOOLEAN NOT NULL DEFAULT false,
            created_at TIMESTAMP,
            updated_at TIMESTAMP
        )
        """
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_trpg_room_disclosures_play_session_id ON trpg_room_disclosures (play_session_id)"
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_trpg_room_disclosures_visibility ON trpg_room_disclosures (visibility)"
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_trpg_room_disclosures_created_at ON trpg_room_disclosures (created_at)"
    )

    bind.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS trpg_private_messages (
            id UUID PRIMARY KEY,
            play_session_id UUID NOT NULL REFERENCES scenario_play_sessions(id) ON DELETE CASCADE,
            sender_participant_id UUID REFERENCES scenario_participants(id) ON DELETE SET NULL,
            sender_label VARCHAR(120) NOT NULL DEFAULT '',
            target_participant_ids JSON NOT NULL DEFAULT '[]',
            message_type VARCHAR(20) NOT NULL DEFAULT 'private',
            content TEXT NOT NULL DEFAULT '',
            message_metadata JSON NOT NULL DEFAULT '{}',
            created_at TIMESTAMP
        )
        """
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_trpg_private_messages_play_session_id ON trpg_private_messages (play_session_id)"
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_trpg_private_messages_created_at ON trpg_private_messages (created_at)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("DROP TABLE IF EXISTS trpg_private_messages")
    bind.exec_driver_sql("DROP TABLE IF EXISTS trpg_room_disclosures")
