type WebSocketSessionMessage = {
  session_id?: unknown;
  data?: unknown;
  [key: string]: unknown;
};

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

export function isWebSocketMessageForSession(
  message: WebSocketSessionMessage,
  sessionId: string | null,
): boolean {
  const eventSessionId = getWebSocketMessageSessionId(message);
  return !eventSessionId || !sessionId || eventSessionId === sessionId;
}
