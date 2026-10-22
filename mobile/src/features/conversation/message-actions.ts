import type {
  ChatResponseModelSelection,
  ConversationMessage,
} from "../../types/api";
import type { ChatResponseTarget } from "./chat-llm-preferences";

export type MessageActionAvailability = {
  isPendingLocal: boolean;
  canEdit: boolean;
  canRetry: boolean;
  canRerun: boolean;
  canCopy: boolean;
  canFork: boolean;
};

function messageState(message: ConversationMessage): string {
  return typeof message.metadata?.message_state === "string"
    ? message.metadata.message_state
    : "";
}

export function isPendingLocalConversationMessage(
  message: ConversationMessage,
): boolean {
  if (message.role !== "user") return false;
  const state = messageState(message);
  return (
    Boolean(message.metadata?.pending) ||
    Boolean(message.metadata?.persistence_error) ||
    state === "direct-failed" ||
    state === "local-draft"
  );
}

export function getPendingMessageRetryRoute(
  message: ConversationMessage,
): "server" | "direct" {
  const state = messageState(message);
  return message.metadata?.delivery_route === "direct" ||
    state === "direct-failed" ||
    state === "local-draft"
    ? "direct"
    : "server";
}
export function getMessageActionAvailability(
  message: ConversationMessage,
  _isAuthenticated: boolean,
): MessageActionAvailability {
  const isPendingLocal = isPendingLocalConversationMessage(message);
  const isUserMessage = message.role === "user";
  const canGenerateAgain =
    !isPendingLocal && (isUserMessage || message.role === "assistant");

  return {
    isPendingLocal,
    // Direct/local conversations are first-class history too. Completed
    // user/assistant messages can fork locally even when no Server message ID
    // exists; only pending/local-draft messages must wait for persistence.
    canEdit: isUserMessage,
    canRetry: isPendingLocal,
    canRerun: canGenerateAgain,
    canCopy: Boolean(message.content),
    canFork: canGenerateAgain,
  };
}

/**
 * A normal resend follows the currently selected response route. Choosing a
 * model explicitly is the only case that intentionally switches to Server.
 * Clone the selection so later preference edits cannot mutate an in-flight
 * submission snapshot.
 */
export function resolveMessageResendTarget(
  currentTarget: ChatResponseTarget,
  responseModel?: ChatResponseModelSelection,
): ChatResponseTarget {
  if (responseModel) {
    return {
      kind: "server",
      responseModel: { ...responseModel },
    };
  }
  if (currentTarget.kind === "direct") {
    return {
      kind: "direct",
      selection: { ...currentTarget.selection },
    };
  }
  return {
    kind: "server",
    ...(currentTarget.responseModel
      ? { responseModel: { ...currentTarget.responseModel } }
      : {}),
  };
}
