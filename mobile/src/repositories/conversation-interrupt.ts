import { chatApi } from "../lib/chat-api";
import type { ConversationMessage } from "../types/api";

/** Confirm cancellation before handing off the next turn. Never ask the user. */
export async function interruptConversationForSend(sessionId: string): Promise<ConversationMessage[]> {
  const result = await chatApi.stopGeneration(sessionId);
  if (result.success === false && result.status !== "cancellation_pending") {
    throw new Error("前の応答を安全に停止・保存できませんでした。入力を未送信として保持しました。");
  }
  const messages = result.messages ?? (result.message ? [result.message] : []);
  if (result.status !== "cancellation_pending") return messages;
  const deadline = Date.now() + 10_000;
  do {
    await new Promise<void>((resolve) => setTimeout(resolve, 100));
    const status = await chatApi.getGenerationStatus(sessionId);
    if (!status.running) return messages;
  } while (Date.now() < deadline);
  throw new Error("前の応答を停止できていません。入力を未送信として保持しました。");
}
