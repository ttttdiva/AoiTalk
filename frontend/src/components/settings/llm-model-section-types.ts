import { formatBytes } from "@/lib/utils";

export interface LlmModelOption {
  id: string;
  label: string;
  description?: string;
  installed?: boolean;
  source?: string;
  source_label?: string;
  base_url?: string;
  server?: string;
  server_label?: string;
  size?: number;
  details?: {
    parameter_size?: string;
    quantization_level?: string;
    family?: string;
  };
  context_length?: number;
  reasoning_effort_options?: string[];
  custom_current?: boolean;
  media?: { image?: boolean; audio?: boolean };
  selection_kind?: "static" | "routing_profile";
  routing_profile_id?: string;
}

export interface LlmProviderCatalog {
  id: string;
  label: string;
  disabled?: boolean;
  models: LlmModelOption[];
  configured_model?: string;
  supports_custom_model: boolean;
  capabilities?: {
    supports_stream?: boolean;
    supports_tools?: boolean;
    supports_response_format?: boolean;
    supports_model_pull?: boolean;
    supports_model_delete?: boolean;
    supports_extra_body?: boolean;
  };
  settings?: {
    base_url?: string;
    api_key_configured?: boolean;
    api_key_placeholder?: string;
    reasoning_effort?: string;
    reasoning_effort_options?: string[];
  };
  source: string;
  refreshed?: boolean;
  cached_at?: string | null;
  error?: string | null;
  selection_kind?: "static" | "routing_profile";
}

export interface LlmModelCatalogResponse {
  current: {
    provider: string;
    model: string;
  };
  providers: LlmProviderCatalog[];
}

export interface LlmEngineResponse {
  success?: boolean;
  provider: string;
  model: string;
  message?: string;
}

export type SpeechRecognitionSettings = {
  current_engine?: string;
  engines?: Record<string, { model?: string }>;
};

export interface SettingsPayload {
  settings?: {
    agent_team?: {
      delegation_enabled?: boolean;
      member_settings_initialized?: boolean;
      confirm_prompt?: boolean;
      notify?: boolean;
      redaction_terms?: string[];
      strategy?: string;
      roster?: ModelRouteSettings[];
      members?: Record<string, AgentTeamMemberSettings>;
      model_groups?: Record<string, AgentTeamModelGroup>;
    };
    model_routing?: ModelRoutingSettings;
    speech_recognition?: SpeechRecognitionSettings;
  };
}

export interface ModelRoutingSettings {
  classes?: {
    heavy?: ModelRouteSettings;
    light?: ModelRouteSettings;
    vision?: ModelRouteSettings & { base_url?: string; api_key?: string };
    audio?: ModelRouteSettings & {
      engine?: "speech_recognition" | "llm" | "off";
      base_url?: string;
      api_key?: string;
    };
  };
  media?: {
    image_mode?: "auto" | "always" | "off";
  };
  overrides?: Record<string, ModelRouteSettings>;
}

export interface ModelRouteSettings {
  key?: string;
  member_key?: string;
  enabled?: boolean;
  provider?: string;
  model?: string;
  mode?: string;
  reasoning_effort?: string;
  external?: boolean;
  label?: string;
  role?: string;
  runner?: string;
  inherit?: boolean;
  scalable?: boolean;
  default_instances?: number;
  max_instances?: number;
  tools?: string[];
  group_id?: string;
  effective_provider?: string;
  effective_model?: string;
  effective_effort?: string;
  effort_policy?: EffortPolicy;
  effort?: string;
}

export type EffortPolicy = "inherit" | "same" | "lower" | "explicit" | "default";

export function effortPolicyLabel(policy: EffortPolicy): string {
  return policy === "inherit"
    ? "グループを継承"
    : policy === "same"
    ? "メインと同じ"
    : policy === "lower"
      ? "メインより1段階低い"
      : policy === "explicit"
        ? "明示指定"
        : "モデル既定";
}

export interface AgentTeamOverride {
  provider?: string;
  model?: string;
  effort_policy?: EffortPolicy;
  effort?: string;
  runner?: string;
}

export interface AgentTeamMemberSettings {
  enabled?: boolean;
  group_id?: string;
  override?: AgentTeamOverride;
  provider?: string;
  model?: string;
  mode?: string;
  reasoning_effort?: string;
  effort_policy?: EffortPolicy;
  effort?: string;
  runner?: string;
  default_instances?: number;
  max_instances?: number;
}

export interface AgentTeamModelGroup {
  name?: string;
  provider?: string;
  model?: string;
  effort_policy?: EffortPolicy;
  effort?: string;
  target_type?: "inherit" | "static" | "pool";
  pool_id?: string;
  routing_profile_id?: string;
}

export interface OllamaPullTask {
  task_id: string;
  model: string;
  status: string;
  message?: string;
  completed?: number;
  total?: number;
  percent?: number;
  done: boolean;
  error?: string | null;
}

export interface OllamaDeleteResponse {
  success: boolean;
  model: string;
}

export type ProviderDraft = {
  model: string;
  customModel: string;
};

export type ProviderSettingsDraft = {
  base_url?: string;
  api_key?: string;
  reasoning_effort?: string;
};

export type ModelRouteKey =
  | "advanced_reasoning"
  | "architect"
  | "explorer"
  | "implementer"
  | "reviewer"
  | "utility"
  | "media"
  | "spotify"
  | "scenario"
  | "writing"
  | "import"
  | "agent_harness";

export type ModelRouteDefinition = {
  key: ModelRouteKey;
  label: string;
  defaultProvider: string;
  defaultModel: string;
  allowedProviders?: string[];
  scalable?: boolean;
  defaultMaxInstances?: number;
};

export type ModelRouteDraft = {
  enabled: boolean;
  groupId: string;
  overrideOpen: boolean;
  provider: string;
  model: string;
  customModel: string;
  mode: string;
  scalable: boolean;
  defaultInstances: number;
  maxInstances: number;
  runner: string;
  effortPolicy: EffortPolicy;
  effectiveProvider: string;
  effectiveModel: string;
  effectiveEffort: string;
};

export type ModelClassDraft = {
  provider: string;
  model: string;
  customModel: string;
  mode: string;
  baseUrl: string;
  apiKey: string;
  effortPolicy: EffortPolicy;
  engine?: "speech_recognition" | "llm" | "off";
  inherit?: boolean;
};

export const MODEL_ROUTE_DEFINITIONS: ModelRouteDefinition[] = [
  {
    key: "advanced_reasoning",
    label: "高度推論",
    defaultProvider: "openai",
    defaultModel: "gpt-4o",
  },
  {
    key: "architect",
    label: "設計",
    defaultProvider: "openai",
    defaultModel: "gpt-4o-mini",
    scalable: true,
    defaultMaxInstances: 2,
  },
  {
    key: "explorer",
    label: "調査",
    defaultProvider: "openai",
    defaultModel: "gpt-4o-mini",
    scalable: true,
    defaultMaxInstances: 6,
  },
  {
    key: "implementer",
    label: "実装",
    defaultProvider: "openai",
    defaultModel: "gpt-4o-mini",
    scalable: true,
    defaultMaxInstances: 4,
  },
  {
    key: "reviewer",
    label: "レビュー",
    defaultProvider: "openai",
    defaultModel: "gpt-4o-mini",
    scalable: true,
    defaultMaxInstances: 4,
  },
  {
    key: "utility",
    label: "ユーティリティ",
    defaultProvider: "openai",
    defaultModel: "gpt-4o-mini",
  },
  {
    key: "media",
    label: "メディア",
    defaultProvider: "openai",
    defaultModel: "gpt-4o-mini",
  },
  {
    key: "spotify",
    label: "Spotify",
    defaultProvider: "openai",
    defaultModel: "gpt-4o-mini",
  },
  {
    key: "scenario",
    label: "TRPG_GM",
    defaultProvider: "openai",
    defaultModel: "gpt-4o-mini",
  },
  {
    key: "writing",
    label: "執筆",
    defaultProvider: "openai",
    defaultModel: "gpt-4o-mini",
  },
  {
    key: "import",
    label: "シナリオ素材取り込み",
    defaultProvider: "openai",
    defaultModel: "gpt-4o-mini",
  },
  {
    key: "agent_harness",
    label: "作業エージェント",
    defaultProvider: "codex-cli",
    defaultModel: "gpt-5-codex",
    allowedProviders: ["codex-cli", "claude-cli"],
  },
];

export type RouteGroup = "heavy" | "light" | "main" | "special";

export const MODEL_ROUTE_GROUP: Record<ModelRouteKey, RouteGroup> = {
  advanced_reasoning: "heavy",
  architect: "heavy",
  explorer: "light",
  implementer: "light",
  reviewer: "light",
  utility: "light",
  media: "light",
  spotify: "light",
  import: "light",
  scenario: "main",
  writing: "main",
  agent_harness: "special",
};

// 未設定（override無し）の行が実際に継承する先の表示文言。
export function routeInheritanceLabel(key: ModelRouteKey): string {
  const group = MODEL_ROUTE_GROUP[key];
  if (group === "heavy") return "既定の高負荷グループ";
  if (group === "light") return "既定の軽量グループ";
  if (group === "special") return "作業エージェント既定 (codex-cli / gpt-5-codex)";
  return "メインを継承";
}

export const EXTERNAL_AGENT_PROVIDERS = new Set([
  "openai",
  "openrouter",
  "gemini",
  "kimi",
  "antigravity-cli",
  "claude-cli",
  "codex-cli",
  "grok-cli",
]);

export const CONNECTION_SETTINGS_PROVIDERS = new Set([
  "ollama",
  "openai_compatible_local",
  "openrouter",
  "sglang",
  "kimi",
]);

export const REASONING_EFFORT_PROVIDERS = new Set([
  "codex-cli",
  "claude-cli",
  "kimi",
]);

export const API_KEY_REQUIRED_PROVIDERS = new Set([
  "openai",
  "gemini",
  "openrouter",
  "kimi",
]);

export const MODEL_PAGE_SIZE = 24;

export async function pyFetch<T = unknown>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api/python-proxy${path}`, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...init?.headers },
    ...init,
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(detail.detail || `API Error: ${res.status}`);
  }
  return res.json();
}

export function providerHint(providerId: string): string {
  switch (providerId) {
    case "codex-cli":
      return "Codex CLI は --model を受け付けます。候補はCLIから取得した一覧ではなく、未掲載モデルは直接入力してください。";
    case "claude-cli":
      return "Claude Code は alias とフルモデル名を受け付けます。候補はCLIから取得した一覧ではありません。";
    case "antigravity-cli":
      return "Antigravity CLI は --model でモデルを指定します。候補は agy models から取得した一覧ではありません。";
    case "grok-cli":
      return "Grok Build CLI はローカルの grok 認証を使います。未認証の場合は `grok login` を実行してください。AoiTalkから --always-approve は付けません。";
    case "sglang":
      return "SGLang は Hugging Face の model path または /v1/models のIDを使います。";
    case "openai_compatible_local":
      return "llama-server、exo、MLX LM などの /v1/chat/completions 互換APIを指定します。候補にBase URLがある場合は自動で反映します。";
    case "openrouter":
      return "OpenRouter は公開 Models API から候補を取得します。";
    case "kimi":
      return "Kimi は Moonshot AI の OpenAI互換APIを使います。API keyを設定してください。";
    case "ollama":
      return "Ollama はインストール済みモデルと Pull 候補を分けて表示します。";
    default:
      return "プロバイダーが受け付けるモデルIDを指定します。";
  }
}

export function modelSourceLabel(item: LlmModelOption): string | null {
  if (item.source_label) return item.source_label;
  if (item.installed) return "インストール済み";
  if (item.custom_current) return "現在の設定";
  return null;
}

export function modelSummary(item: LlmModelOption): string {
  if (item.description) return item.description;
  if (item.server_label && item.base_url) return `${item.server_label} ${item.base_url}`;
  if (item.context_length) return `context ${item.context_length}`;
  if (item.details) {
    return `${item.details.parameter_size || "-"} / ${item.details.quantization_level || "-"} / ${formatBytes(item.size)}`;
  }
  return item.id;
}

export function providerSourceLabel(source: string): string {
  switch (source) {
    case "remote":
      return "API取得";
    case "cached":
      return "前回取得";
    case "installed":
      return "インストール確認済み";
    case "cli-suggested":
      return "CLI候補";
    case "platform-suggested":
      return "OS候補";
    case "static-suggested":
      return "静的候補";
    case "static":
      return "静的候補";
    default:
      return source || "候補";
  }
}

export function providerSelection(provider: LlmProviderCatalog | null | undefined): ProviderDraft {
  const firstModel = provider?.models[0]?.id ?? "";
  const configuredModel = provider?.configured_model?.trim();
  if (!configuredModel) {
    return { model: firstModel, customModel: "" };
  }
  if (provider?.models.some((item) => item.id === configuredModel)) {
    return { model: configuredModel, customModel: "" };
  }
  return { model: firstModel, customModel: configuredModel };
}

export function modelOptionSettings(option: LlmModelOption | null | undefined): ProviderSettingsDraft | undefined {
  if (!option?.base_url) return undefined;
  return { base_url: option.base_url };
}

export function defaultModeForOptions(options: string[] | undefined, preferred = "medium"): string {
  const values = options ?? [];
  if (!values.length) return preferred;
  if (values.includes(preferred)) return preferred;
  if (values.includes("fast")) return "fast";
  if (values.includes("medium")) return "medium";
  return values[0];
}

export function routeSelection(
  provider: LlmProviderCatalog | null | undefined,
  modelId: string,
): ProviderDraft {
  if (!provider) return { model: modelId, customModel: "" };
  return providerSelection({ ...provider, configured_model: modelId });
}

export function buildRouteDrafts(
  members: Record<string, AgentTeamMemberSettings> | undefined,
  providers: LlmProviderCatalog[] | undefined,
  roster: ModelRouteSettings[] = [],
): Record<ModelRouteKey, ModelRouteDraft> {
  return Object.fromEntries(
    MODEL_ROUTE_DEFINITIONS.map((definition) => {
      const member = members?.[definition.key] ?? {};
      const route = member.override ?? member;
      const effective = roster.find((item) => item.key === definition.key || item.member_key === definition.key || item.role === definition.key) ?? {};
      const isHarness = definition.key === "agent_harness";
      const routeProvider = route.provider || (isHarness ? definition.defaultProvider : "");
      const routeModel = route.model || (isHarness ? definition.defaultModel : "");
      const providerCatalog = providers?.find((item) => item.id === routeProvider);
      const selection = routeProvider && routeModel
        ? routeSelection(providerCatalog, routeModel)
        : { model: "", customModel: "" };
      return [
        definition.key,
        {
          enabled: member.enabled ?? false,
          groupId: member.group_id ?? "",
          overrideOpen: false,
          provider: routeProvider,
          model: selection.model,
          customModel: selection.customModel,
          mode: route.effort || member.mode || member.reasoning_effort || "",
          scalable: definition.scalable ?? false,
          defaultInstances: member.default_instances ?? 1,
          maxInstances: member.max_instances ?? definition.defaultMaxInstances ?? 1,
          runner: route.runner ?? "",
          effortPolicy: route.effort_policy ?? member.effort_policy ?? "inherit",
          effectiveProvider: effective.effective_provider ?? effective.provider ?? "",
          effectiveModel: effective.effective_model ?? effective.model ?? "",
          effectiveEffort: effective.effective_effort ?? effective.reasoning_effort ?? effective.mode ?? "",
        },
      ];
    }),
  ) as Record<ModelRouteKey, ModelRouteDraft>;
}

export function buildClassDraft(
  route: (ModelRouteSettings & { base_url?: string; api_key?: string; engine?: "speech_recognition" | "llm" | "off" }) | undefined,
  providers: LlmProviderCatalog[] | undefined,
): ModelClassDraft {
  const routeProvider = route?.provider || "";
  const routeModel = route?.model || "";
  const providerCatalog = providers?.find((item) => item.id === routeProvider);
  const selection = routeProvider && routeModel
    ? routeSelection(providerCatalog, routeModel)
    : { model: "", customModel: "" };
  return {
    provider: routeProvider,
    model: selection.model,
    customModel: selection.customModel || (!providerCatalog && routeModel ? routeModel : ""),
    mode: route?.effort || route?.mode || route?.reasoning_effort || "",
    baseUrl: route?.base_url || "",
    apiKey: "",
    effortPolicy: (route?.effort_policy as EffortPolicy) || "same",
    engine: route?.engine,
    inherit: route?.inherit ?? false,
  };
}
