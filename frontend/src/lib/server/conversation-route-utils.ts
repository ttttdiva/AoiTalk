import { and, eq, gt, isNull, or } from "drizzle-orm";
import { db } from "@/db";
import {
  conversationMessages,
  conversationParticipants,
  conversationSessions,
} from "@/db/schema";
import { decryptTextIfNeeded } from "./field-crypto";

export type ConversationSessionRow = typeof conversationSessions.$inferSelect;

export function sessionToSnake(
  row: Record<string, unknown>,
): Record<string, unknown> {
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
  for (const [key, value] of Object.entries(row)) {
    out[map[key] ?? key] =
      key === "currentSummary" && typeof value === "string"
        ? decryptTextIfNeeded(value, "conversation_sessions.current_summary")
        : value;
  }
  return out;
}

export function messageToSnake(row: typeof conversationMessages.$inferSelect) {
  const content = decryptTextIfNeeded(
    row.content,
    "conversation_messages.content",
  );
  return {
    id: row.id,
    session_id: row.sessionId,
    role: row.role,
    content,
    metadata: row.messageMetadata,
    sender_type: row.senderType,
    sender_id: row.senderId,
    sender_display_name: row.senderDisplayName,
    created_at: row.createdAt,
    token_count: row.tokenCount,
    parent_message_id: row.parentMessageId,
    branch_index: row.branchIndex,
    is_active_branch: row.isActiveBranch,
  };
}

export async function getLiveConversationSession(
  id: string,
  userId: string,
): Promise<ConversationSessionRow | null> {
  const [participant] = await db
    .select({ sessionId: conversationParticipants.sessionId })
    .from(conversationParticipants)
    .where(
      and(
        eq(conversationParticipants.sessionId, id),
        eq(conversationParticipants.participantType, "user"),
        eq(conversationParticipants.participantId, userId),
        or(
          eq(conversationParticipants.status, "joined"),
          eq(conversationParticipants.status, "invited"),
        ),
      ),
    )
    .limit(1);

  const [session] = await db
    .select()
    .from(conversationSessions)
    .where(
      and(
        eq(conversationSessions.id, id),
        participant
          ? eq(conversationSessions.id, participant.sessionId)
          : eq(conversationSessions.userId, userId),
        isNull(conversationSessions.deletedAt),
      ),
    )
    .limit(1);

  return session ?? null;
}

async function hasMessagesAfterDeletion(
  id: string,
  deletedAt: Date,
): Promise<boolean> {
  const [message] = await db
    .select({ id: conversationMessages.id })
    .from(conversationMessages)
    .where(
      and(
        eq(conversationMessages.sessionId, id),
        gt(conversationMessages.createdAt, deletedAt),
      ),
    )
    .limit(1);

  return Boolean(message);
}

export async function resumeConversationSession(
  id: string,
  userId: string,
): Promise<ConversationSessionRow | null> {
  const [existing] = await db
    .select()
    .from(conversationSessions)
    .where(and(eq(conversationSessions.id, id)))
    .limit(1);

  if (!existing) return null;
  const accessible = await getLiveConversationSession(id, userId);
  if (!accessible) return null;

  if (existing.deletedAt) {
    const shouldRepair = await hasMessagesAfterDeletion(id, existing.deletedAt);
    if (!shouldRepair) return null;
  }

  const [session] = await db
    .update(conversationSessions)
    .set({
      isActive: true,
      deletedAt: null,
    })
    .where(and(eq(conversationSessions.id, id)))
    .returning();

  return session ?? null;
}
