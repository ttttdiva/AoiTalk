import * as SecureStore from "expo-secure-store";
import { CHAT_TIMEOUT, STORAGE_KEYS } from "../constants/config";
import type { ConversationMessage } from "../types/api";
import {
  CLOUD_PROVIDER_DEFINITIONS,
  getAdapterKind,
  getDefaultBaseUrlForProvider,
  getDefaultModelForProvider,
  isDirectMobileLlmProvider,
  type CloudAdapterKind,
  type DirectMobileLlmProvider,
  type MobileLlmProvider,
} from "./cloud-model-catalog";

export type { DirectMobileLlmProvider, MobileLlmProvider };

/** プロバイダー単位で共有するプロファイル（APIキー / Base URL）。 */
export interface MobileLlmProviderProfile {
  apiKey: string;
  baseUrl: string;
}

/** スロット（メイン / フォールバック）のプロバイダーとモデルID。 */
export interface MobileLlmSlotSelection {
  provider: MobileLlmProvider;
  model: string;
}

/** フォールバック設定。APIキー / Base URL はプロバイダープロファイルを参照。 */
export interface MobileLlmFallbackConfig {
  enabled: boolean;
  provider: DirectMobileLlmProvider;
  model: string;
}

/** 1 回の呼び出しに必要な解決済み設定。 */
export interface MobileLlmSettings {
  provider: MobileLlmProvider;
  apiKey: string;
  model: string;
  baseUrl: string;
  maxTokens?: number;
  reasoningEffort?: string;
}

export interface DirectMobileLlmSelection {
  provider: DirectMobileLlmProvider;
  model: string;
  reasoningEffort?: string;
}

export const KIMI_ASSISTANT_PAYLOAD_METADATA_KEY = "direct_assistant_payload";

export interface MobileLlmReply {
  content: string;
  assistantPayload?: Record<string, unknown>;
}

export function isDirectProvider(
  provider: MobileLlmProvider,
): provider is DirectMobileLlmProvider {
  return provider !== "server" && isDirectMobileLlmProvider(provider);
}

function normalizeMainProvider(value: string | null): MobileLlmProvider {
  if (value === "server") return "server";
  // 旧 "openai_compatible" は custom へ写像（移行前でも安全側）。
  if (value === "openai_compatible") return "custom";
  if (value && isDirectMobileLlmProvider(value)) return value;
  return "server";
}

function normalizeDirectProvider(
  value: string | null,
): DirectMobileLlmProvider {
  if (value === "openai_compatible") return "custom";
  if (value && isDirectMobileLlmProvider(value)) return value;
  return "openai";
}

/* -------------------------------------------------------------------------- */
/* プロバイダープロファイル（APIキー / Base URL）                              */
/* -------------------------------------------------------------------------- */

function getProfileStorageKeys(provider: DirectMobileLlmProvider): {
  apiKey: string;
  baseUrl: string;
} {
  switch (provider) {
    case "openai":
      return {
        apiKey: STORAGE_KEYS.CHAT_LLM_OPENAI_API_KEY,
        baseUrl: STORAGE_KEYS.CHAT_LLM_OPENAI_BASE_URL,
      };
    case "gemini":
      return {
        apiKey: STORAGE_KEYS.CHAT_LLM_GEMINI_API_KEY,
        baseUrl: STORAGE_KEYS.CHAT_LLM_GEMINI_BASE_URL,
      };
    case "kimi":
      return {
        apiKey: STORAGE_KEYS.CHAT_LLM_KIMI_API_KEY,
        baseUrl: STORAGE_KEYS.CHAT_LLM_KIMI_BASE_URL,
      };
    case "openrouter":
      return {
        apiKey: STORAGE_KEYS.CHAT_LLM_OPENROUTER_API_KEY,
        baseUrl: STORAGE_KEYS.CHAT_LLM_OPENROUTER_BASE_URL,
      };
    case "anthropic":
      return {
        apiKey: STORAGE_KEYS.CHAT_LLM_ANTHROPIC_API_KEY,
        baseUrl: STORAGE_KEYS.CHAT_LLM_ANTHROPIC_BASE_URL,
      };
    case "custom":
      return {
        apiKey: STORAGE_KEYS.CHAT_LLM_CUSTOM_API_KEY,
        baseUrl: STORAGE_KEYS.CHAT_LLM_CUSTOM_BASE_URL,
      };
  }
}

export async function getProviderProfile(
  provider: DirectMobileLlmProvider,
): Promise<MobileLlmProviderProfile> {
  await ensureMobileLlmMigrated();
  const keys = getProfileStorageKeys(provider);
  const [apiKey, baseUrl] = await Promise.all([
    SecureStore.getItemAsync(keys.apiKey),
    SecureStore.getItemAsync(keys.baseUrl),
  ]);
  return {
    apiKey: apiKey ?? "",
    baseUrl: baseUrl || getDefaultBaseUrlForProvider(provider),
  };
}

export async function saveProviderProfile(
  provider: DirectMobileLlmProvider,
  profile: MobileLlmProviderProfile,
): Promise<void> {
  const keys = getProfileStorageKeys(provider);
  const definition = CLOUD_PROVIDER_DEFINITIONS[provider];
  const baseUrl = profile.baseUrl.trim() || definition.defaultBaseUrl;
  if (definition.baseUrlRequired && !baseUrl.trim()) {
    throw new Error("このプロバイダーでは Base URL が必須です。");
  }
  await Promise.all([
    SecureStore.setItemAsync(keys.apiKey, profile.apiKey.trim()),
    SecureStore.setItemAsync(keys.baseUrl, baseUrl.trim()),
  ]);
}

/* -------------------------------------------------------------------------- */
/* メインスロット / フォールバック設定                                        */
/* -------------------------------------------------------------------------- */

export async function getMainSlot(): Promise<MobileLlmSlotSelection> {
  await ensureMobileLlmMigrated();
  const [providerRaw, modelRaw] = await Promise.all([
    SecureStore.getItemAsync(STORAGE_KEYS.CHAT_LLM_PROVIDER),
    SecureStore.getItemAsync(STORAGE_KEYS.CHAT_LLM_MAIN_MODEL),
  ]);
  const provider = normalizeMainProvider(providerRaw);
  const model = (modelRaw ?? "").trim();
  return { provider, model };
}

export async function saveMainSlot(
  provider: MobileLlmProvider,
  model: string,
): Promise<void> {
  const trimmedModel = model.trim();
  if (isDirectProvider(provider) && !trimmedModel) {
    throw new Error("モデルIDを指定してください。");
  }
  await Promise.all([
    SecureStore.setItemAsync(STORAGE_KEYS.CHAT_LLM_PROVIDER, provider),
    SecureStore.setItemAsync(STORAGE_KEYS.CHAT_LLM_MAIN_MODEL, trimmedModel),
  ]);
}

export async function getFallbackConfig(): Promise<MobileLlmFallbackConfig> {
  await ensureMobileLlmMigrated();
  const [enabledRaw, providerRaw, modelRaw] = await Promise.all([
    SecureStore.getItemAsync(STORAGE_KEYS.CHAT_LLM_FALLBACK_ENABLED),
    SecureStore.getItemAsync(STORAGE_KEYS.CHAT_LLM_FALLBACK_PROVIDER),
    SecureStore.getItemAsync(STORAGE_KEYS.CHAT_LLM_FALLBACK_MODEL),
  ]);
  const provider = normalizeDirectProvider(providerRaw);
  const model = (modelRaw ?? "").trim();
  return {
    enabled: enabledRaw === "1",
    provider,
    model,
  };
}

export async function saveFallbackConfig(
  config: MobileLlmFallbackConfig,
): Promise<void> {
  const trimmedModel = config.model.trim();
  if (config.enabled && !trimmedModel) {
    throw new Error("フォールバックのモデルIDを指定してください。");
  }
  await Promise.all([
    SecureStore.setItemAsync(
      STORAGE_KEYS.CHAT_LLM_FALLBACK_ENABLED,
      config.enabled ? "1" : "0",
    ),
    SecureStore.setItemAsync(
      STORAGE_KEYS.CHAT_LLM_FALLBACK_PROVIDER,
      config.provider,
    ),
    SecureStore.setItemAsync(
      STORAGE_KEYS.CHAT_LLM_FALLBACK_MODEL,
      trimmedModel,
    ),
  ]);
}

/* -------------------------------------------------------------------------- */
/* 解決（プロファイル + スロットモデル → 呼び出し用 MobileLlmSettings）        */
/* -------------------------------------------------------------------------- */

async function resolveDirectSettings(
  provider: DirectMobileLlmProvider,
  model: string,
  reasoningEffort?: string,
): Promise<MobileLlmSettings> {
  const profile = await getProviderProfile(provider);
  const resolvedModel = model.trim() || getDefaultModelForProvider(provider);
  return {
    provider,
    apiKey: profile.apiKey,
    model: resolvedModel,
    baseUrl: profile.baseUrl || getDefaultBaseUrlForProvider(provider),
    reasoningEffort,
  };
}

export async function getDirectMobileLlmSettings(
  selection: DirectMobileLlmSelection,
): Promise<MobileLlmSettings> {
  return resolveDirectSettings(
    selection.provider,
    selection.model,
    selection.reasoningEffort,
  );
}

export function getDirectReasoningEffortOptions(
  provider: DirectMobileLlmProvider,
  model: string,
): string[] {
  const normalized = model.toLowerCase();
  if (provider === "kimi" && normalized.includes("kimi-k3")) return ["max"];
  if (
    (provider === "openai" || provider === "openrouter" || provider === "custom") &&
    /(^|[/:-])(gpt-5|o[134])/.test(normalized)
  ) {
    return ["low", "medium", "high"];
  }
  return [];
}

export async function getMobileLlmSettings(): Promise<MobileLlmSettings> {
  const slot = await getMainSlot();
  if (!isDirectProvider(slot.provider)) {
    return {
      provider: "server",
      apiKey: "",
      model: slot.model,
      baseUrl: "",
    };
  }
  return resolveDirectSettings(slot.provider, slot.model);
}

/**
 * メインの Direct 設定を返す（未認証時にサーバー到達不可な場合の直結先）。
 * preferredProvider が Direct ならそのプロバイダーを、そうでなければ
 * フォールバック設定を解決する。既存コントローラーの分岐を維持するための互換関数。
 */
export async function getConfiguredDirectMobileLlmSettings(
  preferredProvider?: MobileLlmProvider,
): Promise<MobileLlmSettings | null> {
  if (preferredProvider && isDirectProvider(preferredProvider)) {
    const slot = await getMainSlot();
    const model =
      slot.provider === preferredProvider ? slot.model : "";
    const settings = await resolveDirectSettings(preferredProvider, model);
    return isMobileLlmConfigured(settings) ? settings : null;
  }
  return getConfiguredFallbackMobileLlmSettings();
}

/**
 * 有効なフォールバック設定を解決して返す。
 * enabled=false、または設定不足のときは null。
 */
export async function getConfiguredFallbackMobileLlmSettings(
  _mainProvider?: MobileLlmProvider,
): Promise<MobileLlmSettings | null> {
  const fallback = await getFallbackConfig();
  if (!fallback.enabled) return null;
  const settings = await resolveDirectSettings(
    fallback.provider,
    fallback.model,
  );
  return isMobileLlmConfigured(settings) ? settings : null;
}

export function isMobileLlmConfigured(settings: MobileLlmSettings): boolean {
  if (!isDirectProvider(settings.provider)) return false;
  if (!settings.apiKey.trim()) return false;
  if (!settings.model.trim()) return false;
  const definition = CLOUD_PROVIDER_DEFINITIONS[settings.provider];
  if (definition.baseUrlRequired && !settings.baseUrl.trim()) return false;
  return true;
}

/* -------------------------------------------------------------------------- */
/* 移行（初回読み込み時、冪等）                                                */
/* -------------------------------------------------------------------------- */

let migrationPromise: Promise<void> | null = null;

async function setIfEmpty(key: string, value: string | null | undefined) {
  if (!value) return;
  const current = await SecureStore.getItemAsync(key);
  if (current === null || current === "") {
    await SecureStore.setItemAsync(key, value);
  }
}

async function runMigration(): Promise<void> {
  const done = await SecureStore.getItemAsync(
    STORAGE_KEYS.CHAT_LLM_SLOT_MIGRATED,
  );
  if (done === "1") return;

  const [
    providerRaw,
    fallbackRaw,
    legacyApiKey,
    legacyModel,
    legacyBaseUrl,
    openaiModel,
    geminiModel,
    ocApiKey,
    ocModel,
    ocBaseUrl,
  ] = await Promise.all([
    SecureStore.getItemAsync(STORAGE_KEYS.CHAT_LLM_PROVIDER),
    SecureStore.getItemAsync(STORAGE_KEYS.CHAT_LLM_FALLBACK_PROVIDER),
    SecureStore.getItemAsync(STORAGE_KEYS.CHAT_LLM_API_KEY),
    SecureStore.getItemAsync(STORAGE_KEYS.CHAT_LLM_MODEL),
    SecureStore.getItemAsync(STORAGE_KEYS.CHAT_LLM_BASE_URL),
    SecureStore.getItemAsync(STORAGE_KEYS.CHAT_LLM_OPENAI_MODEL),
    SecureStore.getItemAsync(STORAGE_KEYS.CHAT_LLM_GEMINI_MODEL),
    SecureStore.getItemAsync(STORAGE_KEYS.CHAT_LLM_OPENAI_COMPATIBLE_API_KEY),
    SecureStore.getItemAsync(STORAGE_KEYS.CHAT_LLM_OPENAI_COMPATIBLE_MODEL),
    SecureStore.getItemAsync(STORAGE_KEYS.CHAT_LLM_OPENAI_COMPATIBLE_BASE_URL),
  ]);

  // 1. 旧共通キー（provider 分割以前）→ openai プロファイルへ。
  await setIfEmpty(STORAGE_KEYS.CHAT_LLM_OPENAI_API_KEY, legacyApiKey);
  await setIfEmpty(STORAGE_KEYS.CHAT_LLM_OPENAI_BASE_URL, legacyBaseUrl);

  // 2. 旧 openai_compatible プロファイル → custom プロファイルへ。
  await setIfEmpty(STORAGE_KEYS.CHAT_LLM_CUSTOM_API_KEY, ocApiKey);
  await setIfEmpty(STORAGE_KEYS.CHAT_LLM_CUSTOM_BASE_URL, ocBaseUrl);

  // 3. メインプロバイダーの写像（openai_compatible → custom）。
  const newMainProvider = normalizeMainProvider(providerRaw);
  if (providerRaw && providerRaw !== newMainProvider) {
    await SecureStore.setItemAsync(
      STORAGE_KEYS.CHAT_LLM_PROVIDER,
      newMainProvider,
    );
  }

  // 4. メインスロットモデル：旧プロバイダー単位モデルから引き当てる。
  const oldModelByProvider = (
    provider: MobileLlmProvider,
  ): string | null => {
    if (provider === "openai") return openaiModel;
    if (provider === "gemini") return geminiModel;
    if (provider === "custom") return ocModel;
    return null;
  };
  const mainModel = oldModelByProvider(newMainProvider) || legacyModel;
  await setIfEmpty(STORAGE_KEYS.CHAT_LLM_MAIN_MODEL, mainModel);

  // 5. フォールバック設定の再構築。
  const migratedEnabled = await SecureStore.getItemAsync(
    STORAGE_KEYS.CHAT_LLM_FALLBACK_ENABLED,
  );
  if (migratedEnabled === null) {
    const fallbackActive =
      fallbackRaw !== null && fallbackRaw !== "" && fallbackRaw !== "off";
    if (fallbackActive) {
      const fbProvider = normalizeDirectProvider(fallbackRaw);
      const fbModel =
        oldModelByProvider(fbProvider) || getDefaultModelForProvider(fbProvider);
      await SecureStore.setItemAsync(
        STORAGE_KEYS.CHAT_LLM_FALLBACK_PROVIDER,
        fbProvider,
      );
      await setIfEmpty(STORAGE_KEYS.CHAT_LLM_FALLBACK_MODEL, fbModel);
      await SecureStore.setItemAsync(
        STORAGE_KEYS.CHAT_LLM_FALLBACK_ENABLED,
        "1",
      );
    } else {
      // 旧値が "off"/未設定なら無効。プロバイダー文字列が "off" のままだと
      // normalize で openai になるため、既定 provider を書いておく。
      if (fallbackRaw === "off" || fallbackRaw === null || fallbackRaw === "") {
        await SecureStore.setItemAsync(
          STORAGE_KEYS.CHAT_LLM_FALLBACK_PROVIDER,
          "openai",
        );
      } else {
        await SecureStore.setItemAsync(
          STORAGE_KEYS.CHAT_LLM_FALLBACK_PROVIDER,
          normalizeDirectProvider(fallbackRaw),
        );
      }
      await SecureStore.setItemAsync(
        STORAGE_KEYS.CHAT_LLM_FALLBACK_ENABLED,
        "0",
      );
    }
  }

  await SecureStore.setItemAsync(STORAGE_KEYS.CHAT_LLM_SLOT_MIGRATED, "1");
}

export async function ensureMobileLlmMigrated(): Promise<void> {
  if (!migrationPromise) {
    migrationPromise = runMigration().catch((error) => {
      // 失敗したら次回再試行できるよう promise をクリア。
      migrationPromise = null;
      throw error;
    });
  }
  return migrationPromise;
}

/* -------------------------------------------------------------------------- */
/* アダプター（URL・認証・ボディ・レスポンス抽出・エラー抽出）                 */
/* -------------------------------------------------------------------------- */

type ContextRole = "system" | "user" | "assistant";

interface ContextMessage {
  role: ContextRole;
  content: string;
  [key: string]: unknown;
}

interface LlmAdapter {
  buildRequest(
    settings: MobileLlmSettings,
    context: ContextMessage[],
  ): { url: string; init: RequestInit };
  extractReply(data: unknown): MobileLlmReply | null;
  extractError(data: unknown, status: number): string;
}

function trimBaseUrl(baseUrl: string, fallback: string): string {
  return (baseUrl || fallback).replace(/\/+$/, "");
}

function readError(data: unknown, status: number): string {
  const record = data && typeof data === "object" ? (data as Record<string, unknown>) : null;
  const errorObj =
    record && typeof record.error === "object" && record.error !== null
      ? (record.error as Record<string, unknown>)
      : null;
  const message =
    (errorObj && typeof errorObj.message === "string" && errorObj.message) ||
    (record && typeof record.message === "string" && record.message) ||
    null;
  return String(message || `LLM request failed: ${status}`);
}

const openAiChatAdapter: LlmAdapter = {
  buildRequest(settings, context) {
    const baseUrl = trimBaseUrl(
      settings.baseUrl,
      getDefaultBaseUrlForProvider(settings.provider as DirectMobileLlmProvider),
    );
    const body: Record<string, unknown> = {
      model: settings.model.trim(),
      messages: context,
    };
    if (settings.provider === "kimi" && isKimiK3Model(settings.model)) {
      body.reasoning_effort = settings.reasoningEffort || "max";
      if (typeof settings.maxTokens === "number" && settings.maxTokens > 0) {
        body.max_completion_tokens = Math.floor(settings.maxTokens);
      }
    } else if (settings.reasoningEffort) {
      body.reasoning_effort = settings.reasoningEffort;
    }
    return {
      url: `${baseUrl}/chat/completions`,
      init: {
        method: "POST",
        headers: {
          Authorization: `Bearer ${settings.apiKey.trim()}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify(body),
      },
    };
  },
  extractReply(data) {
    const record = data as
      | { choices?: Array<{ message?: Record<string, unknown> }> }
      | null;
    const message = record?.choices?.[0]?.message;
    const content = message?.content;
    if (typeof content !== "string" || !content) return null;
    return { content, assistantPayload: message };
  },
  extractError: readError,
};

const geminiAdapter: LlmAdapter = {
  buildRequest(settings, context) {
    const baseUrl = trimBaseUrl(
      settings.baseUrl,
      getDefaultBaseUrlForProvider("gemini"),
    );
    const contents = context
      .filter((message) => message.role !== "system")
      .map((message) => ({
        role: message.role === "assistant" ? "model" : "user",
        parts: [{ text: message.content }],
      }));
    const systemText = context
      .filter((message) => message.role === "system")
      .map((message) => message.content)
      .join("\n")
      .trim();
    const body: Record<string, unknown> = { contents };
    if (systemText) {
      body.systemInstruction = { parts: [{ text: systemText }] };
    }
    return {
      url: `${baseUrl}/models/${encodeURIComponent(
        settings.model.trim(),
      )}:generateContent?key=${encodeURIComponent(settings.apiKey.trim())}`,
      init: {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    };
  },
  extractReply(data) {
    const record = data as
      | {
          candidates?: Array<{
            content?: { parts?: Array<{ text?: string }> };
          }>;
        }
      | null;
    const content = record?.candidates?.[0]?.content?.parts
      ?.map((part) => part.text ?? "")
      .join("");
    return content ? { content: String(content) } : null;
  },
  extractError: readError,
};

const anthropicAdapter: LlmAdapter = {
  buildRequest(settings, context) {
    const baseUrl = trimBaseUrl(
      settings.baseUrl,
      getDefaultBaseUrlForProvider("anthropic"),
    );
    const systemText = context
      .filter((message) => message.role === "system")
      .map((message) => message.content)
      .join("\n")
      .trim();
    const messages = context
      .filter((message) => message.role !== "system")
      .map((message) => ({
        role: message.role === "assistant" ? "assistant" : "user",
        content: message.content,
      }));
    const body: Record<string, unknown> = {
      model: settings.model.trim(),
      max_tokens: 4096,
      messages,
    };
    if (systemText) {
      body.system = systemText;
    }
    return {
      url: `${baseUrl}/v1/messages`,
      init: {
        method: "POST",
        headers: {
          "x-api-key": settings.apiKey.trim(),
          "anthropic-version": "2023-06-01",
          "Content-Type": "application/json",
        },
        body: JSON.stringify(body),
      },
    };
  },
  extractReply(data) {
    const record = data as
      | { content?: Array<{ text?: string }> }
      | null;
    const content = record?.content
      ?.map((block) => block.text ?? "")
      .join("");
    return content ? { content: String(content) } : null;
  },
  extractError: readError,
};

const ADAPTERS: Record<CloudAdapterKind, LlmAdapter> = {
  openai_chat: openAiChatAdapter,
  gemini: geminiAdapter,
  anthropic: anthropicAdapter,
};

function getAdapter(provider: DirectMobileLlmProvider): LlmAdapter {
  return ADAPTERS[getAdapterKind(provider)];
}

/* -------------------------------------------------------------------------- */
/* 実行                                                                        */
/* -------------------------------------------------------------------------- */

function isKimiK3Model(model: string): boolean {
  return model.trim().toLowerCase().startsWith("kimi-k3");
}

function restoredKimiAssistantPayload(
  settings: MobileLlmSettings,
  message: ConversationMessage,
): ContextMessage | null {
  if (settings.provider !== "kimi" || !isKimiK3Model(settings.model)) return null;
  if (message.metadata?.provider !== settings.provider) return null;
  if (message.metadata?.model !== settings.model) return null;
  const payload = message.metadata?.[KIMI_ASSISTANT_PAYLOAD_METADATA_KEY];
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return null;
  const record = payload as Record<string, unknown>;
  if (record.role !== "assistant" || typeof record.content !== "string") return null;
  return record as ContextMessage;
}

function buildContext(
  settings: MobileLlmSettings,
  messages: ConversationMessage[],
  nextUserText: string,
): ContextMessage[] {
  const recent: ContextMessage[] = messages
    .filter(
      (message) => message.role === "user" || message.role === "assistant",
    )
    .slice(-20)
    .map((message) => {
      if (message.role === "assistant") {
        const restored = restoredKimiAssistantPayload(settings, message);
        if (restored) return restored;
      }
      return {
        role: message.role === "assistant" ? "assistant" : "user",
        content: message.content,
      };
    });
  recent.push({ role: "user", content: nextUserText });
  return recent;
}

async function fetchWithTimeout(
  url: string,
  init: RequestInit,
): Promise<Response> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), CHAT_TIMEOUT);
  try {
    return await fetch(url, { ...init, signal: controller.signal });
  } finally {
    clearTimeout(timer);
  }
}

async function runAdapter(
  settings: MobileLlmSettings,
  context: ContextMessage[],
): Promise<MobileLlmReply> {
  if (!isDirectProvider(settings.provider)) {
    throw new Error("Mobile LLM is not configured.");
  }
  const adapter = getAdapter(settings.provider);
  const { url, init } = adapter.buildRequest(settings, context);
  const response = await fetchWithTimeout(url, init);
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    throw new Error(adapter.extractError(data, response.status));
  }
  const reply = adapter.extractReply(data);
  if (!reply) {
    throw new Error("LLM response did not include message content.");
  }
  if (settings.provider !== "kimi" || !isKimiK3Model(settings.model)) {
    return { content: reply.content };
  }
  return reply;
}

export async function generateMobileLlmReply(
  settings: MobileLlmSettings,
  messages: ConversationMessage[],
  nextUserText: string,
): Promise<MobileLlmReply> {
  if (!isMobileLlmConfigured(settings)) {
    throw new Error("Mobile LLM is not configured.");
  }
  return runAdapter(settings, buildContext(settings, messages, nextUserText));
}

export interface MobileLlmConnectionResult {
  ok: boolean;
  message?: string;
}

/** 短い1メッセージを送って疎通を確認する。 */
export async function testMobileLlmConnection(
  settings: MobileLlmSettings,
): Promise<MobileLlmConnectionResult> {
  if (!isMobileLlmConfigured(settings)) {
    return { ok: false, message: "APIキーとモデルIDを設定してください。" };
  }
  try {
    const reply = await runAdapter(settings, [
      { role: "user", content: "ping" },
    ]);
    return { ok: Boolean(reply.content), message: "接続に成功しました。" };
  } catch (error) {
    return {
      ok: false,
      message: error instanceof Error ? error.message : "接続に失敗しました。",
    };
  }
}
