import { NextRequest, NextResponse } from "next/server";
import { db } from "@/db";
import { taskComments } from "@/db/schema";
import { getSession } from "@/lib/auth";
import { decryptTextIfNeeded, encryptText } from "@/lib/server/field-crypto";

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
