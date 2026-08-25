import { NextRequest, NextResponse } from "next/server";
import { db } from "@/db";
import { conversationSessions } from "@/db/schema";
import { and, eq, isNull } from "drizzle-orm";
import { getSession } from "@/lib/auth";
import {
  canManageWritableConversationSession,
  ConversationScopeError,
  getLiveConversationSession,
  sessionToSnake,
  validateAppConversationScope,
} from "@/lib/server/conversation-route-utils";
import {
  appendContentDeletionEvent,
  createDeletionBatchId,
} from "@/lib/server/content-deletion-events";

const DEVELOPMENT_STATUSES = [
  "working",
  "waiting_for_user",
  "completed",
] as const;

type DevelopmentStatus = (typeof DEVELOPMENT_STATUSES)[number];

function isDevelopmentStatus(value: unknown): value is DevelopmentStatus {
  return (
    typeof value === "string" &&
    (DEVELOPMENT_STATUSES as readonly string[]).includes(value)
  );
}

async function updateConversation(
  request: NextRequest,
  id: string,
  user: { id: string; role?: string | null },
) {
  const body = await request.json();
  const session = await getLiveConversationSession(id, user.id);
  if (!session) {
    return NextResponse.json(
      { detail: "セッションが見つかりません" },
      { status: 404 },
    );
  }
  if (!(await canManageWritableConversationSession(id, user))) {
    return NextResponse.json(
      { detail: "会話の管理権限がありません" },
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

  const updates: {
    title?: string;
    developmentStatus?: DevelopmentStatus;
  } = {};
  if (typeof body.title === "string") updates.title = body.title;

  if (body.development_status !== undefined) {
    if (session.appId == null) {
      return NextResponse.json(
        { detail: "App開発状態はApp context付きChatでのみ設定できます" },
        { status: 400 },
      );
    }
    if (!isDevelopmentStatus(body.development_status)) {
      return NextResponse.json(
        {
          detail:
            "development_statusはworking、waiting_for_user、completedのいずれかを指定してください",
        },
        { status: 400 },
      );
    }
    updates.developmentStatus = body.development_status;
  }

  if (Object.keys(updates).length === 0) {
    return NextResponse.json(
      { detail: "更新するフィールドがありません" },
      { status: 400 },
    );
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
      { status: 404 },
    );
  }

  if (body.development_status === undefined) {
    return NextResponse.json({ success: true });
  }

  return NextResponse.json({
    success: true,
    session: sessionToSnake(updated as unknown as Record<string, unknown>),
  });
}

export async function PATCH(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const { id } = await params;
  return updateConversation(request, id, user);
}

export async function PUT(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const { id } = await params;
  return updateConversation(request, id, user);
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
  if (!(await canManageWritableConversationSession(id, user))) {
    return NextResponse.json(
      { detail: "会話の削除権限がありません" },
      { status: 403 }
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

  // Keep the event ledger best-effort for deployments that are upgrading from
  // a schema without content_deletion_events.  The session tombstone itself
  // is already committed and must not be reported as a failed delete merely
  // because an optional audit sink is unavailable.
  try {
    await appendContentDeletionEvent(db, {
      batchId: createDeletionBatchId(),
      entityType: "conversation",
      entityId: id,
      rootEntityId: id,
      projectId: session.projectId ? String(session.projectId) : null,
      actorUserId: user.id,
      action: "deleted",
      displayName: session.title ? String(session.title) : null,
      source: "web.conversations.delete",
      eventAt: updated.deletedAt ?? new Date(),
    });
  } catch (error) {
    console.error("会話削除監査イベントの記録に失敗しました:", error);
  }

  return NextResponse.json({ success: true });
}
