import { NextRequest, NextResponse } from "next/server";
import { db } from "@/db";
import { conversationMessages } from "@/db/schema";
import { and, asc, eq, isNull, or } from "drizzle-orm";
import { getSession } from "@/lib/auth";
import {
  messageToSnake,
  resumeConversationSession,
  sessionToSnake,
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

  const session = await resumeConversationSession(id, user.id);

  if (!session) {
    return NextResponse.json(
      { detail: "セッションが見つかりません" },
      { status: 404 },
    );
  }

  const messages = await db
    .select()
    .from(conversationMessages)
    .where(
      and(
        eq(conversationMessages.sessionId, id),
        or(
          eq(conversationMessages.isActiveBranch, true),
          isNull(conversationMessages.isActiveBranch),
        ),
      ),
    )
    .orderBy(asc(conversationMessages.createdAt));

  return NextResponse.json({
    session: sessionToSnake(session as unknown as Record<string, unknown>),
    messages: messages.map(messageToSnake),
  });
}
