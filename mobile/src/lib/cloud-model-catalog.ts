/**
 * クラウド直結LLMプロバイダーとモデル候補の一元定義。
 *
 * ここが唯一の正本。settings 画面や mobile-llm のアダプター選択は
 * すべてこの定義を参照し、プロバイダーごとの分岐を各所へ持ち込まない。
 *
 * モデル候補は各プロバイダーAPIから動的取得する（fetchCloudModels）。
 * 静的候補（models）は取得失敗時・APIキー未入力時のオフラインシード。
 */

import AsyncStorage from "@react-native-async-storage/async-storage";
import { MODEL_LIST_TIMEOUT, STORAGE_KEYS } from "../constants/config";

export type DirectMobileLlmProvider =
  | "openai"
  | "gemini"
  | "kimi"
  | "openrouter"
  | "anthropic"
  | "custom";

export type MobileLlmProvider = "server" | DirectMobileLlmProvider;

/** アダプター種別（URL・認証・ボディ・抽出の実装系統）。 */
export type CloudAdapterKind = "openai_chat" | "gemini" | "anthropic";

export interface CloudModelCandidate {
  /** API へ渡すモデルID。 */
  id: string;
  /** UI 表示ラベル。省略時は id をそのまま使う。 */
  label?: string;
}

export interface CloudProviderDefinition {
  id: DirectMobileLlmProvider;
  /** UI 表示名。 */
  label: string;
  /** リクエスト実装系統。 */
  adapter: CloudAdapterKind;
  /** 既定 Base URL。 */
  defaultBaseUrl: string;
  /** Base URL をユーザーが編集できるか。 */
  baseUrlEditable: boolean;
  /** Base URL が必須か（custom のみ true）。 */
  baseUrlRequired: boolean;
  /** 上級者向け（通常候補と視覚的に分離する）か。 */
  advanced: boolean;
  /** 代表モデル候補。custom は空（完全手入力）。 */
  models: CloudModelCandidate[];
  /** 既定モデルID。custom は空。 */
  defaultModel: string;
  /** 短い補足文言。 */
  hint?: string;
}

export const GEMINI_DEFAULT_BASE_URL =
  "https://generativelanguage.googleapis.com/v1beta";
export const OPENAI_DEFAULT_BASE_URL = "https://api.openai.com/v1";
export const KIMI_DEFAULT_BASE_URL = "https://api.moonshot.ai/v1";
export const OPENROUTER_DEFAULT_BASE_URL = "https://openrouter.ai/api/v1";
export const ANTHROPIC_DEFAULT_BASE_URL = "https://api.anthropic.com";

export const CLOUD_PROVIDER_DEFINITIONS: Record<
  DirectMobileLlmProvider,
  CloudProviderDefinition
> = {
  openai: {
    id: "openai",
    label: "OpenAI",
    adapter: "openai_chat",
    defaultBaseUrl: OPENAI_DEFAULT_BASE_URL,
    baseUrlEditable: false,
    baseUrlRequired: false,
    advanced: false,
    models: [
      { id: "gpt-5.5" },
      { id: "gpt-5.4" },
      { id: "gpt-4o-mini" },
    ],
    defaultModel: "gpt-5.5",
    hint: "自分のOpenAI APIキーが必要です。",
  },
  gemini: {
    id: "gemini",
    label: "Gemini",
    adapter: "gemini",
    defaultBaseUrl: GEMINI_DEFAULT_BASE_URL,
    baseUrlEditable: false,
    baseUrlRequired: false,
    advanced: false,
    models: [
      { id: "gemini-3-flash-preview" },
      { id: "gemini-3.1-pro-preview" },
      { id: "gemini-2.5-flash" },
    ],
    defaultModel: "gemini-3-flash-preview",
    hint: "自分のGoogle AI Studio APIキーが必要です。",
  },
  kimi: {
    id: "kimi",
    label: "Kimi",
    adapter: "openai_chat",
    defaultBaseUrl: KIMI_DEFAULT_BASE_URL,
    baseUrlEditable: true,
    baseUrlRequired: false,
    advanced: false,
    models: [{ id: "kimi-k3" }],
    defaultModel: "kimi-k3",
    hint: "自分のMoonshot AI APIキーが必要です。",
  },
  openrouter: {
    id: "openrouter",
    label: "OpenRouter",
    adapter: "openai_chat",
    defaultBaseUrl: OPENROUTER_DEFAULT_BASE_URL,
    baseUrlEditable: false,
    baseUrlRequired: false,
    advanced: false,
    models: [
      { id: "openai/gpt-5.5" },
      { id: "anthropic/claude-sonnet-4.5" },
      { id: "google/gemini-3.1-pro-preview" },
      { id: "openai/gpt-4o-mini" },
    ],
    defaultModel: "openai/gpt-5.5",
    hint: "自分のOpenRouter APIキーが必要です。",
  },
  anthropic: {
    id: "anthropic",
    label: "Anthropic",
    adapter: "anthropic",
    defaultBaseUrl: ANTHROPIC_DEFAULT_BASE_URL,
    baseUrlEditable: false,
    baseUrlRequired: false,
    advanced: false,
    models: [
      { id: "claude-sonnet-5" },
      { id: "claude-opus-4-8" },
      { id: "claude-haiku-4-5" },
    ],
    defaultModel: "claude-sonnet-5",
    hint: "自分のAnthropic APIキーが必要です。",
  },
  custom: {
    id: "custom",
    label: "カスタム(OpenAI互換エンドポイント)",
    adapter: "openai_chat",
    defaultBaseUrl: OPENAI_DEFAULT_BASE_URL,
    baseUrlEditable: true,
    baseUrlRequired: true,
    advanced: true,
    models: [],
    defaultModel: "",
    hint: "OpenAI互換の /chat/completions を提供する任意エンドポイント。Base URL 必須。",
  },
};

/** UI 表示順（通常プロバイダー → 上級者向け）。 */
export const DIRECT_PROVIDER_ORDER: DirectMobileLlmProvider[] = [
  "openai",
  "gemini",
  "kimi",
  "openrouter",
  "anthropic",
  "custom",
];

export function isDirectMobileLlmProvider(
  value: unknown,
): value is DirectMobileLlmProvider {
  return (
    value === "openai" ||
    value === "gemini" ||
    value === "kimi" ||
    value === "openrouter" ||
    value === "anthropic" ||
    value === "custom"
  );
}

export function getProviderDefinition(
  provider: DirectMobileLlmProvider,
): CloudProviderDefinition {
  return CLOUD_PROVIDER_DEFINITIONS[provider];
}

export function getProviderLabel(provider: MobileLlmProvider): string {
  if (provider === "server") return "Server";
  return CLOUD_PROVIDER_DEFINITIONS[provider].label;
}

export function getDefaultModelForProvider(
  provider: DirectMobileLlmProvider,
): string {
  return CLOUD_PROVIDER_DEFINITIONS[provider].defaultModel;
}

export function getDefaultBaseUrlForProvider(
  provider: DirectMobileLlmProvider,
): string {
  return CLOUD_PROVIDER_DEFINITIONS[provider].defaultBaseUrl;
}

export function getAdapterKind(
  provider: DirectMobileLlmProvider,
): CloudAdapterKind {
  return CLOUD_PROVIDER_DEFINITIONS[provider].adapter;
}

/** プロバイダーの静的シードモデルID（オフライン時の初期表示）。 */
export function getSeedModelIds(provider: DirectMobileLlmProvider): string[] {
  return CLOUD_PROVIDER_DEFINITIONS[provider].models.map((model) => model.id);
}

/** 動的取得結果と静的シードを重複なく結合する（動的を優先表示）。 */
export function mergeModelIds(
  primary: readonly string[],
  fallback: readonly string[],
): string[] {
  const seen = new Set<string>();
  const result: string[] = [];
  for (const id of [...primary, ...fallback]) {
    const trimmed = id.trim();
    if (!trimmed || seen.has(trimmed)) continue;
    seen.add(trimmed);
    result.push(trimmed);
  }
  return result;
}

/* -------------------------------------------------------------------------- */
/* プロバイダーAPIからのモデル一覧取得                                         */
/* -------------------------------------------------------------------------- */

export interface FetchCloudModelsOptions {
  apiKey?: string;
  baseUrl?: string;
}

function trimTrailingSlash(url: string): string {
  return url.replace(/\/+$/, "");
}

function resolveBaseUrl(
  provider: DirectMobileLlmProvider,
  baseUrl?: string,
): string {
  const raw = (baseUrl ?? "").trim() || getDefaultBaseUrlForProvider(provider);
  return trimTrailingSlash(raw);
}

async function fetchJsonWithTimeout(
  url: string,
  init: RequestInit,
): Promise<unknown> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), MODEL_LIST_TIMEOUT);
  try {
    const response = await fetch(url, { ...init, signal: controller.signal });
    if (!response.ok) {
      throw new Error(`モデル一覧の取得に失敗しました: ${response.status}`);
    }
    return await response.json();
  } finally {
    clearTimeout(timer);
  }
}

// chat/completions 系に関係の薄いモデルを軽く除外する（過剰フィルタは避ける）。
const NON_CHAT_MODEL_PATTERN =
  /(embedding|whisper|\btts\b|text-to-speech|audio|transcribe|dall-?e|moderation|image|realtime)/i;

function isLikelyChatModel(id: string): boolean {
  return !NON_CHAT_MODEL_PATTERN.test(id);
}

function extractOpenAiCompatibleIds(data: unknown): string[] {
  const record = data as { data?: Array<{ id?: unknown }> } | null;
  const items = Array.isArray(record?.data) ? record.data : [];
  return items
    .map((item) => (typeof item?.id === "string" ? item.id : ""))
    .filter((id): id is string => id.length > 0);
}

async function fetchOpenAiModels(
  provider: DirectMobileLlmProvider,
  options: FetchCloudModelsOptions,
): Promise<string[]> {
  const baseUrl = resolveBaseUrl(provider, options.baseUrl);
  const apiKey = (options.apiKey ?? "").trim();
  const headers: Record<string, string> = {};
  // openrouter / custom は公開一覧のためキー任意。あれば付与する。
  if (apiKey) headers.Authorization = `Bearer ${apiKey}`;
  const data = await fetchJsonWithTimeout(`${baseUrl}/models`, { headers });
  return extractOpenAiCompatibleIds(data).filter(isLikelyChatModel);
}

async function fetchOpenRouterModels(
  options: FetchCloudModelsOptions,
): Promise<string[]> {
  const baseUrl = resolveBaseUrl("openrouter", options.baseUrl);
  const apiKey = (options.apiKey ?? "").trim();
  const headers: Record<string, string> = {};
  if (apiKey) headers.Authorization = `Bearer ${apiKey}`;
  const data = await fetchJsonWithTimeout(`${baseUrl}/models`, { headers });
  // OpenRouter は用途が多岐にわたるため chat フィルタはかけない。
  return extractOpenAiCompatibleIds(data);
}

async function fetchGeminiModels(
  options: FetchCloudModelsOptions,
): Promise<string[]> {
  const baseUrl = resolveBaseUrl("gemini", options.baseUrl);
  const apiKey = (options.apiKey ?? "").trim();
  if (!apiKey) return [];
  const data = await fetchJsonWithTimeout(
    `${baseUrl}/models?key=${encodeURIComponent(apiKey)}`,
    {},
  );
  const record = data as
    | {
        models?: Array<{
          name?: unknown;
          supportedGenerationMethods?: unknown;
        }>;
      }
    | null;
  const items = Array.isArray(record?.models) ? record.models : [];
  const result: string[] = [];
  for (const item of items) {
    const name = typeof item?.name === "string" ? item.name : "";
    if (!name.startsWith("models/")) continue;
    const methods = Array.isArray(item?.supportedGenerationMethods)
      ? (item.supportedGenerationMethods as unknown[])
      : [];
    if (methods.length > 0 && !methods.includes("generateContent")) continue;
    result.push(name.slice("models/".length));
  }
  return result;
}

async function fetchAnthropicModels(
  options: FetchCloudModelsOptions,
): Promise<string[]> {
  const baseUrl = resolveBaseUrl("anthropic", options.baseUrl);
  const apiKey = (options.apiKey ?? "").trim();
  if (!apiKey) return [];
  const data = await fetchJsonWithTimeout(`${baseUrl}/v1/models`, {
    headers: {
      "x-api-key": apiKey,
      "anthropic-version": "2023-06-01",
    },
  });
  return extractOpenAiCompatibleIds(data);
}

/**
 * プロバイダーAPIから最新モデルIDの一覧を取得する。
 * 取得できない（キー未入力・ネットワーク失敗など）場合は空配列 or throw。
 * custom は手入力前提のため失敗しても致命扱いにしない（空配列を返す）。
 */
export async function fetchCloudModels(
  provider: DirectMobileLlmProvider,
  options: FetchCloudModelsOptions = {},
): Promise<string[]> {
  switch (provider) {
    case "openai":
      return fetchOpenAiModels("openai", options);
    case "kimi":
      return fetchOpenAiModels("kimi", options);
    case "openrouter":
      return fetchOpenRouterModels(options);
    case "gemini":
      return fetchGeminiModels(options);
    case "anthropic":
      return fetchAnthropicModels(options);
    case "custom":
      try {
        return await fetchOpenAiModels("custom", options);
      } catch {
        // 任意エンドポイントは /models 非対応もあり得る。手入力へフォールバック。
        return [];
      }
  }
}

/* -------------------------------------------------------------------------- */
/* モデル一覧キャッシュ（AsyncStorage・モデルIDのみ。APIキーは保持しない）     */
/* -------------------------------------------------------------------------- */

interface CachedModelEntry {
  models: string[];
  updatedAt: string;
}

type ModelCatalogCache = Partial<Record<DirectMobileLlmProvider, CachedModelEntry>>;

async function readModelCatalogCache(): Promise<ModelCatalogCache> {
  try {
    const raw = await AsyncStorage.getItem(
      STORAGE_KEYS.CHAT_LLM_MODEL_CATALOG_CACHE,
    );
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object"
      ? (parsed as ModelCatalogCache)
      : {};
  } catch {
    return {};
  }
}

/** キャッシュ済みモデルID配列を返す（無ければ空配列）。 */
export async function readCachedModels(
  provider: DirectMobileLlmProvider,
): Promise<string[]> {
  const cache = await readModelCatalogCache();
  const entry = cache[provider];
  const models = entry?.models;
  if (!Array.isArray(models)) return [];
  return models.filter((id): id is string => typeof id === "string" && !!id.trim());
}

/**
 * モデルID配列をキャッシュへ保存する。
 * 保存対象はモデルIDのみで、APIキー等の秘密情報は一切含めない。
 */
export async function writeCachedModels(
  provider: DirectMobileLlmProvider,
  models: readonly string[],
): Promise<void> {
  const normalized = models
    .map((id) => id.trim())
    .filter((id) => id.length > 0);
  if (normalized.length === 0) return;
  const cache = await readModelCatalogCache();
  cache[provider] = {
    models: Array.from(new Set(normalized)),
    updatedAt: new Date().toISOString(),
  };
  try {
    await AsyncStorage.setItem(
      STORAGE_KEYS.CHAT_LLM_MODEL_CATALOG_CACHE,
      JSON.stringify(cache),
    );
  } catch {
    // 永続化失敗は致命ではない（次回また取得する）。
  }
}
