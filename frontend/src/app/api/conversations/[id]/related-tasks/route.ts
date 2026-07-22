import { NextRequest, NextResponse } from "next/server";
import { and, desc, eq, inArray, isNull } from "drizzle-orm";
import { db } from "@/db";
import {
  conversationParticipants,
  conversationSessions,
  projects,
  taskReferences,
  tasks,
} from "@/db/schema";
import { getSession } from "@/lib/auth";
import { getProjectMembership } from "@/lib/server/task-route-utils";

export async function GET(
  _request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await getSession();
  if (!user) return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  const { id } = await params;
  const [participant] = await db
    .select({ sessionId: conversationParticipants.sessionId })
    .from(conversationParticipants)
    .where(and(
      eq(conversationParticipants.sessionId, id),
      eq(conversationParticipants.participantType, "user"),
      eq(conversationParticipants.participantId, user.id),
      eq(conversationParticipants.status, "joined"),
    ))
    .limit(1);
  const [session] = await db
    .select({ id: conversationSessions.id })
    .from(conversationSessions)
    .where(and(
      eq(conversationSessions.id, id),
      isNull(conversationSessions.deletedAt),
      participant ? eq(conversationSessions.id, participant.sessionId) : eq(conversationSessions.userId, user.id),
    ))
    .limit(1);
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
    if (user.role !== "admin" && !(await getProjectMembership(user.id, row.task.projectId))) continue;
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
