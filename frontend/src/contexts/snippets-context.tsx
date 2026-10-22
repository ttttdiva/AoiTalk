"use client";

import {
  createContext,
  useContext,
  useCallback,
  useMemo,
} from "react";
import type { Snippet } from "@/lib/snippets-api";
import { useUserSettings } from "@/contexts/user-settings-context";

interface SnippetsContextType {
  snippets: Snippet[];
  refresh: () => void;
  save: (snippets: Snippet[]) => Promise<void>;
}

const SnippetsContext = createContext<SnippetsContextType>({
  snippets: [],
  refresh: () => {},
  save: async () => {},
});

export const useSnippets = () => useContext(SnippetsContext);

/**
 * Snippets live in the user's settings document. Read them from the enclosing
 * UserSettingsProvider instead of issuing a second GET /api/users/me/settings
 * on every app load, and save through the same provider so every settings
 * consumer sees the persisted snapshot.
 */
export function SnippetsProvider({ children }: { children: React.ReactNode }) {
  const { settings, refresh, patch } = useUserSettings();
  const stored = settings.snippets;
  const snippets = useMemo<Snippet[]>(
    () => (Array.isArray(stored) ? (stored as Snippet[]) : []),
    [stored],
  );

  const save = useCallback(
    async (newSnippets: Snippet[]) => {
      try {
        await patch({ snippets: newSnippets });
      } catch (error) {
        const status = (error as { status?: unknown } | null)?.status;
        throw new Error(
          typeof status === "number"
            ? `スニペットの保存に失敗しました (${status})`
            : "スニペットの保存に失敗しました",
          { cause: error },
        );
      }
    },
    [patch],
  );

  const value = useMemo(
    () => ({ snippets, refresh, save }),
    [snippets, refresh, save],
  );

  return (
    <SnippetsContext.Provider value={value}>
      {children}
    </SnippetsContext.Provider>
  );
}
