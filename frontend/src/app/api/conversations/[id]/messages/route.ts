import { NextRequest, NextResponse } from "next/server";
import { db } from "@/db";
import { conversationMessages, conversationSessions } from "@/db/schema";
import { and, asc, eq, isNull, or, sql } from "drizzle-orm";
import { getSession } from "@/lib/auth";
import {
  getLiveConversationSession,
  messageToSnake,
} from "@/lib/server/conversation-route-utils";
import { encryptText } from "@/lib/server/field-crypto";

export const dynamic = "force-dynamic";
export const fetchCache = "force-no-store";

export async function GET(
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

  const rows = await db
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

  return NextResponse.json(
    { messages: rows.map(messageToSnake) },
    { headers: { "Cache-Control": "no-store" } },
  );
}

export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const { id } = await params;
  const body = await request.json().catch(() => null);
  const role = body?.role;
  const content = typeof body?.content === "string" ? body.content : "";

  if (role !== "user" && role !== "assistant") {
    return NextResponse.json(
      { detail: "role は user または assistant を指定してください" },
      { status: 400 },
    );
  }

  if (!content.trim()) {
    return NextResponse.json({ detail: "content は必須です" }, { status: 400 });
  }

  const session = await getLiveConversationSession(id, user.id);
  if (!session) {
    return NextResponse.json(
      { detail: "セッションが見つかりません" },
      { status: 404 },
    );
  }

  const now = new Date();
  const [message] = await db
    .insert(conversationMessages)
    .values({
      sessionId: id,
      role,
      content: encryptText(content, "conversation_messages.content"),
      messageMetadata: {},
      senderType: role === "user" ? "user" : null,
      senderId: role === "user" ? user.id : null,
      senderDisplayName:
        role === "user" ? user.displayName || user.username || user.email || user.id : null,
      createdAt: now,
      branchIndex: 0,
      isActiveBranch: true,
    })
    .returning();

  await db
    .update(conversationSessions)
    .set({
      lastActivity: now,
      messageCount: sql`coalesce(${conversationSessions.messageCount}, 0) + 1`,
    })
    .where(eq(conversationSessions.id, id));

  return NextResponse.json({ success: true, message: messageToSnake(message) });
}
