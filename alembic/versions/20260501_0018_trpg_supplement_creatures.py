"""Add TRPG supplement and creature tables.

Revision ID: 20260501_0018
Revises: 20260430_0017
Create Date: 2026-05-01
"""

from alembic import op


revision = "20260501_0018"
down_revision = "20260430_0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS trpg_supplement_documents (
            id UUID PRIMARY KEY,
            ruleset_key VARCHAR(50) NOT NULL REFERENCES trpg_ruleset_profiles(key) ON DELETE CASCADE,
            title VARCHAR(200) NOT NULL,
            source_label TEXT NOT NULL DEFAULT '',
            source_text TEXT NOT NULL DEFAULT '',
            document_type VARCHAR(50) NOT NULL DEFAULT 'supplement',
            supplement_kind VARCHAR(80) NOT NULL DEFAULT 'general',
            priority INTEGER NOT NULL DEFAULT 0,
            is_active BOOLEAN NOT NULL DEFAULT true,
            document_metadata JSON NOT NULL DEFAULT '{}',
            created_at TIMESTAMP,
            updated_at TIMESTAMP
        )
        """
    )
    bind.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS trpg_creature_entries (
            id UUID PRIMARY KEY,
            supplement_document_id UUID NOT NULL REFERENCES trpg_supplement_documents(id) ON DELETE CASCADE,
            ruleset_key VARCHAR(50) NOT NULL,
            name VARCHAR(200) NOT NULL,
            normalized_name VARCHAR(240) NOT NULL DEFAULT '',
            entry_type VARCHAR(50) NOT NULL DEFAULT 'creature',
            classification VARCHAR(100) NOT NULL DEFAULT '',
            summary TEXT NOT NULL DEFAULT '',
            source_excerpt TEXT NOT NULL DEFAULT '',
            char_start INTEGER NOT NULL DEFAULT 0,
            char_end INTEGER NOT NULL DEFAULT 0,
            confidence VARCHAR(30) NOT NULL DEFAULT 'medium',
            tags JSON NOT NULL DEFAULT '[]',
            entry_metadata JSON NOT NULL DEFAULT '{}',
            created_at TIMESTAMP,
            updated_at TIMESTAMP
        )
        """
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_trpg_supplement_documents_ruleset_key ON trpg_supplement_documents (ruleset_key)"
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_trpg_supplement_documents_active ON trpg_supplement_documents (ruleset_key, is_active)"
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_trpg_supplement_documents_kind ON trpg_supplement_documents (ruleset_key, supplement_kind)"
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_trpg_creature_entries_ruleset ON trpg_creature_entries (ruleset_key)"
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_trpg_creature_entries_supplement ON trpg_creature_entries (supplement_document_id)"
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_trpg_creature_entries_name ON trpg_creature_entries (ruleset_key, normalized_name)"
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_trpg_creature_entries_type ON trpg_creature_entries (ruleset_key, entry_type)"
    )


def downgrade() -> None:
    op.drop_index("ix_trpg_creature_entries_type", table_name="trpg_creature_entries")
    op.drop_index("ix_trpg_creature_entries_name", table_name="trpg_creature_entries")
    op.drop_index("ix_trpg_creature_entries_supplement", table_name="trpg_creature_entries")
    op.drop_index("ix_trpg_creature_entries_ruleset", table_name="trpg_creature_entries")
    op.drop_table("trpg_creature_entries")
    op.drop_index("ix_trpg_supplement_documents_kind", table_name="trpg_supplement_documents")
    op.drop_index("ix_trpg_supplement_documents_active", table_name="trpg_supplement_documents")
    op.drop_index("ix_trpg_supplement_documents_ruleset_key", table_name="trpg_supplement_documents")
    op.drop_table("trpg_supplement_documents")
