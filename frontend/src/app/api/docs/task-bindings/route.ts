import { NextResponse } from "next/server";
import { and, inArray, isNull } from "drizzle-orm";
import { db } from "@/db";
import { knowledgeNodes, tasks } from "@/db/schema";
import { getSession } from "@/lib/auth";
import { getAccessibleProject } from "@/lib/server/project-access";
import {
  ensureDocsWorkspace,
  getDocsLibraryIdsForReadableProjects,
  getDocsNodeAccessMap,
} from "@/lib/server/knowledge-docs-utils";

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
  const workspaceIds = await getDocsLibraryIdsForReadableProjects(user.id, [workspace.id]);
  const accessibleNodes = await db
    .select({ id: knowledgeNodes.id })
    .from(knowledgeNodes)
    .where(and(inArray(knowledgeNodes.docsLibraryId, workspaceIds), inArray(knowledgeNodes.id, nodeIds), isNull(knowledgeNodes.archivedAt)));
  const accessMap = await getDocsNodeAccessMap(
    accessibleNodes.map((node) => node.id),
    user,
  );
  const accessibleNodeIds = accessibleNodes
    .filter((node) => accessMap.has(node.id))
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

  const nodeAccessById = new Map(
    Array.from(accessMap.entries()).map(([nodeId, nodeAccess]) => [nodeId, nodeAccess]),
  );
  const identityRows = rows.filter((task) => {
    if (!task.knowledgeNodeId) return false;
    const nodeAccess = nodeAccessById.get(task.knowledgeNodeId);
    return Boolean(nodeAccess && task.projectId === nodeAccess.node.projectId);
  });
  const projectIds = Array.from(new Set(
    identityRows
      .map((task) => task.projectId)
      .filter((projectId): projectId is string => Boolean(projectId)),
  ));
  const projectAccessRows = await Promise.all(
    projectIds.map(async (projectId) => [projectId, await getAccessibleProject(projectId, user.id)] as const),
  );
  const projectAccessById = new Map(projectAccessRows);
  const visibleRows = identityRows.filter((task) => {
    if (!task.knowledgeNodeId) return false;
    const nodeAccess = nodeAccessById.get(task.knowledgeNodeId);
    if (!nodeAccess) return false;
    if (task.projectId) return Boolean(projectAccessById.get(task.projectId));
    return (
      nodeAccess.node.projectId === null
      && nodeAccess.workspace.libraryType === "personal"
      && nodeAccess.workspace.ownerUserId === user.id
    );
  });
  const byNodeId = new Map(visibleRows.map((task) => [task.knowledgeNodeId as string, task]));
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
