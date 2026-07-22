"use client";

import {
  createContext,
  useContext,
  useState,
  useCallback,
  useEffect,
  useRef,
  useMemo,
} from "react";
import useSWR from "swr";
import {
  explorerList,
  explorerBookmarks,
  storageContexts,
  filerBrowse,
  type ExplorerListResponse,
  type ExplorerBookmark,
  type StorageContext,
} from "@/lib/explorer-api";
import { listRemoteWorkspaceFiles } from "@/lib/remote-servers";
import { useProject } from "@/contexts/project-context";
import { hfExplorerList } from "@/lib/hf/explorer-loader";
import { HF_PREFIX, isHfPath, parseHfPath } from "@/lib/hf/virtual-path";
import {
  fetchCreatorMapping,
  creatorMatchesQuery,
  type CreatorMapping,
} from "@/lib/hf/creator-mapping";
import { buildExplorerRangeSelection } from "@/lib/explorer-selection";
import {
  DEFAULT_SORT_DIR,
  DEFAULT_SORT_KEY,
  isSortDir,
  isSortKey,
  type SortDir,
  type SortKey,
} from "@/lib/explorer-sort";

export type ViewMode = "grid" | "list";
export type FilerTab = "workspace" | "user" | "hf" | "hydrus";

interface ClipboardState {
  paths: string[];
  operation: "copy" | "cut";
}

interface ExplorerContextType {
  // Navigation
  currentPath: string;
  navigate: (path: string) => void;
  goBack: () => void;
  goForward: () => void;
  goUp: () => void;
  refresh: () => Promise<void>;

  // Data
  browseData: ExplorerListResponse | null;
  setBrowseData: (data: ExplorerListResponse | null) => void;
  loading: boolean;
  error: string | null;

  // View
  viewMode: ViewMode;
  setViewMode: (mode: ViewMode) => void;

  // Sort（FileGrid / FileList / F8・F9 ショートカットで共有）
  sortKey: SortKey;
  sortDir: SortDir;
  setSort: (key: SortKey, dir: SortDir) => void;

  // Selection
  selectedItems: Set<string>;
  focusedItemPath: string | null;
  selectItem: (path: string) => void;
  toggleSelect: (path: string) => void;
  selectRange: (path: string, orderedPaths: string[], additive?: boolean) => void;
  selectAll: () => void;
  clearSelection: () => void;

  // Clipboard
  clipboard: ClipboardState | null;
  setClipboard: (cb: ClipboardState | null) => void;

  // Bookmarks
  bookmarks: ExplorerBookmark[];
  refreshBookmarks: () => void;

  // Filer tabs
  filerTab: FilerTab;
  setFilerTab: (tab: FilerTab) => void;
  contextRootPath: string;

  // Storage Context (legacy)
  storageCtx: StorageContext | null;
  storageCtxList: StorageContext[];
  isAdmin: boolean;

  isAbsoluteFilerPath: boolean;
  isHfMode: boolean;
  isHydrusMode: boolean;

  // Editor
  editingFile: { path: string; name: string; extension: string } | null;
  openEditor: (file: { path: string; name: string; type?: string }) => void;
  closeEditor: () => void;

  // HF creator_mapping.json 検索（60_huggingface-sync 互換のタグ/作者フィルタ）
  hfCreatorMapping: CreatorMapping | null;
  hfSearchQuery: string;
  setHfSearchQuery: (q: string) => void;
}

const ExplorerContext = createContext<ExplorerContextType | null>(null);
const EXPLORER_VIEW_MODE_STORAGE_KEY = "explorer-view-mode";
const EXPLORER_SORT_KEY_STORAGE_KEY = "explorer-sort-key";
const EXPLORER_SORT_DIR_STORAGE_KEY = "explorer-sort-dir";
const FILER_TAB_STORAGE_KEY = "filer-tab";
const FILER_PATH_STORAGE_PREFIX = "filer-last-path";

// ブックマーク一覧の SWR キャッシュキー。ファイラー全体で一意なので固定文字列を使う。
// 取得タイミングは従来どおり呼び出し側の refreshBookmarks（= 手動 revalidate）で駆動し、
// SWR の自動 revalidation は全て無効化して表示挙動を不変に保つ。
const BOOKMARKS_SWR_KEY = "explorer/bookmarks";
const EMPTY_BOOKMARKS: ExplorerBookmark[] = [];

export function useExplorer() {
  const ctx = useContext(ExplorerContext);
  if (!ctx) throw new Error("useExplorer must be used within ExplorerProvider");
  return ctx;
}

// 絶対パス閲覧判定: 絶対パス（D:\, /home/ 等）
function isAbsolutePath(p: string): boolean {
  if (!p) return false;
  if (/^[A-Za-z]:[\\/]/.test(p)) return true;
  if (p.startsWith("/")) return true;
  return false;
}

function readLocalStorage(key: string): string | null {
  if (typeof window === "undefined") return null;
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function writeLocalStorage(key: string, value: string): void {
  if (typeof window === "undefined") return;
  try {
    localStorage.setItem(key, value);
  } catch {
    /* ignore */
  }
}

function isFilerTab(value: string | null): value is FilerTab {
  return (
    value === "workspace" ||
    value === "user" ||
    value === "hf" ||
    value === "hydrus"
  );
}

function lastPathStorageKey(tab: FilerTab, ownerId?: string | null): string {
  if (tab === "workspace") {
    return `${FILER_PATH_STORAGE_PREFIX}:workspace:${ownerId || "default"}`;
  }
  if (tab === "user") {
    return `${FILER_PATH_STORAGE_PREFIX}:user:${ownerId || "default"}`;
  }
  return `${FILER_PATH_STORAGE_PREFIX}:${tab}`;
}

function readLastPath(tab: FilerTab, ownerId?: string | null): string | null {
  const path = readLocalStorage(lastPathStorageKey(tab, ownerId));
  return path && path.trim() ? path : null;
}

function writeLastPath(
  tab: FilerTab,
  path: string,
  ownerId?: string | null,
): void {
  if (tab === "hydrus") return;
  writeLocalStorage(lastPathStorageKey(tab, ownerId), path);
}

function workspaceRoot(projectId: string): string {
  return `_projects/project_${projectId}`;
}

function userRoot(id: string): string {
  return `_users/user_${id}`;
}

function remoteWorkspacePath(profileId: string, projectId: string, path = ""): string {
  const cleanPath = path.replace(/^\/+|\/+$/g, "");
  return `remote://${profileId}/${projectId}${cleanPath ? `/${cleanPath}` : ""}`;
}

function remoteWorkspaceRelativePath(path: string): string {
  const parts = path.replace(/^remote:\/\//, "").split("/");
  return parts.slice(2).join("/");
}

export function ExplorerProvider({ children }: { children: React.ReactNode }) {
  const { selectedProjectId, selectedProject } = useProject();

  const [currentPath, setCurrentPath] = useState("");
  const [browseDataState, setBrowseDataState] =
    useState<ExplorerListResponse | null>(null);
  const [hfCreatorMapping, setHfCreatorMapping] =
    useState<CreatorMapping | null>(null);
  const [hfSearchQuery, setHfSearchQuery] = useState<string>("");
  const loadedRepoKeyRef = useRef<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [viewMode, setViewModeState] = useState<ViewMode>("grid");
  const [sortKey, setSortKeyState] = useState<SortKey>(DEFAULT_SORT_KEY);
  const [sortDir, setSortDirState] = useState<SortDir>(DEFAULT_SORT_DIR);
  const [selectedItems, setSelectedItems] = useState<Set<string>>(new Set());
  const [focusedItemPath, setFocusedItemPath] = useState<string | null>(null);
  const selectionAnchorPathRef = useRef<string | null>(null);
  const previousShiftRangeRef = useRef<Set<string>>(new Set());
  const [clipboard, setClipboard] = useState<ClipboardState | null>(null);
  const [storageCtx, setStorageCtx] = useState<StorageContext | null>(null);
  const [storageCtxList, setStorageCtxList] = useState<StorageContext[]>([]);
  const [isAdmin, setIsAdmin] = useState(false);
  const [isAbsoluteFilerPath, setIsAbsoluteFilerPath] = useState(false);
  const [isHfMode, setIsHfMode] = useState(false);
  const [isHydrusMode, setIsHydrusMode] = useState(false);
  const [filerTab, setFilerTabState] = useState<FilerTab>("workspace");
  const activeFilerTabRef = useRef<FilerTab>("workspace");
  const historyBackRef = useRef<string[]>([]);
  const historyForwardRef = useRef<string[]>([]);
  const [userId, setUserId] = useState<string | null>(null);
  const fetchingRef = useRef(false);
  const pendingFetchPathRef = useRef<string | null>(null);
  const initDoneRef = useRef(false);

  // localStorage からviewMode復元
  useEffect(() => {
    const saved = readLocalStorage(EXPLORER_VIEW_MODE_STORAGE_KEY);
    if (saved === "grid" || saved === "list") setViewModeState(saved);
    const savedTab = readLocalStorage(FILER_TAB_STORAGE_KEY);
    if (isFilerTab(savedTab)) {
      activeFilerTabRef.current = savedTab;
      setFilerTabState(savedTab);
    }
    const savedSortKey = readLocalStorage(EXPLORER_SORT_KEY_STORAGE_KEY);
    if (isSortKey(savedSortKey)) setSortKeyState(savedSortKey);
    const savedSortDir = readLocalStorage(EXPLORER_SORT_DIR_STORAGE_KEY);
    if (isSortDir(savedSortDir)) setSortDirState(savedSortDir);
  }, []);

  const setViewMode = useCallback((mode: ViewMode) => {
    setViewModeState(mode);
    writeLocalStorage(EXPLORER_VIEW_MODE_STORAGE_KEY, mode);
  }, []);

  const setSort = useCallback((key: SortKey, dir: SortDir) => {
    setSortKeyState(key);
    setSortDirState(dir);
    writeLocalStorage(EXPLORER_SORT_KEY_STORAGE_KEY, key);
    writeLocalStorage(EXPLORER_SORT_DIR_STORAGE_KEY, dir);
  }, []);

  // HF 検索クエリで絞り込んだ browseData（HFモード時のみフィルタ）
  const browseData = useMemo<ExplorerListResponse | null>(() => {
    if (!browseDataState) return null;
    if (!isHfMode) return browseDataState;
    const q = hfSearchQuery.trim().toLowerCase();
    if (!q) return browseDataState;
    const filteredDirs = browseDataState.directories.filter((d) =>
      creatorMatchesQuery(hfCreatorMapping, d.name, q),
    );
    const filteredFiles = browseDataState.files.filter((f) =>
      f.name.toLowerCase().includes(q),
    );
    return {
      ...browseDataState,
      directories: filteredDirs,
      files: filteredFiles,
      total_items: filteredDirs.length + filteredFiles.length,
    };
  }, [browseDataState, isHfMode, hfSearchQuery, hfCreatorMapping]);

  const setBrowseData = useCallback(
    (data: ExplorerListResponse | null) => setBrowseDataState(data),
    [],
  );

  const clearNavigationHistory = useCallback(() => {
    historyBackRef.current = [];
    historyForwardRef.current = [];
  }, []);

  // コンテキストルートパス（タブに応じた基準パス）
  const contextRootPath = useMemo(() => {
    if (isAbsoluteFilerPath) return "";
    if (filerTab === "workspace" && selectedProject?.source === "remote") {
      return remoteWorkspacePath(
        selectedProject.remote_server_id ?? "",
        selectedProject.resource_id ?? "",
      );
    }
    if (filerTab === "workspace" && selectedProjectId) {
      return workspaceRoot(selectedProjectId);
    }
    if (filerTab === "user" && userId) {
      return userRoot(userId);
    }
    return "";
  }, [filerTab, selectedProject, selectedProjectId, userId, isAbsoluteFilerPath]);

  const initialPathForTab = useCallback(
    (tab: FilerTab, uid: string | null = userId): string | null => {
      if (tab === "workspace") {
        if (!selectedProjectId) return null;
        if (selectedProject?.source === "remote") {
          return remoteWorkspacePath(
            selectedProject.remote_server_id ?? "",
            selectedProject.resource_id ?? "",
          );
        }
        return (
          readLastPath(tab, selectedProjectId) ??
          workspaceRoot(selectedProjectId)
        );
      }
      if (tab === "user") {
        if (!uid) return null;
        return readLastPath(tab, uid) ?? userRoot(uid);
      }
      if (tab === "hf") {
        return readLastPath(tab) ?? HF_PREFIX;
      }
      return null;
    },
    [selectedProject, selectedProjectId, userId],
  );

  const rememberCurrentPath = useCallback(
    (path: string) => {
      if (!path || isAbsolutePath(path)) return;
      if (isHfPath(path)) {
        writeLastPath("hf", path);
        return;
      }
      if (
        selectedProjectId &&
        path.startsWith(workspaceRoot(selectedProjectId))
      ) {
        writeLastPath("workspace", path, selectedProjectId);
        return;
      }
      if (userId && path.startsWith(userRoot(userId))) {
        writeLastPath("user", path, userId);
        return;
      }
      const activeTab = activeFilerTabRef.current;
      writeLastPath(
        activeTab,
        path,
        activeTab === "user" ? userId : selectedProjectId,
      );
    },
    [selectedProjectId, userId],
  );

  // ディレクトリ読み込み（explorer API or filer path API or HF API を自動判定）
  const fetchDirectory = useCallback(
    async (path: string) => {
      if (fetchingRef.current) {
        pendingFetchPathRef.current = path;
        return;
      }

      let nextPath: string | null = path;
      while (nextPath !== null) {
        const targetPath = nextPath;
        nextPath = null;
        fetchingRef.current = true;
        setLoading(true);
        setError(null);

        const useHf = isHfPath(targetPath);
        const useAbsoluteFilerPath = !useHf && isAbsolutePath(targetPath);

        try {
          if (useHf) {
            const data = await hfExplorerList(targetPath);
            setIsAbsoluteFilerPath(false);
            setIsHfMode(true);
            setIsHydrusMode(false);
            setBrowseDataState(data);
            setCurrentPath(data.current_path);
            rememberCurrentPath(data.current_path);

            // creator_mapping.json はリポごとに一度だけロード（内部キャッシュも併用）
            const parsed = parseHfPath(data.current_path);
            if (parsed?.kind === "repo" && parsed.repoId && parsed.repoType) {
              const key = `${parsed.accountId ?? ""}|${parsed.repoType}|${parsed.repoId}`;
              if (loadedRepoKeyRef.current !== key) {
                loadedRepoKeyRef.current = key;
                setHfCreatorMapping(null);
                setHfSearchQuery("");
                fetchCreatorMapping({
                  accountId: parsed.accountId,
                  repoType: parsed.repoType,
                  repoId: parsed.repoId,
                }).then((m) => {
                  if (loadedRepoKeyRef.current === key) setHfCreatorMapping(m);
                });
              }
            } else {
              loadedRepoKeyRef.current = null;
              setHfCreatorMapping(null);
              setHfSearchQuery("");
            }
          } else if (
            targetPath.startsWith("remote://") &&
            selectedProject?.source === "remote" &&
            selectedProject.remote_server_id &&
            selectedProject.resource_id
          ) {
            const relativePath = remoteWorkspaceRelativePath(targetPath);
            const remoteData = await listRemoteWorkspaceFiles(
              selectedProject.remote_server_id,
              selectedProject.resource_id,
              relativePath,
            );
            const basePath = (remoteData.current_path ?? relativePath).replace(/^\/+|\/+$/g, "");
            const data: ExplorerListResponse = {
              success: remoteData.success !== false,
              current_path: remoteWorkspacePath(
                selectedProject.remote_server_id,
                selectedProject.resource_id,
                basePath,
              ),
              parent_path: remoteData.parent_path
                ? remoteWorkspacePath(
                    selectedProject.remote_server_id,
                    selectedProject.resource_id,
                    remoteData.parent_path,
                  )
                : null,
              can_go_up: Boolean(remoteData.can_go_up),
              directories: (remoteData.directories ?? []).map((item) => ({
                name: String(item.name ?? ""),
                path: remoteWorkspacePath(
                  selectedProject.remote_server_id!,
                  selectedProject.resource_id!,
                  String(item.path ?? item.name ?? ""),
                ),
                item_count: typeof item.item_count === "number" ? item.item_count : undefined,
                modified_at: typeof item.modified_at === "string" ? item.modified_at : undefined,
              })),
              files: (remoteData.files ?? []).map((item) => ({
                name: String(item.name ?? ""),
                path: remoteWorkspacePath(
                  selectedProject.remote_server_id!,
                  selectedProject.resource_id!,
                  String(item.path ?? item.name ?? ""),
                ),
                type: String(item.type ?? "file"),
                size: typeof item.size === "number" ? item.size : undefined,
                modified_at: typeof item.modified_at === "string" ? item.modified_at : undefined,
              })),
              total_items: remoteData.total_items ?? 0,
            };
            setIsAbsoluteFilerPath(true);
            setIsHfMode(false);
            setIsHydrusMode(false);
            setBrowseDataState(data);
            setCurrentPath(data.current_path);
            rememberCurrentPath(data.current_path);
          } else if (useAbsoluteFilerPath && isAdmin) {
            const data = await explorerList(targetPath);
            setIsAbsoluteFilerPath(true);
            setIsHfMode(false);
            setIsHydrusMode(false);
            setBrowseDataState(data);
            setCurrentPath(data.current_path);
            rememberCurrentPath(data.current_path);
          } else if (useAbsoluteFilerPath) {
            await filerBrowse(targetPath);
            throw new Error("absolute path access denied");
          } else {
            const data = await explorerList(targetPath);
            setIsAbsoluteFilerPath(false);
            setIsHfMode(false);
            setIsHydrusMode(false);
            setBrowseDataState(data);
            setCurrentPath(data.current_path);
            rememberCurrentPath(data.current_path);
          }
          setSelectedItems(new Set());
          setFocusedItemPath(null);
          selectionAnchorPathRef.current = null;
          previousShiftRangeRef.current = new Set();
        } catch {
          setError("ディレクトリの読み込みに失敗しました");
        } finally {
          setLoading(false);
          fetchingRef.current = false;
          nextPath = pendingFetchPathRef.current;
          pendingFetchPathRef.current = null;
        }
      }
    },
    [isAdmin, rememberCurrentPath, selectedProject],
  );

  const navigate = useCallback(
    (path: string) => {
      if (currentPath && currentPath !== path) {
        historyBackRef.current = [...historyBackRef.current, currentPath];
        historyForwardRef.current = [];
      }
      fetchDirectory(path);
    },
    [currentPath, fetchDirectory],
  );

  const goBack = useCallback(() => {
    const previousPath = historyBackRef.current.at(-1);
    if (!previousPath) return;

    historyBackRef.current = historyBackRef.current.slice(0, -1);
    if (currentPath && currentPath !== previousPath) {
      historyForwardRef.current = [currentPath, ...historyForwardRef.current];
    }
    fetchDirectory(previousPath);
  }, [currentPath, fetchDirectory]);

  const goForward = useCallback(() => {
    const nextPath = historyForwardRef.current[0];
    if (!nextPath) return;

    historyForwardRef.current = historyForwardRef.current.slice(1);
    if (currentPath && currentPath !== nextPath) {
      historyBackRef.current = [...historyBackRef.current, currentPath];
    }
    fetchDirectory(nextPath);
  }, [currentPath, fetchDirectory]);

  const goUp = useCallback(() => {
    // コンテキストルートより上には行かせない（管理者・絶対パス閲覧時は制限なし）
    if (
      !isAdmin &&
      !isAbsoluteFilerPath &&
      contextRootPath &&
      currentPath === contextRootPath
    ) {
      return;
    }
    if (browseData?.parent_path != null) {
      navigate(browseData.parent_path);
    }
  }, [
    browseData,
    navigate,
    currentPath,
    contextRootPath,
    isAbsoluteFilerPath,
    isAdmin,
  ]);

  const refresh = useCallback(async () => {
    await fetchDirectory(currentPath);
  }, [fetchDirectory, currentPath]);

  // 選択
  const selectItem = useCallback((path: string) => {
    setSelectedItems(new Set([path]));
    setFocusedItemPath(path);
    selectionAnchorPathRef.current = path;
    previousShiftRangeRef.current = new Set();
  }, []);

  const toggleSelect = useCallback((path: string) => {
    setSelectedItems((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
    setFocusedItemPath(path);
    selectionAnchorPathRef.current = path;
    previousShiftRangeRef.current = new Set();
  }, []);

  const selectRange = useCallback(
    (path: string, orderedPaths: string[], additive = false) => {
      setSelectedItems((prev) => {
        const result = buildExplorerRangeSelection({
          orderedPaths,
          anchorPath: selectionAnchorPathRef.current,
          targetPath: path,
          selectedPaths: prev,
          previousShiftRange: previousShiftRangeRef.current,
          additive,
        });
        selectionAnchorPathRef.current = result.anchorPath;
        previousShiftRangeRef.current = result.shiftRange;
        return result.selectedPaths;
      });
      setFocusedItemPath(path);
    },
    [],
  );

  const selectAll = useCallback(() => {
    if (!browseData) return;
    const allPaths = [
      ...browseData.directories.map((d) => d.path),
      ...browseData.files.map((f) => f.path),
    ];
    const all = new Set(allPaths);
    setSelectedItems(all);
    setFocusedItemPath((current) =>
      current && all.has(current) ? current : allPaths[0] ?? null,
    );
    selectionAnchorPathRef.current =
      focusedItemPath && all.has(focusedItemPath)
        ? focusedItemPath
        : (allPaths[0] ?? null);
    previousShiftRangeRef.current = new Set();
  }, [browseData, focusedItemPath]);

  const clearSelection = useCallback(() => {
    setSelectedItems(new Set());
    setFocusedItemPath(null);
    selectionAnchorPathRef.current = null;
    previousShiftRangeRef.current = new Set();
  }, []);

  // ブックマーク（取得・キャッシュ・重複排除を SWR に委譲）。
  // 取得失敗時は従来同様に空配列扱いにするため、fetcher 内で例外を握りつぶす。
  const bookmarksFetcher = useCallback(async (): Promise<ExplorerBookmark[]> => {
    try {
      const data = await explorerBookmarks();
      return data.success ? data.bookmarks : [];
    } catch {
      return [];
    }
  }, []);

  const { data: bookmarksData, mutate: mutateBookmarks } = useSWR<
    ExplorerBookmark[]
  >(BOOKMARKS_SWR_KEY, bookmarksFetcher, {
    // 取得タイミングを従来実装（refreshBookmarks 呼び出し）に一致させるため、
    // SWR の自動 revalidation は全て無効化し、全ての取得を refreshBookmarks 経由にする。
    revalidateOnMount: false,
    revalidateOnFocus: false,
    revalidateOnReconnect: false,
    revalidateIfStale: false,
    keepPreviousData: true,
    dedupingInterval: 0,
  });
  const bookmarks = bookmarksData ?? EMPTY_BOOKMARKS;

  // revalidate を実行（従来の refreshBookmarks と同じ呼び出し駆動）。
  const refreshBookmarks = useCallback(async () => {
    await mutateBookmarks();
  }, [mutateBookmarks]);

  // タブ切り替え
  const setFilerTab = useCallback(
    (tab: FilerTab) => {
      clearNavigationHistory();
      activeFilerTabRef.current = tab;
      setFilerTabState(tab);
      setIsAbsoluteFilerPath(false);
      writeLocalStorage(FILER_TAB_STORAGE_KEY, tab);
      const restorePath = initialPathForTab(tab);
      if (tab === "workspace" && restorePath) {
        setIsHfMode(false);
        setIsHydrusMode(false);
        fetchDirectory(restorePath);
      } else if (tab === "user" && restorePath) {
        setIsHfMode(false);
        setIsHydrusMode(false);
        fetchDirectory(restorePath);
      } else if (tab === "hf") {
        setIsHfMode(true);
        setIsHydrusMode(false);
        fetchDirectory(restorePath ?? HF_PREFIX);
      } else if (tab === "hydrus") {
        setIsHfMode(false);
        setIsHydrusMode(true);
        // Hydrus は ExplorerListResponse にマップしにくいので、
        // browseData を空にして専用検索UI（filer/page.tsx 側）から駆動する
        setBrowseDataState({
          success: true,
          current_path: "Hydrus",
          parent_path: null,
          can_go_up: false,
          directories: [],
          files: [],
          total_items: 0,
        });
        setCurrentPath("Hydrus");
        setLoading(false);
      }
    },
    [clearNavigationHistory, fetchDirectory, initialPathForTab],
  );

  // ユーザー情報取得（/api/auth/status → userId, isAdmin）
  const fetchUserInfo = useCallback(async () => {
    try {
      const res = await fetch("/api/auth/status", { credentials: "include" });
      if (res.ok) {
        const data = await res.json();
        if (data.authenticated && data.user) {
          setUserId(data.user.id);
          setIsAdmin(data.user.role === "admin");
          return {
            userId: data.user.id as string,
            isAdmin: data.user.role === "admin",
          };
        }
      }
    } catch {
      /* ignore */
    }
    // フォールバック: storage/contexts API
    try {
      const data = await storageContexts();
      if (data.success) {
        setStorageCtxList(data.contexts);
        setIsAdmin(data.is_admin);
        setStorageCtx(
          data.contexts.find(
            (ctx) =>
              ctx.type === data.current_context?.type &&
              ctx.id === data.current_context?.id,
          ) ?? null,
        );
        if (data.current_context?.id) {
          setUserId(data.current_context.id);
          return { userId: data.current_context.id, isAdmin: data.is_admin };
        }
      }
    } catch {
      /* ignore */
    }
    return null;
  }, []);

  // ── Editor state ───────────────────────────────────────────────────
  const [editingFile, setEditingFile] = useState<{
    path: string;
    name: string;
    extension: string;
  } | null>(null);

  const openEditor = useCallback(
    (file: { path: string; name: string; type?: string }) => {
      const ext = file.name.includes(".")
        ? "." + file.name.split(".").pop()!.toLowerCase()
        : "";
      setEditingFile({ path: file.path, name: file.name, extension: ext });
    },
    [],
  );

  const closeEditor = useCallback(() => {
    setEditingFile(null);
  }, []);

  // プロジェクト変更時（初期ロード含む）にワークスペースタブなら自動ナビゲート
  useEffect(() => {
    if (!selectedProjectId) return;
    if (filerTab === "workspace") {
      clearNavigationHistory();
      fetchDirectory(
        selectedProject?.source === "remote"
          ? readLastPath("workspace", selectedProjectId) ??
            remoteWorkspacePath(
              selectedProject.remote_server_id ?? "",
              selectedProject.resource_id ?? "",
            )
          : readLastPath("workspace", selectedProjectId) ??
            workspaceRoot(selectedProjectId),
      );
    }
    if (!initDoneRef.current) initDoneRef.current = true;
  }, [selectedProject, selectedProjectId]); // eslint-disable-line react-hooks/exhaustive-deps

  // 初期化
  useEffect(() => {
    (async () => {
      const userInfo = await fetchUserInfo();
      await refreshBookmarks();

      // 保存されていたタブを復元
      const savedTab = readLocalStorage(FILER_TAB_STORAGE_KEY);
      const tab: FilerTab = isFilerTab(savedTab) ? savedTab : "workspace";
      activeFilerTabRef.current = tab;

      const uid = userInfo?.userId || null;

      if (tab === "user" && uid) {
        await fetchDirectory(readLastPath("user", uid) ?? userRoot(uid));
        initDoneRef.current = true;
      } else if (tab === "workspace" && selectedProjectId) {
        await fetchDirectory(
          readLastPath("workspace", selectedProjectId) ??
            workspaceRoot(selectedProjectId),
        );
        initDoneRef.current = true;
      } else if (tab === "hf") {
        setIsHfMode(true);
        await fetchDirectory(readLastPath("hf") ?? HF_PREFIX);
        initDoneRef.current = true;
      } else if (tab === "hydrus") {
        setIsHydrusMode(true);
        setBrowseDataState({
          success: true,
          current_path: "Hydrus",
          parent_path: null,
          can_go_up: false,
          directories: [],
          files: [],
          total_items: 0,
        });
        setCurrentPath("Hydrus");
        setLoading(false);
        initDoneRef.current = true;
      }
      // workspace tab で selectedProjectId がまだnullの場合は
      // selectedProjectId の useEffect で初期化される
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <ExplorerContext.Provider
      value={{
        currentPath,
        navigate,
        goBack,
        goForward,
        goUp,
        refresh,
        browseData,
        setBrowseData,
        loading,
        error,
        viewMode,
        setViewMode,
        sortKey,
        sortDir,
        setSort,
        selectedItems,
        focusedItemPath,
        selectItem,
        toggleSelect,
        selectRange,
        selectAll,
        clearSelection,
        clipboard,
        setClipboard,
        bookmarks,
        refreshBookmarks,
        filerTab,
        setFilerTab,
        contextRootPath,
        storageCtx,
        storageCtxList,
        isAdmin,
        isAbsoluteFilerPath,
        isHfMode,
        isHydrusMode,
        editingFile,
        openEditor,
        closeEditor,
        hfCreatorMapping,
        hfSearchQuery,
        setHfSearchQuery,
      }}
    >
      {children}
    </ExplorerContext.Provider>
  );
}
