import { NextRequest, NextResponse } from "next/server";

import { getSession } from "@/lib/auth";
import {
  deleteSchedulePlacement,
  TaskScheduleServiceError,
  upsertSchedulePlacement,
} from "@/lib/server/task-schedule";

function errorResponse(error: unknown): NextResponse | null {
  if (!(error instanceof TaskScheduleServiceError)) return null;
  return NextResponse.json(
    { detail: error.message, code: error.code },
    { status: error.status },
  );
}

export async function PUT(
  request: NextRequest,
  { params }: { params: Promise<{ id: string; taskId: string }> },
) {
  const user = await getSession();
  if (!user) return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  try {
    const { id, taskId } = await params;
    const body = await request.json().catch(() => null);
    if (!body || typeof body !== "object" || Array.isArray(body)) {
      return NextResponse.json(
        { detail: "JSON形式のリクエスト本文を指定してください" },
        { status: 400 },
      );
    }
    const placement = await upsertSchedulePlacement(
      id,
      taskId,
      user,
      body as Record<string, unknown>,
    );
    return NextResponse.json(placement);
  } catch (error) {
    const response = errorResponse(error);
    if (response) return response;
    console.error("タスクスケジュール配置更新エラー:", error);
    return NextResponse.json(
      { detail: "タスクスケジュール配置の更新に失敗しました" },
      { status: 500 },
    );
  }
}

export async function DELETE(
  _request: NextRequest,
  { params }: { params: Promise<{ id: string; taskId: string }> },
) {
  const user = await getSession();
  if (!user) return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  try {
    const { id, taskId } = await params;
    await deleteSchedulePlacement(id, taskId, user);
    return NextResponse.json({ success: true });
  } catch (error) {
    const response = errorResponse(error);
    if (response) return response;
    console.error("タスクスケジュール配置削除エラー:", error);
    return NextResponse.json(
      { detail: "タスクスケジュール配置の削除に失敗しました" },
      { status: 500 },
    );
  }
}
