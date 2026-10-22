import { useCallback, useEffect, useState } from "react";
import {
  safeLocalStorageGetItem,
  safeLocalStorageSetItem,
} from "@/lib/safe-storage";

/**
 * The wave background is a device-local presentation preference.  Keep the
 * key independent from user settings so a signed-in user can use the same
 * browser preference across accounts.
 */
export const CHAT_BACKGROUND_ANIMATION_STORAGE_KEY =
  "aoitalk-chat-background-animation";

export const CHAT_BACKGROUND_ANIMATION_CHANGE_EVENT =
  "aoitalk:chat-background-animation-change";

export type ChatBackgroundAnimationPreference = boolean | null;

/**
 * Parse only values written by this feature.  Unknown values are treated as
 * missing and therefore fall back to the documented default (enabled).
 */
export function normalizeChatBackgroundAnimationPreference(
  value: string | null | undefined,
): boolean | null {
  if (value === "true" || value === "1" || value === "on") return true;
  if (value === "false" || value === "0" || value === "off") return false;
  return null;
}

export function readChatBackgroundAnimationPreference(): boolean {
  return (
    normalizeChatBackgroundAnimationPreference(
      safeLocalStorageGetItem(CHAT_BACKGROUND_ANIMATION_STORAGE_KEY),
    ) ?? true
  );
}

export function saveChatBackgroundAnimationPreference(enabled: boolean): boolean {
  const saved = safeLocalStorageSetItem(
    CHAT_BACKGROUND_ANIMATION_STORAGE_KEY,
    enabled ? "true" : "false",
  );
  if (!saved || typeof window === "undefined") return saved;

  // Storage events are intentionally not delivered to the same document.
  // Notify a second hook mounted in this tab (for example a settings surface
  // kept alive by the app shell) without making rendering depend on React
  // state from the animation loop.
  try {
    window.dispatchEvent(
      new CustomEvent(CHAT_BACKGROUND_ANIMATION_CHANGE_EVENT, {
        detail: { enabled },
      }),
    );
  } catch {
    // CustomEvent can be unavailable in a non-browser test host.  The storage
    // write itself remains the source of truth in that case.
  }
  return saved;
}

export function useChatBackgroundPreference() {
  // null is the deliberate pre-hydration state.  Consumers should keep the
  // canvas hidden until this becomes a boolean to avoid a one-frame ON flash
  // when a stored false value is present.
  const [enabled, setEnabled] = useState<ChatBackgroundAnimationPreference>(
    null,
  );
  const [hydrated, setHydrated] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- localStorage is intentionally hydrated after the SSR-safe null render.
    setEnabled(readChatBackgroundAnimationPreference());
    setHydrated(true);

    const handleStorage = (event: StorageEvent) => {
      if (event.key !== CHAT_BACKGROUND_ANIMATION_STORAGE_KEY) return;
      setEnabled(
        normalizeChatBackgroundAnimationPreference(event.newValue) ?? true,
      );
      setSaveError(null);
    };
    const handleLocalChange = (event: Event) => {
      const detail = (event as CustomEvent<{ enabled?: unknown }>).detail;
      if (typeof detail?.enabled !== "boolean") return;
      setEnabled(detail.enabled);
      setSaveError(null);
    };

    window.addEventListener("storage", handleStorage);
    window.addEventListener(
      CHAT_BACKGROUND_ANIMATION_CHANGE_EVENT,
      handleLocalChange,
    );
    return () => {
      window.removeEventListener("storage", handleStorage);
      window.removeEventListener(
        CHAT_BACKGROUND_ANIMATION_CHANGE_EVENT,
        handleLocalChange,
      );
    };
  }, []);

  const setPreference = useCallback((nextEnabled: boolean) => {
    // Reflect the requested presentation immediately, even if persistence is
    // unavailable.  The error remains visible to the settings surface so a
    // failed write is never reported as a successful save.
    setEnabled(nextEnabled);
    setSaveError(null);
    if (!saveChatBackgroundAnimationPreference(nextEnabled)) {
      setSaveError("チャット背景アニメーション設定を保存できませんでした。");
    }
  }, []);

  return {
    enabled,
    hydrated,
    saveError,
    setEnabled: setPreference,
  };
}
