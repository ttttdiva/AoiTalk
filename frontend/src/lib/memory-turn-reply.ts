import type { ConversationMessage } from "@/lib/chat-api";

/** The previous answer is history, never the answer of a newly dispatched turn. */
export function memoryTurnReply({ messages, streamingContent, agentRunId, busy }: {
  messages: ConversationMessage[]; streamingContent: string; agentRunId: string | null; busy: boolean;
}): string | null {
  if (streamingContent) return streamingContent;
  const latest = messages.filter(message => message.is_active_branch !== false).at(-1);
  if (latest?.role !== "assistant") return null;
  if (latest.metadata?.fork_source_message_id && busy) return null;
  if (agentRunId && latest.metadata?.agent_run_id !== agentRunId) return null;
  if (busy && !agentRunId) return null;
  return latest.content || null;
}
