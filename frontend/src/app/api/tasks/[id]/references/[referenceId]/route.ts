import { NextRequest, NextResponse } from "next/server";
import { and, eq, isNull, or } from "drizzle-orm";
import { db } from "@/db";
import { taskReferences, taskRelations, tasks } from "@/db/schema";
import { getSession } from "@/lib/auth";
import { canWriteProjectId } from "@/lib/server/task-route-utils";
import { parseTaskRelationReferenceId } from "@/lib/server/task-relations";

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export async function DELETE(
  request: NextRequest,
  { params }: { params: Promise<{ id: string; referenceId: string }> },
) {
  const user = await getSession();
  if (!user) return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  const { id, referenceId } = await params;
  const [task] = await db.select().from(tasks).where(and(eq(tasks.id, id), isNull(tasks.deletedAt))).limit(1);
  if (!task) return NextResponse.json({ detail: "タスクが見つかりません" }, { status: 404 });
  if (!(await canWriteProjectId(user, task.projectId))) return NextResponse.json({ detail: "権限がありません" }, { status: 403 });

  if (referenceId.startsWith("knowledge-node:")) {
    await db.update(tasks).set({ knowledgeNodeId: null }).where(eq(tasks.id, id));
    return new NextResponse(null, { status: 204 });
  }
  const relationId = parseTaskRelationReferenceId(referenceId);
  if (relationId) {
    if (!UUID_RE.test(relationId)) {
      return NextResponse.json({ detail: "参照が見つかりません" }, { status: 404 });
    }
    const [relation] = await db
      .select()
      .from(taskRelations)
      .where(
        and(
          eq(taskRelations.id, relationId),
          or(
            eq(taskRelations.taskAId, id),
            eq(taskRelations.taskBId, id),
          ),
        ),
      )
      .limit(1);
    if (!relation) {
      return NextResponse.json({ detail: "参照が見つかりません" }, { status: 404 });
    }
    await db.delete(taskRelations).where(eq(taskRelations.id, relation.id));
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
