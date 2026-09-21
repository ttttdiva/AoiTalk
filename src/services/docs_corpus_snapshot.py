"""Hash the stored inputs of selected Docs records without returning bodies.

Used only to distinguish relevant corpus edits from unrelated library traffic.
It is not a historical content store or a mutation authorization mechanism.

The overview read projection is deliberately narrower than a whole-library
snapshot.  In particular, a selected node sees its direct, same-library
supertags and the same-library parent closure of those tags; it does not see
every tag, field, or link in the library.  Keep the SQL below aligned with
``build_docs_read_projection`` and ``DocsGraphService.resolve_node_fields``.
Reference values follow the projection's selected-ID gate: an unselected
target is omitted, while a selected target may belong to another library and
retains that library's own visibility/ancestor context.
Selected nodes likewise retain only visibility-relevant ancestor/tag/ACL
context; ancestor content is not part of the fingerprint.

``corpus_fingerprint`` has no actor argument, so task visibility cannot be
resolved to one user's Project ACL here.  It therefore keeps a conservative
dependency only for live tasks whose effective system-field definitions can
produce synthetic task segments, and hashes only those synthetic values.
"""
from __future__ import annotations

import hashlib
from uuid import UUID

from sqlalchemy import select, text

from ..memory.models import KnowledgeNode
from .docs_graph_service import TASK_FIELD_TO_TASK_UPDATE


# The CTE is repeated for each source table so each result is independently
# ordered and hashed.  Task system-field keys are the shared application
# constant ``TASK_FIELD_TO_TASK_UPDATE``.  The only caller-supplied bound
# values are the selected-node and projection-bound UUID arrays.
_SELECTION_CTE = """
WITH RECURSIVE
selected_nodes AS (
    SELECT n.*
    FROM knowledge_nodes n
    WHERE n.id = ANY(CAST(:ids AS uuid[]))
),
selected_context_nodes(id, docs_library_id, parent_id, project_id) AS (
    SELECT n.id, n.docs_library_id, n.parent_id, n.project_id
    FROM selected_nodes n
    UNION
    SELECT parent.id, parent.docs_library_id, parent.parent_id, parent.project_id
    FROM knowledge_nodes parent
    JOIN selected_context_nodes child
      ON child.parent_id = parent.id
     AND child.docs_library_id = parent.docs_library_id
),
node_direct_tags AS (
    SELECT DISTINCT ns.node_id, ns.supertag_id, n.docs_library_id
    FROM knowledge_node_supertags ns
    JOIN selected_nodes n ON n.id = ns.node_id
    JOIN knowledge_supertags t ON t.id = ns.supertag_id
    WHERE t.docs_library_id = n.docs_library_id
),
node_effective_tags(node_id, id, docs_library_id, parent_supertag_id) AS (
    SELECT d.node_id, t.id, t.docs_library_id, t.parent_supertag_id
    FROM knowledge_supertags t
    JOIN node_direct_tags d
      ON d.supertag_id = t.id
     AND d.docs_library_id = t.docs_library_id
    UNION
    SELECT child.node_id, parent.id, parent.docs_library_id, parent.parent_supertag_id
    FROM knowledge_supertags parent
    JOIN node_effective_tags child
      ON child.parent_supertag_id = parent.id
     AND child.docs_library_id = parent.docs_library_id
),
direct_tags AS (
    SELECT DISTINCT supertag_id, docs_library_id
    FROM node_direct_tags
),
effective_tags(id, docs_library_id, parent_supertag_id) AS (
    SELECT DISTINCT id, docs_library_id, parent_supertag_id
    FROM node_effective_tags
),
reference_values AS (
    SELECT DISTINCT fv.target_node_id AS node_id
    FROM knowledge_field_values fv
    JOIN selected_nodes n ON n.id = fv.node_id
    JOIN knowledge_fields f
      ON f.id = fv.field_id
     AND f.docs_library_id = n.docs_library_id
    WHERE fv.target_node_id IS NOT NULL
      AND lower(f.field_type) = 'reference'
      AND fv.target_node_id IN (SELECT id FROM selected_nodes)
),
reference_nodes(id, docs_library_id, parent_id, project_id) AS (
    SELECT target.id, target.docs_library_id, target.parent_id, target.project_id
    FROM knowledge_nodes target
    JOIN reference_values rv ON rv.node_id = target.id
    UNION
    SELECT parent.id, parent.docs_library_id, parent.parent_id, parent.project_id
    FROM knowledge_nodes parent
    JOIN reference_nodes child
      ON child.parent_id = parent.id
     AND child.docs_library_id = parent.docs_library_id
),
context_nodes AS (
    SELECT id, docs_library_id, parent_id, project_id
    FROM selected_context_nodes
    UNION
    SELECT id, docs_library_id, parent_id, project_id
    FROM reference_nodes
),
context_tags AS (
    SELECT DISTINCT ns.supertag_id, rn.docs_library_id
    FROM knowledge_node_supertags ns
    JOIN context_nodes rn ON rn.id = ns.node_id
    JOIN knowledge_supertags t
      ON t.id = ns.supertag_id
     AND t.docs_library_id = rn.docs_library_id
),
context_projects AS (
    SELECT DISTINCT cn.project_id AS id
    FROM context_nodes cn
    WHERE cn.project_id IS NOT NULL
    UNION
    SELECT DISTINCT p.id
    FROM projects p
    JOIN context_nodes cn ON cn.id = p.knowledge_node_id
),
context_libraries AS (
    SELECT DISTINCT docs_library_id AS id
    FROM context_nodes
),
context_users AS (
    SELECT DISTINCT p.owner_id AS id
    FROM projects p
    JOIN context_projects cp ON cp.id = p.id
    WHERE p.owner_id IS NOT NULL
    UNION
    SELECT DISTINCT pm.user_id
    FROM project_members pm
    JOIN context_projects cp ON cp.id = pm.project_id
    WHERE pm.user_id IS NOT NULL
    UNION
    SELECT u.id
    FROM users u
    WHERE lower(u.role) = 'admin'
),
stored_field_ids AS (
    SELECT DISTINCT fv.field_id, n.docs_library_id
    FROM knowledge_field_values fv
    JOIN selected_nodes n ON n.id = fv.node_id
    JOIN knowledge_fields f
      ON f.id = fv.field_id
     AND f.docs_library_id = n.docs_library_id
),
relevant_fields AS (
    SELECT f.id
    FROM knowledge_fields f
    WHERE EXISTS (
        SELECT 1
        FROM effective_tags et
        WHERE et.id = f.supertag_id
          AND et.docs_library_id = f.docs_library_id
    )
    OR EXISTS (
        SELECT 1
        FROM stored_field_ids sf
        WHERE sf.field_id = f.id
          AND sf.docs_library_id = f.docs_library_id
    )
    OR EXISTS (
        SELECT 1
        FROM knowledge_supertag_fields sf
        JOIN effective_tags et ON et.id = sf.supertag_id
        JOIN knowledge_supertags field_owner ON field_owner.id = f.supertag_id
        WHERE sf.field_id = f.id
          AND et.docs_library_id = f.docs_library_id
          AND field_owner.docs_library_id = et.docs_library_id
    )
),
node_task_projection_fields(node_id, field_key) AS (
    SELECT DISTINCT n.id, lower(f.system_key)
    FROM selected_nodes n
    JOIN node_effective_tags nt
      ON nt.node_id = n.id
     AND nt.docs_library_id = n.docs_library_id
    JOIN knowledge_fields f
      ON f.docs_library_id = n.docs_library_id
     AND (
         f.supertag_id = nt.id
         OR EXISTS (
             SELECT 1
             FROM knowledge_supertag_fields sf
             JOIN knowledge_supertags field_owner
               ON field_owner.id = f.supertag_id
              AND field_owner.docs_library_id = n.docs_library_id
             WHERE sf.supertag_id = nt.id
               AND sf.field_id = f.id
         )
     )
    WHERE f.system_key = ANY(CAST(:task_system_keys AS text[]))
)
"""


async def corpus_fingerprint(session, node_ids):
    ids = [UUID(str(value)) for value in node_ids]
    digest = hashlib.sha256(b"docs-record-snapshot.v2")
    if not ids:
        return digest.hexdigest()

    selected_result = await session.execute(
        select(KnowledgeNode).where(KnowledgeNode.id.in_(ids))
    )
    selected_rows = selected_result.scalars().all()
    qa_project_ids = sorted(
        {
            row.project_id
            for row in selected_rows
            if row.project_id is not None
            and isinstance(row.body_json, dict)
            and row.body_json.get("format") == "project_information_doc_block"
            and bool(row.body_json.get("blocks"))
        },
        key=str,
    )

    sources = [
        (
            "knowledge_nodes",
            "r.id",
            "r.id IN (SELECT id FROM selected_nodes) "
            "OR r.id IN (SELECT id FROM context_nodes)",
            "CASE WHEN r.id IN (SELECT id FROM selected_nodes) THEN to_jsonb(r) "
            "ELSE jsonb_build_object("
            "'id', r.id, "
            "'docs_library_id', r.docs_library_id, "
            "'parent_id', r.parent_id, "
            "'system_key', r.system_key, "
            "'hub_root_page_id', CASE WHEN btrim(r.system_key)='project_information_root' "
            "THEN r.root_page_id END, "
            "'hub_archived_at', CASE WHEN btrim(r.system_key)='project_information_root' "
            "THEN r.archived_at END, "
            "'hub_title', CASE WHEN btrim(r.system_key)='project_information_root' "
            "THEN r.title END"
            ") END",
        ),
        (
            "docs_libraries",
            "r.id",
            "r.id IN (SELECT id FROM context_libraries)",
            "jsonb_build_object("
            "'id', r.id, 'owner_user_id', r.owner_user_id, 'library_type', r.library_type"
            ")",
        ),
        (
            "knowledge_node_shares",
            "r.id",
            "r.node_id IN (SELECT id FROM context_nodes)",
            "jsonb_build_object("
            "'id', r.id, 'node_id', r.node_id, 'user_id', r.user_id, "
            "'permission', r.permission"
            ")",
        ),
        (
            "projects",
            "r.id",
            "r.id IN (SELECT id FROM context_projects) "
            "OR r.knowledge_node_id IN (SELECT id FROM context_nodes)",
            "jsonb_build_object("
            "'id', r.id, 'owner_id', r.owner_id, 'deleted_at', r.deleted_at, "
            "'is_completed', r.is_completed, 'knowledge_node_id', r.knowledge_node_id"
            ")",
        ),
        (
            "project_members",
            "r.id",
            "r.project_id IN (SELECT id FROM context_projects)",
            "jsonb_build_object("
            "'id', r.id, 'project_id', r.project_id, 'user_id', r.user_id, "
            "'role', r.role, 'permissions', r.permissions"
            ")",
        ),
        (
            "users",
            "r.id",
            "r.id IN (SELECT id FROM context_users)",
            "jsonb_build_object('id', r.id, 'role', r.role)",
        ),
        (
            "knowledge_field_values",
            "r.node_id::text||':'||r.field_id::text",
            "EXISTS ("
            "SELECT 1 FROM selected_nodes n "
            "JOIN knowledge_fields f ON f.id = r.field_id "
            "AND f.docs_library_id = n.docs_library_id "
            "WHERE n.id = r.node_id "
            "AND (lower(f.field_type) <> 'reference' "
            "OR r.target_node_id IS NULL "
            "OR r.target_node_id IN (SELECT id FROM selected_nodes))"
            "AND NOT ("
            "f.system_key IS NOT NULL "
            "AND f.system_key = ANY(CAST(:task_system_keys AS text[])) "
            "AND EXISTS ("
            "SELECT 1 FROM tasks t "
            "WHERE t.knowledge_node_id = r.node_id "
            "AND t.deleted_at IS NULL"
            ")"
            ")"
            ")",
            "to_jsonb(r)",
        ),
        (
            "knowledge_node_supertags",
            "r.node_id::text||':'||r.supertag_id::text",
            "(r.supertag_id IN (SELECT supertag_id FROM direct_tags) "
            "AND r.node_id IN (SELECT id FROM selected_nodes)) "
            "OR (r.supertag_id IN (SELECT supertag_id FROM context_tags) "
            "AND r.node_id IN (SELECT id FROM context_nodes))",
            "CASE WHEN r.node_id IN (SELECT id FROM selected_nodes) THEN to_jsonb(r) "
            "ELSE jsonb_build_object('node_id', r.node_id, 'supertag_id', r.supertag_id) END",
        ),
        (
            "knowledge_supertags",
            "r.id",
            "r.id IN (SELECT id FROM effective_tags) "
            "OR r.id IN (SELECT supertag_id FROM context_tags)",
            "CASE WHEN r.id IN (SELECT id FROM effective_tags) THEN to_jsonb(r) "
            "ELSE jsonb_build_object("
            "'id', r.id, 'docs_library_id', r.docs_library_id, 'system_key', r.system_key"
            ") END",
        ),
        (
            "knowledge_fields",
            "r.id",
            "r.id IN (SELECT id FROM relevant_fields)",
            "to_jsonb(r)",
        ),
        (
            "knowledge_supertag_fields",
            "r.supertag_id::text||':'||r.field_id::text",
            "EXISTS ("
            "SELECT 1 FROM effective_tags et "
            "JOIN knowledge_fields f ON f.id = r.field_id "
            "AND f.docs_library_id = et.docs_library_id "
            "JOIN knowledge_supertags field_owner ON field_owner.id = f.supertag_id "
            "AND field_owner.docs_library_id = et.docs_library_id "
            "WHERE et.id = r.supertag_id "
            "AND f.id IN (SELECT id FROM relevant_fields)"
            ")",
            "to_jsonb(r)",
        ),
        (
            "tasks",
            "r.id",
            "r.knowledge_node_id IN (SELECT id FROM selected_nodes) "
            "AND r.deleted_at IS NULL "
            "AND EXISTS ("
            "SELECT 1 FROM node_task_projection_fields tf "
            "WHERE tf.node_id = r.knowledge_node_id"
            ")",
            "jsonb_build_object("
            "'knowledge_node_id', r.knowledge_node_id, "
            "'status', CASE WHEN EXISTS (SELECT 1 FROM node_task_projection_fields tf "
            "WHERE tf.node_id = r.knowledge_node_id AND tf.field_key='task_status') "
            "THEN r.status END, "
            "'end_at', CASE WHEN EXISTS (SELECT 1 FROM node_task_projection_fields tf "
            "WHERE tf.node_id = r.knowledge_node_id AND tf.field_key='task_due') "
            "THEN r.end_at END, "
            "'start_at', CASE WHEN EXISTS (SELECT 1 FROM node_task_projection_fields tf "
            "WHERE tf.node_id = r.knowledge_node_id AND tf.field_key='task_start') "
            "THEN r.start_at END, "
            "'priority', CASE WHEN EXISTS (SELECT 1 FROM node_task_projection_fields tf "
            "WHERE tf.node_id = r.knowledge_node_id AND tf.field_key='task_priority') "
            "THEN r.priority END, "
            "'project_id', r.project_id"
            ")",
        ),
        (
            "project_qa_entries",
            "r.id",
            "r.project_id = ANY(CAST(:qa_project_ids AS uuid[])) "
            "AND r.deleted_at IS NULL AND r.review_state='accepted' "
            "AND r.status<>'archived'",
            "to_jsonb(r)",
        ),
    ]
    for table, key, condition, row_json in sources:
        statement = text(
            f"{_SELECTION_CTE}"
            f"SELECT {key}::text, "
            f"encode(sha256(convert_to(({row_json})::text,'UTF8')),'hex') "
            f"FROM {table} r WHERE {condition} ORDER BY {key}"
        )
        result = await session.execute(
            statement,
            {
                "ids": ids,
                "qa_project_ids": qa_project_ids,
                "task_system_keys": list(TASK_FIELD_TO_TASK_UPDATE),
            },
        )
        digest.update(table.encode())
        for identifier, value in result.all():
            digest.update(str(identifier).encode())
            digest.update(value.encode())
    return digest.hexdigest()
