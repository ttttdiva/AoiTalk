import AsyncStorage from "@react-native-async-storage/async-storage";
import { DEFAULT_API_URL, STORAGE_KEYS } from "../../constants/config";
import {
  getApiUrl,
  getCachedToken,
  getToken,
  getTokenAuthScope,
} from "../../lib/auth";
import { normalizeApiUrl } from "../../lib/api-url";
import {
  type DirectMobileLlmSelection,
  normalizeDirectReasoningEffort,
} from "../../lib/mobile-llm";
import { isDirectMobileLlmProvider } from "../../lib/cloud-model-catalog";
import type { LlmModeResponse } from "../../lib/chat-api";
import type {
  ChatResponseModelOption,
  ChatResponseModelSelection,
} from "../../types/api";

export const SERVER_DEFAULT_MODE = "server-default";
export const SERVER_DEFAULT_MODE_LABEL = "サーバー既定";

export type ChatResponseTarget =
  | { kind: "server"; responseModel?: ChatResponseModelSelection }
  | { kind: "direct"; selection: DirectMobileLlmSelection };

export type ChatLlmPreferences = {
  version: 1;
  mode: LlmModeResponse;
  responseModelOptions: ChatResponseModelOption[];
  responseTarget: ChatResponseTarget;
  /** POST /api/llm/modeが未完了。再起動・再接続後も最新値だけを再送する。 */
  modeSyncPending: boolean;
  updatedAt: number;
};

export type NormalizedResponseTarget = {
  target: ChatResponseTarget;
  message: string | null;
};

const writeQueues = new Map<string, Promise<void>>();

function stringValue(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const normalized = value.trim();
  return normalized || null;
}

function uniqueStrings(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return Array.from(
    new Set(
      value
        .map(stringValue)
        .filter((item): item is string => Boolean(item)),
    ),
  );
}

export function defaultLlmMode(): LlmModeResponse {
  return {
    mode: SERVER_DEFAULT_MODE,
    available_modes: [SERVER_DEFAULT_MODE],
    labels: { [SERVER_DEFAULT_MODE]: SERVER_DEFAULT_MODE_LABEL },
    kind: "server_default",
  };
}

export function normalizeLlmMode(value: unknown): LlmModeResponse {
  const record = value && typeof value === "object"
    ? (value as Record<string, unknown>)
    : null;
  const mode = stringValue(record?.mode);
  if (!mode) return defaultLlmMode();
  const options = uniqueStrings(record?.available_modes);
  if (!options.includes(mode)) options.unshift(mode);
  const rawLabels = record?.labels;
  const labels: Record<string, string> = {};
  if (rawLabels && typeof rawLabels === "object" && !Array.isArray(rawLabels)) {
    for (const [key, label] of Object.entries(rawLabels)) {
      const normalizedKey = stringValue(key);
      const normalizedLabel = stringValue(label);
      if (normalizedKey && normalizedLabel) labels[normalizedKey] = normalizedLabel;
    }
  }
  return {
    mode,
    available_modes: options,
    labels,
    kind: stringValue(record?.kind) ?? undefined,
    provider: stringValue(record?.provider) ?? undefined,
    model: stringValue(record?.model) ?? undefined,
    success: typeof record?.success === "boolean" ? record.success : undefined,
    message: stringValue(record?.message) ?? undefined,
  };
}

function normalizeResponseModel(value: unknown): ChatResponseModelSelection | undefined {
  const record = value && typeof value === "object"
    ? (value as Record<string, unknown>)
    : null;
  const provider = stringValue(record?.provider);
  const model = stringValue(record?.model);
  return provider && model ? { provider, model } : undefined;
}

export function normalizeResponseTarget(value: unknown): ChatResponseTarget {
  const record = value && typeof value === "object"
    ? (value as Record<string, unknown>)
    : null;
  if (record?.kind === "direct") {
    const selectionRecord = record.selection && typeof record.selection === "object"
      ? (record.selection as Record<string, unknown>)
      : null;
    const provider = stringValue(selectionRecord?.provider);
    const model = stringValue(selectionRecord?.model);
    const reasoningEffort = stringValue(selectionRecord?.reasoningEffort);
    if (provider && model && isDirectMobileLlmProvider(provider)) {
      const normalizedEffort = normalizeDirectReasoningEffort(
        provider,
        model,
        reasoningEffort,
      );
      return {
        kind: "direct",
        selection: {
          provider,
          model,
          ...(normalizedEffort ? { reasoningEffort: normalizedEffort } : {}),
        },
      };
    }
  }

  return {
    kind: "server",
    ...(normalizeResponseModel(record?.responseModel)
      ? { responseModel: normalizeResponseModel(record?.responseModel) }
      : {}),
  };
}

function normalizeResponseModelOptions(value: unknown): ChatResponseModelOption[] {
  if (!Array.isArray(value)) return [];
  const result: ChatResponseModelOption[] = [];
  const seen = new Set<string>();
  for (const rawOption of value) {
    const record = rawOption && typeof rawOption === "object"
      ? (rawOption as Record<string, unknown>)
      : null;
    const provider = stringValue(record?.provider);
    const model = stringValue(record?.model);
    if (!provider || !model) continue;
    const key = `${provider}:${model}`;
    if (seen.has(key)) continue;
    seen.add(key);
    const providerLabel = stringValue(record?.providerLabel) ?? provider;
    const modelLabel = stringValue(record?.modelLabel) ?? model;
    result.push({
      provider,
      model,
      providerLabel,
      modelLabel,
      label: stringValue(record?.label) ?? `${providerLabel} / ${modelLabel}`,
      isCurrent: record?.isCurrent === true,
    });
  }
  return result;
}

export function createDefaultChatLlmPreferences(now = Date.now()): ChatLlmPreferences {
  return {
    version: 1,
    mode: defaultLlmMode(),
    responseModelOptions: [],
    responseTarget: { kind: "server" },
    modeSyncPending: false,
    updatedAt: now,
  };
}

function normalizePreferences(value: unknown): ChatLlmPreferences | null {
  const record = value && typeof value === "object"
    ? (value as Record<string, unknown>)
    : null;
  if (!record || record.version !== 1) return null;
  return {
    version: 1,
    mode: normalizeLlmMode(record.mode),
    responseModelOptions: normalizeResponseModelOptions(record.responseModelOptions),
    responseTarget: normalizeResponseTarget(record.responseTarget),
    modeSyncPending: record.modeSyncPending === true,
    updatedAt:
      typeof record.updatedAt === "number" && Number.isFinite(record.updatedAt)
        ? record.updatedAt
        : 0,
  };
}

export function buildChatLlmPreferenceScope(
  serverUrl: string | null | undefined,
  accountScope: string | null | undefined,
): string {
  const server = normalizeApiUrl(serverUrl || DEFAULT_API_URL) || normalizeApiUrl(DEFAULT_API_URL);
  const account = stringValue(accountScope) ?? "anonymous";
  return `${server}::${account}`;
}

export async function resolveCurrentChatLlmPreferenceScope(
  accountScope?: string | null,
): Promise<string> {
  let resolvedAccountScope = stringValue(accountScope);
  if (!resolvedAccountScope) {
    let token = getCachedToken();
    if (token === undefined) token = await getToken();
    resolvedAccountScope = getTokenAuthScope(token);
  }
  return buildChatLlmPreferenceScope(await getApiUrl(), resolvedAccountScope);
}

function storageKey(scope: string): string {
  return `${STORAGE_KEYS.CHAT_LLM_UI_PREFERENCES_PREFIX}:${encodeURIComponent(scope)}`;
}

export async function readChatLlmPreferences(
  scope: string,
): Promise<ChatLlmPreferences | null> {
  const raw = await AsyncStorage.getItem(storageKey(scope));
  if (!raw) return null;
  try {
    return normalizePreferences(JSON.parse(raw));
  } catch {
    return null;
  }
}

/**
 * scope単位で書き込みを直列化する。古いAsyncStorage writeが新しい選択を後から
 * 上書きしないため、呼び出し側はawaitせずUI critical pathから外せる。
 */
export function writeChatLlmPreferences(
  scope: string,
  preferences: ChatLlmPreferences,
): Promise<void> {
  const key = storageKey(scope);
  const snapshot: ChatLlmPreferences = {
    ...preferences,
    mode: normalizeLlmMode(preferences.mode),
    responseModelOptions: normalizeResponseModelOptions(preferences.responseModelOptions),
    responseTarget: normalizeResponseTarget(preferences.responseTarget),
    updatedAt: Date.now(),
  };
  const previous = writeQueues.get(key) ?? Promise.resolve();
  const write = previous
    .catch(() => undefined)
    .then(() => AsyncStorage.setItem(key, JSON.stringify(snapshot)));
  const tracked = write.then(
    () => {
      if (writeQueues.get(key) === tracked) writeQueues.delete(key);
    },
    () => {
      if (writeQueues.get(key) === tracked) writeQueues.delete(key);
    },
  );
  writeQueues.set(key, tracked);
  return write;
}

/** サーバー候補から消えた/非表示になった選択を明示的にserver defaultへ正規化する。 */
export function normalizeTargetAgainstServerOptions(
  target: ChatResponseTarget,
  options: readonly ChatResponseModelOption[],
): NormalizedResponseTarget {
  if (target.kind !== "server" || !target.responseModel) {
    return { target, message: null };
  }
  const available = options.some(
    (option) =>
      option.provider === target.responseModel?.provider &&
      option.model === target.responseModel?.model,
  );
  if (available) return { target, message: null };

  const rejectedLabel = `${target.responseModel.provider} / ${target.responseModel.model}`;
  const current = options.find((option) => option.isCurrent);
  return {
    target: { kind: "server" },
    message: current
      ? `${rejectedLabel} は現在利用できないため、サーバー既定（${current.providerLabel} / ${current.modelLabel}）へ戻しました。`
      : `${rejectedLabel} は現在利用できないため、サーバー既定へ戻しました。`,
  };
}
