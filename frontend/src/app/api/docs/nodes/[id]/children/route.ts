import { NextRequest, NextResponse } from "next/server";
import { and, asc, count, eq, gt, inArray, isNull, or, sql } from "drizzle-orm";
import { db } from "@/db";
import {
  knowledgeFieldValues,
  knowledgeAttachments,
  knowledgeNodes,
  knowledgeNodePlacements,
  knowledgeNodeSupertags,
} from "@/db/schema";
import { getSession } from "@/lib/auth";
import {
  decodeDocsChildrenCursor,
  docsChildPageSize,
  encodeDocsChildrenCursor,
} from "@/lib/docs-children-pagination";
import {
  getKnowledgeNodeChildMetadata,
  getUserProjects,
  requireDocsNode,
  serializeNode,
  serializeNodePlacement,
  serializeNodeSupertag,
} from "@/lib/server/knowledge-docs-utils";

type ChildItem = {
  itemId: string;
  sortOrder: number;
  node: typeof knowledgeNodes.$inferSelect;
  placement: typeof knowledgeNodePlacements.$inferSelect | null;
};

function outlineBodyJson(value: unknown) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  const source = value as Record<string, unknown>;
  return Object.fromEntries(
    ["format", "block_type", "checked"]
      .filter((key) => key in source)
      .map((key) => [key, source[key]]),
  );
}

function hasDeferredDetails(node: ReturnType<typeof serializeNode>) {
  const body = node.body_json;
  return typeof body.verbatim_content === "string" || Boolean(body.bookmark);
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
  const projects = await getUserProjects(user.id);
  const accessibleProjectIds = projects.map((project) => project.id);
  const accessibleNodeCondition = accessibleProjectIds.length > 0
    ? or(isNull(knowledgeNodes.projectId), inArray(knowledgeNodes.projectId, accessibleProjectIds))
    : isNull(knowledgeNodes.projectId);
  const nodeSort = sql<number>`coalesce(${knowledgeNodes.sortOrder}, 0)`;
  const placementSort = sql<number>`coalesce(${knowledgeNodePlacements.sortOrder}, 0)`;
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
        eq(knowledgeNodes.workspaceId, access.workspace.id),
        eq(knowledgeNodes.parentId, id),
        isNull(knowledgeNodes.archivedAt),
        accessibleNodeCondition,
        nodeCursorCondition,
      ))
      .orderBy(asc(nodeSort), asc(knowledgeNodes.id))
      .limit(limit + 1),
    db
      .select({ placement: knowledgeNodePlacements, node: knowledgeNodes })
      .from(knowledgeNodePlacements)
      .innerJoin(knowledgeNodes, eq(knowledgeNodePlacements.nodeId, knowledgeNodes.id))
      .where(and(
        eq(knowledgeNodePlacements.parentNodeId, id),
        eq(knowledgeNodes.workspaceId, access.workspace.id),
        isNull(knowledgeNodes.archivedAt),
        accessibleNodeCondition,
        placementCursorCondition,
      ))
      .orderBy(asc(placementSort), asc(knowledgeNodePlacements.id))
      .limit(limit + 1),
  ]);

  const items: ChildItem[] = [
    ...directRows.map((node) => ({
      itemId: node.id,
      sortOrder: node.sortOrder ?? 0,
      node,
      placement: null,
    })),
    ...placementRows.map(({ node, placement }) => ({
      itemId: placement.id,
      sortOrder: placement.sortOrder ?? 0,
      node,
      placement,
    })),
  ].sort((a, b) => a.sortOrder - b.sortOrder || a.itemId.localeCompare(b.itemId));
  const selected = items.slice(0, limit);
  const hasMore = items.length > limit || directRows.length > limit || placementRows.length > limit;
  const last = selected.at(-1);
  const nextCursor = hasMore && last
    ? encodeDocsChildrenCursor({ sortOrder: last.sortOrder, itemId: last.itemId })
    : null;
  const nodes = Array.from(new Map(selected.map((item) => [item.node.id, item.node])).values());
  const nodeIds = nodes.map((node) => node.id);
  const [nodeSupertags, childMetadata, fieldValueRows, attachmentRows, directChildCounts, placementChildCounts] = await Promise.all([
    nodeIds.length
      ? db.select().from(knowledgeNodeSupertags).where(inArray(knowledgeNodeSupertags.nodeId, nodeIds))
      : Promise.resolve([]),
    getKnowledgeNodeChildMetadata(access.workspace.id, nodeIds, accessibleProjectIds),
    nodeIds.length
      ? db.select({ nodeId: knowledgeFieldValues.nodeId }).from(knowledgeFieldValues).where(inArray(knowledgeFieldValues.nodeId, nodeIds))
      : Promise.resolve([]),
    nodeIds.length
      ? db.select({ nodeId: knowledgeAttachments.nodeId }).from(knowledgeAttachments).where(inArray(knowledgeAttachments.nodeId, nodeIds))
      : Promise.resolve([]),
    nodeIds.length
      ? db
          .select({ parentId: knowledgeNodes.parentId, value: count() })
          .from(knowledgeNodes)
          .where(and(
            eq(knowledgeNodes.workspaceId, access.workspace.id),
            inArray(knowledgeNodes.parentId, nodeIds),
            isNull(knowledgeNodes.archivedAt),
            accessibleNodeCondition,
          ))
          .groupBy(knowledgeNodes.parentId)
      : Promise.resolve([]),
    nodeIds.length
      ? db
          .select({ parentId: knowledgeNodePlacements.parentNodeId, value: count() })
          .from(knowledgeNodePlacements)
          .innerJoin(knowledgeNodes, eq(knowledgeNodePlacements.nodeId, knowledgeNodes.id))
          .where(and(
            inArray(knowledgeNodePlacements.parentNodeId, nodeIds),
            eq(knowledgeNodes.workspaceId, access.workspace.id),
            isNull(knowledgeNodes.archivedAt),
            accessibleNodeCondition,
          ))
          .groupBy(knowledgeNodePlacements.parentNodeId)
      : Promise.resolve([]),
  ]);
  const serializedNodes = nodes.map((node) => {
    const full = serializeNode(node);
    return { full, outline: serializeOutlineNode(full) };
  });
  const hasDetailsIds = Array.from(new Set([
    ...serializedNodes.filter(({ full }) => hasDeferredDetails(full)).map(({ full }) => full.id),
    ...fieldValueRows.map((row) => row.nodeId),
    ...attachmentRows.map((row) => row.nodeId),
  ]));
  const childCountByParent: Record<string, number> = {};
  for (const row of [...directChildCounts, ...placementChildCounts]) {
    if (!row.parentId) continue;
    childCountByParent[row.parentId] = (childCountByParent[row.parentId] ?? 0) + Number(row.value);
  }

  return NextResponse.json({
    parent_node_id: id,
    nodes: serializedNodes.map(({ outline }) => outline),
    node_supertags: nodeSupertags.map(serializeNodeSupertag),
    placements: selected.flatMap((item) => item.placement ? [serializeNodePlacement(item.placement)] : []),
    has_children_ids: childMetadata.hasChildrenIds,
    child_count_by_parent: childCountByParent,
    has_details_ids: hasDetailsIds,
    loaded_children_parent_ids: rawCursor ? [] : [id],
    children_next_cursor_by_parent: { [id]: nextCursor },
    next_cursor: nextCursor,
  });
}
