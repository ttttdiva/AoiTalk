import { NextResponse } from "next/server";
import { and, eq, isNotNull } from "drizzle-orm";
import { db } from "@/db";
import { conversationSessions } from "@/db/schema";
import { getSession } from "@/lib/auth";
import {
  canManageDeletedWritableConversationSession,
  ConversationScopeError,
  getConversationSessionForUser,
  validateAppConversationScope,
} from "@/lib/server/conversation-route-utils";
import {
  appendContentDeletionEvent,
  createDeletionBatchId,
} from "@/lib/server/content-deletion-events";
import { readDeletionRetentionDays } from "@/lib/server/deletion-retention";

export async function POST(
  _request: Request,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const { id } = await params;
  const session = await getConversationSessionForUser(id, user.id, true);
  if (!session || !session.deletedAt) {
    return NextResponse.json(
      { detail: "復元可能なセッションが見つかりません" },
      { status: 404 },
    );
  }
  if (!(await canManageDeletedWritableConversationSession(id, user))) {
    return NextResponse.json(
      { detail: "会話の復元権限がありません" },
      { status: 403 },
    );
  }

  if (session.appId != null) {
    try {
      await validateAppConversationScope({
        appId: String(session.appId),
        appTargetId: session.appTargetId ? String(session.appTargetId) : null,
        projectId: session.projectId ? String(session.projectId) : null,
        user,
      });
    } catch (error) {
      if (error instanceof ConversationScopeError) {
        return NextResponse.json({ detail: error.message }, { status: error.status });
      }
      throw error;
    }
  }

  const retentionDays = readDeletionRetentionDays();
  const expiresAt = new Date(
    session.deletedAt.getTime() + retentionDays * 24 * 60 * 60 * 1000,
  );
  if (new Date() >= expiresAt) {
    return NextResponse.json(
      { detail: "会話の復元期間が終了しています", expires_at: expiresAt.toISOString() },
      { status: 410 },
    );
  }

  const [restored] = await db
    .update(conversationSessions)
    .set({ deletedAt: null, isActive: true })
    .where(
      and(
        eq(conversationSessions.id, id),
        isNotNull(conversationSessions.deletedAt),
      ),
    )
    .returning();
  if (!restored) {
    return NextResponse.json(
      { detail: "会話の復元に失敗しました" },
      { status: 409 },
    );
  }

  await appendContentDeletionEvent(db, {
    batchId: createDeletionBatchId(),
    entityType: "conversation",
    entityId: id,
    rootEntityId: id,
    projectId: session.projectId ? String(session.projectId) : null,
    actorUserId: user.id,
    action: "restored",
    displayName: session.title ? String(session.title) : null,
    source: "web.conversations.restore",
  });

  return NextResponse.json({
    success: true,
    session: restored,
    expires_at: expiresAt.toISOString(),
  });
}
