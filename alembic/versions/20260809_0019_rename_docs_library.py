"""Rename the Docs domain from *workspace* to *Docs Library*.

The project-wide terminology change is deliberately a forward migration.  The
Filer/filesystem ``workspace`` vocabulary is outside this list and is not
renamed.  Applied migrations (0013/0015/0016) remain byte-for-byte historical
records; this revision only changes the live schema used by current code.

The SQL is guarded for partially upgraded installations and can be run twice
without changing data.  A short-lived compatibility alias is provided by the
ORM (`KnowledgeWorkspace = DocsLibrary`, and `workspace_id` synonyms) so older
Python/mobile sync clients can dual-read while the new wire keys are rolled
out.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260809_0019"
down_revision = "20260809_0018"
branch_labels = None
depends_on = None


_UPGRADE_SQL = r"""
DO $$
DECLARE
    item RECORD;
BEGIN
    -- Rename the Docs library table first.  PostgreSQL updates foreign-key
    -- targets automatically, while explicit constraint/index renames below
    -- keep catalog names and diagnostics aligned with the new vocabulary.
    IF to_regclass('public.knowledge_workspaces') IS NOT NULL
       AND to_regclass('public.docs_libraries') IS NULL THEN
        ALTER TABLE knowledge_workspaces RENAME TO docs_libraries;
    END IF;

    -- ``workspace_type`` is a library discriminator, not project identity.
    IF to_regclass('public.docs_libraries') IS NOT NULL
       AND EXISTS (
           SELECT 1
           FROM information_schema.columns
           WHERE table_schema = 'public'
             AND table_name = 'docs_libraries'
             AND column_name = 'workspace_type'
       )
       AND NOT EXISTS (
           SELECT 1
           FROM information_schema.columns
           WHERE table_schema = 'public'
             AND table_name = 'docs_libraries'
             AND column_name = 'library_type'
       ) THEN
        ALTER TABLE docs_libraries RENAME COLUMN workspace_type TO library_type;
    END IF;

    -- All columns in this list are Docs-domain columns.  Deliberately do not
    -- touch fw_* Filer tables or generic filesystem APIs that happen to use a
    -- ``workspace_id`` identifier.
    FOR item IN
        SELECT *
        FROM (VALUES
            ('knowledge_nodes', 'workspace_id', 'docs_library_id'),
            ('knowledge_supertags', 'workspace_id', 'docs_library_id'),
            ('knowledge_fields', 'workspace_id', 'docs_library_id'),
            ('knowledge_search_index', 'workspace_id', 'docs_library_id'),
            ('knowledge_saved_views', 'workspace_id', 'docs_library_id'),
            ('knowledge_ai_suggestions', 'workspace_id', 'docs_library_id'),
            ('knowledge_import_jobs', 'workspace_id', 'docs_library_id'),
            ('knowledge_views', 'workspace_id', 'docs_library_id')
        ) AS columns(table_name, old_column, new_column)
    LOOP
        IF to_regclass('public.' || item.table_name) IS NOT NULL
           AND EXISTS (
               SELECT 1
               FROM information_schema.columns
               WHERE table_schema = 'public'
                 AND information_schema.columns.table_name = item.table_name
                 AND information_schema.columns.column_name = item.old_column
           )
           AND NOT EXISTS (
               SELECT 1
               FROM information_schema.columns
               WHERE table_schema = 'public'
                 AND information_schema.columns.table_name = item.table_name
                 AND information_schema.columns.column_name = item.new_column
           ) THEN
            EXECUTE format(
                'ALTER TABLE %I RENAME COLUMN %I TO %I',
                item.table_name, item.old_column, item.new_column
            );
        END IF;
    END LOOP;

    -- Constraint/index names are not part of the data contract, but keeping
    -- them coherent makes schema introspection and generated SQL predictable.
    FOR item IN
        SELECT *
        FROM (VALUES
            ('docs_libraries', 'ck_knowledge_workspaces_workspace_type', 'ck_docs_libraries_library_type'),
            ('docs_libraries', 'ck_knowledge_workspaces_project_scope', 'ck_docs_libraries_project_scope'),
            ('knowledge_supertags', 'uq_knowledge_supertags_workspace_system_key', 'uq_knowledge_supertags_docs_library_system_key')
        ) AS constraints(table_name, old_name, new_name)
    LOOP
        IF EXISTS (
            SELECT 1
            FROM pg_constraint AS c
            JOIN pg_class AS rel ON rel.oid = c.conrelid
            JOIN pg_namespace AS ns ON ns.oid = rel.relnamespace
            WHERE ns.nspname = 'public'
              AND rel.relname = item.table_name
              AND c.conname = item.old_name
        )
           AND NOT EXISTS (
               SELECT 1
               FROM pg_constraint AS c
               JOIN pg_class AS rel ON rel.oid = c.conrelid
               JOIN pg_namespace AS ns ON ns.oid = rel.relnamespace
               WHERE ns.nspname = 'public'
                 AND rel.relname = item.table_name
                 AND c.conname = item.new_name
           ) THEN
            EXECUTE format(
                'ALTER TABLE %I RENAME CONSTRAINT %I TO %I',
                item.table_name, item.old_name, item.new_name
            );
        END IF;
    END LOOP;

    FOR item IN
        SELECT *
        FROM (VALUES
            ('ix_knowledge_workspaces_owner_user', 'ix_docs_libraries_owner_user'),
            ('uq_knowledge_workspaces_personal_owner', 'uq_docs_libraries_personal_owner'),
            ('uq_knowledge_workspaces_project', 'uq_docs_libraries_project'),
            ('ix_knowledge_workspaces_project', 'ix_docs_libraries_project'),
            ('uq_knowledge_nodes_workspace_system_key', 'uq_knowledge_nodes_docs_library_system_key'),
            ('ix_knowledge_nodes_workspace', 'ix_knowledge_nodes_docs_library'),
            ('ix_knowledge_nodes_workspace_parent_sort', 'ix_knowledge_nodes_docs_library_parent_sort'),
            ('ix_knowledge_nodes_workspace_project', 'ix_knowledge_nodes_docs_library_project'),
            ('ix_knowledge_nodes_workspace_day', 'ix_knowledge_nodes_docs_library_day'),
            ('ix_knowledge_supertags_workspace', 'ix_knowledge_supertags_docs_library'),
            ('ix_knowledge_fields_workspace', 'ix_knowledge_fields_docs_library'),
            ('ix_knowledge_search_index_workspace', 'ix_knowledge_search_index_docs_library'),
            ('ix_knowledge_saved_views_workspace', 'ix_knowledge_saved_views_docs_library'),
            ('ix_knowledge_ai_suggestions_workspace', 'ix_knowledge_ai_suggestions_docs_library'),
            ('ix_knowledge_import_jobs_workspace', 'ix_knowledge_import_jobs_docs_library')
        ) AS indexes(old_name, new_name)
    LOOP
        IF to_regclass('public.' || item.old_name) IS NOT NULL
           AND to_regclass('public.' || item.new_name) IS NULL THEN
            EXECUTE format(
                'ALTER INDEX %I RENAME TO %I', item.old_name, item.new_name
            );
        END IF;
    END LOOP;
END
$$;
"""


def upgrade() -> None:
    op.execute(sa.text(_UPGRADE_SQL))


def downgrade() -> None:
    # A reverse rename would make rolling back current application code unsafe
    # and would resurrect the deprecated API vocabulary.  Refuse explicitly so
    # Alembic cannot move the revision pointer while the live schema remains
    # in the Docs Library shape.
    raise RuntimeError(
        "20260809_0019 is forward-only; restore a backup and run a reviewed "
        "compatibility migration instead of downgrading"
    )
