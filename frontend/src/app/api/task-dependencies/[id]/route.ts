import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import {
  deleteTaskDependency,
  requireTaskDependencyUuid,
  TaskDependencyServiceError,
} from "@/lib/server/task-dependencies";

export async function DELETE(
  _request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  try {
    const { id: rawId } = await params;
    const id = requireTaskDependencyUuid(rawId, "依存関係ID");
    await deleteTaskDependency(user, id);
    return NextResponse.json({ success: true });
  } catch (error) {
    if (error instanceof TaskDependencyServiceError) {
      return NextResponse.json(
        { detail: error.message },
        { status: error.status },
      );
    }
    console.error("タスク依存関係の削除エラー:", error);
    return NextResponse.json(
      { detail: "タスク依存関係の削除に失敗しました" },
      { status: 500 },
    );
  }
}
