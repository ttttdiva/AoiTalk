import {
  DEFAULT_AUDIO_PLAYER_SETTINGS,
  normalizeAudioPlayerSettings,
  type AudioPlayerSettings,
} from "./audio-player-settings";

export type UserSettings = Record<string, unknown>;
export type EditorLinkDefaultDisplayMode = "embed" | "link";
export const DEFAULT_EDITOR_LINK_DISPLAY_MODE: EditorLinkDefaultDisplayMode =
  "embed";
export const DEFAULT_TASK_NOTIFICATIONS_ENABLED = true;

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

export function getAudioPlayerSettings(
  settings: UserSettings | null | undefined,
): AudioPlayerSettings {
  return normalizeAudioPlayerSettings(settings?.audio_player);
}

export { DEFAULT_AUDIO_PLAYER_SETTINGS };
export type { AudioPlayerSettings };

export async function getUserSettings(): Promise<UserSettings> {
  const res = await fetch("/api/users/me/settings", {
    credentials: "include",
    signal: AbortSignal.timeout(5000),
  });

  if (!res.ok) {
    throw new Error("ユーザー設定の取得に失敗しました");
  }

  const data = (await res.json()) as { settings?: UserSettings };
  return data.settings ?? {};
}

export async function patchUserSettings(
  patch: UserSettings,
): Promise<UserSettings> {
  const res = await fetch("/api/users/me/settings", {
    method: "PATCH",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
    signal: AbortSignal.timeout(5000),
  });

  if (!res.ok) {
    throw new Error("ユーザー設定の保存に失敗しました");
  }

  const data = (await res.json()) as { settings?: UserSettings };
  return data.settings ?? {};
}
