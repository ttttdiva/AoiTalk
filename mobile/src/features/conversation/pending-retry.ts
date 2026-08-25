import { isLikelyConnectivityFailure } from "./fallback-error";
import type { ConversationMessage } from "../../types/api";

export type PendingRetryResult =
  | { ok: true }
  | { ok: false; error: unknown; connectivityFailure: boolean };

export async function attemptPendingRetry(
  dispatch: () => Promise<unknown>,
): Promise<PendingRetryResult> {
  try {
    await dispatch();
    return { ok: true };
  } catch (error) {
    return {
      ok: false,
      error,
      connectivityFailure: isLikelyConnectivityFailure(error),
    };
  }
}

export function findAcceptedRemoteMessage(
  messages: ConversationMessage[],
  clientMessageId: string,
): ConversationMessage | null {
  return (
    messages.find(
      (message) =>
        message.role === "user" &&
        message.metadata?.client_message_id === clientMessageId,
    ) ?? null
  );
}
