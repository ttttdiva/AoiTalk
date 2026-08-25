import { NextResponse } from "next/server";
import { and, eq, isNull } from "drizzle-orm";
import { db } from "@/db";
import { tasks } from "@/db/schema";
import { getSession } from "@/lib/auth";
import { getAccessibleProject } from "@/lib/server/project-access";
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

  type LinkedTask = NonNullable<typeof task>;
  let visibleTask: LinkedTask | null = task ?? null;
  if (visibleTask) {
    const sameProject = visibleTask.projectId === access.node.projectId;
    if (!sameProject) {
      visibleTask = null;
    } else if (visibleTask.projectId) {
      // A readable Docs node does not imply that its linked Task's Project is
      // readable.  Re-check the Project ACL before exposing task metadata.
      const projectAccess = await getAccessibleProject(visibleTask.projectId, user.id);
      if (!projectAccess) visibleTask = null;
    } else if (
      access.node.projectId
      || access.workspace.libraryType !== "personal"
      || access.workspace.ownerUserId !== user.id
    ) {
      // Project-less task bindings are legacy personal-node data and are only
      // valid for the owner of a Personal Docs Library.
      visibleTask = null;
    }
  }

  return NextResponse.json({
    task: visibleTask
      ? {
          id: visibleTask.id,
          project_id: visibleTask.projectId,
          knowledge_node_id: visibleTask.knowledgeNodeId,
          title: visibleTask.title,
          status: visibleTask.status,
        }
      : null,
  });
}
