import { and, inArray, isNotNull, lt, sql } from "drizzle-orm";
import { db } from "@/db";
import {
  conversationArchives,
  conversationHistory,
  conversationMessages,
  conversationParticipants,
  conversationSessions,
} from "@/db/schema";
import {
  appendContentDeletionEvent,
  createDeletionBatchId,
} from "@/lib/server/content-deletion-events";
import {
  DEFAULT_DELETION_RETENTION_DAYS,
  MAX_DELETION_RETENTION_DAYS,
  readDeletionRetentionDays,
} from "@/lib/server/deletion-retention";

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
  retentionDays = readDeletionRetentionDays(),
  batchSize = DEFAULT_BATCH_SIZE,
}: {
  now?: Date;
  retentionDays?: number;
  batchSize?: number;
} = {}): Promise<ConversationRetentionCleanupResult> {
  const effectiveRetentionDays =
    Number.isSafeInteger(retentionDays) &&
    retentionDays > 0 &&
    retentionDays <= MAX_DELETION_RETENTION_DAYS
      ? retentionDays
      : DEFAULT_DELETION_RETENTION_DAYS;
  const expiredSessions = await db
    .select({
      id: conversationSessions.id,
      projectId: conversationSessions.projectId,
      title: conversationSessions.title,
    })
    .from(conversationSessions)
    .where(
      and(
        isNotNull(conversationSessions.deletedAt),
        lt(
          conversationSessions.deletedAt,
          cutoffDate(now, effectiveRetentionDays),
        ),
      ),
    )
    .limit(batchSize);

  const sessionIds = expiredSessions.map((session) => session.id);
  if (sessionIds.length === 0) {
    return { expiredSessionCount: 0, deletedSessionCount: 0 };
  }

  return await db.transaction(async (tx) => {
    // Re-select and lock the candidates inside the destructive transaction.
    // A concurrent restore may clear deleted_at after the initial scan; the
    // lock/recheck prevents that restored session from being physically
    // removed by a stale cleanup snapshot.
    const lockedSessions = await tx
      .select({
        id: conversationSessions.id,
        projectId: conversationSessions.projectId,
        title: conversationSessions.title,
      })
      .from(conversationSessions)
      .where(
        and(
          inArray(conversationSessions.id, sessionIds),
          isNotNull(conversationSessions.deletedAt),
          lt(
            conversationSessions.deletedAt,
            cutoffDate(now, effectiveRetentionDays),
          ),
        ),
      )
      .for("update");
    const lockedSessionIds = lockedSessions.map((session) => session.id);
    if (lockedSessionIds.length === 0) {
      return { expiredSessionCount: 0, deletedSessionCount: 0 };
    }
    const sessionIdList = uuidList(lockedSessionIds);
    const batchId = createDeletionBatchId();

    // The ledger is independent of the rows being purged, so append events
    // before deleting the sessions. Keep cleanup usable during a rolling
    // deployment where the optional audit table may not exist yet.
    if (typeof tx.insert === "function") {
      for (const session of lockedSessions) {
        await appendContentDeletionEvent(tx, {
          batchId,
          entityType: "conversation",
          entityId: session.id,
          rootEntityId: session.id,
          projectId: session.projectId ? String(session.projectId) : null,
          action: "purged",
          displayName: session.title ? String(session.title) : null,
          source: "web.conversations.retention_cleanup",
          eventAt: now,
          metadata: { retention_days: effectiveRetentionDays },
        });
      }
    }

    await tx.delete(conversationHistory).where(
      inArray(conversationHistory.sessionId, lockedSessionIds),
    );
    await tx.delete(conversationArchives).where(
      inArray(conversationArchives.originalSessionId, lockedSessionIds),
    );

    await tx.execute(sql`
      UPDATE story_writing_sessions
      SET conversation_session_id = NULL
      WHERE conversation_session_id IN (${sessionIdList})
    `);

    // image_studio_structured_commands deliberately uses RESTRICT because a
    // live command must never lose its conversation anchor implicitly.  The
    // retention purge is the explicit destructive boundary, so remove those
    // command rows before deleting the parent sessions when that optional
    // image-studio table exists in this deployment.
    const imageCommandRelation = (await tx.execute(sql`
      SELECT to_regclass('public.image_studio_structured_commands') AS relation
    `)) as Array<{ relation?: string | null }>;
    if (imageCommandRelation?.[0]?.relation) {
      await tx.execute(sql`
        DELETE FROM image_studio_structured_commands
        WHERE conversation_session_id IN (${sessionIdList})
      `);
    }

    await tx.delete(conversationMessages).where(
      inArray(conversationMessages.sessionId, lockedSessionIds),
    );
    await tx.delete(conversationParticipants).where(
      inArray(conversationParticipants.sessionId, lockedSessionIds),
    );

    const deletedSessions = await tx
      .delete(conversationSessions)
      .where(inArray(conversationSessions.id, lockedSessionIds))
      .returning({ id: conversationSessions.id });

    return {
      expiredSessionCount: lockedSessionIds.length,
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
