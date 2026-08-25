import { NextRequest, NextResponse } from "next/server";
import { db } from "@/db";
import { taskComments, tasks } from "@/db/schema";
import { and, eq, isNull } from "drizzle-orm";
import { getSession } from "@/lib/auth";
import { decryptTextIfNeeded, encryptText } from "@/lib/server/field-crypto";
import { canWriteProjectId } from "@/lib/server/task-route-utils";

export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const { id } = await params;
  const body = await request.json();

  const [task] = await db
    .select({ projectId: tasks.projectId })
    .from(tasks)
    .where(and(eq(tasks.id, id), isNull(tasks.deletedAt)))
    .limit(1);
  if (!task) {
    return NextResponse.json({ detail: "タスクが見つかりません" }, { status: 404 });
  }
  if (!(await canWriteProjectId(user, task.projectId))) {
    return NextResponse.json({ detail: "権限がありません" }, { status: 403 });
  }

  if (!body.content) {
    return NextResponse.json(
      { detail: "contentは必須です" },
      { status: 400 }
    );
  }

  const [comment] = await db
    .insert(taskComments)
    .values({
      taskId: id,
      userId: user.id,
      content: encryptText(String(body.content), "task_comments.content"),
    })
    .returning();

  return NextResponse.json({
    id: comment.id,
    task_id: comment.taskId,
    user_id: comment.userId,
    content: decryptTextIfNeeded(comment.content, "task_comments.content"),
    created_at: comment.createdAt,
    updated_at: comment.updatedAt,
  });
}
