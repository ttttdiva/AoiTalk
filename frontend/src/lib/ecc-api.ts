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

/**
 * 料金カタログ由来の pricing status。
 * バックエンドの `PricingStatus` に対応し、集計行では複数状態が混在した場合に
 * `"mixed"` が返る。将来値に備えて string も許容する。
 */
export type PricingStatus =
  | "priced"
  | "provider_reported"
  | "free_incentive"
  | "subscription"
  | "local"
  | "unknown"
  | "mixed"
  | (string & {});

/**
 * 集計 API が各行へ付与する料金メタデータ。
 * バックエンドが旧版の場合は全て欠落するため、すべて optional にしている。
 */
export interface UsageCostMetrics {
  /** 定価換算コスト（無料枠を反映しない） */
  list_cost?: number;
  /** 無料枠を反映した推定請求額 */
  estimated_billed_cost?: number;
  /** list_cost - estimated_billed_cost */
  savings?: number;
  /** プロバイダ報告額（OpenRouter 等） */
  provider_reported_cost?: number | null;
  /** 料金未登録のリクエスト数 */
  unpriced_request_count?: number;
  /** 料金未登録のトークン数 */
  unpriced_tokens?: number;
  /** 料金カバー率（0-100） */
  pricing_coverage_percent?: number;
  /** 料金未登録を含む部分集計かどうか */
  is_partial?: boolean;
  /** by_model の行のみ。混在時は "mixed" */
  pricing_status?: PricingStatus;
}

export interface TokenUsageTotals extends UsageCostMetrics {
  /** 後方互換の概算値。正本は list_cost / estimated_billed_cost */
  total_cost: number;
  total_tokens: number;
  request_count: number;
}

export interface TokenUsageDailyPoint extends TokenUsageTotals {
  date: string;
}

export interface TokenUsageModelDailyPoint extends TokenUsageDailyPoint {
  provider: string;
  model: string;
  resolved_model?: string | null;
  /** 料金カタログ上の無料枠グループ（"1m" / "10m" など） */
  free_incentive_group?: string | null;
}

export interface TokenUsageModelRow extends TokenUsageTotals {
  provider: string;
  model: string;
}

export interface TokenUsageSummary {
  today: TokenUsageTotals;
  /** JST の当月1日〜今日の合計。旧バックエンドでは欠落する場合がある。 */
  monthly_total?: TokenUsageTotals;
  daily_trend: TokenUsageDailyPoint[];
  /** 過去30日の日別・モデル別推移。旧バックエンドでは欠落する場合がある。 */
  daily_model_trend: TokenUsageModelDailyPoint[];
  model_breakdown: TokenUsageModelRow[];
  /** ダッシュボード応答に同梱される料金カタログ状態。 */
  pricing?: PricingCatalogStatus;
  /** ダッシュボード応答に同梱される無料枠状態。 */
  free_tier?: FreeTierResponse;
}

export interface TokenUsageProjectRow extends TokenUsageTotals {
  project_id: string | null;
  project_name?: string | null;
}

export interface TokenUsageAgentRow extends TokenUsageTotals {
  agent_id?: string | null;
  agent_name?: string | null;
}

export interface TokenUsageUserRow extends TokenUsageTotals {
  user_id: string;
  user_name: string;
  total_input?: number;
  total_output?: number;
  total_cached?: number;
}

/** `GET /api/usage/pricing/status` の `pricing.sources[]` */
export interface PricingSourceState {
  source_key: string;
  catalog_version?: string | null;
  last_success_at?: string | null;
  last_attempt_at?: string | null;
  last_status?: string | null;
  last_error?: string | null;
  rule_count?: number;
}

export interface PricingCatalogStatus {
  catalog_version?: string | null;
  rule_count?: number;
  sources?: PricingSourceState[];
}

export interface FreeTierGroup {
  /** "1m" | "10m" */
  group: string;
  used_tokens: number;
  limit_tokens: number;
  remaining_tokens: number;
}

/** `GET /api/usage/free-tier` */
export interface FreeTierResponse {
  success?: boolean;
  enabled?: boolean;
  /** "tier_1_2" | "tier_3_plus" */
  tier?: string;
  billing_scope_id?: string | null;
  /** 無料枠の日界は UTC。集計対象の UTC 日付 */
  utc_date?: string;
  groups?: FreeTierGroup[];
}

/** `GET /api/usage/pricing/status` */
export interface PricingStatusResponse {
  success?: boolean;
  pricing?: PricingCatalogStatus;
  free_tier?: FreeTierResponse;
}

type TokenUsageDashboardResponse = Partial<
  Omit<TokenUsageSummary, "daily_trend" | "daily_model_trend">
> & {
  daily_trend?: TokenUsageSummary["daily_trend"];
  daily_model_trend?: TokenUsageSummary["daily_model_trend"];
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

/**
 * 旧バックエンド（list_cost 等を返さない版）でも UI が壊れないよう、
 * 料金メタデータへ total_cost ベースのフォールバックを入れる。
 */
function withCostFallback<T extends UsageCostMetrics & { total_cost?: number }>(
  row: T,
): T {
  const totalCost = typeof row.total_cost === "number" ? row.total_cost : 0;
  const listCost = row.list_cost ?? totalCost;
  const billedCost = row.estimated_billed_cost ?? listCost;
  const savings = row.savings ?? Math.max(listCost - billedCost, 0);
  return {
    ...row,
    total_cost: totalCost,
    list_cost: listCost,
    estimated_billed_cost: billedCost,
    savings,
  };
}

function usageParams(
  base: Record<string, string>,
  includeFreeIncentive?: boolean,
): URLSearchParams {
  const params = new URLSearchParams(base);
  if (includeFreeIncentive !== undefined) {
    params.set("include_free_incentive", includeFreeIncentive ? "true" : "false");
  }
  return params;
}

export const usageApi = {
  getDashboard: async (includeFreeIncentive?: boolean) => {
    const params = usageParams({}, includeFreeIncentive);
    const query = params.toString();
    const data = await get<TokenUsageDashboardResponse>(
      query ? `/usage/dashboard?${query}` : "/usage/dashboard",
    );
    return {
      today: withCostFallback(
        data.today ?? {
          total_cost: 0,
          total_tokens: 0,
          request_count: 0,
        },
      ),
      monthly_total: data.monthly_total
        ? withCostFallback(data.monthly_total)
        : undefined,
      daily_trend: (data.daily_trend ?? data.weekly_trend ?? []).map(
        withCostFallback,
      ),
      daily_model_trend: (data.daily_model_trend ?? []).map(withCostFallback),
      model_breakdown: (data.model_breakdown ?? []).map(withCostFallback),
      pricing: data.pricing,
      free_tier: data.free_tier,
    } satisfies TokenUsageSummary;
  },

  getDaily: async (
    start: string,
    end: string,
    includeFreeIncentive?: boolean,
  ) => {
    const params = usageParams({ start, end }, includeFreeIncentive);
    const data = await get<{
      success: boolean;
      daily: TokenUsageDailyPoint[];
    }>(`/usage/daily?${params}`);
    return (data.daily ?? []).map(withCostFallback);
  },

  getByModel: async (
    start: string,
    end: string,
    includeFreeIncentive?: boolean,
  ) => {
    const params = usageParams({ start, end }, includeFreeIncentive);
    const data = await get<{
      success: boolean;
      by_model: TokenUsageModelRow[];
    }>(`/usage/by-model?${params}`);
    return (data.by_model ?? []).map(withCostFallback);
  },

  getByProject: async (
    start: string,
    end: string,
    includeFreeIncentive?: boolean,
  ) => {
    const params = usageParams({ start, end }, includeFreeIncentive);
    const data = await get<{
      success: boolean;
      by_project: TokenUsageProjectRow[];
    }>(`/usage/by-project?${params}`);
    return (data.by_project ?? []).map(withCostFallback);
  },

  getByAgent: async (
    start: string,
    end: string,
    includeFreeIncentive?: boolean,
  ) => {
    const params = usageParams({ start, end }, includeFreeIncentive);
    const data = await get<{
      success: boolean;
      by_agent: TokenUsageAgentRow[];
    }>(`/usage/by-agent?${params}`);
    return (data.by_agent ?? []).map(withCostFallback);
  },

  getByUser: async (
    start: string,
    end: string,
    includeFreeIncentive?: boolean,
  ) => {
    const params = usageParams({ start, end }, includeFreeIncentive);
    const data = await get<{
      success: boolean;
      by_user: TokenUsageUserRow[];
    }>(`/usage/by-user?${params}`);
    return (data.by_user ?? []).map(withCostFallback);
  },

  getTotal: async (
    start: string,
    end: string,
    includeFreeIncentive?: boolean,
  ) => {
    const params = usageParams({ start, end }, includeFreeIncentive);
    const data = await get<{
      success: boolean;
      total: TokenUsageTotals;
    }>(`/usage/total?${params}`);
    return withCostFallback(
      data.total ?? { total_cost: 0, total_tokens: 0, request_count: 0 },
    );
  },

  /** 料金カタログの版と各ソースの最終更新状況を取得する */
  getPricingStatus: () =>
    get<PricingStatusResponse>("/usage/pricing/status"),

  /** 料金表を再取得する（管理者のみ） */
  refreshPricing: () =>
    post<{ success: boolean; result: unknown }>("/usage/pricing/refresh"),

  /** OpenAI データ共有インセンティブ無料枠の当日消費状況（UTC 日界）を取得する */
  getFreeTier: () => get<FreeTierResponse>("/usage/free-tier"),
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

// ─── Scoped Memory ───────────────────────────────────────────────────

export type MemoryScope = "global" | "user" | "project" | "task" | "session";

export interface ScopedMemory {
  id: string;
  user_id?: string | null;
  project_id?: string | null;
  task_id?: string | null;
  session_id?: string | null;
  scope_type: MemoryScope;
  scope_id?: string | null;
  memory_type: string;
  title?: string | null;
  content: string;
  structured_data?: JsonObject;
  source_type: string;
  source_ref?: string | null;
  confidence?: number;
  importance?: number;
  trust_level?: string;
  sensitivity?: string;
  evidence_refs?: JsonObject[];
  evidence_span?: JsonObject;
  dedupe_key?: string | null;
  supersedes_id?: string | null;
  version: number;
  created_by_actor?: string | null;
  rejection_reason?: string | null;
  projection_metadata?: JsonObject;
  migration_id?: string | null;
  status: string;
  is_pinned?: boolean;
  is_active?: boolean;
  created_at: string | null;
  updated_at: string | null;
  last_used_at?: string | null;
  expires_at?: string | null;
}

/** Legacy name retained for callers outside the Memory management surface. */
export type DreamingMemory = ScopedMemory;

export interface ScopedMemorySettings {
  user_auto_enabled: boolean;
  project_auto_enabled: boolean | null;
  project_id: string | null;
}

export interface ScopedMemoryJob {
  id: string;
  session_id: string;
  project_id?: string | null;
  status: string;
  attempts: number;
  error?: string | null;
  next_retry_at?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface ScopedMemoryMutation {
  success: boolean;
  memory_id: string;
  scope: MemoryScope;
  scope_id?: string | null;
  operation: string;
  replaced_id?: string | null;
  reason: string;
  memory?: ScopedMemory;
  project_information_node_id?: string;
}

export type ScopedMemoryListOptions = {
  scope?: MemoryScope;
  scope_id?: string;
  project_id?: string;
  task_id?: string;
  session_id?: string;
  status?: string;
  include_history?: boolean;
  limit?: number;
};

function memoryQuery(options: ScopedMemoryListOptions = {}): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(options)) {
    if (value === undefined || value === null || value === "") continue;
    params.set(key, String(value));
  }
  const query = params.toString();
  return query ? `?${query}` : "";
}

export const memoryApi = {
  list: (options?: ScopedMemoryListOptions) =>
    get<{ success: boolean; memories: ScopedMemory[] }>(
      `/memories${memoryQuery(options)}`,
    ),

  create: (data: {
    content: string;
    scope?: MemoryScope;
    scope_id?: string;
    project_id?: string;
    task_id?: string;
    session_id?: string;
    memory_type?: string;
    title?: string;
    source_ref?: string;
    evidence_refs?: JsonObject[];
    evidence_span?: JsonObject;
    confidence?: number;
    importance?: number;
    is_pinned?: boolean;
    status?: string;
    idempotency_key?: string;
  }) => post<ScopedMemoryMutation>("/memories", data),

  update: (id: string, data: Partial<ScopedMemory> & { version: number }) =>
    patch<ScopedMemoryMutation>(`/memories/${id}`, data),

  delete: (id: string, version?: number) =>
    del<ScopedMemoryMutation>(
      `/memories/${id}${version === undefined ? "" : `?version=${version}`}`,
    ),

  deleteAll: () => del<{ success: boolean; forgotten: number }>("/memories/all"),

  approve: (id: string, version: number, reason?: string) =>
    post<ScopedMemoryMutation>(`/memories/${id}/approve`, { version, reason }),

  reject: (id: string, version: number, reason?: string) =>
    post<ScopedMemoryMutation>(`/memories/${id}/reject`, { version, reason }),

  moveScope: (
    id: string,
    data: {
      version: number;
      scope: MemoryScope;
      scope_id?: string;
      project_id?: string;
      task_id?: string;
      session_id?: string;
      reason?: string;
    },
  ) => post<ScopedMemoryMutation>(`/memories/${id}/move-scope`, data),

  promote: (
    id: string,
    version: number,
    targetSection?: string,
    sourceRefs?: JsonObject[],
  ) =>
    post<ScopedMemoryMutation>(`/memories/${id}/promote`, {
      version,
      target_section: targetSection,
      source_refs: sourceRefs,
    }),

  explain: (id: string) =>
    get<{
      success: boolean;
      memory: ScopedMemory;
      lineage: { ancestors: ScopedMemory[]; descendants: ScopedMemory[] };
      explanation: JsonObject;
    }>(`/memories/${id}/explain`),

  getSettings: (projectId?: string) =>
    get<{ success: boolean; settings: ScopedMemorySettings }>(
      `/memories/settings${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ""}`,
    ),

  updateSettings: (data: {
    user_auto_enabled?: boolean;
    project_id?: string;
    project_auto_enabled?: boolean;
  }) =>
    patch<{ success: boolean; settings: ScopedMemorySettings }>(
      "/memories/settings",
      data,
    ),

  listJobs: (limit = 50) =>
    get<{ success: boolean; jobs: ScopedMemoryJob[] }>(
      `/memories/jobs?limit=${limit}`,
    ),
};
