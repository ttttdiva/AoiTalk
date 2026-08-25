import type { ConversationMessage } from "../../types/api";

export function buildReplaceableConversationFallbackTitle(
  messages: readonly ConversationMessage[],
): string | null {
  const firstUser = messages.find(
    (message) =>
      message.role === "user" && message.content.replace(/\s+/g, " ").trim(),
  );
  if (!firstUser) return null;
  const compact = firstUser.content.replace(/\s+/g, " ").trim();
  return compact.length > 40 ? `${compact.slice(0, 37)}...` : compact;
}
