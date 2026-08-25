import { NextRequest, NextResponse } from "next/server";
import { and, asc, eq, gte, inArray, isNull, lte, or, sql } from "drizzle-orm";
import { alias } from "drizzle-orm/pg-core";
import { db } from "@/db";
import {
  knowledgeFields,
  knowledgeFieldValues,
  knowledgeNodes,
} from "@/db/schema";
import { docsLibraries } from "@/lib/server/docs-library-schema";
import { getSession } from "@/lib/auth";
import { getParticipatingProjectIds } from "@/lib/server/task-route-utils";
import {
  getDocsLibraryIdsForReadableProjects,
  getDocsNodeAccessMap,
  serializeField,
  serializeFieldValue,
  serializeNode,
} from "@/lib/server/knowledge-docs-utils";

function parseDateParam(value: string | null, fallback: Date) {
  if (!value) return fallback;
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? fallback : parsed;
}

export async function GET(request: NextRequest) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const now = new Date();
  const defaultStart = new Date(now);
  defaultStart.setDate(defaultStart.getDate() - 30);
  const defaultEnd = new Date(now);
  defaultEnd.setDate(defaultEnd.getDate() + 90);
  const start = parseDateParam(request.nextUrl.searchParams.get("start"), defaultStart);
  const end = parseDateParam(request.nextUrl.searchParams.get("end"), defaultEnd);
  // Keep the participating-project lookup for compatibility/observability;
  // explicit node shares are merged as additional candidates below and the
  // final ACL resolver remains authoritative.
  await getParticipatingProjectIds(user.id);
  // Calendar reads must be side-effect free.  Do not call ensureDocsWorkspace
  // or ensureProjectDocsWorkspace here: a GET should not create/seed a
  // workspace just because the user opened the calendar.

  // Generic calendar reads are rooted in the actor's own Personal Docs
  // Library. Project identity remains on each node and membership ACL below
  // controls which project-linked nodes are visible; no secondary library is
  // selected from a query parameter.
  const workspaceConditions = [
    and(
      eq(docsLibraries.libraryType, "personal"),
      eq(docsLibraries.ownerUserId, user.id),
    ),
  ];
  const workspaces = await db
    .select({
      id: docsLibraries.id,
    })
    .from(docsLibraries)
    .where(or(...workspaceConditions))
    .orderBy(asc(docsLibraries.createdAt));

  // Explicit node shares are independent of Project membership.  Include
  // their Personal Libraries as calendar candidates, then let the canonical
  // per-node ACL map below perform the final source/target authorization.
  let sharedWorkspaceRows: Array<{ docsLibraryId: string }> = [];
  try {
    // Keep this lookup independent of the Drizzle schema export so rolling
    // test/sync adapters that predate node-share metadata can safely fall
    // back to the actor's own library.
    const result = await db.execute(sql`
      select distinct n.docs_library_id as "docsLibraryId"
      from knowledge_node_shares s
      inner join knowledge_nodes n on n.id = s.node_id
      inner join docs_libraries l on l.id = n.docs_library_id
      where s.user_id = ${user.id}
        and l.library_type = 'personal'
    `);
    sharedWorkspaceRows = (result as unknown as Array<Record<string, unknown>>)
      .map((row) => ({ docsLibraryId: String(row.docsLibraryId ?? row.docs_library_id ?? "") }))
      .filter((row) => Boolean(row.docsLibraryId));
  } catch {
    // Older test/compatibility adapters may not expose the share table.
  }

  const workspaceIds = await getDocsLibraryIdsForReadableProjects(
    user.id,
    [
      ...workspaces.map((workspace) => workspace.id),
      ...sharedWorkspaceRows.map((row) => row.docsLibraryId),
    ],
  );
  // A recipient may have no library of their own while still being a member
  // of a project whose information nodes live in the owner's Personal
  // Library.  The cross-library helper above adds those candidates; only an
  // empty candidate set can short-circuit the query.
  if (workspaceIds.length === 0) {
    return NextResponse.json({ items: [] });
  }
  const targetNodes = alias(knowledgeNodes, "calendar_target_nodes");

  // Project identity lives on each node. A null project_id is an ordinary
  // personal node; project-linked nodes are constrained to participating
  // project IDs and then re-checked through the canonical ACL resolver.
  // Do not reject a shared personal node merely because its stale
  // `project_id` is not in the actor's participating list.  Explicit shares
  // are candidates here; getDocsNodeAccessMap is the final project/share ACL.
  const projectVisibilityCondition = undefined;
  const sourceNotLegacyEmailBlank = sql<boolean>`NOT (
    ${knowledgeNodes.title} = '（空行）'
    AND EXISTS (
      WITH RECURSIVE email_ancestors AS (
        SELECT id, parent_id, system_key, docs_library_id,
               ARRAY[id]::uuid[] AS visited_path, 0 AS depth
        FROM knowledge_nodes
        WHERE id = ${knowledgeNodes.id}
          AND docs_library_id = ${knowledgeNodes.docsLibraryId}
        UNION ALL
        SELECT parent.id, parent.parent_id, parent.system_key,
               parent.docs_library_id,
               child.visited_path || ARRAY[parent.id]::uuid[], child.depth + 1
        FROM knowledge_nodes AS parent
        INNER JOIN email_ancestors AS child ON parent.id = child.parent_id
        WHERE parent.docs_library_id = child.docs_library_id
          AND child.depth < 512
          AND NOT parent.id = ANY(child.visited_path)
      )
      SELECT 1 FROM email_ancestors
      WHERE system_key LIKE 'project_mail:%'
    )
  )`;
  const targetNotLegacyEmailBlank = sql<boolean>`NOT (
    ${targetNodes.title} = '（空行）'
    AND EXISTS (
      WITH RECURSIVE email_ancestors AS (
        SELECT id, parent_id, system_key, docs_library_id,
               ARRAY[id]::uuid[] AS visited_path, 0 AS depth
        FROM knowledge_nodes
        WHERE id = ${targetNodes.id}
          AND docs_library_id = ${targetNodes.docsLibraryId}
        UNION ALL
        SELECT parent.id, parent.parent_id, parent.system_key,
               parent.docs_library_id,
               child.visited_path || ARRAY[parent.id]::uuid[], child.depth + 1
        FROM knowledge_nodes AS parent
        INNER JOIN email_ancestors AS child ON parent.id = child.parent_id
        WHERE parent.docs_library_id = child.docs_library_id
          AND child.depth < 512
          AND NOT parent.id = ANY(child.visited_path)
      )
      SELECT 1 FROM email_ancestors
      WHERE system_key LIKE 'project_mail:%'
    )
  )`;
  const targetProjectVisibilityCondition = undefined;

  type CalendarRow = {
    node: typeof knowledgeNodes.$inferSelect;
    field: typeof knowledgeFields.$inferSelect;
    value: typeof knowledgeFieldValues.$inferSelect;
    targetNode: typeof knowledgeNodes.$inferSelect | null;
  };
  const rows = (await db
    .select({
      node: knowledgeNodes,
      field: knowledgeFields,
      value: knowledgeFieldValues,
      targetNode: targetNodes,
    })
    .from(knowledgeFieldValues)
    .innerJoin(knowledgeFields, eq(knowledgeFieldValues.fieldId, knowledgeFields.id))
    .innerJoin(knowledgeNodes, eq(knowledgeFieldValues.nodeId, knowledgeNodes.id))
    .innerJoin(docsLibraries, eq(knowledgeNodes.docsLibraryId, docsLibraries.id))
    .leftJoin(
      targetNodes,
      and(
        eq(knowledgeFieldValues.targetNodeId, targetNodes.id),
        // A target reference is visible only inside the same already-visible
        // workspace and project scope as its calendar source node.
        eq(targetNodes.docsLibraryId, knowledgeNodes.docsLibraryId),
        isNull(targetNodes.archivedAt),
        sql`nullif(btrim(${targetNodes.title}), '') is not null`,
        targetNotLegacyEmailBlank,
        targetProjectVisibilityCondition,
      ),
    )
    .where(
      and(
        inArray(knowledgeNodes.docsLibraryId, workspaceIds),
        isNull(knowledgeNodes.archivedAt),
        sql`nullif(btrim(${knowledgeNodes.title}), '') is not null`,
        sourceNotLegacyEmailBlank,
        projectVisibilityCondition,
        // Field IDs are globally unique, but malformed/legacy rows can point
        // across workspaces.  Do not expose a foreign field's name/options.
        eq(knowledgeFields.docsLibraryId, knowledgeNodes.docsLibraryId),
        eq(knowledgeFields.fieldType, "date"),
        gte(knowledgeFieldValues.valueDatetime, start),
        lte(knowledgeFieldValues.valueDatetime, end),
      ),
    )
    .limit(500)) as CalendarRow[];

  // SQL candidate predicates intentionally stay broad enough for shared
  // personal subtrees and owner-library project nodes. Re-check both source
  // and target references through the canonical ACL resolver before emitting
  // calendar values; a revoked share or malformed project pointer must not
  // leak through a stale calendar row.
  const aclCandidateIds = rows.flatMap((row) => [
    row.node.id,
    row.targetNode?.id,
  ].filter((value): value is string => Boolean(value)));
  const accessMap = await getDocsNodeAccessMap(aclCandidateIds, user);

  return NextResponse.json({
    items: [
      ...rows
        // Keep a defensive application-level guard in addition to the SQL
        // workspace equality above; it also protects against malformed rows
        // returned by a compatibility database adapter.
        .filter((row) =>
          row.field.docsLibraryId === row.node.docsLibraryId
          && accessMap.has(row.node.id),
        )
        .map((row) => ({
          id: `${row.node.id}:${row.field.id}`,
          node: serializeNode(row.node),
          field: serializeField(row.field),
          value: serializeFieldValue({
            ...row.value,
            targetNodeId:
              row.targetNode && accessMap.has(row.targetNode.id)
                ? row.value.targetNodeId
                : null,
          }),
          start: row.value.valueDatetime,
          end: row.value.valueDatetime,
          all_day: true,
        })),
    ],
  });
}
