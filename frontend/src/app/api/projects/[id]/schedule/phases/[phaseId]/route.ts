import { NextRequest, NextResponse } from "next/server";

import { getSession } from "@/lib/auth";
import {
  deleteSchedulePhase,
  TaskScheduleServiceError,
  updateSchedulePhase,
} from "@/lib/server/task-schedule";

function errorResponse(error: unknown): NextResponse | null {
  if (!(error instanceof TaskScheduleServiceError)) return null;
  return NextResponse.json(
    { detail: error.message, code: error.code },
    { status: error.status },
  );
}

async function parseBody(request: NextRequest): Promise<Record<string, unknown>> {
  const body = await request.json().catch(() => null);
  if (!body || typeof body !== "object" || Array.isArray(body)) {
    throw new TaskScheduleServiceError(
      400,
      "JSON形式のリクエスト本文を指定してください",
    );
  }
  return body as Record<string, unknown>;
}

export async function PATCH(
  request: NextRequest,
  { params }: { params: Promise<{ id: string; phaseId: string }> },
) {
  const user = await getSession();
  if (!user) return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  try {
    const { id, phaseId } = await params;
    const phase = await updateSchedulePhase(id, phaseId, user, await parseBody(request));
    return NextResponse.json(phase);
  } catch (error) {
    const response = errorResponse(error);
    if (response) return response;
    console.error("タスクスケジュール工程更新エラー:", error);
    return NextResponse.json(
      { detail: "タスクスケジュール工程の更新に失敗しました" },
      { status: 500 },
    );
  }
}

export async function DELETE(
  _request: NextRequest,
  { params }: { params: Promise<{ id: string; phaseId: string }> },
) {
  const user = await getSession();
  if (!user) return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  try {
    const { id, phaseId } = await params;
    await deleteSchedulePhase(id, phaseId, user);
    return NextResponse.json({ success: true });
  } catch (error) {
    const response = errorResponse(error);
    if (response) return response;
    console.error("タスクスケジュール工程削除エラー:", error);
    return NextResponse.json(
      { detail: "タスクスケジュール工程の削除に失敗しました" },
      { status: 500 },
    );
  }
}
