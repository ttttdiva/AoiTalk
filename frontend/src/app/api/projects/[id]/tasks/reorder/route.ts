import { NextRequest, NextResponse } from "next/server";
import { db } from "@/db";
import { tasks } from "@/db/schema";
import { and, eq, inArray, isNull, sql } from "drizzle-orm";
import { getSession } from "@/lib/auth";
import { getWritableProject } from "@/lib/server/project-access";

export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
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

  const body = await request.json();
  const { task_ids } = body;

  if (!task_ids || !Array.isArray(task_ids)) {
    return NextResponse.json(
      { detail: "task_idsは配列で指定してください" },
      { status: 400 }
    );
  }

  const taskIds = task_ids.filter(
    (taskId, index): taskId is string =>
      typeof taskId === "string" && task_ids.indexOf(taskId) === index,
  );
  if (taskIds.length === 0) {
    return NextResponse.json({ success: true });
  }

  const topLevelRows = await db
    .select({ id: tasks.id })
    .from(tasks)
    .where(and(eq(tasks.projectId, projectId), isNull(tasks.parentTaskId)));
  const topLevelIds = new Set(topLevelRows.map((task) => task.id));
  const requestedIds = new Set(taskIds);

  if (requestedIds.size !== topLevelIds.size) {
    return NextResponse.json(
      { detail: "task_ids must include every top-level task in the project" },
      { status: 409 },
    );
  }
  for (const taskId of taskIds) {
    if (!topLevelIds.has(taskId)) {
      return NextResponse.json(
        { detail: "task_ids contains a task outside the top-level project scope" },
        { status: 400 },
      );
    }
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
        isNull(tasks.parentTaskId),
        inArray(tasks.id, taskIds),
      ),
    );

  return NextResponse.json({ success: true });
}
