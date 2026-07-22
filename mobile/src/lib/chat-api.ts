/**
 * チャットAPI
 */

import { fetchApi } from "./api-client";
import { CHAT_TIMEOUT } from "../constants/config";
import type {
  ChatResponseModelSelection,
  ConversationSession,
  ConversationMessage,
  LlmModelCatalogResponse,
} from "../types/api";
import type {
  ChatCommandCapability,
  SkillSlashCommand,
} from "../features/conversation/chat-commands";
import { skillSlashCommands } from "../features/conversation/chat-commands";

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

  /** セッション一覧取得 */
  async listSessions(projectId?: string): Promise<ConversationSession[]> {
    const params = projectId ? `?project_id=${projectId}` : "";
    const data = await fetchApi<{ conversations: ConversationSession[] }>(
      `/api/conversations${params}`,
    );
    return data.conversations;
  },

  /** セッション作成 */
  async createSession(
    characterName: string = "default",
    projectId?: string,
  ): Promise<ConversationSession> {
    const body: Record<string, string> = { character_name: characterName };
    if (projectId) body.project_id = projectId;

    const data = await fetchApi<{
      session: ConversationSession;
      first_message?: string;
    }>(
      "/api/conversations",
      { method: "POST", body: JSON.stringify(body) },
      CHAT_TIMEOUT,
    );
    return data.session;
  },

  /** メッセージ取得（アクティブブランチのみ） */
  async getMessages(sessionId: string): Promise<ConversationMessage[]> {
    const data = await fetchApi<{ messages: ConversationMessage[] }>(
      `/api/conversations/${sessionId}/active-messages`,
    );
    return data.messages;
  },

  /** セッション再開 */
  async resumeSession(sessionId: string): Promise<{
    session: ConversationSession;
    messages: ConversationMessage[];
  }> {
    return fetchApi(
      `/api/conversations/${sessionId}/resume`,
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
      agent_mode?: string;
      include_project_context?: boolean;
      edit_message_id?: string;
      response_model?: ChatResponseModelSelection;
      command_capabilities?: ChatCommandCapability[];
      mentions?: Array<{ type: string; id: string; name: string }>;
      attachments?: Array<Record<string, unknown>>;
    },
  ): Promise<{ success: boolean; queued: boolean; session_id: string }> {
    return fetchApi(
      `/api/conversations/${sessionId}/dispatch`,
      {
        method: "POST",
        body: JSON.stringify(data),
      },
      CHAT_TIMEOUT,
    );
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
    responses: Array<{
      character_slug: string;
      character_name: string;
      content: string;
    }>;
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
