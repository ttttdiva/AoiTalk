import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import {
  createTaskDependency,
  listTaskDependencies,
  requireTaskDependencyUuid,
  TaskDependencyServiceError,
} from "@/lib/server/task-dependencies";

function serviceErrorResponse(error: unknown): NextResponse | null {
  if (!(error instanceof TaskDependencyServiceError)) return null;
  return NextResponse.json({ detail: error.message }, { status: error.status });
}

export async function GET(request: NextRequest) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  try {
    const rawProjectId = request.nextUrl.searchParams.get("project_id");
    const rawTaskId = request.nextUrl.searchParams.get("task_id");
    const projectId =
      rawProjectId === null
        ? undefined
        : requireTaskDependencyUuid(rawProjectId, "project_id");
    const taskId =
      rawTaskId === null
        ? undefined
        : requireTaskDependencyUuid(rawTaskId, "task_id");
    const dependencies = await listTaskDependencies(user, {
      projectId,
      taskId,
    });
    return NextResponse.json(dependencies);
  } catch (error) {
    const response = serviceErrorResponse(error);
    if (response) return response;
    console.error("タスク依存関係の取得エラー:", error);
    return NextResponse.json(
      { detail: "タスク依存関係の取得に失敗しました" },
      { status: 500 },
    );
  }
}

export async function POST(request: NextRequest) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  try {
    const body = await request.json().catch(() => null);
    if (!body || typeof body !== "object" || Array.isArray(body)) {
      throw new TaskDependencyServiceError(
        400,
        "JSON形式のリクエスト本文を指定してください",
      );
    }
    const record = body as Record<string, unknown>;
    const taskId = requireTaskDependencyUuid(record.task_id, "task_id");
    const dependsOnTaskId = requireTaskDependencyUuid(
      record.depends_on_task_id,
      "depends_on_task_id",
    );
    const dependency = await createTaskDependency(user, {
      taskId,
      dependsOnTaskId,
    });
    return NextResponse.json(dependency, { status: 201 });
  } catch (error) {
    const response = serviceErrorResponse(error);
    if (response) return response;
    console.error("タスク依存関係の作成エラー:", error);
    return NextResponse.json(
      { detail: "タスク依存関係の作成に失敗しました" },
      { status: 500 },
    );
  }
}
