import type { AgentRun, ConversationSession } from "@/lib/chat-api";

/** Public Agent identity used by the Chat session selector. */
export type ConversationAgent = {
  id: string;
  display_name: string;
  slug?: string | null;
  state?: string | null;
  latest_revision_id?: string | null;
  latest_revision_version?: number | null;
  character_id?: string | null;
  created_by?: string | null;
};

export type ConversationAgentBinding = {
  id?: string | null;
  session_id: string;
  agent_id: string;
  agent_revision_id: string;
  revision_version?: number | null;
  display_name?: string | null;
  agent_team_id?: string | null;
  execution_profile_id?: string | null;
  created_at?: string | null;
};

export type ConversationAgentErrorCode =
  | "agent_binding_stale"
  | "agent_binding_busy"
  | "agent_not_found"
  | "agent_inactive"
  | "agent_revision_missing"
  | "conversation_not_found"
  | "conversation_manage_denied"
  | "project_write_denied"
  | "project_read_denied"
  | string;

export class ConversationAgentApiError extends Error {
  readonly status: number;
  readonly code: ConversationAgentErrorCode;

  constructor(status: number, code: ConversationAgentErrorCode, message?: string) {
    super(message || code || "Agent API request failed");
    this.name = "ConversationAgentApiError";
    this.status = status;
    this.code = code;
  }
}

type ConversationAgentRequestInit = RequestInit & { signal?: AbortSignal };

function errorCode(payload: unknown): ConversationAgentErrorCode {
  if (!payload || typeof payload !== "object") return "conversation_agent_error";
  const record = payload as { code?: unknown; detail?: unknown; error?: unknown };
  if (typeof record.code === "string" && record.code.trim()) return record.code;
  if (record.detail && typeof record.detail === "object") {
    const nested = record.detail as { code?: unknown };
    if (typeof nested.code === "string" && nested.code.trim()) return nested.code;
  }
  if (record.error && typeof record.error === "object") {
    const nested = record.error as { code?: unknown };
    if (typeof nested.code === "string" && nested.code.trim()) return nested.code;
  }
  return "conversation_agent_error";
}

function errorMessage(payload: unknown, status: number): string {
  if (payload && typeof payload === "object") {
    const record = payload as { detail?: unknown; message?: unknown; error?: unknown };
    if (typeof record.message === "string" && record.message.trim()) return record.message;
    if (typeof record.detail === "string" && record.detail.trim()) return record.detail;
    if (record.detail && typeof record.detail === "object") {
      const nested = record.detail as { message?: unknown };
      if (typeof nested.message === "string" && nested.message.trim()) return nested.message;
    }
    if (record.error && typeof record.error === "object") {
      const nested = record.error as { message?: unknown };
      if (typeof nested.message === "string" && nested.message.trim()) return nested.message;
    }
  }
  if (status === 401) return "ログインが必要です";
  if (status === 403) return "このAgentを使用する権限がありません";
  if (status === 404) return "Agentまたは会話が見つかりません";
  if (status === 409) return "Agentの選択が競合しました。再読み込みしてください";
  return "Agent情報を取得できませんでした";
}

async function request<T>(path: string, init?: ConversationAgentRequestInit): Promise<T> {
  const requestPath = path.startsWith("/api/") ? path : `/api/python-proxy${path}`;
  const response = await fetch(requestPath, {
    ...init,
    credentials: "include",
    cache: "no-store",
    headers: {
      "Content-Type": "application/json",
      ...init?.headers,
    },
  });
  const raw = await response.text();
  let payload: unknown = null;
  try {
    payload = raw ? JSON.parse(raw) : null;
  } catch {
    payload = null;
  }
  if (!response.ok) {
    throw new ConversationAgentApiError(
      response.status,
      errorCode(payload),
      errorMessage(payload, response.status),
    );
  }
  return (payload ?? {}) as T;
}

function encoded(value: string): string {
  return encodeURIComponent(value);
}

export const conversationAgentApi = {
  list: (options?: {
    sessionId?: string | null;
    projectId?: string | null;
    signal?: AbortSignal;
  }) => {
    const query = new URLSearchParams();
    if (options?.sessionId) query.set("session_id", options.sessionId);
    if (options?.projectId) query.set("project_id", options.projectId);
    const suffix = query.toString() ? `?${query.toString()}` : "";
    return request<{ success?: boolean; agents: ConversationAgent[] }>(
      `/conversation-agents${suffix}`,
      { signal: options?.signal },
    );
  },

  bindSessionAgent: (
    sessionId: string,
    agentId: string | null,
    expectedAgentId: string | null,
  ) =>
    request<{
      success: boolean;
      session: ConversationSession;
      binding: ConversationAgentBinding | null;
    }>(`/api/conversations/${encoded(sessionId)}/agent`, {
      method: "PATCH",
      body: JSON.stringify({
        // Keep null explicit so clearing a session binding is distinguishable
        // from a client that omitted the field.
        agent_id: agentId,
        expected_agent_id: expectedAgentId,
      }),
    }),

  /** Role runtime defaults only; never used to mutate the Agent revision. */
  getAgent: (agentId: string, signal?: AbortSignal) =>
    request<{ success?: boolean; agent?: ConversationAgent | null }>(
      `/agents/${encoded(agentId)}`,
      { signal },
    ),

  getAgentRevision: (revisionId: string, signal?: AbortSignal) =>
    request<{
      success?: boolean;
      revision?: {
        id?: string;
        agent_id?: string;
        agent_team_id?: string | null;
        execution_profile_id?: string | null;
      } | null;
    }>(`/agent-revisions/${encoded(revisionId)}`, { signal }),

  listAgentConversations: (agentId: string, signal?: AbortSignal) =>
    request<{
      success?: boolean;
      agent_id?: string;
      sessions?: ConversationSession[];
      conversations?: ConversationSession[];
    }>(`/agents/${encoded(agentId)}/conversations`, { signal }),

  listAgentRuns: (
    agentId: string,
    options?: { status?: string; limit?: number; signal?: AbortSignal },
  ) => {
    const query = new URLSearchParams();
    if (options?.status) query.set("status", options.status);
    if (options?.limit) query.set("limit", String(options.limit));
    const suffix = query.toString() ? `?${query.toString()}` : "";
    return request<{
      success?: boolean;
      agent_id?: string;
      agent_runs?: AgentRun[];
      runs?: AgentRun[];
    }>(`/agents/${encoded(agentId)}/agent-runs${suffix}`, {
      signal: options?.signal,
    });
  },
};

export function unwrapAgentConversations(
  response: Awaited<ReturnType<typeof conversationAgentApi.listAgentConversations>>,
): ConversationSession[] {
  return Array.isArray(response.sessions)
    ? response.sessions
    : Array.isArray(response.conversations)
      ? response.conversations
      : [];
}
export function unwrapAgentRuns(
  response: Awaited<ReturnType<typeof conversationAgentApi.listAgentRuns>>,
): AgentRun[] {
  return Array.isArray(response.agent_runs)
    ? response.agent_runs
    : Array.isArray(response.runs)
      ? response.runs
      : [];
}

