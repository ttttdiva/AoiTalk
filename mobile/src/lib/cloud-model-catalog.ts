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
import {
  descriptorForUrl,
  executeMobileEgress,
  fetchWithTimeout,
  type MobileEgressReviewCallback,
  type MobilePrivacyMode,
  type MobileReviewPolicy,
} from "../privacy/outbound-gateway";

export type DirectMobileLlmProvider =
  | "openai"
  | "gemini"
  | "deepseek"
  | "deepinfra"
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
export const DEEPSEEK_DEFAULT_BASE_URL = "https://api.deepseek.com";
export const DEEPINFRA_DEFAULT_BASE_URL = "https://api.deepinfra.com/v1/openai";
export const OPENROUTER_DEFAULT_BASE_URL = "https://openrouter.ai/api/v1";
export const ANTHROPIC_DEFAULT_BASE_URL = "https://api.anthropic.com";

// 廃止済みモデルIDは送信・保存せず、動的カタログからも除外する。
// 文字列を分割して保持し、古いIDを通常の候補として再利用できない形にする。
export const FORBIDDEN_MODEL_ID = ["gpt", "4o", "mini"].join("-");

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
      { id: "gpt-5.6-luna" },
      { id: "gpt-5.5" },
      { id: "gpt-5.4" },
    ],
    defaultModel: "gpt-5.6-luna",
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
  deepseek: {
    id: "deepseek",
    label: "DeepSeek",
    adapter: "openai_chat",
    defaultBaseUrl: DEEPSEEK_DEFAULT_BASE_URL,
    baseUrlEditable: true,
    baseUrlRequired: false,
    advanced: false,
    models: [{ id: "deepseek-v4-flash" }, { id: "deepseek-v4-pro" }],
    defaultModel: "deepseek-v4-flash",
    hint: "自分のDeepSeek APIキーが必要です。公式APIのBase URLは /v1 不要です。",
  },
  deepinfra: {
    id: "deepinfra",
    label: "DeepInfra",
    adapter: "openai_chat",
    defaultBaseUrl: DEEPINFRA_DEFAULT_BASE_URL,
    baseUrlEditable: true,
    baseUrlRequired: false,
    advanced: false,
    models: [
      { id: "deepseek-ai/DeepSeek-V4-Flash" },
      { id: "deepseek-ai/DeepSeek-V4-Pro" },
    ],
    defaultModel: "deepseek-ai/DeepSeek-V4-Flash",
    hint: "自分のDeepInfra API tokenが必要です。Chat URLは /v1/openai、モデル一覧は公式 /v1/models から取得します。",
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
  "deepseek",
  "deepinfra",
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
    value === "deepseek" ||
    value === "deepinfra" ||
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
  return sanitizeModelIds(
    CLOUD_PROVIDER_DEFINITIONS[provider].models.map((model) => model.id),
  );
}

/**
 * 既知の廃止モデルを、どのプロバイダー経由でも選択・送信しないための判定。
 *
 * OpenRouter のようなプロバイダーは provider prefix 付きの形で返すため、
 * 最後の path segment も確認する。大文字・前後空白は API / SecureStore の
 * 取り込み時に揺れ得るので、判定時に正規化する。
 */
export function isForbiddenModelId(value: unknown): boolean {
  const normalized = String(value ?? "").trim().toLowerCase();
  if (!normalized) return false;
  const terminal = (normalized.split("/").at(-1) ?? normalized).split(":", 1)[0];
  return (
    terminal === FORBIDDEN_MODEL_ID ||
    terminal.startsWith(`${FORBIDDEN_MODEL_ID}-`)
  );
}

/** モデルID配列から廃止モデル・空値・重複を除去する。 */
export function sanitizeModelIds(values: readonly unknown[]): string[] {
  const seen = new Set<string>();
  const result: string[] = [];
  for (const value of values) {
    const trimmed = typeof value === "string" ? value.trim() : "";
    if (!trimmed || isForbiddenModelId(trimmed) || seen.has(trimmed)) continue;
    seen.add(trimmed);
    result.push(trimmed);
  }
  return result;
}

/** 動的取得結果と静的シードを重複なく結合する（動的を優先表示）。 */
export function mergeModelIds(
  primary: readonly string[],
  fallback: readonly string[],
): string[] {
  return sanitizeModelIds([...primary, ...fallback]);
}

/* -------------------------------------------------------------------------- */
/* プロバイダーAPIからのモデル一覧取得                                         */
/* -------------------------------------------------------------------------- */

export interface FetchCloudModelsOptions {
  apiKey?: string;
  baseUrl?: string;
  /** Optional local-only/review policy for the catalog transport. */
  privacyMode?: MobilePrivacyMode;
  reviewPolicy?: MobileReviewPolicy;
  trustedLocalHosts?: readonly string[];
  review?: MobileEgressReviewCallback;
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
  provider: DirectMobileLlmProvider,
  url: string,
  init: RequestInit,
  options: FetchCloudModelsOptions,
): Promise<unknown> {
  const response = await executeMobileEgress(
    { url, init },
    {
      descriptor: descriptorForUrl(
        {
          action: "model.catalog",
          transport: "http.fetch",
          destination: options.baseUrl,
          provider,
          tool: "cloud_model_catalog",
        },
        url,
      ),
      mode: options.privacyMode ?? "direct",
      reviewPolicy: options.reviewPolicy ?? "high_risk",
      trustedLocalHosts: options.trustedLocalHosts ?? [],
      review: options.review,
    },
    (request) => fetchWithTimeout(request.url, request.init, MODEL_LIST_TIMEOUT),
  );
  if (!response.ok) {
    throw new Error(`モデル一覧の取得に失敗しました: ${response.status}`);
  }
  return response.json();
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
  return sanitizeModelIds(items
    .map((item) => (typeof item?.id === "string" ? item.id : ""))
    .filter((id): id is string => id.length > 0));
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
  const data = await fetchJsonWithTimeout(provider, `${baseUrl}/models`, { headers }, options);
  return extractOpenAiCompatibleIds(data).filter(isLikelyChatModel);
}

function resolveDeepInfraModelsUrl(baseUrl?: string): string {
  const normalized = resolveBaseUrl("deepinfra", baseUrl);
  if (normalized.endsWith("/openai")) {
    return `${normalized.slice(0, -"/openai".length)}/models`;
  }
  return "";
}

async function fetchDeepInfraModels(
  options: FetchCloudModelsOptions,
): Promise<string[]> {
  const apiKey = (options.apiKey ?? "").trim();
  const modelsUrl = resolveDeepInfraModelsUrl(options.baseUrl);
  if (!apiKey || !modelsUrl) return [];
  const data = await fetchJsonWithTimeout(
    "deepinfra",
    modelsUrl,
    { headers: { Authorization: `Bearer ${apiKey}` } },
    options,
  );
  const record = data as
    | {
        data?: Array<{
          id?: unknown;
          model?: unknown;
          name?: unknown;
          reported_type?: unknown;
          task?: unknown;
          type?: unknown;
          deprecated?: unknown;
        }>;
        models?: Array<Record<string, unknown>>;
      }
    | null;
  const items = Array.isArray(record?.data)
    ? record.data
    : Array.isArray(record?.models)
      ? record.models
      : [];
  const nonText = /(embedding|image|audio|speech|whisper|moderation|rerank|transcrib)/i;
  const ids: string[] = [];
  for (const item of items) {
    if (item?.deprecated === true) continue;
    const id = [item?.id, item?.model, item?.name].find(
      (value): value is string => typeof value === "string" && value.trim().length > 0,
    );
    if (!id) continue;
    const declared = [item?.reported_type, item?.task, item?.type]
      .find((value): value is string => typeof value === "string" && value.trim().length > 0)
      ?.toLowerCase();
    if (declared && (nonText.test(declared) || !/(text|chat|generation|causal)/i.test(declared))) continue;
    ids.push(id);
  }
  return sanitizeModelIds(ids);
}

async function fetchOpenRouterModels(
  options: FetchCloudModelsOptions,
): Promise<string[]> {
  const baseUrl = resolveBaseUrl("openrouter", options.baseUrl);
  const apiKey = (options.apiKey ?? "").trim();
  const headers: Record<string, string> = {};
  if (apiKey) headers.Authorization = `Bearer ${apiKey}`;
  const data = await fetchJsonWithTimeout("openrouter", `${baseUrl}/models`, { headers }, options);
  // OpenRouter は用途が多岐にわたるため chat フィルタはかけない。
  return sanitizeModelIds(extractOpenAiCompatibleIds(data));
}

async function fetchGeminiModels(
  options: FetchCloudModelsOptions,
): Promise<string[]> {
  const baseUrl = resolveBaseUrl("gemini", options.baseUrl);
  const apiKey = (options.apiKey ?? "").trim();
  if (!apiKey) return [];
  const data = await fetchJsonWithTimeout(
    "gemini",
    `${baseUrl}/models?key=${encodeURIComponent(apiKey)}`,
    {},
    options,
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
  return sanitizeModelIds(result);
}

async function fetchAnthropicModels(
  options: FetchCloudModelsOptions,
): Promise<string[]> {
  const baseUrl = resolveBaseUrl("anthropic", options.baseUrl);
  const apiKey = (options.apiKey ?? "").trim();
  if (!apiKey) return [];
  const data = await fetchJsonWithTimeout(
    "anthropic",
    `${baseUrl}/v1/models`,
    {
      headers: {
        "x-api-key": apiKey,
        "anthropic-version": "2023-06-01",
      },
    },
    options,
  );
  return sanitizeModelIds(extractOpenAiCompatibleIds(data));
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
    case "deepseek":
      return fetchOpenAiModels("deepseek", options);
    case "deepinfra":
      return fetchDeepInfraModels(options);
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
  const sanitized = sanitizeModelIds(models);
  if (sanitized.length !== models.length) {
    cache[provider] = {
      models: sanitized,
      updatedAt: new Date().toISOString(),
    };
    try {
      await AsyncStorage.setItem(
        STORAGE_KEYS.CHAT_LLM_MODEL_CATALOG_CACHE,
        JSON.stringify(cache),
      );
    } catch {
      // キャッシュ浄化の失敗は致命ではない（次回再試行する）。
    }
  }
  return sanitized;
}

/**
 * モデルID配列をキャッシュへ保存する。
 * 保存対象はモデルIDのみで、APIキー等の秘密情報は一切含めない。
 */
export async function writeCachedModels(
  provider: DirectMobileLlmProvider,
  models: readonly string[],
): Promise<void> {
  const rawNormalized = models
    .map((id) => id.trim())
    .filter((id) => id.length > 0);
  if (rawNormalized.length === 0) return;
  const normalized = sanitizeModelIds(rawNormalized);
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
