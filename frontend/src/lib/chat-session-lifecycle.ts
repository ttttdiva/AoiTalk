import type { ConversationSession } from "@/lib/chat-api";

/**
 * Shared ordering for making a newly-created conversation active.  Registering
 * before activation prevents the Chat page from observing an active id with no
 * session record while its hydration effect is starting.
 */
export function registerAndActivateChatSession({
  session,
  addSession,
  activateSession,
  initializeGeneration,
}: {
  session: ConversationSession;
  addSession: (session: ConversationSession) => void;
  activateSession: (sessionId: string) => void;
  initializeGeneration?: (sessionId: string) => void;
}) {
  initializeGeneration?.(session.id);
  addSession(session);
  activateSession(session.id);
}
