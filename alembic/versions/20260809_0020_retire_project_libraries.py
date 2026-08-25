"""Retire the last split Project Docs Libraries.

``0018`` made the project-information pointer a real child of the owner's
Personal Docs Library.  A few installations (and older clients) can still
leave the per-project library containing the generated ``Home``/navigation
seed rows, custom nodes, or schema definitions.  This revision is a final,
forward-only reconciliation:

* every non-seed row is moved to the owner's Personal Library with its UUID;
  parent/root IDs, encrypted body, revisions, attachments, placements,
  imports and edges are not rewritten or deleted;
* Home/navigation seeds are moved like every other row; no value-bearing row
  is deleted or cascaded;
* Project tags/fields retain their UUIDs and are moved without destructive
  merging, and all Docs metadata follows the same library;
* the empty legacy library row is deleted and ``docs_libraries.project_id`` is
  dropped.  ``library_type`` remains a generic discriminator but explicitly
  rejects the retired ``project`` value.

The migration is idempotent and leaves an audit row for every retired source
library.  Deleted Projects and default-Inbox projects are reconciled in the
same owner scope; no project library is skipped.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TypeVar

import sqlalchemy as sa
from alembic import op


revision = "20260809_0020"
down_revision = "20260809_0019"
branch_labels = None
depends_on = None


_T = TypeVar("_T")

_SEED_TITLES = frozenset(
    {
        "Home",
        "案件",
        "案件一覧",
        "最近更新",
        "最近更新されたノード",
        "タスク",
        "未完了タスク",
    }
)


def _fixture_seed_only_nodes(
    rows: Iterable[Mapping[str, _T]],
    root_id: _T,
    *,
    id_key: str = "id",
    parent_key: str = "parent_id",
    title_key: str = "title",
    system_key: str = "system_key",
    untouched_key: str = "untouched",
) -> list[_T]:
    """Return a strict, cycle-safe Home/navigation seed subtree.

    Contract tests use this helper without a PostgreSQL connection.  A node is
    seed-only only when every descendant is a known generated title (or the
    ``home`` marker) and every row is untouched.  The online SQL additionally
    checks attachments/revisions/edges/etc. before deleting the result.
    """

    by_parent: dict[_T | None, list[Mapping[str, _T]]] = {}
    for row in rows:
        by_parent.setdefault(row.get(parent_key), []).append(row)
    for siblings in by_parent.values():
        siblings.sort(key=lambda row: str(row.get(id_key)))

    selected: list[_T] = []
    stack = [root_id]
    seen: set[_T] = set()
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        row = next(
            (candidate for siblings in by_parent.values() for candidate in siblings
             if candidate.get(id_key) == current),
            None,
        )
        if row is None:
            return []
        title = str(row.get(title_key) or "")
        marker = str(row.get(system_key) or "")
        untouched = row.get(untouched_key, True)
        if not untouched or (marker != "home" and title not in _SEED_TITLES):
            return []
        selected.append(current)
        stack.extend(reversed([child[id_key] for child in by_parent.get(current, [])]))
    return selected


_UPGRADE_SQL = r"""
DO $$
DECLARE
    lib RECORD;
    pointer_row RECORD;
    node_row RECORD;
    tag_row RECORD;
    personal_id uuid;
    hub_id uuid;
    root_id uuid;
    pointer_id uuid;
    source_library_id uuid;
    node_count integer;
    cross_library_parent_count integer;
    candidate_count integer;
    duplicate_candidate_count integer;
    pointer_valid boolean;
    pointer_reason text;
    metadata jsonb;
    canonical_tag_id uuid;
    legacy_view_count integer;
BEGIN
    -- Every source row is moved by UUID.  The temp snapshot is deliberately
    -- limited to the source Docs Library; parent links to another library are
    -- never followed and are recorded before being reattached to the project
    -- root.  This keeps a malformed cross-library edge from moving unrelated
    -- Personal data.
    CREATE TEMP TABLE IF NOT EXISTS _aoi_retired_nodes (
        id uuid PRIMARY KEY,
        old_parent_id uuid,
        old_root_page_id uuid,
        source_library_id uuid NOT NULL,
        project_id uuid NOT NULL
    ) ON COMMIT DROP;

    FOR lib IN
        SELECT l.*, p.id AS project_row_id, p.owner_id, p.deleted_at,
               p.knowledge_node_id AS pointer_node_id,
               p.name AS project_name, p.description AS project_description,
               p.slug AS project_slug,
               p.project_metadata, p.aliases AS project_aliases
        FROM docs_libraries AS l
        LEFT JOIN projects AS p ON p.id = l.project_id
        WHERE l.library_type = 'project'
        ORDER BY l.id
        FOR UPDATE OF l
    LOOP
        -- There is no safe ACL owner for an orphaned project library.  Refuse
        -- before any DDL instead of leaving a project row for the new CHECK.
        IF lib.project_row_id IS NULL THEN
            RAISE EXCEPTION '20260809_0020 cannot retire orphan project Docs Library %', lib.id
                USING DETAIL = jsonb_build_object(
                    'library_id', lib.id,
                    'reason', 'missing_project_owner',
                    'recovery', 'restore projects row or quarantine the library in a reviewed transaction'
                )::text;
        END IF;
        IF lib.owner_id IS NULL THEN
            RAISE EXCEPTION '20260809_0020 cannot retire project % Docs Library % without owner',
                lib.project_row_id, lib.id
                USING DETAIL = jsonb_build_object(
                    'library_id', lib.id,
                    'project_id', lib.project_row_id,
                    'reason', 'missing_owner',
                    'recovery', 'restore projects.owner_id or quarantine the library in a reviewed transaction'
                )::text;
        END IF;

        -- Default Inbox is a real owner/project scope.  It is intentionally
        -- reconciled, not skipped, so its library cannot survive the CHECK.
        pointer_reason := NULL;
        pointer_id := lib.pointer_node_id;
        pointer_valid := false;
        candidate_count := 0;
        source_library_id := NULL;

        SELECT l.id
        INTO personal_id
        FROM docs_libraries AS l
        WHERE l.library_type = 'personal'
          AND l.owner_user_id = lib.owner_id
        ORDER BY l.created_at NULLS LAST, l.id
        LIMIT 1
        FOR UPDATE;
        IF personal_id IS NULL THEN
            personal_id := gen_random_uuid();
            INSERT INTO docs_libraries (
                id, name, description, owner_user_id, library_type,
                settings_json, created_at, updated_at
            ) VALUES (
                personal_id, 'Personal Docs', NULL, lib.owner_id, 'personal',
                '{}'::json, now(), now()
            );
        END IF;

        -- Repair a missing Personal canonical hub before resolving a stale
        -- pointer.  The hub identity is exact and owner-scoped.
        SELECT n.id
        INTO hub_id
        FROM knowledge_nodes AS n
        WHERE n.docs_library_id = personal_id
          AND n.project_id IS NULL
          AND n.system_key = 'project_information_root'
          AND n.parent_id IS NULL
          AND n.archived_at IS NULL
        ORDER BY n.created_at NULLS LAST, n.id
        LIMIT 1
        FOR UPDATE;
        IF hub_id IS NULL THEN
            INSERT INTO knowledge_nodes (
                id, docs_library_id, parent_id, root_page_id, project_id,
                title, body_json, body_text, node_type, description,
                display_props, view_json, sort_order, created_at, updated_at,
                system_key
            ) VALUES (
                gen_random_uuid(), personal_id, NULL, NULL, NULL,
                '案件情報', '{}'::json, '案件情報', 'page', '',
                '{}'::json, '{}'::json, 0, now(), now(),
                'project_information_root'
            )
            RETURNING id INTO hub_id;
            UPDATE knowledge_nodes
            SET root_page_id = hub_id, updated_at = now()
            WHERE id = hub_id;
        END IF;

        SELECT s.id
        INTO canonical_tag_id
        FROM knowledge_supertags AS s
        WHERE s.docs_library_id = personal_id
          AND s.system_key = 'project_info'
        ORDER BY s.id
        LIMIT 1
        FOR UPDATE;
        IF canonical_tag_id IS NULL THEN
            INSERT INTO knowledge_supertags (
                id, docs_library_id, parent_supertag_id, system_key, name,
                base_type, description, template_json, pinned_field_ids,
                config_json, created_at, updated_at
            ) VALUES (
                gen_random_uuid(), personal_id, NULL, 'project_info',
                '案件情報', 'project_information',
                'Canonical project information schema', '{}'::json,
                '[]'::json, '{}'::json, now(), now()
            )
            RETURNING id INTO canonical_tag_id;
        END IF;

        -- Pointer validation is intentionally stricter than a title/name
        -- match.  A valid pointer must have project_id, exact system_key,
        -- project_info tag, and either the owner's Personal hub parent or the
        -- source Project Library.  Ordinary Home is therefore never hijacked.
        IF pointer_id IS NOT NULL THEN
            SELECT n.*
            INTO pointer_row
            FROM knowledge_nodes AS n
            JOIN docs_libraries AS source ON source.id = n.docs_library_id
            WHERE n.id = pointer_id
              AND n.project_id = lib.project_row_id
              AND n.system_key = 'project_information:' || lib.project_row_id::text
              AND EXISTS (
                  SELECT 1
                  FROM knowledge_node_supertags AS nt
                  JOIN knowledge_supertags AS st ON st.id = nt.supertag_id
                  WHERE nt.node_id = n.id
                    AND st.docs_library_id = n.docs_library_id
                    AND st.system_key = 'project_info'
              )
              AND (
                  (source.library_type = 'personal'
                   AND source.owner_user_id = lib.owner_id
                   AND n.parent_id = hub_id
                   AND n.root_page_id = hub_id)
                  OR (source.library_type = 'project'
                      AND source.id = lib.id
                      AND source.project_id = lib.project_row_id)
              )
            FOR UPDATE;
            IF FOUND THEN
                root_id := pointer_row.id;
                source_library_id := pointer_row.docs_library_id;
                pointer_valid := true;
            ELSE
                pointer_reason := CASE
                    WHEN NOT EXISTS (SELECT 1 FROM knowledge_nodes WHERE id = pointer_id)
                        THEN 'missing_pointer'
                    WHEN EXISTS (SELECT 1 FROM knowledge_nodes WHERE id = pointer_id AND system_key = 'home')
                        THEN 'ordinary_home_pointer'
                    ELSE 'canonical_identity_mismatch'
                END;
            END IF;
        ELSE
            pointer_reason := 'null_pointer';
        END IF;

        -- A stale pointer can adopt exactly one already-canonical Personal
        -- candidate.  Ambiguous/untagged duplicates are explicit conflicts;
        -- only a genuinely missing candidate permits a new node.
        IF NOT pointer_valid THEN
            -- A node with the canonical key/project identity but without the
            -- schema tag is not a safe adoption candidate.  Refuse active
            -- duplicates instead of silently leaving two roots behind.
            SELECT count(*)
            INTO duplicate_candidate_count
            FROM knowledge_nodes AS duplicate
            WHERE duplicate.docs_library_id = personal_id
              AND duplicate.parent_id = hub_id
              AND duplicate.project_id = lib.project_row_id
              AND duplicate.system_key = 'project_information:' || lib.project_row_id::text
              AND duplicate.archived_at IS NULL
              AND NOT EXISTS (
                  SELECT 1
                  FROM knowledge_node_supertags AS nt
                  JOIN knowledge_supertags AS st ON st.id = nt.supertag_id
                  WHERE nt.node_id = duplicate.id
                    AND st.docs_library_id = personal_id
                    AND st.system_key = 'project_info'
              );
            IF duplicate_candidate_count > 0 THEN
                RAISE EXCEPTION
                    '20260809_0020 active canonical duplicate without project_info tag for project %',
                    lib.project_row_id
                    USING DETAIL = 'review/archive duplicate before retrying';
            END IF;

            SELECT count(*)
            INTO candidate_count
            FROM knowledge_nodes AS n
            WHERE n.docs_library_id = personal_id
              AND n.parent_id = hub_id
              AND n.project_id = lib.project_row_id
              AND n.system_key = 'project_information:' || lib.project_row_id::text
              AND n.archived_at IS NULL
              AND EXISTS (
                  SELECT 1
                  FROM knowledge_node_supertags AS nt
                  JOIN knowledge_supertags AS st ON st.id = nt.supertag_id
                  WHERE nt.node_id = n.id
                    AND st.docs_library_id = personal_id
                    AND st.system_key = 'project_info'
              );
            IF candidate_count > 1 THEN
                RAISE EXCEPTION
                    '20260809_0020 ambiguous canonical project_info candidates for project %',
                    lib.project_row_id
                    USING DETAIL = 'resolve duplicate canonical roots before retrying';
            ELSIF candidate_count = 1 THEN
                SELECT n.id
                INTO root_id
                FROM knowledge_nodes AS n
                WHERE n.docs_library_id = personal_id
                  AND n.parent_id = hub_id
                  AND n.project_id = lib.project_row_id
                  AND n.system_key = 'project_information:' || lib.project_row_id::text
                  AND n.archived_at IS NULL
                  AND EXISTS (
                      SELECT 1
                      FROM knowledge_node_supertags AS nt
                      JOIN knowledge_supertags AS st ON st.id = nt.supertag_id
                      WHERE nt.node_id = n.id
                        AND st.docs_library_id = personal_id
                        AND st.system_key = 'project_info'
                  )
                ORDER BY n.id
                LIMIT 1
                FOR UPDATE;
                source_library_id := personal_id;
            ELSE
                INSERT INTO knowledge_nodes (
                    id, docs_library_id, parent_id, root_page_id, project_id,
                    title, body_json, body_text, node_type, description,
                    display_props, view_json, sort_order, created_at, updated_at,
                    system_key
                ) VALUES (
                    gen_random_uuid(), personal_id, hub_id, NULL,
                    lib.project_row_id, coalesce(lib.project_name, 'Project'),
                    '{}'::json, coalesce(lib.project_name, 'Project'), 'page',
                    coalesce(lib.project_description, ''), '{}'::json, '{}'::json,
                    0, now(), now(),
                    'project_information:' || lib.project_row_id::text
                )
                RETURNING id INTO root_id;
                UPDATE knowledge_nodes
                SET root_page_id = hub_id, updated_at = now()
                WHERE id = root_id;
                source_library_id := personal_id;
            END IF;
        END IF;

        -- A valid source pointer must not overwrite a different active
        -- Personal canonical node.  Raising here preserves both bodies and
        -- prevents a system_key hijack; the transaction rolls back before DDL.
        IF source_library_id = lib.id
           AND EXISTS (
               SELECT 1
               FROM knowledge_nodes AS existing
               WHERE existing.docs_library_id = personal_id
                 AND existing.system_key = 'project_information:' || lib.project_row_id::text
                 AND existing.id <> root_id
                 AND existing.archived_at IS NULL
           ) THEN
            RAISE EXCEPTION '20260809_0020 project % has an active canonical pointer conflict', lib.project_row_id
                USING DETAIL = jsonb_build_object(
                    'project_id', lib.project_row_id,
                    'source_library_id', lib.id,
                    'canonical_library_id', personal_id,
                    'pointer_id', root_id,
                    'recovery', 'resolve duplicate canonical identity and retry'
                )::text;
        END IF;

        TRUNCATE _aoi_retired_nodes;
        INSERT INTO _aoi_retired_nodes (id, old_parent_id, old_root_page_id, source_library_id, project_id)
        SELECT n.id, n.parent_id, n.root_page_id, lib.id, lib.project_row_id
        FROM knowledge_nodes AS n
        WHERE n.docs_library_id = lib.id;
        SELECT count(*) INTO node_count FROM _aoi_retired_nodes;
        SELECT count(*)
        INTO cross_library_parent_count
        FROM _aoi_retired_nodes AS moving
        WHERE moving.id <> root_id
          AND moving.old_parent_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM _aoi_retired_nodes AS parent
              WHERE parent.id = moving.old_parent_id
          );

        -- Never let a source node hijack an existing Personal system_key.
        -- The UUID/body remains intact; only the non-canonical legacy key is
        -- namespaced and recorded in the project audit metadata.
        FOR node_row IN
            SELECT n.id, n.system_key
            FROM knowledge_nodes AS n
            WHERE n.docs_library_id = lib.id
            ORDER BY n.id
            FOR UPDATE
        LOOP
            IF node_row.system_key IS NOT NULL
               AND node_row.id <> root_id
               AND EXISTS (
                   SELECT 1 FROM knowledge_nodes AS existing
                   WHERE existing.docs_library_id = personal_id
                     AND existing.system_key = node_row.system_key
               ) THEN
                UPDATE knowledge_nodes
                SET system_key = 'legacy_project:' || lib.project_row_id::text || ':' || node_row.id::text,
                    updated_at = now()
                WHERE id = node_row.id;
            END IF;
        END LOOP;

        -- Preserve every project tag/field UUID.  If a Personal definition
        -- has the same identity, suffix only the legacy definition rather than
        -- deleting/merging it; links, values and supertag-field edges remain.
        FOR tag_row IN
            SELECT s.*
            FROM knowledge_supertags AS s
            WHERE s.docs_library_id = lib.id
            ORDER BY s.id
            FOR UPDATE
        LOOP
            IF EXISTS (
                SELECT 1 FROM knowledge_supertags AS personal_tag
                WHERE personal_tag.docs_library_id = personal_id
                  AND (personal_tag.name = tag_row.name
                       OR (tag_row.system_key IS NOT NULL
                           AND personal_tag.system_key = tag_row.system_key))
            ) THEN
                UPDATE knowledge_supertags
                SET name = left(coalesce(tag_row.name, 'Legacy tag') || ' [legacy ' || tag_row.id::text || ']', 120),
                    system_key = CASE
                        WHEN tag_row.system_key IS NULL THEN NULL
                        ELSE tag_row.system_key || ':legacy:' || tag_row.id::text
                    END,
                    updated_at = now()
                WHERE id = tag_row.id;
            END IF;
            UPDATE knowledge_supertags
            SET docs_library_id = personal_id, updated_at = now()
            WHERE id = tag_row.id;
        END LOOP;
        UPDATE knowledge_fields
        SET docs_library_id = personal_id, updated_at = now()
        WHERE docs_library_id = lib.id;

        -- Preserve node IDs/body/revisions/attachments/edges.  Only Docs
        -- ownership columns and invalid cross-library parent links change.
        -- knowledge_revisions, knowledge_attachments, knowledge_edges, and
        -- knowledge_node_placements remain keyed by the same node UUIDs.
        -- SET docs_library_id=personal_id is the only metadata ownership move;
        -- parent_id=CASE below records cross-library links without moving parents.
        UPDATE knowledge_nodes AS n
        SET docs_library_id = personal_id,
            project_id = lib.project_row_id,
            parent_id = CASE
                WHEN n.id = root_id THEN hub_id
                WHEN EXISTS (
                    SELECT 1 FROM _aoi_retired_nodes AS parent
                    WHERE parent.id = moving.old_parent_id
                ) THEN moving.old_parent_id
                ELSE root_id
            END,
            root_page_id = hub_id,
            updated_at = now()
        FROM _aoi_retired_nodes AS moving
        WHERE n.id = moving.id;
        UPDATE projects
        SET knowledge_node_id = root_id, updated_at = now()
        WHERE id = lib.project_row_id;

        UPDATE knowledge_search_index
        SET docs_library_id = personal_id, project_id = lib.project_row_id, updated_at = now()
        WHERE docs_library_id = lib.id
           OR node_id IN (SELECT id FROM _aoi_retired_nodes);
        UPDATE knowledge_ai_suggestions
        SET docs_library_id = personal_id, updated_at = now()
        WHERE docs_library_id = lib.id
           OR node_id IN (SELECT id FROM _aoi_retired_nodes);
        -- Import jobs are moved only when their source Docs Library is this
        -- retiring library.  A source job with a different (or missing)
        -- project identity is not safe to rebind; abort before UPDATE so the
        -- transaction preserves the original provenance.  Likewise, an
        -- imported subtree item pointing at a foreign library is malformed
        -- and must not be silently adopted by this project.
        IF EXISTS (
            SELECT 1
            FROM knowledge_import_jobs AS job
            WHERE job.docs_library_id = lib.id
              AND job.project_id IS DISTINCT FROM lib.project_row_id
        ) THEN
            INSERT INTO docs_workspace_migration_log (
                project_id, legacy_workspace_id, canonical_workspace_id,
                root_node_id, moved_count, status, metadata
            ) VALUES (
                lib.project_row_id, lib.id, personal_id, root_id, 0,
                'conflict', jsonb_build_object(
                    'migration_revision', '20260809_0020',
                    'reason', 'foreign_import_job_project',
                    'source_library_id', lib.id,
                    'expected_project_id', lib.project_row_id,
                    'provenance', 'preserved_by_transaction_rollback'
                )
            );
            RAISE EXCEPTION
                '20260809_0020 source import job project mismatch for library %',
                lib.id;
        END IF;
        IF EXISTS (
            SELECT 1
            FROM knowledge_import_jobs AS job
            JOIN knowledge_import_items AS item ON item.job_id = job.id
            WHERE item.node_id IN (SELECT id FROM _aoi_retired_nodes)
              AND job.docs_library_id IS DISTINCT FROM lib.id
        ) THEN
            INSERT INTO docs_workspace_migration_log (
                project_id, legacy_workspace_id, canonical_workspace_id,
                root_node_id, moved_count, status, metadata
            ) VALUES (
                lib.project_row_id, lib.id, personal_id, root_id, 0,
                'conflict', jsonb_build_object(
                    'migration_revision', '20260809_0020',
                    'reason', 'foreign_import_job_scope',
                    'source_library_id', lib.id,
                    'provenance', 'preserved_by_transaction_rollback'
                )
            );
            RAISE EXCEPTION
                '20260809_0020 imported item references foreign job for library %',
                lib.id;
        END IF;
        UPDATE knowledge_import_jobs AS job
        SET docs_library_id = personal_id,
            project_id = lib.project_row_id,
            updated_at = now()
        WHERE job.docs_library_id = lib.id;
        IF to_regclass('public.knowledge_views') IS NOT NULL THEN
            EXECUTE $legacy_views$
                UPDATE public.knowledge_views
                SET docs_library_id = $1,
                    updated_at = now()
                WHERE docs_library_id = $2
            $legacy_views$ USING personal_id, lib.id;
        END IF;
        UPDATE knowledge_saved_views
        SET docs_library_id = personal_id, updated_at = now()
        WHERE docs_library_id = lib.id;

        INSERT INTO knowledge_node_supertags (node_id, supertag_id, created_at, updated_at)
        VALUES (root_id, canonical_tag_id, now(), now())
        ON CONFLICT (node_id, supertag_id) DO UPDATE SET updated_at = now();

        -- No source row may remain when the project identity is dropped.  A
        -- residual is an explicit pre-DDL failure; never leave it silently.
        legacy_view_count := 0;
        IF to_regclass('public.knowledge_views') IS NOT NULL THEN
            EXECUTE 'SELECT count(*) FROM public.knowledge_views WHERE docs_library_id = $1'
            INTO legacy_view_count
            USING lib.id;
        END IF;
        IF EXISTS (SELECT 1 FROM knowledge_nodes WHERE docs_library_id = lib.id)
           OR EXISTS (SELECT 1 FROM knowledge_supertags WHERE docs_library_id = lib.id)
           OR EXISTS (SELECT 1 FROM knowledge_fields WHERE docs_library_id = lib.id)
           OR EXISTS (SELECT 1 FROM knowledge_search_index WHERE docs_library_id = lib.id)
           OR EXISTS (SELECT 1 FROM knowledge_ai_suggestions WHERE docs_library_id = lib.id)
           OR EXISTS (SELECT 1 FROM knowledge_import_jobs WHERE docs_library_id = lib.id)
           OR legacy_view_count > 0
           OR EXISTS (SELECT 1 FROM knowledge_saved_views WHERE docs_library_id = lib.id)
        THEN
            RAISE EXCEPTION '20260809_0020 residual Docs rows prevent retiring library %', lib.id
                USING DETAIL = jsonb_build_object(
                    'library_id', lib.id,
                    'project_id', lib.project_row_id,
                    'recovery', 'inspect residual rows; no DDL was applied'
                )::text;
        END IF;

        metadata := jsonb_build_object(
            'migration_revision', '20260809_0020',
            'retired_project_library', true,
            'duplicate_identity_policy', 'namespace_legacy_keys_without_deleting_rows',
            'action', CASE
                WHEN coalesce(lib.project_metadata::jsonb, '{}'::jsonb)->>'isInboxDefault' = 'true'
                     OR lib.project_slug = 'inbox-project-' || lib.owner_id::text
                    THEN 'default_inbox_reconciled'
                WHEN pointer_valid THEN 'moved_valid_pointer'
                WHEN candidate_count = 1 THEN 'adopted_canonical_candidate'
                ELSE 'created_canonical_root'
            END,
            'legacy_library_id', lib.id,
            'personal_library_id', personal_id,
            'project_id', lib.project_row_id,
            'pointer_id', pointer_id,
            'pointer_reason', pointer_reason,
            'root_node_id', root_id,
            'moved_node_count', node_count,
            'cross_library_parent_links', cross_library_parent_count,
            'preservation', jsonb_build_array('node_uuid', 'body_json', 'body_text', 'revisions', 'attachments', 'edges', 'placements', 'field_values')
        );
        INSERT INTO docs_workspace_migration_log (
            project_id, legacy_workspace_id, canonical_workspace_id,
            root_node_id, moved_count, status, metadata
        ) VALUES (
            lib.project_row_id, lib.id, personal_id, root_id,
            node_count, 'moved', metadata
        );
        DELETE FROM docs_libraries WHERE id = lib.id;
    END LOOP;

    -- This invariant is checked before any ALTER/DROP.  If a row somehow
    -- appeared concurrently, the transaction aborts rather than installing a
    -- CHECK that strands it.
    IF EXISTS (SELECT 1 FROM docs_libraries WHERE library_type = 'project') THEN
        RAISE EXCEPTION '20260809_0020 cannot add library_type CHECK: project libraries remain';
    END IF;

    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema = current_schema()
                 AND table_name = 'docs_libraries'
                 AND column_name = 'project_id') THEN
        IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_docs_libraries_project_scope') THEN
            ALTER TABLE docs_libraries DROP CONSTRAINT ck_docs_libraries_project_scope;
        END IF;
        IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_knowledge_workspaces_project_id') THEN
            ALTER TABLE docs_libraries DROP CONSTRAINT fk_knowledge_workspaces_project_id;
        END IF;
        IF to_regclass('public.uq_docs_libraries_project') IS NOT NULL THEN
            DROP INDEX uq_docs_libraries_project;
        END IF;
        IF to_regclass('public.ix_docs_libraries_project') IS NOT NULL THEN
            DROP INDEX ix_docs_libraries_project;
        END IF;
        ALTER TABLE docs_libraries DROP COLUMN project_id;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_docs_libraries_library_type') THEN
        ALTER TABLE docs_libraries DROP CONSTRAINT ck_docs_libraries_library_type;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_docs_libraries_library_type_not_project') THEN
        ALTER TABLE docs_libraries
        ADD CONSTRAINT ck_docs_libraries_library_type_not_project
        CHECK (library_type <> 'project');
    END IF;
END
$$;
"""


def upgrade() -> None:
    op.execute(sa.text(_UPGRADE_SQL))


def downgrade() -> None:
    # Recreating split libraries would duplicate or discard IDs; refuse
    # explicitly so Alembic cannot move the revision pointer silently.
    raise RuntimeError(
        "20260809_0020 is forward-only; restore a backup and run a reviewed "
        "inverse migration instead of downgrading"
    )
