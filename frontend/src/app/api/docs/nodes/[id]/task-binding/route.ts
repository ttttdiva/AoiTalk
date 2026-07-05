import { NextResponse } from "next/server";
import { and, eq, isNull } from "drizzle-orm";
import { db } from "@/db";
import { tasks } from "@/db/schema";
import { getSession } from "@/lib/auth";
import { requireDocsNode } from "@/lib/server/knowledge-docs-utils";

export async function GET(
  _request: Request,
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

  const [task] = await db
    .select({
      id: tasks.id,
      projectId: tasks.projectId,
      knowledgeNodeId: tasks.knowledgeNodeId,
      title: tasks.title,
      status: tasks.status,
    })
    .from(tasks)
    .where(
      and(
        eq(tasks.knowledgeNodeId, access.node.id),
        isNull(tasks.deletedAt),
        isNull(tasks.archivedAt),
      ),
    )
    .limit(1);

  return NextResponse.json({
    task: task
      ? {
          id: task.id,
          project_id: task.projectId,
          knowledge_node_id: task.knowledgeNodeId,
          title: task.title,
          status: task.status,
        }
      : null,
  });
}
