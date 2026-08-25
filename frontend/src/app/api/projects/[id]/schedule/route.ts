import { NextRequest, NextResponse } from "next/server";

import { getSession } from "@/lib/auth";
import {
  createSchedulePhase,
  listProjectSchedule,
  TaskScheduleServiceError,
} from "@/lib/server/task-schedule";

function errorResponse(error: unknown): NextResponse | null {
  if (!(error instanceof TaskScheduleServiceError)) return null;
  return NextResponse.json(
    { detail: error.message, code: error.code },
    { status: error.status },
  );
}

export async function GET(
  _request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await getSession();
  if (!user) return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  try {
    const { id } = await params;
    return NextResponse.json(await listProjectSchedule(id, user));
  } catch (error) {
    const response = errorResponse(error);
    if (response) return response;
    console.error("タスクスケジュール取得エラー:", error);
    return NextResponse.json(
      { detail: "タスクスケジュールの取得に失敗しました" },
      { status: 500 },
    );
  }
}

export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await getSession();
  if (!user) return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  try {
    const { id } = await params;
    const body = await request.json().catch(() => null);
    if (!body || typeof body !== "object" || Array.isArray(body)) {
      return NextResponse.json(
        { detail: "JSON形式のリクエスト本文を指定してください" },
        { status: 400 },
      );
    }
    const phase = await createSchedulePhase(id, user, body as Record<string, unknown>);
    return NextResponse.json(phase, { status: 201 });
  } catch (error) {
    const response = errorResponse(error);
    if (response) return response;
    console.error("タスクスケジュール工程作成エラー:", error);
    return NextResponse.json(
      { detail: "タスクスケジュール工程の作成に失敗しました" },
      { status: 500 },
    );
  }
}
