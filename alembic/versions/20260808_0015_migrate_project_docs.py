"""Move legacy project Docs subtrees into canonical project workspaces.

``projects.knowledge_node_id`` predates the project-owned Docs workspace from
revision ``20260808_0013``.  The pointer is the stable identity of a project's
information page, therefore this migration moves that node (and every child)
without changing any node IDs or encrypted body columns.  References, edges,
attachments, revisions and placements continue to point at the same IDs.

The migration is intentionally conservative when a canonical workspace already
contains a different node with the same ``system_key``.  An active conflict is
logged and aborts the transaction rather than silently replacing document body
data.  An already-archived conflicting row has no visible content; its key is
cleared, the row remains archived, and the legacy node can be moved safely.
Each successful move records the old workspace/parent/project state in
``docs_workspace_migration_log`` so the downgrade can restore it.
Projects whose legacy pointer no longer resolves are recorded as
``missing_root`` with the old UUID (or an explicit NULL marker) in JSON
metadata and do not block unrelated projects.
"""

from __future__ import annotations

from typing import Iterable, Mapping, Sequence, TypeVar

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260808_0015"
down_revision: str = "20260808_0014"
branch_labels: Sequence[str] | None = None
depends_on: str | None = None


# This small pure-Python helper mirrors the recursive CTE used by the online
# migration.  It is useful to keep fixture tests independent from a production
# database while making the subtree boundary explicit and cycle-safe.
_T = TypeVar("_T")


def _fixture_subtree_ids(
    rows: Iterable[Mapping[str, _T]],
    root_id: _T,
    *,
    id_key: str = "id",
    parent_key: str = "parent_id",
) -> list[_T]:
    """Return a deterministic, cycle-safe depth-first subtree fixture.

    ``knowledge_nodes.id`` is globally unique, so a node is included once even
    if malformed legacy data contains a parent cycle.  Production uses
    ``UNION`` (rather than ``UNION ALL``) for the same invariant.
    """

    children: dict[_T | None, list[_T]] = {}
    for row in rows:
        node_id = row[id_key]
        parent_id = row.get(parent_key)
        children.setdefault(parent_id, []).append(node_id)

    for siblings in children.values():
        siblings.sort(key=str)

    seen: set[_T] = set()
    result: list[_T] = []
    stack = [root_id]
    while stack:
        node_id = stack.pop()
        if node_id in seen:
            continue
        seen.add(node_id)
        result.append(node_id)
        stack.extend(reversed(children.get(node_id, [])))
    return result


_LEGACY_PROJECT_DOC_SYSTEM_KEYS = {
    # ``project_information:<uuid>`` is the key used by the canonical page
    # writer.  ``project_info:<uuid>`` is retained solely for rows created by
    # the pre-canonical implementation.  Deliberately do not use
    # ``project_id = <uuid>`` as an archive signal: ordinary Personal Docs,
    # Agent Memory indexes, and imported notes may carry that association too.
    "project_information:{project_id}",
    "project_info:{project_id}",
}


def _fixture_legacy_candidate_ids(
    rows: Iterable[Mapping[str, _T]],
    project_id: _T,
    *,
    pointer_ids: Iterable[_T] = (),
    id_key: str = "id",
    system_key: str = "system_key",
) -> list[_T]:
    """Return only strongly identified legacy project-info roots.

    This fixture helper intentionally mirrors the SQL candidate predicate and
    excludes the authoritative pointer subtree.  It is used by tests and by
    reviewers as a dry-run contract without touching a database.
    """

    pointer_set = set(pointer_ids)
    allowed = {
        template.format(project_id=project_id)
        for template in _LEGACY_PROJECT_DOC_SYSTEM_KEYS
    }
    return sorted(
        {
            row[id_key]
            for row in rows
            if row[id_key] not in pointer_set and row.get(system_key) in allowed
        },
        key=str,
    )


def _fixture_archive_root_ids(
    rows: Iterable[Mapping[str, _T]],
    project_id: _T,
    *,
    pointer_ids: Iterable[_T] = (),
    id_key: str = "id",
    parent_key: str = "parent_id",
    archived_key: str = "archived_at",
) -> list[_T]:
    """Mirror the online top-level active-candidate selection.

    An archived candidate parent does not hide an active nested candidate;
    that child becomes an archive root.  Active descendants of an active root
    remain covered by the root's recursive archive CTE.
    """

    pointer_set = set(pointer_ids)
    allowed = {
        template.format(project_id=project_id)
        for template in _LEGACY_PROJECT_DOC_SYSTEM_KEYS
    }
    candidates = {
        row[id_key]: row
        for row in rows
        if row[id_key] not in pointer_set and row.get("system_key") in allowed
    }
    return sorted(
        {
            node_id
            for node_id, row in candidates.items()
            if row.get(archived_key) is None
            and not any(
                parent_id is not None
                and parent_id == row.get(parent_key)
                and parent.get(archived_key) is None
                for parent_id, parent in candidates.items()
            )
        },
        key=str,
    )


# Read-only SQL used by the audit script and by operators reviewing a planned
# migration.  Keep this query in the migration module so the audit and online
# code share the same narrow predicate instead of drifting independently.
_PROJECT_DOCS_AUDIT_SQL = r"""
WITH RECURSIVE pointer_subtrees AS (
    SELECT p.id AS project_id,
           n.id,
           n.workspace_id AS source_workspace_id,
           0::integer AS depth,
           ARRAY[n.id]::uuid[] AS path
      FROM projects AS p
      JOIN knowledge_nodes AS n ON n.id = p.knowledge_node_id
     WHERE p.knowledge_node_id IS NOT NULL
       AND n.workspace_id IS NOT NULL
    UNION
    SELECT s.project_id,
           child.id,
           child.workspace_id,
           s.depth + 1,
           s.path || child.id
      FROM pointer_subtrees AS s
      JOIN knowledge_nodes AS child
        ON child.parent_id = s.id
       AND child.workspace_id = s.source_workspace_id
     WHERE s.depth < 512
       AND NOT child.id = ANY(s.path)
), candidate_roots AS (
    SELECT p.id AS project_id,
           n.id AS node_id,
           n.title,
           n.workspace_id,
           n.parent_id,
           n.root_page_id,
           n.project_id AS node_project_id,
           n.system_key,
           n.archived_at
      FROM projects AS p
      JOIN knowledge_nodes AS n
        ON n.system_key IN (
            'project_information:' || p.id::text,
            'project_info:' || p.id::text
        )
      JOIN knowledge_workspaces AS personal
        ON personal.id = n.workspace_id
       AND personal.workspace_type = 'personal'
     WHERE p.knowledge_node_id IS NOT NULL
       AND NOT EXISTS (
         SELECT 1
           FROM pointer_subtrees AS pointer
          WHERE pointer.project_id = p.id
            AND pointer.id = n.id
     )
), archive_roots AS (
    -- This is intentionally the same boundary as the online archive loop:
    -- an active candidate whose parent is archived (or not a candidate) is a
    -- root, while an active child of another active candidate is covered by
    -- that parent's recursive subtree.
    SELECT candidate.*
      FROM candidate_roots AS candidate
     WHERE candidate.archived_at IS NULL
       AND NOT EXISTS (
           SELECT 1
             FROM candidate_roots AS parent_candidate
            WHERE parent_candidate.node_id = candidate.parent_id
              AND parent_candidate.archived_at IS NULL
       )
), archive_subtrees AS (
    SELECT root.project_id AS project_id,
           root.node_id AS archive_root_id,
           node.id AS node_id,
           node.title,
           node.workspace_id,
           node.parent_id,
           node.root_page_id,
           node.project_id AS node_project_id,
           node.system_key,
           node.archived_at,
           0::integer AS depth,
           ARRAY[node.id]::uuid[] AS path
      FROM archive_roots AS root
      JOIN knowledge_nodes AS node
        ON node.id = root.node_id
       AND node.workspace_id = root.workspace_id
    UNION
    SELECT subtree.project_id,
           subtree.archive_root_id,
           child.id,
           child.title,
           child.workspace_id,
           child.parent_id,
           child.root_page_id,
           child.project_id,
           child.system_key,
           child.archived_at,
           subtree.depth + 1,
           subtree.path || child.id
      FROM archive_subtrees AS subtree
      JOIN knowledge_nodes AS child
        ON child.parent_id = subtree.node_id
       AND child.workspace_id = subtree.workspace_id
     WHERE subtree.depth < 512
       AND NOT child.id = ANY(subtree.path)
)
SELECT project_id,
       archive_root_id,
       node_id,
       title,
       workspace_id,
       parent_id,
       root_page_id,
       node_project_id,
       system_key,
       archived_at,
       CASE WHEN archived_at IS NULL THEN 'would_archive' ELSE 'already_archived' END AS action
  FROM archive_subtrees
 ORDER BY project_id, archive_root_id, node_id;
"""


_UPGRADE_SQL = r"""
DO $$
DECLARE
    project_row RECORD;
    root_row RECORD;
    conflict_row RECORD;
    canonical_id uuid;
    legacy_workspace_id uuid;
    node_count integer;
    pointer_node_count integer;
    created_workspace boolean;
    duplicate_workspace_id uuid;
    archived_conflicts jsonb := '[]'::jsonb;
    archive_metadata jsonb;
    state_metadata jsonb;
    deep_node_id uuid;
BEGIN
    -- A temporary table keeps the old values needed by downgrade and avoids
    -- changing parent/root IDs while the rows are being moved.
    CREATE TEMP TABLE IF NOT EXISTS _aoi_project_docs_nodes (
        id uuid PRIMARY KEY,
        old_workspace_id uuid NOT NULL,
        old_parent_id uuid,
        old_root_page_id uuid,
        old_project_id uuid,
        system_key text
    ) ON COMMIT DROP;
    CREATE TEMP TABLE IF NOT EXISTS _aoi_project_docs_candidates (
        id uuid PRIMARY KEY,
        old_workspace_id uuid NOT NULL,
        old_parent_id uuid,
        old_root_page_id uuid,
        old_project_id uuid,
        system_key text,
        old_archived_at timestamptz
    ) ON COMMIT DROP;
    CREATE TEMP TABLE IF NOT EXISTS _aoi_project_docs_archive_nodes (
        id uuid PRIMARY KEY,
        old_archived_at timestamptz,
        old_workspace_id uuid,
        old_parent_id uuid,
        old_project_id uuid,
        title text,
        system_key text
    ) ON COMMIT DROP;

    FOR project_row IN
        SELECT id, name, description, knowledge_node_id
        FROM projects
        ORDER BY id
        FOR UPDATE
    LOOP
        TRUNCATE _aoi_project_docs_nodes;
        archived_conflicts := '[]'::jsonb;

        SELECT n.id, n.workspace_id, n.parent_id, n.root_page_id,
               n.project_id, n.system_key
        INTO root_row
        FROM knowledge_nodes AS n
        WHERE n.id = project_row.knowledge_node_id
        FOR UPDATE;

        -- The project FK normally makes this impossible.  Keep an explicit
        -- log/exception for installations that repaired the FK manually.
        IF NOT FOUND THEN
            INSERT INTO docs_workspace_migration_log (
                project_id, root_node_id, status, metadata
            ) VALUES (
                project_row.id,
                NULL,
                'missing_root',
                jsonb_build_object(
                    'reason', CASE
                        WHEN project_row.knowledge_node_id IS NULL
                            THEN 'project knowledge_node_id is NULL'
                        ELSE 'project knowledge_node_id does not resolve'
                    END,
                    'project_id', project_row.id,
                    'project_name', project_row.name,
                    'legacy_root_node_id', project_row.knowledge_node_id,
                    'recovery', 'restore the legacy root or explicitly reconcile this project before retrying'
                )
            );
            -- A repaired/legacy database may have lost the pointed row even
            -- when the FK was temporarily disabled.  Preserve the audit row
            -- and continue with other projects; no workspace can be created
            -- safely without a root.
            CONTINUE;
        END IF;

        legacy_workspace_id := root_row.workspace_id;

        SELECT w.id
        INTO canonical_id
        FROM knowledge_workspaces AS w
        WHERE w.workspace_type = 'project'
          AND w.project_id = project_row.id
        ORDER BY w.created_at NULLS LAST, w.id
        LIMIT 1
        FOR UPDATE;

        created_workspace := false;
        IF canonical_id IS NULL THEN
            canonical_id := gen_random_uuid();
            created_workspace := true;
            INSERT INTO knowledge_workspaces (
                id,
                name,
                description,
                owner_user_id,
                workspace_type,
                project_id,
                settings_json,
                created_at,
                updated_at
            ) VALUES (
                canonical_id,
                left(coalesce(project_row.name, 'Project') || ' Docs', 200),
                project_row.description,
                NULL,
                'project',
                project_row.id,
                json_build_object(
                    'workspace_type', 'project',
                    'project_id', project_row.id::text,
                    'migration_revision', '20260808_0015'
                ),
                now(),
                now()
            );
        ELSE
            -- Canonical project workspaces are ownerless; authorization is
            -- derived from ProjectMember rather than an individual account.
            UPDATE knowledge_workspaces
            SET owner_user_id = NULL,
                workspace_type = 'project',
                updated_at = now()
            WHERE id = canonical_id;
        END IF;

        -- Do not silently truncate a deep tree at the safety bound.  Detect a
        -- 513th level with the same source-workspace/path rules and abort the
        -- whole transaction before any node from this project is moved.
        deep_node_id := NULL;
        WITH RECURSIVE deep_subtree AS (
            SELECT n.id,
                   n.workspace_id,
                   0::integer AS depth,
                   ARRAY[n.id]::uuid[] AS path
            FROM knowledge_nodes AS n
            WHERE n.id = project_row.knowledge_node_id
              AND n.workspace_id = legacy_workspace_id
            UNION
            SELECT child.id,
                   child.workspace_id,
                   parent.depth + 1,
                   parent.path || child.id
            FROM knowledge_nodes AS child
            JOIN deep_subtree AS parent
              ON parent.id = child.parent_id
             AND child.workspace_id = parent.workspace_id
            WHERE parent.depth < 513
              AND NOT child.id = ANY(parent.path)
        )
        SELECT id
        INTO deep_node_id
        FROM deep_subtree
        WHERE depth > 512
        LIMIT 1;
        IF deep_node_id IS NOT NULL THEN
            RAISE EXCEPTION
                '20260808_0015 project % subtree exceeds depth 512 at node %',
                project_row.id, deep_node_id;
        END IF;

        WITH RECURSIVE subtree AS (
            SELECT n.id,
                   n.workspace_id,
                   n.parent_id,
                   n.root_page_id,
                   n.project_id,
                   n.system_key,
                   0::integer AS depth,
                   ARRAY[n.id]::uuid[] AS path
            FROM knowledge_nodes AS n
            WHERE n.id = project_row.knowledge_node_id
              AND n.workspace_id = legacy_workspace_id
            UNION
            SELECT child.id,
                   child.workspace_id,
                   child.parent_id,
                   child.root_page_id,
                   child.project_id,
                   child.system_key,
                   parent.depth + 1,
                   parent.path || child.id
            FROM knowledge_nodes AS child
            JOIN subtree AS parent ON parent.id = child.parent_id
                                  AND child.workspace_id = parent.workspace_id
            WHERE parent.depth < 512
              AND NOT child.id = ANY(parent.path)
        )
        INSERT INTO _aoi_project_docs_nodes (
            id, old_workspace_id, old_parent_id, old_root_page_id,
            old_project_id, system_key
        )
        SELECT id, workspace_id, parent_id, root_page_id, project_id, system_key
        FROM subtree;

        SELECT count(*) INTO pointer_node_count FROM _aoi_project_docs_nodes;
        node_count := pointer_node_count;

        -- IDs are global primary keys.  A row with the same ID in the target
        -- workspace can only be the already-moved source row; any other shape
        -- indicates corruption and must not be overwritten.
        SELECT n.id
        INTO conflict_row
        FROM knowledge_nodes AS n
        JOIN _aoi_project_docs_nodes AS legacy ON legacy.id = n.id
        WHERE n.workspace_id = canonical_id
          AND legacy.old_workspace_id <> canonical_id
        LIMIT 1;
        IF FOUND THEN
            INSERT INTO docs_workspace_migration_log (
                project_id, legacy_workspace_id, canonical_workspace_id,
                root_node_id, moved_count, status, metadata
            ) VALUES (
                project_row.id, legacy_workspace_id, canonical_id,
                project_row.knowledge_node_id, 0, 'conflict',
                jsonb_build_object(
                    'conflict_type', 'node_id',
                    'node_id', conflict_row.id
                )
            );
            -- The INSERT above is useful when an operator runs this block in
            -- a diagnostic transaction, but Alembic's transaction rollback
            -- removes it on failure.  Therefore the exception itself carries
            -- every value needed to recover/reconcile the row.
            RAISE EXCEPTION '%',
                format(
                    'project %s has a node ID collision while moving Docs subtree (%s)',
                    project_row.id, conflict_row.id
                )
                USING DETAIL = jsonb_build_object(
                    'conflict_type', 'node_id',
                    'project_id', project_row.id,
                    'legacy_workspace_id', legacy_workspace_id,
                    'canonical_workspace_id', canonical_id,
                    'pointer_root_node_id', project_row.knowledge_node_id,
                    'conflict_node_id', conflict_row.id,
                    'recovery', 'leave source row unchanged; inspect and reconcile the canonical workspace before retrying'
                )::text;
        END IF;

        -- ``system_key`` is unique within a workspace.  Do not replace an
        -- active canonical row with another node/body.  An archived row is not
        -- user-visible; clear only its key and retain it as an audit trail.
        FOR conflict_row IN
            SELECT legacy.id AS legacy_id,
                   legacy.system_key,
                   existing.id AS existing_id,
                   existing.archived_at
            FROM _aoi_project_docs_nodes AS legacy
            JOIN knowledge_nodes AS existing
              ON existing.workspace_id = canonical_id
             AND existing.system_key = legacy.system_key
             AND existing.id <> legacy.id
            WHERE legacy.system_key IS NOT NULL
            ORDER BY legacy.id
        LOOP
            IF conflict_row.archived_at IS NULL THEN
                INSERT INTO docs_workspace_migration_log (
                    project_id, legacy_workspace_id, canonical_workspace_id,
                    root_node_id, moved_count, status, metadata
                ) VALUES (
                    project_row.id, legacy_workspace_id, canonical_id,
                    project_row.knowledge_node_id, 0, 'conflict',
                    jsonb_build_object(
                        'conflict_type', 'system_key',
                        'legacy_node_id', conflict_row.legacy_id,
                        'canonical_node_id', conflict_row.existing_id,
                        'system_key', conflict_row.system_key
                    )
                );
                RAISE EXCEPTION '%',
                    format(
                        'project %s has an active canonical Docs system_key conflict (%s)',
                        project_row.id, conflict_row.system_key
                    )
                    USING DETAIL = jsonb_build_object(
                        'conflict_type', 'system_key',
                        'project_id', project_row.id,
                        'legacy_workspace_id', legacy_workspace_id,
                        'canonical_workspace_id', canonical_id,
                        'pointer_root_node_id', project_row.knowledge_node_id,
                        'legacy_node_id', conflict_row.legacy_id,
                        'canonical_node_id', conflict_row.existing_id,
                        'system_key', conflict_row.system_key,
                        'canonical_archived_at', conflict_row.archived_at,
                        'recovery', 'no rows were replaced; preserve both bodies, resolve the key collision, then retry'
                    )::text;
            END IF;

            UPDATE knowledge_nodes
            SET system_key = NULL,
                updated_at = now()
            WHERE id = conflict_row.existing_id;
            archived_conflicts := archived_conflicts || jsonb_build_array(
                jsonb_build_object(
                    'node_id', conflict_row.existing_id,
                    'system_key', conflict_row.system_key,
                    'archived_at', conflict_row.archived_at
                )
            );
        END LOOP;

        SELECT jsonb_build_object(
            'created_workspace', created_workspace,
            'archived_conflicts', archived_conflicts,
            'nodes', coalesce(
                jsonb_agg(
                    jsonb_build_object(
                        'id', id,
                        'workspace_id', old_workspace_id,
                        'parent_id', old_parent_id,
                        'root_page_id', old_root_page_id,
                        'project_id', old_project_id
                    )
                    ORDER BY id
                ),
                '[]'::jsonb
            )
        )
        INTO state_metadata
        FROM _aoi_project_docs_nodes;

        -- The source root's old personal parent is deliberately not moved:
        -- canonical project workspaces own their root.  Descendants retain
        -- their IDs and internal parent references.
        UPDATE knowledge_nodes AS node
        SET workspace_id = canonical_id,
            project_id = project_row.id,
            parent_id = CASE
                WHEN node.id = project_row.knowledge_node_id THEN NULL
                WHEN EXISTS (
                    SELECT 1
                    FROM _aoi_project_docs_nodes AS parent
                    WHERE parent.id = node.parent_id
                ) THEN node.parent_id
                ELSE NULL
            END,
            root_page_id = CASE
                WHEN node.id = project_row.knowledge_node_id
                    THEN project_row.knowledge_node_id
                ELSE project_row.knowledge_node_id
            END,
            updated_at = now()
        FROM _aoi_project_docs_nodes AS legacy
        WHERE node.id = legacy.id;

        -- These are derived workspace projections.  References, edges,
        -- attachments, revisions and placements keep their node IDs and need
        -- no rewrite.
        UPDATE knowledge_search_index AS search_index
        SET workspace_id = canonical_id,
            project_id = project_row.id,
            updated_at = now()
        WHERE search_index.node_id IN (
            SELECT id FROM _aoi_project_docs_nodes
        );

        UPDATE knowledge_ai_suggestions AS suggestion
        SET workspace_id = canonical_id
        WHERE suggestion.node_id IN (
            SELECT id FROM _aoi_project_docs_nodes
        );

        -- A project can have more than one legacy project-information root in
        -- a personal workspace (for example, an older import and a
        -- runtime-created project-information node).  The pointer subtree
        -- above is authoritative; only rows carrying one of the two exact
        -- legacy project-information system keys are candidates.  In
        -- particular, ``project_id = project_row.id`` is *not* sufficient:
        -- ordinary Personal Docs and Agent Memory indexes may use that field.
        -- Lock every candidate before selecting roots.  Active candidate
        -- subtrees are archived in place, never deleted or rewritten, and
        -- receive an individual migration log row.
        TRUNCATE _aoi_project_docs_candidates;
        INSERT INTO _aoi_project_docs_candidates (
            id, old_workspace_id, old_parent_id, old_root_page_id,
            old_project_id, system_key, old_archived_at
        )
        SELECT n.id,
               n.workspace_id,
               n.parent_id,
               n.root_page_id,
               n.project_id,
               n.system_key,
               n.archived_at
        FROM knowledge_nodes AS n
        JOIN knowledge_workspaces AS personal
          ON personal.id = n.workspace_id
         AND personal.workspace_type = 'personal'
        WHERE n.system_key IN (
            'project_information:' || project_row.id::text,
            'project_info:' || project_row.id::text
        )
          AND NOT EXISTS (
              SELECT 1
              FROM _aoi_project_docs_nodes AS canonical
              WHERE canonical.id = n.id
          );

        -- Explicit FOR UPDATE iteration is intentional: a single SELECT ...
        -- FOR UPDATE would only lock one row when consumed into a RECORD.
        FOR conflict_row IN
            SELECT n.id
            FROM knowledge_nodes AS n
            JOIN _aoi_project_docs_candidates AS candidate ON candidate.id = n.id
            FOR UPDATE
        LOOP
            NULL;
        END LOOP;

        DELETE FROM _aoi_project_docs_candidates AS candidate
        WHERE EXISTS (
            SELECT 1
            FROM _aoi_project_docs_nodes AS canonical
            WHERE canonical.id = candidate.id
        );

        -- Archive only top-level *active* candidates.  Descendants are
        -- included by the recursive archive CTE, even when their project_id
        -- is NULL.  If a candidate root is already archived but contains an
        -- active nested legacy candidate, that nested candidate has no active
        -- candidate parent and is selected here instead of being stranded by
        -- the top-level test.
        FOR conflict_row IN
            SELECT candidate.id
            FROM _aoi_project_docs_candidates AS candidate
            WHERE candidate.old_archived_at IS NULL
              AND NOT EXISTS (
                  SELECT 1
                  FROM _aoi_project_docs_candidates AS parent_candidate
                  WHERE parent_candidate.id = candidate.old_parent_id
                    AND parent_candidate.old_archived_at IS NULL
              )
            ORDER BY candidate.id
        LOOP
            SELECT old_workspace_id
            INTO legacy_workspace_id
            FROM _aoi_project_docs_candidates
            WHERE id = conflict_row.id;
            deep_node_id := NULL;
            WITH RECURSIVE deep_duplicate AS (
                SELECT n.id,
                       n.workspace_id,
                       0::integer AS depth,
                       ARRAY[n.id]::uuid[] AS path
                FROM knowledge_nodes AS n
                WHERE n.id = conflict_row.id
                  AND n.workspace_id = legacy_workspace_id
                UNION
                SELECT child.id,
                       child.workspace_id,
                       parent.depth + 1,
                       parent.path || child.id
                FROM knowledge_nodes AS child
                JOIN deep_duplicate AS parent
                  ON parent.id = child.parent_id
                 AND child.workspace_id = parent.workspace_id
                WHERE parent.depth < 513
                  AND NOT child.id = ANY(parent.path)
            )
            SELECT id
            INTO deep_node_id
            FROM deep_duplicate
            WHERE depth > 512
            LIMIT 1;
            IF deep_node_id IS NOT NULL THEN
                RAISE EXCEPTION
                    '20260808_0015 duplicate subtree exceeds depth 512 at node %',
                    deep_node_id;
            END IF;
            TRUNCATE _aoi_project_docs_archive_nodes;
            WITH RECURSIVE duplicate_subtree AS (
                SELECT n.id,
                       n.archived_at,
                       n.workspace_id,
                       n.parent_id,
                       n.project_id,
                       n.title,
                       n.system_key,
                       0::integer AS depth,
                       ARRAY[n.id]::uuid[] AS path
                FROM knowledge_nodes AS n
                JOIN _aoi_project_docs_candidates AS candidate_root
                  ON candidate_root.id = conflict_row.id
                 AND candidate_root.old_workspace_id = n.workspace_id
                WHERE n.id = conflict_row.id
                UNION
                SELECT child.id,
                       child.archived_at,
                       child.workspace_id,
                       child.parent_id,
                       child.project_id,
                       child.title,
                       child.system_key,
                       parent.depth + 1,
                       parent.path || child.id
                FROM knowledge_nodes AS child
                JOIN duplicate_subtree AS parent ON parent.id = child.parent_id
                JOIN _aoi_project_docs_candidates AS candidate_root
                  ON candidate_root.id = conflict_row.id
                 AND candidate_root.old_workspace_id = child.workspace_id
                WHERE parent.depth < 512
                  AND child.workspace_id = parent.workspace_id
                  AND NOT child.id = ANY(parent.path)
            )
            INSERT INTO _aoi_project_docs_archive_nodes (
                id, old_archived_at, old_workspace_id, old_parent_id,
                old_project_id, title, system_key
            )
            SELECT id, archived_at, workspace_id, parent_id,
                   project_id, title, system_key
            FROM duplicate_subtree;

            SELECT count(*) INTO node_count
            FROM _aoi_project_docs_archive_nodes;

            UPDATE knowledge_nodes AS node
            SET archived_at = coalesce(node.archived_at, now()),
                updated_at = now()
            FROM _aoi_project_docs_archive_nodes AS archived
            WHERE node.id = archived.id;

            SELECT old_workspace_id
            INTO duplicate_workspace_id
            FROM _aoi_project_docs_candidates
            WHERE id = conflict_row.id;

            SELECT jsonb_build_object(
                'action', 'archive_duplicate',
                'nodes', coalesce(
                    jsonb_agg(
                        jsonb_build_object(
                            'id', archived.id,
                            'archived_at', archived.old_archived_at,
                            'workspace_id', archived.old_workspace_id,
                            'parent_id', archived.old_parent_id,
                            'project_id', archived.old_project_id,
                            'title', archived.title,
                            'system_key', archived.system_key
                        )
                        ORDER BY archived.id
                    ),
                    '[]'::jsonb
                )
            )
            INTO archive_metadata
            FROM _aoi_project_docs_archive_nodes AS archived;

            INSERT INTO docs_workspace_migration_log (
                project_id,
                legacy_workspace_id,
                canonical_workspace_id,
                root_node_id,
                moved_count,
                status,
                metadata
            ) VALUES (
                project_row.id,
                duplicate_workspace_id,
                canonical_id,
                conflict_row.id,
                node_count,
                'archived_duplicate',
                archive_metadata
            );
        END LOOP;

        INSERT INTO docs_workspace_migration_log (
            project_id,
            legacy_workspace_id,
            canonical_workspace_id,
            root_node_id,
            moved_count,
            status,
            metadata
        ) VALUES (
            project_row.id,
            legacy_workspace_id,
            canonical_id,
            project_row.knowledge_node_id,
            CASE WHEN legacy_workspace_id = canonical_id THEN 0 ELSE pointer_node_count END,
            CASE WHEN legacy_workspace_id = canonical_id
                 THEN 'already_canonical' ELSE 'moved' END,
            state_metadata
        );
    END LOOP;
END
$$;
"""


_DOWNGRADE_SQL = r"""
DO $$
DECLARE
    migration_row RECORD;
    state jsonb;
BEGIN
    FOR migration_row IN
        SELECT id, status, canonical_workspace_id, metadata
        FROM docs_workspace_migration_log
        WHERE status IN ('moved', 'already_canonical', 'archived_duplicate')
        ORDER BY created_at DESC, id DESC
    LOOP
        IF migration_row.status = 'archived_duplicate' THEN
            UPDATE knowledge_nodes AS node
            SET archived_at = NULLIF(state_row.archived_at, '')::timestamptz,
                updated_at = now()
            FROM jsonb_to_recordset(migration_row.metadata->'nodes') AS state_row(
                id text,
                archived_at text
            )
            WHERE node.id = (state_row.id)::uuid;
            CONTINUE;
        END IF;

        -- Restore the exact workspace/project/parent state captured before the
        -- move.  Body ciphertext and all node IDs remain untouched.
        UPDATE knowledge_nodes AS node
        SET workspace_id = (state_row.workspace_id)::uuid,
            parent_id = NULLIF(state_row.parent_id, '')::uuid,
            root_page_id = NULLIF(state_row.root_page_id, '')::uuid,
            project_id = NULLIF(state_row.project_id, '')::uuid,
            updated_at = now()
        FROM jsonb_to_recordset(migration_row.metadata->'nodes') AS state_row(
            id text,
            workspace_id text,
            parent_id text,
            root_page_id text,
            project_id text
        )
        WHERE node.id = (state_row.id)::uuid
          AND node.workspace_id = migration_row.canonical_workspace_id;

        UPDATE knowledge_search_index AS search_index
        SET workspace_id = (state_row.workspace_id)::uuid,
            project_id = NULLIF(state_row.project_id, '')::uuid,
            updated_at = now()
        FROM jsonb_to_recordset(migration_row.metadata->'nodes') AS state_row(
            id text,
            workspace_id text,
            parent_id text,
            root_page_id text,
            project_id text
        )
        WHERE search_index.node_id = (state_row.id)::uuid;

        UPDATE knowledge_ai_suggestions AS suggestion
        SET workspace_id = node.workspace_id
        FROM knowledge_nodes AS node
        WHERE suggestion.node_id = node.id
          AND node.id IN (
              SELECT (state_row.id)::uuid
              FROM jsonb_to_recordset(migration_row.metadata->'nodes') AS state_row(
                  id text,
                  workspace_id text,
                  parent_id text,
                  root_page_id text,
                  project_id text
              )
          );

        FOR state IN
            SELECT value
            FROM jsonb_array_elements(
                coalesce(migration_row.metadata->'archived_conflicts', '[]'::jsonb)
            )
        LOOP
            UPDATE knowledge_nodes
            SET system_key = state->>'system_key',
                updated_at = now()
            WHERE id = (state->>'node_id')::uuid
              AND system_key IS NULL;
        END LOOP;

        IF coalesce((migration_row.metadata->>'created_workspace')::boolean, false) THEN
            DELETE FROM knowledge_workspaces AS workspace
            WHERE workspace.id = migration_row.canonical_workspace_id
              AND NOT EXISTS (
                  SELECT 1
                  FROM knowledge_nodes AS node
                  WHERE node.workspace_id = workspace.id
              );
        END IF;
    END LOOP;
END
$$;
"""


def upgrade() -> None:
    op.create_table(
        "docs_workspace_migration_log",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("legacy_workspace_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("canonical_workspace_id", postgresql.UUID(as_uuid=True), nullable=True),
        # Missing legacy pointers are logged with the old UUID in metadata;
        # the FK column must therefore permit NULL.
        sa.Column("root_node_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("moved_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column(
            "metadata",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_docs_workspace_migration_log_project",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["legacy_workspace_id"],
            ["knowledge_workspaces.id"],
            name="fk_docs_workspace_migration_log_legacy_workspace",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["canonical_workspace_id"],
            ["knowledge_workspaces.id"],
            name="fk_docs_workspace_migration_log_canonical_workspace",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["root_node_id"],
            ["knowledge_nodes.id"],
            name="fk_docs_workspace_migration_log_root_node",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "status IN ('moved', 'already_canonical', 'archived_duplicate', "
            "'conflict', 'missing_root')",
            name="ck_docs_workspace_migration_log_status",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_docs_workspace_migration_log_project",
        "docs_workspace_migration_log",
        ["project_id"],
    )
    op.create_index(
        "ix_docs_workspace_migration_log_root",
        "docs_workspace_migration_log",
        ["root_node_id"],
    )
    op.execute(sa.text(_UPGRADE_SQL))


def downgrade() -> None:
    op.execute(sa.text(_DOWNGRADE_SQL))
    op.drop_index(
        "ix_docs_workspace_migration_log_root",
        table_name="docs_workspace_migration_log",
    )
    op.drop_index(
        "ix_docs_workspace_migration_log_project",
        table_name="docs_workspace_migration_log",
    )
    op.drop_table("docs_workspace_migration_log")
