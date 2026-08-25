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
      type: "rebind_client_message_session";
      fromSessionId: string;
      toSessionId: string;
      clientMessageId: string;
    }
  | {
      type: "remove_client_message";
      sessionId: string;
      clientMessageId: string;
    }
  | {
      type: "update_by_id";
      messageId: string;
      update: (message: ConversationMessage) => ConversationMessage;
      fallback?: ConversationMessage;
    }
  | {
      type: "update_by_client_message_id";
      sessionId: string;
      clientMessageId: string;
      update: (message: ConversationMessage) => ConversationMessage;
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
      messageId?: string | null;
      agentRunId?: string | null;
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

export function getMessageAgentRunId(
  message: ConversationMessage,
): string | null {
  const value = message.metadata?.agent_run_id;
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

function sameLegacyMessage(
  a: ConversationMessage,
  b: ConversationMessage,
): boolean {
  return (
    a.session_id === b.session_id &&
    a.role === b.role &&
    a.content === b.content
  );
}

function sameSession(a: ConversationMessage, b: ConversationMessage): boolean {
  return a.session_id === b.session_id;
}

function haveDifferentServerIdentities(
  a: ConversationMessage,
  b: ConversationMessage,
): boolean {
  return hasServerId(a) && hasServerId(b) && a.id !== b.id;
}

function findStableEquivalentIndex(
  messages: ConversationMessage[],
  incoming: ConversationMessage,
): number {
  if (incoming.session_id == null || incoming.session_id === "") return -1;

  const byId = incoming.id
    ? messages.findIndex(
        (message) =>
          sameSession(message, incoming) &&
          message.id === incoming.id,
      )
    : -1;
  if (byId >= 0) return byId;

  const incomingClientId = getMessageClientId(incoming);
  if (incomingClientId) {
    const byClientId = messages.findIndex(
      (message) =>
        sameSession(message, incoming) &&
        !haveDifferentServerIdentities(message, incoming) &&
        getMessageClientId(message) === incomingClientId,
    );
    if (byClientId >= 0) return byClientId;
  }

  const incomingAgentRunId = getMessageAgentRunId(incoming);
  if (incomingAgentRunId) {
    const byAgentRunId = messages.findIndex(
      (message) =>
        sameSession(message, incoming) &&
        !haveDifferentServerIdentities(message, incoming) &&
        message.role === incoming.role &&
        getMessageAgentRunId(message) === incomingAgentRunId,
    );
    if (byAgentRunId >= 0) return byAgentRunId;
  }

  return -1;
}

function upsertMessage(
  messages: ConversationMessage[],
  incoming: ConversationMessage,
): ConversationMessage[] {
  // Live append is intentionally identity-only.  Two legitimate turns may
  // have the same role and text, so renderable content is never a live
  // duplicate key.
  const index = findStableEquivalentIndex(messages, incoming);
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

function findHydrationEquivalentIndex(
  localMessages: ConversationMessage[],
  persisted: ConversationMessage,
  consumedLocalIndexes: Set<number>,
): number {
  const stableIndex = findStableEquivalentIndex(localMessages, persisted);
  if (stableIndex >= 0 && !consumedLocalIndexes.has(stableIndex)) {
    return stableIndex;
  }

  // Legacy rows without client/run IDs may still be reconciled while
  // hydrating old data.  The candidate is consumed once, so two messages
  // with the same text cannot collapse into one another.
  if (
    getMessageClientId(persisted) ||
    getMessageAgentRunId(persisted) ||
    !persisted.content
  ) {
    return -1;
  }
  return localMessages.findIndex(
    (message, index) =>
      !consumedLocalIndexes.has(index) &&
      isTransientChatMessage(message) &&
      !getMessageClientId(message) &&
      !getMessageAgentRunId(message) &&
      sameLegacyMessage(message, persisted),
  );
}

export function mergePersistedChatMessages(
  localMessages: ConversationMessage[],
  persistedMessages: ConversationMessage[],
  sessionId: string,
): ConversationMessage[] {
  // Hydration is deliberately separate from live append.  Persisted rows are
  // authoritative and are deduplicated only by stable identity.  In
  // particular, two persisted rows with identical content remain two rows.
  const merged: ConversationMessage[] = [];
  const persistedIndexesByMessage = new Map<string, number>();
  for (const persisted of persistedMessages) {
    const index = persisted.id
      ? persistedIndexesByMessage.get(persisted.id)
      : undefined;
    if (index == null) {
      if (persisted.id) persistedIndexesByMessage.set(persisted.id, merged.length);
      merged.push(persisted);
      continue;
    }
    merged[index] = persisted;
  }

  const consumedLocalIndexes = new Set<number>();
  for (const persisted of merged) {
    const index = findHydrationEquivalentIndex(
      localMessages,
      persisted,
      consumedLocalIndexes,
    );
    if (index >= 0) consumedLocalIndexes.add(index);
  }

  for (let index = 0; index < localMessages.length; index += 1) {
    const local = localMessages[index];
    if (local.session_id !== sessionId) continue;
    if (!isTransientChatMessage(local)) continue;
    if (isUnscopedWsTransientMessage(local)) continue;
    if (consumedLocalIndexes.has(index)) continue;
    merged.push(local);
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
            message.session_id === action.sessionId &&
            (isTransientChatMessage(message) ||
              // A newly-created session may already have promoted its
              // client-identified user row before the activation effect runs.
              // Keep that row through the first session cleanup; hydration
              // will reconcile it with the authoritative persisted message.
              (message.role === "user" &&
                getMessageClientId(message) !== null)),
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
      {
        let promoted = false;
        return {
          messages: state.messages.map((message) => {
            if (
              promoted ||
              message.session_id !== action.sessionId ||
              !isTransientChatMessage(message) ||
              getMessageClientId(message) !== action.clientMessageId
            ) {
              return message;
            }
            promoted = true;
            return { ...message, id: action.serverMessageId };
          }),
        };
      }
    case "rebind_client_message_session":
      {
        let rebound = false;
        return {
          messages: state.messages.map((message) => {
            if (
              rebound ||
              message.session_id !== action.fromSessionId ||
              message.role !== "user" ||
              !isTransientChatMessage(message) ||
              getMessageClientId(message) !== action.clientMessageId
            ) {
              return message;
            }
            rebound = true;
            // Keep the optimistic message's identity and metadata intact;
            // only its session binding changes when a provisional session is
            // replaced by the real session returned by the server.
            return { ...message, session_id: action.toSessionId };
          }),
        };
      }
    case "remove_client_message":
      return {
        messages: state.messages.filter(
          (message) =>
            !(
              message.session_id === action.sessionId &&
              message.role === "user" &&
              isTransientChatMessage(message) &&
              getMessageClientId(message) === action.clientMessageId
            ),
        ),
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
    case "update_by_client_message_id": {
      const index = state.messages.findIndex(
        (message) =>
          message.session_id === action.sessionId &&
          getMessageClientId(message) === action.clientMessageId,
      );
      if (index < 0) return state;
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
      if (action.messageId) {
        index = state.messages.findIndex(
          (message) =>
            message.session_id === action.sessionId &&
            message.role === "assistant" &&
            message.id === action.messageId,
        );
      } else if (action.agentRunId) {
        for (let i = state.messages.length - 1; i >= 0; i -= 1) {
          const message = state.messages[i];
          if (
            message.session_id === action.sessionId &&
            message.role === "assistant" &&
            getMessageAgentRunId(message) === action.agentRunId
          ) {
            index = i;
            break;
          }
        }
      }
      if (index < 0) return state;

      const target = state.messages[index];
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
