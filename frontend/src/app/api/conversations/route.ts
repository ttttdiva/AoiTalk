import { NextRequest, NextResponse } from "next/server";
import { db } from "@/db";
import {
  conversationMessages,
  conversationParticipants,
  conversationSessions,
} from "@/db/schema";
import { eq, and, inArray, isNull, desc, sql, or, isNotNull } from "drizzle-orm";
import { getSession } from "@/lib/auth";
import { decryptTextIfNeeded, encryptText } from "@/lib/server/field-crypto";
import {
  canReadProject,
  canWriteProject,
  ConversationScopeError,
  messageToSnake,
  validateAppConversationScope,
} from "@/lib/server/conversation-route-utils";
import { cleanupExpiredDeletedConversationsIfDue } from "@/lib/server/conversation-retention-cleanup";
import { fetchPythonApi, type InternalPythonUser } from "@/lib/server/python-api-proxy";
import { jsonWithConditional } from "@/lib/server/http-cache";
import { getReadableProjectIds } from "@/lib/server/task-route-utils";

class CharacterResolutionError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "CharacterResolutionError";
  }
}

async function resolveCharacterSlug(
  requestedName: string,
  user: InternalPythonUser,
): Promise<string | null> {
  const normalized = requestedName.trim();
  if (!normalized) return null;
  if (
    normalized.startsWith("scenario_roleplay:") ||
    normalized.startsWith("scenario_") ||
    normalized.startsWith("trpg_room_")
  ) {
    return normalized;
  }

  let response: Response;
  try {
    response = await fetchPythonApi(
      "/api/characters/manage?enabled_only=true",
      { method: "GET", user },
    );
  } catch (error) {
    throw new CharacterResolutionError(
      502,
      error instanceof Error
        ? error.message
        : "キャラクターサービスに接続できません",
    );
  }
  if (!response.ok) {
    const payload = (await response.json().catch(() => null)) as {
      detail?: unknown;
    } | null;
    const detail =
      typeof payload?.detail === "string" && payload.detail.trim()
        ? payload.detail
        : "キャラクター一覧を取得できませんでした";
    const status =
      response.status === 401 || response.status === 403
        ? response.status
        : response.status >= 500
          ? 502
          : response.status;
    throw new CharacterResolutionError(status, detail);
  }
  const payload = (await response.json().catch(() => null)) as {
    characters?: Array<{
      slug?: string;
      name?: string;
      recognition_aliases?: string[];
    }>;
  } | null;
  const characters = Array.isArray(payload?.characters)
    ? payload.characters
    : [];
  const key = normalized.toLocaleLowerCase();
  const character = characters.find((item) => {
    const candidates = [
      item.slug,
      item.name,
      ...(Array.isArray(item.recognition_aliases)
        ? item.recognition_aliases
        : []),
    ];
    return candidates.some(
      (candidate) =>
        typeof candidate === "string" &&
          candidate.trim().toLocaleLowerCase() === key,
    );
  });
  return character?.slug?.trim() || null;
}

function sessionToSnake(row: Record<string, unknown>): Record<string, unknown> {
  const map: Record<string, string> = {
    id: "id",
    userId: "user_id",
    characterName: "character_name",
    title: "title",
    sessionStart: "session_start",
    lastActivity: "last_activity",
    messageCount: "message_count",
    context: "context",
    currentSummary: "current_summary",
    isActive: "is_active",
    deletedAt: "deleted_at",
    projectId: "project_id",
    appId: "app_id",
    appTargetId: "app_target_id",
    developmentStatus: "development_status",
    lastReadAt: "last_read_at",
    isGroupChat: "is_group_chat",
    groupCharacterNames: "group_character_names",
  };
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(row)) {
    out[map[k] ?? k] =
      k === "currentSummary" && typeof v === "string"
        ? decryptTextIfNeeded(v, "conversation_sessions.current_summary")
        : v;
  }
  return out;
}

function isStoryWorkflowSession(row: Record<string, unknown>): boolean {
  const characterName = String(row.characterName ?? "");
  const title = String(row.title ?? "");
  return (
    characterName.startsWith("story_") ||
    title.startsWith("[執筆]")
  );
}

export async function GET(request: NextRequest) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  await cleanupExpiredDeletedConversationsIfDue();

  const { searchParams } = new URL(request.url);
  const projectId = searchParams.get("project_id");
  const appId = searchParams.get("app_id");
  const appTargetId = searchParams.get("app_target_id");

  let readableProjectIds: string[] | null = null;
  if (projectId) {
    try {
      if (!(await canReadProject(projectId, user))) {
        return NextResponse.json({ detail: "Projectを閲覧できません" }, { status: 403 });
      }
    } catch (error) {
      if (error instanceof ConversationScopeError) {
        return NextResponse.json({ detail: error.message }, { status: error.status });
      }
      throw error;
    }
  } else {
    readableProjectIds = await getReadableProjectIds(user.id);
  }

  let appScope: Awaited<ReturnType<typeof validateAppConversationScope>> = null;
  if (appId || appTargetId) {
    try {
      appScope = await validateAppConversationScope({
        appId,
        appTargetId,
        projectId,
        user,
      });
    } catch (error) {
      if (error instanceof ConversationScopeError) {
        return NextResponse.json({ detail: error.message }, { status: error.status });
      }
      throw error;
    }
  }

  const conditions = [
    or(
      eq(conversationSessions.userId, user.id),
      isNotNull(conversationParticipants.id),
    ),
    isNull(conversationSessions.deletedAt),
  ];

  if (projectId) {
    conditions.push(eq(conversationSessions.projectId, projectId));
  } else if (readableProjectIds?.length) {
    conditions.push(
      or(
        isNull(conversationSessions.projectId),
        inArray(conversationSessions.projectId, readableProjectIds),
      ),
    );
  } else {
    conditions.push(isNull(conversationSessions.projectId));
  }
  if (appScope?.appId) {
    conditions.push(eq(conversationSessions.appId, appScope.appId));
  }
  if (appScope?.appTargetId) {
    conditions.push(eq(conversationSessions.appTargetId, appScope.appTargetId));
  }

  const rows = await db
    .select({
      session: conversationSessions,
      actualMessageCount: sql<number>`count(${conversationMessages.id})`,
    })
    .from(conversationSessions)
    .leftJoin(
      conversationParticipants,
      and(
        eq(conversationParticipants.sessionId, conversationSessions.id),
        eq(conversationParticipants.participantType, "user"),
        eq(conversationParticipants.participantId, user.id),
        eq(conversationParticipants.status, "joined"),
      ),
    )
    .leftJoin(
      conversationMessages,
      eq(conversationMessages.sessionId, conversationSessions.id),
    )
    .where(and(...conditions))
    .groupBy(conversationSessions.id)
    .orderBy(
      // last_activity is the durable activity boundary. Deriving this from
      // message.created_at makes the list response disagree with resume (and
      // causes a click-only session replacement to reorder the sidebar).
      sql`coalesce(${conversationSessions.lastActivity}, ${conversationSessions.sessionStart}) desc nulls last`,
      sql`${conversationSessions.sessionStart} desc nulls last`,
      desc(conversationSessions.id),
    );

  const result = rows
    .filter(
      (r) =>
        Number(r.actualMessageCount ?? 0) > 0 &&
        !isStoryWorkflowSession(r.session as unknown as Record<string, unknown>),
    )
    .map((r) => {
      const session = sessionToSnake({
        ...(r.session as unknown as Record<string, unknown>),
        // Keep the same activity source as the resume endpoint. Session
        // activity changes only for a session/message/agent response event,
        // not when the session is opened or its title/read state is changed.
        lastActivity: r.session.lastActivity ?? r.session.sessionStart,
      });
      const lastActivity = session.last_activity;
      const lastReadAt = session.last_read_at;
      const activityMs =
        lastActivity instanceof Date
          ? lastActivity.getTime()
          : typeof lastActivity === "string"
            ? Date.parse(lastActivity)
            : Number.NaN;
      const readMs =
        lastReadAt instanceof Date
          ? lastReadAt.getTime()
          : typeof lastReadAt === "string"
            ? Date.parse(lastReadAt)
            : Number.NaN;
      const isUnread =
        session.app_id != null &&
        session.development_status === "waiting_for_user" &&
        Number.isFinite(activityMs) &&
        (!lastReadAt || !Number.isFinite(readMs) || activityMs > readMs);
      return { ...session, is_unread: isUnread };
    });

  return jsonWithConditional(request, {
    conversations: result,
    total: result.length,
  });
}

export async function POST(request: NextRequest) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const body = await request.json();
  const { character_name, project_id } = body;
  const appId =
    typeof body?.app_id === "string" && body.app_id.trim()
      ? body.app_id.trim()
      : null;
  const appTargetId =
    typeof body?.app_target_id === "string" && body.app_target_id.trim()
      ? body.app_target_id.trim()
      : null;
  const initialMessage =
    body?.initial_message && typeof body.initial_message === "object"
      ? body.initial_message
      : null;
  const initialContent =
    typeof initialMessage?.content === "string" ? initialMessage.content : "";
  const initialClientMessageId =
    typeof initialMessage?.client_message_id === "string"
      ? initialMessage.client_message_id
      : null;

  if (!character_name) {
    return NextResponse.json(
      { detail: "character_nameは必須です" },
      { status: 400 }
    );
  }

  const requestedProjectId =
    typeof project_id === "string" && project_id.trim()
      ? project_id.trim()
      : null;
  if (project_id !== null && project_id !== undefined && !requestedProjectId) {
    return NextResponse.json(
      { detail: "project_idの形式が不正です" },
      { status: 400 },
    );
  }
  if (requestedProjectId) {
    try {
      if (!(await canWriteProject(requestedProjectId, user))) {
        return NextResponse.json(
          { detail: "プロジェクトへの書き込み権限がありません" },
          { status: 403 },
        );
      }
    } catch (error) {
      if (error instanceof ConversationScopeError) {
        return NextResponse.json(
          { detail: error.message },
          { status: error.status },
        );
      }
      throw error;
    }
  }

  // App開発チャットは、App/Target権限の検証と進行状態の初期化を
  // Python側の正規作成経路に委譲する。通常チャットのBFF直書き経路とは
  // 分けることで、権限検証を迂回して他ユーザーのAppを紐付けられないようにする。
  if (appId || appTargetId) {
    let response: Response;
    try {
      response = await fetchPythonApi("/api/conversations", {
        method: "POST",
        user,
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          character_name,
          project_id: requestedProjectId,
          app_id: appId,
          app_target_id: appTargetId,
        }),
      });
    } catch (error) {
      return NextResponse.json(
        {
          detail:
            error instanceof Error
              ? error.message
              : "会話サービスに接続できません",
        },
        { status: 502 },
      );
    }
    const responseBody = await response.text();
    if (!response.ok || !initialContent.trim()) {
      return new NextResponse(responseBody, {
        status: response.status,
        headers: {
          "content-type":
            response.headers.get("content-type") ?? "application/json",
        },
      });
    }

    // Python側でApp scopeを検証しつつ、Web composerが同じ本文を二重送信
    // しないよう、任意の初回ユーザーメッセージを作成レスポンスへ戻す。
    let created: {
      session?: { id?: unknown };
      [key: string]: unknown;
    };
    try {
      created = JSON.parse(responseBody) as typeof created;
    } catch {
      return new NextResponse(responseBody, {
        status: response.status,
        headers: {
          "content-type":
            response.headers.get("content-type") ?? "application/json",
        },
      });
    }
    const createdSessionId =
      typeof created.session?.id === "string" ? created.session.id : null;
    if (!createdSessionId) {
      return NextResponse.json(created, { status: response.status });
    }

    let messageResponse: Response;
    try {
      messageResponse = await fetchPythonApi(
        `/api/conversations/${encodeURIComponent(createdSessionId)}/messages`,
        {
          method: "POST",
          user,
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            role: "user",
            content: initialContent,
            client_message_id: initialClientMessageId,
          }),
        },
      );
    } catch {
      return NextResponse.json(
        { detail: "初回メッセージを保存できませんでした" },
        { status: 502 },
      );
    }
    const messageBody = await messageResponse.text();
    if (!messageResponse.ok) {
      return new NextResponse(messageBody, {
        status: messageResponse.status,
        headers: {
          "content-type":
            messageResponse.headers.get("content-type") ?? "application/json",
        },
      });
    }
    try {
      const added = JSON.parse(messageBody) as { message?: unknown };
      if (added.message) created.initial_message = added.message;
    } catch {
      // セッション作成レスポンスは有効なまま返し、次のメッセージ取得で
      // 保存済みの初回ユーザーメッセージを同期できるようにする。
    }
    return NextResponse.json(created, { status: response.status });
  }

  let characterSlug: string | null;
  try {
    characterSlug = await resolveCharacterSlug(String(character_name), user);
  } catch (error) {
    if (error instanceof CharacterResolutionError) {
      return NextResponse.json(
        { detail: error.message },
        { status: error.status },
      );
    }
    throw error;
  }
  if (!characterSlug) {
    return NextResponse.json(
      { detail: `存在しないキャラクターです: ${String(character_name)}` },
      { status: 400 },
    );
  }

  const now = new Date();
  const result = await db.transaction(async (tx) => {
    const [session] = await tx
      .insert(conversationSessions)
      .values({
        userId: user.id,
        characterName: characterSlug,
        projectId: requestedProjectId,
        sessionStart: now,
        lastActivity: now,
        messageCount: initialContent.trim() ? 1 : 0,
        isActive: true,
      })
      .returning();

    await tx.insert(conversationParticipants).values({
      sessionId: session.id,
      participantType: "user",
      participantId: user.id,
      displayName: user.displayName || user.username || user.email || user.id,
      role: "owner",
      status: "joined",
      autoRespond: false,
      participantMetadata: {},
      createdAt: now,
      updatedAt: now,
    });

    let initial_message: typeof conversationMessages.$inferSelect | undefined;
    if (initialContent.trim()) {
      [initial_message] = await tx
        .insert(conversationMessages)
        .values({
          sessionId: session.id,
          role: "user",
          content: encryptText(initialContent, "conversation_messages.content"),
          messageMetadata: {
            ...(initialClientMessageId
              ? { client_message_id: initialClientMessageId }
              : {}),
          },
          senderType: "user",
          senderId: user.id,
          senderDisplayName:
            user.displayName || user.username || user.email || user.id,
          createdAt: now,
          branchIndex: 0,
          isActiveBranch: true,
        })
        .returning();
    }

    return { session, initial_message };
  });

  return NextResponse.json({
    success: true,
    session: sessionToSnake(result.session as unknown as Record<string, unknown>),
    initial_message: result.initial_message
      ? messageToSnake(result.initial_message)
      : undefined,
  });
}
