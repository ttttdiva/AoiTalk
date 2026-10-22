"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { MediaUnifiedSearchResponse } from "@/lib/media-search-api";
import {
  FILES_SEMANTIC_SEARCH_LIMIT,
  filesIndexApi,
  judgeFilesSemanticOutcome,
  searchFilesSemantic,
  type FilesIndexStatus,
  type FilesSemanticOutcome,
  type FilesSemanticResult,
  type FilesSemanticScope,
} from "@/lib/explorer/files-semantic-search";

/** 検索パネル表示中に索引状況を更新する間隔。 */
const STATUS_POLL_MS = 10_000;

export function filesSemanticScopeKey(scope: FilesSemanticScope | null): string | null {
  return scope ? `${scope.scope}:${scope.projectId ?? ""}:${scope.rootPath}` : null;
}

export type FilesSemanticSearchState =
  | { phase: "idle" }
  | { phase: "loading"; query: string; scopeKey: string }
  | {
      phase: "ready";
      query: string;
      scopeKey: string;
      underFolder: boolean;
      folder: string;
      response: MediaUnifiedSearchResponse;
      results: FilesSemanticResult[];
    }
  | { phase: "error"; query: string; scopeKey: string; error: string };

interface StatusState {
  key: string | null;
  data: FilesIndexStatus | null;
  error: string | null;
  loading: boolean;
}

/**
 * Files 画面の内容（意味）検索と索引状況。
 *
 * 結果はフォルダ移動（「Filesで場所を開く」）後も保持し、タブ（User/Project）
 * やプロジェクトが変わったら破棄する。索引状況は panel 表示中だけ取得し、
 * アイドル中も定期的に更新し、外部更新や再スキャンの開始を取り込む。
 */
export function useFilesSemanticSearch(scope: FilesSemanticScope | null, active: boolean) {
  const scopeKey = filesSemanticScopeKey(scope);
  const scopeRef = useRef(scope);
  useEffect(() => {
    scopeRef.current = scope;
  }, [scope]);

  const [status, setStatus] = useState<StatusState>({ key: null, data: null, error: null, loading: false });
  const statusControllerRef = useRef<AbortController | null>(null);

  const refreshStatus = useCallback(async () => {
    const current = scopeRef.current;
    const key = filesSemanticScopeKey(current);
    statusControllerRef.current?.abort();
    if (!current || !key) {
      setStatus({ key: null, data: null, error: null, loading: false });
      return null;
    }
    const controller = new AbortController();
    statusControllerRef.current = controller;
    setStatus((prev) => ({ key, data: prev.key === key ? prev.data : null, error: null, loading: true }));
    try {
      const data = await filesIndexApi.status(current, controller.signal);
      if (controller.signal.aborted) return null;
      setStatus({ key, data, error: null, loading: false });
      return data;
    } catch (error) {
      if (controller.signal.aborted) return null;
      setStatus((prev) => ({
        key,
        data: prev.key === key ? prev.data : null,
        error: error instanceof Error ? error.message : "索引状況を取得できません",
        loading: false,
      }));
      return null;
    }
  }, []);

  useEffect(() => {
    if (!active || !scopeKey) return;
    void refreshStatus();
    return () => statusControllerRef.current?.abort();
  }, [active, refreshStatus, scopeKey]);

  const currentStatus = status.key === scopeKey ? status.data : null;
  useEffect(() => {
    if (!active || !scopeKey) return;
    const timer = window.setInterval(() => void refreshStatus(), STATUS_POLL_MS);
    return () => window.clearInterval(timer);
  }, [active, refreshStatus, scopeKey]);

  const [search, setSearch] = useState<FilesSemanticSearchState>({ phase: "idle" });
  const searchControllerRef = useRef<AbortController | null>(null);

  const reset = useCallback(() => {
    searchControllerRef.current?.abort();
    searchControllerRef.current = null;
    setSearch({ phase: "idle" });
  }, []);

  // タブ・プロジェクト・ユーザーが変わったら結果を破棄する。
  useEffect(() => {
    reset();
  }, [reset, scopeKey]);

  useEffect(() => () => {
    searchControllerRef.current?.abort();
    statusControllerRef.current?.abort();
  }, []);

  const run = useCallback(
    async (query: string, underFolder: boolean) => {
      const current = scopeRef.current;
      const key = filesSemanticScopeKey(current);
      const trimmed = query.trim();
      searchControllerRef.current?.abort();
      if (!current || !key || !trimmed) {
        setSearch({ phase: "idle" });
        return;
      }
      const controller = new AbortController();
      searchControllerRef.current = controller;
      const effectiveUnderFolder = underFolder && Boolean(current.relativeFolder);
      setSearch({ phase: "loading", query: trimmed, scopeKey: key });
      try {
        const [{ response, results }] = await Promise.all([
          searchFilesSemantic(trimmed, current, { underFolder: effectiveUnderFolder }, controller.signal),
          // 0件判定には検索時点の索引状況を使う。
          refreshStatus(),
        ]);
        if (controller.signal.aborted) return;
        setSearch({
          phase: "ready",
          query: trimmed,
          scopeKey: key,
          underFolder: effectiveUnderFolder,
          folder: effectiveUnderFolder ? current.relativeFolder : "",
          response,
          results,
        });
      } catch (error) {
        if (controller.signal.aborted) return;
        setSearch({
          phase: "error",
          query: trimmed,
          scopeKey: key,
          error: error instanceof Error ? error.message : "検索を利用できません",
        });
      }
    },
    [refreshStatus],
  );

  const outcome = useMemo<FilesSemanticOutcome | null>(() => {
    if (search.phase !== "ready") return null;
    return judgeFilesSemanticOutcome({
      status: status.key === search.scopeKey ? status.data : null,
      response: search.response,
      resultCount: search.results.length,
      limitedToTopResults: search.underFolder && search.response.results.length >= FILES_SEMANTIC_SEARCH_LIMIT,
    });
  }, [search, status]);

  return {
    status: currentStatus,
    statusError: status.key === scopeKey ? status.error : null,
    statusLoading: status.key === scopeKey && status.loading,
    refreshStatus,
    search,
    outcome,
    run,
    reset,
  };
}

export type FilesSemanticSearchController = ReturnType<typeof useFilesSemanticSearch>;
