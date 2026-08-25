import { NextRequest, NextResponse } from "next/server";
import { db } from "@/db";
import { tasks } from "@/db/schema";
import { and, eq, inArray, isNull, sql } from "drizzle-orm";
import { getSession } from "@/lib/auth";
import { getWritableProject } from "@/lib/server/project-access";
import { normalizeOptionalUuid } from "@/lib/server/task-route-utils";

export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const { id: projectId } = await params;
  const access = await getWritableProject(projectId, user);
  if (!access) {
    return NextResponse.json(
      { detail: "Project not found or not writable" },
      { status: 404 },
    );
  }

  const body = (await request.json()) as Record<string, unknown>;
  const task_ids = body.task_ids;

  if (!task_ids || !Array.isArray(task_ids)) {
    return NextResponse.json(
      { detail: "task_idsは配列で指定してください" },
      { status: 400 },
    );
  }

  const taskIds: string[] = [];
  const seenTaskIds = new Set<string>();
  for (const value of task_ids) {
    const taskId = normalizeOptionalUuid(value);
    if (!taskId) {
      return NextResponse.json(
        { detail: "task_idsはUUID形式で指定してください" },
        { status: 400 },
      );
    }
    if (seenTaskIds.has(taskId)) {
      return NextResponse.json(
        { detail: "task_idsに重複があります" },
        { status: 400 },
      );
    }
    seenTaskIds.add(taskId);
    taskIds.push(taskId);
  }

  const hasParentTaskId = Object.prototype.hasOwnProperty.call(
    body,
    "parent_task_id",
  );
  const parentTaskId =
    body.parent_task_id === null || !hasParentTaskId
      ? null
      : normalizeOptionalUuid(body.parent_task_id);
  if (hasParentTaskId && body.parent_task_id !== null && !parentTaskId) {
    return NextResponse.json(
      { detail: "parent_task_idはUUID形式またはnullで指定してください" },
      { status: 400 },
    );
  }

  // 既存clientの「parent_task_id省略 + 空配列」はno-opとして互換維持する。
  // 明示scopeは空配列でも親と全siblingsの検証を必ず通す。
  if (taskIds.length === 0 && !hasParentTaskId) {
    return NextResponse.json({ success: true });
  }

  if (parentTaskId) {
    const [parentTask] = await db
      .select({
        id: tasks.id,
        projectId: tasks.projectId,
        source: tasks.source,
      })
      .from(tasks)
      .where(
        and(
          eq(tasks.id, parentTaskId),
          eq(tasks.projectId, projectId),
          isNull(tasks.deletedAt),
        ),
      )
      .limit(1);
    if (!parentTask || parentTask.source === "remote") {
      return NextResponse.json(
        { detail: "親タスクが同一プロジェクトの書き込み可能taskではありません" },
        { status: 400 },
      );
    }
  }

  const siblingScope = parentTaskId
    ? eq(tasks.parentTaskId, parentTaskId)
    : isNull(tasks.parentTaskId);
  const siblingRows = await db
    .select({ id: tasks.id, source: tasks.source })
    .from(tasks)
    .where(
      and(
        eq(tasks.projectId, projectId),
        siblingScope,
        isNull(tasks.deletedAt),
      ),
    );
  if (siblingRows.some((task) => task.source === "remote")) {
    return NextResponse.json(
      { detail: "remote taskの順序は変更できません" },
      { status: 400 },
    );
  }
  const siblingIds = new Set(siblingRows.map((task) => task.id));
  const requestedIds = new Set(taskIds);

  if (requestedIds.size !== siblingIds.size) {
    return NextResponse.json(
      { detail: "task_idsは同一親の全タスクを含めてください" },
      { status: 409 },
    );
  }
  for (const taskId of taskIds) {
    if (!siblingIds.has(taskId)) {
      return NextResponse.json(
        { detail: "task_idsに別projectまたは別parentのタスクが含まれています" },
        { status: 400 },
      );
    }
  }
  if (taskIds.length === 0) {
    return NextResponse.json({ success: true });
  }

  const sortOrderCases = taskIds.map(
    (taskId, index) => sql`when ${tasks.id} = ${taskId} then ${index}`,
  );

  await db
    .update(tasks)
    .set({
      sortOrder: sql`case ${sql.join(sortOrderCases, sql.raw(" "))} else ${tasks.sortOrder} end`,
    })
    .where(
      and(
        eq(tasks.projectId, projectId),
        siblingScope,
        isNull(tasks.deletedAt),
        inArray(tasks.id, taskIds),
      ),
    );

  return NextResponse.json({ success: true });
}
