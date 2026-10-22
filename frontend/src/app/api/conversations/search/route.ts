import { NextRequest, NextResponse } from "next/server";
import { and, desc, eq, inArray, isNotNull, isNull, or, sql } from "drizzle-orm";
import { db } from "@/db";
import {
  conversationMessages,
  conversationParticipants,
  conversationSessions,
} from "@/db/schema";
import { getSession } from "@/lib/auth";
import { decryptTextIfNeeded } from "@/lib/server/field-crypto";
import {
  canReadProject,
  ConversationScopeError,
} from "@/lib/server/conversation-route-utils";
import { getReadableProjectIds } from "@/lib/server/task-route-utils";
import {
  ConversationHistoryStatusBridgeError,
  fetchConversationHistoryStatuses,
  isConversationHistoryStatusBridgeError,
} from "@/lib/server/conversation-history-status";

type SearchMatchType = "message" | "session";
const MESSAGE_PAGE_SIZE = 200;

function isCorruptConversationContentError(error: unknown): boolean {
  if (!(error instanceof Error)) return false;
  if (error.message === "unsupported encrypted field format") return true;
  if (error.message === "encrypted field authentication failed") return true;
  return /unable to authenticate data/i.test(error.message);
}

function decryptConversationContent(value: string): string | null {
  try {
    return decryptTextIfNeeded(value, "conversation_messages.content") || "";
  } catch (error) {
    if (!isCorruptConversationContentError(error)) throw error;
    return null;
  }
}

function escapeLikeTerm(value: string): string {
  return value.replace(/[\\%_]/g, (match) => `\\${match}`).toLowerCase();
}

function containsPattern(column: unknown, pattern: string) {
  return sql`lower(coalesce(${column}, '')) like ${pattern} escape '\\'`;
}

function createSnippet(content: string, query: string, maxLength = 160): string {
  const normalizedContent = content.replace(/\s+/g, " ").trim();
  if (normalizedContent.length <= maxLength) return normalizedContent;

  const lowerContent = normalizedContent.toLowerCase();
  const lowerQuery = query.toLowerCase();
  const matchIndex = lowerContent.indexOf(lowerQuery);
  const center = matchIndex >= 0 ? matchIndex : 0;
  const start = Math.max(0, center - Math.floor(maxLength / 3));
  const end = Math.min(normalizedContent.length, start + maxLength);
  const prefix = start > 0 ? "..." : "";
  const suffix = end < normalizedContent.length ? "..." : "";
  return `${prefix}${normalizedContent.slice(start, end)}${suffix}`;
}

function toResult(args: {
  matchType: SearchMatchType;
  session: typeof conversationSessions.$inferSelect;
  message?: typeof conversationMessages.$inferSelect;
  messageContent?: string;
  query: string;
}) {
  const { matchType, session, message, messageContent, query } = args;
  const agentId = (session as unknown as { agentId?: unknown }).agentId;
  const createdAt = message?.createdAt ?? session.lastActivity ?? session.sessionStart;
  const snippet = message
    ? createSnippet(messageContent ?? "", query)
    : [
        session.title ? `タイトル: ${session.title}` : null,
        session.characterName ? `相手: ${session.characterName}` : null,
      ]
        .filter(Boolean)
        .join(" / ");

  return {
    id: message ? `message:${message.id}` : `session:${session.id}`,
    match_type: matchType,
    session_id: session.id,
    message_id: message?.id ?? null,
    title: session.title || "無題の会話",
    character_name: session.characterName,
    role: message?.role ?? null,
    snippet,
    created_at: createdAt,
    last_activity: session.lastActivity ?? session.sessionStart,
    project_id: session.projectId,
    agent_id: typeof agentId === "string" ? agentId : null,
    is_pinned: session.isPinned,
    pinned_at: session.pinnedAt,
  };
}

export async function GET(request: NextRequest) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const { searchParams } = new URL(request.url);
  const query = (searchParams.get("q") || "").trim();
  if (!query) {
    return NextResponse.json({ results: [], total: 0 });
  }

  const requestedLimit = Number.parseInt(searchParams.get("limit") || "30", 10);
  const limit = Number.isFinite(requestedLimit)
    ? Math.min(Math.max(requestedLimit, 1), 50)
    : 30;
  const projectId = searchParams.get("project_id");
  const pattern = `%${escapeLikeTerm(query)}%`;

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

  const sessionConditions = [
    or(
      eq(conversationSessions.userId, user.id),
      isNotNull(conversationParticipants.id),
    ),
    eq(conversationSessions.isActive, true),
    isNull(conversationSessions.deletedAt),
  ];
  if (projectId) {
    sessionConditions.push(eq(conversationSessions.projectId, projectId));
  } else if (readableProjectIds?.length) {
    sessionConditions.push(
      or(
        isNull(conversationSessions.projectId),
        inArray(conversationSessions.projectId, readableProjectIds),
      ),
    );
  } else {
    sessionConditions.push(isNull(conversationSessions.projectId));
  }

  const lowerQuery = query.toLowerCase();
  const results: ReturnType<typeof toResult>[] = [];
  const messageOrderCreatedAt = sql`
    coalesce(${conversationMessages.createdAt}, timestamp '0001-01-01 00:00:00')
  `;
  const messageCursorCreatedAt = sql<string>`${messageOrderCreatedAt}::text`;
  let messageCursor: { createdAt: string; id: string } | null = null;

  while (results.length < limit) {
    const messageConditions = [
      ...sessionConditions,
      isNull(conversationMessages.deletedAt),
      or(
        eq(conversationMessages.isActiveBranch, true),
        isNull(conversationMessages.isActiveBranch),
      ),
    ];
    if (messageCursor) {
      const cursorCreatedAt = sql`${messageCursor.createdAt}::text::timestamp`;
      messageConditions.push(sql`(
        ${messageOrderCreatedAt} < ${cursorCreatedAt}
        or (
          ${messageOrderCreatedAt} = ${cursorCreatedAt}
          and ${conversationMessages.id} < ${messageCursor.id}
        )
      )`);
    }

    const messageRows = await db
      .select({
        session: conversationSessions,
        message: conversationMessages,
        messageCursorCreatedAt,
      })
      .from(conversationMessages)
      .innerJoin(
        conversationSessions,
        eq(conversationMessages.sessionId, conversationSessions.id),
      )
      .leftJoin(
        conversationParticipants,
        and(
          eq(conversationParticipants.sessionId, conversationSessions.id),
          eq(conversationParticipants.participantType, "user"),
          eq(conversationParticipants.participantId, user.id),
          eq(conversationParticipants.status, "joined"),
        ),
      )
      .where(and(...messageConditions))
      .orderBy(desc(messageOrderCreatedAt), desc(conversationMessages.id))
      .limit(MESSAGE_PAGE_SIZE);

    if (messageRows.length === 0) break;
    for (const row of messageRows) {
      if (results.length >= limit) break;
      const content = decryptConversationContent(row.message.content);
      if (content === null || !content.toLowerCase().includes(lowerQuery)) continue;
      results.push(
        toResult({
          matchType: "message",
          session: row.session,
          message: row.message,
          messageContent: content,
          query,
        }),
      );
    }

    const lastRow = messageRows[messageRows.length - 1];
    const lastMessage = lastRow?.message;
    if (!lastMessage?.id || !lastRow?.messageCursorCreatedAt) break;
    messageCursor = {
      createdAt: lastRow.messageCursorCreatedAt,
      id: lastMessage.id,
    };
    if (messageRows.length < MESSAGE_PAGE_SIZE) break;
  }

  const remainingLimit = Math.max(0, limit - results.length);
  if (remainingLimit > 0) {
    const matchedSessionIds = new Set(results.map((result) => result.session_id));
    const sessionRows = await db
      .select()
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
      .where(
        and(
          ...sessionConditions,
          or(
            containsPattern(conversationSessions.title, pattern),
            containsPattern(conversationSessions.characterName, pattern),
          ),
        ),
      )
      .orderBy(
        desc(
          sql`coalesce(${conversationSessions.lastActivity}, ${conversationSessions.sessionStart})`,
        ),
        desc(conversationSessions.id),
      )
      .limit(remainingLimit);

    for (const row of sessionRows) {
      const session = "conversation_sessions" in row
        ? row.conversation_sessions
        : row;
      if (matchedSessionIds.has(session.id)) continue;
      results.push(toResult({ matchType: "session", session, query }));
    }
  }

  if (results.length === 0) {
    return NextResponse.json({ results: [], total: 0 });
  }

  let statusProjections: Awaited<
    ReturnType<typeof fetchConversationHistoryStatuses>
  >;
  try {
    statusProjections = await fetchConversationHistoryStatuses(
      results.map((result) => result.session_id),
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

  let projectedResults: typeof results;
  try {
    projectedResults = results.map((result) => {
      const projection = statusProjections.get(result.session_id);
      if (!projection) {
        throw new ConversationHistoryStatusBridgeError(
          502,
          "history_status_missing_projection",
          "会話履歴の状態投影が一部欠落しています",
        );
      }
      return {
        ...result,
        history_status: projection.history_status,
        history_completed_at: projection.history_completed_at,
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

  return NextResponse.json({
    results: projectedResults,
    total: projectedResults.length,
  });
}
