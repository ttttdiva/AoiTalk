import { and, eq, isNull, sql } from "drizzle-orm";
import { NextRequest, NextResponse } from "next/server";
import { db } from "@/db";
import { knowledgeNodeShares, knowledgeNodes } from "@/db/schema";
import { docsLibraries } from "@/lib/server/docs-library-schema";
import { getSession } from "@/lib/auth";
import {
  getDocsNodeAccess,
  serializeNodeWithoutBody,
  serializeWorkspace,
} from "@/lib/server/knowledge-docs-utils";

function sharedNodeVisibleCondition() {
  // Shared-node projections must obey the same nonblank contract as the
  // workspace/bootstrap projections.  Legacy mail imports additionally used
  // the literal （空行） marker; suppress it only when an email ancestor is
  // present so an ordinary user-authored title remains visible.
  return sql<boolean>`
    regexp_replace(trim(${knowledgeNodes.title}), '[[:space:]]+', '', 'g') <> ''
    AND NOT (
      ${knowledgeNodes.title} = '（空行）'
      AND EXISTS (
        WITH RECURSIVE email_ancestors AS (
          SELECT id, parent_id, system_key, body_json, docs_library_id,
                 ARRAY[id]::uuid[] AS visited_path, 0 AS depth
          FROM knowledge_nodes
          WHERE id = ${knowledgeNodes.id}
            AND docs_library_id = ${knowledgeNodes.docsLibraryId}
          UNION ALL
          SELECT parent.id, parent.parent_id, parent.system_key, parent.body_json,
                 parent.docs_library_id,
                 child.visited_path || ARRAY[parent.id]::uuid[], child.depth + 1
          FROM knowledge_nodes AS parent
          INNER JOIN email_ancestors AS child ON parent.id = child.parent_id
          WHERE parent.docs_library_id = ${knowledgeNodes.docsLibraryId}
            AND child.depth < 512
            AND NOT parent.id = ANY(child.visited_path)
        )
        SELECT 1
        FROM email_ancestors
        WHERE system_key LIKE 'project_mail:%'
          OR body_json::jsonb ->> 'format' = 'email'
      )
    )`;
}

export async function GET(_request: NextRequest) {
  const user = await getSession();
  if (!user) return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });

  const rows = await db
    .select({
      share: knowledgeNodeShares,
      node: knowledgeNodes,
      workspace: docsLibraries,
    })
    .from(knowledgeNodeShares)
    .innerJoin(knowledgeNodes, eq(knowledgeNodeShares.nodeId, knowledgeNodes.id))
    .innerJoin(docsLibraries, eq(knowledgeNodes.docsLibraryId, docsLibraries.id))
    .where(
      and(
        eq(knowledgeNodeShares.userId, user.id),
        eq(docsLibraries.libraryType, "personal"),
        isNull(knowledgeNodes.archivedAt),
        sharedNodeVisibleCondition(),
      ),
    );
  if (rows.length === 0) return NextResponse.json({ nodes: [], shared_nodes: [] });

  const rowsByWorkspace = new Map<string, typeof rows>();
  for (const row of rows) {
    const bucket = rowsByWorkspace.get(row.workspace.id) ?? [];
    bucket.push(row);
    rowsByWorkspace.set(row.workspace.id, bucket);
  }
  const result: Array<Record<string, unknown>> = [];
  for (const [docsLibraryId, sharedRows] of rowsByWorkspace) {
    const allNodes = await db
      .select({ id: knowledgeNodes.id, parentId: knowledgeNodes.parentId })
      .from(knowledgeNodes)
      .where(eq(knowledgeNodes.docsLibraryId, docsLibraryId));
    const parentById = new Map(allNodes.map((node) => [node.id, node.parentId]));
    // A share row is only a hint that a node may be visible.  Resolve the
    // effective ACL for every candidate before collapsing nested roots.  This
    // is important for personal nodes that carry a project_id: a revoked
    // project membership must revoke the inherited personal share as well.
    const allowedRows = (
      await Promise.all(
        sharedRows.map(async (row) => ({
          row,
          access: await getDocsNodeAccess(row.node.id, user),
        })),
      )
    ).filter(
      (
        item,
      ): item is {
        row: (typeof sharedRows)[number];
        access: NonNullable<Awaited<ReturnType<typeof getDocsNodeAccess>>>;
      } => Boolean(item.access),
    );
    const sharedIds = new Set(allowedRows.map(({ row }) => row.node.id));
    for (const { row, access } of allowedRows) {
      let parentId = parentById.get(row.node.id) ?? null;
      let nested = false;
      const visited = new Set<string>();
      while (parentId && !visited.has(parentId)) {
        visited.add(parentId);
        if (sharedIds.has(parentId)) {
          nested = true;
          break;
        }
        parentId = parentById.get(parentId) ?? null;
      }
      if (nested) continue;
      result.push({
        ...serializeNodeWithoutBody(row.node),
        permission: access.permission,
        share_id: row.share.id,
        library: serializeWorkspace(row.workspace),
      });
    }
  }
  result.sort((left, right) => String(left.title ?? "").localeCompare(String(right.title ?? ""), "ja"));
  return NextResponse.json({ nodes: result, shared_nodes: result });
}
