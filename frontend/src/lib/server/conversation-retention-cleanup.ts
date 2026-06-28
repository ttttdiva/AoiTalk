import { and, inArray, isNotNull, lt, sql } from "drizzle-orm";
import { db } from "@/db";
import {
  conversationArchives,
  conversationHistory,
  conversationMessages,
  conversationParticipants,
  conversationSessions,
} from "@/db/schema";

const DEFAULT_RETENTION_DAYS = 90;
const DEFAULT_BATCH_SIZE = 500;
const CLEANUP_INTERVAL_MS = 24 * 60 * 60 * 1000;

let lastCleanupAttemptAt = 0;
let cleanupInFlight: Promise<ConversationRetentionCleanupResult> | null = null;

export type ConversationRetentionCleanupResult = {
  expiredSessionCount: number;
  deletedSessionCount: number;
};

function cutoffDate(now: Date, retentionDays: number): Date {
  return new Date(now.getTime() - retentionDays * 24 * 60 * 60 * 1000);
}

function uuidList(ids: string[]) {
  return sql.join(
    ids.map((id) => sql`${id}`),
    sql`, `,
  );
}

export async function cleanupExpiredDeletedConversations({
  now = new Date(),
  retentionDays = DEFAULT_RETENTION_DAYS,
  batchSize = DEFAULT_BATCH_SIZE,
}: {
  now?: Date;
  retentionDays?: number;
  batchSize?: number;
} = {}): Promise<ConversationRetentionCleanupResult> {
  const expiredSessions = await db
    .select({ id: conversationSessions.id })
    .from(conversationSessions)
    .where(
      and(
        isNotNull(conversationSessions.deletedAt),
        lt(conversationSessions.deletedAt, cutoffDate(now, retentionDays)),
      ),
    )
    .limit(batchSize);

  const sessionIds = expiredSessions.map((session) => session.id);
  if (sessionIds.length === 0) {
    return { expiredSessionCount: 0, deletedSessionCount: 0 };
  }

  return await db.transaction(async (tx) => {
    const sessionIdList = uuidList(sessionIds);

    await tx.delete(conversationHistory).where(
      inArray(conversationHistory.sessionId, sessionIds),
    );
    await tx.delete(conversationArchives).where(
      inArray(conversationArchives.originalSessionId, sessionIds),
    );

    await tx.execute(sql`
      UPDATE scenario_writing_sessions
      SET conversation_session_id = NULL
      WHERE conversation_session_id IN (${sessionIdList})
    `);

    await tx.delete(conversationMessages).where(
      inArray(conversationMessages.sessionId, sessionIds),
    );
    await tx.delete(conversationParticipants).where(
      inArray(conversationParticipants.sessionId, sessionIds),
    );

    const deletedSessions = await tx
      .delete(conversationSessions)
      .where(inArray(conversationSessions.id, sessionIds))
      .returning({ id: conversationSessions.id });

    return {
      expiredSessionCount: sessionIds.length,
      deletedSessionCount: deletedSessions.length,
    };
  });
}

export async function cleanupExpiredDeletedConversationsIfDue(): Promise<ConversationRetentionCleanupResult> {
  const now = Date.now();
  if (cleanupInFlight) return cleanupInFlight;
  if (now - lastCleanupAttemptAt < CLEANUP_INTERVAL_MS) {
    return { expiredSessionCount: 0, deletedSessionCount: 0 };
  }

  lastCleanupAttemptAt = now;
  cleanupInFlight = cleanupExpiredDeletedConversations()
    .catch((error) => {
      console.error("期限切れ会話履歴の実削除に失敗しました:", error);
      return { expiredSessionCount: 0, deletedSessionCount: 0 };
    })
    .finally(() => {
      cleanupInFlight = null;
    });

  return cleanupInFlight;
}
