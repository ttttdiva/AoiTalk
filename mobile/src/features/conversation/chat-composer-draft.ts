import AsyncStorage from "@react-native-async-storage/async-storage";
import { STORAGE_KEYS } from "../../constants/config";

export const CHAT_COMPOSER_DRAFT_MAX_LENGTH = 4000;

export type ChatComposerDraftScope = {
  authUser: string;
  serverProfile: string;
  conversationKey: string;
};

export type ChatComposerDraftRecord = {
  version: 1;
  text: string;
  activeCommand: string | null;
  revision: string;
  updatedAt: number;
};

export type ChatComposerDraftSnapshot = {
  scope: ChatComposerDraftScope | null;
  scopeKey: string | null;
  text: string;
  activeCommand: string | null;
  revision: string;
};

const writeQueues = new Map<string, Promise<void>>();

function stringValue(value: unknown): string | null {
  if (typeof value !== "string") return null;
  return value;
}

function nonEmptyString(value: unknown): string | null {
  const normalized = stringValue(value)?.trim();
  return normalized || null;
}

export function chatComposerDraftScopeKey(scope: ChatComposerDraftScope): string {
  return JSON.stringify([
    scope.serverProfile,
    scope.authUser,
    scope.conversationKey,
  ]);
}

function storageKey(scope: ChatComposerDraftScope): string {
  return `${STORAGE_KEYS.CHAT_COMPOSER_DRAFT_PREFIX}:${encodeURIComponent(
    chatComposerDraftScopeKey(scope),
  )}`;
}

function normalizeRecord(value: unknown): ChatComposerDraftRecord | null {
  if (!value || typeof value !== "object") return null;
  const record = value as Record<string, unknown>;
  const revision = nonEmptyString(record.revision);
  if (record.version !== 1 || !revision) return null;
  const text = stringValue(record.text);
  if (text === null) return null;
  const updatedAt = typeof record.updatedAt === "number" && Number.isFinite(record.updatedAt)
    ? record.updatedAt
    : 0;
  const activeCommand = nonEmptyString(record.activeCommand);
  return {
    version: 1,
    text: text.slice(0, CHAT_COMPOSER_DRAFT_MAX_LENGTH),
    activeCommand,
    revision,
    updatedAt,
  };
}

function enqueue(key: string, operation: () => Promise<void>): Promise<void> {
  const previous = writeQueues.get(key) ?? Promise.resolve();
  const next = previous
    .catch(() => undefined)
    .then(operation)
    .finally(() => {
      if (writeQueues.get(key) === next) writeQueues.delete(key);
    });
  writeQueues.set(key, next);
  return next;
}

export async function readChatComposerDraft(
  scope: ChatComposerDraftScope,
): Promise<ChatComposerDraftRecord | null> {
  try {
    const key = storageKey(scope);
    await writeQueues.get(key)?.catch(() => undefined);
    const raw = await AsyncStorage.getItem(key);
    if (!raw) return null;
    return normalizeRecord(JSON.parse(raw));
  } catch {
    // A corrupt/unavailable private draft must not block the composer, and is
    // deliberately not removed before a successful scoped readback.
    return null;
  }
}

export function writeChatComposerDraft(
  scope: ChatComposerDraftScope,
  record: ChatComposerDraftRecord,
): Promise<void> {
  const normalized = normalizeRecord(record);
  if (!normalized) return Promise.resolve();
  const key = storageKey(scope);
  return enqueue(key, async () => {
    if (!normalized.text && !normalized.activeCommand) {
      await AsyncStorage.removeItem(key);
      return;
    }
    await AsyncStorage.setItem(key, JSON.stringify(normalized));
  });
}

export function clearChatComposerDraftIfRevisionMatches(
  snapshot: ChatComposerDraftSnapshot,
): Promise<boolean> {
  if (!snapshot.scope || !snapshot.scopeKey || !snapshot.revision) {
    return Promise.resolve(false);
  }
  const scope = snapshot.scope;
  const key = storageKey(scope);
  return new Promise<boolean>((resolve) => {
    void enqueue(key, async () => {
      try {
        const raw = await AsyncStorage.getItem(key);
        const current = raw ? normalizeRecord(JSON.parse(raw)) : null;
        if (current?.revision !== snapshot.revision) {
          resolve(false);
          return;
        }
        await AsyncStorage.removeItem(key);
        resolve(true);
      } catch {
        resolve(false);
      }
    });
  });
}
