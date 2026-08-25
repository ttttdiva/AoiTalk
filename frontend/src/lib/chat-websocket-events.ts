type WebSocketSessionMessage = {
  session_id?: unknown;
  data?: unknown;
  [key: string]: unknown;
};

const GLOBAL_WEBSOCKET_EVENT_TYPES = new Set([
  "bgm_change",
  "chat_cleared",
  "llm_mode_change",
  "rms_update",
]);

export type CancelledAssistantPayload = {
  messageId: string;
  content: string;
  agentRunId: string | null;
  metadata: Record<string, unknown>;
};

export function getWebSocketMessageEventId(
  message: WebSocketSessionMessage,
): string | null {
  const record = messageRecord(message);
  const rawEventId = record.event_id;
  if (
    (typeof rawEventId === "string" && rawEventId.trim()) ||
    typeof rawEventId === "number"
  ) {
    return String(rawEventId);
  }

  const rawSequence = record.event_sequence ?? record.sequence;
  if (
    (typeof rawSequence === "string" && rawSequence.trim()) ||
    typeof rawSequence === "number"
  ) {
    const sessionId = getWebSocketMessageSessionId(message) ?? "global";
    const agentRunId = getWebSocketMessageAgentRunId(message) ?? "runless";
    return `sequence:${sessionId}:${agentRunId}:${String(rawSequence)}`;
  }
  return null;
}

export function getWebSocketMessageEventKey(
  message: WebSocketSessionMessage,
): string | null {
  const eventId = getWebSocketMessageEventId(message);
  if (!eventId) return null;
  return `${getWebSocketMessageSessionId(message) ?? "global"}:${
    getWebSocketMessageAgentRunId(message) ?? "runless"
  }:${eventId}`;
}

function messageRecord(
  message: WebSocketSessionMessage,
): Record<string, unknown> {
  const nested = message.data;
  return nested && typeof nested === "object" && !Array.isArray(nested)
    ? { ...(nested as Record<string, unknown>), ...message }
    : message;
}

export function getWebSocketMessageSessionId(
  message: WebSocketSessionMessage,
): string | null {
  if (typeof message.session_id === "string" && message.session_id) {
    return message.session_id;
  }

  const nested = message.data;
  if (!nested || typeof nested !== "object" || Array.isArray(nested)) {
    return null;
  }

  const nestedSessionId = (nested as { session_id?: unknown }).session_id;
  return typeof nestedSessionId === "string" && nestedSessionId
    ? nestedSessionId
    : null;
}

export function getWebSocketMessageAgentRunId(
  message: WebSocketSessionMessage,
): string | null {
  if (typeof message.agent_run_id === "string" && message.agent_run_id.trim()) {
    return message.agent_run_id;
  }

  const nested = message.data;
  if (!nested || typeof nested !== "object" || Array.isArray(nested)) {
    return null;
  }

  const nestedAgentRunId = (nested as { agent_run_id?: unknown }).agent_run_id;
  return typeof nestedAgentRunId === "string" && nestedAgentRunId.trim()
    ? nestedAgentRunId
    : null;
}

export function getWebSocketMessageClientMessageId(
  message: WebSocketSessionMessage,
): string | null {
  const record = messageRecord(message);
  const value = record.client_message_id;
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

export function getWebSocketMessageId(
  message: WebSocketSessionMessage,
): string | null {
  const record = messageRecord(message);
  const rawId = record.id;
  if (typeof rawId === "string" && rawId.trim()) {
    return rawId;
  }
  const rawMessageId = record.message_id;
  if (typeof rawMessageId === "string" && rawMessageId.trim()) {
    return rawMessageId;
  }
  const nestedMessage = record.message;
  if (
    nestedMessage &&
    typeof nestedMessage === "object" &&
    !Array.isArray(nestedMessage)
  ) {
    const nestedId = (nestedMessage as { id?: unknown }).id;
    if (typeof nestedId === "string" && nestedId.trim()) return nestedId;
  }
  return null;
}

export function getCancelledAssistantPayload(
  message: WebSocketSessionMessage,
): CancelledAssistantPayload | null {
  const record = messageRecord(message);
  return cancelledAssistantPayloadFromRecord(record, message);
}

function cancelledAssistantPayloadFromRecord(
  record: Record<string, unknown>,
  envelope: WebSocketSessionMessage,
): CancelledAssistantPayload | null {
  const messageId = record.id ?? record.message_id;
  if (typeof messageId !== "string" || !messageId.trim()) {
    return null;
  }
  const metadata =
    record.metadata &&
    typeof record.metadata === "object" &&
    !Array.isArray(record.metadata)
      ? { ...(record.metadata as Record<string, unknown>) }
      : {};
  const metadataAgentRunId = metadata.agent_run_id;
  const agentRunId =
    typeof metadataAgentRunId === "string" && metadataAgentRunId.trim()
      ? metadataAgentRunId
      : getWebSocketMessageAgentRunId(envelope);
  if (agentRunId) {
    metadata.agent_run_id = agentRunId;
  }
  metadata.generation_status = "cancelled";
  metadata.partial = true;
  return {
    messageId,
    content: typeof record.content === "string" ? record.content : "",
    agentRunId,
    metadata,
  };
}

export function getCancelledAssistantPayloads(
  message: WebSocketSessionMessage,
): CancelledAssistantPayload[] {
  const record = messageRecord(message);
  if (Array.isArray(record.messages)) {
    const payloads = record.messages
      .map((item) =>
        item && typeof item === "object" && !Array.isArray(item)
          ? cancelledAssistantPayloadFromRecord(
              item as Record<string, unknown>,
              message,
            )
          : null,
      )
      .filter((item): item is CancelledAssistantPayload => item !== null);
    if (payloads.length > 0) return payloads;
  }
  const fallback = getCancelledAssistantPayload(message);
  return fallback ? [fallback] : [];
}

export function isWebSocketMessageForSession(
  message: WebSocketSessionMessage,
  sessionId: string | null,
): boolean {
  const record = messageRecord(message);
  const eventType = typeof record.type === "string" ? record.type : "";
  const eventSessionId = getWebSocketMessageSessionId(message);
  // A session-scoped event without an explicit matching session is unsafe.
  if (eventSessionId) {
    return Boolean(sessionId && eventSessionId === sessionId);
  }
  // Keep this allowlist explicit.  An unknown unscoped event must not be
  // treated as global because that would reintroduce cross-session leakage.
  return GLOBAL_WEBSOCKET_EVENT_TYPES.has(eventType);
}

export function isTerminalGenerationEvent(
  message: WebSocketSessionMessage,
): boolean {
  const record = messageRecord(message);
  if (record.type === "stream_end" || record.type === "response") return true;
  return (
    record.type === "stream_cancelled" &&
    (record.status == null ||
      record.status === "cancelled" ||
      record.status === "cancellation_failed")
  );
}
