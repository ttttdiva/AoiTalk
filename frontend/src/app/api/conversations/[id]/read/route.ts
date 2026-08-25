import { NextRequest, NextResponse } from "next/server";
import { db } from "@/db";
import { conversationSessions } from "@/db/schema";
import { and, eq, isNull } from "drizzle-orm";
import { getSession } from "@/lib/auth";
import {
  canManageConversationSession,
  getLiveConversationSession,
} from "@/lib/server/conversation-route-utils";

export async function POST(
  _request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const { id } = await params;
  const session = await getLiveConversationSession(id, user.id);
  if (!session) {
    return NextResponse.json(
      { detail: "セッションが見つかりません" },
      { status: 404 },
    );
  }
  if (!(await canManageConversationSession(id, user))) {
    return NextResponse.json(
      { detail: "会話の既読状態を変更する権限がありません" },
      { status: 403 },
    );
  }

  const lastReadAt = new Date();
  const [updated] = await db
    .update(conversationSessions)
    .set({ lastReadAt })
    .where(
      and(
        eq(conversationSessions.id, id),
        isNull(conversationSessions.deletedAt),
      ),
    )
    .returning({ id: conversationSessions.id });

  if (!updated) {
    return NextResponse.json(
      { detail: "セッションが見つかりません" },
      { status: 404 },
    );
  }

  return NextResponse.json({
    success: true,
    session_id: id,
    last_read_at: lastReadAt.toISOString(),
  });
}
