import { NextRequest, NextResponse } from "next/server";
import { and, eq, isNull } from "drizzle-orm";
import { db } from "@/db";
import { taskReferences, tasks } from "@/db/schema";
import { getSession } from "@/lib/auth";
import { canWriteMembership, getProjectMembership } from "@/lib/server/task-route-utils";

export async function DELETE(
  request: NextRequest,
  { params }: { params: Promise<{ id: string; referenceId: string }> },
) {
  const user = await getSession();
  if (!user) return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  const { id, referenceId } = await params;
  const [task] = await db.select().from(tasks).where(and(eq(tasks.id, id), isNull(tasks.deletedAt))).limit(1);
  if (!task) return NextResponse.json({ detail: "タスクが見つかりません" }, { status: 404 });
  const membership = await getProjectMembership(user.id, task.projectId);
  if (!canWriteMembership(user, membership)) return NextResponse.json({ detail: "権限がありません" }, { status: 403 });

  if (referenceId.startsWith("knowledge-node:")) {
    await db.update(tasks).set({ knowledgeNodeId: null }).where(eq(tasks.id, id));
    return new NextResponse(null, { status: 204 });
  }
  const [row] = await db.select().from(taskReferences).where(and(eq(taskReferences.id, referenceId), eq(taskReferences.taskId, id))).limit(1);
  if (!row) return NextResponse.json({ detail: "参照が見つかりません" }, { status: 404 });
  if (row.relationType === "source" && request.nextUrl.searchParams.get("confirm_source") !== "true") {
    return NextResponse.json({ detail: "作成元参照の解除には確認が必要です", requires_confirmation: true }, { status: 409 });
  }
  await db.delete(taskReferences).where(and(eq(taskReferences.id, referenceId), eq(taskReferences.taskId, id)));
  return new NextResponse(null, { status: 204 });
}
