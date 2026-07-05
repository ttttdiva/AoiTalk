import { NextRequest, NextResponse } from "next/server";
import { and, asc, desc, eq, ilike, inArray, isNotNull, isNull, or } from "drizzle-orm";
import { db } from "@/db";
import { knowledgeNodes, knowledgeSearchIndex } from "@/db/schema";
import { getSession } from "@/lib/auth";
import {
  cleanOptionalString,
  ensureDocsWorkspace,
  ensureProjectReadable,
  serializeNode,
} from "@/lib/server/knowledge-docs-utils";
import { normalizeDocsNodeType } from "@/lib/docs-model";

export async function GET(request: NextRequest) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const workspace = await ensureDocsWorkspace(user);
  const searchParams = request.nextUrl.searchParams;
  const query = cleanOptionalString(searchParams.get("q"), 200);
  const projectId = cleanOptionalString(searchParams.get("project_id"), 80);
  const nodeType = cleanOptionalString(searchParams.get("node_type"), 40);
  const includeArchived = searchParams.get("include_archived") === "1";
  const archivedOnly = searchParams.get("archived_only") === "1";
  const rootsOnly = searchParams.get("roots_only") === "1";
  const limit = Math.min(Math.max(Number(searchParams.get("limit")) || 40, 1), 500);

  if (projectId) {
    const project = await ensureProjectReadable(projectId, user);
    if (!project) {
      return NextResponse.json({ detail: "Projectへの読み取り権限がありません" }, { status: 403 });
    }
  }

  let searchNodeIds: string[] | null = null;
  if (query) {
    const matched = await db
      .select({ nodeId: knowledgeSearchIndex.nodeId })
      .from(knowledgeSearchIndex)
      .where(
        and(
          eq(knowledgeSearchIndex.workspaceId, workspace.id),
          or(
            ilike(knowledgeSearchIndex.titleText, `%${query}%`),
            ilike(knowledgeSearchIndex.bodyTextPlain, `%${query}%`),
          ),
        ),
      )
      .limit(limit);
    searchNodeIds = matched.map((row) => row.nodeId);
    if (searchNodeIds.length === 0) return NextResponse.json({ nodes: [] });
  }

  const conditions = [
    eq(knowledgeNodes.workspaceId, workspace.id),
    archivedOnly ? isNotNull(knowledgeNodes.archivedAt) : includeArchived ? undefined : isNull(knowledgeNodes.archivedAt),
    projectId ? eq(knowledgeNodes.projectId, projectId) : undefined,
    nodeType ? eq(knowledgeNodes.nodeType, normalizeDocsNodeType(nodeType)) : undefined,
    rootsOnly ? isNull(knowledgeNodes.parentId) : undefined,
    searchNodeIds ? inArray(knowledgeNodes.id, searchNodeIds) : undefined,
  ].filter(Boolean);

  const nodes = await db
    .select()
    .from(knowledgeNodes)
    .where(and(...conditions))
    .orderBy(asc(knowledgeNodes.sortOrder), asc(knowledgeNodes.createdAt), asc(knowledgeNodes.id), desc(knowledgeNodes.updatedAt))
    .limit(limit);

  return NextResponse.json({ nodes: nodes.map(serializeNode) });
}
