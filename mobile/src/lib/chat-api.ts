/**
 * チャットAPI
 */

import { fetchApi } from "./api-client";
import { CHAT_TIMEOUT } from "../constants/config";
import type {
  ChatResponseModelSelection,
  AgentRun,
  ConversationSession,
  ConversationMessage,
  LlmModelCatalogResponse,
} from "../types/api";
import type { components as GeneratedApiComponents } from "../types/api-types.gen";
import { requireCharacterSlug } from "./character-api";

type CanonicalConversationSearchResponse =
  GeneratedApiComponents["schemas"]["ConversationSearchResponse"];
import type {
  ChatCommandCapability,
  SkillSlashCommand,
} from "../features/conversation/chat-commands";
import { skillSlashCommands } from "../features/conversation/chat-commands";
import { conversationPerformanceDiagnostics } from "../features/conversation/performance-diagnostics";

export type LlmMode = string;

/** セッションへ紐付ける App/Target コンテキスト。 */
export type ChatAppContext = {
  appId: string;
  appTargetId?: string | null;
  projectId?: string | null;
};

export type CreateSessionOptions = ChatAppContext & {
  mainRoute?: {
    provider?: string;
    model?: string;
    effort?: string;
  } | null;
};

export type ContextSnapshotCategory = {
  id?: string;
  category?: string;
  label?: string;
  input_tokens?: number | null;
  tokens?: number | null;
  chars?: number | null;
  [key: string]: unknown;
};

export type ContextRequestSnapshot = {
  id?: string;
  request_index?: number;
  request_count?: number;
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
  measurement?: string;
  categories?: ContextSnapshotCategory[];
  components?: ContextSnapshotCategory[];
  [key: string]: unknown;
};

export type ContextSnapshot = ContextRequestSnapshot & {
  session_id?: string;
  message_id?: string;
  main?: ContextRequestSnapshot | null;
  requests?: ContextRequestSnapshot[];
  effective?: {
    main?: ContextRequestSnapshot | null;
    [key: string]: unknown;
  } | null;
};

export type ContextSnapshotResponse = {
  success?: boolean;
  status: "available" | "unavailable" | "missing" | string;
  snapshot?: ContextSnapshot | null;
};

/**
 * Resolve the one Main request to show in the compact mobile context rail.
 * Newer servers expose `snapshot.main`; transitional servers may expose it
 * under `snapshot.effective.main`, while legacy servers put Main at top level.
 */
export function resolveMainContextSnapshot(
  snapshot?: ContextSnapshot | null,
): ContextRequestSnapshot | null {
  if (!snapshot) return null;
  return snapshot.main ?? snapshot.effective?.main ?? snapshot;
}

export type ConversationSearchResult = {
  id: string;
  match_type: "message" | "session" | string;
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

export type GenerationSteerResponse = {
  success: boolean;
  queued?: boolean;
  interrupted?: boolean;
  blocked?: boolean;
  status?: string;
  agent_run_id?: string | null;
  client_message_id?: string | null;
  user_message_id?: string | null;
  persistence_failed?: boolean;
  duplicate?: boolean;
  session_id: string;
};

export type ConversationForkResponse = {
  success: boolean;
  session: ConversationSession;
};

export type GroupCharacterResponse = {
  character_slug: string;
  character_name: string;
  content: string;
};

export type ChatAttachmentMetadata = {
  name: string;
  path?: string;
  project_relative_path?: string;
  kind?: "wbs" | "issue" | "risk" | "request" | "attachment" | string;
  registered?: boolean;
  size?: number;
  mime_type?: string;
  upload_failed?: boolean;
  error?: string;
};

/** Keep only server-recognised attachment metadata; never upload local blobs. */
export function sanitizeChatAttachmentMetadata(
  value: unknown,
): ChatAttachmentMetadata[] {
  if (!Array.isArray(value)) return [];
  return value.slice(0, 8).flatMap((raw) => {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return [];
    const input = raw as Record<string, unknown>;
    const name =
      typeof input.name === "string"
        ? input.name.trim()
        : typeof input.display_name === "string"
          ? input.display_name.trim()
          : typeof input.file_name === "string"
            ? input.file_name.trim()
            : "";
    if (!name) return [];
    const item: ChatAttachmentMetadata = { name };
    const stringKeys = [
      "path",
      "project_relative_path",
      "kind",
      "mime_type",
      "error",
    ] as const;
    for (const key of stringKeys) {
      if (typeof input[key] === "string" && input[key].trim()) {
        item[key] = input[key].trim();
      }
    }
    if (typeof input.registered === "boolean") item.registered = input.registered;
    if (typeof input.upload_failed === "boolean") item.upload_failed = input.upload_failed;
    if (typeof input.size === "number" && Number.isFinite(input.size) && input.size >= 0) {
      item.size = input.size;
    }
    return [item];
  });
}

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

type ConversationMessageWire = Omit<
  ConversationMessage,
  "branch_count" | "branch_index" | "is_active_branch"
> & {
  branch_count?: number | null;
  branch_index?: number | null;
  is_active_branch?: boolean | null;
};

type ConversationMessagesWireResponse = {
  success?: boolean;
  messages: ConversationMessageWire[];
  server_time: string;
};

export type ConversationMessagesResponse = {
  success?: boolean;
  messages: ConversationMessage[];
  server_time: string;
};

function normalizeConversationMessage(
  message: ConversationMessageWire,
): ConversationMessage {
  return {
    ...message,
    branch_index: message.branch_index ?? 0,
    is_active_branch: message.is_active_branch ?? true,
  };
}

export type ResumeSessionOptions = {
  includeMessages?: boolean;
};

export const chatApi = {
  async getLlmMode(): Promise<LlmModeResponse> {
    return fetchApi<LlmModeResponse>("/api/llm/mode");
  },

  async setLlmMode(mode: LlmMode): Promise<LlmModeResponse> {
    return fetchApi<LlmModeResponse>("/api/llm/mode", {
      method: "POST",
      body: JSON.stringify({ mode }),
    });
  },

  async getLlmModelCatalog(): Promise<LlmModelCatalogResponse> {
    return fetchApi<LlmModelCatalogResponse>("/api/llm/models");
  },

  async listSkillSlashCommands(projectId?: string | null): Promise<SkillSlashCommand[]> {
    const query = projectId ? `?project_id=${encodeURIComponent(projectId)}` : "";
    const data = await fetchApi<{
      skills?: Array<{ name: string; description?: string; trigger_mode?: string }>;
    }>(`/api/skills${query}`);
    return skillSlashCommands(data.skills ?? []);
  },

  /** セッション一覧取得（App/Target絞り込みにも対応） */
  async listSessions(
    projectId?: string,
    options?: { appId?: string | null; appTargetId?: string | null },
  ): Promise<ConversationSession[]> {
    const query = new URLSearchParams();
    if (projectId) query.set("project_id", projectId);
    if (options?.appId) query.set("app_id", options.appId);
    if (options?.appTargetId) query.set("app_target_id", options.appTargetId);
    const params = query.toString() ? `?${query.toString()}` : "";
    const data = await fetchApi<{ conversations: ConversationSession[] }>(
      `/api/conversations${params}`,
    );
    return data.conversations;
  },

  /** Canonical FastAPI online conversation search endpoint. */
  async searchConversations(
    query: string,
    projectId?: string | null,
    limit = 50,
  ): Promise<{ results: ConversationSearchResult[]; total: number }> {
    const normalized = query.trim();
    if (!normalized) return { results: [], total: 0 };
    const params = new URLSearchParams({
      q: normalized,
      limit: String(Math.min(50, Math.max(1, Math.floor(limit)))),
    });
    if (projectId) params.set("project_id", projectId);
    return fetchApi<CanonicalConversationSearchResponse>(
      `/api/conversations/search?${params.toString()}`,
      {},
      CHAT_TIMEOUT,
    );
  },

  async searchSessions(
    query: string,
    projectId?: string | null,
    limit = 50,
  ): Promise<{ results: ConversationSearchResult[]; total: number }> {
    return this.searchConversations(query, projectId, limit);
  },

  /** セッション作成 */
  async createSession(
    characterName: string,
    projectId?: string,
    options?: Partial<CreateSessionOptions> | null,
    legacyAppContext?: { appId: string; targetId?: string | null } | null,
    legacyMainRoute?: CreateSessionOptions["mainRoute"],
  ): Promise<ConversationSession> {
    const normalizedOptions: Partial<CreateSessionOptions> =
      options && ("appId" in options || "appTargetId" in options || "mainRoute" in options)
        ? options
        : {
            ...(legacyAppContext?.appId ? { appId: legacyAppContext.appId } : {}),
            ...(legacyAppContext?.targetId
              ? { appTargetId: legacyAppContext.targetId }
              : {}),
            ...(legacyMainRoute ? { mainRoute: legacyMainRoute } : {}),
          };
    const body: Record<string, string> = {
      character_name: requireCharacterSlug(characterName),
    };
    if (projectId) body.project_id = projectId;
    if (normalizedOptions.appId) body.app_id = normalizedOptions.appId;
    if (normalizedOptions.appTargetId) body.app_target_id = normalizedOptions.appTargetId;
    const mainRoute = normalizedOptions.mainRoute;

    const data = await fetchApi<{
      session: ConversationSession;
      first_message?: string;
    }>(
      "/api/conversations",
      {
        method: "POST",
        body: JSON.stringify({
          ...body,
          ...(mainRoute?.provider && mainRoute?.model
            ? { main_route: mainRoute }
            : {}),
        }),
      },
      CHAT_TIMEOUT,
    );
    return data.session;
  },

  /**
   * メッセージを永続化する（AIを発火させず role+content だけ保存）。
   * ローカル専用セッションのサーバー同期で使用する。
   */
  async addMessage(
    sessionId: string,
    data: {
      role: "user" | "assistant";
      content: string;
      client_message_id?: string;
    },
  ): Promise<ConversationMessage> {
    const result = await fetchApi<{
      success?: boolean;
      message: ConversationMessage;
    }>(
      `/api/conversations/${sessionId}/messages`,
      { method: "POST", body: JSON.stringify(data) },
      CHAT_TIMEOUT,
    );
    return result.message;
  },

  /** メッセージ取得（アクティブブランチのみ） */
  async getMessages(sessionId: string): Promise<ConversationMessage[]> {
    const data = await this.getMessagesDelta(sessionId);
    return data.messages;
  },

  /**
   * メッセージ全量/差分取得。
   *
   * since は直前にサーバーが返した server_time をそのまま渡す。5秒の
   * overlap はサーバー側で行われ、SQLite側のrevision比較で冪等適用する。
   * fetchApi はResponse headerを公開していないため、ETagはここでは送らない。
   */
  async getMessagesDelta(
    sessionId: string,
    since?: string | null,
  ): Promise<ConversationMessagesResponse> {
    const query = since ? `?since=${encodeURIComponent(since)}` : "";
    conversationPerformanceDiagnostics.increment(
      "http",
      "conversation-messages.requests",
    );
    const data = await conversationPerformanceDiagnostics.measureAsync(
      "http",
      "conversation-messages",
      () =>
        fetchApi<ConversationMessagesWireResponse>(
          `/api/conversations/${sessionId}/messages${query}`,
        ),
    );
    conversationPerformanceDiagnostics.increment(
      "http",
      "conversation-messages.payload-items",
      data.messages.length,
    );
    return {
      ...data,
      messages: data.messages.map(normalizeConversationMessage),
    };
  },

  /** セッション再開 */
  async resumeSession(
    sessionId: string,
    options: ResumeSessionOptions = {},
  ): Promise<{
    session: ConversationSession;
    messages: ConversationMessage[];
  }> {
    const includeMessages = options.includeMessages ?? true;
    return fetchApi(
      `/api/conversations/${sessionId}/resume?include_messages=${includeMessages ? "true" : "false"}`,
      { method: "POST" },
      CHAT_TIMEOUT,
    );
  },

  /** セッション削除 */
  async dispatchMessage(
    sessionId: string,
    data: {
      message: string;
      project_id?: string;
      app_id?: string | null;
      app_target_id?: string | null;
      agent_mode?: string;
      generation_profile?: string;
      include_project_context?: boolean;
      edit_message_id?: string;
      response_model?: ChatResponseModelSelection;
      command_capabilities?: ChatCommandCapability[];
      tools_required?: boolean;
      mentions?: Array<{ type: string; id: string; name: string }>;
      attachments?: Array<ChatAttachmentMetadata | Record<string, unknown>>;
      client_message_id?: string;
    },
  ): Promise<{
    success: boolean;
    queued: boolean;
    session_id: string;
    user_message_id?: string;
    agent_run_id?: string;
  }> {
    const body = {
      ...data,
      ...(data.attachments
        ? { attachments: sanitizeChatAttachmentMetadata(data.attachments) }
        : {}),
    };
    return fetchApi(
      `/api/conversations/${sessionId}/dispatch`,
      {
        method: "POST",
        body: JSON.stringify(body),
      },
      CHAT_TIMEOUT,
    );
  },

  /** セッションの Project/App/Target コンテキストを一括更新。 */
  async updateSessionContext(
    sessionId: string,
    context: {
      projectId?: string | null;
      appId?: string | null;
      appTargetId?: string | null;
      developmentStatus?: "working" | "waiting_for_user" | "completed" | null;
    },
  ): Promise<ConversationSession> {
    const body: Record<string, unknown> = {};
    if (Object.prototype.hasOwnProperty.call(context, "projectId")) {
      body.project_id = context.projectId ?? null;
    }
    if (Object.prototype.hasOwnProperty.call(context, "appId")) {
      body.app_id = context.appId ?? null;
    }
    if (Object.prototype.hasOwnProperty.call(context, "appTargetId")) {
      body.app_target_id = context.appTargetId ?? null;
    }
    if (Object.prototype.hasOwnProperty.call(context, "developmentStatus")) {
      body.development_status = context.developmentStatus ?? null;
    }
    const data = await fetchApi<{
      success: boolean;
      session: ConversationSession;
    }>(`/api/conversations/${sessionId}`, {
      method: "PUT",
      body: JSON.stringify(body),
    });
    return data.session;
  },

  async bindAppContext(
    sessionId: string,
    context: ChatAppContext | null,
  ): Promise<ConversationSession> {
    return this.updateSessionContext(sessionId, {
      appId: context?.appId ?? null,
      appTargetId: context?.appTargetId ?? null,
      ...(context && Object.prototype.hasOwnProperty.call(context, "projectId")
        ? { projectId: context.projectId ?? null }
        : {}),
      developmentStatus: context ? "working" : null,
    });
  },

  async getContextSnapshot(sessionId: string): Promise<ContextSnapshotResponse> {
    return fetchApi<ContextSnapshotResponse>(
      `/api/conversations/${sessionId}/context-snapshot`,
      {},
      CHAT_TIMEOUT,
    );
  },

  async getSessionContextSnapshot(sessionId: string): Promise<ContextSnapshotResponse> {
    return this.getContextSnapshot(sessionId);
  },

  async getMainContextSnapshot(
    sessionId: string,
  ): Promise<ContextRequestSnapshot | null> {
    const result = await this.getContextSnapshot(sessionId);
    return resolveMainContextSnapshot(result.snapshot);
  },

  async stopGeneration(sessionId: string): Promise<{
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
  }> {
    return fetchApi(
      `/api/conversations/${sessionId}/generation/stop`,
      { method: "POST" },
      CHAT_TIMEOUT,
    );
  },

  async getAgentRun(runId: string): Promise<AgentRun> {
    const data = await fetchApi<{ success: boolean; agent_run: AgentRun }>(
      `/api/agent-runs/${runId}?include_events=false&include_tool_calls=false&include_timeline=true`,
    );
    return data.agent_run;
  },

  async getGenerationStatus(sessionId: string): Promise<{
    success: boolean;
    session_id: string;
    running: boolean;
    status: string;
    message?: string | null;
    active_tool?: string | null;
    agent_run_id?: string | null;
  }> {
    return fetchApi(`/api/conversations/${sessionId}/generation/status`);
  },

  /** 生成中だけ有効な即時ステア。失敗時は呼び出し側が入力を保持する。 */
  async steerGeneration(
    sessionId: string,
    message: string,
    options?: { clientMessageId?: string; agentRunId?: string | null },
  ): Promise<GenerationSteerResponse> {
    const text = message.trim();
    if (!text) throw new Error("ステア内容を入力してください。");
    return fetchApi<GenerationSteerResponse>(
      `/api/conversations/${sessionId}/generation/steer`,
      {
        method: "POST",
        body: JSON.stringify({
          message: text,
          ...(options?.clientMessageId
            ? { client_message_id: options.clientMessageId }
            : {}),
          ...(options?.agentRunId ? { agent_run_id: options.agentRunId } : {}),
        }),
      },
      CHAT_TIMEOUT,
    );
  },

  async steer(
    sessionId: string,
    message: string,
    options?: { clientMessageId?: string; agentRunId?: string | null },
  ): Promise<GenerationSteerResponse> {
    return this.steerGeneration(sessionId, message, options);
  },

  async markSessionRead(sessionId: string): Promise<{
    success: boolean;
    session_id: string;
    last_read_at?: string | null;
  }> {
    return fetchApi(`/api/conversations/${sessionId}/read`, {
      method: "POST",
    });
  },

  async deleteSession(sessionId: string): Promise<void> {
    await fetchApi(`/api/conversations/${sessionId}`, { method: "DELETE" });
  },

  /** タイトル更新 */
  async updateTitle(sessionId: string, title: string): Promise<void> {
    await fetchApi(`/api/conversations/${sessionId}`, {
      method: "PUT",
      body: JSON.stringify({ title }),
    });
  },

  /** 指定メッセージまでを独立した会話へフォーク。 */
  async forkSession(
    sessionId: string,
    fromMessageId: string,
    title?: string | null,
  ): Promise<ConversationForkResponse> {
    return fetchApi<ConversationForkResponse>(
      `/api/conversations/${sessionId}/fork`,
      {
        method: "POST",
        body: JSON.stringify({
          from_message_id: fromMessageId,
          ...(title?.trim() ? { title: title.trim() } : {}),
        }),
      },
      CHAT_TIMEOUT,
    );
  },

  async forkConversation(
    sessionId: string,
    fromMessageId: string,
    title?: string | null,
  ): Promise<ConversationForkResponse> {
    return this.forkSession(sessionId, fromMessageId, title);
  },

  /** 通常セッションのキャラクター更新（制約判定はrepository側で行う）。 */
  async updateCharacter(
    sessionId: string,
    characterSlug: string,
  ): Promise<ConversationSession> {
    const data = await fetchApi<{
      success: boolean;
      session: ConversationSession;
    }>(`/api/conversations/${sessionId}`, {
      method: "PUT",
      body: JSON.stringify({
        character_name: requireCharacterSlug(characterSlug),
      }),
    });
    return data.session;
  },

  /** セッションに関連付けるプロジェクトを更新する。空文字は関連解除。 */
  async updateProject(
    sessionId: string,
    projectId: string | null,
  ): Promise<ConversationSession> {
    const data = await fetchApi<{
      success: boolean;
      session: ConversationSession;
    }>(`/api/conversations/${sessionId}`, {
      method: "PUT",
      body: JSON.stringify({ project_id: projectId ?? "" }),
    });
    return data.session;
  },

  /** 初回会話文脈からサーバー側LLMでタイトルを生成 */
  async generateSessionTitle(sessionId: string): Promise<{
    success: boolean;
    title: string;
    generated: boolean;
    source?: "llm" | "fallback" | null;
  }> {
    return fetchApi(
      `/api/conversations/${sessionId}/generate-title`,
      { method: "POST" },
      CHAT_TIMEOUT,
    );
  },

  async editMessage(
    sessionId: string,
    messageId: string,
    content: string,
  ): Promise<ConversationMessage> {
    const data = await fetchApi<{ message: ConversationMessage }>(
      `/api/conversations/${sessionId}/messages/${messageId}`,
      {
        method: "PUT",
        body: JSON.stringify({ content }),
      },
      CHAT_TIMEOUT,
    );
    return data.message;
  },

  async getMessageBranches(
    sessionId: string,
    messageId: string,
  ): Promise<ConversationMessage[]> {
    const data = await fetchApi<{ branches: ConversationMessage[] }>(
      `/api/conversations/${sessionId}/messages/${messageId}/branches`,
    );
    return data.branches;
  },

  async switchBranch(
    sessionId: string,
    messageId: string,
    branchIndex: number,
  ): Promise<void> {
    await fetchApi(
      `/api/conversations/${sessionId}/messages/${messageId}/switch-branch`,
      {
        method: "POST",
        body: JSON.stringify({ branch_index: branchIndex }),
      },
    );
  },

  async groupRespond(
    sessionId: string,
    message: string,
    strategy?: string,
  ): Promise<{
    responses: GroupCharacterResponse[];
  }> {
    return fetchApi(
      `/api/conversations/${sessionId}/group-respond`,
      {
        method: "POST",
        body: JSON.stringify({ message, strategy }),
      },
      CHAT_TIMEOUT,
    );
  },

  /** 2人以上のキャラクターを参加させたグループセッションを作成。 */
  async createGroupSession(
    characterNames: string[],
    projectId?: string | null,
    userIds: string[] = [],
    agentIds: string[] = [],
  ): Promise<{
    session: ConversationSession;
    first_messages?: GroupCharacterResponse[];
  }> {
    const names = Array.from(
      new Set(characterNames.map((name) => name.trim()).filter(Boolean)),
    );
    if (names.length + new Set(userIds).size + new Set(agentIds).size < 2) {
      throw new Error("グループチャットには2人以上の参加者が必要です。");
    }
    return fetchApi(
      "/api/conversations/group",
      {
        method: "POST",
        body: JSON.stringify({
          character_names: names.map((name) => requireCharacterSlug(name)),
          user_ids: userIds,
          agent_ids: agentIds,
          project_id: projectId ?? null,
        }),
      },
      CHAT_TIMEOUT,
    );
  },

  async createGroup(
    characterNames: string[],
    projectId?: string | null,
    userIds: string[] = [],
    agentIds: string[] = [],
  ): Promise<{ session: ConversationSession; first_messages?: GroupCharacterResponse[] }> {
    return this.createGroupSession(characterNames, projectId, userIds, agentIds);
  },

  async getRpSettings(sessionId: string): Promise<Record<string, number>> {
    const data = await fetchApi<{ rp_settings: Record<string, number> }>(
      `/api/conversations/${sessionId}/rp-settings`,
    );
    return data.rp_settings;
  },

  async updateRpSettings(
    sessionId: string,
    settings: Record<string, number>,
  ): Promise<void> {
    await fetchApi(`/api/conversations/${sessionId}/rp-settings`, {
      method: "PUT",
      body: JSON.stringify(settings),
    });
  },
};
