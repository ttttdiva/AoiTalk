import type { AgentRun, ConversationSession } from "@/lib/chat-api";
import { readFileTransferCompatibilityPreference } from "@/lib/file-transfer-preference";

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
  legacy?: { agent_team_id?: string | null; execution_profile_id?: string | null } | null;
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

const RETRYABLE_STATUSES = new Set([408, 425, 429, 500, 502, 503, 504]);
const TRANSFER_COMPATIBILITY_READ_RETRIES = 2;
const TRANSFER_COMPATIBILITY_RETRY_DELAY_MS = 300;
const TRANSFER_COMPATIBILITY_READ_TIMEOUT_MS = 10_000;
const MAX_ERROR_BODY_LENGTH = 32 * 1024;
const MAX_ERROR_MESSAGE_LENGTH = 512;

function boundedErrorText(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const text = value.trim();
  return text && text.length <= MAX_ERROR_MESSAGE_LENGTH ? text : null;
}

function isJsonContentType(contentType: string | null): boolean {
  if (!contentType) return true;
  const mediaType = (contentType.split(";", 1)[0] ?? "").trim().toLowerCase();
  return mediaType === "application/json" || mediaType === "text/json" || mediaType.endsWith("+json");
}

function errorCode(payload: unknown): ConversationAgentErrorCode {
  if (!payload || typeof payload !== "object") return "conversation_agent_error";
  const record = payload as { code?: unknown; detail?: unknown; error?: unknown };
  if (typeof record.code === "string" && record.code.trim()) return record.code.slice(0, 128);
  if (record.detail && typeof record.detail === "object") {
    const nested = record.detail as { code?: unknown };
    if (typeof nested.code === "string" && nested.code.trim()) return nested.code.slice(0, 128);
  }
  if (record.error && typeof record.error === "object") {
    const nested = record.error as { code?: unknown };
    if (typeof nested.code === "string" && nested.code.trim()) return nested.code.slice(0, 128);
  }
  return "conversation_agent_error";
}

function errorMessage(payload: unknown, status: number): string {
  if (payload && typeof payload === "object") {
    const record = payload as { detail?: unknown; message?: unknown; error?: unknown };
    const message = boundedErrorText(record.message);
    if (message) return message;
    const detail = boundedErrorText(record.detail);
    if (detail) return detail;
    if (record.detail && typeof record.detail === "object") {
      const nested = record.detail as { message?: unknown };
      const nestedMessage = boundedErrorText(nested.message);
      if (nestedMessage) return nestedMessage;
    }
    if (record.error && typeof record.error === "object") {
      const nested = record.error as { message?: unknown };
      const nestedMessage = boundedErrorText(nested.message);
      if (nestedMessage) return nestedMessage;
    }
  }
  if (status === 401) return "ログインが必要です";
  if (status === 403) return "このAgentを使用する権限がありません";
  if (status === 404) return "Agentまたは会話が見つかりません";
  if (status === 409) return "Agentの選択が競合しました。再読み込みしてください";
  return "Agent情報を取得できませんでした";
}

function createAbortError(): Error {
  if (typeof DOMException !== "undefined") {
    return new DOMException("The operation was aborted", "AbortError");
  }
  const error = new Error("The operation was aborted");
  error.name = "AbortError";
  return error;
}

function createTimeoutError(): Error {
  if (typeof DOMException !== "undefined") {
    return new DOMException("The operation timed out", "TimeoutError");
  }
  const error = new Error("The operation timed out");
  error.name = "TimeoutError";
  return error;
}

type CompatibilityReadTimeout = {
  signal: AbortSignal;
  error: Error;
  didTimeout: () => boolean;
  dispose: () => void;
};

function startCompatibilityReadTimeout(): CompatibilityReadTimeout {
  const controller = new AbortController();
  const error = createTimeoutError();
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort(error);
  }, TRANSFER_COMPATIBILITY_READ_TIMEOUT_MS);
  return {
    signal: controller.signal,
    error,
    didTimeout: () => timedOut,
    dispose: () => clearTimeout(timer),
  };
}

function sleep(ms: number, signal?: AbortSignal | null) {
  if (signal?.aborted) return Promise.reject(createAbortError());
  return new Promise<void>((resolve, reject) => {
    const timer = setTimeout(() => {
      cleanup();
      resolve();
    }, ms);
    const cleanup = () => {
      clearTimeout(timer);
      signal?.removeEventListener("abort", onAbort);
    };
    const onAbort = () => {
      cleanup();
      reject(createAbortError());
    };
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

function isRetryableError(error: unknown, callerSignal?: AbortSignal | null) {
  if (callerSignal?.aborted) return false;
  if (error instanceof ConversationAgentApiError) {
    return RETRYABLE_STATUSES.has(error.status);
  }
  if (error instanceof DOMException) {
    return error.name === "TimeoutError";
  }
  if (error instanceof Error && error.name === "TimeoutError") {
    return true;
  }
  return error instanceof TypeError;
}

async function readBoundedErrorBody(response: Response): Promise<string> {
  const reader = response.body?.getReader();
  if (!reader) {
    try {
      return (await response.text()).slice(0, MAX_ERROR_BODY_LENGTH);
    } catch {
      return "";
    }
  }

  const decoder = new TextDecoder();
  let text = "";
  try {
    while (text.length < MAX_ERROR_BODY_LENGTH) {
      const { done, value } = await reader.read();
      if (done) {
        text += decoder.decode();
        break;
      }
      text += decoder.decode(value, { stream: true });
      if (text.length >= MAX_ERROR_BODY_LENGTH) {
        text = text.slice(0, MAX_ERROR_BODY_LENGTH);
        await reader.cancel().catch(() => undefined);
        break;
      }
    }
  } catch {
    // Keep any bounded prefix collected before a stream error.
  }
  return text.slice(0, MAX_ERROR_BODY_LENGTH);
}

async function request<T>(path: string, init?: ConversationAgentRequestInit): Promise<T> {
  const requestPath = path.startsWith("/api/") ? path : `/api/python-proxy${path}`;
  const method = (init?.method ?? "GET").toString().toUpperCase();
  const compatibilityMode =
    method === "GET" && readFileTransferCompatibilityPreference();
  const retries = compatibilityMode ? TRANSFER_COMPATIBILITY_READ_RETRIES : 0;
  let lastError: unknown;

  for (let attempt = 0; attempt <= retries; attempt += 1) {
    if (init?.signal?.aborted) throw createAbortError();
    const timeout = compatibilityMode && init?.signal == null
      ? startCompatibilityReadTimeout()
      : null;
    try {
      const response = await fetch(requestPath, {
        ...init,
        ...(timeout ? { signal: timeout.signal } : {}),
        credentials: "include",
        cache: "no-store",
        headers: {
          "Content-Type": "application/json",
          ...init?.headers,
        },
      });
      if (timeout?.didTimeout()) throw timeout.error;
      const contentType = response.headers.get("content-type");
      const raw = response.ok
        ? await response.text()
        : await readBoundedErrorBody(response);
      if (timeout?.didTimeout()) throw timeout.error;
      let payload: unknown = null;
      if (isJsonContentType(contentType)) {
        try {
          payload = raw ? JSON.parse(raw) : null;
        } catch {
          payload = null;
        }
      }
      if (!response.ok) {
        throw new ConversationAgentApiError(
          response.status,
          errorCode(payload),
          errorMessage(payload, response.status),
        );
      }
      return (payload ?? {}) as T;
    } catch (error) {
      const failure = timeout?.didTimeout() ? timeout.error : error;
      lastError = failure;
      if (init?.signal?.aborted) throw createAbortError();
      if (
        attempt >= retries ||
        !isRetryableError(failure, init?.signal)
      ) {
        throw failure;
      }
    } finally {
      timeout?.dispose();
    }

    await sleep(
      TRANSFER_COMPATIBILITY_RETRY_DELAY_MS * 2 ** attempt,
      init?.signal,
    );
  }

  throw lastError;
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
        legacy?: { agent_team_id?: string | null; execution_profile_id?: string | null } | null;
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

