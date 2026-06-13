"""Backfill unified parent documents for structured rule items.

Revision ID: 20260509_0031
Revises: 20260509_0030
Create Date: 2026-05-09
"""

from alembic import op


revision = "20260509_0031"
down_revision = "20260509_0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(
        """
        WITH grouped AS (
            SELECT
                ruleset_key,
                COALESCE(NULLIF(source_title, ''), ruleset_key || ' structured rules') AS title,
                COALESCE(NULLIF(source_kind, ''), 'rulebook') AS source_kind,
                MIN(created_at) AS created_at,
                MAX(updated_at) AS updated_at
            FROM trpg_rule_items
            WHERE reference_document_id IS NULL
            GROUP BY
                ruleset_key,
                COALESCE(NULLIF(source_title, ''), ruleset_key || ' structured rules'),
                COALESCE(NULLIF(source_kind, ''), 'rulebook')
        )
        INSERT INTO trpg_reference_documents (
            id, ruleset_key, title, source_label, source_text, document_type,
            supplement_kind, structure, priority, is_active, document_metadata,
            import_status, legacy_source_table, legacy_source_id, created_at, updated_at
        )
        SELECT
            md5(g.ruleset_key || ':rulebook:' || g.title)::uuid,
            g.ruleset_key,
            g.title,
            'structured rule import',
            '',
            'rulebook',
            'general',
            '{}',
            80,
            true,
            json_build_object('structured_import', true, 'source_kind', g.source_kind),
            'structured',
            'trpg_rule_items',
            NULL,
            g.created_at,
            g.updated_at
        FROM grouped AS g
        ON CONFLICT (id) DO NOTHING
        """
    )
    bind.exec_driver_sql(
        """
        UPDATE trpg_rule_items AS ri
        SET reference_document_id = md5(
            ri.ruleset_key || ':rulebook:' || COALESCE(NULLIF(ri.source_title, ''), ri.ruleset_key || ' structured rules')
        )::uuid
        WHERE ri.reference_document_id IS NULL
          AND md5(
              ri.ruleset_key || ':rulebook:' || COALESCE(NULLIF(ri.source_title, ''), ri.ruleset_key || ' structured rules')
          )::uuid
              IN (SELECT id FROM trpg_reference_documents)
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(
        """
        UPDATE trpg_rule_items
        SET reference_document_id = NULL
        WHERE reference_document_id IN (
            SELECT id FROM trpg_reference_documents
            WHERE legacy_source_table = 'trpg_rule_items'
        )
        """
    )
    bind.exec_driver_sql("DELETE FROM trpg_reference_documents WHERE legacy_source_table = 'trpg_rule_items'")
