"""Repair cross-library descendants captured by the historical 0015 walk.

The 0015 recursive query followed ``parent_id`` without constraining the
child's source workspace.  Its per-node audit snapshot records the original
workspace (``old_workspace_id`` in repaired audit rows; ``workspace_id`` in
the earliest rows), parent, root and project identity.  This forward-only
repair uses that snapshot to restore only nodes whose recorded workspace is
different from the migration's legacy source.  A node that has since been
edited, moved, or lost its source library is a hard conflict: the migration
records the conflict and raises instead of guessing or overwriting data.

Node UUIDs and all content/projection rows (body, revisions, attachments,
edges (``knowledge_edges``) and placements
(``knowledge_node_placements``) are preserved.  The final pass also normalizes existing
canonical project roots to the runtime contract: the hub owns
``root_page_id`` for the root and every descendant.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

import sqlalchemy as sa
from alembic import op


revision = "20260809_0022"
down_revision = "20260809_0021"
branch_labels = None
depends_on = None


def _fixture_cross_library_restore_plan(
    entries: Iterable[Mapping[str, Any]], legacy_workspace_id: str
) -> list[dict[str, Any]]:
    """Return unambiguous cross-library entries without mutating fixture rows.

    This tiny contract helper mirrors the SQL's identity rule and is used by
    migration tests for ambiguity and payload-preservation fixtures.
    """

    result: list[dict[str, Any]] = []
    seen: dict[Any, Any] = {}
    for entry in entries:
        node_id = entry.get("id")
        old_workspace_id = entry.get("old_workspace_id")
        if old_workspace_id is None:
            old_workspace_id = entry.get("workspace_id")
        if node_id is None or old_workspace_id is None:
            continue
        if str(old_workspace_id) == str(legacy_workspace_id):
            continue
        previous = seen.get(node_id)
        if previous is not None and str(previous) != str(old_workspace_id):
            raise ValueError(f"ambiguous cross-library node {node_id}")
        if previous is not None:
            raise ValueError(f"duplicate cross-library node {node_id}")
        seen[node_id] = old_workspace_id
        result.append(dict(entry))
    return result


_UPGRADE_SQL = r"""
DO $$
DECLARE
    repair_row RECORD;
    alias_row RECORD;
    node_row RECORD;
    hub_row RECORD;
    project_row RECORD;
    owner_library_id uuid;
    hub_id uuid;
    hub_count integer;
    malformed_hub_count integer;
    pointer_id uuid;
    repaired_count integer;
    restore_parent_id uuid;
    restore_root_page_id uuid;
    restore_project_id uuid;
    relation_quarantined boolean;
BEGIN
    -- Do not put a primary key on this staging table: duplicate/ambiguous
    -- audit entries must be detected and rejected explicitly below.
    CREATE TEMP TABLE IF NOT EXISTS _aoi_0022_cross_library_restore (
        log_id uuid NOT NULL,
        project_id uuid NOT NULL,
        node_id uuid NOT NULL,
        legacy_workspace_id uuid,
        canonical_workspace_id uuid,
        root_node_id uuid,
        old_workspace_id uuid NOT NULL,
        old_parent_id uuid,
        old_root_page_id uuid,
        old_project_id uuid,
        restore_library_id uuid,
        canonical_library_id uuid,
        source_project_id uuid,
        source_root_id uuid,
        source_hub_id uuid,
        source_retired boolean NOT NULL DEFAULT false
    ) ON COMMIT DROP;
    CREATE TEMP TABLE IF NOT EXISTS _aoi_0022_library_alias (
        old_library_id uuid NOT NULL,
        current_library_id uuid NOT NULL,
        source_project_id uuid,
        source_root_id uuid,
        source_hub_id uuid,
        source_retired boolean NOT NULL DEFAULT false
    ) ON COMMIT DROP;

    -- 0019/0020/0021 preserve UUIDs in audit rows while moving a retired
    -- project library into its owner's Personal library.  Resolve those old
    -- workspace IDs before checking a cross-library node; an exact UUID is
    -- preferred, then the deterministic Personal target from the audit.
    INSERT INTO _aoi_0022_library_alias (
        old_library_id, current_library_id, source_project_id,
        source_root_id, source_hub_id, source_retired
    )
    SELECT id, id, NULL::uuid, NULL::uuid, NULL::uuid, false FROM docs_libraries;
    INSERT INTO _aoi_0022_library_alias (
        old_library_id, current_library_id, source_project_id,
        source_root_id, source_hub_id, source_retired
    )
    SELECT COALESCE(
               logged.legacy_workspace_id,
               NULLIF(logged.metadata->>'legacy_library_id', '')::uuid,
               NULLIF(logged.metadata->>'old_workspace_id', '')::uuid
           ),
           COALESCE(
               NULLIF(logged.metadata->>'personal_library_id', '')::uuid,
               logged.canonical_workspace_id
           ),
           COALESCE(
               NULLIF(logged.metadata->>'project_id', '')::uuid,
               logged.project_id
           ),
           logged.root_node_id,
           NULL::uuid,
           (
               logged.metadata ? 'legacy_library_id'
               OR coalesce(
                   logged.metadata->>'migration_revision' IN ('20260809_0020', '20260809_0021'),
                   false
               )
           )
    FROM docs_workspace_migration_log AS logged
    WHERE COALESCE(
              logged.legacy_workspace_id,
              NULLIF(logged.metadata->>'legacy_library_id', '')::uuid,
              NULLIF(logged.metadata->>'old_workspace_id', '')::uuid
          ) IS NOT NULL
      AND (
          logged.metadata ? 'personal_library_id'
          OR EXISTS (
              SELECT 1 FROM docs_libraries AS target
              WHERE target.id = logged.canonical_workspace_id
          )
      )
      AND COALESCE(
          NULLIF(logged.metadata->>'personal_library_id', '')::uuid,
          logged.canonical_workspace_id
      ) IS NOT NULL
      AND EXISTS (
          SELECT 1
          FROM docs_libraries AS target
          WHERE target.id = COALESCE(
              NULLIF(logged.metadata->>'personal_library_id', '')::uuid,
              logged.canonical_workspace_id
          )
      );
    UPDATE _aoi_0022_library_alias AS alias
    SET source_hub_id = hub.id
    FROM knowledge_nodes AS hub
    WHERE alias.source_project_id IS NOT NULL
      AND alias.source_retired
      AND hub.docs_library_id = alias.current_library_id
      AND hub.project_id IS NULL
      AND hub.system_key = 'project_information_root'
      AND hub.parent_id IS NULL
      AND hub.archived_at IS NULL;
    IF EXISTS (
        SELECT 1
        FROM _aoi_0022_library_alias
        GROUP BY old_library_id
        HAVING count(DISTINCT current_library_id) > 1
    ) THEN
        RAISE EXCEPTION
            '20260809_0022 conflicting retired-library aliases';
    END IF;

    INSERT INTO _aoi_0022_cross_library_restore (
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

    UPDATE _aoi_0022_cross_library_restore AS repair
    SET restore_library_id = alias.current_library_id
    FROM _aoi_0022_library_alias AS alias
    WHERE alias.old_library_id = repair.old_workspace_id;
    UPDATE _aoi_0022_cross_library_restore AS repair
    SET canonical_library_id = alias.current_library_id
    FROM _aoi_0022_library_alias AS alias
    WHERE alias.old_library_id = repair.canonical_workspace_id;
    UPDATE _aoi_0022_cross_library_restore AS repair
    SET source_project_id = alias.source_project_id,
        source_root_id = alias.source_root_id,
        source_hub_id = alias.source_hub_id
    FROM _aoi_0022_library_alias AS alias
    WHERE alias.old_library_id = repair.old_workspace_id
      AND alias.source_project_id IS NOT NULL
      AND alias.source_retired;

    IF EXISTS (
        SELECT 1
        FROM _aoi_0022_cross_library_restore
        GROUP BY node_id
        HAVING count(*) > 1
            OR count(DISTINCT old_workspace_id) > 1
    ) THEN
        INSERT INTO docs_workspace_migration_log (
            project_id, moved_count, status, metadata
        )
        SELECT project_id,
               0, 'conflict',
               jsonb_build_object(
                   'migration_revision', '20260809_0022',
                   'reason', 'ambiguous_cross_library_audit'
               )
        FROM _aoi_0022_cross_library_restore
        GROUP BY project_id;
        RAISE EXCEPTION
            '20260809_0022 ambiguous cross-library audit entries';
    END IF;

    FOR repair_row IN
        SELECT *
        FROM _aoi_0022_cross_library_restore
        ORDER BY node_id, log_id
    LOOP
        IF repair_row.canonical_workspace_id IS NULL THEN
            RAISE EXCEPTION
                '20260809_0022 missing legacy/canonical source for node %',
                repair_row.node_id;
        END IF;
        IF repair_row.restore_library_id IS NULL
           OR repair_row.canonical_library_id IS NULL THEN
            INSERT INTO docs_workspace_migration_log (
                project_id, moved_count, status, metadata
            ) VALUES (
                repair_row.project_id, 0, 'conflict', jsonb_build_object(
                    'migration_revision', '20260809_0022',
                    'node_id', repair_row.node_id,
                    'reason', 'missing_library_alias'
                )
            );
            RAISE EXCEPTION
                '20260809_0022 source library % has no deterministic current alias for node %',
                repair_row.old_workspace_id, repair_row.node_id;
        END IF;

        SELECT n.*
        INTO node_row
        FROM knowledge_nodes AS n
        WHERE n.id = repair_row.node_id
        FOR UPDATE;
        IF NOT FOUND THEN
            INSERT INTO docs_workspace_migration_log (
                project_id, moved_count, status, metadata
            ) VALUES (
                repair_row.project_id, 0, 'conflict', jsonb_build_object(
                    'migration_revision', '20260809_0022',
                    'node_id', repair_row.node_id,
                    'reason', 'missing_audited_node'
                )
            );
            RAISE EXCEPTION
                '20260809_0022 audited node % is missing',
                repair_row.node_id;
        END IF;

        -- An already-restored row is valid only when every captured identity
        -- value agrees.  Do not overwrite a later legitimate edit.
        IF node_row.docs_library_id = repair_row.restore_library_id
           AND node_row.parent_id IS NOT DISTINCT FROM repair_row.old_parent_id
           AND node_row.root_page_id IS NOT DISTINCT FROM repair_row.old_root_page_id
           AND node_row.project_id IS NOT DISTINCT FROM repair_row.old_project_id THEN
            CONTINUE;
        END IF;
        -- A completed 0018/0020/0021 move has a deterministic shape: the
        -- current library is the mapped Personal target, parent is either the
        -- captured parent, the canonical project root, or NULL when that
        -- parent was outside the source subtree, and root_page_id is the old
        -- root or the owner's hub.  Accept exactly those shapes; any other
        -- value is a later edit and is rejected.
        hub_row := NULL;
        SELECT h.id
        INTO hub_row
        FROM knowledge_nodes AS h
        WHERE h.docs_library_id = repair_row.canonical_library_id
          AND h.project_id IS NULL
          AND h.system_key = 'project_information_root'
          AND h.parent_id IS NULL
          AND h.archived_at IS NULL
        ORDER BY h.id
        LIMIT 1;
        alias_row := NULL;
        SELECT a.*
        INTO alias_row
        FROM _aoi_0022_library_alias AS a
        WHERE a.old_library_id = repair_row.old_workspace_id
          AND a.source_project_id IS NOT NULL
          AND a.source_retired
        ORDER BY a.current_library_id, a.source_project_id
        LIMIT 1;
        IF node_row.docs_library_id <> repair_row.canonical_library_id
           AND node_row.docs_library_id <> repair_row.restore_library_id
           OR (
               node_row.parent_id IS DISTINCT FROM repair_row.old_parent_id
               AND node_row.parent_id IS DISTINCT FROM repair_row.root_node_id
               AND (alias_row.source_root_id IS NULL
                    OR node_row.parent_id IS DISTINCT FROM alias_row.source_root_id)
               AND node_row.parent_id IS NOT NULL
           )
           OR (
               node_row.root_page_id IS DISTINCT FROM repair_row.root_node_id
               AND (hub_row.id IS NULL OR node_row.root_page_id IS DISTINCT FROM hub_row.id)
               AND (alias_row.source_root_id IS NULL
                    OR node_row.root_page_id IS DISTINCT FROM alias_row.source_root_id)
               AND (alias_row.source_hub_id IS NULL
                    OR node_row.root_page_id IS DISTINCT FROM alias_row.source_hub_id)
           )
           OR (
               node_row.project_id IS DISTINCT FROM repair_row.project_id
               AND (alias_row.source_project_id IS NULL
                    OR node_row.project_id IS DISTINCT FROM alias_row.source_project_id)
           ) THEN
            INSERT INTO docs_workspace_migration_log (
                project_id, moved_count, status, metadata
            ) VALUES (
                repair_row.project_id, 0, 'conflict', jsonb_build_object(
                    'migration_revision', '20260809_0022',
                    'node_id', repair_row.node_id,
                    'reason', 'later_edit_shape_conflict'
                )
            );
            RAISE EXCEPTION
                '20260809_0022 later edit conflicts with cross-library node %',
                repair_row.node_id;
        END IF;
        -- Restore parent/root only when both endpoints are in the target
        -- library.  A historical endpoint in A while the node returns to B
        -- would recreate a cross-library edge; quarantine as a standalone
        -- node instead and retain the captured IDs in the audit metadata.
        relation_quarantined := false;
        restore_project_id := repair_row.old_project_id;
        IF restore_project_id IS NOT NULL
           AND (
               alias_row.source_project_id IS NULL
               OR alias_row.source_project_id IS DISTINCT FROM restore_project_id
               OR NOT EXISTS (
                   SELECT 1
                   FROM docs_libraries AS restore_library
                   JOIN projects AS restored_project
                     ON restored_project.id = restore_project_id
                    AND restored_project.owner_id = restore_library.owner_user_id
                   WHERE restore_library.id = repair_row.restore_library_id
               )
           ) THEN
            restore_project_id := NULL;
            relation_quarantined := true;
        END IF;
        restore_parent_id := repair_row.old_parent_id;
        restore_root_page_id := repair_row.old_root_page_id;
        IF (
            repair_row.old_parent_id IS NOT NULL
            AND NOT EXISTS (
                SELECT 1 FROM knowledge_nodes AS parent
                WHERE parent.id = repair_row.old_parent_id
                  AND parent.docs_library_id = repair_row.restore_library_id
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
            repair_row.old_root_page_id IS NOT NULL
            AND NOT EXISTS (
                SELECT 1 FROM knowledge_nodes AS root
                WHERE root.id = repair_row.old_root_page_id
                  AND root.docs_library_id = repair_row.restore_library_id
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
            restore_root_page_id := node_row.id;
        END IF;

        UPDATE knowledge_nodes
        SET docs_library_id = repair_row.restore_library_id,
            parent_id = restore_parent_id,
            root_page_id = restore_root_page_id,
            project_id = restore_project_id,
            updated_at = now()
        WHERE id = repair_row.node_id;
        UPDATE knowledge_search_index
        SET docs_library_id = repair_row.restore_library_id,
            -- Mirror the node's validated project relation; do not leave a
            -- stale cross-project value on a quarantined index row.
            project_id = restore_project_id,
            updated_at = now()
        WHERE node_id = repair_row.node_id;
        UPDATE knowledge_ai_suggestions
        SET docs_library_id = repair_row.restore_library_id
        WHERE node_id = repair_row.node_id;
        UPDATE docs_workspace_migration_log AS audit
        SET metadata = audit.metadata || jsonb_build_object(
            'cross_library_repairs',
            coalesce(audit.metadata->'cross_library_repairs', '[]'::jsonb)
                || jsonb_build_array(jsonb_build_object(
                    'node_id', repair_row.node_id,
                    'old_workspace_id', repair_row.old_workspace_id,
                    'old_parent_id', repair_row.old_parent_id,
                    'old_root_page_id', repair_row.old_root_page_id,
                    'restored_parent_id', restore_parent_id,
                    'restored_root_page_id', restore_root_page_id,
                    'restored_project_id', restore_project_id,
                    'quarantined', relation_quarantined,
                    'preserved', jsonb_build_array(
                        'node_uuid', 'body_json', 'body_text', 'revisions',
                        'attachments', 'edges', 'placements'
                    )
                ))
        )
        WHERE audit.id = repair_row.log_id;
    END LOOP;

    IF EXISTS (
        SELECT 1
        FROM _aoi_0022_cross_library_restore AS repaired
        JOIN knowledge_nodes AS child ON child.id = repaired.node_id
        LEFT JOIN knowledge_nodes AS parent ON parent.id = child.parent_id
        LEFT JOIN knowledge_nodes AS root ON root.id = child.root_page_id
        WHERE (parent.id IS NOT NULL AND parent.docs_library_id <> child.docs_library_id)
           OR (root.id IS NOT NULL AND root.docs_library_id <> child.docs_library_id)
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
            '20260809_0022 cross-library repair left a parent/root edge across libraries';
    END IF;

    -- Runtime resolvers use the owner's hub as root_page_id.  Normalize old
    -- 0015/0018 roots (which used self-root IDs) without touching unrelated
    -- ordinary Home nodes.  Every project must have one strict pointer and a
    -- personal hub; ambiguity is a hard failure rather than a guess.
    FOR project_row IN
        SELECT p.id, p.owner_id, p.knowledge_node_id,
               p.name, p.slug, p.project_metadata, p.aliases
        FROM projects AS p
        WHERE p.deleted_at IS NULL
        ORDER BY p.id
        FOR UPDATE
    LOOP
        SELECT l.id
        INTO owner_library_id
        FROM docs_libraries AS l
        WHERE l.owner_user_id = project_row.owner_id
          AND l.library_type = 'personal'
        ORDER BY l.created_at NULLS LAST, l.id
        LIMIT 1;
        IF owner_library_id IS NULL THEN
            RAISE EXCEPTION
                '20260809_0022 active project % has no owner Personal library',
                project_row.id;
        END IF;
        -- A Personal library has exactly one active canonical hub.  Count
        -- before selecting so ORDER BY cannot silently pick one of several
        -- competing identities.  A row carrying the reserved key but a
        -- foreign project/parent is malformed rather than an adoption
        -- candidate and therefore aborts this transactional repair.
        SELECT count(*)
        INTO hub_count
        FROM knowledge_nodes AS n
        WHERE n.docs_library_id = owner_library_id
          AND n.system_key = 'project_information_root'
          AND n.archived_at IS NULL
          AND n.project_id IS NULL
          AND n.parent_id IS NULL;
        IF hub_count <> 1 THEN
            RAISE EXCEPTION
                '20260809_0022 Personal library % has % active canonical hubs for project %',
                owner_library_id, hub_count, project_row.id;
        END IF;
        SELECT count(*)
        INTO malformed_hub_count
        FROM knowledge_nodes AS n
        WHERE n.docs_library_id = owner_library_id
          AND n.system_key = 'project_information_root'
          AND n.archived_at IS NULL
          AND (n.project_id IS NOT NULL OR n.parent_id IS NOT NULL);
        IF malformed_hub_count <> 0 THEN
            RAISE EXCEPTION
                '20260809_0022 Personal library % has % malformed active hubs for project %',
                owner_library_id, malformed_hub_count, project_row.id;
        END IF;
        SELECT n.id
        INTO hub_id
        FROM knowledge_nodes AS n
        WHERE n.docs_library_id = owner_library_id
          AND n.project_id IS NULL
          AND n.system_key = 'project_information_root'
          AND n.parent_id IS NULL
          AND n.archived_at IS NULL
        ORDER BY n.id
        LIMIT 1;
        IF hub_id IS NULL THEN
            RAISE EXCEPTION
                '20260809_0022 owner Personal library for project % has no canonical hub',
                project_row.id;
        END IF;
        -- Hub root identity is self, even when 0015/0018 left a stale,
        -- NULL, or foreign root_page_id.  This update is deterministic and
        -- precedes pointer/descendant validation below.
        UPDATE knowledge_nodes
        SET parent_id = NULL,
            project_id = NULL,
            root_page_id = hub_id,
            updated_at = now()
        WHERE id = hub_id;
        pointer_id := project_row.knowledge_node_id;
        IF pointer_id IS NULL THEN
            -- The built-in Inbox project is intentionally pointerless on
            -- some legacy installations.  Only the explicit metadata marker
            -- or owner-bound canonical slug qualifies; a name/alias match is
            -- not enough because ordinary projects may be called Inbox.
            IF coalesce(project_row.project_metadata::jsonb, '{}'::jsonb)->>'isInboxDefault' = 'true'
               OR project_row.slug = 'inbox-project-' || project_row.owner_id::text THEN
                CONTINUE;
            END IF;
            RAISE EXCEPTION
                '20260809_0022 active project % has a NULL Docs pointer',
                project_row.id;
        END IF;
        SELECT n.*
        INTO node_row
        FROM knowledge_nodes AS n
        WHERE n.id = pointer_id
          AND n.docs_library_id = owner_library_id
          AND n.project_id = project_row.id
          AND n.system_key = 'project_information:' || project_row.id::text
          AND n.parent_id = hub_id
          AND n.archived_at IS NULL
          AND EXISTS (
              SELECT 1
              FROM knowledge_node_supertags AS nt
              JOIN knowledge_supertags AS st ON st.id = nt.supertag_id
              WHERE nt.node_id = n.id
                AND st.docs_library_id = owner_library_id
                AND st.system_key = 'project_info'
          )
        FOR UPDATE;
        IF NOT FOUND THEN
            RAISE EXCEPTION
                '20260809_0022 canonical project pointer % is invalid for project %',
                pointer_id, project_row.id;
        END IF;
        IF node_row.root_page_id IS DISTINCT FROM hub_id THEN
            UPDATE knowledge_nodes
            SET root_page_id = hub_id, updated_at = now()
            WHERE id = pointer_id;
        END IF;
        UPDATE knowledge_nodes AS descendant
        SET root_page_id = hub_id, updated_at = now()
        WHERE descendant.docs_library_id = owner_library_id
          AND descendant.project_id = project_row.id
          AND descendant.id <> pointer_id;
    END LOOP;

    -- A residual project library is never silently ignored by this repair.
    IF EXISTS (SELECT 1 FROM docs_libraries WHERE library_type = 'project') THEN
        RAISE EXCEPTION
            '20260809_0022 project libraries remain; resolve with 0021 first';
    END IF;
END
$$;
"""


def upgrade() -> None:
    op.execute(sa.text(_UPGRADE_SQL))


def downgrade() -> None:
    raise RuntimeError(
        "20260809_0022 is forward-only; restore a backup and run a reviewed "
        "inverse migration instead of downgrading"
    )
