import { useCallback, useEffect, useState } from "react";
import { useFocusEffect } from "expo-router";
import {
  getChatWaveBackgroundEnabled,
  saveChatWaveBackgroundEnabled,
} from "../../lib/preferences";

export type ChatWavePreferenceState = {
  enabled: boolean;
  loaded: boolean;
  saving: boolean;
  error: string | null;
};

const preference: ChatWavePreferenceState = {
  enabled: false,
  loaded: false,
  saving: false,
  error: null,
};
const listeners = new Set<() => void>();
let loadPromise: Promise<void> | null = null;
let loadVersion = 0;

function notify(): void {
  listeners.forEach((listener) => listener());
}

async function loadPreference(): Promise<void> {
  if (preference.loaded) return;
  if (loadPromise) return loadPromise;
  const version = ++loadVersion;
  preference.error = null;
  preference.loaded = false;
  notify();
  loadPromise = getChatWaveBackgroundEnabled()
    .then((enabled) => {
      if (version !== loadVersion) return;
      preference.enabled = enabled;
      preference.loaded = true;
    })
    .catch(() => {
      if (version !== loadVersion) return;
      // A SecureStore failure must never make the chat unusable.  Keep the
      // renderer hidden and expose the failure to the settings screen.
      preference.enabled = false;
      preference.error = "チャット背景アニメーション設定を読み込めませんでした。";
      preference.loaded = true;
    })
    .finally(() => {
      if (version === loadVersion) {
        loadPromise = null;
        notify();
      }
    });
  return loadPromise;
}

export function useChatWavePreference(): ChatWavePreferenceState & {
  update: (enabled: boolean) => Promise<boolean>;
} {
  const [, setRevision] = useState(0);
  useEffect(() => {
    const listener = () => setRevision((value) => value + 1);
    listeners.add(listener);
    void loadPreference();
    return () => {
      listeners.delete(listener);
    };
  }, []);

  useFocusEffect(
    useCallback(() => {
      void loadPreference();
      return undefined;
    }, []),
  );

  const update = useCallback(async (enabled: boolean): Promise<boolean> => {
    if (!preference.loaded || preference.saving) return false;
    const previous = preference.enabled;
    preference.saving = true;
    preference.error = null;
    // Keep all mounted ConversationSurface instances in sync immediately;
    // failed writes below restore the previous value before reporting error.
    preference.enabled = enabled;
    notify();
    try {
      await saveChatWaveBackgroundEnabled(enabled);
      return true;
    } catch {
      preference.enabled = previous;
      preference.error = "チャット背景アニメーション設定を保存できませんでした。";
      return false;
    } finally {
      preference.saving = false;
      notify();
    }
  }, []);

  return { ...preference, update };
}

/** Test-only reset; production callers should keep the device cache intact. */
export function resetChatWavePreferenceForTests(): void {
  preference.enabled = false;
  preference.loaded = false;
  preference.saving = false;
  preference.error = null;
  loadPromise = null;
  loadVersion += 1;
  notify();
}
