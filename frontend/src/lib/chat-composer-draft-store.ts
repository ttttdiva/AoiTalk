import type { MentionItem } from "@/components/chat/mention-menu";
import type { ActiveChatCommand } from "@/lib/chat-commands";

export const NEW_CHAT_COMPOSER_DRAFT_KEY = "__new_chat__";

/** localStorage のキーを他のアプリケーション領域と衝突させないための接頭辞。 */
export const CHAT_COMPOSER_DRAFT_STORAGE_PREFIX = "aoitalk:chat-draft:v1";
export const CHAT_COMPOSER_DRAFT_SCHEMA_VERSION = 1;
export const CHAT_COMPOSER_DRAFT_DEBOUNCE_MS = 400;

// 長期間開かれていない会話の下書きだけを簡易的に回収する。
export const CHAT_COMPOSER_DRAFT_MAX_AGE_MS = 1000 * 60 * 60 * 24 * 30;
const ANONYMOUS_SCOPE = "__anonymous__";

export type ChatComposerDraft = {
  content: string;
  mentions: MentionItem[];
  activeCommand: ActiveChatCommand | null;
  toolFreeMode: boolean;
};

export const EMPTY_CHAT_COMPOSER_DRAFT: ChatComposerDraft = {
  content: "",
  mentions: [],
  activeCommand: null,
  toolFreeMode: false,
};

type PersistedChatComposerDraft = {
  schema: typeof CHAT_COMPOSER_DRAFT_SCHEMA_VERSION;
  updatedAt: number;
  draft: ChatComposerDraft;
};

type DraftScope = {
  key: string;
  userId: string | null;
  scopeKey: string;
};

const drafts = new Map<string, ChatComposerDraft>();
const listeners = new Map<string, Set<() => void>>();
const loadedScopes = new Set<string>();
const pendingWrites = new Map<
  string,
  { timer: ReturnType<typeof setTimeout>; scope: DraftScope }
>();
let pagehideListenerAttached = false;

function getBrowserStorage(): Storage | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage ?? null;
  } catch {
    return null;
  }
}

function encodeStoragePart(value: string): string {
  return encodeURIComponent(value);
}

function normalizeUserId(userId?: string | null): string | null {
  const normalized = typeof userId === "string" ? userId.trim() : "";
  return normalized ? normalized : null;
}

/**
 * userId を含む localStorage キーを返す。認証ユーザーが無い場合は null
 * を返し、未認証・SSR 時に共有キーへ保存しないようにする。
 */
export function getChatComposerDraftStorageKey(
  userId: string | null | undefined,
  key: string,
): string | null {
  const normalizedUserId = normalizeUserId(userId);
  if (!normalizedUserId || !key) return null;
  // user/session ID に区切り文字が含まれてもキー衝突しないようエンコードする。
  return `${CHAT_COMPOSER_DRAFT_STORAGE_PREFIX}:${encodeStoragePart(normalizedUserId)}:${encodeStoragePart(key)}`;
}

function getDraftScope(key: string, userId?: string | null): DraftScope {
  const normalizedUserId = normalizeUserId(userId);
  // 制御文字を区切りに使い、user/session ID に記号が含まれても衝突させない。
  const scopeKey = `${normalizedUserId ?? ANONYMOUS_SCOPE}\u0000${key}`;
  return { key, userId: normalizedUserId, scopeKey };
}

function notify(scopeKey: string): void {
  listeners.get(scopeKey)?.forEach((listener) => listener());
}

function isDraftEmpty(draft: ChatComposerDraft): boolean {
  return (
    draft.content.length === 0 &&
    draft.mentions.length === 0 &&
    draft.activeCommand === null &&
    draft.toolFreeMode === false
  );
}

function isMentionItem(value: unknown): value is MentionItem {
  if (!value || typeof value !== "object") return false;
  const item = value as Record<string, unknown>;
  return (
    (item.type === "file" ||
      item.type === "task" ||
      item.type === "project" ||
      item.type === "app" ||
      item.type === "docs" ||
      item.type === "chat_session") &&
    typeof item.id === "string" &&
    typeof item.name === "string" &&
    (item.detail === undefined || typeof item.detail === "string")
  );
}

function isActiveChatCommand(value: unknown): value is ActiveChatCommand {
  if (!value || typeof value !== "object") return false;
  const command = value as Record<string, unknown>;
  return (
    typeof command.command === "string" &&
    typeof command.label === "string" &&
    typeof command.description === "string" &&
    command.kind === "capability" &&
    (command.capability === "web_search" ||
      command.capability === "image_generation" ||
      command.capability === "work_intake" ||
      command.capability === "project_db_update" ||
      command.capability === "project_progress_review" ||
      command.capability === "task_update" ||
      command.capability === "wbs_sync")
  );
}

function parsePersistedDraft(value: unknown): {
  draft: ChatComposerDraft;
  updatedAt: number;
} | null {
  if (!value || typeof value !== "object") return null;
  const record = value as Record<string, unknown>;
  const schema = record.schema ?? record.version;
  if (schema !== CHAT_COMPOSER_DRAFT_SCHEMA_VERSION) return null;

  const rawDraft =
    record.draft && typeof record.draft === "object"
      ? (record.draft as Record<string, unknown>)
      : record;
  if (typeof rawDraft.content !== "string") return null;
  if (
    rawDraft.mentions !== undefined &&
    (!Array.isArray(rawDraft.mentions) ||
      !rawDraft.mentions.every(isMentionItem))
  ) {
    return null;
  }
  if (
    rawDraft.activeCommand !== undefined &&
    rawDraft.activeCommand !== null &&
    !isActiveChatCommand(rawDraft.activeCommand)
  ) {
    return null;
  }
  if (
    rawDraft.toolFreeMode !== undefined &&
    typeof rawDraft.toolFreeMode !== "boolean"
  ) {
    return null;
  }

  const updatedAt =
    typeof record.updatedAt === "number" && Number.isFinite(record.updatedAt)
      ? record.updatedAt
      : Date.now();
  return {
    draft: {
      content: rawDraft.content,
      mentions: rawDraft.mentions ? [...rawDraft.mentions] : [],
      activeCommand:
        rawDraft.activeCommand === undefined
          ? null
          : (rawDraft.activeCommand as ActiveChatCommand | null),
      toolFreeMode: rawDraft.toolFreeMode === true,
    },
    updatedAt,
  };
}

function removeStoredDraft(storageKey: string | null): boolean {
  const storage = getBrowserStorage();
  if (!storageKey || !storage) return false;
  try {
    storage.removeItem(storageKey);
    return true;
  } catch {
    // localStorage が無効化されていても入力操作は継続できるようにする。
    return false;
  }
}

function cancelPendingWrite(scopeKey: string): void {
  const pending = pendingWrites.get(scopeKey);
  if (pending !== undefined) {
    clearTimeout(pending.timer);
    pendingWrites.delete(scopeKey);
  }
}

function writeDraftNow(scope: DraftScope): boolean {
  cancelPendingWrite(scope.scopeKey);
  const storage = getBrowserStorage();
  if (!scope.userId || !storage) return false;
  const storageKey = getChatComposerDraftStorageKey(scope.userId, scope.key);
  if (!storageKey) return false;
  const draft = drafts.get(scope.scopeKey) ?? EMPTY_CHAT_COMPOSER_DRAFT;
  if (isDraftEmpty(draft)) {
    return removeStoredDraft(storageKey);
  }
  const payload: PersistedChatComposerDraft = {
    schema: CHAT_COMPOSER_DRAFT_SCHEMA_VERSION,
    updatedAt: Date.now(),
    draft: {
      content: draft.content,
      mentions: draft.mentions,
      activeCommand: draft.activeCommand,
      toolFreeMode: draft.toolFreeMode,
    },
  };
  try {
    storage.setItem(storageKey, JSON.stringify(payload));
    return true;
  } catch {
    // 容量超過・SecurityError 等は入力機能へ波及させない。
    return false;
  }
}

function scheduleDraftWrite(scope: DraftScope): void {
  if (!scope.userId || !getBrowserStorage()) return;
  cancelPendingWrite(scope.scopeKey);
  const timer = setTimeout(() => {
    pendingWrites.delete(scope.scopeKey);
    writeDraftNow(scope);
  }, CHAT_COMPOSER_DRAFT_DEBOUNCE_MS);
  pendingWrites.set(scope.scopeKey, { timer, scope });

  if (!pagehideListenerAttached) {
    window.addEventListener("pagehide", flushChatComposerDraftStore);
    pagehideListenerAttached = true;
  }
}

function loadDraftFromStorage(scope: DraftScope): void {
  const storage = getBrowserStorage();
  if (!scope.userId || !storage) return;
  if (loadedScopes.has(scope.scopeKey)) return;
  loadedScopes.add(scope.scopeKey);
  const storageKey = getChatComposerDraftStorageKey(scope.userId, scope.key);
  if (!storageKey) return;

  let raw: string | null = null;
  try {
    raw = storage.getItem(storageKey);
  } catch {
    return;
  }
  if (!raw) return;

  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    removeStoredDraft(storageKey);
    return;
  }
  const persisted = parsePersistedDraft(parsed);
  if (!persisted || Date.now() - persisted.updatedAt > CHAT_COMPOSER_DRAFT_MAX_AGE_MS) {
    removeStoredDraft(storageKey);
    return;
  }
  if (isDraftEmpty(persisted.draft)) {
    removeStoredDraft(storageKey);
    return;
  }
  drafts.set(scope.scopeKey, persisted.draft);
  notify(scope.scopeKey);
}

/**
 * ChatComposer のマウント後に呼び出して localStorage の下書きをメモリへ
 * 復元する。レンダー中に localStorage を読むことを避けるため明示的に行う。
 */
export function hydrateChatComposerDraft(
  key: string,
  userId?: string | null,
): void {
  loadDraftFromStorage(getDraftScope(key, userId));
}

/** 既存コードで呼びやすい別名。 */
export const loadChatComposerDraft = hydrateChatComposerDraft;

export function getChatComposerDraft(
  key: string,
  userId?: string | null,
): ChatComposerDraft {
  const scope = getDraftScope(key, userId);
  return drafts.get(scope.scopeKey) ?? EMPTY_CHAT_COMPOSER_DRAFT;
}

export function subscribeChatComposerDraft(
  key: string,
  listener: () => void,
  userId?: string | null,
): () => void {
  const scope = getDraftScope(key, userId);
  const keyListeners = listeners.get(scope.scopeKey) ?? new Set<() => void>();
  keyListeners.add(listener);
  listeners.set(scope.scopeKey, keyListeners);
  return () => {
    keyListeners.delete(listener);
    if (keyListeners.size === 0) listeners.delete(scope.scopeKey);
  };
}

export function updateChatComposerDraft(
  key: string,
  updater: (current: ChatComposerDraft) => ChatComposerDraft,
  userId?: string | null,
): void {
  const scope = getDraftScope(key, userId);
  // 入力が復元より先に発生しても保存済み下書きを上書きしない。
  loadDraftFromStorage(scope);
  const current = getChatComposerDraft(key, userId);
  const next = updater(current);
  if (next === current) return;
  drafts.set(scope.scopeKey, next);
  notify(scope.scopeKey);
  scheduleDraftWrite(scope);
}

/** 下書きを即時に消し、保留中の localStorage 書き込みも取り消す。 */
export function clearChatComposerDraft(
  key: string,
  userId?: string | null,
): void {
  const scope = getDraftScope(key, userId);
  drafts.delete(scope.scopeKey);
  cancelPendingWrite(scope.scopeKey);
  removeStoredDraft(getChatComposerDraftStorageKey(scope.userId, scope.key));
  notify(scope.scopeKey);
}

/** pagehide 等で debounce 待ちの下書きをまとめて保存する。 */
export function flushChatComposerDraftStore(): void {
  const scopes = Array.from(pendingWrites.values(), ({ scope }) => scope);
  for (const scope of scopes) {
    writeDraftNow(scope);
  }
}

function collectStorageKeysForUser(userId: string): string[] {
  const storage = getBrowserStorage();
  if (!storage) return [];
  const prefix = `${CHAT_COMPOSER_DRAFT_STORAGE_PREFIX}:${encodeStoragePart(userId)}:`;
  const keys: string[] = [];
  try {
    for (let index = 0; index < storage.length; index += 1) {
      const key = storage.key(index);
      if (key?.startsWith(prefix)) keys.push(key);
    }
  } catch {
    return [];
  }
  return keys;
}

/** 現在ユーザーの古い・壊れた下書きを軽量に整理する。 */
export function gcChatComposerDrafts(userId?: string | null): void {
  const normalizedUserId = normalizeUserId(userId);
  const storage = getBrowserStorage();
  if (!normalizedUserId || !storage) return;
  for (const storageKey of collectStorageKeysForUser(normalizedUserId)) {
    let raw: string | null = null;
    try {
      raw = storage.getItem(storageKey);
    } catch {
      continue;
    }
    if (!raw) continue;
    try {
      const persisted = parsePersistedDraft(JSON.parse(raw));
      if (
        !persisted ||
        Date.now() - persisted.updatedAt > CHAT_COMPOSER_DRAFT_MAX_AGE_MS
      ) {
        removeStoredDraft(storageKey);
      }
    } catch {
      removeStoredDraft(storageKey);
    }
  }
}

/**
 * null session で入力していた draft を、作成された session に明示的に
 * 引き継ぐ。通常の A→B 切替ではこの関数を呼ばないため、別会話へ漏れない。
 */
export function promoteNewChatComposerDraft(
  sessionId: string,
  userId?: string | null,
): void {
  if (!sessionId) return;
  const source = getDraftScope(NEW_CHAT_COMPOSER_DRAFT_KEY, userId);
  const target = getDraftScope(sessionId, userId);
  loadDraftFromStorage(source);
  const draft = drafts.get(source.scopeKey);
  if (!draft) return;

  // target の保存に成功してから source を削除する。容量超過や
  // SecurityError で target が保存できない場合は、source を残して
  // 下書きを失わない。
  if (target.userId) {
    const previousTarget = drafts.get(target.scopeKey);
    drafts.set(target.scopeKey, draft);
    if (!writeDraftNow(target)) {
      if (previousTarget) drafts.set(target.scopeKey, previousTarget);
      else drafts.delete(target.scopeKey);
      return;
    }
  }

  drafts.set(target.scopeKey, draft);
  drafts.delete(source.scopeKey);
  cancelPendingWrite(source.scopeKey);
  cancelPendingWrite(target.scopeKey);
  notify(target.scopeKey);
  notify(source.scopeKey);

  // 新規チャットの下書きは、promote 直後に target だけ残す。
  removeStoredDraft(
    getChatComposerDraftStorageKey(source.userId, NEW_CHAT_COMPOSER_DRAFT_KEY),
  );
}

export function resetChatComposerDraftStore(): void {
  pendingWrites.forEach(({ timer }) => clearTimeout(timer));
  pendingWrites.clear();
  drafts.clear();
  loadedScopes.clear();
  listeners.forEach((keyListeners) => keyListeners.forEach((listener) => listener()));
}
