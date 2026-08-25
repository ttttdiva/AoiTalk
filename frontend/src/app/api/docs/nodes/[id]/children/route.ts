import { NextRequest, NextResponse } from "next/server";
import { and, asc, eq, gt, inArray, isNull, or, sql } from "drizzle-orm";
import { db } from "@/db";
import {
  knowledgeFieldValues,
  knowledgeFields,
  knowledgeAttachments,
  knowledgeNodes,
  knowledgeNodePlacements,
  knowledgeNodeSupertags,
  knowledgeSupertags,
} from "@/db/schema";
import { getSession } from "@/lib/auth";
import {
  decodeDocsChildrenCursor,
  docsChildPageSize,
  encodeDocsChildrenCursor,
} from "@/lib/docs-children-pagination";
import {
  getKnowledgeNodeChildMetadata,
  getDocsNodeAccess,
  requireDocsNode,
  serializeFieldValue,
  serializeNode,
  serializeNodePlacement,
  serializeNodeSupertag,
} from "@/lib/server/knowledge-docs-utils";
import { jsonWithConditional } from "@/lib/server/http-cache";

type ChildItem = {
  itemId: string;
  sortOrder: number;
  node: typeof knowledgeNodes.$inferSelect;
  placement: typeof knowledgeNodePlacements.$inferSelect | null;
};

function outlineBodyJson(value: unknown) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  const source = value as Record<string, unknown>;
  const body = Object.fromEntries(
    ["format", "block_type", "checked"]
      .filter((key) => key in source)
      .map((key) => [key, source[key]]),
  ) as Record<string, unknown>;
  // Multiline imported content is a regular editable typed block. Keep only
  // its public contract in the lightweight outline response; legacy
  // verbatim_* keys are intentionally never sent to the UI.
  if (
    source.format === "doc_block"
    && (source.block_type === "markdown" || source.block_type === "code")
    && typeof source.content === "string"
  ) {
    body.content = source.content.replace(/\r\n?/g, "\n");
    if (typeof source.label === "string") body.label = source.label;
    if (source.clip_ingest && typeof source.clip_ingest === "object" && !Array.isArray(source.clip_ingest)) {
      body.clip_ingest = source.clip_ingest;
    }
  }
  return body;
}

function hasDeferredDetails(node: ReturnType<typeof serializeNode>) {
  const body = node.body_json;
  return Boolean(body.bookmark);
}

function serializeOutlineNode(serialized: ReturnType<typeof serializeNode>) {
  return {
    ...serialized,
    body_json: outlineBodyJson(serialized.body_json),
    body_text: "",
  };
}

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await getSession();
  if (!user) return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });

  const { id } = await params;
  const access = await requireDocsNode(id, user, "read");
  if (!access) {
    return NextResponse.json({ detail: "nodeが見つからないか権限がありません" }, { status: 404 });
  }

  const rawCursor = request.nextUrl.searchParams.get("cursor");
  const cursor = decodeDocsChildrenCursor(rawCursor);
  if (rawCursor && !cursor) {
    return NextResponse.json({ detail: "cursorが不正です" }, { status: 400 });
  }
  const limit = docsChildPageSize(request.nextUrl.searchParams.get("limit"));
  // Do not pre-filter by project membership. A personal node may be
  // explicitly shared even when its project_id is inaccessible to the
  // recipient; getDocsNodeAccess below is the final authority.
  const candidateLimit = Math.min(Math.max(limit * 4 + 1, limit + 1), 200);
  const nodeSort = sql<number>`coalesce(${knowledgeNodes.sortOrder}, 0)`;
  const placementSort = sql<number>`coalesce(${knowledgeNodePlacements.sortOrder}, 0)`;
  // Keep an invisible legacy blank parent in the lazy-load payload when it
  // has meaningful descendants; the client hoists those descendants instead
  // of making the subtree unreachable.
  const nodeVisibleOrBridge = sql<boolean>`(
    regexp_replace(trim(${knowledgeNodes.title}), '[[:space:]]+', '', 'g') <> ''
    OR EXISTS (
      WITH RECURSIVE blank_descendants AS (
        SELECT id, parent_id, title, archived_at, docs_library_id,
               ARRAY[id]::uuid[] AS visited_path, 0 AS depth
        FROM knowledge_nodes
        WHERE parent_id = ${knowledgeNodes.id}
          AND docs_library_id = ${access.workspace.id}
        UNION ALL
        SELECT child.id, child.parent_id, child.title, child.archived_at,
               child.docs_library_id,
               ancestor.visited_path || ARRAY[child.id]::uuid[], ancestor.depth + 1
        FROM knowledge_nodes AS child
        INNER JOIN blank_descendants AS ancestor ON child.parent_id = ancestor.id
        WHERE child.docs_library_id = ${access.workspace.id}
          AND ancestor.depth < 512
          AND NOT child.id = ANY(ancestor.visited_path)
      )
      SELECT 1 FROM blank_descendants
      WHERE archived_at IS NULL
        AND regexp_replace(trim(title), '[[:space:]]+', '', 'g') <> ''
    )
  )`;
  const notLegacyEmailBlank = sql<boolean>`NOT (
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
  )`;
  const nodeCursorCondition = cursor
    ? or(
        gt(nodeSort, cursor.sortOrder),
        and(eq(nodeSort, cursor.sortOrder), gt(knowledgeNodes.id, cursor.itemId)),
      )
    : undefined;
  const placementCursorCondition = cursor
    ? or(
        gt(placementSort, cursor.sortOrder),
        and(eq(placementSort, cursor.sortOrder), gt(knowledgeNodePlacements.id, cursor.itemId)),
      )
    : undefined;

  const [directRows, placementRows] = await Promise.all([
    db
      .select()
      .from(knowledgeNodes)
      .where(and(
        eq(knowledgeNodes.docsLibraryId, access.workspace.id),
        eq(knowledgeNodes.parentId, id),
        isNull(knowledgeNodes.archivedAt),
        nodeVisibleOrBridge,
        notLegacyEmailBlank,
        nodeCursorCondition,
      ))
      .orderBy(asc(nodeSort), asc(knowledgeNodes.id))
      .limit(candidateLimit),
    db
      .select({ placement: knowledgeNodePlacements, node: knowledgeNodes })
      .from(knowledgeNodePlacements)
      .innerJoin(knowledgeNodes, eq(knowledgeNodePlacements.nodeId, knowledgeNodes.id))
      .where(and(
        eq(knowledgeNodePlacements.parentNodeId, id),
        eq(knowledgeNodes.docsLibraryId, access.workspace.id),
        isNull(knowledgeNodes.archivedAt),
        nodeVisibleOrBridge,
        notLegacyEmailBlank,
        placementCursorCondition,
      ))
      .orderBy(asc(placementSort), asc(knowledgeNodePlacements.id))
      .limit(candidateLimit),
  ]);

  const [directAccessRows, placementAccessRows] = await Promise.all([
    Promise.all(directRows.map((node) => getDocsNodeAccess(node.id, user))),
    Promise.all(placementRows.map(({ node }) => getDocsNodeAccess(node.id, user))),
  ]);
  const visibleDirectIds = new Set(
    directAccessRows
      .filter((item): item is NonNullable<typeof item> => Boolean(item))
      .map((item) => item.node.id),
  );
  const visiblePlacementIds = new Set(
    placementAccessRows
      .filter((item): item is NonNullable<typeof item> => Boolean(item))
      .map((item) => item.node.id),
  );
  const permissionByNodeId = new Map(
    [...directAccessRows, ...placementAccessRows]
      .filter((item): item is NonNullable<typeof item> => Boolean(item))
      .map((item) => [item.node.id, item.permission]),
  );
  const items: ChildItem[] = [
    ...directRows.filter((node) => visibleDirectIds.has(node.id)).map((node) => ({
      itemId: node.id,
      sortOrder: node.sortOrder ?? 0,
      node,
      placement: null,
    })),
    ...placementRows.filter(({ node }) => visiblePlacementIds.has(node.id)).map(({ node, placement }) => ({
      itemId: placement.id,
      sortOrder: placement.sortOrder ?? 0,
      node,
      placement,
    })),
  ].sort((a, b) => a.sortOrder - b.sortOrder || a.itemId.localeCompare(b.itemId));
  const selected = items.slice(0, limit);
  const hasMore = items.length > limit || directRows.length >= candidateLimit || placementRows.length >= candidateLimit;
  const last = selected.at(-1);
  const nextCursor = hasMore && last
    ? encodeDocsChildrenCursor({ sortOrder: last.sortOrder, itemId: last.itemId })
    : null;
  const nodes = Array.from(new Map(selected.map((item) => [item.node.id, item.node])).values());
  const nodeIds = nodes.map((node) => node.id);
  const [nodeSupertags, childMetadata, fieldValueRows, attachmentRows] = await Promise.all([
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
    getKnowledgeNodeChildMetadata(
      access.workspace.id,
      nodeIds,
      null,
      false,
      user,
    ),
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
      ? db.select({ nodeId: knowledgeAttachments.nodeId }).from(knowledgeAttachments).where(inArray(knowledgeAttachments.nodeId, nodeIds))
      : Promise.resolve([]),
  ]);
  const targetIds = Array.from(new Set(
    fieldValueRows
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
  const visibleFieldValueRows = fieldValueRows.filter(
    (value) => !value.targetNodeId || targetAccessById.has(value.targetNodeId),
  );
  const serializedNodes = nodes.map((node) => {
    const full = serializeNode(node);
    const withPermission = { ...full, permission: permissionByNodeId.get(node.id) };
    return { full: withPermission, outline: serializeOutlineNode(withPermission) };
  });
  const hasDetailsIds = Array.from(new Set([
    ...serializedNodes.filter(({ full }) => hasDeferredDetails(full)).map(({ full }) => full.id),
    ...visibleFieldValueRows.map((row) => row.nodeId),
    ...attachmentRows.map((row) => row.nodeId),
  ]));
  const childCountByParent = childMetadata.childCountByParent;

  return jsonWithConditional(request, {
    parent_node_id: id,
    nodes: serializedNodes.map(({ outline }) => outline),
    node_supertags: nodeSupertags.map(serializeNodeSupertag),
    field_values: visibleFieldValueRows.map(serializeFieldValue),
    placements: selected.flatMap((item) => item.placement ? [serializeNodePlacement(item.placement)] : []),
    has_children_ids: childMetadata.hasChildrenIds,
    child_count_by_parent: childCountByParent,
    has_details_ids: hasDetailsIds,
    loaded_children_parent_ids: rawCursor ? [] : [id],
    children_next_cursor_by_parent: { [id]: nextCursor },
    next_cursor: nextCursor,
  });
}
