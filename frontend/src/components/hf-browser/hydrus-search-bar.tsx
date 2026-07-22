"use client";

/**
 * Hydrus 検索バー: タグ/数値フィルタを入力して検索し、結果を ExplorerListResponse に
 * 変換して ExplorerContext にセットする。結果は既存の FileGrid/FileList で表示される。
 *
 * タグ構文 (参考リポジトリと同等):
 *   - "rating:like/1"    → 好評価1件以上
 *   - "system:filetype:image" 等のシステムタグも素通しで渡す
 *   - 数値フィルタ: "width>1000", "filesize<50MB" のような system: プリセットを
 *     UI 側でサジェストする（現状は入力のまま transmit）
 *
 * タグは空白で分割せず入力文字列をそのまま1タグとして扱う
 * （"system:import time: since 3 days ago" のようなシステムタグを壊さないため）。
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Search,
  X,
  Star,
  History,
  LayoutGrid,
  List,
  Trash2,
} from "lucide-react";
import {
  hydrusSearch,
  hydrusGetMetadata,
  type HydrusFileMetadata,
} from "@/lib/hf-api";
import type { ExplorerFile, ExplorerListResponse } from "@/lib/explorer-api";
import { buildHydrusPath } from "@/lib/hydrus/virtual-path";
import { useExplorer } from "@/contexts/explorer-context";
import { cn } from "@/lib/utils";
import {
  HYDRUS_DEFAULT_SEARCH_TAGS,
  clearTagHistory,
  pushTagHistory,
  readSearchTags,
  readTagBookmarks,
  readTagHistory,
  removeTagHistory,
  toggleTagBookmark,
  writeSearchTags,
} from "@/lib/hydrus/tag-store";
import { getOrCreatePagePromise } from "@/lib/hydrus/page-cache";

/**
 * Hydrus Client API の file_sort_type。2 = import time。
 * file_sort_asc=false でインポート日時の新しい順（降順）。
 * 検索時に Hydrus 側へ渡すことで、ページネーションを含む全件が
 * インポート日時順になる（1ページ分だけをフロントで並べ替えない）。
 */
const HYDRUS_SORT_IMPORT_TIME = 2;
const HYDRUS_SORT_ASC = false;

interface Props {
  onResults: (data: ExplorerListResponse) => void;
  onError: (msg: string) => void;
  onPagingChange?: (controller: HydrusPagingController | null) => void;
}

export interface HydrusPageResult {
  queryKey: string;
  page: number;
  totalPages: number;
  total: number;
  data: ExplorerListResponse;
}

export interface HydrusPagingController {
  page: number;
  totalPages: number;
  loadPage: (page: number) => Promise<HydrusPageResult | null>;
  prefetchPage: (page: number) => Promise<HydrusPageResult | null>;
}

function guessExt(mime?: string): string {
  if (!mime) return "";
  if (mime.startsWith("image/")) return "." + mime.slice(6);
  if (mime.startsWith("video/")) return "." + mime.slice(6);
  if (mime.startsWith("audio/")) return "." + mime.slice(6);
  return "";
}

function metadataToFile(m: HydrusFileMetadata): ExplorerFile {
  const ext = guessExt(m.mime);
  return {
    name: (m.hash ? m.hash.slice(0, 12) : `file_${m.file_id}`) + ext,
    path: buildHydrusPath(m.file_id),
    type: m.mime || "application/octet-stream",
    size: m.size,
    modified_at: m.time_modified
      ? new Date(m.time_modified * 1000).toISOString()
      : undefined,
    extension: ext,
  };
}

const EMPTY_RESULT: ExplorerListResponse = {
  success: true,
  current_path: "Hydrus",
  parent_path: null,
  can_go_up: false,
  directories: [],
  files: [],
  total_items: 0,
};

export function HydrusSearchBar({ onResults, onError, onPagingChange }: Props) {
  const { viewMode, setViewMode, isHydrusMode, currentPath } = useExplorer();
  const [tagInput, setTagInput] = useState("");
  const [tags, setTags] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [page, setPage] = useState(1);
  const [perPage] = useState(60);
  const [totalPages, setTotalPages] = useState(1);
  const [total, setTotal] = useState(0);
  const [history, setHistory] = useState<string[]>([]);
  const [bookmarks, setBookmarks] = useState<string[]>([]);
  const initializedRef = useRef(false);
  const tagsRef = useRef<string[]>([]);
  const generationRef = useRef(0);
  const activeAbortRef = useRef<AbortController | null>(null);
  const pagingAbortRef = useRef(new AbortController());
  const pageCacheRef = useRef(new Map<string, Promise<HydrusPageResult>>());

  const queryKey = useCallback(
    (targetTags: string[]) => JSON.stringify({ tags: targetTags, perPage }),
    [perPage],
  );

  const fetchPage = useCallback(
    async (
      targetTags: string[],
      targetPage: number,
      signal?: AbortSignal,
    ): Promise<HydrusPageResult> => {
      const searchResp = await hydrusSearch({
        tags: targetTags,
        page: targetPage,
        perPage,
        fileSortType: HYDRUS_SORT_IMPORT_TIME,
        fileSortAsc: HYDRUS_SORT_ASC,
        signal,
      });
      const ids = searchResp.file_ids || [];
      let files: ExplorerFile[] = [];
      if (ids.length > 0) {
        const meta = await hydrusGetMetadata(ids, true, signal);
        const metaById = new Map(meta.metadata.map((item) => [item.file_id, item]));
        files = ids
          .map((id) => metaById.get(id))
          .filter((item): item is HydrusFileMetadata => item != null)
          .map(metadataToFile);
      }
      return {
        queryKey: queryKey(targetTags),
        page: targetPage,
        totalPages: searchResp.total_pages || 1,
        total: searchResp.total || 0,
        data: {
          success: true,
          current_path: "Hydrus",
          parent_path: null,
          can_go_up: false,
          directories: [],
          files,
          total_items: files.length,
        },
      };
    },
    [perPage, queryKey],
  );

  const cachedPage = useCallback(
    (targetTags: string[], targetPage: number) => {
      const key = `${queryKey(targetTags)}:${targetPage}`;
      return getOrCreatePagePromise(pageCacheRef.current, key, () =>
        fetchPage(targetTags, targetPage, pagingAbortRef.current.signal),
      );
    },
    [fetchPage, queryKey],
  );

  const activatePage = useCallback(
    (result: HydrusPageResult, targetTags: string[]) => {
      if (result.queryKey !== queryKey(tagsRef.current)) return false;
      setPage(result.page);
      setTotalPages(result.totalPages);
      setTotal(result.total);
      setHistory(pushTagHistory(targetTags));
      onResults(result.data);
      return true;
    },
    [onResults, queryKey],
  );

  const runSearch = useCallback(
    async (targetTags: string[], targetPage: number) => {
      const previousQuery = queryKey(tagsRef.current);
      const nextQuery = queryKey(targetTags);
      tagsRef.current = targetTags;
      if (previousQuery !== nextQuery) pageCacheRef.current.clear();
      pagingAbortRef.current.abort();
      pagingAbortRef.current = new AbortController();
      if (targetTags.length === 0) {
        activeAbortRef.current?.abort();
        generationRef.current += 1;
        onResults(EMPTY_RESULT);
        setTotal(0);
        setTotalPages(1);
        setPage(1);
        return;
      }
      activeAbortRef.current?.abort();
      const abort = new AbortController();
      activeAbortRef.current = abort;
      const generation = ++generationRef.current;
      setLoading(true);
      try {
        const result = await fetchPage(targetTags, targetPage, abort.signal);
        if (generation !== generationRef.current || abort.signal.aborted) return;
        pageCacheRef.current.set(`${result.queryKey}:${targetPage}`, Promise.resolve(result));
        activatePage(result, targetTags);
      } catch (e) {
        if (abort.signal.aborted) return;
        onError(`Hydrus 検索失敗: ${String(e)}`);
      } finally {
        if (generation === generationRef.current) setLoading(false);
      }
    },
    [activatePage, fetchPage, onError, onResults, queryKey],
  );

  const loadPage = useCallback(
    async (targetPage: number, activate: boolean) => {
      const snapshot = [...tagsRef.current];
      if (targetPage < 1 || snapshot.length === 0) return null;
      const generation = generationRef.current;
      const signal = pagingAbortRef.current.signal;
      const result = await cachedPage(snapshot, targetPage);
      if (
        signal.aborted ||
        generation !== generationRef.current ||
        result.queryKey !== queryKey(tagsRef.current)
      ) return null;
      if (targetPage > result.totalPages) return null;
      if (activate) activatePage(result, snapshot);
      return result;
    },
    [activatePage, cachedPage, queryKey],
  );

  useEffect(() => {
    onPagingChange?.({
      page,
      totalPages,
      loadPage: (targetPage) => loadPage(targetPage, true),
      prefetchPage: (targetPage) => loadPage(targetPage, false),
    });
    return () => onPagingChange?.(null);
  }, [loadPage, onPagingChange, page, totalPages]);

  useEffect(() => {
    const pageCache = pageCacheRef.current;
    if (pagingAbortRef.current.signal.aborted) {
      pagingAbortRef.current = new AbortController();
    }
    return () => {
      initializedRef.current = false;
      generationRef.current += 1;
      activeAbortRef.current?.abort();
      pagingAbortRef.current.abort();
      pageCache.clear();
    };
  }, []);

  // 初回のみ: 保存済みの検索条件があれば復元し、
  // 無ければ初期条件（直近3日のインポート）で自動検索する。
  //
  // ExplorerContext は Hydrus タブ初期化時に browseData を空へリセットするが、
  // その初期化は非同期のため、先に検索結果を流し込むと後から空で上書きされる。
  // currentPath が "Hydrus" になる（＝リセット完了）まで待ってから検索する。
  useEffect(() => {
    if (initializedRef.current) return;
    if (!isHydrusMode || currentPath !== "Hydrus") return;
    initializedRef.current = true;
    setHistory(readTagHistory());
    setBookmarks(readTagBookmarks());
    const saved = readSearchTags();
    const initial = saved ?? HYDRUS_DEFAULT_SEARCH_TAGS;
    setTags(initial);
    if (saved === null) writeSearchTags(initial);
    void runSearch(initial, 1);
  }, [isHydrusMode, currentPath, runSearch]);

  const applyTags = useCallback(
    (next: string[]) => {
      setTags(next);
      writeSearchTags(next);
      setPage(1);
      void runSearch(next, 1);
    },
    [runSearch],
  );

  const handleAddTag = () => {
    const t = tagInput.trim();
    if (!t) return;
    setTagInput("");
    if (tags.includes(t)) return;
    applyTags([...tags, t]);
  };

  const handleRemoveTag = (idx: number) => {
    applyTags(tags.filter((_, i) => i !== idx));
  };

  /** 履歴/ブックマークからの1クリック追加。既存タグは重複追加しない。 */
  const handleUseTag = (tag: string) => {
    if (tags.includes(tag)) return;
    applyTags([...tags, tag]);
  };

  const handleToggleBookmark = (tag: string) => {
    setBookmarks(toggleTagBookmark(tag));
  };

  const handleRemoveHistory = (tag: string) => {
    setHistory(removeTagHistory(tag));
  };

  const handleClearHistory = () => {
    setHistory(clearTagHistory());
  };

  const handleKey = (e: React.KeyboardEvent) => {
    if (e.key === "Enter") {
      e.preventDefault();
      handleAddTag();
    }
  };

  const goPage = (p: number) => {
    if (p < 1 || p > totalPages) return;
    void runSearch(tags, p);
  };

  return (
    <div className="flex flex-col gap-2 rounded-md border bg-muted/20 p-2">
      <div className="flex items-center gap-2">
        <Search className="size-4 text-muted-foreground" />
        <Input
          value={tagInput}
          onChange={(e) => setTagInput(e.target.value)}
          onKeyDown={handleKey}
          placeholder="タグ追加（例: rating:like/1, system:filetype:image, width>1000）Enterで追加"
          className="h-8"
        />
        <Button size="sm" onClick={handleAddTag} disabled={!tagInput.trim()}>
          追加
        </Button>
        <div className="flex items-center rounded-md border">
          <Button
            size="icon-sm"
            variant={viewMode === "grid" ? "secondary" : "ghost"}
            onClick={() => setViewMode("grid")}
            title="サムネイル表示 ( : )"
            aria-label="サムネイル表示"
            aria-pressed={viewMode === "grid"}
          >
            <LayoutGrid className="size-4" />
          </Button>
          <Button
            size="icon-sm"
            variant={viewMode === "list" ? "secondary" : "ghost"}
            onClick={() => setViewMode("list")}
            title="リスト表示 ( ; )"
            aria-label="リスト表示"
            aria-pressed={viewMode === "list"}
          >
            <List className="size-4" />
          </Button>
        </div>
      </div>

      {/* 現在の検索条件 */}
      {tags.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {tags.map((t, i) => (
            <span
              key={`${t}-${i}`}
              className="inline-flex items-center gap-1 rounded bg-primary/10 px-2 py-0.5 text-xs"
            >
              <button
                className="hover:text-amber-500"
                onClick={() => handleToggleBookmark(t)}
                title={
                  bookmarks.includes(t)
                    ? "ブックマーク解除"
                    : "ブックマークに追加"
                }
                aria-label={
                  bookmarks.includes(t)
                    ? "ブックマーク解除"
                    : "ブックマークに追加"
                }
              >
                <Star
                  className={cn(
                    "size-3",
                    bookmarks.includes(t) && "fill-amber-400 text-amber-400",
                  )}
                />
              </button>
              {t}
              <button
                className="hover:text-destructive"
                onClick={() => handleRemoveTag(i)}
                title="検索条件から削除"
                aria-label="検索条件から削除"
              >
                <X className="size-3" />
              </button>
            </span>
          ))}
        </div>
      )}

      {/* ブックマーク */}
      {bookmarks.length > 0 && (
        <div className="flex flex-wrap items-center gap-1">
          <span className="inline-flex items-center gap-1 text-[10px] text-muted-foreground">
            <Star className="size-3" />
            ブックマーク
          </span>
          {bookmarks.map((t) => (
            <span
              key={`bm-${t}`}
              className="inline-flex items-center gap-1 rounded bg-amber-500/10 px-2 py-0.5 text-xs"
            >
              <button
                className="hover:underline disabled:opacity-50 disabled:no-underline"
                onClick={() => handleUseTag(t)}
                disabled={tags.includes(t)}
                title={
                  tags.includes(t) ? "既に検索条件にあります" : "検索条件へ追加"
                }
              >
                {t}
              </button>
              <button
                className="hover:text-destructive"
                onClick={() => handleToggleBookmark(t)}
                title="ブックマーク解除"
                aria-label="ブックマーク解除"
              >
                <X className="size-3" />
              </button>
            </span>
          ))}
        </div>
      )}

      {/* 使用履歴 */}
      {history.length > 0 && (
        <div className="flex flex-wrap items-center gap-1">
          <span className="inline-flex items-center gap-1 text-[10px] text-muted-foreground">
            <History className="size-3" />
            使用履歴
          </span>
          {history.map((t) => (
            <span
              key={`hist-${t}`}
              className="inline-flex items-center gap-1 rounded bg-muted px-2 py-0.5 text-xs"
            >
              <button
                className="hover:text-amber-500"
                onClick={() => handleToggleBookmark(t)}
                title={
                  bookmarks.includes(t)
                    ? "ブックマーク解除"
                    : "ブックマークに追加"
                }
                aria-label={
                  bookmarks.includes(t)
                    ? "ブックマーク解除"
                    : "ブックマークに追加"
                }
              >
                <Star
                  className={cn(
                    "size-3",
                    bookmarks.includes(t) && "fill-amber-400 text-amber-400",
                  )}
                />
              </button>
              <button
                className="hover:underline disabled:opacity-50 disabled:no-underline"
                onClick={() => handleUseTag(t)}
                disabled={tags.includes(t)}
                title={
                  tags.includes(t) ? "既に検索条件にあります" : "検索条件へ追加"
                }
              >
                {t}
              </button>
              <button
                className="hover:text-destructive"
                onClick={() => handleRemoveHistory(t)}
                title="履歴から削除"
                aria-label="履歴から削除"
              >
                <X className="size-3" />
              </button>
            </span>
          ))}
          <Button
            size="xs"
            variant="ghost"
            onClick={handleClearHistory}
            title="履歴をすべて削除"
          >
            <Trash2 className="size-3" />
            クリア
          </Button>
        </div>
      )}

      {(loading || total > 0) && (
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          {loading ? (
            <span>検索中...</span>
          ) : (
            <>
              <span>
                {total}件 / {page}/{totalPages}ページ（インポート日時の新しい順）
              </span>
              <Button
                size="xs"
                variant="ghost"
                onClick={() => goPage(page - 1)}
                disabled={page <= 1}
              >
                前
              </Button>
              <Button
                size="xs"
                variant="ghost"
                onClick={() => goPage(page + 1)}
                disabled={page >= totalPages}
              >
                次
              </Button>
            </>
          )}
        </div>
      )}
    </div>
  );
}
