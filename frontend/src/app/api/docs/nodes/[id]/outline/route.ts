import { NextRequest, NextResponse } from "next/server";
import { and, asc, eq, gt, inArray, isNull, or, sql } from "drizzle-orm";
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
  docsNodeVisibleOrBridge,
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

type OutlineCursor = { sortOrder: number; itemId: string };

function decodeOutlineCursor(raw: string | null): OutlineCursor | null {
  if (!raw) return null;
  try {
    const parsed = JSON.parse(Buffer.from(raw, "base64url").toString("utf8")) as Partial<OutlineCursor>;
    if (
      typeof parsed.sortOrder !== "number"
      || !Number.isFinite(parsed.sortOrder)
      || typeof parsed.itemId !== "string"
      || parsed.itemId.length === 0
      || parsed.itemId.length > 200
    ) return null;
    return { sortOrder: parsed.sortOrder, itemId: parsed.itemId };
  } catch {
    return null;
  }
}

function encodeOutlineCursor(cursor: OutlineCursor) {
  return Buffer.from(JSON.stringify(cursor), "utf8").toString("base64url");
}

export async function GET(
  request: NextRequest,
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
  // The outline endpoint is a compatibility projection and historically
  // loaded an entire root subtree.  Bound each response so a large library
  // cannot turn a single focused request into an unbounded metadata query.
  // Clients may request a smaller page; the server cap remains finite.
  const requestedLimit = Number.parseInt(request.nextUrl.searchParams.get("limit") ?? "500", 10);
  const pageLimit = Number.isFinite(requestedLimit)
    ? Math.min(Math.max(requestedLimit, 1), 2000)
    : 500;
  const rawCursor = request.nextUrl.searchParams.get("cursor");
  const cursor = decodeOutlineCursor(rawCursor);
  if (rawCursor && !cursor) {
    return NextResponse.json({ detail: "cursorが不正です" }, { status: 400 });
  }
  const cursorCondition = cursor
    ? or(
        gt(knowledgeNodes.sortOrder, cursor.sortOrder),
        and(
          eq(knowledgeNodes.sortOrder, cursor.sortOrder),
          gt(knowledgeNodes.id, cursor.itemId),
        ),
      )
    : undefined;
  const queriedNodes = await db
    .select()
    .from(knowledgeNodes)
    .where(
      and(
        eq(knowledgeNodes.docsLibraryId, access.workspace.id),
        isNull(knowledgeNodes.archivedAt),
        docsNodeVisibleOrBridge(access.workspace.id),
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
        cursorCondition,
      ),
    )
    .orderBy(asc(knowledgeNodes.sortOrder), asc(knowledgeNodes.id))
    .limit(pageLimit + 1);
  const hasMore = queriedNodes.length > pageLimit;
  const rawNodes = hasMore ? queriedNodes.slice(0, pageLimit) : queriedNodes;
  const tail = rawNodes.at(-1);
  const nextCursor = hasMore && tail
    ? encodeOutlineCursor({ sortOrder: tail.sortOrder ?? 0, itemId: tail.id })
    : null;
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
  // A shared/read Project node must not expose unrelated Personal-library
  // definitions.  The owner keeps the complete library projection; other
  // readers receive only tags/fields actually attached to the visible nodes
  // (and views scoped to those tags).
  const ownerLibrary = access.workspace.ownerUserId === user.id;
  const visibleSupertagIds = new Set(nodeSupertags.map((relation) => relation.supertagId));
  const exposedSupertags = ownerLibrary
    ? supertags
    : supertags.filter((tag) => visibleSupertagIds.has(tag.id));
  const exposedSupertagFields = ownerLibrary
    ? supertagFields
    : supertagFields.filter((relation) => visibleSupertagIds.has(relation.supertagId));
  const attachedFieldIds = new Set(exposedSupertagFields.map((relation) => relation.fieldId));
  const exposedFieldIds = ownerLibrary
    ? new Set(fields.map((field) => field.id))
    : attachedFieldIds;
  const exposedFields = ownerLibrary
    ? fields
    : fields.filter((field) => exposedFieldIds.has(field.id));
  const exposedViews = ownerLibrary
    ? views
    : views.filter((view) => Boolean(view.supertagId && visibleSupertagIds.has(view.supertagId)));
  const taskFieldValues = nodeIds.length
    ? await listDocsTaskSyntheticFieldValues({ nodeIds, fields: exposedFields, user })
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
    (value) => exposedFieldIds.has(value.fieldId)
      && (!value.targetNodeId || targetAccessById.has(value.targetNodeId)),
  );
  const fieldValues = [...visibleStoredFieldValues, ...taskFieldValues];
  const visibleNodeIdSet = new Set(nodeIds);
  const visiblePlacements = placements.filter(
    (placement) => visibleNodeIdSet.has(placement.nodeId)
      && visibleNodeIdSet.has(placement.parentNodeId),
  );

  return NextResponse.json({
    focus_node_id: access.node.id,
    root_page_id: rootPageId,
    has_more: hasMore,
    limit: pageLimit,
    next_cursor: nextCursor,
    nodes: nodes.map(serializeNode),
    supertags: exposedSupertags.map(serializeSupertag),
    supertag_fields: exposedSupertagFields.map(serializeSupertagField),
    placements: visiblePlacements.map(serializeNodePlacement),
    fields: exposedFields.map(serializeField),
    node_supertags: nodeSupertags.map(serializeNodeSupertag),
    field_values: fieldValues.map(serializeFieldValue),
    views: exposedViews.map(serializeView),
    projects,
  });
}
