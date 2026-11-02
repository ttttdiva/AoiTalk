import { NextRequest, NextResponse } from "next/server";
import { db } from "@/db";
import {
  conversationMessages,
  conversationParticipants,
  conversationSessions,
} from "@/db/schema";
import { eq, and, inArray, isNull, desc, sql, or } from "drizzle-orm";
import { getSession } from "@/lib/auth";
import { decryptTextIfNeeded, encryptText } from "@/lib/server/field-crypto";
import {
  canReadProject,
  canWriteProject,
  canWriteProjectWithExecutor,
  ConversationScopeError,
  messageToSnake,
  validateAppConversationScope,
} from "@/lib/server/conversation-route-utils";
import { cleanupExpiredDeletedConversationsIfDue } from "@/lib/server/conversation-retention-cleanup";
import { fetchPythonApi, type InternalPythonUser } from "@/lib/server/python-api-proxy";
import { jsonWithConditional } from "@/lib/server/http-cache";
import { getReadableProjectIds } from "@/lib/server/task-route-utils";
import {
  ConversationHistoryStatusBridgeError,
  fetchConversationHistoryStatuses,
  isConversationHistoryStatusBridgeError,
} from "@/lib/server/conversation-history-status";

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
    isPinned: "is_pinned",
    pinnedAt: "pinned_at",
    pinUpdatedAt: "pin_updated_at",
    deletedAt: "deleted_at",
    projectId: "project_id",
    appId: "app_id",
    appTargetId: "app_target_id",
    agentId: "agent_id",
    agentBindingUpdatedAt: "agent_binding_updated_at",
    developmentStatus: "development_status",
    lastReadAt: "last_read_at",
    isGroupChat: "is_group_chat",
    groupCharacterNames: "group_character_names",
    historyStatus: "history_status",
    historyCompletedAt: "history_completed_at",
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

const CONVERSATION_PAGE_DEFAULT = 10;
const CONVERSATION_PAGE_MAX = 50;
const CONVERSATION_CURSOR_VERSION = 1;

type ConversationListCursor = {
  v: typeof CONVERSATION_CURSOR_VERSION;
  p: boolean;
  a: string | null;
  s: string | null;
  i: string;
  scope: {
    projectId: string | null;
    appId: string | null;
    appTargetId: string | null;
  };
};

function encodeConversationCursor(cursor: ConversationListCursor): string {
  return Buffer.from(JSON.stringify(cursor), "utf8").toString("base64url");
}

function decodeConversationCursor(
  raw: string,
  scope: ConversationListCursor["scope"],
): ConversationListCursor {
  if (!/^[A-Za-z0-9_-]{1,2048}$/.test(raw)) {
    throw new Error("cursor の形式が不正です");
  }
  let value: unknown;
  try {
    value = JSON.parse(Buffer.from(raw, "base64url").toString("utf8"));
  } catch {
    throw new Error("cursor の形式が不正です");
  }
  if (!value || typeof value !== "object") {
    throw new Error("cursor の形式が不正です");
  }
  const candidate = value as Partial<ConversationListCursor>;
  if (
    candidate.v !== CONVERSATION_CURSOR_VERSION ||
    typeof candidate.p !== "boolean" ||
    (candidate.a !== null && typeof candidate.a !== "string") ||
    (candidate.s !== null && typeof candidate.s !== "string") ||
    typeof candidate.i !== "string" ||
    candidate.i.length === 0 ||
    !candidate.scope ||
    typeof candidate.scope !== "object"
  ) {
    throw new Error("cursor の形式が不正です");
  }
  const candidateScope = candidate.scope as ConversationListCursor["scope"];
  if (
    (candidateScope.projectId ?? null) !== scope.projectId ||
    (candidateScope.appId ?? null) !== scope.appId ||
    (candidateScope.appTargetId ?? null) !== scope.appTargetId
  ) {
    throw new Error("cursor の対象スコープが一致しません");
  }
  for (const stamp of [candidate.a, candidate.s]) {
    if (stamp !== null && Number.isNaN(new Date(stamp).getTime())) {
      throw new Error("cursor の日時が不正です");
    }
  }
  return {
    v: CONVERSATION_CURSOR_VERSION,
    p: candidate.p,
    a: candidate.a ?? null,
    s: candidate.s ?? null,
    i: candidate.i,
    scope,
  };
}

function pageLimit(searchParams: URLSearchParams): number | null {
  // Keep the unbounded response for existing non-Chat consumers. The Chat
  // client opts into the bounded contract by sending `limit=10`.
  if (!searchParams.has("limit") && !searchParams.has("cursor")) return null;
  const raw = searchParams.get("limit") ?? String(CONVERSATION_PAGE_DEFAULT);
  if (!/^[0-9]+$/.test(raw)) throw new Error("limit は整数で指定してください");
  const parsed = Number(raw);
  if (!Number.isSafeInteger(parsed) || parsed < 1 || parsed > CONVERSATION_PAGE_MAX) {
    throw new Error(`limit は1から${CONVERSATION_PAGE_MAX}の範囲で指定してください`);
  }
  return parsed;
}

function cursorDate(value: string | null) {
  if (value === null) return null;

  // List timestamps are PostgreSQL `timestamp without time zone` values whose
  // stored fields are UTC. New cursors preserve those fields directly; older
  // cursors used Date.toISOString() and end in Z, so normalize those to UTC
  // fields before PostgreSQL discards the timezone marker on the cast.
  const normalized = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(value)
    ? new Date(value).toISOString()
    : value;
  return sql`${normalized.replace("T", " ").replace(/Z$/i, "")}::timestamp`;
}

function conversationCursorScope(
  projectId: string | null,
  appScope: { appId?: string | null; appTargetId?: string | null } | null,
): ConversationListCursor["scope"] {
  return {
    projectId: projectId ?? null,
    appId: appScope?.appId ?? null,
    appTargetId: appScope?.appTargetId ?? null,
  };
}

function conversationCursorCondition(cursor: ConversationListCursor) {
  const activity = sql`coalesce(${conversationSessions.lastActivity}, ${conversationSessions.sessionStart})`;
  const activityAfter =
    cursor.a === null
      ? sql`false`
      : sql`(${activity} < ${cursorDate(cursor.a)} or ${activity} is null)`;
  const activityEqual =
    cursor.a === null
      ? sql`${activity} is null`
      : sql`${activity} = ${cursorDate(cursor.a)}`;
  const sessionStart = conversationSessions.sessionStart;
  const sessionStartAfter =
    cursor.s === null
      ? sql`false`
      : sql`(${sessionStart} < ${cursorDate(cursor.s)} or ${sessionStart} is null)`;
  const sessionStartEqual =
    cursor.s === null
      ? sql`${sessionStart} is null`
      : sql`${sessionStart} = ${cursorDate(cursor.s)}`;

  return sql`(
    ${conversationSessions.isPinned} < ${cursor.p}
    or (
      ${conversationSessions.isPinned} = ${cursor.p}
      and (
        ${activityAfter}
        or (${activityEqual} and (${sessionStartAfter} or (${sessionStartEqual} and ${conversationSessions.id} < ${cursor.i})))
      )
    )
  )`;
}

function cursorFromSession(
  row: {
    session: typeof conversationSessions.$inferSelect;
    cursorActivity: string | null;
    cursorSessionStart: string | null;
  },
  scope: ConversationListCursor["scope"],
): ConversationListCursor {
  return {
    v: CONVERSATION_CURSOR_VERSION,
    p: row.session.isPinned,
    a: row.cursorActivity,
    s: row.cursorSessionStart,
    i: row.session.id,
    scope,
  };
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

  let limit: number | null;
  let cursor: ConversationListCursor | null = null;
  const cursorScope = conversationCursorScope(projectId, appScope);
  try {
    limit = pageLimit(searchParams);
    const rawCursor = searchParams.get("cursor");
    if (rawCursor !== null) {
      cursor = decodeConversationCursor(rawCursor, cursorScope);
    }
    if (cursor && limit === null) limit = CONVERSATION_PAGE_DEFAULT;
  } catch (error) {
    return NextResponse.json(
      {
        detail: error instanceof Error ? error.message : "ページ指定が不正です",
        code: "invalid_pagination",
      },
      { status: 400 },
    );
  }

  const joinedParticipant = sql`exists (
    select 1
    from ${conversationParticipants}
    where ${conversationParticipants.sessionId} = ${conversationSessions.id}
      and ${conversationParticipants.participantType} = 'user'
      and ${conversationParticipants.participantId} = ${user.id}
      and ${conversationParticipants.status} = 'joined'
  )`;
  const conditions = [
    or(eq(conversationSessions.userId, user.id), joinedParticipant),
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
  if (cursor) conditions.push(conversationCursorCondition(cursor));
  // Keep the non-empty-session rule in SQL without joining every message row.
  // This lets PostgreSQL apply the keyset limit against session rows instead
  // of materializing and grouping the full transcript for every session.
  conditions.push(
    sql`exists (
      select 1
      from ${conversationMessages}
      where ${conversationMessages.sessionId} = ${conversationSessions.id}
    )`,
  );

  const rowsQuery = db
    .select({
      session: conversationSessions,
      // Keep the exact database precision for pagination. Drizzle's Date mode
      // and the shared OID 1114 parser expose milliseconds, while PostgreSQL
      // timestamps retain microseconds.
      cursorActivity: sql<string | null>`to_char(coalesce(${conversationSessions.lastActivity}, ${conversationSessions.sessionStart}), 'YYYY-MM-DD"T"HH24:MI:SS.US')`,
      cursorSessionStart: sql<string | null>`to_char(${conversationSessions.sessionStart}, 'YYYY-MM-DD"T"HH24:MI:SS.US')`,
    })
    .from(conversationSessions)
    .where(and(...conditions))
    .orderBy(
      desc(conversationSessions.isPinned),
      // last_activity is the durable activity boundary. Deriving this from
      // message.created_at makes the list response disagree with resume (and
      // causes a click-only session replacement to reorder the sidebar).
      sql`coalesce(${conversationSessions.lastActivity}, ${conversationSessions.sessionStart}) desc nulls last`,
      sql`${conversationSessions.sessionStart} desc nulls last`,
      desc(conversationSessions.id),
    );

  const rows = await (limit === null
    ? rowsQuery
    : rowsQuery.limit(limit + 1));

  const hasMore = limit !== null && rows.length > limit;
  const pageRows = limit === null ? rows : rows.slice(0, limit);

  const result = pageRows.map((r) => {
    const session = sessionToSnake({
      ...(r.session as unknown as Record<string, unknown>),
      // Keep the same activity source as the resume endpoint. Session
      // activity changes only for a session/message/agent response event,
      // not when the session is opened or its title/read state is changed.
      lastActivity: r.session.lastActivity ?? r.session.sessionStart,
    });
    return session;
  });

  let statusProjections: Awaited<
    ReturnType<typeof fetchConversationHistoryStatuses>
  >;
  try {
    statusProjections = await fetchConversationHistoryStatuses(
      result
        .map((session) => (session as { id?: unknown }).id)
        .filter((sessionId): sessionId is string => typeof sessionId === "string"),
      user,
    );
  } catch (error) {
    if (isConversationHistoryStatusBridgeError(error)) {
      return NextResponse.json(
        { detail: error.message, code: error.code },
        { status: error.status },
      );
    }
    throw error;
  }

  let projectedResult: typeof result;
  try {
    projectedResult = result.map((session) => {
      const sessionId = (session as { id?: unknown }).id;
      const projection = statusProjections.get(String(sessionId));
      if (!projection) {
        throw new ConversationHistoryStatusBridgeError(
          502,
          "history_status_missing_projection",
          "会話履歴の状態投影が一部欠落しています",
        );
      }
      return {
        ...session,
        history_status: projection.history_status,
        history_completed_at: projection.history_completed_at,
        // Keep the legacy field as a compatibility alias for the shared
        // assistant-completion/read-fence projection across normal and App chat.
        is_unread: projection.history_status === "ready",
      };
    });
  } catch (error) {
    if (isConversationHistoryStatusBridgeError(error)) {
      return NextResponse.json(
        { detail: error.message, code: error.code },
        { status: error.status },
      );
    }
    throw error;
  }

  const nextCursor =
    hasMore && pageRows.length > 0
      ? encodeConversationCursor(
          cursorFromSession(pageRows[pageRows.length - 1], cursorScope),
        )
      : null;

  return jsonWithConditional(request, {
    conversations: projectedResult,
    total: projectedResult.length,
    has_more: hasMore,
    next_cursor: nextCursor,
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
  const requestedAgentId =
    typeof body?.agent_id === "string" && body.agent_id.trim()
      ? body.agent_id.trim()
      : null;
  const clientSessionId =
    typeof body?.client_session_id === "string" && body.client_session_id.trim()
      ? body.client_session_id.trim()
      : null;
  if (
    body?.agent_id !== undefined &&
    body?.agent_id !== null &&
    (typeof body.agent_id !== "string" || !body.agent_id.trim())
  ) {
    return NextResponse.json({ detail: "agent_idの形式が不正です" }, { status: 400 });
  }
  if (
    clientSessionId &&
    !/^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/iu.test(clientSessionId)
  ) {
    return NextResponse.json({ detail: "client_session_idの形式が不正です" }, { status: 400 });
  }
  const initialMessage =
    body?.initial_message && typeof body.initial_message === "object"
      ? body.initial_message
      : null;
  const initialContent =
    typeof initialMessage?.content === "string" ? initialMessage.content : "";
  const initialClientMessageId =
    typeof initialMessage?.client_message_id === "string" &&
    initialMessage.client_message_id.trim()
      ? initialMessage.client_message_id.trim()
      : null;

  if (!character_name) {
    return NextResponse.json(
      { detail: "character_nameは必須です" },
      { status: 400 }
    );
  }
  if (initialClientMessageId && initialClientMessageId.length > 512) {
    return NextResponse.json(
      { detail: "initial_message.client_message_idは512文字以内で指定してください" },
      { status: 400 },
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

  // App開発チャットとAgent-bound Chatは、scope/identityの検証を
  // Python側の正規作成経路に委譲する。通常チャットのBFF直書き経路とは
  // 分けることで、クライアントがAgentやAppの所有権検証を迂回できないようにする。
  if (clientSessionId || appId || appTargetId || requestedAgentId) {
    let response: Response;
    try {
      const canonicalPath = clientSessionId
        ? `/api/conversations/client/${encodeURIComponent(clientSessionId)}`
        : "/api/conversations";
      response = await fetchPythonApi(canonicalPath, {
        method: clientSessionId ? "PUT" : "POST",
        user,
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          character_name,
          project_id: requestedProjectId,
          app_id: appId,
          app_target_id: appTargetId,
          ...(requestedAgentId ? { agent_id: requestedAgentId } : {}),
          ...(clientSessionId && initialMessage
            ? { initial_message: initialMessage }
            : {}),
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
    if (!response.ok || !initialContent.trim() || clientSessionId) {
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
  let result: {
    session: typeof conversationSessions.$inferSelect;
    initial_message?: typeof conversationMessages.$inferSelect;
  };
  try {
    result = await db.transaction(async (tx) => {
      // The request-level project check above is only an early rejection.
      // Lock and revalidate the Project/membership rows in the creation
      // transaction so a concurrent permission revoke cannot create a new
      // project-bound conversation after access has been removed.
      if (
        requestedProjectId &&
        !(await canWriteProjectWithExecutor(
          tx as unknown as typeof db,
          requestedProjectId,
          user,
        ))
      ) {
        throw new ConversationScopeError(
          403,
          "プロジェクトへの書き込み権限がありません",
        );
      }

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
            clientMessageId: initialClientMessageId,
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
  } catch (error) {
    if (error instanceof ConversationScopeError) {
      return NextResponse.json(
        { detail: error.message },
        { status: error.status },
      );
    }
    throw error;
  }

  return NextResponse.json({
    success: true,
    session: sessionToSnake(result.session as unknown as Record<string, unknown>),
    initial_message: result.initial_message
      ? messageToSnake(result.initial_message)
      : undefined,
  });
}
