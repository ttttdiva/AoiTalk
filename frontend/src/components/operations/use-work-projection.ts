"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  fetchWorkProjection,
  mergeProjection,
  WorkProjectionError,
  type WorkProjection,
  type WorkScope,
} from "@/lib/operations-work-projection";

type State = {
  data: WorkProjection | null;
  loading: boolean;
  paging: boolean;
  error: string | null;
  stale: boolean;
};
const initial: State = {
  data: null,
  loading: true,
  paging: false,
  error: null,
  stale: false,
};

/** Mounted once per exact server scope. Old requests cannot publish into a new scope. */
export function useWorkProjection(scope: WorkScope) {
  const [state, setState] = useState<State>(initial);
  const request = useRef(0);
  const controller = useRef<AbortController | null>(null);
  const mounted = useRef(true);
  const paging = useRef(false);
  const data = useRef<WorkProjection | null>(null);
  const pagesLoaded = useRef(1);
  const scopeKey = JSON.stringify(scope);

  const load = useCallback(
    async (append = false) => {
      if (append && (paging.current || !data.current?.page.next_cursor)) return;
      const requestedPages = append ? pagesLoaded.current + 1 : 1;
      const generation = ++request.current;
      controller.current?.abort();
      const nextController = new AbortController();
      controller.current = nextController;
      paging.current = append;

      setState((value) => ({
        ...value,
        loading: !append,
        paging: append,
        error: null,
      }));
      try {
        const current = () =>
          mounted.current &&
          !nextController.signal.aborted &&
          generation === request.current;
        // Every continuation revalidates the displayed page window. A cursor is
        // not an authorization grant for previously displayed business records.
        let result = await fetchWorkProjection(
          JSON.parse(scopeKey) as WorkScope,
          nextController.signal,
        );
        if (!current()) return;
        let fetchedPages = 1;
        const seen = new Set<string>();
        // Once the first page was successfully reauthorized, never fall back
        // to the older window, even if later pages fail or the total is unchanged.
        data.current = result;
        pagesLoaded.current = 1;
        setState((value) => ({ ...value, data: result, stale: false }));
        while (fetchedPages < requestedPages && result.page.has_more) {
          const cursor = result.page.next_cursor!;
          if (seen.has(cursor))
            throw new Error(
              "追加取得の位置が更新されませんでした。更新して取り直してください。",
            );
          seen.add(cursor);
          const next = await fetchWorkProjection(
            JSON.parse(scopeKey) as WorkScope,
            nextController.signal,
            cursor,
          );
          if (!current()) return;
          if (next.page.available !== result.page.available) {
            data.current = null;
            setState((value) => ({ ...value, data: null, stale: false }));
            throw new Error(
              "追加取得中に閲覧範囲が変わりました。更新して取り直してください。",
            );
          }
          if (next.page.next_cursor && seen.has(next.page.next_cursor))
            throw new Error(
              "追加取得の位置が更新されませんでした。更新して取り直してください。",
            );
          result = mergeProjection(result, next);
          fetchedPages += 1;
          data.current = result;
          pagesLoaded.current = fetchedPages;
          setState((value) => ({ ...value, data: result, stale: false }));
        }
        pagesLoaded.current = fetchedPages;
        data.current = result;
        setState({
          data: result,
          loading: false,
          paging: false,
          error: null,
          stale: false,
        });
      } catch (error) {
        if (
          !mounted.current ||
          nextController.signal.aborted ||
          generation !== request.current
        )
          return;
        const forbidden =
          error instanceof WorkProjectionError &&
          [401, 403].includes(error.status);
        if (forbidden) data.current = null;
        setState((value) => ({
          ...value,
          data: forbidden ? null : value.data,
          loading: false,
          paging: false,
          stale: !forbidden && Boolean(value.data),
          error:
            error instanceof WorkProjectionError
              ? error.message
              : error instanceof Error && error.message.startsWith("追加取得")
                ? error.message
                : "仕事の情報を取得・検証できませんでした。更新して再試行してください。",
        }));
      } finally {
        if (generation === request.current) paging.current = false;
      }
    },
    [scopeKey],
  );
  useEffect(() => {
    mounted.current = true;
    void load();
    return () => {
      mounted.current = false;
      request.current += 1;
      controller.current?.abort();
    };
  }, [load]);
  return {
    ...state,
    refresh: () => void load(),
    loadMore: () => void load(true),
  };
}
