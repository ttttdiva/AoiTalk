"use client";

import {
  createContext,
  useContext,
  useState,
  useEffect,
  useCallback,
} from "react";
import { getSnippets, saveSnippets, type Snippet } from "@/lib/snippets-api";

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

export function SnippetsProvider({ children }: { children: React.ReactNode }) {
  const [snippets, setSnippets] = useState<Snippet[]>([]);

  const refresh = useCallback(() => {
    getSnippets()
      .then(setSnippets)
      .catch(() => {});
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const save = useCallback(async (newSnippets: Snippet[]) => {
    await saveSnippets(newSnippets);
    setSnippets(newSnippets);
  }, []);

  return (
    <SnippetsContext.Provider value={{ snippets, refresh, save }}>
      {children}
    </SnippetsContext.Provider>
  );
}
