/**
 * チャットAPI クライアント
 * /api/ 経由（Next.js Route Handler）
 */

import type { ChatCommandCapability } from "@/lib/chat-commands";
export type { ChatCommandCapability } from "@/lib/chat-commands";
import type { LlmDeploymentMetadata } from "@/lib/llm-provider-visibility";
export type { LlmDeploymentMetadata } from "@/lib/llm-provider-visibility";
import type { components } from "@/lib/api-types.gen";

// ─── OpenAPI 生成型ユーティリティ ───
// FastAPI の OpenAPI スキーマから openapi-typescript が生成した型。
// backend の Pydantic モデルを唯一の正とするため、`/api/python-proxy/...`
// 経由（= FastAPI 直）のリクエストボディはここから型を引く。
// （`/api/conversations/...` 等の Next.js BFF 経由は OpenAPI 対象外なので触らない）
type Schemas = components["schemas"];

// openapi-typescript は Pydantic のサーバー側デフォルト値を持つフィールドを
// required として出力する。クライアント送信では省略可能なため、指定キーを任意化する。
type OptionalizeDefaults<T, K extends keyof T> = Omit<T, K> &
  Partial<Pick<T, K>>;

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
  /** Public session metadata; privacy_mode is used only for effective-mode display. */
  context?: Record<string, unknown>;
  project_id?: string | null;
  is_group_chat?: boolean;
  app_id?: string | null;
  app_target_id?: string | null;
  development_status?: "working" | "waiting_for_user" | "completed" | null;
  last_read_at?: string | null;
  is_unread?: boolean;
  parent_session_id?: string | null;
  forked_from_message_id?: string | null;
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
  data_url?: string;
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
  generation_status?: "cancelled" | "completed" | "failed" | string;
  partial?: boolean;
  finish_reason?: string;
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

export type ContextMeasurement =
  | "measured"
  | "tokenizer_estimate"
  | "character_estimate"
  | "estimated"
  | "approximate"
  | "unavailable"
  | "unknown";

export type ContextSnapshotCategory = {
  id?: string;
  category?: string;
  label: string;
  tokens?: number | null;
  percentage?: number | null;
  status?: "active" | "deferred";
  measurement?: ContextMeasurement;
  source?: string | null;
  preview?: string | null;
  selection_reason?: string | null;
  duration_ms?: number | null;
  retrieved_chars?: number | null;
  selected_chars?: number | null;
  size_chars?: number | null;
};

export type ContextRequestSnapshot = {
  id?: string;
  request_index?: number;
  /** Number of model requests represented by this persisted snapshot series. */
  request_count?: number;
  /** Number of older requests omitted by backend retention bounds. */
  requests_omitted?: number;
  request_kind?: string;
  captured_at?: string | null;
  created_at?: string | null;
  input_tokens?: number | null;
  context_window_tokens?: number | null;
  remaining_tokens?: number | null;
  percentage?: number | null;
  usage_percent?: number | null;
  provider?: string | null;
  model?: string | null;
  measurement?: ContextMeasurement;
  categories?: ContextSnapshotCategory[];
  components?: ContextSnapshotCategory[];
};

export type ContextSnapshot = ContextRequestSnapshot & {
  message_id?: string;
  session_id?: string;
  /** Optional explicit Main request supplied by newer backends. */
  main?: ContextRequestSnapshot | null;
  requests?: ContextRequestSnapshot[];
};

export type ContextSnapshotResponse = {
  success: boolean;
  status: "available" | "unavailable" | "missing" | string;
  snapshot?: ContextSnapshot | null;
};

/**
 * Resolve the single effective Main request for UI display.
 *
 * `requests` is bounded diagnostic history (retries/tool follow-ups), not a
 * token budget to aggregate. Newer APIs may send an explicit `main` object;
 * legacy APIs already put the effective Main observation at the top level.
 */
export function resolveMainContextSnapshot(
  snapshot?: ContextSnapshot | null,
): ContextRequestSnapshot | null {
  if (!snapshot) return null;
  return snapshot.main ?? snapshot;
}

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
  updated_at?: string | null;
  parent_message_id?: string | null;
  branch_count?: number | null;
  branch_index: number;
  is_active_branch: boolean;
};

// FastAPI の ResponseModelSelection（{provider, model}）と構造一致するため
// 生成型へ委譲する。
export type ChatResponseModelSelection = Schemas["ResponseModelSelection"];

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
  selection_kind?: "static" | "routing_profile";
  routing_profile_id?: string;
  reasoning_effort_options?: string[];
  reasoning_effort_default?: string;
  reasoning_effort_supports_disable?: boolean;
  reasoning_effort_wire?: { transport?: string; path?: string };
  context_window_tokens?: number | null;
  supports_reasoning?: boolean;
};

export type LlmCatalogProvider = {
  id: string;
  label: string;
  /** Backend deployment/profile availability. Omitted on personal/legacy APIs. */
  available?: boolean;
  disabled?: boolean;
  unavailable?: boolean;
  availability_reason?: string | null;
  configured_model?: string;
  models: LlmCatalogModelOption[];
  settings?: {
    api_key_configured?: boolean;
    reasoning_effort?: string | null;
    reasoning_effort_options?: string[];
    reasoning_effort_default?: string | null;
    reasoning_effort_supports_disable?: boolean;
    reasoning_effort_wire?: { transport?: string; path?: string } | null;
  };
  selection_kind?: "static" | "routing_profile";
};

export type LlmModelCatalogResponse = {
  current: ChatResponseModelSelection;
  providers: LlmCatalogProvider[];
  deployment?: LlmDeploymentMetadata | null;
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
  client_message_id?: string | null;
  started_at?: string | null;
  updated_at?: string | null;
};

export type AgentRunUsage = {
  input_tokens?: number | null;
  output_tokens?: number | null;
  cached_tokens?: number | null;
  total_tokens?: number | null;
};

export type AgentRunTimelineItem = {
  id: string;
  source: "event" | "tool_call" | string;
  run_id: string;
  event_id?: string | null;
  sequence?: number | null;
  event_type?: string | null;
  visibility?: "normal" | "audit" | string | null;
  status?: string | null;
  display_status?: string | null;
  actor_type?: string | null;
  actor_key?: string | null;
  actor_label?: string | null;
  provider?: string | null;
  model?: string | null;
  mode?: string | null;
  group_id?: string | null;
  routing_profile?: string | null;
  pool?: string | null;
  credential_profile?: string | null;
  candidate?: string | null;
  quota_pool_ids?: string[];
  fallback_count?: number;
  action: string;
  message?: string | null;
  tool_name?: string | null;
  raw_tool_name?: string | null;
  tool_call_id?: string | null;
  arguments?: Record<string, unknown>;
  result?: string | null;
  result_preview?: string | null;
  error?: string | null;
  success?: boolean;
  mutation_confirmed?: boolean;
  duration_ms?: number | null;
  /** agent_team 集約項目に付く子 run の id（子タイムラインへのドリルダウン用） */
  child_run_id?: string | null;
  payload?: Record<string, unknown>;
  created_at?: string | null;
  started_at?: string | null;
  ended_at?: string | null;
};

/** 生の実行イベント（include_events=true 時のみ含まれる） */
export type AgentRunEvent = {
  id: string;
  run_id: string;
  sequence?: number | null;
  event_type: string;
  visibility?: "normal" | "audit" | string | null;
  status?: string | null;
  message?: string | null;
  payload?: Record<string, unknown>;
  created_at?: string | null;
};

/** 生のツール呼び出し（include_tool_calls=true 時のみ含まれる） */
export type AgentRunToolCall = {
  id: string;
  run_id: string;
  event_id?: string | null;
  tool_name: string;
  tool_call_id?: string | null;
  arguments?: Record<string, unknown>;
  result?: string | null;
  success?: boolean;
  mutation_confirmed?: boolean;
  metadata?: Record<string, unknown>;
  started_at?: string | null;
  ended_at?: string | null;
  duration_ms?: number | null;
  created_at?: string | null;
};

/** Agent Runの成功したタスク/Docs操作をチャット成果物向けに正規化した値 */
export type AgentResourceMutation = {
  resource_type: "task" | "docs_node";
  resource_id: string;
  title: string;
  operation: "created" | "updated" | "moved" | "archived" | "deleted";
  success: boolean;
  project_name?: string | null;
  start_at?: string | null;
  due_date?: string | null;
  end_at?: string | null;
  all_day?: boolean | null;
  updated_at?: string | null;
  occurred_at?: string | null;
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
  result?: Record<string, unknown>;
  validation?: Record<string, unknown>;
  metadata?: Record<string, unknown>;
  created_at?: string | null;
  updated_at?: string | null;
  started_at?: string | null;
  ended_at?: string | null;
  last_event_at?: string | null;
  usage?: AgentRunUsage | null;
  timeline?: AgentRunTimelineItem[];
  events?: AgentRunEvent[];
  tool_calls?: AgentRunToolCall[];
  resource_mutations?: AgentResourceMutation[];
};

export type AgentRunFetchOptions = {
  includeEvents?: boolean;
  includeToolCalls?: boolean;
  includeTimeline?: boolean;
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
  /** ヘッダーで現在選択されているキャラクターを取得 */
  getCurrentCharacterName: async () => {
    const data = await request<{
      current?: unknown;
      characters?: unknown;
    }>("/api/python-proxy/characters", {
      cache: "no-store",
      timeoutMs: 5000,
    });
    const current = typeof data.current === "string" ? data.current.trim() : "";
    const characters = Array.isArray(data.characters)
      ? data.characters.filter(
          (value): value is string =>
            typeof value === "string" && value.trim().length > 0,
        )
      : [];
    if (!current) {
      throw new Error("現在のキャラクターを取得できませんでした");
    }
    if (characters.length === 0 || characters.includes(current)) return current;

    // /api/characters は表示名だけを返す旧互換APIのため、current が
    // canonical slug（例: kotonoha_aoi）になっている場合は詳細一覧で
    // 解決してからセッション作成へ渡す。
    const catalog = await request<{
      characters?: Array<{
        slug?: unknown;
        name?: unknown;
        recognition_aliases?: unknown;
      }>;
    }>("/api/python-proxy/characters/manage?enabled_only=true", {
      cache: "no-store",
      timeoutMs: 5000,
    });
    const key = current.toLocaleLowerCase();
    const match = (
      Array.isArray(catalog.characters) ? catalog.characters : []
    ).find((character) => {
      const candidates = [
        character.slug,
        character.name,
        ...(Array.isArray(character.recognition_aliases)
          ? character.recognition_aliases
          : []),
      ];
      return candidates.some(
        (candidate) =>
          typeof candidate === "string" &&
          candidate.trim().toLocaleLowerCase() === key,
      );
    });
    const slug = typeof match?.slug === "string" ? match.slug.trim() : "";
    if (slug) return slug;
    throw new Error("現在のキャラクターを解決できませんでした");
  },

  /** セッションのキャラクター種別（assistant / roleplay 等）を取得 */
  getCharacterType: async (characterName: string) => {
    const catalog = await request<{
      characters?: Array<{
        slug?: unknown;
        name?: unknown;
        character_type?: unknown;
        recognition_aliases?: unknown;
      }>;
    }>("/api/python-proxy/characters/manage?enabled_only=true", {
      cache: "no-store",
      timeoutMs: 5000,
    });
    const key = characterName.trim().toLocaleLowerCase();
    const match = (
      Array.isArray(catalog.characters) ? catalog.characters : []
    ).find((character) => {
      const candidates = [
        character.slug,
        character.name,
        ...(Array.isArray(character.recognition_aliases)
          ? character.recognition_aliases
          : []),
      ];
      return candidates.some(
        (candidate) =>
          typeof candidate === "string" &&
          candidate.trim().toLocaleLowerCase() === key,
      );
    });
    return typeof match?.character_type === "string"
      ? match.character_type
      : null;
  },

  /** セッション一覧取得 */
  listSessions: (projectId?: string, appId?: string) => {
    const params = new URLSearchParams();
    if (projectId) params.set("project_id", projectId);
    if (appId) params.set("app_id", appId);
    const query = params.toString();
    return request<{ conversations: ConversationSession[] }>(
      `/api/conversations${query ? `?${query}` : ""}`,
      { retries: 3, retryDelayMs: 500 },
    );
  },

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
    appContext?: { appId: string; targetId: string } | null,
    mainRoute?: {
      provider?: string;
      model?: string;
      effort?: string;
    } | null,
  ) =>
    request<{
      session: ConversationSession;
      initial_message?: ConversationMessage;
    }>("/api/conversations", {
      method: "POST",
      body: JSON.stringify({
        character_name: characterName,
        project_id: projectId,
        app_id: appContext?.appId,
        app_target_id: appContext?.targetId,
        initial_message: initialMessage,
        ...(mainRoute?.provider && mainRoute?.model
          ? { main_route: mainRoute }
          : {}),
      }),
    }),

  /**
   * セッションのメッセージ一覧取得。
   * since（ISO8601）を渡すと差分のみ取得し、レスポンスの server_time を次回 since に使う。
   */
  getMessages: (sessionId: string, since?: string) => {
    const query = since ? `?since=${encodeURIComponent(since)}` : "";
    // cache: "no-store" は付けない。サーバの ETag + Cache-Control: private, no-cache を活かし、
    // ブラウザの条件付き GET（If-None-Match → 304）で帯域を節約する。
    // 差分が無い間は ?since が一定になり同一 URL の 304 が成立する。
    return request<{ messages: ConversationMessage[]; server_time?: string }>(
      `/api/conversations/${sessionId}/messages${query}`,
      {
        headers: { "Cache-Control": "no-cache" },
        retries: 2,
        retryDelayMs: 500,
      },
    );
  },

  getContextSnapshot: (sessionId: string) =>
    request<ContextSnapshotResponse>(
      `/api/python-proxy/conversations/${sessionId}/context-snapshot`,
      {
        cache: "no-store",
        headers: { "Cache-Control": "no-cache" },
        retries: 1,
        retryDelayMs: 300,
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

  /** セッション再開。キャッシュ再訪時は includeMessages=false で全履歴転送を省く。 */
  resumeSession: (sessionId: string, includeMessages = true) =>
    request<{ session: ConversationSession; messages: ConversationMessage[] }>(
      `/api/conversations/${sessionId}/resume?include_messages=${includeMessages ? "true" : "false"}`,
      {
        method: "POST",
        retries: 3,
        retryDelayMs: 500,
        timeoutMs: 15000,
      },
    ),

  /** メッセージをディスパッチ（FastAPI: POST /api/conversations/{id}/dispatch） */
  dispatchMessage: (
    sessionId: string,
    // 生成型 ConversationDispatchRequest を正とする。default 値を持つ
    // include_project_context / skip_user_persistence はクライアントでは任意化。
    data: OptionalizeDefaults<
      Schemas["ConversationDispatchRequest"],
      "include_project_context" | "skip_user_persistence"
    >,
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

  getAgentRun: (runId: string, options?: AgentRunFetchOptions) => {
    const query = new URLSearchParams({
      include_events: String(options?.includeEvents ?? false),
      include_tool_calls: String(options?.includeToolCalls ?? false),
      include_timeline: String(options?.includeTimeline ?? true),
    });
    return request<{ success: boolean; agent_run: AgentRun }>(
      `/api/python-proxy/agent-runs/${runId}?${query.toString()}`,
      {
        cache: "no-store",
        headers: { "Cache-Control": "no-cache" },
        retries: 1,
        retryDelayMs: 300,
        timeoutMs: 10000,
      },
    );
  },

  stopGeneration: (sessionId: string) =>
    request<{
      success: boolean;
      cancelled: number;
      session_id: string;
      status?: string;
      message?: ConversationMessage | null;
      messages?: ConversationMessage[];
      persistence_failed?: boolean;
      persistence_failed_run_ids?: string[];
      agent_run_id?: string | null;
      agent_run_ids?: string[];
    }>(`/api/python-proxy/conversations/${sessionId}/generation/stop`, {
      method: "POST",
    }),

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

  steerGeneration: (
    sessionId: string,
    message: string,
    clientMessageId?: string,
    agentRunId?: string | null,
  ) => {
    const body = {
      message,
      client_message_id: clientMessageId,
      agent_run_id: agentRunId || undefined,
    };
    return request<{
      success: boolean;
      queued: boolean;
      interrupted?: boolean;
      blocked?: boolean;
      status?: string;
      agent_run_id?: string;
      client_message_id?: string;
      user_message_id?: string;
      persistence_failed?: boolean;
      duplicate?: boolean;
      session_id: string;
    }>(
      `/api/python-proxy/conversations/${sessionId}/generation/steer`,
      {
        method: "POST",
        body: JSON.stringify(body),
      },
    );
  },

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

  markSessionRead: (sessionId: string) =>
    request<{
      success: boolean;
      session_id: string;
      last_read_at?: string | null;
    }>(`/api/conversations/${sessionId}/read`, {
      method: "POST",
      retries: 1,
      retryDelayMs: 250,
    }),

  updateSessionDevelopmentStatus: (
    sessionId: string,
    developmentStatus: "working" | "waiting_for_user" | "completed",
  ) =>
    request<{ success: boolean; session: ConversationSession }>(
      `/api/conversations/${sessionId}`,
      {
        method: "PUT",
        body: JSON.stringify({ development_status: developmentStatus }),
      },
    ),

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

  /** 指定メッセージまでを独立した会話へフォーク */
  forkSession: (sessionId: string, fromMessageId: string, title?: string) =>
    request<{ success: boolean; session: ConversationSession }>(
      `/api/python-proxy/conversations/${sessionId}/fork`,
      {
        method: "POST",
        body: JSON.stringify({
          from_message_id: fromMessageId,
          title,
        }),
      },
    ),

  /** メッセージ編集（新ブランチ作成） */
  editMessage: (sessionId: string, messageId: string, content: string) => {
    const body: Schemas["EditMessageRequest"] = { content };
    return request<{ message: ConversationMessage }>(
      `/api/python-proxy/conversations/${sessionId}/messages/${messageId}`,
      {
        method: "PUT",
        body: JSON.stringify(body),
        signal: AbortSignal.timeout(15000),
      },
    );
  },

  /** メッセージのブランチ一覧取得（兄弟ブランチ） */
  getMessageBranches: (sessionId: string, messageId: string) =>
    request<{ branches: ConversationMessage[] }>(
      `/api/python-proxy/conversations/${sessionId}/messages/${messageId}/branches`,
    ),

  /** ブランチ切替 */
  switchBranch: (sessionId: string, messageId: string, branchIndex: number) => {
    const body: Schemas["SwitchBranchRequest"] = { branch_index: branchIndex };
    return request<{ success: boolean }>(
      `/api/python-proxy/conversations/${sessionId}/messages/${messageId}/switch-branch`,
      {
        method: "POST",
        body: JSON.stringify(body),
      },
    );
  },

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
  ) => {
    const body: Schemas["CreateGroupSessionRequest"] = {
      character_names: characterNames,
      user_ids: userIds ?? [],
      agent_ids: agentIds ?? [],
      project_id: projectId,
    };
    return request<{
      session: ConversationSession;
      first_messages?: Array<{
        character_slug: string;
        character_name: string;
        content: string;
      }>;
    }>("/api/python-proxy/conversations/group", {
      method: "POST",
      body: JSON.stringify(body),
    });
  },

  /** グループ応答 */
  groupRespond: (sessionId: string, message: string, strategy?: string) => {
    const body: OptionalizeDefaults<
      Schemas["GroupRespondRequest"],
      "strategy"
    > = { message, strategy };
    return request<{
      responses: Array<{
        character_slug: string;
        character_name: string;
        content: string;
      }>;
    }>(`/api/python-proxy/conversations/${sessionId}/group-respond`, {
      method: "POST",
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(30000),
    });
  },

  // ─── RPスライダー関連API ───

  /** RP設定取得 */
  getRpSettings: (sessionId: string) =>
    request<{ rp_settings: Record<string, number> }>(
      `/api/python-proxy/conversations/${sessionId}/rp-settings`,
    ),

  /** RP設定更新 */
  updateRpSettings: (sessionId: string, settings: Record<string, number>) =>
    request<{ success: boolean }>(
      `/api/python-proxy/conversations/${sessionId}/rp-settings`,
      {
        method: "PUT",
        body: JSON.stringify(settings),
      },
    ),

};
