"use client";

import { useCallback, useEffect, useRef, useState } from "react";

export type DocsSearchHit = {
  id: string;
  title: string;
  aliases?: string[];
  node_type?: string;
  tags?: string[];
  breadcrumb?: string[];
  snippet?: string;
};

type SearchResult = {
  query: string;
  nodes: DocsSearchHit[];
  nextCursor: string | null;
  loading: boolean;
  error: boolean;
};

const emptyResult = (): SearchResult => ({
  query: "",
  nodes: [],
  nextCursor: null,
  loading: false,
  error: false,
});

/** Every query/close/scope change cancels the entire paging session. */
export function useDocsPaletteSearch(query: string, enabled: boolean) {
  const [result, setResult] = useState<SearchResult>(emptyResult);
  const sessionRef = useRef<{
    controller: AbortController;
    query: string;
    loading: boolean;
  } | null>(null);
  const timerRef = useRef<number | null>(null);
  const trimmedQuery = query.trim();

  const cancel = useCallback(() => {
    sessionRef.current?.controller.abort();
    sessionRef.current = null;
    if (timerRef.current !== null) window.clearTimeout(timerRef.current);
    timerRef.current = null;
  }, []);

  const reset = useCallback(() => {
    cancel();
    setResult(emptyResult());
  }, [cancel]);

  const requestPage = useCallback(async (cursor: string | null) => {
    const session = sessionRef.current;
    if (!session || session.controller.signal.aborted || session.loading) return;
    session.loading = true;
    setResult((current) => ({
      ...(cursor && current.query === session.query ? current : emptyResult()),
      query: session.query,
      loading: true,
      error: false,
    }));
    try {
      const params = new URLSearchParams({ q: session.query, limit: "30" });
      if (cursor) params.set("cursor", cursor);
      const response = await fetch(`/api/docs/search?${params.toString()}`, {
        signal: session.controller.signal,
      });
      if (!response.ok) throw new Error("Docs search failed");
      const data = await response.json() as {
        results?: DocsSearchHit[];
        nodes?: DocsSearchHit[];
        next_cursor?: string | null;
      };
      if (session.controller.signal.aborted || sessionRef.current !== session) return;
      setResult((current) => {
        const previous = cursor && current.query === session.query ? current.nodes : [];
        const seen = new Set(previous.map((node) => node.id));
        const nodes = [...previous];
        for (const node of data.results ?? data.nodes ?? []) {
          if (seen.has(node.id)) continue;
          seen.add(node.id);
          nodes.push(node);
        }
        return {
          query: session.query,
          nodes,
          // A repeated cursor must not allow an endless fetch loop.
          nextCursor: data.next_cursor && data.next_cursor !== cursor ? data.next_cursor : null,
          loading: false,
          error: false,
        };
      });
    } catch {
      if (session.controller.signal.aborted || sessionRef.current !== session) return;
      setResult((current) => ({ ...current, loading: false, error: true }));
    } finally {
      session.loading = false;
    }
  }, []);

  useEffect(() => {
    cancel();
    if (!enabled || !trimmedQuery) return;
    sessionRef.current = {
      controller: new AbortController(),
      query: trimmedQuery,
      loading: false,
    };
    timerRef.current = window.setTimeout(() => {
      timerRef.current = null;
      void requestPage(null);
    }, 80);
    return cancel;
  }, [cancel, enabled, query, requestPage, trimmedQuery]);

  const current = enabled && result.query === trimmedQuery ? result : emptyResult();
  return {
    ...current,
    reset,
    loadMore: () => {
      if (current.nextCursor) void requestPage(current.nextCursor);
    },
    retry: () => void requestPage(current.nextCursor),
  };
}
