import { NextRequest, NextResponse } from "next/server";
import { and, desc, eq, isNotNull, isNull, or, sql } from "drizzle-orm";
import { db } from "@/db";
import {
  conversationMessages,
  conversationParticipants,
  conversationSessions,
} from "@/db/schema";
import { getSession } from "@/lib/auth";
import { decryptTextIfNeeded } from "@/lib/server/field-crypto";

type SearchMatchType = "message" | "session";
const MESSAGE_SCAN_LIMIT = 2000;

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

function isScenarioWorkflowSession(row: {
  characterName?: string | null;
  title?: string | null;
}): boolean {
  const characterName = row.characterName || "";
  const title = row.title || "";
  return (
    /^scenario_roleplay:[^:]+:[^:]+$/.test(characterName) ||
    characterName.startsWith("scenario_") ||
    characterName.startsWith("trpg_room_") ||
    title.startsWith("[シナリオ]") ||
    title.startsWith("[執筆") ||
    title.startsWith("[TRPG]")
  );
}

function toResult(args: {
  matchType: SearchMatchType;
  session: typeof conversationSessions.$inferSelect;
  message?: typeof conversationMessages.$inferSelect;
  messageContent?: string;
  query: string;
}) {
  const { matchType, session, message, messageContent, query } = args;
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

  const sessionConditions = [
    or(
      eq(conversationSessions.userId, user.id),
      isNotNull(conversationParticipants.id),
    ),
    isNull(conversationSessions.deletedAt),
  ];
  if (projectId) {
    sessionConditions.push(eq(conversationSessions.projectId, projectId));
  }

  const messageRows = await db
    .select({
      session: conversationSessions,
      message: conversationMessages,
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
        or(
          eq(conversationParticipants.status, "joined"),
          eq(conversationParticipants.status, "invited"),
        ),
      ),
    )
    .where(
      and(
        ...sessionConditions,
        or(
          eq(conversationMessages.isActiveBranch, true),
          isNull(conversationMessages.isActiveBranch),
        ),
      ),
    )
    .orderBy(desc(conversationMessages.createdAt), desc(conversationMessages.id))
    .limit(MESSAGE_SCAN_LIMIT);

  const lowerQuery = query.toLowerCase();
  const results: ReturnType<typeof toResult>[] = [];
  for (const row of messageRows) {
    if (results.length >= limit) break;
    if (isScenarioWorkflowSession(row.session)) continue;
    const content = decryptTextIfNeeded(
      row.message.content,
      "conversation_messages.content",
    ) || "";
    if (!content.toLowerCase().includes(lowerQuery)) continue;
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
          or(
            eq(conversationParticipants.status, "joined"),
            eq(conversationParticipants.status, "invited"),
          ),
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
      if (isScenarioWorkflowSession(session)) continue;
      results.push(toResult({ matchType: "session", session, query }));
    }
  }

  return NextResponse.json({ results, total: results.length });
}
