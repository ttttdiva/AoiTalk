import { NextRequest, NextResponse } from "next/server";
import { db } from "@/db";
import { conversationSessions } from "@/db/schema";
import { and, eq, isNull, sql } from "drizzle-orm";
import { getSession } from "@/lib/auth";
import {
  canManageConversationSession,
  getLiveConversationSession,
} from "@/lib/server/conversation-route-utils";

export async function POST(
  _request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  // Capture the read fence at request entry.  Capturing it after the ACL
  // awaits can move the fence past an assistant completion that happened
  // while this request was authorizing, hiding a genuinely new response.
  const readFence = new Date();
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

  const [updated] = await db
    .update(conversationSessions)
    .set({
      // Concurrent read requests may complete out of order.  Keep the durable
      // read fence monotonic so a slower request cannot reopen an older fence.
      // postgres-js rejects a raw Date inside a sql template (500 on every
      // read).  Bind the same UTC ISO text Drizzle uses for this
      // timestamp-without-time-zone column and cast it explicitly.
      lastReadAt: sql`greatest(coalesce(${conversationSessions.lastReadAt}, timestamp '1970-01-01'), ${readFence.toISOString()}::timestamp)`,
    })
    .where(
      and(
        eq(conversationSessions.id, id),
        isNull(conversationSessions.deletedAt),
      ),
    )
    .returning({
      id: conversationSessions.id,
      lastReadAt: conversationSessions.lastReadAt,
    });

  if (!updated) {
    return NextResponse.json(
      { detail: "セッションが見つかりません" },
      { status: 404 },
    );
  }

  return NextResponse.json({
    success: true,
    session_id: id,
    last_read_at: (updated.lastReadAt ?? readFence).toISOString(),
  });
}
