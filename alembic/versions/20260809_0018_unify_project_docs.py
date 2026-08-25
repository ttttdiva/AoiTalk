"""Unify project Docs trees under each owner's Personal Docs Library.

Revision ``20260808_0015`` moved project trees into one workspace per project.
That made the rows safe to migrate, but it also left the user-facing
``案件情報`` hierarchy split across those workspaces.  This forward-only
repair puts the *existing* canonical root (and every descendant) back under
the owner's personal hub without copying or deleting a document.  Node IDs,
encrypted body columns, revisions, attachments, placements and edges are
therefore preserved.

The operation is intentionally idempotent.  A project pointer is authoritative
when it resolves to a compatible node.  A null/stale pointer is adopted only
when one unambiguous personal candidate has both the project name and the
``project_info`` supertag; otherwise a new real node is created.  Deleted and
default-Inbox projects are retained and audited, but are not made visible in
the active hub.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TypeVar

import sqlalchemy as sa
from alembic import op


revision = "20260809_0018"
down_revision = "20260808_0017"
branch_labels = None
depends_on = None


_T = TypeVar("_T")


def _fixture_project_is_default_inbox(project: Mapping[str, _T]) -> bool:
    """Return whether a fixture project is a default Inbox.

    Production uses the same conservative checks in :data:`_UPGRADE_SQL`.
    Keeping this helper small makes migration contract tests independent from
    PostgreSQL and, importantly, avoids treating a project named ``Inbox`` as
    a default Inbox unless its metadata explicitly says so or its owner-bound
    ``inbox-project-<owner_id>`` slug is present.
    """

    metadata = project.get("project_metadata") or {}
    if isinstance(metadata, Mapping):
        marker = metadata.get("isInboxDefault")
        if marker is True or str(marker).lower() == "true":
            return True
    owner_id = project.get("owner_id")
    slug = str(project.get("slug") or "").strip().lower()
    return bool(owner_id and slug == f"inbox-project-{owner_id}".lower())


_UPGRADE_SQL = r"""
DO $$
DECLARE
    project_row RECORD;
    restore_row RECORD;
    restore_node RECORD;
    root_row RECORD;
    node_row RECORD;
    tag_row RECORD;
    field_row RECORD;
    view_row RECORD;
    workspace_row RECORD;
    personal_id uuid;
    hub_id uuid;
    root_id uuid;
    candidate_id uuid;
    candidate_count integer;
    source_workspace_id uuid;
    canonical_tag_id uuid;
    mapped_tag_id uuid;
    mapped_field_id uuid;
    node_count integer;
    created_root boolean;
    adopted_root boolean;
    source_workspace_count integer;
    cross_library_parent_count integer;
    duplicate_candidate_count integer;
    legacy_key_rename_count integer;
    pointer_valid boolean;
    pointer_reason text;
    stale_pointer_id uuid;
    restore_parent_id uuid;
    restore_root_page_id uuid;
    restore_project_id uuid;
    relation_quarantined boolean;
    deep_node_id uuid;
    metadata jsonb;
    legacy_view_count integer;
BEGIN
    -- Temp maps are scoped to this migration transaction.  Keeping mappings
    -- explicit is safer than deleting project tags/fields by name: personal
    -- tags are shared and must never be removed as a side effect.
    CREATE TEMP TABLE IF NOT EXISTS _aoi_project_nodes (
        id uuid PRIMARY KEY,
        old_workspace_id uuid NOT NULL,
        old_parent_id uuid,
        old_root_page_id uuid,
        depth integer NOT NULL DEFAULT 0,
        path uuid[] NOT NULL DEFAULT '{}'::uuid[]
    ) ON COMMIT DROP;
    CREATE TEMP TABLE IF NOT EXISTS _aoi_project_tag_map (
        old_id uuid PRIMARY KEY,
        new_id uuid NOT NULL
    ) ON COMMIT DROP;
    CREATE TEMP TABLE IF NOT EXISTS _aoi_project_field_map (
        old_id uuid PRIMARY KEY,
        new_id uuid NOT NULL
    ) ON COMMIT DROP;

    -- Revision 0015 used an unconstrained recursive walk.  If a child from a
    -- different legacy workspace was pulled into that walk, its node audit
    -- entry is the only authoritative record of the original workspace and
    -- parent.  Repair those rows before this revision starts moving project
    -- trees.  The old key was ``workspace_id`` in early 0015 builds; repaired
    -- builds use ``old_workspace_id``.  We accept both without changing 0015.
    CREATE TEMP TABLE IF NOT EXISTS _aoi_cross_library_restore (
        log_id uuid NOT NULL,
        project_id uuid NOT NULL,
        node_id uuid NOT NULL,
        legacy_workspace_id uuid,
        canonical_workspace_id uuid,
        root_node_id uuid,
        old_workspace_id uuid NOT NULL,
        old_parent_id uuid,
        old_root_page_id uuid,
        old_project_id uuid
    ) ON COMMIT DROP;
    INSERT INTO _aoi_cross_library_restore (
        log_id, project_id, node_id, legacy_workspace_id,
        canonical_workspace_id, root_node_id, old_workspace_id, old_parent_id,
        old_root_page_id, old_project_id
    )
    SELECT logged.id,
           logged.project_id,
           (entry->>'id')::uuid,
           logged.legacy_workspace_id,
           logged.canonical_workspace_id,
           logged.root_node_id,
           COALESCE(
               NULLIF(entry->>'old_workspace_id', '')::uuid,
               NULLIF(entry->>'workspace_id', '')::uuid
           ),
           NULLIF(
               COALESCE(entry->>'old_parent_id', entry->>'parent_id'), ''
           )::uuid,
           NULLIF(
               COALESCE(entry->>'old_root_page_id', entry->>'root_page_id'), ''
           )::uuid,
           NULLIF(
               COALESCE(entry->>'old_project_id', entry->>'project_id'), ''
           )::uuid
    FROM docs_workspace_migration_log AS logged
    CROSS JOIN LATERAL jsonb_array_elements(
        COALESCE(logged.metadata->'nodes', '[]'::jsonb)
    ) AS entries(entry)
    WHERE logged.metadata ? 'nodes'
      AND (
          NOT (logged.metadata ? 'migration_revision')
          OR logged.metadata->>'migration_revision' IN ('20260808_0015', '20260809_0015')
      )
      AND (entry ? 'old_workspace_id' OR entry ? 'workspace_id')
      AND NULLIF(entry->>'id', '') IS NOT NULL
      AND COALESCE(
          NULLIF(entry->>'old_workspace_id', '')::uuid,
          NULLIF(entry->>'workspace_id', '')::uuid
      ) IS DISTINCT FROM logged.legacy_workspace_id;

    IF EXISTS (
        SELECT 1
        FROM _aoi_cross_library_restore
        GROUP BY node_id
        HAVING count(*) > 1
            OR count(DISTINCT old_workspace_id) > 1
    ) THEN
        RAISE EXCEPTION
            '0018 cross-library repair is ambiguous for one or more node UUIDs';
    END IF;

    FOR restore_row IN
        SELECT *
        FROM _aoi_cross_library_restore
        ORDER BY node_id, log_id
    LOOP
        IF restore_row.legacy_workspace_id IS NULL
           OR restore_row.canonical_workspace_id IS NULL THEN
            INSERT INTO docs_workspace_migration_log (
                project_id, moved_count, status, metadata
            ) VALUES (
                restore_row.project_id, 0, 'conflict', jsonb_build_object(
                    'migration_revision', '20260809_0018',
                    'node_id', restore_row.node_id,
                    'reason', 'missing_canonical_source'
                )
            );
            RAISE EXCEPTION
                '0018 cross-library repair has no canonical source for node %',
                restore_row.node_id;
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM knowledge_workspaces AS source
            WHERE source.id = restore_row.old_workspace_id
        ) THEN
            INSERT INTO docs_workspace_migration_log (
                project_id, moved_count, status, metadata
            ) VALUES (
                restore_row.project_id, 0, 'conflict', jsonb_build_object(
                    'migration_revision', '20260809_0018',
                    'node_id', restore_row.node_id,
                    'reason', 'missing_original_workspace'
                )
            );
            RAISE EXCEPTION
                '0018 cross-library repair source workspace % is missing for node %',
                restore_row.old_workspace_id, restore_row.node_id;
        END IF;

        SELECT n.*
        INTO restore_node
        FROM knowledge_nodes AS n
        WHERE n.id = restore_row.node_id
        FOR UPDATE;
        IF NOT FOUND THEN
            INSERT INTO docs_workspace_migration_log (
                project_id, moved_count, status, metadata
            ) VALUES (
                restore_row.project_id, 0, 'conflict', jsonb_build_object(
                    'migration_revision', '20260809_0018',
                    'node_id', restore_row.node_id,
                    'reason', 'missing_audited_node'
                )
            );
            RAISE EXCEPTION
                '0018 cross-library repair node % is missing',
                restore_row.node_id;
        END IF;

        -- Already-restored rows are idempotent, but their identity must still
        -- match the audit snapshot.  A node freshly moved by 0015 may have
        -- been reparented to the project root (or NULL when its old parent was
        -- outside the source subtree), and its root_page_id is the project
        -- root.  Accept only those deterministic shapes; later edits raise.
        IF restore_node.workspace_id = restore_row.old_workspace_id
           AND restore_node.parent_id IS NOT DISTINCT FROM restore_row.old_parent_id
           AND restore_node.root_page_id IS NOT DISTINCT FROM restore_row.old_root_page_id
           AND restore_node.project_id IS NOT DISTINCT FROM restore_row.old_project_id THEN
            CONTINUE;
        END IF;
        IF restore_node.workspace_id <> restore_row.canonical_workspace_id
           OR (
               restore_node.parent_id IS DISTINCT FROM restore_row.old_parent_id
               AND restore_node.parent_id IS DISTINCT FROM restore_row.root_node_id
               AND restore_node.parent_id IS NOT NULL
           )
           OR restore_node.root_page_id IS DISTINCT FROM restore_row.root_node_id
           OR restore_node.project_id IS DISTINCT FROM restore_row.project_id THEN
            INSERT INTO docs_workspace_migration_log (
                project_id, legacy_workspace_id, canonical_workspace_id,
                root_node_id, moved_count, status, metadata
            ) VALUES (
                restore_row.project_id, restore_row.legacy_workspace_id,
                restore_row.canonical_workspace_id, restore_row.root_node_id,
                0, 'conflict', jsonb_build_object(
                    'migration_revision', '20260809_0018',
                    'node_id', restore_row.node_id,
                    'reason', 'later_edit_shape_conflict'
                )
            );
            RAISE EXCEPTION
                '0018 cross-library repair conflicts with later edits for node %',
                restore_row.node_id;
        END IF;

        -- Restore a captured relation only when both endpoints remain in the
        -- original workspace.  A cross-library endpoint is quarantined with
        -- parent NULL/root self; the original IDs stay in the audit metadata.
        relation_quarantined := false;
        restore_project_id := restore_row.old_project_id;
        IF restore_project_id IS NOT NULL
           AND NOT EXISTS (
               SELECT 1
               FROM knowledge_workspaces AS source
               WHERE source.id = restore_row.old_workspace_id
                 AND source.project_id = restore_project_id
           ) THEN
            restore_project_id := NULL;
            relation_quarantined := true;
        END IF;
        restore_parent_id := restore_row.old_parent_id;
        restore_root_page_id := restore_row.old_root_page_id;
        IF (
            restore_row.old_parent_id IS NOT NULL
            AND NOT EXISTS (
                SELECT 1 FROM knowledge_nodes AS parent
                WHERE parent.id = restore_row.old_parent_id
                  AND parent.workspace_id = restore_row.old_workspace_id
                  AND (
                      parent.project_id = restore_project_id
                      OR (
                          parent.project_id IS NULL
                          AND parent.system_key = 'project_information_root'
                          AND parent.parent_id IS NULL
                          AND parent.archived_at IS NULL
                      )
                  )
            )
        ) OR (
            restore_row.old_root_page_id IS NOT NULL
            AND NOT EXISTS (
                SELECT 1 FROM knowledge_nodes AS root
                WHERE root.id = restore_row.old_root_page_id
                  AND root.workspace_id = restore_row.old_workspace_id
                  AND (
                      root.project_id = restore_project_id
                      OR (
                          root.project_id IS NULL
                          AND root.system_key = 'project_information_root'
                          AND root.parent_id IS NULL
                          AND root.archived_at IS NULL
                      )
                  )
            )
        ) THEN
            relation_quarantined := true;
            restore_parent_id := NULL;
            restore_root_page_id := restore_node.id;
        END IF;

        UPDATE knowledge_nodes
        SET workspace_id = restore_row.old_workspace_id,
            parent_id = restore_parent_id,
            root_page_id = restore_root_page_id,
            project_id = restore_project_id,
            updated_at = now()
        WHERE id = restore_row.node_id;
        UPDATE knowledge_search_index
        SET workspace_id = restore_row.old_workspace_id,
            -- Keep secondary indexes consistent with the quarantined node:
            -- a project relation is retained only when its owner workspace
            -- actually belongs to that project.
            project_id = restore_project_id,
            updated_at = now()
        WHERE node_id = restore_row.node_id;
        UPDATE knowledge_ai_suggestions
        SET workspace_id = restore_row.old_workspace_id
        WHERE node_id = restore_row.node_id;
        UPDATE docs_workspace_migration_log AS audit
        SET metadata = audit.metadata || jsonb_build_object(
            'cross_library_repairs',
            coalesce(audit.metadata->'cross_library_repairs', '[]'::jsonb)
                || jsonb_build_array(jsonb_build_object(
                    'node_id', restore_row.node_id,
                    'old_workspace_id', restore_row.old_workspace_id,
                    'old_parent_id', restore_row.old_parent_id,
                    'old_root_page_id', restore_row.old_root_page_id,
                    'restored_parent_id', restore_parent_id,
                    'restored_root_page_id', restore_root_page_id,
                    'restored_project_id', restore_project_id,
                    'quarantined', relation_quarantined
                ))
        )
        WHERE audit.id = restore_row.log_id;
    END LOOP;

    IF EXISTS (
        SELECT 1
        FROM _aoi_cross_library_restore AS repaired
        JOIN knowledge_nodes AS child ON child.id = repaired.node_id
        LEFT JOIN knowledge_nodes AS parent ON parent.id = child.parent_id
        LEFT JOIN knowledge_nodes AS root ON root.id = child.root_page_id
        WHERE (parent.id IS NOT NULL AND parent.workspace_id <> child.workspace_id)
           OR (root.id IS NOT NULL AND root.workspace_id <> child.workspace_id)
           OR (
               parent.id IS NOT NULL
               AND parent.project_id IS DISTINCT FROM child.project_id
               AND NOT (
                   parent.project_id IS NULL
                   AND parent.system_key = 'project_information_root'
                   AND parent.parent_id IS NULL
                   AND parent.archived_at IS NULL
               )
           )
           OR (
               root.id IS NOT NULL
               AND root.project_id IS DISTINCT FROM child.project_id
               AND NOT (
                   root.project_id IS NULL
                   AND root.system_key = 'project_information_root'
                   AND root.parent_id IS NULL
                   AND root.archived_at IS NULL
               )
           )
    ) THEN
        RAISE EXCEPTION
            '0018 cross-library repair left a parent/root edge across libraries';
    END IF;

    FOR project_row IN
        SELECT p.*
        FROM projects AS p
        ORDER BY p.id
        FOR UPDATE
    LOOP
        -- Re-running an interrupted deployment is safe.  The entire upgrade
        -- is transactional, but the guard also avoids duplicate audit rows
        -- when a DBA deliberately replays the revision in a repaired DB.
        IF EXISTS (
            SELECT 1
            FROM docs_workspace_migration_log AS logged
            WHERE logged.project_id = project_row.id
            AND logged.metadata->>'migration_revision' = '20260809_0018'
        ) THEN
            CONTINUE;
        END IF;

        -- ``project_metadata`` is JSON in older installations, so cast to
        -- jsonb only after coalescing a null value.  A project named Inbox is
        -- not skipped unless the explicit marker or owner-bound canonical
        -- Inbox slug exists.
        IF coalesce(project_row.project_metadata::jsonb, '{}'::jsonb)->>'isInboxDefault' = 'true'
           OR project_row.slug = 'inbox-project-' || project_row.owner_id::text THEN
            INSERT INTO docs_workspace_migration_log (
                project_id, root_node_id, moved_count, status, metadata
            ) VALUES (
                project_row.id,
                project_row.knowledge_node_id,
                0,
                'already_canonical',
                jsonb_build_object(
                    'migration_revision', '20260809_0018',
                    'action', 'skipped_default_inbox'
                )
            );
            CONTINUE;
        END IF;

        -- Deleted projects stay queryable for audit/recovery, but are not
        -- reintroduced into the normal active hub tree.
        IF project_row.deleted_at IS NOT NULL THEN
            INSERT INTO docs_workspace_migration_log (
                project_id, root_node_id, moved_count, status, metadata
            ) VALUES (
                project_row.id,
                project_row.knowledge_node_id,
                0,
                'already_canonical',
                jsonb_build_object(
                    'migration_revision', '20260809_0018',
                    'action', 'skipped_deleted',
                    'deleted_at', project_row.deleted_at
                )
            );
            CONTINUE;
        END IF;

        -- Resolve the owner's canonical personal library.  A partially
        -- upgraded installation may have no row yet; create one rather than
        -- falling back to an ownerless workspace.
        SELECT w.id
        INTO personal_id
        FROM knowledge_workspaces AS w
        WHERE w.workspace_type = 'personal'
          AND w.owner_user_id = project_row.owner_id
        ORDER BY w.created_at NULLS LAST, w.id
        LIMIT 1
        FOR UPDATE;

        IF personal_id IS NULL THEN
            personal_id := gen_random_uuid();
            INSERT INTO knowledge_workspaces (
                id, name, description, owner_user_id, workspace_type,
                project_id, settings_json, created_at, updated_at
            ) VALUES (
                personal_id,
                'Personal Docs',
                NULL,
                project_row.owner_id,
                'personal',
                NULL,
                '{}'::json,
                now(),
                now()
            );
        END IF;

        -- There is one active hub per personal library.  If malformed legacy
        -- data contains duplicates, choose the stable UUID and leave the
        -- others untouched for audit rather than deleting user data.
        SELECT n.id
        INTO hub_id
        FROM knowledge_nodes AS n
        WHERE n.workspace_id = personal_id
          AND n.project_id IS NULL
          AND n.system_key = 'project_information_root'
          AND n.archived_at IS NULL
        ORDER BY n.id
        LIMIT 1
        FOR UPDATE;

        IF hub_id IS NULL THEN
            INSERT INTO knowledge_nodes (
                id, workspace_id, parent_id, root_page_id, project_id,
                title, body_json, body_text, node_type, description,
                display_props, view_json, sort_order, system_key,
                created_at, updated_at
            ) VALUES (
                gen_random_uuid(), personal_id, NULL, NULL, NULL,
                '案件情報', '{}'::json, '案件情報', 'page', '', '{}'::json, '{}',
                0, 'project_information_root', now(), now()
            )
            RETURNING id INTO hub_id;

            UPDATE knowledge_nodes
            SET root_page_id = hub_id,
                updated_at = now()
            WHERE id = hub_id;
        END IF;

        -- The tag is an ownership/schema marker only.  ACL is derived from
        -- project membership and the project_id reverse link, never from this
        -- tag alone.  Missing definitions are recreated in the personal
        -- library; existing personal tags are shared and are not deleted.
        SELECT s.id
        INTO canonical_tag_id
        FROM knowledge_supertags AS s
        WHERE s.workspace_id = personal_id
          AND s.system_key = 'project_info'
        ORDER BY s.id
        LIMIT 1
        FOR UPDATE;

        IF canonical_tag_id IS NULL THEN
            INSERT INTO knowledge_supertags (
                id, workspace_id, parent_supertag_id, system_key, name,
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

        root_id := project_row.knowledge_node_id;
        stale_pointer_id := root_id;
        candidate_count := 0;
        legacy_key_rename_count := 0;
        pointer_valid := false;
        pointer_reason := NULL;
        created_root := false;
        adopted_root := false;
        source_workspace_id := NULL;

        -- A pointer is authoritative only when *all* canonical identity
        -- invariants hold.  In particular, an ordinary Home node (even one
        -- with a matching title) is never reclassified as project Docs.
        -- Personal pointers must already be under this owner's hub; legacy
        -- project pointers must be in this project's source library.
        IF root_id IS NOT NULL THEN
            SELECT n.*
            INTO root_row
            FROM knowledge_nodes AS n
            JOIN knowledge_workspaces AS source
              ON source.id = n.workspace_id
            WHERE n.id = root_id
              AND n.project_id = project_row.id
              AND n.system_key = 'project_information:' || project_row.id::text
              AND (n.root_page_id = n.id OR n.root_page_id = hub_id)
              AND EXISTS (
                  SELECT 1
                  FROM knowledge_node_supertags AS node_tag
                  JOIN knowledge_supertags AS tag ON tag.id = node_tag.supertag_id
                  WHERE node_tag.node_id = n.id
                    AND tag.system_key = 'project_info'
                    AND tag.workspace_id = n.workspace_id
              )
              AND (
                  (source.workspace_type = 'personal'
                   AND source.owner_user_id = project_row.owner_id
                   AND n.parent_id = hub_id
                   AND n.root_page_id = hub_id)
                  OR (source.workspace_type = 'project'
                      AND source.project_id = project_row.id)
              )
            FOR UPDATE;
            IF FOUND THEN
                pointer_valid := true;
                source_workspace_id := root_row.workspace_id;
            ELSE
                pointer_reason := CASE
                    WHEN NOT EXISTS (SELECT 1 FROM knowledge_nodes WHERE id = root_id)
                        THEN 'missing_pointer'
                    WHEN EXISTS (SELECT 1 FROM knowledge_nodes WHERE id = root_id AND system_key = 'home')
                        THEN 'ordinary_home_pointer'
                    ELSE 'canonical_identity_mismatch'
                END;
                root_id := NULL;
            END IF;
        ELSE
            pointer_reason := 'null_pointer';
        END IF;

        -- Recovery candidates are equally strict: owner Personal + canonical
        -- hub parent + exact project key + project_id + project_info tag.
        -- Name alone is never an adoption signal.
        IF NOT pointer_valid THEN
            candidate_id := NULL;
            candidate_count := 0;
            SELECT count(*)
            INTO candidate_count
            FROM knowledge_nodes AS n
            WHERE n.workspace_id = personal_id
              AND n.parent_id = hub_id
              AND n.project_id = project_row.id
              AND n.system_key = 'project_information:' || project_row.id::text
              AND n.archived_at IS NULL
              AND EXISTS (
                  SELECT 1
                  FROM knowledge_node_supertags AS node_tag
                  JOIN knowledge_supertags AS tag ON tag.id = node_tag.supertag_id
                  WHERE node_tag.node_id = n.id
                    AND tag.workspace_id = personal_id
                    AND tag.system_key = 'project_info'
              );
            IF candidate_count = 1 THEN
                SELECT n.id
                INTO candidate_id
                FROM knowledge_nodes AS n
                WHERE n.workspace_id = personal_id
                  AND n.parent_id = hub_id
                  AND n.project_id = project_row.id
                  AND n.system_key = 'project_information:' || project_row.id::text
                  AND n.archived_at IS NULL
                ORDER BY n.id
                LIMIT 1;
                root_id := candidate_id;
                source_workspace_id := personal_id;
                adopted_root := true;
            ELSIF candidate_count > 1 THEN
                pointer_reason := 'ambiguous_canonical_candidates';
            END IF;
        END IF;

        IF root_id IS NULL THEN
            INSERT INTO knowledge_nodes (
                id, workspace_id, parent_id, root_page_id, project_id,
                title, body_json, body_text, node_type, description,
                display_props, view_json, sort_order, system_key,
                created_at, updated_at
            ) VALUES (
                gen_random_uuid(), personal_id, hub_id, NULL, project_row.id,
                coalesce(project_row.name, 'Project'), '{}'::json,
                coalesce(project_row.name, 'Project'),
                'page', coalesce(project_row.description, ''), '{}', '{}',
                0, 'project_information:' || project_row.id::text,
                now(), now()
            )
            RETURNING id INTO root_id;

            UPDATE knowledge_nodes
            SET root_page_id = hub_id,
                updated_at = now()
            WHERE id = root_id;
            source_workspace_id := personal_id;
            created_root := true;
        END IF;

        -- The transfer walk below is deliberately bounded at depth 512.  Do
        -- not silently leave a deeper descendant in the source workspace:
        -- preflight the complete source-scoped walk first and fail closed so
        -- the surrounding Alembic transaction can roll back any root repair
        -- or other partial changes made for this project.
        deep_node_id := NULL;
        IF root_id IS NOT NULL AND source_workspace_id IS NOT NULL THEN
            WITH RECURSIVE deep_subtree AS (
                SELECT n.id,
                       n.workspace_id,
                       0::integer AS depth,
                       ARRAY[n.id]::uuid[] AS path
                FROM knowledge_nodes AS n
                WHERE n.id = root_id
                  AND n.workspace_id = source_workspace_id
                UNION
                SELECT child.id,
                       child.workspace_id,
                       parent.depth + 1,
                       parent.path || child.id
                FROM knowledge_nodes AS child
                JOIN deep_subtree AS parent ON parent.id = child.parent_id
                WHERE child.workspace_id = source_workspace_id
                  AND parent.depth < 513
                  AND NOT child.id = ANY(parent.path)
            )
            SELECT id
            INTO deep_node_id
            FROM deep_subtree
            WHERE depth > 512
            ORDER BY depth, id
            LIMIT 1;
            IF deep_node_id IS NOT NULL THEN
                RAISE EXCEPTION
                    '20260809_0018 project % subtree exceeds depth 512 at node %',
                    project_row.id, deep_node_id
                    USING DETAIL = jsonb_build_object(
                        'project_id', project_row.id,
                        'root_node_id', root_id,
                        'source_workspace_id', source_workspace_id,
                        'overflow_node_id', deep_node_id,
                        'max_depth', 512,
                        'recovery', 'shorten or quarantine the source subtree before retrying'
                    )::text;
            END IF;
        END IF;

        -- A resolved root may have been archived while the project remains
        -- active (an archived-root production case).  Restore the root
        -- itself; descendant archive state is user data and is preserved.
        UPDATE knowledge_nodes
        SET archived_at = NULL,
            updated_at = now()
        WHERE id = root_id
          AND project_row.deleted_at IS NULL;

        -- Normalize the canonical identity fields without touching title/body
        -- ciphertext.  The project pointer is the authoritative reverse link.
        UPDATE knowledge_nodes
        SET project_id = project_row.id,
            system_key = 'project_information:' || project_row.id::text,
            parent_id = hub_id,
            root_page_id = hub_id,
            updated_at = now()
        WHERE id = root_id;

        -- Snapshot the complete, cycle-safe subtree before changing workspace
        -- or parent columns.  Orphan descendants are reattached to the root;
        -- no row is dropped.
        TRUNCATE _aoi_project_nodes;
        WITH RECURSIVE subtree AS (
            SELECT n.id,
                   n.workspace_id,
                   n.parent_id,
                   n.root_page_id,
                   0::integer AS depth,
                   ARRAY[n.id]::uuid[] AS path
            FROM knowledge_nodes AS n
            WHERE n.id = root_id
              AND n.workspace_id = source_workspace_id
            UNION
            SELECT child.id,
                   child.workspace_id,
                   child.parent_id,
                   child.root_page_id,
                   parent.depth + 1,
                   parent.path || child.id
            FROM knowledge_nodes AS child
            JOIN subtree AS parent ON parent.id = child.parent_id
            WHERE child.workspace_id = source_workspace_id
              AND parent.depth < 512
              AND NOT child.id = ANY(parent.path)
        )
        INSERT INTO _aoi_project_nodes (
            id, old_workspace_id, old_parent_id, old_root_page_id, depth, path
        )
        SELECT id, workspace_id, parent_id, root_page_id, depth, path
        FROM subtree;

        SELECT count(*) INTO node_count FROM _aoi_project_nodes;
        SELECT count(*)
        INTO cross_library_parent_count
        FROM _aoi_project_nodes AS moving
        WHERE moving.id <> root_id
          AND moving.old_parent_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM _aoi_project_nodes AS parent
              WHERE parent.id = moving.old_parent_id
          );

        -- A valid source pointer must not hijack a different active Personal
        -- canonical node.  Stale/new-root cases namespace the old source key
        -- instead, preserving that row's UUID/body for the later retirement
        -- pass and recording the recovery in migration metadata.
        IF source_workspace_id <> personal_id
           AND EXISTS (
               SELECT 1 FROM knowledge_nodes AS existing
               WHERE existing.workspace_id = personal_id
                 AND existing.system_key = 'project_information:' || project_row.id::text
                 AND existing.id <> root_id
                 AND existing.archived_at IS NULL
           )
           AND pointer_valid THEN
            RAISE EXCEPTION 'project % has an active canonical pointer conflict', project_row.id
                USING DETAIL = jsonb_build_object(
                    'project_id', project_row.id,
                    'source_workspace_id', source_workspace_id,
                    'personal_workspace_id', personal_id,
                    'pointer_root_id', root_id,
                    'recovery', 'resolve duplicate canonical identity and retry'
                )::text;
        END IF;
        FOR node_row IN
            SELECT n.id, n.system_key
            FROM knowledge_nodes AS n
            JOIN _aoi_project_nodes AS moving ON moving.id = n.id
            WHERE n.system_key IS NOT NULL
            ORDER BY n.id
            FOR UPDATE
        LOOP
            IF node_row.id <> root_id
               AND EXISTS (
                   SELECT 1 FROM knowledge_nodes AS existing
                   WHERE existing.workspace_id = personal_id
                     AND existing.system_key = node_row.system_key
               ) THEN
                UPDATE knowledge_nodes
                SET system_key = 'legacy_project:' || project_row.id::text || ':' || node_row.id::text,
                    updated_at = now()
                WHERE id = node_row.id;
                legacy_key_rename_count := legacy_key_rename_count + 1;
            END IF;
        END LOOP;

        -- Tag/field repair is performed before deleting an empty project
        -- library.  Existing personal definitions are reused by system_key or
        -- name; project-only definitions are moved with their IDs intact.
        TRUNCATE _aoi_project_tag_map;
        FOR tag_row IN
            SELECT s.*
            FROM knowledge_supertags AS s
            JOIN knowledge_workspaces AS w ON w.id = s.workspace_id
            WHERE w.workspace_type = 'project'
              AND w.project_id = project_row.id
            ORDER BY s.id
            FOR UPDATE
        LOOP
            SELECT personal_tag.id
            INTO mapped_tag_id
            FROM knowledge_supertags AS personal_tag
            WHERE personal_tag.workspace_id = personal_id
              AND (
                  (tag_row.system_key IS NOT NULL
                   AND personal_tag.system_key = tag_row.system_key)
                  OR personal_tag.name = tag_row.name
              )
            ORDER BY CASE
                WHEN tag_row.system_key IS NOT NULL
                     AND personal_tag.system_key = tag_row.system_key THEN 0
                ELSE 1
            END, personal_tag.id
            LIMIT 1;

            IF mapped_tag_id IS NULL THEN
                UPDATE knowledge_supertags
                SET workspace_id = personal_id,
                    updated_at = now()
                WHERE id = tag_row.id;
                mapped_tag_id := tag_row.id;
            END IF;

            INSERT INTO _aoi_project_tag_map (old_id, new_id)
            VALUES (tag_row.id, mapped_tag_id)
            ON CONFLICT (old_id) DO UPDATE SET new_id = EXCLUDED.new_id;
        END LOOP;

        UPDATE knowledge_supertags AS old_tag
        SET parent_supertag_id = mapped.new_id,
            updated_at = now()
        FROM _aoi_project_tag_map AS mapped
        WHERE old_tag.id = mapped.old_id
          AND mapped.old_id <> mapped.new_id;

        -- Replace node/tag links while preserving the primary-key invariant.
        DELETE FROM knowledge_node_supertags AS old_link
        USING _aoi_project_tag_map AS mapped
        WHERE old_link.supertag_id = mapped.old_id
          AND mapped.old_id <> mapped.new_id
          AND EXISTS (
              SELECT 1
              FROM knowledge_node_supertags AS canonical_link
              WHERE canonical_link.node_id = old_link.node_id
                AND canonical_link.supertag_id = mapped.new_id
          );
        UPDATE knowledge_node_supertags AS node_tag
        SET supertag_id = mapped.new_id,
            updated_at = now()
        FROM _aoi_project_tag_map AS mapped
        WHERE node_tag.supertag_id = mapped.old_id
          AND mapped.old_id <> mapped.new_id;

        TRUNCATE _aoi_project_field_map;
        FOR field_row IN
            SELECT f.*, mapped.new_id AS mapped_supertag_id
            FROM knowledge_fields AS f
            JOIN _aoi_project_tag_map AS mapped ON mapped.old_id = f.supertag_id
            WHERE f.workspace_id IN (
                SELECT id FROM knowledge_workspaces
                WHERE workspace_type = 'project' AND project_id = project_row.id
            )
            ORDER BY f.id
            FOR UPDATE
        LOOP
            SELECT personal_field.id
            INTO mapped_field_id
            FROM knowledge_fields AS personal_field
            WHERE personal_field.workspace_id = personal_id
              AND personal_field.supertag_id = field_row.mapped_supertag_id
              AND (
                  (field_row.system_key IS NOT NULL
                   AND personal_field.system_key = field_row.system_key)
                  OR personal_field.name = field_row.name
              )
            ORDER BY CASE
                WHEN field_row.system_key IS NOT NULL
                     AND personal_field.system_key = field_row.system_key THEN 0
                ELSE 1
            END, personal_field.id
            LIMIT 1;

            IF mapped_field_id IS NULL THEN
                UPDATE knowledge_fields
                SET workspace_id = personal_id,
                    supertag_id = field_row.mapped_supertag_id,
                    updated_at = now()
                WHERE id = field_row.id;
                mapped_field_id := field_row.id;
            END IF;

            INSERT INTO _aoi_project_field_map (old_id, new_id)
            VALUES (field_row.id, mapped_field_id)
            ON CONFLICT (old_id) DO UPDATE SET new_id = EXCLUDED.new_id;
        END LOOP;

        -- Values are merged rather than discarded when a canonical field was
        -- already present.  Existing non-null values win; otherwise the
        -- project value is copied before the old key is removed.
        FOR field_row IN
            SELECT old_value.*, mapped.new_id AS mapped_field_id
            FROM knowledge_field_values AS old_value
            JOIN _aoi_project_field_map AS mapped
              ON mapped.old_id = old_value.field_id
            WHERE mapped.old_id <> mapped.new_id
            FOR UPDATE
        LOOP
            IF EXISTS (
                SELECT 1
                FROM knowledge_field_values AS existing_value
                WHERE existing_value.node_id = field_row.node_id
                  AND existing_value.field_id = field_row.mapped_field_id
            ) THEN
                UPDATE knowledge_field_values AS existing_value
                SET value_json = coalesce(existing_value.value_json, field_row.value_json),
                    value_text = coalesce(existing_value.value_text, field_row.value_text),
                    value_number = coalesce(existing_value.value_number, field_row.value_number),
                    value_datetime = coalesce(existing_value.value_datetime, field_row.value_datetime),
                    target_node_id = coalesce(existing_value.target_node_id, field_row.target_node_id),
                    updated_at = now(),
                    updated_by = coalesce(existing_value.updated_by, field_row.updated_by)
                WHERE existing_value.node_id = field_row.node_id
                  AND existing_value.field_id = field_row.mapped_field_id;
                DELETE FROM knowledge_field_values
                WHERE node_id = field_row.node_id
                  AND field_id = field_row.field_id;
            ELSE
                UPDATE knowledge_field_values
                SET field_id = field_row.mapped_field_id,
                    updated_at = now()
                WHERE node_id = field_row.node_id
                  AND field_id = field_row.field_id;
            END IF;
        END LOOP;

        DELETE FROM knowledge_supertag_fields AS old_link
        USING _aoi_project_tag_map AS tag_map,
              _aoi_project_field_map AS field_map
        WHERE old_link.supertag_id = tag_map.old_id
          AND old_link.field_id = field_map.old_id
          AND (
              tag_map.old_id <> tag_map.new_id
              OR field_map.old_id <> field_map.new_id
          )
          AND EXISTS (
              SELECT 1
              FROM knowledge_supertag_fields AS canonical_link
              WHERE canonical_link.supertag_id = tag_map.new_id
                AND canonical_link.field_id = field_map.new_id
          );
        UPDATE knowledge_supertag_fields AS link
        SET supertag_id = tag_map.new_id,
            field_id = field_map.new_id
        FROM _aoi_project_tag_map AS tag_map,
             _aoi_project_field_map AS field_map
        WHERE link.supertag_id = tag_map.old_id
          AND link.field_id = field_map.old_id;

        -- Saved views, generic views and AI/search projections are Docs
        -- metadata and follow the same canonical library.  Filer ``fw_*``
        -- tables intentionally do not appear here.  ``knowledge_edges``,
        -- attachments and revisions carry globally stable node IDs and are
        -- deliberately not rewritten or deleted.
        UPDATE knowledge_saved_views AS saved
        SET supertag_id = tag_map.new_id,
            updated_at = now()
        FROM _aoi_project_tag_map AS tag_map
        WHERE saved.supertag_id = tag_map.old_id;
        UPDATE knowledge_saved_views AS saved
        SET workspace_id = personal_id,
            updated_at = now()
        FROM knowledge_workspaces AS source
        WHERE saved.workspace_id = source.id
          AND source.workspace_type = 'project'
          AND source.project_id = project_row.id;
        IF to_regclass('public.knowledge_views') IS NOT NULL THEN
            EXECUTE $legacy_views$
                UPDATE public.knowledge_views AS view_item
                SET workspace_id = $1,
                    updated_at = now()
                FROM public.knowledge_workspaces AS source
                WHERE view_item.workspace_id = source.id
                  AND source.workspace_type = 'project'
                  AND source.project_id = $2
            $legacy_views$ USING personal_id, project_row.id;
        END IF;

        -- Move the full node subtree.  Parent/root IDs remain stable; only an
        -- orphaned descendant is attached to the canonical root so every row
        -- remains reachable from the hub.
        UPDATE knowledge_nodes AS node
        SET workspace_id = personal_id,
            project_id = project_row.id,
            parent_id = CASE
                WHEN node.id = root_id THEN hub_id
                WHEN EXISTS (
                    SELECT 1 FROM _aoi_project_nodes AS parent
                    WHERE parent.id = moving.old_parent_id
                ) THEN moving.old_parent_id
                ELSE root_id
            END,
            root_page_id = hub_id,
            updated_at = now()
        FROM _aoi_project_nodes AS moving
        WHERE node.id = moving.id;

        UPDATE projects
        SET knowledge_node_id = root_id,
            updated_at = now()
        WHERE id = project_row.id;

        UPDATE knowledge_search_index AS search_index
        SET workspace_id = personal_id,
            project_id = project_row.id,
            updated_at = now()
        WHERE search_index.node_id IN (SELECT id FROM _aoi_project_nodes);
        UPDATE knowledge_ai_suggestions AS suggestion
        SET workspace_id = personal_id
        WHERE suggestion.node_id IN (SELECT id FROM _aoi_project_nodes);
        -- Import jobs follow explicit project/subtree ownership only.  A job
        -- selected by project_id or a moved item must still belong to this
        -- source workspace (or the owner's Personal workspace).  Rebinding a
        -- foreign-workspace job would destroy provenance, so abort before the
        -- UPDATE and let the Alembic transaction roll back all changes.
        IF EXISTS (
            SELECT 1
            FROM knowledge_import_jobs AS import_job
            WHERE (
                import_job.project_id = project_row.id
                OR import_job.id IN (
                    SELECT item.job_id
                    FROM knowledge_import_items AS item
                    WHERE item.node_id IN (SELECT id FROM _aoi_project_nodes)
                )
            )
              AND import_job.workspace_id IS DISTINCT FROM source_workspace_id
              AND import_job.workspace_id IS DISTINCT FROM personal_id
        ) THEN
            INSERT INTO docs_workspace_migration_log (
                project_id, legacy_workspace_id, canonical_workspace_id,
                root_node_id, moved_count, status, metadata
            ) VALUES (
                project_row.id, source_workspace_id, personal_id,
                root_id, 0, 'conflict', jsonb_build_object(
                    'migration_revision', '20260809_0018',
                    'reason', 'foreign_import_job_scope',
                    'source_workspace_id', source_workspace_id,
                    'personal_workspace_id', personal_id,
                    'provenance', 'preserved_by_transaction_rollback'
                )
            );
            RAISE EXCEPTION
                '20260809_0018 foreign import job scope for project %',
                project_row.id;
        END IF;
        UPDATE knowledge_import_jobs AS import_job
        SET workspace_id = personal_id,
            project_id = project_row.id,
            updated_at = now()
        WHERE import_job.workspace_id IN (source_workspace_id, personal_id)
          AND (
               import_job.project_id = project_row.id
               OR import_job.id IN (
               SELECT item.job_id
               FROM knowledge_import_items AS item
               WHERE item.node_id IN (SELECT id FROM _aoi_project_nodes)
               )
          );

        -- Add the canonical schema marker after all link remapping.  The
        -- supertag remains metadata; project_id/ProjectMember ACL is identity.
        INSERT INTO knowledge_node_supertags (node_id, supertag_id, created_at, updated_at)
        VALUES (root_id, canonical_tag_id, now(), now())
        ON CONFLICT (node_id, supertag_id) DO UPDATE
        SET updated_at = now();

        -- Keep the project field value in sync when the canonical field exists
        -- (field names are intentionally matched, not hard-coded IDs).
        SELECT f.id
        INTO mapped_field_id
        FROM knowledge_fields AS f
        WHERE f.workspace_id = personal_id
          AND f.supertag_id = canonical_tag_id
          AND (f.name = 'Project' OR f.system_key = 'project')
        ORDER BY f.id
        LIMIT 1;
        IF mapped_field_id IS NOT NULL THEN
            INSERT INTO knowledge_field_values (node_id, field_id, value_json, value_text, updated_at)
            VALUES (root_id, mapped_field_id, to_json(project_row.id::text), project_row.id::text, now())
            ON CONFLICT (node_id, field_id) DO UPDATE
            SET value_json = coalesce(knowledge_field_values.value_json, EXCLUDED.value_json),
                value_text = coalesce(knowledge_field_values.value_text, EXCLUDED.value_text),
                updated_at = now();
        END IF;

        -- Archive other active project-information candidates but retain their
        -- IDs, body, descendants and audit metadata.  This prevents duplicate
        -- visible roots without data loss.
        FOR node_row IN
            SELECT candidate.id
            FROM knowledge_nodes AS candidate
            WHERE candidate.workspace_id = personal_id
              AND candidate.id <> root_id
              AND candidate.archived_at IS NULL
              AND candidate.project_id = project_row.id
              AND candidate.system_key = 'project_information:' || project_row.id::text
              AND candidate.parent_id = hub_id
        LOOP
            UPDATE knowledge_nodes
            SET archived_at = now(),
                updated_at = now()
            WHERE id = node_row.id;
        END LOOP;

        -- Empty project libraries are safe to remove only after all valuable
        -- definitions/metadata have been mapped and all nodes have moved.
        FOR workspace_row IN
            SELECT w.id
            FROM knowledge_workspaces AS w
            WHERE w.workspace_type = 'project'
              AND w.project_id = project_row.id
              AND w.id <> personal_id
            FOR UPDATE
        LOOP
            legacy_view_count := 0;
            IF to_regclass('public.knowledge_views') IS NOT NULL THEN
                EXECUTE 'SELECT count(*) FROM public.knowledge_views WHERE workspace_id = $1'
                INTO legacy_view_count
                USING workspace_row.id;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM knowledge_nodes WHERE workspace_id = workspace_row.id)
               AND NOT EXISTS (SELECT 1 FROM knowledge_supertags WHERE workspace_id = workspace_row.id)
               AND NOT EXISTS (SELECT 1 FROM knowledge_fields WHERE workspace_id = workspace_row.id)
               AND NOT EXISTS (SELECT 1 FROM knowledge_saved_views WHERE workspace_id = workspace_row.id)
               AND legacy_view_count = 0
               AND NOT EXISTS (SELECT 1 FROM knowledge_ai_suggestions WHERE workspace_id = workspace_row.id)
               AND NOT EXISTS (SELECT 1 FROM knowledge_import_jobs WHERE workspace_id = workspace_row.id)
            THEN
                DELETE FROM knowledge_workspaces WHERE id = workspace_row.id;
            END IF;
        END LOOP;

        metadata := jsonb_build_object(
            'migration_revision', '20260809_0018',
            'action', CASE
                WHEN created_root THEN 'created'
                WHEN adopted_root THEN 'adopted'
                WHEN source_workspace_id = personal_id THEN 'already_canonical'
                ELSE 'moved'
            END,
            'owner_id', project_row.owner_id,
            'personal_library_id', personal_id,
            'hub_node_id', hub_id,
            'root_node_id', root_id,
            'old_workspace_id', source_workspace_id,
            'moved_count', node_count,
            'cross_library_parent_links', cross_library_parent_count,
            'pointer_valid', pointer_valid,
            'pointer_reason', pointer_reason,
            'stale_pointer_id', stale_pointer_id,
            'candidate_count', candidate_count,
            'legacy_key_rename_count', legacy_key_rename_count,
            'project_info_supertag_id', canonical_tag_id
        );

        INSERT INTO docs_workspace_migration_log (
            project_id, legacy_workspace_id, canonical_workspace_id,
            root_node_id, moved_count, status, metadata
        ) VALUES (
            project_row.id,
            CASE WHEN EXISTS (
                SELECT 1 FROM knowledge_workspaces
                WHERE id = source_workspace_id
            ) THEN source_workspace_id ELSE NULL END,
            personal_id,
            root_id,
            node_count,
            CASE WHEN created_root THEN 'moved'
                 WHEN source_workspace_id = personal_id
                 THEN 'already_canonical' ELSE 'moved' END,
            metadata
        );
    END LOOP;
END
$$;
"""


def upgrade() -> None:
    # 0015 declared ``root_node_id`` nullable so missing/default-Inbox
    # pointers can be audited.  A pre-release copy of that migration was
    # applied with a NOT NULL column; repair the live schema before writing the
    # new skipped/deleted audit rows.  ``DROP NOT NULL`` is idempotent.
    op.execute(
        sa.text(
            "ALTER TABLE docs_workspace_migration_log "
            "ALTER COLUMN root_node_id DROP NOT NULL"
        )
    )
    op.execute(sa.text(_UPGRADE_SQL))


def downgrade() -> None:
    # Moving/merging existing rows is not bijective.  Refuse explicitly so an
    # Alembic downgrade cannot silently advance while data remains in the
    # unified shape.
    raise RuntimeError(
        "20260809_0018 is forward-only; restore a backup and run a reviewed "
        "inverse migration instead of downgrading"
    )
