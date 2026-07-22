import { sanitizeChatCommandCapabilities } from "../features/conversation/chat-commands";
import type {
  ChatResponseModelSelection,
  ConversationMessage,
} from "../types/api";
import type { ChatCommandCapability } from "../features/conversation/chat-commands";

export function pendingResponseModel(
  message: ConversationMessage,
): ChatResponseModelSelection | undefined {
  const value = message.metadata?.response_model;
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
  const provider = (value as Record<string, unknown>).provider;
  const model = (value as Record<string, unknown>).model;
  return typeof provider === "string" && typeof model === "string"
    ? { provider, model }
    : undefined;
}

export function pendingCommandCapabilities(
  message: ConversationMessage,
): ChatCommandCapability[] | undefined {
  const value = message.metadata?.command_capabilities;
  if (!Array.isArray(value)) return undefined;
  return sanitizeChatCommandCapabilities(value);
}
