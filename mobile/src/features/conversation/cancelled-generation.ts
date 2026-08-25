import type {
  AgentRunTimelineItem,
  ConversationMessage,
  WSMessage,
} from "../../types/api";

function recordOf(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

export function cancelledAssistantMessage(
  event: WSMessage,
  sessionId: string,
  fallbackContent: string,
): ConversationMessage | null {
  const data = recordOf(event.data);
  const messageId = String(event.message_id ?? data.message_id ?? "").trim();
  if (!messageId) return null;
  const metadata = {
    ...recordOf(data.metadata),
    ...recordOf(event.metadata),
    agent_run_id: String(
      event.agent_run_id ?? data.agent_run_id ?? "",
    ).trim() || undefined,
    generation_status: "cancelled",
    partial: true,
  };
  return {
    id: messageId,
    session_id: sessionId,
    role: "assistant",
    content: String(
      event.content ?? data.content ?? fallbackContent,
    ),
    metadata,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    parent_message_id: null,
    branch_index: 0,
    is_active_branch: true,
  };
}

export function cancelledAssistantMessages(
  event: WSMessage,
  sessionId: string,
  fallbackContent: string,
): ConversationMessage[] {
  const data = recordOf(event.data);
  const rawMessages = Array.isArray(event.messages)
    ? event.messages
    : Array.isArray(data.messages)
      ? data.messages
      : [];
  const messages: ConversationMessage[] = [];
  for (const item of rawMessages) {
    const record = recordOf(item);
    const id = String(record.id ?? record.message_id ?? "").trim();
    if (!id) continue;
    messages.push({
      ...(record as unknown as ConversationMessage),
      id,
      session_id: String(record.session_id ?? sessionId),
      role: "assistant",
      content: String(record.content ?? ""),
      metadata: {
        ...recordOf(record.metadata),
        generation_status: "cancelled",
        partial: true,
      },
    });
  }
  if (messages.length > 0) return messages;
  const fallback = cancelledAssistantMessage(
    event,
    sessionId,
    fallbackContent,
  );
  return fallback ? [fallback] : [];
}

export function isAssistantPersistenceEvent(event: WSMessage): boolean {
  const data = recordOf(event.data);
  return String(event.role ?? data.role ?? "") === "assistant";
}

export function isPublicAgentRunTimelineItem(
  item: AgentRunTimelineItem,
): boolean {
  if (item.source === "tool_call" || item.event_type === "tool_operation") {
    return true;
  }
  const eventType = item.event_type ?? "";
  return (
    eventType === "agent_operation" ||
    eventType.startsWith("agent_team.") ||
    [
      "stream.reasoning_progress",
      "stream.status_update",
      "stream.steering_update",
      "stream.agentic_review",
    ].includes(eventType)
  );
}

export function agentRunTimelineText(item: AgentRunTimelineItem): {
  title: string;
  detail: string;
} {
  const title =
    item.message?.trim() ||
    item.action?.trim() ||
    item.tool_name?.trim() ||
    "作業";
  const detail =
    item.error?.trim() ||
    item.result?.trim() ||
    item.result_preview?.trim() ||
    "";
  return { title, detail };
}
