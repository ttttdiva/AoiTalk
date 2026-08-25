import { NextRequest, NextResponse } from "next/server";
import { and, asc, eq, inArray, isNull, or, sql } from "drizzle-orm";
import { db } from "@/db";
import {
  knowledgeFieldValues,
  knowledgeFields,
  knowledgeNodePlacements,
  knowledgeNodes,
  knowledgeNodeSupertags,
  knowledgeSupertagFields,
  knowledgeSupertags,
} from "@/db/schema";
import { getSession } from "@/lib/auth";
import {
  getUserProjects,
  getWorkspaceViews,
  getDocsNodeAccess,
  requireDocsNode,
  serializeField,
  serializeFieldValue,
  serializeNode,
  serializeNodePlacement,
  serializeNodeSupertag,
  serializeSupertag,
  serializeSupertagField,
  serializeView,
} from "@/lib/server/knowledge-docs-utils";
import { listDocsTaskSyntheticFieldValues } from "@/lib/server/docs-task-binding";

export async function GET(
  _request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const { id } = await params;
  const access = await requireDocsNode(id, user, "read");
  if (!access) {
    return NextResponse.json({ detail: "nodeが見つからないか権限がありません" }, { status: 404 });
  }

  const rootPageId = access.node.rootPageId ?? access.node.id;
  const rawNodes = await db
    .select()
    .from(knowledgeNodes)
    .where(
      and(
        eq(knowledgeNodes.docsLibraryId, access.workspace.id),
        isNull(knowledgeNodes.archivedAt),
        sql<boolean>`(
          regexp_replace(trim(${knowledgeNodes.title}), '[[:space:]]+', '', 'g') <> ''
          OR EXISTS (
            SELECT 1 FROM knowledge_nodes AS blank_child_descendant
            WHERE blank_child_descendant.parent_id = ${knowledgeNodes.id}
              AND blank_child_descendant.docs_library_id = ${access.workspace.id}
              AND blank_child_descendant.archived_at IS NULL
              AND regexp_replace(trim(blank_child_descendant.title), '[[:space:]]+', '', 'g') <> ''
          )
        )`,
        sql<boolean>`NOT (
          ${knowledgeNodes.title} = '（空行）'
          AND EXISTS (
            WITH RECURSIVE email_ancestors AS (
              SELECT id, parent_id, system_key, docs_library_id,
                     ARRAY[id]::uuid[] AS visited_path, 0 AS depth
              FROM knowledge_nodes
              WHERE id = ${knowledgeNodes.id}
                AND docs_library_id = ${access.workspace.id}
              UNION ALL
              SELECT parent.id, parent.parent_id, parent.system_key,
                     parent.docs_library_id,
                     child.visited_path || ARRAY[parent.id]::uuid[], child.depth + 1
              FROM knowledge_nodes AS parent
              INNER JOIN email_ancestors AS child ON parent.id = child.parent_id
              WHERE parent.docs_library_id = ${access.workspace.id}
                AND child.depth < 512
                AND NOT parent.id = ANY(child.visited_path)
            )
            SELECT 1 FROM email_ancestors WHERE system_key LIKE 'project_mail:%'
          )
        )`,
        or(eq(knowledgeNodes.id, rootPageId), eq(knowledgeNodes.rootPageId, rootPageId)),
      ),
    )
    .orderBy(asc(knowledgeNodes.sortOrder));
  const accessRows = await Promise.all(
    rawNodes.map((node) => requireDocsNode(node.id, user, "read")),
  );
  const allowedNodeIds = new Set(
    accessRows
      .filter((item): item is NonNullable<typeof item> => Boolean(item))
      .map((item) => item.node.id),
  );
  const nodes = rawNodes.filter((node) => allowedNodeIds.has(node.id));
  const nodeIds = nodes.map((node) => node.id);

  const [
    supertags,
    supertagFields,
    fields,
    nodeSupertags,
    storedFieldValues,
    placements,
    views,
    projects,
  ] = await Promise.all([
    db
      .select()
      .from(knowledgeSupertags)
      .where(eq(knowledgeSupertags.docsLibraryId, access.workspace.id))
      .orderBy(asc(knowledgeSupertags.name)),
    db
      .select({ relation: knowledgeSupertagFields })
      .from(knowledgeSupertagFields)
      .innerJoin(knowledgeSupertags, eq(knowledgeSupertagFields.supertagId, knowledgeSupertags.id))
      .where(eq(knowledgeSupertags.docsLibraryId, access.workspace.id))
      .then((rows) => rows.map((row) => row.relation)),
    db
      .select()
      .from(knowledgeFields)
      .where(eq(knowledgeFields.docsLibraryId, access.workspace.id))
      .orderBy(asc(knowledgeFields.sortOrder), asc(knowledgeFields.name)),
    nodeIds.length
      ? db
          .select({
            relation: knowledgeNodeSupertags,
            supertagWorkspaceId: knowledgeSupertags.docsLibraryId,
          })
          .from(knowledgeNodeSupertags)
          .innerJoin(knowledgeSupertags, eq(knowledgeNodeSupertags.supertagId, knowledgeSupertags.id))
          .where(
            and(
              inArray(knowledgeNodeSupertags.nodeId, nodeIds),
              eq(knowledgeSupertags.docsLibraryId, access.workspace.id),
            ),
          )
          .then((rows) => rows
            .filter((row) => row.supertagWorkspaceId === access.workspace.id)
            .map((row) => row.relation))
      : Promise.resolve([]),
    nodeIds.length
      ? db
          .select({
            value: knowledgeFieldValues,
            fieldWorkspaceId: knowledgeFields.docsLibraryId,
          })
          .from(knowledgeFieldValues)
          .innerJoin(knowledgeFields, eq(knowledgeFieldValues.fieldId, knowledgeFields.id))
          .where(
            and(
              inArray(knowledgeFieldValues.nodeId, nodeIds),
              eq(knowledgeFields.docsLibraryId, access.workspace.id),
            ),
          )
          .then((rows) => rows
            .filter((row) => row.fieldWorkspaceId === access.workspace.id)
            .map((row) => row.value))
      : Promise.resolve([]),
    nodeIds.length
      ? db
          .select()
          .from(knowledgeNodePlacements)
          .where(
            or(
              inArray(knowledgeNodePlacements.nodeId, nodeIds),
              inArray(knowledgeNodePlacements.parentNodeId, nodeIds),
            ),
          )
      : Promise.resolve([]),
    getWorkspaceViews(access.workspace.id),
    getUserProjects(user.id),
  ]);
  const taskFieldValues = nodeIds.length
    ? await listDocsTaskSyntheticFieldValues({ nodeIds, fields, user })
    : [];
  const targetIds = Array.from(new Set(
    storedFieldValues
      .map((value) => value.targetNodeId)
      .filter((value): value is string => Boolean(value)),
  ));
  const targetAccessRows = await Promise.all(
    targetIds.map((targetId) => getDocsNodeAccess(targetId, user)),
  );
  const targetAccessById = new Map(
    targetAccessRows
      .filter((item): item is NonNullable<typeof item> => Boolean(item))
      .filter((item) => item.workspace.id === access.workspace.id)
      .map((item) => [item.node.id, item]),
  );
  const visibleStoredFieldValues = storedFieldValues.filter(
    (value) => !value.targetNodeId || targetAccessById.has(value.targetNodeId),
  );
  const fieldValues = [...visibleStoredFieldValues, ...taskFieldValues];

  return NextResponse.json({
    focus_node_id: access.node.id,
    root_page_id: rootPageId,
    nodes: nodes.map(serializeNode),
    supertags: supertags.map(serializeSupertag),
    supertag_fields: supertagFields.map(serializeSupertagField),
    placements: placements.map(serializeNodePlacement),
    fields: fields.map(serializeField),
    node_supertags: nodeSupertags.map(serializeNodeSupertag),
    field_values: fieldValues.map(serializeFieldValue),
    views: views.map(serializeView),
    projects,
  });
}
