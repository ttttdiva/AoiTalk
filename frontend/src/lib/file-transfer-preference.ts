import { useCallback, useEffect, useState } from "react";
import {
  safeLocalStorageGetItem,
  safeLocalStorageSetItem,
} from "@/lib/safe-storage";

/**
 * File transfer mode is a browser-local preference. Keep the key independent
 * from the signed-in account so the same browser keeps the selected mode.
 */
export const FILE_TRANSFER_COMPATIBILITY_STORAGE_KEY =
  "aoitalk-file-transfer-compatibility-mode";

export const FILE_TRANSFER_COMPATIBILITY_CHANGE_EVENT =
  "aoitalk:file-transfer-compatibility-change";

export type FileTransferCompatibilityPreference = boolean | null;

// A storage write can fail after the user has selected a new value. Keep the
// requested value in this document so React and non-React transfer callers do
// not disagree about the effective mode. It intentionally disappears on a
// full page reload because it is not durable state.
let volatilePreferenceOverride: boolean | null = null;
let storageListenerAttached = false;

function handleStoragePreferenceChange(event: StorageEvent) {
  if (event.key !== FILE_TRANSFER_COMPATIBILITY_STORAGE_KEY) return;
  volatilePreferenceOverride = null;
}

function ensureStoragePreferenceListener() {
  if (storageListenerAttached || typeof window === "undefined") return;
  window.addEventListener("storage", handleStoragePreferenceChange);
  storageListenerAttached = true;
}

export function normalizeFileTransferCompatibilityPreference(
  value: string | null | undefined,
): boolean | null {
  if (value === "true" || value === "1" || value === "on") return true;
  if (value === "false" || value === "0" || value === "off") return false;
  return null;
}

/** Read the persisted mode for non-React consumers such as the transfer worker. */
export function readFileTransferCompatibilityPreference(): boolean {
  ensureStoragePreferenceListener();
  if (volatilePreferenceOverride !== null) {
    return volatilePreferenceOverride;
  }
  return (
    normalizeFileTransferCompatibilityPreference(
      safeLocalStorageGetItem(FILE_TRANSFER_COMPATIBILITY_STORAGE_KEY),
    ) ?? true
  );
}

export function saveFileTransferCompatibilityPreference(enabled: boolean): boolean {
  ensureStoragePreferenceListener();
  const saved = safeLocalStorageSetItem(
    FILE_TRANSFER_COMPATIBILITY_STORAGE_KEY,
    enabled ? "true" : "false",
  );
  if (!saved) {
    if (typeof window !== "undefined") volatilePreferenceOverride = enabled;
    return false;
  }
  volatilePreferenceOverride = null;
  if (typeof window === "undefined") return true;

  // Storage events do not reach the document that performed the write.
  // Notify other hooks in this tab as well as listeners in other tabs.
  try {
    window.dispatchEvent(
      new CustomEvent(FILE_TRANSFER_COMPATIBILITY_CHANGE_EVENT, {
        detail: { enabled },
      }),
    );
  } catch {
    // Keep the successful storage write as the source of truth in test hosts
    // without CustomEvent support.
  }
  return saved;
}

export function useFileTransferCompatibilityPreference() {
  const [enabled, setEnabledState] =
    useState<FileTransferCompatibilityPreference>(null);
  const [hydrated, setHydrated] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- localStorage is intentionally hydrated after the SSR-safe null render.
    setEnabledState(readFileTransferCompatibilityPreference());
    setHydrated(true);

    const handleStorage = (event: StorageEvent) => {
      if (event.key !== FILE_TRANSFER_COMPATIBILITY_STORAGE_KEY) return;
      volatilePreferenceOverride = null;
      setEnabledState(
        normalizeFileTransferCompatibilityPreference(event.newValue) ?? true,
      );
      setSaveError(null);
    };
    const handleLocalChange = (event: Event) => {
      const detail = (event as CustomEvent<{ enabled?: unknown }>).detail;
      if (typeof detail?.enabled !== "boolean") return;
      setEnabledState(detail.enabled);
      setSaveError(null);
    };

    window.addEventListener("storage", handleStorage);
    window.addEventListener(
      FILE_TRANSFER_COMPATIBILITY_CHANGE_EVENT,
      handleLocalChange,
    );
    return () => {
      window.removeEventListener("storage", handleStorage);
      window.removeEventListener(
        FILE_TRANSFER_COMPATIBILITY_CHANGE_EVENT,
        handleLocalChange,
      );
    };
  }, []);

  const setEnabled = useCallback((nextEnabled: boolean) => {
    // Keep the current tab responsive even when storage is unavailable. The
    // error makes the failed persistence visible in the settings card.
    setEnabledState(nextEnabled);
    setSaveError(null);
    if (!saveFileTransferCompatibilityPreference(nextEnabled)) {
      setSaveError("転送互換モードを保存できませんでした。");
    }
  }, []);

  return {
    enabled,
    hydrated,
    saveError,
    setEnabled,
  };
}
