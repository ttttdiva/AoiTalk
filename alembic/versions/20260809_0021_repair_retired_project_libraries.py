"""Repair legacy Project Docs rows left by an early 0020 deployment.

Some installations reached ``20260809_0020`` after the old implementation had
already dropped ``docs_libraries.project_id`` while leaving one or more rows
whose ``library_type`` was still ``project``.  This forward-only repair infers a
single project from the surviving node/pointer identities, moves every row by
UUID into the owner Personal Docs Library, and refuses ambiguous/orphan data
before installing the non-project CHECK.  Revisions, attachments, placements,
edges, encrypted bodies, field values, and node IDs are never deleted.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260809_0021"
down_revision = "20260809_0020"
branch_labels = None
depends_on = None


_UPGRADE_SQL = r"""
DO $$
DECLARE
    lib RECORD;
    map_row RECORD;
    pointer_row RECORD;
    node_row RECORD;
    tag_row RECORD;
    personal_id uuid;
    hub_id uuid;
    root_id uuid;
    pointer_id uuid;
    node_count integer;
    candidate_count integer;
    cross_parent_count integer;
    pointer_valid boolean;
    pointer_reason text;
    canonical_tag_id uuid;
    metadata jsonb;
    legacy_view_count integer;
BEGIN
    CREATE TEMP TABLE IF NOT EXISTS _aoi_0021_library_map (
        library_id uuid PRIMARY KEY,
        project_id uuid NOT NULL,
        owner_id uuid NOT NULL
    ) ON COMMIT DROP;
    CREATE TEMP TABLE IF NOT EXISTS _aoi_0021_nodes (
        id uuid PRIMARY KEY,
        old_parent_id uuid,
        old_root_page_id uuid,
        source_library_id uuid NOT NULL,
        project_id uuid NOT NULL
    ) ON COMMIT DROP;

    -- A surviving project library has no project_id column after old 0020.
    -- knowledge_revisions, knowledge_attachments, knowledge_edges, and
    -- knowledge_node_placements remain untouched and retain node UUIDs.
    -- Infer only from a unique node.project_id or an exact projects pointer;
    -- guessing from a title/name would be an ACL and data-integrity hazard.
    WITH candidates AS (
        SELECT l.id AS library_id, n.project_id
        FROM docs_libraries AS l
        JOIN knowledge_nodes AS n ON n.docs_library_id = l.id
        WHERE l.library_type = 'project' AND n.project_id IS NOT NULL
        GROUP BY l.id, n.project_id
        UNION
        SELECT l.id AS library_id, p.id AS project_id
        FROM docs_libraries AS l
        JOIN knowledge_nodes AS n ON n.docs_library_id = l.id
        JOIN projects AS p ON p.knowledge_node_id = n.id
        WHERE l.library_type = 'project'
    )
    SELECT c.library_id
    INTO lib
    FROM candidates AS c
    GROUP BY c.library_id
    HAVING count(DISTINCT c.project_id) > 1
    LIMIT 1;
    IF FOUND THEN
        RAISE EXCEPTION '20260809_0021 project library % maps to multiple projects', lib.library_id
            USING DETAIL = 'resolve project_id/node identities before retrying; no rows were changed';
    END IF;

    INSERT INTO _aoi_0021_library_map (library_id, project_id, owner_id)
    SELECT c.library_id, c.project_id, p.owner_id
    FROM (
        SELECT l.id AS library_id, n.project_id
        FROM docs_libraries AS l
        JOIN knowledge_nodes AS n ON n.docs_library_id = l.id
        WHERE l.library_type = 'project' AND n.project_id IS NOT NULL
        GROUP BY l.id, n.project_id
        UNION
        SELECT l.id AS library_id, p.id AS project_id
        FROM docs_libraries AS l
        JOIN knowledge_nodes AS n ON n.docs_library_id = l.id
        JOIN projects AS p ON p.knowledge_node_id = n.id
        WHERE l.library_type = 'project'
    ) AS c
    JOIN projects AS p ON p.id = c.project_id
    GROUP BY c.library_id, c.project_id, p.owner_id;

    IF EXISTS (
        SELECT 1
        FROM docs_libraries AS l
        WHERE l.library_type = 'project'
          AND NOT EXISTS (SELECT 1 FROM _aoi_0021_library_map AS m WHERE m.library_id = l.id)
    ) THEN
        RAISE EXCEPTION '20260809_0021 cannot infer owner/project for a surviving project library'
            USING DETAIL = 'restore a projects pointer or quarantine the library in a reviewed transaction';
    END IF;

    FOR map_row IN
        SELECT m.*, p.name AS project_name, p.description AS project_description,
               p.knowledge_node_id AS pointer_node_id, p.project_metadata
        FROM _aoi_0021_library_map AS m
        JOIN projects AS p ON p.id = m.project_id
        ORDER BY m.library_id
    LOOP
        SELECT l.id
        INTO personal_id
        FROM docs_libraries AS l
        WHERE l.library_type = 'personal' AND l.owner_user_id = map_row.owner_id
        ORDER BY l.created_at NULLS LAST, l.id
        LIMIT 1
        FOR UPDATE;
        IF personal_id IS NULL THEN
            personal_id := gen_random_uuid();
            INSERT INTO docs_libraries (
                id, name, description, owner_user_id, library_type,
                settings_json, created_at, updated_at
            ) VALUES (
                personal_id, 'Personal Docs', NULL, map_row.owner_id, 'personal',
                '{}'::json, now(), now()
            );
        END IF;

        SELECT n.id
        INTO hub_id
        FROM knowledge_nodes AS n
        WHERE n.docs_library_id = personal_id
          AND n.project_id IS NULL
          AND n.system_key = 'project_information_root'
          AND n.parent_id IS NULL
          AND n.archived_at IS NULL
        ORDER BY n.id
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
                'Project Information', '{}'::json, 'Project Information', 'page', '',
                '{}'::json, '{}'::json, 0, now(), now(), 'project_information_root'
            )
            RETURNING id INTO hub_id;
            UPDATE knowledge_nodes SET root_page_id = hub_id, updated_at = now()
            WHERE id = hub_id;
        END IF;

        SELECT s.id
        INTO canonical_tag_id
        FROM knowledge_supertags AS s
        WHERE s.docs_library_id = personal_id AND s.system_key = 'project_info'
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
                'Project Information', 'project_information',
                'Canonical project information schema', '{}'::json,
                '[]'::json, '{}'::json, now(), now()
            )
            RETURNING id INTO canonical_tag_id;
        END IF;

        pointer_id := map_row.pointer_node_id;
        pointer_reason := NULL;
        pointer_valid := false;
        candidate_count := 0;
        root_id := NULL;

        IF pointer_id IS NOT NULL THEN
            SELECT n.*
            INTO pointer_row
            FROM knowledge_nodes AS n
            WHERE n.id = pointer_id
              AND n.project_id = map_row.project_id
              AND n.system_key = 'project_information:' || map_row.project_id::text
              AND EXISTS (
                  SELECT 1
                  FROM knowledge_node_supertags AS nt
                  JOIN knowledge_supertags AS st ON st.id = nt.supertag_id
                  WHERE nt.node_id = n.id
                    AND st.docs_library_id = n.docs_library_id
                    AND st.system_key = 'project_info'
              )
              AND (
                  (n.docs_library_id = personal_id AND n.parent_id = hub_id AND n.root_page_id = hub_id)
                  OR n.docs_library_id = map_row.library_id
              )
            FOR UPDATE;
            IF FOUND THEN
                root_id := pointer_row.id;
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

        IF NOT pointer_valid THEN
            SELECT count(*)
            INTO candidate_count
            FROM knowledge_nodes AS n
            WHERE n.docs_library_id = personal_id
              AND n.parent_id = hub_id
              AND n.project_id = map_row.project_id
              AND n.system_key = 'project_information:' || map_row.project_id::text
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
                INSERT INTO docs_workspace_migration_log (
                    project_id, legacy_workspace_id, canonical_workspace_id,
                    root_node_id, moved_count, status, metadata
                ) VALUES (
                    map_row.project_id, map_row.library_id, personal_id,
                    pointer_id, 0, 'conflict', jsonb_build_object(
                        'migration_revision', '20260809_0021',
                        'reason', 'ambiguous_canonical_candidates',
                        'candidate_count', candidate_count,
                        'personal_library_id', personal_id,
                        'provenance', 'preserved_by_transaction_rollback'
                    )
                );
                RAISE EXCEPTION
                    '20260809_0021 ambiguous canonical candidates for project %',
                    map_row.project_id;
            ELSIF candidate_count = 1 THEN
                SELECT n.id INTO root_id
                FROM knowledge_nodes AS n
                WHERE n.docs_library_id = personal_id
                  AND n.parent_id = hub_id
                  AND n.project_id = map_row.project_id
                  AND n.system_key = 'project_information:' || map_row.project_id::text
                  AND n.archived_at IS NULL
                LIMIT 1 FOR UPDATE;
            ELSE
                INSERT INTO knowledge_nodes (
                    id, docs_library_id, parent_id, root_page_id, project_id,
                    title, body_json, body_text, node_type, description,
                    display_props, view_json, sort_order, created_at, updated_at,
                    system_key
                ) VALUES (
                    gen_random_uuid(), personal_id, hub_id, NULL, map_row.project_id,
                    coalesce(map_row.project_name, 'Project'), '{}'::json,
                    coalesce(map_row.project_name, 'Project'), 'page',
                    coalesce(map_row.project_description, ''), '{}'::json, '{}'::json,
                    0, now(), now(), 'project_information:' || map_row.project_id::text
                )
                RETURNING id INTO root_id;
                UPDATE knowledge_nodes SET root_page_id = hub_id, updated_at = now()
                WHERE id = root_id;
            END IF;
        END IF;

        IF root_id IS NULL THEN
            RAISE EXCEPTION '20260809_0021 failed to resolve canonical root for project %', map_row.project_id;
        END IF;

        IF root_id IS DISTINCT FROM pointer_id AND EXISTS (
            SELECT 1 FROM knowledge_nodes AS n
            WHERE n.docs_library_id = personal_id
              AND n.system_key = 'project_information:' || map_row.project_id::text
              AND n.id <> root_id AND n.archived_at IS NULL
        ) THEN
            RAISE EXCEPTION '20260809_0021 active canonical pointer conflict for project %', map_row.project_id;
        END IF;

        TRUNCATE _aoi_0021_nodes;
        INSERT INTO _aoi_0021_nodes (id, old_parent_id, old_root_page_id, source_library_id, project_id)
        SELECT n.id, n.parent_id, n.root_page_id, map_row.library_id, map_row.project_id
        FROM knowledge_nodes AS n
        WHERE n.docs_library_id = map_row.library_id;
        SELECT count(*) INTO node_count FROM _aoi_0021_nodes;
        SELECT count(*) INTO cross_parent_count
        FROM _aoi_0021_nodes AS moving
        WHERE moving.id <> root_id AND moving.old_parent_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM _aoi_0021_nodes AS parent WHERE parent.id = moving.old_parent_id);

        FOR node_row IN
            SELECT n.id, n.system_key
            FROM knowledge_nodes AS n
            WHERE n.docs_library_id = map_row.library_id
            ORDER BY n.id FOR UPDATE
        LOOP
            IF node_row.system_key IS NOT NULL AND node_row.id <> root_id
               AND EXISTS (
                   SELECT 1 FROM knowledge_nodes AS existing
                   WHERE existing.docs_library_id = personal_id
                     AND existing.system_key = node_row.system_key
               ) THEN
                UPDATE knowledge_nodes
                SET system_key = 'legacy_project:' || map_row.project_id::text || ':' || node_row.id::text,
                    updated_at = now()
                WHERE id = node_row.id;
            END IF;
        END LOOP;

        FOR tag_row IN
            SELECT s.* FROM knowledge_supertags AS s
            WHERE s.docs_library_id = map_row.library_id
            ORDER BY s.id FOR UPDATE
        LOOP
            IF EXISTS (
                SELECT 1 FROM knowledge_supertags AS personal_tag
                WHERE personal_tag.docs_library_id = personal_id
                  AND (personal_tag.name = tag_row.name
                       OR (tag_row.system_key IS NOT NULL AND personal_tag.system_key = tag_row.system_key))
            ) THEN
                UPDATE knowledge_supertags
                SET name = left(coalesce(tag_row.name, 'Legacy tag') || ' [legacy ' || tag_row.id::text || ']', 120),
                    system_key = CASE WHEN tag_row.system_key IS NULL THEN NULL
                                      ELSE tag_row.system_key || ':legacy:' || tag_row.id::text END,
                    updated_at = now()
                WHERE id = tag_row.id;
            END IF;
            UPDATE knowledge_supertags SET docs_library_id = personal_id, updated_at = now()
            WHERE id = tag_row.id;
        END LOOP;
        UPDATE knowledge_fields SET docs_library_id = personal_id, updated_at = now()
        WHERE docs_library_id = map_row.library_id;

        UPDATE knowledge_nodes AS n
        SET docs_library_id = personal_id,
            project_id = map_row.project_id,
            parent_id = CASE
                WHEN n.id = root_id THEN hub_id
                WHEN EXISTS (SELECT 1 FROM _aoi_0021_nodes AS p WHERE p.id = moving.old_parent_id)
                    THEN moving.old_parent_id
                ELSE root_id
            END,
            root_page_id = hub_id,
            updated_at = now()
        FROM _aoi_0021_nodes AS moving
        WHERE n.id = moving.id;
        UPDATE projects SET knowledge_node_id = root_id, updated_at = now()
        WHERE id = map_row.project_id;

        UPDATE knowledge_search_index
        SET docs_library_id = personal_id, project_id = map_row.project_id, updated_at = now()
        WHERE docs_library_id = map_row.library_id
           OR node_id IN (SELECT id FROM _aoi_0021_nodes);
        UPDATE knowledge_ai_suggestions
        SET docs_library_id = personal_id, updated_at = now()
        WHERE docs_library_id = map_row.library_id
           OR node_id IN (SELECT id FROM _aoi_0021_nodes);
        -- A repair must not adopt an import job from another Docs Library or
        -- rewrite a job that belongs to a different Project.  Those rows are
        -- provenance, not descendants of the Docs node tree, so fail before
        -- mutating them and let the Alembic transaction roll back.
        IF EXISTS (
            SELECT 1
            FROM knowledge_import_jobs AS job
            WHERE job.docs_library_id = map_row.library_id
              AND job.project_id IS DISTINCT FROM map_row.project_id
        ) THEN
            RAISE EXCEPTION
                '20260809_0021 source import job project mismatch for library %',
                map_row.library_id;
        END IF;
        IF EXISTS (
            SELECT 1
            FROM knowledge_import_jobs AS job
            JOIN knowledge_import_items AS item ON item.job_id = job.id
            WHERE item.node_id IN (SELECT id FROM _aoi_0021_nodes)
              AND job.docs_library_id IS DISTINCT FROM map_row.library_id
        ) THEN
            RAISE EXCEPTION
                '20260809_0021 imported item references foreign job for library %',
                map_row.library_id;
        END IF;
        UPDATE knowledge_import_jobs AS job
        SET docs_library_id = personal_id, project_id = map_row.project_id, updated_at = now()
        WHERE job.docs_library_id = map_row.library_id
          AND (
               job.project_id = map_row.project_id
               OR job.id IN (
               SELECT item.job_id FROM knowledge_import_items AS item
               WHERE item.node_id IN (SELECT id FROM _aoi_0021_nodes)
               )
          );
        IF to_regclass('public.knowledge_views') IS NOT NULL THEN
            EXECUTE $legacy_views$
                UPDATE public.knowledge_views
                SET docs_library_id = $1,
                    updated_at = now()
                WHERE docs_library_id = $2
            $legacy_views$ USING personal_id, map_row.library_id;
        END IF;
        UPDATE knowledge_saved_views SET docs_library_id = personal_id, updated_at = now()
        WHERE docs_library_id = map_row.library_id;

        INSERT INTO knowledge_node_supertags (node_id, supertag_id, created_at, updated_at)
        VALUES (root_id, canonical_tag_id, now(), now())
        ON CONFLICT (node_id, supertag_id) DO UPDATE SET updated_at = now();

        legacy_view_count := 0;
        IF to_regclass('public.knowledge_views') IS NOT NULL THEN
            EXECUTE 'SELECT count(*) FROM public.knowledge_views WHERE docs_library_id = $1'
            INTO legacy_view_count
            USING map_row.library_id;
        END IF;
        IF EXISTS (SELECT 1 FROM knowledge_nodes WHERE docs_library_id = map_row.library_id)
           OR EXISTS (SELECT 1 FROM knowledge_supertags WHERE docs_library_id = map_row.library_id)
           OR EXISTS (SELECT 1 FROM knowledge_fields WHERE docs_library_id = map_row.library_id)
           OR EXISTS (SELECT 1 FROM knowledge_search_index WHERE docs_library_id = map_row.library_id)
           OR EXISTS (SELECT 1 FROM knowledge_ai_suggestions WHERE docs_library_id = map_row.library_id)
           OR EXISTS (SELECT 1 FROM knowledge_import_jobs WHERE docs_library_id = map_row.library_id)
           OR legacy_view_count > 0
           OR EXISTS (SELECT 1 FROM knowledge_saved_views WHERE docs_library_id = map_row.library_id)
        THEN
            RAISE EXCEPTION '20260809_0021 residual rows prevent repair of library %', map_row.library_id;
        END IF;

        metadata := jsonb_build_object(
            'migration_revision', '20260809_0021',
            'action', CASE WHEN pointer_valid THEN 'repaired_valid_pointer'
                           WHEN candidate_count = 1 THEN 'adopted_canonical_candidate'
                           ELSE 'created_canonical_root' END,
            'legacy_library_id', map_row.library_id,
            'personal_library_id', personal_id,
            'project_id', map_row.project_id,
            'pointer_id', pointer_id,
            'pointer_reason', pointer_reason,
            'root_node_id', root_id,
            'moved_node_count', node_count,
            'cross_library_parent_links', cross_parent_count,
            'preservation', jsonb_build_array('node_uuid', 'body_json', 'body_text', 'revisions', 'attachments', 'edges', 'placements', 'field_values')
        );
        INSERT INTO docs_workspace_migration_log (
            project_id, legacy_workspace_id, canonical_workspace_id,
            root_node_id, moved_count, status, metadata
        ) VALUES (map_row.project_id, map_row.library_id, personal_id,
                  root_id, node_count, 'moved', metadata);
        DELETE FROM docs_libraries WHERE id = map_row.library_id;
    END LOOP;

    IF EXISTS (SELECT 1 FROM docs_libraries WHERE library_type = 'project') THEN
        RAISE EXCEPTION '20260809_0021 project libraries remain after repair';
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
    raise RuntimeError(
        "20260809_0021 is forward-only; restore a backup and run a reviewed "
        "inverse migration instead of downgrading"
    )
