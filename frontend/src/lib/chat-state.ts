import type { ConversationMessage } from "@/lib/chat-api";

export type ChatTimelineState = {
  messages: ConversationMessage[];
};

export type ChatTimelineAction =
  | { type: "clear" }
  | { type: "replace"; messages: ConversationMessage[] }
  | { type: "append"; message: ConversationMessage }
  | { type: "append_many"; messages: ConversationMessage[] }
  | {
      type: "keep_transient_for_session";
      sessionId: string;
    }
  | {
      type: "hydrate_persisted";
      sessionId: string;
      messages: ConversationMessage[];
    }
  | {
      type: "promote_client_message";
      sessionId: string;
      clientMessageId: string;
      serverMessageId: string;
    }
  | {
      type: "update_by_id";
      messageId: string;
      update: (message: ConversationMessage) => ConversationMessage;
      fallback?: ConversationMessage;
    }
  | {
      type: "replace_by_id";
      messageId: string;
      message: ConversationMessage;
      appendIfMissing?: boolean;
    }
  | {
      type: "append_to_last_assistant";
      sessionId: string;
      content: string;
    };

export const initialChatTimelineState: ChatTimelineState = {
  messages: [],
};

export function getMessageClientId(
  message: ConversationMessage,
): string | null {
  const value = message.metadata?.client_message_id;
  return typeof value === "string" && value.length > 0 ? value : null;
}

export function isTransientChatMessage(message: ConversationMessage): boolean {
  return message.id.startsWith("temp-") || message.id.startsWith("msg-");
}

function hasServerId(message: ConversationMessage): boolean {
  return !isTransientChatMessage(message);
}

function isUnscopedWsTransientMessage(message: ConversationMessage): boolean {
  return message.metadata?.transient_source === "unscoped_ws_new_message";
}

function sameRenderableMessage(
  a: ConversationMessage,
  b: ConversationMessage,
): boolean {
  return (
    a.session_id === b.session_id &&
    a.role === b.role &&
    a.content === b.content
  );
}

function findEquivalentIndex(
  messages: ConversationMessage[],
  incoming: ConversationMessage,
): number {
  const byId = messages.findIndex((message) => message.id === incoming.id);
  if (byId >= 0) return byId;

  const incomingClientId = getMessageClientId(incoming);
  if (incomingClientId) {
    const byClientId = messages.findIndex(
      (message) => getMessageClientId(message) === incomingClientId,
    );
    if (byClientId >= 0) return byClientId;
  }

  if (isTransientChatMessage(incoming)) {
    return messages.findIndex((message) =>
      sameRenderableMessage(message, incoming),
    );
  }

  return messages.findIndex(
    (message) =>
      isTransientChatMessage(message) && sameRenderableMessage(message, incoming),
  );
}

function upsertMessage(
  messages: ConversationMessage[],
  incoming: ConversationMessage,
): ConversationMessage[] {
  const index = findEquivalentIndex(messages, incoming);
  if (index < 0) return [...messages, incoming];

  const existing = messages[index];
  if (isTransientChatMessage(incoming) && hasServerId(existing)) {
    return messages;
  }

  const next = [...messages];
  next[index] =
    hasServerId(incoming) || isTransientChatMessage(existing)
      ? incoming
      : { ...existing, ...incoming, id: existing.id };
  return next;
}

export function mergePersistedChatMessages(
  localMessages: ConversationMessage[],
  persistedMessages: ConversationMessage[],
  sessionId: string,
): ConversationMessage[] {
  let merged: ConversationMessage[] = [];
  for (const persisted of persistedMessages) {
    merged = upsertMessage(merged, persisted);
  }

  for (const local of localMessages) {
    if (local.session_id !== sessionId) continue;
    if (!isTransientChatMessage(local)) continue;
    if (isUnscopedWsTransientMessage(local)) continue;

    const equivalentPersisted = persistedMessages.some(
      (persisted) =>
        persisted.id === local.id ||
        (Boolean(getMessageClientId(local)) &&
          getMessageClientId(local) === getMessageClientId(persisted)) ||
        sameRenderableMessage(local, persisted),
    );
    if (!equivalentPersisted) {
      merged = upsertMessage(merged, local);
    }
  }

  return merged;
}

export function chatTimelineReducer(
  state: ChatTimelineState,
  action: ChatTimelineAction,
): ChatTimelineState {
  switch (action.type) {
    case "clear":
      return initialChatTimelineState;
    case "replace":
      return { messages: action.messages };
    case "append":
      return { messages: upsertMessage(state.messages, action.message) };
    case "append_many":
      return {
        messages: action.messages.reduce(upsertMessage, state.messages),
      };
    case "keep_transient_for_session":
      return {
        messages: state.messages.filter(
          (message) =>
            isTransientChatMessage(message) &&
            message.session_id === action.sessionId,
        ),
      };
    case "hydrate_persisted":
      return {
        messages: mergePersistedChatMessages(
          state.messages,
          action.messages,
          action.sessionId,
        ),
      };
    case "promote_client_message":
      return {
        messages: state.messages.map((message) => {
          if (message.session_id !== action.sessionId) return message;
          if (getMessageClientId(message) !== action.clientMessageId) {
            return message;
          }
          return { ...message, id: action.serverMessageId };
        }),
      };
    case "update_by_id": {
      const index = state.messages.findIndex(
        (message) => message.id === action.messageId,
      );
      if (index < 0) {
        return action.fallback
          ? { messages: upsertMessage(state.messages, action.fallback) }
          : state;
      }
      const messages = [...state.messages];
      messages[index] = action.update(messages[index]);
      return { messages };
    }
    case "replace_by_id": {
      const index = state.messages.findIndex(
        (message) => message.id === action.messageId,
      );
      if (index < 0) {
        return action.appendIfMissing
          ? { messages: upsertMessage(state.messages, action.message) }
          : state;
      }
      const messages = [...state.messages];
      messages[index] = action.message;
      return { messages };
    }
    case "append_to_last_assistant": {
      let index = -1;
      for (let i = state.messages.length - 1; i >= 0; i -= 1) {
        const message = state.messages[i];
        if (
          message.session_id === action.sessionId &&
          message.role === "assistant"
        ) {
          index = i;
          break;
        }
      }
      if (index < 0) return state;

      const target = state.messages[index];
      if (target.content.includes(action.content)) return state;

      const messages = [...state.messages];
      messages[index] = {
        ...target,
        content: `${target.content}\n\n${action.content}`.trim(),
      };
      return { messages };
    }
    default:
      return state;
  }
}
