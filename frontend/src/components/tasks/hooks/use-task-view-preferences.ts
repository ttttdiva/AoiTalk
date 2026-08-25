"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { useUserSettings } from "@/contexts/user-settings-context";
import {
  isUserSettingsRequestOffline,
  isUserSettingsRequestRetryable,
} from "@/lib/user-settings";

export const TASK_VIEW_PREFERENCES_KEY = "tasks-view-preferences";
/**
 * Legacy values did not carry an account id.  These two companion keys are
 * only honoured when the owner is explicitly known, so an anonymous value
 * cannot be copied from account A into account B on a shared browser.
 */
export const TASK_VIEW_PREFERENCES_LEGACY_OWNER_KEY =
  `${TASK_VIEW_PREFERENCES_KEY}:owner`;
export const TASK_VIEW_PREFERENCES_MIGRATION_PREFIX =
  `${TASK_VIEW_PREFERENCES_KEY}:migrated:`;
/** User-settings key.  The legacy localStorage key above is kept only as a
 * migration fallback for installs that render this hook outside the app
 * provider (and for offline/unauthenticated pages). */
export const TASK_VIEW_PREFERENCES_SETTING_KEY = "tasks_view_preferences";
export const TASK_VIEW_PREFERENCES_VERSION = 3;
export const TASK_VIEW_PREFERENCES_MAX_AUTO_RETRIES = 3;
export const TASK_VIEW_PREFERENCES_RETRY_DELAYS_MS = [
  250,
  750,
  2_000,
] as const;

export type TaskViewPreferencesSaveStatus =
  | "idle"
  | "saving"
  | "saved"
  | "error"
  | "offline";

export type TaskViewMode = "list" | "schedule";
export type LegacyTaskViewMode = "list" | "tree" | "timeline" | "dependency";

/** Desktop list columns. Task Name is structural and is always shown. */
export type TaskListColumn =
  | "project"
  | "start"
  | "due"
  | "priority"
  | "assignee"
  /** @deprecated Tags are rendered beside Task Name and are not a column. */
  | "tags"
  | "time";

export type TaskListColumnVisibility = Record<TaskListColumn, boolean>;

export type TaskListResizableColumn =
  | "taskName"
  | "project"
  | "start"
  | "due"
  | "priority"
  | "assignee"
  | "time";

export type TaskListColumnWidths = Record<TaskListResizableColumn, number>;

export const TASK_LIST_RESIZABLE_COLUMNS: readonly TaskListResizableColumn[] = [
  "taskName",
  "project",
  "start",
  "due",
  "priority",
  "assignee",
  "time",
];

/**
 * Widths are intentionally expressed in px rather than Tailwind classes.  The
 * values below are the baseline used when a user has no saved layout yet;
 * persisted per-column values always take precedence over these defaults.
 */
export const DEFAULT_TASK_COLUMN_WIDTHS: TaskListColumnWidths = {
  taskName: 280,
  project: 120,
  start: 168,
  due: 168,
  priority: 108,
  assignee: 144,
  // Keep Time Tracked denser than the old 112px utility width.  The cell
  // uses compact labels so the timer control and actual duration still fit.
  time: 104,
};

export const TASK_LIST_COLUMN_MIN_WIDTHS: TaskListColumnWidths = {
  taskName: 160,
  project: 96,
  start: 148,
  due: 148,
  priority: 80,
  assignee: 96,
  time: 96,
};

export const TASK_LIST_COLUMN_MAX_WIDTHS: TaskListColumnWidths = {
  taskName: 720,
  project: 480,
  start: 480,
  due: 480,
  priority: 320,
  assignee: 480,
  time: 360,
};

/** Stitch list defaults: keep the high-density table focused on dates/time. */
export const DEFAULT_TASK_COLUMN_VISIBILITY: TaskListColumnVisibility = {
  project: false,
  start: true,
  due: true,
  priority: false,
  assignee: false,
  tags: false,
  time: true,
};

export type TaskViewPreferences = {
  version: typeof TASK_VIEW_PREFERENCES_VERSION;
  viewMode: TaskViewMode;
  /** Optional for backwards compatibility with the original v2 shape. */
  columns?: Partial<TaskListColumnVisibility>;
  /** Optional for backwards compatibility with pre-resizable preferences. */
  columnWidths?: Partial<TaskListColumnWidths>;
};

export const DEFAULT_TASK_VIEW_PREFERENCES: TaskViewPreferences = {
  version: TASK_VIEW_PREFERENCES_VERSION,
  viewMode: "list",
};

const TASK_VIEW_MODES = new Set<TaskViewMode>(["list", "schedule"]);
const LEGACY_MODES = new Set<LegacyTaskViewMode>([
  "list",
  "tree",
  "timeline",
  "dependency",
]);

const TASK_COLUMNS = new Set<TaskListColumn>([
  "project",
  "start",
  "due",
  "priority",
  "assignee",
  "time",
]);

function parseColumns(value: unknown): Partial<TaskListColumnVisibility> | undefined {
  if (!value || typeof value !== "object") return undefined;
  const columns: Partial<TaskListColumnVisibility> = {};
  for (const [key, visible] of Object.entries(value)) {
    if (TASK_COLUMNS.has(key as TaskListColumn) && typeof visible === "boolean") {
      columns[key as TaskListColumn] = visible;
    }
  }
  return Object.keys(columns).length > 0 ? columns : undefined;
}

function parseColumnWidths(value: unknown): Partial<TaskListColumnWidths> | undefined {
  if (!value || typeof value !== "object") return undefined;
  const widths: Partial<TaskListColumnWidths> = {};
  for (const [key, rawWidth] of Object.entries(value)) {
    if (!TASK_LIST_RESIZABLE_COLUMNS.includes(key as TaskListResizableColumn)) {
      continue;
    }
    const width = typeof rawWidth === "number" ? rawWidth : Number(rawWidth);
    if (!Number.isFinite(width)) continue;
    const column = key as TaskListResizableColumn;
    widths[column] = Math.min(
      TASK_LIST_COLUMN_MAX_WIDTHS[column],
      Math.max(TASK_LIST_COLUMN_MIN_WIDTHS[column], Math.round(width)),
    );
  }
  return Object.keys(widths).length > 0 ? widths : undefined;
}

export function resolveTaskColumnVisibility(
  columns?: Partial<TaskListColumnVisibility> | null,
): TaskListColumnVisibility {
  return {
    ...DEFAULT_TASK_COLUMN_VISIBILITY,
    ...(columns ?? {}),
    // Tags are always rendered inline with Task Name.  Force the legacy key
    // off so stale v2 localStorage values cannot resurrect a Tags column.
    tags: false,
  };
}

export function resolveTaskColumnWidths(
  widths?: Partial<TaskListColumnWidths> | null,
): TaskListColumnWidths {
  const resolved = { ...DEFAULT_TASK_COLUMN_WIDTHS };
  for (const column of TASK_LIST_RESIZABLE_COLUMNS) {
    const width = widths?.[column];
    if (typeof width !== "number" || !Number.isFinite(width)) continue;
    resolved[column] = Math.min(
      TASK_LIST_COLUMN_MAX_WIDTHS[column],
      Math.max(TASK_LIST_COLUMN_MIN_WIDTHS[column], Math.round(width)),
    );
  }
  return resolved;
}

/**
 * Restore the current two-view preference shape. Version 1 values are read
 * once and migrated in memory; the hook persists the new version immediately
 * after hydration. Invalid JSON never prevents the task page from mounting.
 */
export function parseTaskViewPreferences(
  value: string | null | undefined,
): TaskViewPreferences {
  if (!value) return DEFAULT_TASK_VIEW_PREFERENCES;
  try {
    const parsed = JSON.parse(value) as {
      version?: unknown;
      viewMode?: unknown;
      columns?: unknown;
      columnWidths?: unknown;
    } | null;
    if (!parsed || typeof parsed !== "object") return DEFAULT_TASK_VIEW_PREFERENCES;
    if (parsed.version === TASK_VIEW_PREFERENCES_VERSION) {
      return TASK_VIEW_MODES.has(parsed.viewMode as TaskViewMode)
        ? (() => {
            const columns = parseColumns(parsed.columns);
            const columnWidths = parseColumnWidths(parsed.columnWidths);
            return columns
              ? {
                  version: TASK_VIEW_PREFERENCES_VERSION,
                  viewMode: parsed.viewMode as TaskViewMode,
                  columns,
                  ...(columnWidths ? { columnWidths } : {}),
                }
              : columnWidths
                ? {
                    version: TASK_VIEW_PREFERENCES_VERSION,
                    viewMode: parsed.viewMode as TaskViewMode,
                    columnWidths,
                  }
                : {
                    version: TASK_VIEW_PREFERENCES_VERSION,
                    viewMode: parsed.viewMode as TaskViewMode,
                  };
          })()
        : DEFAULT_TASK_VIEW_PREFERENCES;
    }
    if (
      (parsed.version === 1 &&
        LEGACY_MODES.has(parsed.viewMode as LegacyTaskViewMode)) ||
      (parsed.version === 2 &&
        (TASK_VIEW_MODES.has(parsed.viewMode as TaskViewMode) ||
          LEGACY_MODES.has(parsed.viewMode as LegacyTaskViewMode)))
    ) {
      const columns = parsed.version === 2 ? parseColumns(parsed.columns) : undefined;
      return {
        version: TASK_VIEW_PREFERENCES_VERSION,
        viewMode:
          parsed.viewMode === "list" ? "list" : "schedule",
        ...(columns ? { columns } : {}),
      };
    }
    return DEFAULT_TASK_VIEW_PREFERENCES;
  } catch {
    return DEFAULT_TASK_VIEW_PREFERENCES;
  }
}

type LegacyTaskViewMigration = {
  userId: string;
  preferences: TaskViewPreferences;
};

type PendingMigration = {
  userId: string;
  signature: string;
};

type PersistenceQueueItem = {
  signature: string;
  preferences: TaskViewPreferences;
  migrationUserId?: string;
};

/**
 * Read the fixed localStorage key only when it can be safely attributed to
 * the currently authenticated user.  Values written by older releases were
 * anonymous, so they are deliberately ignored unless an owner companion key
 * (or an embedded ownerUserId/userId field) is present.
 */
function readLegacyPreferencesForUser(
  userId: string | null | undefined,
): LegacyTaskViewMigration | null {
  if (!userId) return null;
  try {
    if (
      window.localStorage.getItem(
        `${TASK_VIEW_PREFERENCES_MIGRATION_PREFIX}${userId}`,
      ) === "1"
    ) {
      return null;
    }
    const raw = window.localStorage.getItem(TASK_VIEW_PREFERENCES_KEY);
    if (!raw) return null;

    let embeddedOwner: string | null = null;
    try {
      const parsed = JSON.parse(raw) as {
        ownerUserId?: unknown;
        userId?: unknown;
      } | null;
      if (parsed && typeof parsed === "object") {
        if (typeof parsed.ownerUserId === "string") {
          embeddedOwner = parsed.ownerUserId;
        } else if (typeof parsed.userId === "string") {
          embeddedOwner = parsed.userId;
        }
      }
    } catch {
      return null;
    }

    const owner =
      embeddedOwner ??
      window.localStorage.getItem(TASK_VIEW_PREFERENCES_LEGACY_OWNER_KEY);
    if (owner !== userId) return null;
    return {
      userId,
      preferences: parseTaskViewPreferences(raw),
    };
  } catch {
    return null;
  }
}

export function useTaskViewPreferences() {
  const {
    settings,
    settingsReady,
    settingsError,
    settingsLoadStatus,
    userId,
    patch: patchUserSettings,
  } = useUserSettings();
  const hasSettingsProvider = settingsReady !== undefined;
  const [preferences, setPreferences] = useState<TaskViewPreferences>(
    DEFAULT_TASK_VIEW_PREFERENCES,
  );
  const [storageReady, setStorageReady] = useState(false);
  const [saveStatus, setSaveStatus] =
    useState<TaskViewPreferencesSaveStatus>("idle");
  const [saveError, setSaveError] = useState<Error | null>(null);
  const serverHydratedRef = useRef(false);
  const hydratedUserIdRef = useRef<string | null | undefined>(undefined);
  const persistedSignatureRef = useRef<string | null>(null);
  const persistenceQueueRef = useRef<PersistenceQueueItem | null>(null);
  const persistenceInFlightRef = useRef(false);
  const persistenceGenerationRef = useRef(0);
  const mountedRef = useRef(false);
  const retryTimerRef = useRef<number | null>(null);
  const retryAttemptRef = useRef(0);
  const queuedSignatureRef = useRef<string | null>(null);
  const pendingMigrationRef = useRef<PendingMigration | null>(null);
  const flushPersistenceQueueRef = useRef<(() => void) | null>(null);
  const hydratingUserRef = useRef(false);
  const awaitingFreshUserSettingsRef = useRef(false);
  const settingsErrorRef = useRef(false);

  const browserIsOnline = useCallback(
    () =>
      typeof navigator === "undefined" || navigator.onLine !== false,
    [],
  );

  const clearRetryTimer = useCallback(() => {
    if (retryTimerRef.current !== null) {
      window.clearTimeout(retryTimerRef.current);
      retryTimerRef.current = null;
    }
  }, []);

  const normalizeSaveError = useCallback((error: unknown): Error => {
    if (error instanceof Error) return error;
    return new Error(String(error || "ユーザー設定の保存に失敗しました"));
  }, []);

  const flushPersistenceQueue = useCallback(() => {
    if (!mountedRef.current) return;
    if (persistenceInFlightRef.current) return;
    const next = persistenceQueueRef.current;
    if (!next) return;
    if (persistedSignatureRef.current === next.signature) {
      persistenceQueueRef.current = null;
      queuedSignatureRef.current = null;
      retryAttemptRef.current = 0;
      setSaveError(null);
      setSaveStatus("saved");
      return;
    }
    if (!browserIsOnline()) {
      setSaveStatus("offline");
      return;
    }

    persistenceQueueRef.current = null;
    queuedSignatureRef.current = next.signature;
    const operationGeneration = persistenceGenerationRef.current;
    setSaveError(null);
    setSaveStatus("saving");

    persistenceInFlightRef.current = true;
    let continueWithQueuedSnapshot = true;
    void Promise.resolve()
      .then(() =>
        patchUserSettings({
          [TASK_VIEW_PREFERENCES_SETTING_KEY]: next.preferences,
        }),
      )
      .then(() => {
        if (
          !mountedRef.current ||
          operationGeneration !== persistenceGenerationRef.current
        ) {
          return;
        }
        // Update the signature only after PATCH succeeds.  Requests are
        // serialized, so an older response can never overwrite a newer one.
        persistedSignatureRef.current = next.signature;
        retryAttemptRef.current = 0;
        if (
          next.migrationUserId &&
          pendingMigrationRef.current?.userId === next.migrationUserId
        ) {
          try {
            window.localStorage.setItem(
              `${TASK_VIEW_PREFERENCES_MIGRATION_PREFIX}${next.migrationUserId}`,
              "1",
            );
          } catch {
            // localStorage is optional; server settings are already migrated.
          }
          pendingMigrationRef.current = null;
        }
        if (!persistenceQueueRef.current) {
          queuedSignatureRef.current = null;
          setSaveError(null);
          setSaveStatus("saved");
        }
      })
      .catch((error: unknown) => {
        if (
          !mountedRef.current ||
          operationGeneration !== persistenceGenerationRef.current
        ) {
          return;
        }
        const normalizedError = normalizeSaveError(error);
        const offline =
          !browserIsOnline() || isUserSettingsRequestOffline(normalizedError);
        const newerSnapshot =
          persistenceQueueRef.current &&
          persistenceQueueRef.current.signature !== next.signature;
        if (
          newerSnapshot
        ) {
          continueWithQueuedSnapshot = true;
        } else {
          persistenceQueueRef.current = next;
          continueWithQueuedSnapshot = false;
        }
        setSaveError(normalizedError);
        if (offline) {
          setSaveStatus("offline");
        } else if (
          !newerSnapshot &&
          isUserSettingsRequestRetryable(normalizedError) &&
          retryAttemptRef.current < TASK_VIEW_PREFERENCES_MAX_AUTO_RETRIES
        ) {
          const retryIndex = retryAttemptRef.current;
          retryAttemptRef.current += 1;
          setSaveStatus("saving");
          clearRetryTimer();
          retryTimerRef.current = window.setTimeout(() => {
            retryTimerRef.current = null;
            if (!mountedRef.current) return;
            flushPersistenceQueueRef.current?.();
          }, TASK_VIEW_PREFERENCES_RETRY_DELAYS_MS[retryIndex] ?? 2_000);
        } else {
          setSaveStatus("error");
        }
      })
      .finally(() => {
        persistenceInFlightRef.current = false;
        if (
          mountedRef.current &&
          (continueWithQueuedSnapshot ||
            operationGeneration !== persistenceGenerationRef.current)
        ) {
          flushPersistenceQueueRef.current?.();
        }
      });
  }, [
    browserIsOnline,
    clearRetryTimer,
    normalizeSaveError,
    patchUserSettings,
  ]);
  useEffect(() => {
    flushPersistenceQueueRef.current = flushPersistenceQueue;
    return () => {
      if (flushPersistenceQueueRef.current === flushPersistenceQueue) {
        flushPersistenceQueueRef.current = null;
      }
    };
  }, [flushPersistenceQueue]);

  useEffect(() => {
    mountedRef.current = true;
    const handleOnline = () => {
      if (!persistenceQueueRef.current) return;
      retryAttemptRef.current = 0;
      clearRetryTimer();
      setSaveError(null);
      setSaveStatus("saving");
      flushPersistenceQueueRef.current?.();
    };
    const handleOffline = () => {
      if (persistenceQueueRef.current) setSaveStatus("offline");
    };
    window.addEventListener("online", handleOnline);
    window.addEventListener("offline", handleOffline);
    return () => {
      mountedRef.current = false;
      persistenceGenerationRef.current += 1;
      clearRetryTimer();
      persistenceQueueRef.current = null;
      queuedSignatureRef.current = null;
      flushPersistenceQueueRef.current = null;
      window.removeEventListener("online", handleOnline);
      window.removeEventListener("offline", handleOffline);
    };
  }, [clearRetryTimer]);

  const readLegacyPreferences = useCallback(() => {
    try {
      return parseTaskViewPreferences(
        window.localStorage.getItem(TASK_VIEW_PREFERENCES_KEY),
      );
    } catch {
      // localStorageが無効な環境ではタスク一覧の既定表示を利用する。
      return DEFAULT_TASK_VIEW_PREFERENCES;
    }
  }, []);

  useEffect(() => {
    let active = true;
    if (hasSettingsProvider) {
      if (settingsLoadStatus === "loading") {
        if (serverHydratedRef.current) {
          awaitingFreshUserSettingsRef.current = true;
          hydratingUserRef.current = true;
          if (hydratedUserIdRef.current !== userId) {
            // The provider clears its snapshot asynchronously on account
            // changes.  Do not keep rendering account A's layout while the
            // account B request is in flight (or let a queued A write leak).
            persistedSignatureRef.current = null;
            persistenceQueueRef.current = null;
            queuedSignatureRef.current = null;
            pendingMigrationRef.current = null;
            persistenceGenerationRef.current += 1;
            retryAttemptRef.current = 0;
            clearRetryTimer();
            window.queueMicrotask(() => {
              if (active) setPreferences(DEFAULT_TASK_VIEW_PREFERENCES);
            });
          }
        }
        return () => {
          active = false;
        };
      }
      if (!settingsReady) {
        // The provider clears its snapshot before fetching a switched
        // account. Keep the old account id until the ready=true pass so the
        // stale settings object cannot be hydrated into the new user.
        if (
          serverHydratedRef.current &&
          hydratedUserIdRef.current !== userId
        ) {
          awaitingFreshUserSettingsRef.current = true;
          hydratingUserRef.current = true;
        }
        return () => {
          active = false;
        };
      }
      if (settingsError) {
        settingsErrorRef.current = true;
        // Keep a task list usable while the settings GET is unavailable, but
        // do not write defaults over an account whose server state is unknown.
        if (awaitingFreshUserSettingsRef.current || hydratingUserRef.current) {
          // A switched account must never display the previous account's
          // preferences, even when its first GET fails.  The next successful
          // refresh will hydrate the scoped server value.
          window.queueMicrotask(() => {
            if (active) setPreferences(DEFAULT_TASK_VIEW_PREFERENCES);
          });
        }
        if (!storageReady) {
          serverHydratedRef.current = true;
          hydratedUserIdRef.current = userId;
          persistenceQueueRef.current = null;
          queuedSignatureRef.current = null;
          persistedSignatureRef.current = JSON.stringify(
            DEFAULT_TASK_VIEW_PREFERENCES,
          );
          window.queueMicrotask(() => {
            if (!active) return;
            setPreferences(DEFAULT_TASK_VIEW_PREFERENCES);
            setStorageReady(true);
          });
        }
        return () => {
          active = false;
        };
      }
      const recoveredFromSettingsError = settingsErrorRef.current;
      settingsErrorRef.current = false;
      if (
          (storageReady &&
          serverHydratedRef.current &&
          hydratedUserIdRef.current === userId &&
          !hydratingUserRef.current &&
          !recoveredFromSettingsError &&
          !awaitingFreshUserSettingsRef.current)
      ) {
        return () => {
          active = false;
        };
      }
      const switchedUser =
        serverHydratedRef.current && hydratedUserIdRef.current !== userId;
      if (switchedUser) {
        // Do not let the previous account's in-memory snapshot persist while
        // the new user's GET response is being hydrated.
        hydratingUserRef.current = true;
        persistedSignatureRef.current = null;
        persistenceQueueRef.current = null;
        queuedSignatureRef.current = null;
        persistenceGenerationRef.current += 1;
        retryAttemptRef.current = 0;
        clearRetryTimer();
        pendingMigrationRef.current = null;
        window.queueMicrotask(() => {
          if (active) {
            setSaveError(null);
            setSaveStatus("idle");
          }
        });
      }
      serverHydratedRef.current = true;
      hydratedUserIdRef.current = userId;
      const raw = settings[TASK_VIEW_PREFERENCES_SETTING_KEY];
      const hasServerPreferences = raw !== undefined && raw !== null;
      let serverVersion: unknown;
      if (typeof raw === "string") {
        try {
          const parsed = JSON.parse(raw) as { version?: unknown } | null;
          serverVersion = parsed?.version;
        } catch {
          serverVersion = undefined;
        }
      } else if (raw && typeof raw === "object") {
        serverVersion = (raw as { version?: unknown }).version;
      }
      let saved =
        typeof raw === "string"
          ? parseTaskViewPreferences(raw)
          : raw && typeof raw === "object"
            ? parseTaskViewPreferences(JSON.stringify(raw))
            : DEFAULT_TASK_VIEW_PREFERENCES;
      const legacyMigration =
        !hasServerPreferences && userId
          ? readLegacyPreferencesForUser(userId)
          : null;
      if (legacyMigration) saved = legacyMigration.preferences;
      const savedSignature = JSON.stringify(saved);
      // Server-stored v1/v2 values are already user-scoped.  Queue a one-time
      // v3 write after hydration so view mode/visibility survive the upgrade.
      const needsServerVersionMigration =
        hasServerPreferences && serverVersion !== TASK_VIEW_PREFERENCES_VERSION;
      persistedSignatureRef.current =
        legacyMigration || needsServerVersionMigration ? null : savedSignature;
      pendingMigrationRef.current = legacyMigration
        ? { userId: legacyMigration.userId, signature: savedSignature }
        : null;
      retryAttemptRef.current = 0;
      clearRetryTimer();
      window.queueMicrotask(() => {
        if (!active) return;
        setPreferences(saved);
        setStorageReady(true);
        awaitingFreshUserSettingsRef.current = false;
        hydratingUserRef.current = false;
      });
      return () => {
        active = false;
      };
    }

    // Unit tests and legacy embeds do not mount UserSettingsProvider.  Keep
    // the old localStorage path for those environments only.  The app always
    // mounts the provider with a user id, so an authenticated user's settings
    // never use this anonymous fixed-key fallback.
    if (storageReady && serverHydratedRef.current) return;
    serverHydratedRef.current = true;
    hydratedUserIdRef.current = undefined;
    hydratingUserRef.current = false;
    pendingMigrationRef.current = null;
    persistenceQueueRef.current = null;
    const saved = readLegacyPreferences();
    persistedSignatureRef.current = JSON.stringify(saved);
    window.queueMicrotask(() => {
      if (!active) return;
      setPreferences(saved);
      setStorageReady(true);
    });
    return () => {
      active = false;
    };
  }, [
    clearRetryTimer,
    hasSettingsProvider,
    readLegacyPreferences,
    settings,
    settingsError,
    settingsLoadStatus,
    settingsReady,
    storageReady,
    userId,
  ]);

  useEffect(() => {
    if (!storageReady) return;
    // In the full app, the server-side user settings object is authoritative.
    // Keep localStorage only for legacy/unit-test embeds that do not mount the
    // provider; this avoids leaking one user's widths into another user on a
    // shared browser while retaining backwards compatibility for those embeds.
    if (!hasSettingsProvider) {
      try {
        window.localStorage.setItem(
          TASK_VIEW_PREFERENCES_KEY,
          JSON.stringify(preferences),
        );
      } catch {
        // localStorageが無効な環境ではメモリ上の設定だけを利用する。
      }
      return;
    }
    if (!settingsReady) return;
    if (hydratingUserRef.current) return;
    // A failed initial GET leaves the local view usable but must not turn the
    // default snapshot into an overwrite of unknown server state.
    if (settingsError) return;
    const signature = JSON.stringify(preferences);
    if (queuedSignatureRef.current !== signature) {
      retryAttemptRef.current = 0;
      clearRetryTimer();
      queuedSignatureRef.current = signature;
    }
    if (
      persistedSignatureRef.current === signature &&
      !persistenceInFlightRef.current
    ) {
      return;
    }
    if (
      persistenceInFlightRef.current &&
      persistenceQueueRef.current?.signature === signature
    ) {
      return;
    }
    persistenceQueueRef.current = {
      signature,
      preferences,
      migrationUserId:
        pendingMigrationRef.current?.userId ?? undefined,
    };
    window.queueMicrotask(() => flushPersistenceQueueRef.current?.());
  }, [
    clearRetryTimer,
    hasSettingsProvider,
    patchUserSettings,
    preferences,
    settingsError,
    settingsReady,
    storageReady,
    userId,
    flushPersistenceQueue,
  ]);

  const retrySave = useCallback(() => {
    if (!mountedRef.current || !hasSettingsProvider || settingsError) return;
    const signature = JSON.stringify(preferences);
    if (
      !persistenceQueueRef.current &&
      persistedSignatureRef.current !== signature
    ) {
      persistenceQueueRef.current = {
        signature,
        preferences,
        migrationUserId:
          pendingMigrationRef.current?.userId ?? undefined,
      };
    }
    if (!persistenceQueueRef.current) return;
    clearRetryTimer();
    retryAttemptRef.current = 0;
    setSaveError(null);
    if (!browserIsOnline()) {
      setSaveStatus("offline");
      return;
    }
    setSaveStatus("saving");
    flushPersistenceQueueRef.current?.();
  }, [
    browserIsOnline,
    clearRetryTimer,
    hasSettingsProvider,
    preferences,
    settingsError,
  ]);

  const setViewMode = useCallback((viewMode: TaskViewMode) => {
    setSaveError(null);
    setPreferences((current) => ({
      ...current,
      viewMode: viewMode === "list" ? "list" : "schedule",
    }));
  }, []);

  const columnVisibility = resolveTaskColumnVisibility(preferences.columns);
  const setColumnVisibility = useCallback(
    (column: TaskListColumn, visible: boolean) => {
      // Kept in the type only so callers can safely pass legacy values.  The
      // list no longer exposes a Tags column toggle.
      if (column === "tags") return;
      setSaveError(null);
      setPreferences((current) => ({
        ...current,
        columns: {
          ...resolveTaskColumnVisibility(current.columns),
          [column]: visible,
        },
      }));
    },
    [],
  );

  const columnWidths = useMemo(
    () => resolveTaskColumnWidths(preferences.columnWidths),
    [preferences.columnWidths],
  );
  const setColumnWidth = useCallback(
    (column: TaskListResizableColumn, width: number) => {
      const normalized = resolveTaskColumnWidths({ [column]: width })[column];
      setSaveError(null);
      setPreferences((current) => ({
        ...current,
        columnWidths: {
          ...resolveTaskColumnWidths(current.columnWidths),
          [column]: normalized,
        },
      }));
    },
    [],
  );

  return {
    preferences,
    storageReady,
    saveStatus,
    // Aliases keep the hook convenient for callers that use the queue's
    // persistence terminology while `saveStatus` remains the canonical API.
    saveState: saveStatus,
    persistenceStatus: saveStatus,
    saveError,
    persistenceError: saveError,
    retrySave,
    retry: retrySave,
    setViewMode,
    columnVisibility,
    setColumnVisibility,
    columnWidths,
    setColumnWidth,
  };
}
