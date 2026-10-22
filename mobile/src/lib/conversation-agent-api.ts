import { fetchApi } from "./api-client";
import { CHAT_TIMEOUT } from "../constants/config";
import type { AgentRun, ConversationAgent, ConversationSession } from "../types/api";

/**
 * Conversation Agent endpoints are deliberately separate from general sync.
 * Agent IDs are ACL checked by the server and must never be written through a
 * raw general-sync UUID update.
 */
export const conversationAgentApi = {
  async listWithMeta(options: {
    sessionId?: string | null;
    projectId?: string | null;
  } = {}): Promise<{ agents: ConversationAgent[]; complete: boolean }> {
    const query = new URLSearchParams({ limit: "200" });
    if (options.sessionId?.trim()) query.set("session_id", options.sessionId.trim());
    if (options.projectId?.trim()) query.set("project_id", options.projectId.trim());
    const suffix = `?${query.toString()}`;
    const response = await fetchApi<{ agents?: unknown; complete?: unknown; has_more?: unknown }>(
      `/api/conversation-agents${suffix}`,
      {},
      CHAT_TIMEOUT,
    );
    const agents = normalizeAgents(response.agents);
    // The current server response has no explicit completion bit. The route
    // caps limit at 200, so a short page (including empty) is complete while
    // a full page must retain prior candidates until a future complete flag or
    // pagination contract is available.
    const complete = response.complete === true
      || response.has_more === false
      || agents.length < 200;
    return { agents, complete };
  },

  async list(options: {
    sessionId?: string | null;
    projectId?: string | null;
  } = {}): Promise<ConversationAgent[]> {
    return (await this.listWithMeta(options)).agents;
  },

  async listConversations(agentId: string): Promise<ConversationSession[]> {
    const response = await fetchApi<{ sessions?: ConversationSession[] }>(
      `/api/agents/${encodeURIComponent(agentId)}/conversations`,
      {},
      CHAT_TIMEOUT,
    );
    return Array.isArray(response.sessions) ? response.sessions : [];
  },

  async listRuns(agentId: string): Promise<AgentRun[]> {
    const response = await fetchApi<{ runs?: AgentRun[] }>(
      `/api/agents/${encodeURIComponent(agentId)}/agent-runs`,
      {},
      CHAT_TIMEOUT,
    );
    return Array.isArray(response.runs) ? response.runs : [];
  },

  async bindSession(
    sessionId: string,
    agentId: string | null,
    expectedAgentId: string | null,
  ): Promise<ConversationSession> {
    const response = await fetchApi<{ session: ConversationSession }>(
      `/api/conversations/${encodeURIComponent(sessionId)}/agent`,
      {
        method: "PATCH",
        body: JSON.stringify({
          agent_id: agentId,
          expected_agent_id: expectedAgentId,
        }),
      },
      CHAT_TIMEOUT,
    );
    return response.session;
  },
};

function text(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function normalizeAgents(value: unknown): ConversationAgent[] {
  if (!Array.isArray(value)) return [];
  return value.slice(0, 500).flatMap((candidate): ConversationAgent[] => {
    if (!candidate || typeof candidate !== "object" || Array.isArray(candidate)) {
      return [];
    }
    const row = candidate as Record<string, unknown>;
    const id = text(row.id);
    const displayName = text(row.display_name ?? row.name) ?? id;
    if (!id || !displayName) return [];
    const utilization = row.utilization && typeof row.utilization === "object" && !Array.isArray(row.utilization)
      ? row.utilization as ConversationAgent["utilization"]
      : null;
    return [{
      id,
      display_name: displayName,
      state: text(row.state),
      latest_revision_id: text(row.latest_revision_id ?? row.revision_id),
      character_id: text(row.character_id),
      character_name: text(row.character_name),
      updated_at: text(row.updated_at),
      utilization,
    }];
  });
}
