import { NextResponse } from "next/server";
import { and, eq, inArray, isNull } from "drizzle-orm";
import { db } from "@/db";
import { knowledgeNodes, tasks } from "@/db/schema";
import { getSession } from "@/lib/auth";
import { ensureDocsWorkspace } from "@/lib/server/knowledge-docs-utils";

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export async function POST(request: Request) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const body = await request.json().catch(() => ({})) as Record<string, unknown>;
  const nodeIds = Array.isArray(body?.node_ids)
    ? Array.from(new Set(body.node_ids.filter((value: unknown): value is string => typeof value === "string" && UUID_RE.test(value)))).slice(0, 300)
    : [];
  if (nodeIds.length === 0) {
    return NextResponse.json({ bindings: [] });
  }

  const workspace = await ensureDocsWorkspace(user);
  const accessibleNodes = await db
    .select({ id: knowledgeNodes.id })
    .from(knowledgeNodes)
    .where(and(eq(knowledgeNodes.workspaceId, workspace.id), inArray(knowledgeNodes.id, nodeIds), isNull(knowledgeNodes.archivedAt)));
  const accessibleNodeIds = accessibleNodes
    .filter((node) => nodeIds.includes(node.id))
    .map((node) => node.id);

  if (accessibleNodeIds.length === 0 || !workspace.id) {
    return NextResponse.json({ bindings: nodeIds.map((nodeId) => ({ node_id: nodeId, task: null })) });
  }

  const rows = await db
    .select({
      id: tasks.id,
      projectId: tasks.projectId,
      knowledgeNodeId: tasks.knowledgeNodeId,
      title: tasks.title,
      status: tasks.status,
    })
    .from(tasks)
    .where(and(inArray(tasks.knowledgeNodeId, accessibleNodeIds), isNull(tasks.deletedAt), isNull(tasks.archivedAt)));

  const byNodeId = new Map(rows.filter((task) => task.knowledgeNodeId).map((task) => [task.knowledgeNodeId as string, task]));
  return NextResponse.json({
    bindings: nodeIds.map((nodeId) => {
      const task = byNodeId.get(nodeId) ?? null;
      return {
        node_id: nodeId,
        task: task
          ? {
              id: task.id,
              project_id: task.projectId,
              knowledge_node_id: task.knowledgeNodeId,
              title: task.title,
              status: task.status,
            }
          : null,
      };
    }),
  });
}
