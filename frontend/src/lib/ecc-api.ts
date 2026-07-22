// ECC (Extended Claude Capabilities) API クライアント
// ブラウザから直接呼ぶ用（"use client"コンポーネントから使用）
// 全て /api/python-proxy/api/ 経由（Python バックエンド）

// ─── Types ───────────────────────────────────────────────────────────

export type JsonValue =
  | string
  | number
  | boolean
  | null
  | JsonObject
  | JsonValue[];

export type JsonObject = {
  [key: string]: JsonValue | undefined;
};

// ワークフロー関連の旧型定義は削除済み（.mdベースに移行）

export interface TokenUsageSummary {
  today: { total_cost: number; total_tokens: number; request_count: number };
  daily_trend: Array<{
    date: string;
    total_cost: number;
    total_tokens: number;
    request_count: number;
  }>;
  model_breakdown: Array<{
    provider: string;
    model: string;
    total_cost: number;
    total_tokens: number;
    request_count: number;
  }>;
}

type TokenUsageDashboardResponse = Partial<
  Omit<TokenUsageSummary, "daily_trend">
> & {
  daily_trend?: TokenUsageSummary["daily_trend"];
  weekly_trend?: TokenUsageSummary["daily_trend"];
};

export interface SkillCategory {
  id: string;
  name: string;
  display_name: string;
  description: string;
  icon: string;
  color: string;
  sort_order: number;
}

export interface SkillPreset {
  id: string;
  name: string;
  display_name: string;
  description: string;
  category: string;
  prompt_template: string;
  trigger_mode: string;
  is_builtin: boolean;
  install_count: number;
}

export interface SkillChain {
  id: string;
  name: string;
  display_name: string;
  description: string;
  steps: Array<{
    skill_name: string;
    input_mapping?: Record<string, string>;
    on_error?: string;
  }>;
}

export interface MCPServer {
  name: string;
  command: string;
  status: string;
  tools_count?: number;
  error?: string;
}

export interface MCPTool {
  name: string;
  description: string;
  input_schema?: JsonObject;
}

export interface MCPHealthStatus {
  total: number;
  running: number;
  stopped: number;
  errors: number;
  error_details: Array<{ server: string; error: string }>;
}

export interface QualityReport {
  score: number;
  is_acceptable: boolean;
  issues: string[];
  suggestions: string[];
}

// ─── Fetch helper ────────────────────────────────────────────────────

const API_BASE = "/api/python-proxy/api";
const TIMEOUT_MS = 10000;

async function fetchWithTimeout<T>(
  url: string,
  options?: RequestInit,
): Promise<T> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), TIMEOUT_MS);
  try {
    const res = await fetch(url, {
      ...options,
      signal: controller.signal,
      credentials: "include",
      headers: { "Content-Type": "application/json", ...options?.headers },
    });
    if (res.status === 401) {
      if (typeof window !== "undefined") {
        window.location.href = "/login";
      }
      throw new Error("認証が必要です");
    }
    if (!res.ok) {
      const detail = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(detail.detail || res.statusText);
    }
    if (res.status === 204) return undefined as T;
    return res.json();
  } finally {
    clearTimeout(timeout);
  }
}

function get<T>(path: string): Promise<T> {
  return fetchWithTimeout<T>(`${API_BASE}${path}`);
}

function post<T>(path: string, body?: unknown): Promise<T> {
  return fetchWithTimeout<T>(`${API_BASE}${path}`, {
    method: "POST",
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
}

function patch<T>(path: string, body: unknown): Promise<T> {
  return fetchWithTimeout<T>(`${API_BASE}${path}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

function del<T>(path: string): Promise<T> {
  return fetchWithTimeout<T>(`${API_BASE}${path}`, { method: "DELETE" });
}

// ─── Automations ─────────────────────────────────────────────────────

// ─── Token Usage ─────────────────────────────────────────────────────

export const usageApi = {
  getDashboard: async () => {
    const data = await get<TokenUsageDashboardResponse>("/usage/dashboard");
    return {
      today: data.today ?? {
        total_cost: 0,
        total_tokens: 0,
        request_count: 0,
      },
      daily_trend: data.daily_trend ?? data.weekly_trend ?? [],
      model_breakdown: data.model_breakdown ?? [],
    } satisfies TokenUsageSummary;
  },

  getDaily: async (start: string, end: string) => {
    const params = new URLSearchParams({ start, end });
    const data = await get<{
      success: boolean;
      daily: TokenUsageSummary["daily_trend"];
    }>(`/usage/daily?${params}`);
    return data.daily ?? [];
  },

  getByModel: async (start: string, end: string) => {
    const params = new URLSearchParams({ start, end });
    const data = await get<{
      success: boolean;
      by_model: TokenUsageSummary["model_breakdown"];
    }>(`/usage/by-model?${params}`);
    return data.by_model ?? [];
  },

  getByProject: async (start: string, end: string) => {
    const params = new URLSearchParams({ start, end });
    const data = await get<{
      success: boolean;
      by_project: Array<{
        project_id: string;
        project_name: string;
        total_cost: number;
        total_tokens: number;
        request_count: number;
      }>;
    }>(`/usage/by-project?${params}`);
    return data.by_project ?? [];
  },

  getByAgent: async (start: string, end: string) => {
    const params = new URLSearchParams({ start, end });
    const data = await get<{
      success: boolean;
      by_agent: Array<{
        agent_id: string;
        agent_name: string;
        total_cost: number;
        total_tokens: number;
        request_count: number;
      }>;
    }>(`/usage/by-agent?${params}`);
    return data.by_agent ?? [];
  },

  getByUser: async (start: string, end: string) => {
    const params = new URLSearchParams({ start, end });
    const data = await get<{
      success: boolean;
      by_user: Array<{
        user_id: string;
        user_name: string;
        total_input: number;
        total_output: number;
        total_cached: number;
        total_cost: number;
        total_tokens: number;
        request_count: number;
      }>;
    }>(`/usage/by-user?${params}`);
    return data.by_user ?? [];
  },

  getTotal: async (start: string, end: string) => {
    const params = new URLSearchParams({ start, end });
    const data = await get<{
      success: boolean;
      total: {
        total_cost: number;
        total_tokens: number;
        request_count: number;
      };
    }>(`/usage/total?${params}`);
    return data.total;
  },
};

// ─── Skill Extensions ────────────────────────────────────────────────

export const skillExtApi = {
  listCategories: () => get<SkillCategory[]>("/skills/categories"),

  createCategory: (data: Partial<SkillCategory>) =>
    post<SkillCategory>("/skills/categories", data),

  listPresets: (category?: string) => {
    const params = category ? `?category=${encodeURIComponent(category)}` : "";
    return get<SkillPreset[]>(`/skills/presets${params}`);
  },

  installPreset: (presetId: string) =>
    post<{ success: boolean }>(`/skills/presets/${presetId}/install`),

  listChains: () => get<SkillChain[]>("/skills/chains"),

  createChain: (data: Partial<SkillChain>) =>
    post<SkillChain>("/skills/chains", data),

  updateChain: (id: string, data: Partial<SkillChain>) =>
    patch<SkillChain>(`/skills/chains/${id}`, data),

  deleteChain: (id: string) => del<void>(`/skills/chains/${id}`),

  executeChain: (id: string) =>
    post<{ success: boolean; results: unknown[] }>(
      `/skills/chains/${id}/execute`,
    ),
};

// ─── MCP ─────────────────────────────────────────────────────────────

export const mcpApi = {
  getServers: () => get<MCPServer[]>("/mcp/servers"),

  getServerTools: (name: string) =>
    get<MCPTool[]>(`/mcp/servers/${encodeURIComponent(name)}/tools`),

  toggleServer: (name: string) =>
    post<MCPServer>(`/mcp/servers/${encodeURIComponent(name)}/toggle`),

  restartServer: (name: string) =>
    post<MCPServer>(`/mcp/servers/${encodeURIComponent(name)}/restart`),

  getHealth: () => get<MCPHealthStatus>("/mcp/health"),
};

// ─── Quality ─────────────────────────────────────────────────────────

export const qualityApi = {
  verify: (data: { user_input: string; response: string; context?: string }) =>
    post<QualityReport>("/quality/verify", data),

  getConfig: () => get<JsonObject>("/quality/config"),

  updateConfig: (data: JsonObject) =>
    patch<JsonObject>("/quality/config", data),
};

// ─── Workflows ───────────────────────────────────────────────────────

// ─── Dreaming Memories ───────────────────────────────────────────────

export interface DreamingMemory {
  id: string;
  user_id?: string | null;
  scope_type?: string;
  scope_id?: string | null;
  memory_type: string;
  title?: string | null;
  content: string;
  structured_data?: JsonObject;
  source_type: string;
  source_ref?: string | null;
  confidence?: number;
  importance?: number;
  status: string;
  is_pinned?: boolean;
  is_active?: boolean;
  created_at: string | null;
  updated_at: string | null;
  last_used_at?: string | null;
  expires_at?: string | null;
}

export const memoryApi = {
  list: () => get<{ success: boolean; memories: DreamingMemory[] }>("/memories"),

  create: (data: { content: string; memory_type?: string; title?: string }) =>
    post<{ success: boolean; memory: DreamingMemory }>("/memories", data),

  update: (id: string, data: Partial<DreamingMemory>) =>
    patch<{ success: boolean; memory: DreamingMemory }>(`/memories/${id}`, data),

  delete: (id: string) => del<void>(`/memories/${id}`),

  deleteAll: () => del<void>("/memories/all"),
};
