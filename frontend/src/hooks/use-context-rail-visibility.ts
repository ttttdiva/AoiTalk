"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  safeLocalStorageGetItem,
  safeLocalStorageSetItem,
} from "@/lib/safe-storage";

export const CONTEXT_RAIL_VISIBILITY_STORAGE_PREFIX =
  "aoitalk:context-rail-visible:";

export function getContextRailVisibilityStorageKey(userId: string): string {
  return `${CONTEXT_RAIL_VISIBILITY_STORAGE_PREFIX}${userId}`;
}

export function parseContextRailVisibility(value: string | null): boolean | undefined {
  if (value === "true") return true;
  if (value === "false") return false;
  return undefined;
}

/**
 * Keep the persistent chat rail preference scoped to the authenticated user.
 *
 * The shell renders the rail by default until this hook hydrates the current
 * user's value. A user change first resets the effective value to the default
 * and only then applies the new user's value, so a previous user's preference
 * cannot leak across an auth transition. Storage failures intentionally keep
 * the default and do not affect the rail UI.
 */
export function useContextRailVisibility(userId: string | null | undefined): {
  visible: boolean;
  hydrated: boolean;
  toggle: () => void;
} {
  const storageKey = useMemo(
    () => (userId ? getContextRailVisibilityStorageKey(userId) : null),
    [userId],
  );
  const [visible, setVisible] = useState(true);
  const [hydratedStorageKey, setHydratedStorageKey] = useState<string | null>(null);
  const visibleRef = useRef(visible);
  useEffect(() => {
    visibleRef.current = visible;
  }, [visible]);

  useEffect(() => {
    let active = true;
    const stored = storageKey
      ? parseContextRailVisibility(safeLocalStorageGetItem(storageKey))
      : undefined;
    if (active) {
      // The default `visible` value is exposed while this client-only
      // hydration runs. The effective value is derived from the matching key,
      // so a previous user's preference cannot leak during the transition.
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setVisible(stored ?? true);
      setHydratedStorageKey(storageKey);
    }

    return () => {
      active = false;
    };
  }, [storageKey]);

  const hydrated = hydratedStorageKey === storageKey;
  const toggle = useCallback(() => {
    // A click that races auth/storage hydration must not overwrite the new
    // user's value with the pre-hydration default.
    if (!hydrated) return;
    const next = !visibleRef.current;
    visibleRef.current = next;
    setVisible(next);
    if (storageKey) safeLocalStorageSetItem(storageKey, String(next));
  }, [hydrated, storageKey]);

  return {
    // During a user transition, the default is the safe visual fallback while
    // the registration waits for the matching user's preference.
    visible: hydrated ? visible : true,
    hydrated,
    toggle,
  };
}
