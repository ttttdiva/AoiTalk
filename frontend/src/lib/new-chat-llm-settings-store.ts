import {
  saveSessionLlmSettings,
  FREE_TEAM_ROUTING_PROFILE_ID,
  type SessionLlmSettings,
  type SessionLlmSettingsResponse,
} from "@/lib/chat-llm-settings";
import { assertPendingHandoffApplied, buildHandoffSettingsPatch } from "@/lib/chat-session-route-handoff";
import { hasExplicitSessionRoute } from "@/lib/chat-session-route";
import {
  CHAT_COMPOSER_DRAFT_MAX_AGE_MS,
  CHAT_COMPOSER_DRAFT_SCHEMA_VERSION,
  getChatComposerDraft,
  getChatComposerDraftStorageKey,
  NEW_CHAT_COMPOSER_DRAFT_KEY,
  subscribeChatComposerDraft,
  type ChatComposerDraft,
} from "@/lib/chat-composer-draft-store";

export const NEW_CHAT_LLM_SETTINGS_KEY = "__new_chat__";
export const NEW_CHAT_LLM_SETTINGS_STORAGE_PREFIX =
  "aoitalk:new-chat-llm-settings:v1";
export const NEW_CHAT_LLM_SETTINGS_SCHEMA_VERSION = 1;
export const NEW_CHAT_LLM_SETTINGS_MAX_AGE_MS = 1000 * 60 * 60 * 24 * 30;
const ANONYMOUS_SCOPE = "__anonymous__";

const defaultSessionSettings = (): SessionLlmSettings => ({
  agent_team_selection: {
    mode: "auto",
    team_id: "",
    loaded_team_ids: [],
  },
  main_route: {},
  special_routing: {},
  execution_profile_id: "",
});

type PersistedNewChatLlmSettings = {
  schema: typeof NEW_CHAT_LLM_SETTINGS_SCHEMA_VERSION;
  updatedAt: number;
  settings: SessionLlmSettings;
};

type SettingsScope = {
  key: string;
  userId: string | null;
  scopeKey: string;
};

const pendingByScope = new Map<string, SessionLlmSettings>();
const emptySnapshotsByScope = new Map<string, SessionLlmSettings>();
const listeners = new Map<string, Set<() => void>>();
const loadedScopes = new Set<string>();
const draftLifecycleBindings = new Map<string, () => void>();

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

function getSettingsScope(key: string, userId?: string | null): SettingsScope {
  const normalizedUserId = normalizeUserId(userId);
  const scopeKey = `${normalizedUserId ?? ANONYMOUS_SCOPE}\u0000${key}`;
  return { key, userId: normalizedUserId, scopeKey };
}

function getEmptySnapshot(scopeKey: string): SessionLlmSettings {
  let snapshot = emptySnapshotsByScope.get(scopeKey);
  if (!snapshot) {
    snapshot = defaultSessionSettings();
    emptySnapshotsByScope.set(scopeKey, snapshot);
  }
  return snapshot;
}

function getStorageKey(userId: string | null, key: string): string | null {
  if (!userId || !key) return null;
  return `${NEW_CHAT_LLM_SETTINGS_STORAGE_PREFIX}:${encodeStoragePart(userId)}:${encodeStoragePart(key)}`;
}

function notify(scopeKey: string): void {
  listeners.get(scopeKey)?.forEach((listener) => listener());
}

function isAgentTeamSelection(
  value: unknown,
): value is SessionLlmSettings["agent_team_selection"] {
  if (!value || typeof value !== "object") return false;
  const selection = value as Record<string, unknown>;
  return (
    (selection.mode === "auto" || selection.mode === "fixed") &&
    typeof selection.team_id === "string" &&
    Array.isArray(selection.loaded_team_ids) &&
    selection.loaded_team_ids.every((item) => typeof item === "string")
  );
}

function isSessionMainRoute(
  value: unknown,
): value is NonNullable<SessionLlmSettings["main_route"]> {
  if (!value || typeof value !== "object") return false;
  const route = value as Record<string, unknown>;
  return (
    (route.provider === undefined || typeof route.provider === "string") &&
    (route.model === undefined || typeof route.model === "string") &&
    (route.effort === undefined || typeof route.effort === "string")
  );
}

function isSpecialRouting(
  value: unknown,
): value is NonNullable<SessionLlmSettings["special_routing"]> {
  if (!value || typeof value !== "object") return false;
  const routing = value as Record<string, unknown>;
  return (
    routing.routing_profile_id === undefined ||
    typeof routing.routing_profile_id === "string"
  );
}

function isComposerDraftActive(draft: ChatComposerDraft): boolean {
  return (
    draft.content.length > 0 ||
    draft.mentions.length > 0 ||
    draft.activeCommand !== null ||
    draft.toolFreeMode
  );
}

function readPersistedNewChatDraftUpdatedAt(userId: string): number | null {
  const storage = getBrowserStorage();
  const storageKey = getChatComposerDraftStorageKey(userId, NEW_CHAT_COMPOSER_DRAFT_KEY);
  if (!storage || !storageKey) return null;

  let raw: string | null = null;
  try {
    raw = storage.getItem(storageKey);
  } catch {
    return null;
  }
  if (!raw) return null;

  try {
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    const schema = parsed.schema ?? parsed.version;
    if (schema !== CHAT_COMPOSER_DRAFT_SCHEMA_VERSION) return null;
    const updatedAt =
      typeof parsed.updatedAt === "number" ? parsed.updatedAt : Number.NaN;
    if (!Number.isFinite(updatedAt)) return null;
    if (Date.now() - updatedAt > CHAT_COMPOSER_DRAFT_MAX_AGE_MS) return null;
    return updatedAt;
  } catch {
    return null;
  }
}

function isNewChatDraftActive(userId: string | null): boolean {
  if (!userId) return false;
  const draft = getChatComposerDraft(NEW_CHAT_COMPOSER_DRAFT_KEY, userId);
  if (isComposerDraftActive(draft)) return true;
  const updatedAt = readPersistedNewChatDraftUpdatedAt(userId);
  return updatedAt !== null;
}

function bindDraftLifecycle(scope: SettingsScope): void {
  if (!scope.userId || scope.key !== NEW_CHAT_LLM_SETTINGS_KEY) return;
  const existing = draftLifecycleBindings.get(scope.userId);
  if (existing) return;

  const unsubscribe = subscribeChatComposerDraft(
    NEW_CHAT_COMPOSER_DRAFT_KEY,
    () => {
      if (!isNewChatDraftActive(scope.userId)) {
        clearPendingNewChatLlmSettings(scope.userId);
      }
    },
    scope.userId,
  );
  draftLifecycleBindings.set(scope.userId, unsubscribe);
}

function unbindDraftLifecycle(userId: string | null): void {
  if (!userId) return;
  const unsubscribe = draftLifecycleBindings.get(userId);
  if (!unsubscribe) return;
  unsubscribe();
  draftLifecycleBindings.delete(userId);
}

function cloneSettings(settings: SessionLlmSettings): SessionLlmSettings {
  return {
    agent_team_selection: {
      mode: settings.agent_team_selection.mode,
      team_id: settings.agent_team_selection.team_id,
      loaded_team_ids: [...settings.agent_team_selection.loaded_team_ids],
    },
    main_route: settings.main_route ? { ...settings.main_route } : {},
    special_routing: settings.special_routing
      ? { ...settings.special_routing }
      : {},
    execution_profile_id:
      typeof settings.execution_profile_id === "string"
        ? settings.execution_profile_id
        : "",
  };
}

function snapshotForSessionSave(
  settings: SessionLlmSettings,
  generationReadyMain?: SessionLlmSettings["main_route"] | null,
): Partial<SessionLlmSettings> {
  return buildHandoffSettingsPatch(settings, generationReadyMain);
}

function parsePersistedSettings(value: unknown): SessionLlmSettings | null {
  if (!value || typeof value !== "object") return null;
  const record = value as Record<string, unknown>;
  const schema = record.schema ?? record.version;
  if (schema !== NEW_CHAT_LLM_SETTINGS_SCHEMA_VERSION) return null;

  const updatedAt =
    typeof record.updatedAt === "number" ? record.updatedAt : Number.NaN;
  if (!Number.isFinite(updatedAt)) return null;
  if (Date.now() - updatedAt > NEW_CHAT_LLM_SETTINGS_MAX_AGE_MS) return null;

  const rawSettings =
    record.settings && typeof record.settings === "object"
      ? (record.settings as Record<string, unknown>)
      : record;
  if (
    !isAgentTeamSelection(rawSettings.agent_team_selection) ||
    (rawSettings.main_route !== undefined &&
      !isSessionMainRoute(rawSettings.main_route)) ||
    (rawSettings.special_routing !== undefined &&
      !isSpecialRouting(rawSettings.special_routing)) ||
    (rawSettings.execution_profile_id !== undefined &&
      typeof rawSettings.execution_profile_id !== "string")
  ) {
    return null;
  }

  const settings: SessionLlmSettings = {
    agent_team_selection: {
      mode: rawSettings.agent_team_selection.mode,
      team_id: rawSettings.agent_team_selection.team_id,
      loaded_team_ids: [...rawSettings.agent_team_selection.loaded_team_ids],
    },
    main_route: rawSettings.main_route ? { ...rawSettings.main_route } : {},
    execution_profile_id:
      typeof rawSettings.execution_profile_id === "string"
        ? rawSettings.execution_profile_id
        : "",
  };
  if (rawSettings.special_routing) {
    settings.special_routing = { ...rawSettings.special_routing };
  }
  return settings;
}

function hasMeaningfulPending(settings: SessionLlmSettings): boolean {
  const route = settings.main_route;
  const hasRoute = Boolean(route?.provider?.trim() || route?.model?.trim());
  const team = settings.agent_team_selection;
  const hasCustomTeam =
    team.mode !== "auto" ||
    Boolean(team.team_id.trim()) ||
    team.loaded_team_ids.length > 0;
  const hasFreeTeam =
    settings.special_routing?.routing_profile_id?.trim() === FREE_TEAM_ROUTING_PROFILE_ID;
  const hasExecutionProfile = Boolean(settings.execution_profile_id?.trim());
  return hasRoute || hasCustomTeam || hasFreeTeam || hasExecutionProfile;
}

function mergeSettings(
  current: SessionLlmSettings,
  patch: Partial<SessionLlmSettings>,
): SessionLlmSettings {
  const next: SessionLlmSettings = {
    agent_team_selection:
      patch.agent_team_selection ?? current.agent_team_selection,
    main_route:
      patch.main_route !== undefined
        ? { ...current.main_route, ...patch.main_route }
        : current.main_route,
    execution_profile_id:
      patch.execution_profile_id !== undefined
        ? patch.execution_profile_id
        : (current.execution_profile_id ?? ""),
  };
  if (patch.special_routing !== undefined) {
    next.special_routing = patch.special_routing
      ? { ...patch.special_routing }
      : undefined;
  } else if (current.special_routing) {
    next.special_routing = { ...current.special_routing };
  }
  return next;
}

function removeStoredSettings(storageKey: string | null): void {
  const storage = getBrowserStorage();
  if (!storageKey || !storage) return;
  try {
    storage.removeItem(storageKey);
  } catch {
    // localStorage が無効でもメモリ上の pending は維持する。
  }
}

function writeSettingsNow(scope: SettingsScope, settings: SessionLlmSettings): void {
  const storage = getBrowserStorage();
  const storageKey = getStorageKey(scope.userId, scope.key);
  if (!scope.userId || !storage || !storageKey) return;
  if (!hasMeaningfulPending(settings)) {
    removeStoredSettings(storageKey);
    return;
  }
  const payload: PersistedNewChatLlmSettings = {
    schema: NEW_CHAT_LLM_SETTINGS_SCHEMA_VERSION,
    updatedAt: Date.now(),
    settings: cloneSettings(settings),
  };
  try {
    storage.setItem(storageKey, JSON.stringify(payload));
  } catch {
    // 容量超過等は入力操作へ波及させない。
  }
}

function loadSettingsFromStorage(scope: SettingsScope): void {
  const storage = getBrowserStorage();
  if (!scope.userId || !storage) return;
  if (loadedScopes.has(scope.scopeKey)) return;
  loadedScopes.add(scope.scopeKey);

  bindDraftLifecycle(scope);

  if (!isNewChatDraftActive(scope.userId)) {
    removeStoredSettings(getStorageKey(scope.userId, scope.key));
    return;
  }

  const storageKey = getStorageKey(scope.userId, scope.key);
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
    removeStoredSettings(storageKey);
    return;
  }

  const settings = parsePersistedSettings(parsed);
  if (!settings || !hasMeaningfulPending(settings)) {
    removeStoredSettings(storageKey);
    return;
  }
  pendingByScope.set(scope.scopeKey, settings);
  notify(scope.scopeKey);
}

export function hydratePendingNewChatLlmSettings(userId?: string | null): void {
  const scope = getSettingsScope(NEW_CHAT_LLM_SETTINGS_KEY, userId);
  loadSettingsFromStorage(scope);
  if (scope.userId && !isNewChatDraftActive(scope.userId)) {
    pendingByScope.delete(scope.scopeKey);
    removeStoredSettings(getStorageKey(scope.userId, scope.key));
    notify(scope.scopeKey);
  }
}

export function getPendingNewChatLlmSettings(
  userId?: string | null,
): SessionLlmSettings {
  const scope = getSettingsScope(NEW_CHAT_LLM_SETTINGS_KEY, userId);
  return pendingByScope.get(scope.scopeKey) ?? getEmptySnapshot(scope.scopeKey);
}

export function getPendingNewChatLlmSettingsServerSnapshot(): SessionLlmSettings {
  return getEmptySnapshot(`${ANONYMOUS_SCOPE}\u0000${NEW_CHAT_LLM_SETTINGS_KEY}`);
}

export function subscribePendingNewChatLlmSettings(
  listener: () => void,
  userId?: string | null,
): () => void {
  const scope = getSettingsScope(NEW_CHAT_LLM_SETTINGS_KEY, userId);
  const scopeListeners = listeners.get(scope.scopeKey) ?? new Set<() => void>();
  scopeListeners.add(listener);
  listeners.set(scope.scopeKey, scopeListeners);
  return () => {
    scopeListeners.delete(listener);
    if (scopeListeners.size === 0) listeners.delete(scope.scopeKey);
  };
}

export function setPendingNewChatLlmSettings(
  settings: SessionLlmSettings,
  userId?: string | null,
): void {
  const scope = getSettingsScope(NEW_CHAT_LLM_SETTINGS_KEY, userId);
  if (!scope.userId) return;
  loadSettingsFromStorage(scope);
  const next = cloneSettings(settings);
  pendingByScope.set(scope.scopeKey, next);
  notify(scope.scopeKey);
  writeSettingsNow(scope, next);
}

export function updatePendingNewChatLlmSettings(
  patch: Partial<SessionLlmSettings>,
  userId?: string | null,
): void {
  const scope = getSettingsScope(NEW_CHAT_LLM_SETTINGS_KEY, userId);
  if (!scope.userId) return;
  if (patch.agent_team_selection && !scope.userId) {
    return;
  }
  loadSettingsFromStorage(scope);
  const current = getPendingNewChatLlmSettings(userId);
  const next = mergeSettings(current, patch);
  pendingByScope.set(scope.scopeKey, next);
  notify(scope.scopeKey);
  writeSettingsNow(scope, next);
}

export function clearPendingNewChatLlmSettings(userId?: string | null): void {
  const scope = getSettingsScope(NEW_CHAT_LLM_SETTINGS_KEY, userId);
  pendingByScope.delete(scope.scopeKey);
  removeStoredSettings(getStorageKey(scope.userId, scope.key));
  notify(scope.scopeKey);
}

export function hasPendingNewChatRoute(userId?: string | null): boolean {
  const route = getPendingNewChatLlmSettings(userId).main_route;
  return Boolean(route?.provider?.trim() && route?.model?.trim());
}

export async function applyPendingNewChatLlmSettingsToSession(
  sessionId: string,
  userId?: string | null,
  generationReadyMain?: SessionLlmSettings["main_route"] | null,
): Promise<SessionLlmSettingsResponse | null> {
  if (!sessionId) return null;
  const scope = getSettingsScope(NEW_CHAT_LLM_SETTINGS_KEY, userId);
  loadSettingsFromStorage(scope);
  const pending =
    pendingByScope.get(scope.scopeKey) ?? getEmptySnapshot(scope.scopeKey);
  const merged = cloneSettings(pending);
  // Callers must pass the generation-ready route, never the runtime-only
  // display fallback. A runtime route is not enough to establish session
  // authority and is rejected by the caller before session creation.
  if (hasExplicitSessionRoute(generationReadyMain)) {
    merged.main_route = generationReadyMain ? { ...generationReadyMain } : {};
  }
  if (!hasMeaningfulPending(merged)) return null;

  const saved = await saveSessionLlmSettings(
    sessionId,
    snapshotForSessionSave(merged, generationReadyMain),
  );
  const verified = assertPendingHandoffApplied(merged, saved);
  clearPendingNewChatLlmSettings(userId);
  return verified;
}

export function resetNewChatLlmSettingsStore(): void {
  draftLifecycleBindings.forEach((unsubscribe) => unsubscribe());
  draftLifecycleBindings.clear();
  pendingByScope.clear();
  loadedScopes.clear();
  listeners.forEach((scopeListeners) =>
    scopeListeners.forEach((listener) => listener()),
  );
}
