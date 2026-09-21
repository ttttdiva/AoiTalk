import type { ConversationMessage } from "../../types/api";
import { interruptConversationForSend } from "../../repositories/conversation-interrupt";

type DirectHandoffOptions = {
  sessionId: string;
  /** False for anonymous/local conversations and when the Server is offline. */
  canStopServer: boolean;
  signal: AbortSignal;
  assertScope: () => Promise<void>;
  retireServerGeneration: () => void;
  persistInterruptedMessages: (messages: ConversationMessage[]) => Promise<void>;
};

function assertNotInterrupted(signal: AbortSignal): void {
  if (signal.aborted) {
    throw Object.assign(new Error("次の入力で応答を中断しました。"), { name: "AbortError" });
  }
}

/**
 * A route change is still an interrupting send. Confirm an accessible Server's
 * cancellation before starting Direct, and retire its stream before any new
 * reply. Offline/anonymous Direct must not acquire a Server dependency.
 */
export async function prepareDirectConversationHandoff({
  sessionId, canStopServer, signal, assertScope,
  retireServerGeneration, persistInterruptedMessages,
}: DirectHandoffOptions): Promise<void> {
  assertNotInterrupted(signal);
  if (canStopServer) {
    await assertScope();
    assertNotInterrupted(signal);
    const interrupted = await interruptConversationForSend(sessionId);
    await assertScope();
    // Even if another input aborted this Direct request during stop(), finish
    // retiring/preserving the old reply before the queued input takes over.
    retireServerGeneration();
    if (interrupted.length) await persistInterruptedMessages(interrupted);
  } else {
    // No network probe when Server is down. Suppress the known stale stream
    // locally so a late transport event cannot overwrite the Direct reply.
    retireServerGeneration();
  }
  assertNotInterrupted(signal);
}
