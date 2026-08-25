import { NextRequest, NextResponse } from "next/server";
import { and, desc, eq, inArray, isNull } from "drizzle-orm";
import { db } from "@/db";
import {
  projects,
  taskReferences,
  tasks,
} from "@/db/schema";
import { getSession } from "@/lib/auth";
import {
  canReadProjectId,
} from "@/lib/server/task-route-utils";
import { getLiveConversationSession } from "@/lib/server/conversation-route-utils";

export async function GET(
  _request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await getSession();
  if (!user) return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  const { id } = await params;
  const session = await getLiveConversationSession(id, user.id);
  if (!session) return NextResponse.json({ detail: "会話が見つかりません" }, { status: 404 });

  const refs = await db.select({ taskId: taskReferences.taskId })
    .from(taskReferences)
    .where(and(
      inArray(taskReferences.referenceType, ["conversation_session", "conversation_message"]),
      eq(taskReferences.targetId, id),
    ));
  const taskIds = [...new Set(refs.map((ref) => ref.taskId))];
  if (taskIds.length === 0) return NextResponse.json({ tasks: [] });
  const rows = await db.select({ task: tasks, projectName: projects.name })
    .from(tasks)
    .leftJoin(projects, eq(tasks.projectId, projects.id))
    .where(and(inArray(tasks.id, taskIds), isNull(tasks.deletedAt)))
    .orderBy(desc(tasks.updatedAt));
  const readable = [];
  for (const row of rows) {
    if (!(await canReadProjectId(user, row.task.projectId))) continue;
    readable.push({
      id: row.task.id,
      title: row.task.title,
      status: row.task.status,
      priority: row.task.priority,
      project_id: row.task.projectId,
      project_name: row.projectName,
      updated_at: row.task.updatedAt,
    });
  }
  return NextResponse.json({ tasks: readable });
}
