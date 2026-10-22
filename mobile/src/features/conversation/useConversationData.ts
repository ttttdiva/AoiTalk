import { chatApi } from "../../lib/chat-api";
import {
  applyForegroundConversationSessions, conversationsRepo, excludeUserDeletedSessions, getPromotedConversationSessionId,
} from "../../repositories/conversations";
import type { ConversationMessage, ConversationSession } from "../../types/api";

export type ConversationRemoteData = {
  session: ConversationSession;
  messages: ConversationMessage[];
  refreshMode: "full" | "delta" | "full-reconcile";
  receivedCount: number;
  upsertedCount: number;
};

/**
 * Resume owns session metadata only. Durable messages always flow through the
 * cursor-aware repository so cached sessions never download resume history.
 */
export async function loadConversationRemoteData(
  sessionId: string,
): Promise<ConversationRemoteData> {
  const remote = await chatApi.resumeSession(sessionId, {
    includeMessages: false,
  });
  // Subsequent refreshes read session metadata from SQLite as well. Persist
  // the resumed title/group context so they cannot revert to an old cache.
  await applyForegroundConversationSessions([remote.session]);
  const refresh = await conversationsRepo.refreshMessagesDetailed(sessionId);
  return {
    session: remote.session,
    messages: refresh.messages,
    refreshMode: refresh.mode,
    receivedCount: refresh.receivedCount,
    upsertedCount: refresh.upsertedCount,
  };
}


/** Recover the durable route target after upload committed but App binding failed. */
export async function loadConversationLocalData(sessionId: string): Promise<{
  sessionId: string;
  session: ConversationSession | null;
  messages: ConversationMessage[];
}> {
  let session = await conversationsRepo.getSessionLocal(sessionId);
  if (!session && !(await excludeUserDeletedSessions([sessionId], (id) => id)).length) {
    throw new Error("この会話は削除されています。");
  }
  const promotedId = session ? null : await getPromotedConversationSessionId(sessionId);
  const effectiveSessionId = promotedId ?? sessionId;
  if (promotedId) {
    session = await conversationsRepo.getSessionLocal(promotedId);
    // A user-deleted/missing target must not be fetched and displayed again
    // merely because its old local alias still retains a promotion receipt.
    if (!session) throw new Error("昇格先の会話セッションが見つかりません。");
  }
  return {
    sessionId: effectiveSessionId,
    session,
    messages: await conversationsRepo.listMessagesLocal(effectiveSessionId),
  };
}
