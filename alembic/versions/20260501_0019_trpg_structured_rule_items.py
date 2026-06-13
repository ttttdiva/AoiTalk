"""Add structured TRPG rule items and mechanic links.

Revision ID: 20260501_0019
Revises: 20260501_0018
Create Date: 2026-05-01
"""

from alembic import op


revision = "20260501_0019"
down_revision = "20260501_0018"
branch_labels = None
depends_on = None


def _add_column_if_missing(table: str, column_sql: str) -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column_sql}")


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS trpg_rule_items (
            id UUID PRIMARY KEY,
            ruleset_key VARCHAR(50) NOT NULL REFERENCES trpg_ruleset_profiles(key) ON DELETE CASCADE,
            source_document_id UUID REFERENCES trpg_rulebook_documents(id) ON DELETE SET NULL,
            source_kind VARCHAR(50) NOT NULL DEFAULT 'rulebook',
            source_title VARCHAR(200) NOT NULL DEFAULT '',
            rule_domain VARCHAR(80) NOT NULL DEFAULT 'general',
            mechanic_key VARCHAR(120) NOT NULL DEFAULT '',
            title VARCHAR(240) NOT NULL,
            normalized_name VARCHAR(240) NOT NULL DEFAULT '',
            source_span JSON NOT NULL DEFAULT '{}',
            raw_excerpt TEXT NOT NULL DEFAULT '',
            structured_data JSON NOT NULL DEFAULT '{}',
            confidence DOUBLE PRECISION NOT NULL DEFAULT 0,
            needs_review BOOLEAN NOT NULL DEFAULT true,
            tags JSON NOT NULL DEFAULT '[]',
            priority INTEGER NOT NULL DEFAULT 0,
            is_active BOOLEAN NOT NULL DEFAULT true,
            created_at TIMESTAMP,
            updated_at TIMESTAMP
        )
        """
    )
    bind.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS trpg_mechanic_rule_links (
            id UUID PRIMARY KEY,
            ruleset_key VARCHAR(50) NOT NULL REFERENCES trpg_ruleset_profiles(key) ON DELETE CASCADE,
            mechanic_key VARCHAR(120) NOT NULL,
            rule_item_id UUID NOT NULL REFERENCES trpg_rule_items(id) ON DELETE CASCADE,
            runtime_module VARCHAR(240) NOT NULL DEFAULT '',
            runtime_function VARCHAR(160) NOT NULL DEFAULT '',
            priority INTEGER NOT NULL DEFAULT 0,
            link_metadata JSON NOT NULL DEFAULT '{}',
            created_at TIMESTAMP,
            updated_at TIMESTAMP
        )
        """
    )
    bind.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_trpg_rule_items_ruleset ON trpg_rule_items (ruleset_key)")
    bind.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_trpg_rule_items_domain ON trpg_rule_items (ruleset_key, rule_domain)")
    bind.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_trpg_rule_items_mechanic ON trpg_rule_items (ruleset_key, mechanic_key)")
    bind.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_trpg_rule_items_normalized_name ON trpg_rule_items (ruleset_key, normalized_name)")
    bind.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_trpg_rule_items_active ON trpg_rule_items (ruleset_key, is_active)")
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_trpg_mechanic_rule_links_mechanic ON trpg_mechanic_rule_links (ruleset_key, mechanic_key)"
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_trpg_mechanic_rule_links_rule_item ON trpg_mechanic_rule_links (rule_item_id)"
    )

    _add_column_if_missing("trpg_supplement_documents", "import_status VARCHAR(40) NOT NULL DEFAULT 'metadata_only'")
    _add_column_if_missing("trpg_creature_entries", "source_span JSON NOT NULL DEFAULT '{}'")
    _add_column_if_missing("trpg_creature_entries", "ocr_status VARCHAR(40) NOT NULL DEFAULT 'unreviewed'")
    _add_column_if_missing("trpg_creature_entries", "characteristics JSON NOT NULL DEFAULT '{}'")
    _add_column_if_missing("trpg_creature_entries", "skills JSON NOT NULL DEFAULT '{}'")
    _add_column_if_missing("trpg_creature_entries", "attacks JSON NOT NULL DEFAULT '[]'")
    _add_column_if_missing("trpg_creature_entries", "armor TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing("trpg_creature_entries", "spells JSON NOT NULL DEFAULT '[]'")
    _add_column_if_missing("trpg_creature_entries", "abilities JSON NOT NULL DEFAULT '[]'")
    _add_column_if_missing("trpg_creature_entries", "san_loss VARCHAR(80) NOT NULL DEFAULT ''")
    _add_column_if_missing("trpg_creature_entries", "mechanic_links JSON NOT NULL DEFAULT '[]'")
    _add_column_if_missing("trpg_creature_entries", "needs_review BOOLEAN NOT NULL DEFAULT true")


def downgrade() -> None:
    op.drop_index("ix_trpg_mechanic_rule_links_rule_item", table_name="trpg_mechanic_rule_links")
    op.drop_index("ix_trpg_mechanic_rule_links_mechanic", table_name="trpg_mechanic_rule_links")
    op.drop_table("trpg_mechanic_rule_links")
    op.drop_index("ix_trpg_rule_items_active", table_name="trpg_rule_items")
    op.drop_index("ix_trpg_rule_items_normalized_name", table_name="trpg_rule_items")
    op.drop_index("ix_trpg_rule_items_mechanic", table_name="trpg_rule_items")
    op.drop_index("ix_trpg_rule_items_domain", table_name="trpg_rule_items")
    op.drop_index("ix_trpg_rule_items_ruleset", table_name="trpg_rule_items")
    op.drop_table("trpg_rule_items")
