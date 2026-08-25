import {
  DEFAULT_AUDIO_PLAYER_SETTINGS,
  normalizeAudioPlayerSettings,
  type AudioPlayerSettings,
} from "./audio-player-settings";
import type { AppNavigationVisibility } from "./app-navigation";

export type UserSettings = Record<string, unknown>;
export type UserSettingsRequestOptions = {
  signal?: AbortSignal;
};

/** Error emitted by the settings client while retaining enough information
 * for callers to distinguish a retryable transport failure from a rejected
 * request. */
export class UserSettingsRequestError extends Error {
  readonly status?: number;
  readonly retryable: boolean;
  readonly offline: boolean;

  constructor(
    message: string,
    options: {
      status?: number;
      retryable: boolean;
      offline?: boolean;
      cause?: unknown;
    },
  ) {
    super(message);
    this.name = "UserSettingsRequestError";
    this.status = options.status;
    this.retryable = options.retryable;
    this.offline = options.offline === true;
    if (options.cause !== undefined) {
      (this as Error & { cause?: unknown }).cause = options.cause;
    }
  }
}

function browserIsOffline(): boolean {
  return typeof navigator !== "undefined" && navigator.onLine === false;
}

function isRetryableStatus(status: number): boolean {
  return status === 408 || status === 425 || status === 429 || status >= 500;
}

function settingsRequestError(
  message: string,
  options: {
    status?: number;
    cause?: unknown;
  } = {},
): UserSettingsRequestError {
  const offline = browserIsOffline();
  return new UserSettingsRequestError(message, {
    status: options.status,
    retryable:
      offline ||
      options.status === undefined ||
      isRetryableStatus(options.status),
    offline,
    cause: options.cause,
  });
}

export function isUserSettingsRequestRetryable(error: unknown): boolean {
  if (error instanceof UserSettingsRequestError) return error.retryable;
  // Test doubles and callers that wrap fetch failures in a plain Error should
  // still receive the bounded retry behaviour. Explicit 4xx errors should be
  // represented by UserSettingsRequestError and are not retried.
  if (error && typeof error === "object" && "status" in error) {
    const status = (error as { status?: unknown }).status;
    return typeof status !== "number" || isRetryableStatus(status);
  }
  return true;
}

export function isUserSettingsRequestOffline(error: unknown): boolean {
  return (
    (error instanceof UserSettingsRequestError && error.offline) ||
    browserIsOffline()
  );
}

export type EditorLinkDefaultDisplayMode = "embed" | "link";
export const DEFAULT_EDITOR_LINK_DISPLAY_MODE: EditorLinkDefaultDisplayMode =
  "embed";
export const DEFAULT_TASK_NOTIFICATIONS_ENABLED = true;
export const DEFAULT_REMOTE_SERVER_CONNECTION_ENABLED = false;
export const DEFAULT_SCENARIO_TAB_VISIBLE = false; // public-publish-default: false
export const DEFAULT_TRPG_TAB_VISIBLE = false; // public-publish-default: false
export type { AppNavigationVisibility };

export function normalizeEditorLinkDefaultDisplayMode(
  value: unknown,
): EditorLinkDefaultDisplayMode {
  return value === "link" || value === "embed"
    ? value
    : DEFAULT_EDITOR_LINK_DISPLAY_MODE;
}

export function getEditorLinkDefaultDisplayMode(
  settings: UserSettings,
): EditorLinkDefaultDisplayMode {
  const editor = settings.editor;
  if (typeof editor !== "object" || editor === null || Array.isArray(editor)) {
    return DEFAULT_EDITOR_LINK_DISPLAY_MODE;
  }

  return normalizeEditorLinkDefaultDisplayMode(
    (editor as Record<string, unknown>).link_default_display_mode,
  );
}

export function getTaskNotificationsDefaultEnabled(
  settings: UserSettings | null | undefined,
): boolean {
  return typeof settings?.task_notifications_default_enabled === "boolean"
    ? settings.task_notifications_default_enabled
    : DEFAULT_TASK_NOTIFICATIONS_ENABLED;
}

export function getRemoteServerConnectionEnabled(
  settings: UserSettings | null | undefined,
): boolean {
  return settings?.remote_server_connection_enabled === true
    ? true
    : DEFAULT_REMOTE_SERVER_CONNECTION_ENABLED;
}

export function getAppNavigationVisibility(
  settings: UserSettings | null | undefined,
): AppNavigationVisibility {
  const navigationTabs = settings?.navigation_tabs;
  if (
    typeof navigationTabs !== "object" ||
    navigationTabs === null ||
    Array.isArray(navigationTabs)
  ) {
    return {
      scenarios: DEFAULT_SCENARIO_TAB_VISIBLE,
      trpg: DEFAULT_TRPG_TAB_VISIBLE,
    };
  }

  const values = navigationTabs as Record<string, unknown>;
  return {
    scenarios:
      typeof values.scenarios === "boolean"
        ? values.scenarios
        : DEFAULT_SCENARIO_TAB_VISIBLE,
    trpg:
      typeof values.trpg === "boolean"
        ? values.trpg
        : DEFAULT_TRPG_TAB_VISIBLE,
  };
}

export function getAudioPlayerSettings(
  settings: UserSettings | null | undefined,
): AudioPlayerSettings {
  return normalizeAudioPlayerSettings(settings?.audio_player);
}

export { DEFAULT_AUDIO_PLAYER_SETTINGS };
export type { AudioPlayerSettings };

function timeoutSignal(): AbortSignal | undefined {
  return typeof AbortSignal !== "undefined" && "timeout" in AbortSignal
    ? AbortSignal.timeout(5000)
    : undefined;
}

export async function getUserSettings(
  options: UserSettingsRequestOptions = {},
): Promise<UserSettings> {
  let res: Response;
  try {
    res = await fetch("/api/users/me/settings", {
      credentials: "include",
      signal: options.signal ?? timeoutSignal(),
    });
  } catch (error) {
    throw settingsRequestError("ユーザー設定の取得に失敗しました", {
      cause: error,
    });
  }

  if (!res.ok) {
    throw settingsRequestError("ユーザー設定の取得に失敗しました", {
      status: res.status,
    });
  }

  try {
    const data = (await res.json()) as { settings?: UserSettings };
    return data.settings ?? {};
  } catch (error) {
    throw settingsRequestError("ユーザー設定の取得に失敗しました", {
      cause: error,
    });
  }
}

export async function patchUserSettings(
  patch: UserSettings,
  options: UserSettingsRequestOptions = {},
): Promise<UserSettings> {
  let res: Response;
  try {
    res = await fetch("/api/users/me/settings", {
      method: "PATCH",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
      signal: options.signal ?? timeoutSignal(),
    });
  } catch (error) {
    throw settingsRequestError("ユーザー設定の保存に失敗しました", {
      cause: error,
    });
  }

  if (!res.ok) {
    throw settingsRequestError("ユーザー設定の保存に失敗しました", {
      status: res.status,
    });
  }

  try {
    const data = (await res.json()) as { settings?: UserSettings };
    return data.settings ?? {};
  } catch (error) {
    throw settingsRequestError("ユーザー設定の保存に失敗しました", {
      cause: error,
    });
  }
}
