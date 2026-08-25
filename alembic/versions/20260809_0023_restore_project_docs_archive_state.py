"""Restore descendants accidentally archived by the first 0018 rollout.

Revision ID: 20260809_0023
Revises: 20260809_0022
"""

from __future__ import annotations

from alembic import op


revision = "20260809_0023"
down_revision = "20260809_0022"
branch_labels = None
depends_on = None


_UPGRADE_SQL = r"""
DO $$
DECLARE
    log_row RECORD;
    restored_count integer;
BEGIN
    -- An early, already-applied draft of 0018 archived every previously-active
    -- descendant with the exact transaction timestamp used by its migration
    -- log row.  Pre-existing archived nodes keep their older timestamps, so
    -- this equality is the lossless discriminator for the damaged rows.
    FOR log_row IN
        SELECT DISTINCT ON (migration.project_id)
               migration.project_id,
               migration.created_at,
               migration.canonical_workspace_id AS docs_library_id,
               migration.root_node_id
        FROM docs_workspace_migration_log AS migration
        WHERE migration.metadata::jsonb ->> 'migration_revision' = '20260809_0018'
          AND migration.status IN ('moved', 'already_canonical')
          AND migration.project_id IS NOT NULL
        ORDER BY migration.project_id, migration.created_at
    LOOP
        UPDATE knowledge_nodes AS node
        SET archived_at = NULL
        WHERE node.project_id = log_row.project_id
          AND node.archived_at = log_row.created_at::timestamp
          AND node.id IS DISTINCT FROM log_row.root_node_id
          AND node.system_key IS DISTINCT FROM
              'project_information:' || log_row.project_id::text;
        GET DIAGNOSTICS restored_count = ROW_COUNT;

        IF restored_count > 0 THEN
            INSERT INTO docs_workspace_migration_log (
                project_id,
                legacy_workspace_id,
                canonical_workspace_id,
                root_node_id,
                moved_count,
                status,
                metadata
            ) VALUES (
                log_row.project_id,
                NULL,
                log_row.docs_library_id,
                log_row.root_node_id,
                restored_count,
                'already_canonical',
                jsonb_build_object(
                    'migration_revision', '20260809_0023',
                    'action', 'restore_0018_descendant_archive_state',
                    'source_migration_revision', '20260809_0018',
                    'source_transaction_at', log_row.created_at,
                    'restored_count', restored_count,
                    'preservation', jsonb_build_array(
                        'node_uuid', 'title', 'body_json', 'body_text',
                        'children', 'revisions', 'attachments', 'edges',
                        'placements', 'field_values'
                    )
                )
            );
        END IF;
    END LOOP;
END
$$;
"""


def upgrade() -> None:
    op.execute(_UPGRADE_SQL)


def downgrade() -> None:
    raise RuntimeError(
        "20260809_0023 is a forward-only data repair; restoring the corrupt "
        "archive state would re-hide valid Project Docs descendants"
    )
