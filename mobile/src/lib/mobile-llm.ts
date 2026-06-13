import * as SecureStore from "expo-secure-store";
import { CHAT_TIMEOUT, STORAGE_KEYS } from "../constants/config";
import type { ConversationMessage } from "../types/api";

export type MobileLlmProvider =
  | "server"
  | "openai"
  | "gemini"
  | "openai_compatible";
export type DirectMobileLlmProvider = Exclude<MobileLlmProvider, "server">;
export type MobileLlmFallbackProvider = "off" | DirectMobileLlmProvider;

export interface MobileLlmSettings {
  provider: MobileLlmProvider;
  apiKey: string;
  model: string;
  baseUrl: string;
}

export type MobileLlmProfile = Omit<MobileLlmSettings, "provider">;

const GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta";
export const DEFAULT_MOBILE_LLM_SETTINGS: MobileLlmSettings = {
  provider: "server",
  apiKey: "",
  model: "gpt-4o-mini",
  baseUrl: "https://api.openai.com/v1",
};

const DEFAULT_DIRECT_LLM_PROFILES: Record<
  DirectMobileLlmProvider,
  MobileLlmProfile
> = {
  openai: {
    apiKey: "",
    model: "gpt-4o-mini",
    baseUrl: "https://api.openai.com/v1",
  },
  gemini: {
    apiKey: "",
    model: "gemini-1.5-flash",
    baseUrl: GEMINI_BASE_URL,
  },
  openai_compatible: {
    apiKey: "",
    model: "gpt-4o-mini",
    baseUrl: "https://api.openai.com/v1",
  },
};

function normalizeProvider(value: string | null): MobileLlmProvider {
  if (value === "server" || value === "off") {
    return "server";
  }
  if (value === "openai" || value === "gemini" || value === "openai_compatible") {
    return value;
  }
  return "server";
}

function normalizeFallbackProvider(
  value: string | null,
): MobileLlmFallbackProvider {
  if (value === "openai" || value === "gemini" || value === "openai_compatible") {
    return value;
  }
  return "off";
}

export function isDirectProvider(
  provider: MobileLlmProvider,
): provider is DirectMobileLlmProvider {
  return provider !== "server";
}

function getProfileStorageKeys(provider: DirectMobileLlmProvider) {
  switch (provider) {
    case "openai":
      return {
        apiKey: STORAGE_KEYS.CHAT_LLM_OPENAI_API_KEY,
        model: STORAGE_KEYS.CHAT_LLM_OPENAI_MODEL,
        baseUrl: STORAGE_KEYS.CHAT_LLM_OPENAI_BASE_URL,
      };
    case "gemini":
      return {
        apiKey: STORAGE_KEYS.CHAT_LLM_GEMINI_API_KEY,
        model: STORAGE_KEYS.CHAT_LLM_GEMINI_MODEL,
        baseUrl: STORAGE_KEYS.CHAT_LLM_GEMINI_BASE_URL,
      };
    case "openai_compatible":
      return {
        apiKey: STORAGE_KEYS.CHAT_LLM_OPENAI_COMPATIBLE_API_KEY,
        model: STORAGE_KEYS.CHAT_LLM_OPENAI_COMPATIBLE_MODEL,
        baseUrl: STORAGE_KEYS.CHAT_LLM_OPENAI_COMPATIBLE_BASE_URL,
      };
  }
}

async function getStoredProfile(
  provider: DirectMobileLlmProvider,
): Promise<MobileLlmProfile> {
  const keys = getProfileStorageKeys(provider);
  const defaults = DEFAULT_DIRECT_LLM_PROFILES[provider];
  const [apiKey, model, baseUrl] = await Promise.all([
    SecureStore.getItemAsync(keys.apiKey),
    SecureStore.getItemAsync(keys.model),
    SecureStore.getItemAsync(keys.baseUrl),
  ]);
  return {
    apiKey: apiKey ?? "",
    model: model || defaults.model,
    baseUrl: baseUrl || defaults.baseUrl,
  };
}

async function migrateLegacyProfileIfNeeded(
  provider: DirectMobileLlmProvider,
): Promise<void> {
  const keys = getProfileStorageKeys(provider);
  const [
    storedApiKey,
    storedModel,
    storedBaseUrl,
    legacyApiKey,
    legacyModel,
    legacyBaseUrl,
  ] = await Promise.all([
    SecureStore.getItemAsync(keys.apiKey),
    SecureStore.getItemAsync(keys.model),
    SecureStore.getItemAsync(keys.baseUrl),
    SecureStore.getItemAsync(STORAGE_KEYS.CHAT_LLM_API_KEY),
    SecureStore.getItemAsync(STORAGE_KEYS.CHAT_LLM_MODEL),
    SecureStore.getItemAsync(STORAGE_KEYS.CHAT_LLM_BASE_URL),
  ]);
  const writes: Array<Promise<void>> = [];
  if (storedApiKey === null && legacyApiKey) {
    writes.push(SecureStore.setItemAsync(keys.apiKey, legacyApiKey));
  }
  if (storedModel === null && legacyModel) {
    writes.push(SecureStore.setItemAsync(keys.model, legacyModel));
  }
  if (storedBaseUrl === null && legacyBaseUrl) {
    writes.push(SecureStore.setItemAsync(keys.baseUrl, legacyBaseUrl));
  }
  await Promise.all(writes);
}

export async function getMobileLlmProfile(
  provider: MobileLlmProvider,
): Promise<MobileLlmProfile> {
  if (!isDirectProvider(provider)) {
    return {
      apiKey: "",
      model: DEFAULT_MOBILE_LLM_SETTINGS.model,
      baseUrl: DEFAULT_MOBILE_LLM_SETTINGS.baseUrl,
    };
  }
  return getStoredProfile(provider);
}

export async function saveMobileLlmProfile(
  provider: DirectMobileLlmProvider,
  profile: MobileLlmProfile,
): Promise<void> {
  const keys = getProfileStorageKeys(provider);
  await Promise.all([
    SecureStore.setItemAsync(keys.apiKey, profile.apiKey.trim()),
    SecureStore.setItemAsync(keys.model, profile.model.trim()),
    SecureStore.setItemAsync(keys.baseUrl, profile.baseUrl.trim()),
  ]);
}

export async function getMobileLlmSettings(): Promise<MobileLlmSettings> {
  const provider = await SecureStore.getItemAsync(STORAGE_KEYS.CHAT_LLM_PROVIDER);
  const normalizedProvider = normalizeProvider(provider);
  if (isDirectProvider(normalizedProvider)) {
    await migrateLegacyProfileIfNeeded(normalizedProvider);
  }
  const profile = await getMobileLlmProfile(normalizedProvider);
  return {
    provider: normalizedProvider,
    ...profile,
  };
}

export async function getMobileLlmFallbackProvider(): Promise<MobileLlmFallbackProvider> {
  const stored = await SecureStore.getItemAsync(
    STORAGE_KEYS.CHAT_LLM_FALLBACK_PROVIDER,
  );
  const normalized = normalizeFallbackProvider(stored);
  if (stored !== null) {
    return normalized;
  }
  return "off";
}

export async function saveMobileLlmFallbackProvider(
  provider: MobileLlmFallbackProvider,
): Promise<void> {
  await SecureStore.setItemAsync(
    STORAGE_KEYS.CHAT_LLM_FALLBACK_PROVIDER,
    provider,
  );
}

export async function getConfiguredDirectMobileLlmSettings(
  preferredProvider?: MobileLlmProvider,
): Promise<MobileLlmSettings | null> {
  const provider =
    preferredProvider && isDirectProvider(preferredProvider)
      ? preferredProvider
      : await getMobileLlmFallbackProvider();
  if (!provider || provider === "off") {
    return null;
  }

  await migrateLegacyProfileIfNeeded(provider);
  const profile = await getStoredProfile(provider);
  const settings = { provider, ...profile };
  if (isMobileLlmConfigured(settings)) {
    return settings;
  }
  return null;
}

export async function saveMobileLlmSettings(
  settings: MobileLlmSettings,
): Promise<void> {
  const writes = [
    SecureStore.setItemAsync(STORAGE_KEYS.CHAT_LLM_PROVIDER, settings.provider),
  ];
  if (isDirectProvider(settings.provider)) {
    writes.push(saveMobileLlmProfile(settings.provider, settings));
  }
  await Promise.all(writes);
}

export function isMobileLlmConfigured(settings: MobileLlmSettings): boolean {
  return settings.provider !== "server" && Boolean(settings.apiKey.trim()) && Boolean(settings.model.trim());
}

function buildContext(messages: ConversationMessage[], nextUserText: string) {
  const recent = messages
    .filter((message) => message.role === "user" || message.role === "assistant")
    .slice(-20)
    .map((message) => ({
      role: message.role === "assistant" ? "assistant" : "user",
      content: message.content,
    }));
  recent.push({ role: "user", content: nextUserText });
  return recent;
}

async function fetchWithTimeout(url: string, init: RequestInit): Promise<Response> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), CHAT_TIMEOUT);
  try {
    return await fetch(url, { ...init, signal: controller.signal });
  } finally {
    clearTimeout(timer);
  }
}

async function generateOpenAiCompatible(
  settings: MobileLlmSettings,
  messages: ConversationMessage[],
  nextUserText: string,
): Promise<string> {
  const baseUrl = settings.baseUrl.replace(/\/+$/, "");
  const response = await fetchWithTimeout(`${baseUrl}/chat/completions`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${settings.apiKey.trim()}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      model: settings.model.trim(),
      messages: buildContext(messages, nextUserText),
      temperature: 0.7,
    }),
  });
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    const message =
      data?.error?.message || data?.message || `LLM request failed: ${response.status}`;
    throw new Error(String(message));
  }
  const content = data?.choices?.[0]?.message?.content;
  if (!content) throw new Error("LLM response did not include message content.");
  return String(content);
}

async function generateGemini(
  settings: MobileLlmSettings,
  messages: ConversationMessage[],
  nextUserText: string,
): Promise<string> {
  const baseUrl = (settings.baseUrl || GEMINI_BASE_URL).replace(/\/+$/, "");
  const context = buildContext(messages, nextUserText).map((message) => ({
    role: message.role === "assistant" ? "model" : "user",
    parts: [{ text: message.content }],
  }));
  const response = await fetchWithTimeout(
    `${baseUrl}/models/${encodeURIComponent(settings.model.trim())}:generateContent?key=${encodeURIComponent(settings.apiKey.trim())}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ contents: context }),
    },
  );
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    const message =
      data?.error?.message || data?.message || `LLM request failed: ${response.status}`;
    throw new Error(String(message));
  }
  const content = data?.candidates?.[0]?.content?.parts
    ?.map((part: { text?: string }) => part.text ?? "")
    .join("");
  if (!content) throw new Error("LLM response did not include message content.");
  return String(content);
}

export async function generateMobileLlmReply(
  settings: MobileLlmSettings,
  messages: ConversationMessage[],
  nextUserText: string,
): Promise<string> {
  if (!isMobileLlmConfigured(settings)) {
    throw new Error("Mobile LLM is not configured.");
  }
  if (settings.provider === "gemini") {
    return generateGemini(settings, messages, nextUserText);
  }
  return generateOpenAiCompatible(settings, messages, nextUserText);
}
