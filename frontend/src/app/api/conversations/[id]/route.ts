import { NextRequest, NextResponse } from "next/server";
import { db } from "@/db";
import { conversationSessions } from "@/db/schema";
import { and, eq, isNull } from "drizzle-orm";
import { getSession } from "@/lib/auth";
import { getLiveConversationSession } from "@/lib/server/conversation-route-utils";

export async function PATCH(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const { id } = await params;
  const body = await request.json();
  const session = await getLiveConversationSession(id, user.id);
  if (!session) {
    return NextResponse.json(
      { detail: "セッションが見つかりません" },
      { status: 404 }
    );
  }

  const updates: Record<string, unknown> = {};
  if (typeof body.title === "string") updates.title = body.title;

  if (Object.keys(updates).length === 0) {
    return NextResponse.json({ detail: "更新するフィールドがありません" }, { status: 400 });
  }

  const [updated] = await db
    .update(conversationSessions)
    .set(updates)
    .where(
      and(
        eq(conversationSessions.id, id),
        isNull(conversationSessions.deletedAt),
      ),
    )
    .returning();

  if (!updated) {
    return NextResponse.json(
      { detail: "セッションが見つかりません" },
      { status: 404 }
    );
  }

  return NextResponse.json({ success: true });
}

export async function DELETE(
  _request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
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
      { status: 404 }
    );
  }

  const [updated] = await db
    .update(conversationSessions)
    .set({ deletedAt: new Date() })
    .where(
      and(
        eq(conversationSessions.id, id),
        isNull(conversationSessions.deletedAt),
      ),
    )
    .returning();

  if (!updated) {
    return NextResponse.json(
      { detail: "セッションが見つかりません" },
      { status: 404 }
    );
  }

  return NextResponse.json({ success: true });
}
