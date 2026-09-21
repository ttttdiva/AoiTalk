import { NextRequest, NextResponse } from "next/server";
import { and, asc, eq, gt, inArray, isNull, ne, or, sql } from "drizzle-orm";
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
  docsNodeVisibleOrBridge,
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
    ["format", "block_type", "checked", "blank"]
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
  const nodeVisibleOrBridge = docsNodeVisibleOrBridge(access.workspace.id);
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
  type StreamCursor = { sortOrder: number; itemId: string } | null;
  const nodeCursorCondition = (after: StreamCursor) => after
    ? or(
        gt(nodeSort, after.sortOrder),
        and(eq(nodeSort, after.sortOrder), gt(knowledgeNodes.id, after.itemId)),
      )
    : undefined;
  const placementCursorCondition = (after: StreamCursor) => after
    ? or(
        gt(placementSort, after.sortOrder),
        and(eq(placementSort, after.sortOrder), gt(knowledgeNodePlacements.id, after.itemId)),
      )
    : undefined;
  const fetchDirectRows = (after: StreamCursor) => db
    .select()
    .from(knowledgeNodes)
    .where(and(
      eq(knowledgeNodes.docsLibraryId, access.workspace.id),
      eq(knowledgeNodes.parentId, id),
      isNull(knowledgeNodes.archivedAt),
      nodeVisibleOrBridge,
      notLegacyEmailBlank,
      nodeCursorCondition(after),
    ))
    .orderBy(asc(nodeSort), asc(knowledgeNodes.id))
    .limit(candidateLimit);
  const fetchPlacementRows = (after: StreamCursor) => db
    .select({ placement: knowledgeNodePlacements, node: knowledgeNodes })
    .from(knowledgeNodePlacements)
    .innerJoin(knowledgeNodes, eq(knowledgeNodePlacements.nodeId, knowledgeNodes.id))
    .where(and(
      eq(knowledgeNodePlacements.parentNodeId, id),
      eq(knowledgeNodes.docsLibraryId, access.workspace.id),
      // A structural child is already represented by the direct stream.  Do
      // not emit its placement twin: because the two streams have independent
      // cursors, allowing both can duplicate one node across page boundaries.
      or(isNull(knowledgeNodes.parentId), ne(knowledgeNodes.parentId, id)),
      isNull(knowledgeNodes.archivedAt),
      nodeVisibleOrBridge,
      notLegacyEmailBlank,
      placementCursorCondition(after),
    ))
    .orderBy(asc(placementSort), asc(knowledgeNodePlacements.id))
    .limit(candidateLimit);

  // ACL is intentionally resolved after the broad candidate query because a
  // shared personal subtree can be visible even when its project_id is not.
  // Do not let an inaccessible prefix consume the only page: stream both
  // ordered sources until `limit` visible items (or both sources' ends) are
  // reached.  The cursor advances over the last *examined* raw item, so the
  // next request cannot loop over an inaccessible prefix.
  let directRows = await fetchDirectRows(cursor);
  let placementRows = await fetchPlacementRows(cursor);
  let directExhausted = directRows.length < candidateLimit;
  let placementExhausted = placementRows.length < candidateLimit;
  let directAfter: StreamCursor = directRows.length > 0
    ? { sortOrder: directRows.at(-1)?.sortOrder ?? 0, itemId: directRows.at(-1)?.id ?? "" }
    : cursor;
  let placementAfter: StreamCursor = placementRows.length > 0
    ? { sortOrder: placementRows.at(-1)?.placement.sortOrder ?? 0, itemId: placementRows.at(-1)?.placement.id ?? "" }
    : cursor;
  const accessByNodeId = new Map<string, Awaited<ReturnType<typeof getDocsNodeAccess>>>();
  const permissionByNodeId = new Map<string, "owner" | "read" | "write">();
  const selected: ChildItem[] = [];
  const selectedNodeIds = new Set<string>();
  let lastExamined: StreamCursor = null;

  while (selected.length < limit) {
    if (directRows.length === 0 && !directExhausted) {
      directRows = await fetchDirectRows(directAfter);
      directExhausted = directRows.length < candidateLimit;
      if (directRows.length > 0) {
        const tail = directRows.at(-1)!;
        directAfter = { sortOrder: tail.sortOrder ?? 0, itemId: tail.id };
      }
    }
    if (placementRows.length === 0 && !placementExhausted) {
      placementRows = await fetchPlacementRows(placementAfter);
      placementExhausted = placementRows.length < candidateLimit;
      if (placementRows.length > 0) {
        const tail = placementRows.at(-1)!;
        placementAfter = { sortOrder: tail.placement.sortOrder ?? 0, itemId: tail.placement.id };
      }
    }
    if (directRows.length === 0 && placementRows.length === 0) break;

    const directHead = directRows[0];
    const placementHead = placementRows[0];
    const useDirect = Boolean(
      directHead
      && (!placementHead
        || (directHead.sortOrder ?? 0) < (placementHead.placement.sortOrder ?? 0)
        || (directHead.sortOrder ?? 0) === (placementHead.placement.sortOrder ?? 0)
          && directHead.id.localeCompare(placementHead.placement.id) <= 0),
    );
    const item: ChildItem = useDirect
      ? {
          itemId: directHead.id,
          sortOrder: directHead.sortOrder ?? 0,
          node: directHead,
          placement: null,
        }
      : {
          itemId: placementHead!.placement.id,
          sortOrder: placementHead!.placement.sortOrder ?? 0,
          node: placementHead!.node,
          placement: placementHead!.placement,
        };
    if (useDirect) directRows = directRows.slice(1);
    else placementRows = placementRows.slice(1);
    lastExamined = { sortOrder: item.sortOrder, itemId: item.itemId };

    let nodeAccess = accessByNodeId.get(item.node.id);
    if (nodeAccess === undefined) {
      nodeAccess = await getDocsNodeAccess(item.node.id, user);
      accessByNodeId.set(item.node.id, nodeAccess);
    }
    if (!nodeAccess) continue;
    // A node can have both its structural parent and a placement reference
    // under the same requested parent.  Count it once per page; the stream
    // cursor still advances over the duplicate raw item so the next page does
    // not repeat it.
    if (selectedNodeIds.has(item.node.id)) continue;
    permissionByNodeId.set(item.node.id, nodeAccess.permission);
    selectedNodeIds.add(item.node.id);
    selected.push(item);
  }

  const hasMore = directRows.length > 0 || placementRows.length > 0
    || !directExhausted || !placementExhausted;
  const nextCursor = hasMore && lastExamined
    ? encodeDocsChildrenCursor(lastExamined)
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
