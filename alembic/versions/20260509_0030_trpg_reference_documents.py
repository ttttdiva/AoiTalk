"""Unify TRPG rulebook and supplement parent documents.

Revision ID: 20260509_0030
Revises: 20260508_0029
Create Date: 2026-05-09
"""

from alembic import op


revision = "20260509_0030"
down_revision = "20260508_0029"
branch_labels = None
depends_on = None


def _add_column_if_missing(table: str, column_sql: str) -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column_sql}")


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS trpg_reference_documents (
            id UUID PRIMARY KEY,
            ruleset_key VARCHAR(50) NOT NULL REFERENCES trpg_ruleset_profiles(key) ON DELETE CASCADE,
            title VARCHAR(200) NOT NULL,
            source_label TEXT NOT NULL DEFAULT '',
            source_text TEXT NOT NULL DEFAULT '',
            document_type VARCHAR(50) NOT NULL DEFAULT 'rulebook',
            supplement_kind VARCHAR(80) NOT NULL DEFAULT 'general',
            structure JSON NOT NULL DEFAULT '{}',
            priority INTEGER NOT NULL DEFAULT 0,
            is_active BOOLEAN NOT NULL DEFAULT true,
            document_metadata JSON NOT NULL DEFAULT '{}',
            import_status VARCHAR(40) NOT NULL DEFAULT 'metadata_only',
            legacy_source_table VARCHAR(80) NOT NULL DEFAULT '',
            legacy_source_id UUID,
            created_at TIMESTAMP,
            updated_at TIMESTAMP
        )
        """
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_trpg_reference_documents_ruleset_key "
        "ON trpg_reference_documents (ruleset_key)"
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_trpg_reference_documents_active "
        "ON trpg_reference_documents (ruleset_key, is_active)"
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_trpg_reference_documents_type "
        "ON trpg_reference_documents (ruleset_key, document_type)"
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_trpg_reference_documents_legacy "
        "ON trpg_reference_documents (legacy_source_table, legacy_source_id)"
    )

    _add_column_if_missing(
        "trpg_rule_items",
        "reference_document_id UUID REFERENCES trpg_reference_documents(id) ON DELETE SET NULL",
    )
    _add_column_if_missing(
        "trpg_creature_entries",
        "reference_document_id UUID REFERENCES trpg_reference_documents(id) ON DELETE CASCADE",
    )
    bind.exec_driver_sql("ALTER TABLE trpg_creature_entries ALTER COLUMN supplement_document_id DROP NOT NULL")
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_trpg_rule_items_reference "
        "ON trpg_rule_items (reference_document_id)"
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_trpg_creature_entries_reference "
        "ON trpg_creature_entries (reference_document_id)"
    )

    bind.exec_driver_sql(
        """
        INSERT INTO trpg_reference_documents (
            id, ruleset_key, title, source_label, source_text, document_type,
            supplement_kind, structure, priority, is_active, document_metadata,
            import_status, legacy_source_table, legacy_source_id, created_at, updated_at
        )
        SELECT
            id, ruleset_key, title, COALESCE(source_label, ''), COALESCE(source_text, ''),
            'rulebook', 'general', COALESCE(structure, '{}'), COALESCE(priority, 0),
            COALESCE(is_active, true), '{}', 'structured',
            'trpg_rulebook_documents', id, created_at, updated_at
        FROM trpg_rulebook_documents
        ON CONFLICT (id) DO NOTHING
        """
    )
    bind.exec_driver_sql(
        """
        INSERT INTO trpg_reference_documents (
            id, ruleset_key, title, source_label, source_text, document_type,
            supplement_kind, structure, priority, is_active, document_metadata,
            import_status, legacy_source_table, legacy_source_id, created_at, updated_at
        )
        SELECT
            id, ruleset_key, title, COALESCE(source_label, ''), COALESCE(source_text, ''),
            COALESCE(document_type, 'supplement'), COALESCE(supplement_kind, 'general'),
            '{}', COALESCE(priority, 0), COALESCE(is_active, true),
            COALESCE(document_metadata, '{}'), COALESCE(import_status, 'metadata_only'),
            'trpg_supplement_documents', id, created_at, updated_at
        FROM trpg_supplement_documents
        ON CONFLICT (id) DO NOTHING
        """
    )
    bind.exec_driver_sql(
        """
        UPDATE trpg_rule_items
        SET reference_document_id = source_document_id
        WHERE reference_document_id IS NULL
          AND source_document_id IN (SELECT id FROM trpg_reference_documents)
        """
    )
    bind.exec_driver_sql(
        """
        UPDATE trpg_rule_items AS ri
        SET reference_document_id = rd.id
        FROM trpg_reference_documents AS rd
        WHERE ri.reference_document_id IS NULL
          AND ri.ruleset_key = rd.ruleset_key
          AND ri.source_title = rd.title
          AND rd.document_type = 'rulebook'
        """
    )
    bind.exec_driver_sql(
        """
        UPDATE trpg_creature_entries
        SET reference_document_id = supplement_document_id
        WHERE reference_document_id IS NULL
          AND supplement_document_id IN (SELECT id FROM trpg_reference_documents)
        """
    )


def downgrade() -> None:
    op.drop_index("ix_trpg_creature_entries_reference", table_name="trpg_creature_entries")
    op.drop_index("ix_trpg_rule_items_reference", table_name="trpg_rule_items")
    op.drop_column("trpg_creature_entries", "reference_document_id")
    op.drop_column("trpg_rule_items", "reference_document_id")
    op.drop_index("ix_trpg_reference_documents_legacy", table_name="trpg_reference_documents")
    op.drop_index("ix_trpg_reference_documents_type", table_name="trpg_reference_documents")
    op.drop_index("ix_trpg_reference_documents_active", table_name="trpg_reference_documents")
    op.drop_index("ix_trpg_reference_documents_ruleset_key", table_name="trpg_reference_documents")
    op.drop_table("trpg_reference_documents")
