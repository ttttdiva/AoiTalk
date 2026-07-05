/**
 * チャットAPI クライアント
 * /api/ 経由（Next.js Route Handler）
 */

import type { ChatCommandCapability } from "@/lib/chat-commands";
export type { ChatCommandCapability } from "@/lib/chat-commands";

// ─── 型定義 ───

export type ConversationSession = {
  id: string;
  user_id: string;
  character_name: string;
  title: string;
  session_start?: string | null;
  last_activity?: string | null;
  message_count: number;
  is_active: boolean;
  project_id?: string | null;
  is_group_chat?: boolean;
  group_character_names?: string[];
  participants?: ConversationParticipant[];
  rp_settings?: Record<string, number>;
};

export type ConversationParticipant = {
  id: string;
  session_id: string;
  participant_type: "user" | "character" | "agent" | string;
  participant_id: string;
  display_name?: string | null;
  role?: string | null;
  status?: string | null;
  auto_respond?: boolean;
  metadata?: Record<string, unknown>;
};

export type ChatAttachmentKind =
  | "wbs"
  | "issue"
  | "risk"
  | "request"
  | "attachment";

export type ChatAttachmentMetadata = {
  name: string;
  path?: string;
  project_relative_path?: string;
  kind?: ChatAttachmentKind;
  registered?: boolean;
  size?: number;
  mime_type?: string;
  upload_failed?: boolean;
  error?: string;
};

export type ConversationMessageMetadata = Record<string, unknown> & {
  client_message_id?: string;
  agent_run_id?: string;
  attachments?: ChatAttachmentMetadata[];
  command_capabilities?: ChatCommandCapability[];
  generation_metrics?: ChatGenerationMetrics;
  response_elapsed_ms?: number;
  has_image?: boolean;
  image_mime_type?: string | null;
  image_name?: string | null;
  tool_results?: ChatToolResultMetadata[];
};

export type ChatGenerationMetrics = {
  provider?: string;
  model?: string;
  tokens_per_second?: number;
  output_tokens?: number;
  prompt_tokens?: number;
  total_tokens?: number;
  generation_ms?: number;
  prompt_ms?: number;
};

export type ChatToolResultMetadata = {
  tool?: string;
  query?: string | null;
  urls?: string[];
  output?: string;
  truncated?: boolean;
};

export type ConversationMessage = {
  id: string;
  session_id: string;
  role: "user" | "assistant" | "system";
  content: string;
  metadata: ConversationMessageMetadata;
  sender_type?: string | null;
  sender_id?: string | null;
  sender_display_name?: string | null;
  created_at?: string | null;
  parent_message_id?: string | null;
  branch_index: number;
  is_active_branch: boolean;
};

export type ChatResponseModelSelection = {
  provider: string;
  model: string;
};

export type ChatResponseModelOption = ChatResponseModelSelection & {
  label: string;
  providerLabel: string;
  modelLabel: string;
  isCurrent?: boolean;
};

export type LlmCatalogModelOption = {
  id: string;
  label: string;
  description?: string;
  installed?: boolean;
  source?: string;
  source_label?: string;
  provider_configured?: boolean;
  custom_current?: boolean;
};

export type LlmCatalogProvider = {
  id: string;
  label: string;
  configured_model?: string;
  models: LlmCatalogModelOption[];
  settings?: {
    api_key_configured?: boolean;
  };
};

export type LlmModelCatalogResponse = {
  current: ChatResponseModelSelection;
  providers: LlmCatalogProvider[];
};

export type ConversationSearchResult = {
  id: string;
  match_type: "message" | "session";
  session_id: string;
  message_id?: string | null;
  title: string;
  character_name: string;
  role?: "user" | "assistant" | "system" | string | null;
  snippet: string;
  created_at?: string | null;
  last_activity?: string | null;
  project_id?: string | null;
};

export type ConversationGenerationStatus = {
  success?: boolean;
  session_id: string | null;
  running: boolean;
  status: string;
  message?: string | null;
  active_tool?: string | null;
  agent_run_id?: string | null;
  started_at?: string | null;
  updated_at?: string | null;
};

export type AgentRunTimelineItem = {
  id: string;
  source: "event" | "tool_call" | string;
  run_id: string;
  event_id?: string | null;
  sequence?: number | null;
  event_type?: string | null;
  status?: string | null;
  display_status?: string | null;
  actor_type?: string | null;
  actor_key?: string | null;
  actor_label?: string | null;
  provider?: string | null;
  model?: string | null;
  mode?: string | null;
  action: string;
  message?: string | null;
  tool_name?: string | null;
  raw_tool_name?: string | null;
  tool_call_id?: string | null;
  arguments?: Record<string, unknown>;
  result_preview?: string | null;
  success?: boolean;
  mutation_confirmed?: boolean;
  duration_ms?: number | null;
  payload?: Record<string, unknown>;
  created_at?: string | null;
  started_at?: string | null;
  ended_at?: string | null;
};

export type AgentRunTimelineColumn = {
  key: string;
  label: string;
  actor_type?: string | null;
  provider?: string | null;
  model?: string | null;
  items: AgentRunTimelineItem[];
};

export type AgentRun = {
  id: string;
  root_run_id?: string | null;
  parent_run_id?: string | null;
  session_id?: string | null;
  project_id?: string | null;
  user_id?: string | null;
  run_type: string;
  status: string;
  title?: string;
  objective?: string;
  generation_profile?: string | null;
  provider?: string | null;
  model?: string | null;
  error?: string | null;
  metadata?: Record<string, unknown>;
  created_at?: string | null;
  updated_at?: string | null;
  started_at?: string | null;
  ended_at?: string | null;
  last_event_at?: string | null;
  timeline?: AgentRunTimelineItem[];
  timeline_columns?: AgentRunTimelineColumn[];
};

export type ScenarioLogType = "writing" | "roleplay" | "trpg";

export type ScenarioLogEntry = {
  id: string;
  type: ScenarioLogType;
  type_label: string;
  scenario_id: string;
  conversation_session_id?: string | null;
  room_id?: string | null;
  target_id?: string | null;
  target_label: string;
  title: string;
  status: string;
  count: number;
  created_at?: string | null;
  updated_at?: string | null;
  href?: string | null;
};

export type ScenarioLogResponse = {
  scenario: {
    id: string;
    title: string;
    scenario_kind?: "writing" | "trpg" | string;
  };
  logs: ScenarioLogEntry[];
  count: number;
  active_log_id?: string | null;
  active_log_type?: ScenarioLogType | null;
};

// ─── ベースリクエスト関数 ───

type ChatRequestInit = RequestInit & {
  retries?: number;
  retryDelayMs?: number;
  timeoutMs?: number;
};

class ChatApiError extends Error {
  status?: number;

  constructor(message: string, status?: number) {
    super(message);
    this.name = "ChatApiError";
    this.status = status;
  }
}

const RETRYABLE_STATUSES = new Set([408, 425, 429, 500, 502, 503, 504]);

function sleep(ms: number) {
  return new Promise<void>((resolve) => setTimeout(resolve, ms));
}

function isRetryableError(error: unknown) {
  if (error instanceof ChatApiError) {
    return error.status != null && RETRYABLE_STATUSES.has(error.status);
  }
  if (error instanceof DOMException) {
    return error.name === "AbortError" || error.name === "TimeoutError";
  }
  return error instanceof TypeError;
}

async function requestOnce<T>(
  path: string,
  init?: RequestInit,
  timeoutMs = 5000,
): Promise<T> {
  const res = await fetch(path, {
    ...init,
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...init?.headers,
    },
    signal: init?.signal ?? AbortSignal.timeout(timeoutMs),
  });

  if (res.status === 401) {
    const shouldRedirectToLogin = !path.startsWith("/api/python-proxy/");
    if (shouldRedirectToLogin && typeof window !== "undefined") {
      window.location.href = "/login";
    }
    throw new Error("認証が必要です");
  }
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new ChatApiError(
      `Chat API error: ${res.status} ${res.statusText} - ${text}`,
      res.status,
    );
  }

  // DELETE等で空レスポンスの場合
  const contentType = res.headers.get("content-type");
  if (!contentType || !contentType.includes("application/json")) {
    return undefined as T;
  }

  return res.json() as Promise<T>;
}

async function request<T>(path: string, init?: ChatRequestInit): Promise<T> {
  const {
    retries = 0,
    retryDelayMs = 500,
    timeoutMs = 5000,
    ...requestInit
  } = init ?? {};
  let lastError: unknown;

  for (let attempt = 0; attempt <= retries; attempt += 1) {
    try {
      return await requestOnce<T>(path, requestInit, timeoutMs);
    } catch (error) {
      lastError = error;
      if (attempt >= retries || !isRetryableError(error)) {
        throw error;
      }
      await sleep(retryDelayMs * 2 ** attempt);
    }
  }

  throw lastError;
}

export type LlmMode = string;

export type LlmModeResponse = {
  mode: LlmMode;
  available_modes?: LlmMode[];
  labels?: Record<string, string>;
  kind?: string;
  provider?: string;
  model?: string;
  success?: boolean;
  message?: string;
};

const LLM_MODE_API_PATH = "/api/python-proxy/llm/mode";

export function getLlmMode() {
  return request<LlmModeResponse>(LLM_MODE_API_PATH);
}

export function setLlmMode(mode: LlmMode) {
  return request<LlmModeResponse>(LLM_MODE_API_PATH, {
    method: "POST",
    body: JSON.stringify({ mode }),
  });
}

export function getLlmModelCatalog() {
  return request<LlmModelCatalogResponse>("/api/python-proxy/llm/models", {
    cache: "no-store",
  });
}

// ─── API関数 ───

export const chatApi = {
  /** セッション一覧取得 */
  listSessions: (projectId?: string) =>
    request<{ conversations: ConversationSession[] }>(
      `/api/conversations${projectId ? `?project_id=${projectId}` : ""}`,
      { retries: 3, retryDelayMs: 500 },
    ),

  searchConversations: (query: string, projectId?: string | null) => {
    const params = new URLSearchParams({ q: query });
    if (projectId) params.set("project_id", projectId);
    return request<{
      results: ConversationSearchResult[];
      total: number;
    }>(`/api/conversations/search?${params.toString()}`, {
      retries: 1,
      retryDelayMs: 300,
      timeoutMs: 8000,
    });
  },

  /** 新規セッション作成 */
  createSession: (
    characterName: string,
    projectId?: string,
    initialMessage?: {
      content: string;
      client_message_id?: string;
    },
  ) =>
    request<{
      session: ConversationSession;
      initial_message?: ConversationMessage;
    }>("/api/conversations", {
      method: "POST",
      body: JSON.stringify({
        character_name: characterName,
        project_id: projectId,
        initial_message: initialMessage,
      }),
    }),

  /** セッションのメッセージ一覧取得 */
  getMessages: (sessionId: string) =>
    request<{ messages: ConversationMessage[] }>(
      `/api/conversations/${sessionId}/messages`,
      {
        cache: "no-store",
        headers: { "Cache-Control": "no-cache" },
        retries: 2,
        retryDelayMs: 500,
      },
    ),

  /** セッションにメッセージを追加 */
  addMessage: (
    sessionId: string,
    data: {
      role: "user" | "assistant";
      content: string;
    },
  ) =>
    request<{ success: boolean; message: ConversationMessage }>(
      `/api/conversations/${sessionId}/messages`,
      {
        method: "POST",
        body: JSON.stringify(data),
        signal: AbortSignal.timeout(15000),
      },
    ),

  /** セッション再開（メッセージ付き） */
  resumeSession: (sessionId: string) =>
    request<{ session: ConversationSession; messages: ConversationMessage[] }>(
      `/api/conversations/${sessionId}/resume`,
      {
        method: "POST",
        retries: 3,
        retryDelayMs: 500,
        timeoutMs: 15000,
      },
    ),

  /** セッション削除 */
  dispatchMessage: (
    sessionId: string,
    data: {
      message: string;
      project_id?: string;
      generation_profile?: string;
      include_project_context?: boolean;
      edit_message_id?: string;
      response_model?: ChatResponseModelSelection;
      client_message_id?: string;
      command_capabilities?: ChatCommandCapability[];
      skip_user_persistence?: boolean;
      persisted_user_message_id?: string;
    },
  ) =>
    request<{
      success: boolean;
      queued: boolean;
      session_id: string;
      user_message_id?: string | null;
      agent_run_id?: string | null;
    }>(`/api/python-proxy/conversations/${sessionId}/dispatch`, {
      method: "POST",
      keepalive: true,
      body: JSON.stringify(data),
    }),

  getAgentRun: (runId: string) =>
    request<{ success: boolean; agent_run: AgentRun }>(
      `/api/python-proxy/agent-runs/${runId}?include_events=false&include_tool_calls=false&include_timeline=true`,
      {
        cache: "no-store",
        headers: { "Cache-Control": "no-cache" },
        retries: 1,
        retryDelayMs: 300,
        timeoutMs: 10000,
      },
    ),

  stopGeneration: (sessionId: string) =>
    request<{ success: boolean; cancelled: number; session_id: string }>(
      `/api/python-proxy/conversations/${sessionId}/generation/stop`,
      {
        method: "POST",
      },
    ),

  getGenerationStatus: (sessionId: string) =>
    request<ConversationGenerationStatus>(
      `/api/python-proxy/conversations/${sessionId}/generation/status`,
      {
        cache: "no-store",
        headers: { "Cache-Control": "no-cache" },
        retries: 1,
        retryDelayMs: 300,
      },
    ),

  steerGeneration: (sessionId: string, message: string) =>
    request<{ success: boolean; queued: boolean; session_id: string }>(
      `/api/python-proxy/conversations/${sessionId}/generation/steer`,
      {
        method: "POST",
        body: JSON.stringify({ message }),
      },
    ),

  deleteSession: (sessionId: string) =>
    request<void>(`/api/conversations/${sessionId}`, {
      method: "DELETE",
    }),

  /** セッションタイトル更新 */
  updateSessionTitle: (sessionId: string, title: string) =>
    request<{ success: boolean }>(`/api/conversations/${sessionId}`, {
      method: "PATCH",
      body: JSON.stringify({ title }),
    }),

  /** 初回会話文脈からセッションタイトルを生成 */
  generateSessionTitle: (sessionId: string) =>
    request<{
      success: boolean;
      title: string;
      generated: boolean;
      source?: "llm" | "fallback" | null;
    }>(`/api/python-proxy/conversations/${sessionId}/generate-title`, {
      method: "POST",
      timeoutMs: 30000,
    }),

  // ─── ブランチング関連API ───

  /** メッセージ編集（新ブランチ作成） */
  editMessage: (sessionId: string, messageId: string, content: string) =>
    request<{ message: ConversationMessage }>(
      `/api/python-proxy/conversations/${sessionId}/messages/${messageId}`,
      {
        method: "PUT",
        body: JSON.stringify({ content }),
        signal: AbortSignal.timeout(15000),
      },
    ),

  /** メッセージのブランチ一覧取得（兄弟ブランチ） */
  getMessageBranches: (sessionId: string, messageId: string) =>
    request<{ branches: ConversationMessage[] }>(
      `/api/python-proxy/conversations/${sessionId}/messages/${messageId}/branches`,
    ),

  /** ブランチ切替 */
  switchBranch: (sessionId: string, messageId: string, branchIndex: number) =>
    request<{ success: boolean }>(
      `/api/python-proxy/conversations/${sessionId}/messages/${messageId}/switch-branch`,
      {
        method: "POST",
        body: JSON.stringify({ branch_index: branchIndex }),
      },
    ),

  /** アクティブメッセージパス取得 */
  getActiveMessages: (sessionId: string) =>
    request<{ messages: ConversationMessage[] }>(
      `/api/python-proxy/conversations/${sessionId}/active-messages`,
    ),

  // ─── グループチャット関連API ───

  /** グループセッション作成 */
  createGroupSession: (
    characterNames: string[],
    projectId?: string,
    userIds?: string[],
    agentIds?: string[],
  ) =>
    request<{
      session: ConversationSession;
      first_messages?: Array<{
        character_slug: string;
        character_name: string;
        content: string;
      }>;
    }>("/api/python-proxy/conversations/group", {
      method: "POST",
      body: JSON.stringify({
        character_names: characterNames,
        user_ids: userIds ?? [],
        agent_ids: agentIds ?? [],
        project_id: projectId,
      }),
    }),

  /** グループ応答 */
  groupRespond: (sessionId: string, message: string, strategy?: string) =>
    request<{
      responses: Array<{
        character_slug: string;
        character_name: string;
        content: string;
      }>;
    }>(`/api/python-proxy/conversations/${sessionId}/group-respond`, {
      method: "POST",
      body: JSON.stringify({ message, strategy }),
      signal: AbortSignal.timeout(30000),
    }),

  // ─── RPスライダー関連API ───

  /** RP設定取得 */
  getRpSettings: (sessionId: string) =>
    request<{ rp_settings: Record<string, number> }>(
      `/api/conversations/${sessionId}/rp-settings`,
    ),

  /** RP設定更新 */
  updateRpSettings: (sessionId: string, settings: Record<string, number>) =>
    request<{ success: boolean }>(
      `/api/conversations/${sessionId}/rp-settings`,
      {
        method: "PUT",
        body: JSON.stringify(settings),
      },
    ),

  // ─── シナリオ関連API ───

  /** シナリオ詳細を取得 */
  getScenario: (scenarioId: string) =>
    request<{
      id: string;
      title: string;
      description?: string;
      characters?: Array<{
        id: string;
        name: string;
        role?: string;
        description?: string;
      }>;
    }>(`/api/python-proxy/scenarios/${scenarioId}`),

  /** シナリオに紐づくログ一覧を取得 */
  getScenarioLogs: (scenarioId: string) =>
    request<ScenarioLogResponse>(
      `/api/python-proxy/scenarios/${scenarioId}/logs`,
    ),

  /** 会話セッションIDからシナリオログ文脈を取得 */
  getScenarioLogContextByConversation: (convSessionId: string) =>
    request<ScenarioLogResponse | null>(
      `/api/python-proxy/scenarios/logs/by-conversation/${convSessionId}`,
    ),

  /** 会話セッションIDからプレイセッション情報を取得 */
  getScenarioPlaySessionByConversation: (convSessionId: string) =>
    request<{
      id: string;
      scenario_id: string;
      conversation_session_id: string;
      current_scene_id: string | null;
      player_state: Record<string, unknown>;
      perspective: string;
      status: string;
      scenario?: {
        id: string;
        title: string;
        description: string;
      };
      current_scene?: {
        id: string;
        title: string;
        description: string;
      };
    } | null>(`/api/python-proxy/scenarios/by-conversation/${convSessionId}`),

  /** 会話セッションIDから執筆セッション情報を取得 */
  getWritingSessionByConversation: (convSessionId: string) =>
    request<{
      id: string;
      scenario_id: string;
      conversation_session_id: string;
      target_scene_id: string;
      status: string;
      scenario?: {
        id: string;
        title: string;
      };
      target_scene?: {
        id: string;
        title: string;
      };
      target_episode?: {
        id: string;
        title: string;
      };
    } | null>(
      `/api/python-proxy/scenarios/write/by-conversation/${convSessionId}`,
    ),
};
