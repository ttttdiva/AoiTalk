import { NextRequest, NextResponse } from "next/server";
import { db } from "@/db";
import {
  conversationMessages,
  conversationParticipants,
  conversationSessions,
} from "@/db/schema";
import { eq, and, isNull, desc, sql, or, isNotNull } from "drizzle-orm";
import { getSession } from "@/lib/auth";
import { decryptTextIfNeeded, encryptText } from "@/lib/server/field-crypto";
import { messageToSnake } from "@/lib/server/conversation-route-utils";
import { cleanupExpiredDeletedConversationsIfDue } from "@/lib/server/conversation-retention-cleanup";
import { fetchPythonApi, type InternalPythonUser } from "@/lib/server/python-api-proxy";

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

function isScenarioWorkflowSession(row: Record<string, unknown>): boolean {
  const characterName = String(row.characterName ?? "");
  const title = String(row.title ?? "");
  return (
    /^scenario_roleplay:[^:]+:[^:]+$/.test(characterName) ||
    characterName.startsWith("scenario_") ||
    characterName.startsWith("trpg_room_") ||
    title.startsWith("[シナリオ]") ||
    title.startsWith("[執筆]") ||
    title.startsWith("[TRPG]")
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

  const conditions = [
    or(
      eq(conversationSessions.userId, user.id),
      isNotNull(conversationParticipants.id),
    ),
    isNull(conversationSessions.deletedAt),
  ];

  if (projectId) {
    conditions.push(eq(conversationSessions.projectId, projectId));
  }

  const rows = await db
    .select({
      session: conversationSessions,
      lastMessageAt: sql<Date | null>`max(${conversationMessages.createdAt})`,
      actualMessageCount: sql<number>`count(${conversationMessages.id})`,
    })
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
    .leftJoin(
      conversationMessages,
      eq(conversationMessages.sessionId, conversationSessions.id),
    )
    .where(and(...conditions))
    .groupBy(conversationSessions.id)
    .orderBy(
      desc(
        sql`coalesce(max(${conversationMessages.createdAt}), ${conversationSessions.sessionStart}, ${conversationSessions.lastActivity})`,
      ),
      desc(conversationSessions.sessionStart),
      desc(conversationSessions.id),
    );

  const result = rows
    .filter(
      (r) =>
        Number(r.actualMessageCount ?? 0) > 0 &&
        !isScenarioWorkflowSession(r.session as unknown as Record<string, unknown>),
    )
    .map((r) =>
      sessionToSnake({
        ...(r.session as unknown as Record<string, unknown>),
        lastActivity:
          r.lastMessageAt ??
          r.session.sessionStart ??
          r.session.lastActivity,
      }),
    );

  return NextResponse.json({ conversations: result, total: result.length });
}

export async function POST(request: NextRequest) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const body = await request.json();
  const { character_name, project_id } = body;
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
        projectId: project_id || null,
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
