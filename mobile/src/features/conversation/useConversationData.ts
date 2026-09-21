import { chatApi } from "../../lib/chat-api";
import { conversationsRepo } from "../../repositories";
import { applyForegroundConversationSessions } from "../../repositories/conversations";
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
