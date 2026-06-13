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
import {
  explorerList,
  explorerBookmarks,
  storageContexts,
  filerBrowse,
  type ExplorerListResponse,
  type ExplorerBookmark,
  type StorageContext,
} from "@/lib/explorer-api";
import {
  listProjectRecordTables,
  recordTableToExplorerFile,
} from "@/lib/record-tables-api";
import { useProject } from "@/contexts/project-context";
import { hfExplorerList } from "@/lib/hf/explorer-loader";
import { HF_PREFIX, isHfPath, parseHfPath } from "@/lib/hf/virtual-path";
import {
  fetchCreatorMapping,
  creatorMatchesQuery,
  type CreatorMapping,
} from "@/lib/hf/creator-mapping";

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

  // Selection
  selectedItems: Set<string>;
  toggleSelect: (path: string) => void;
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

  // Admin system mode
  isSystemMode: boolean;
  isAbsoluteFilerPath: boolean;
  isHfMode: boolean;
  isHydrusMode: boolean;
  enterSystemMode: () => void;
  exitSystemMode: () => void;

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
const FILER_TAB_STORAGE_KEY = "filer-tab";
const FILER_PATH_STORAGE_PREFIX = "filer-last-path";

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

export function ExplorerProvider({ children }: { children: React.ReactNode }) {
  const { selectedProjectId } = useProject();

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
  const [selectedItems, setSelectedItems] = useState<Set<string>>(new Set());
  const [clipboard, setClipboard] = useState<ClipboardState | null>(null);
  const [bookmarks, setBookmarks] = useState<ExplorerBookmark[]>([]);
  const [storageCtx, setStorageCtx] = useState<StorageContext | null>(null);
  const [storageCtxList, setStorageCtxList] = useState<StorageContext[]>([]);
  const [isAdmin, setIsAdmin] = useState(false);
  const [isSystemMode, setIsSystemMode] = useState(false);
  const [isAbsoluteFilerPath, setIsAbsoluteFilerPath] = useState(false);
  const [isHfMode, setIsHfMode] = useState(false);
  const [isHydrusMode, setIsHydrusMode] = useState(false);
  const [filerTab, setFilerTabState] = useState<FilerTab>("workspace");
  const activeFilerTabRef = useRef<FilerTab>("workspace");
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
  }, []);

  const setViewMode = useCallback((mode: ViewMode) => {
    setViewModeState(mode);
    writeLocalStorage(EXPLORER_VIEW_MODE_STORAGE_KEY, mode);
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

  // コンテキストルートパス（タブに応じた基準パス）
  const contextRootPath = useMemo(() => {
    if (isSystemMode) return "";
    if (isAbsoluteFilerPath) return "";
    if (filerTab === "workspace" && selectedProjectId) {
      return workspaceRoot(selectedProjectId);
    }
    if (filerTab === "user" && userId) {
      return userRoot(userId);
    }
    return "";
  }, [filerTab, selectedProjectId, userId, isSystemMode, isAbsoluteFilerPath]);

  const initialPathForTab = useCallback(
    (tab: FilerTab, uid: string | null = userId): string | null => {
      if (tab === "workspace") {
        if (!selectedProjectId) return null;
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
    [selectedProjectId, userId],
  );

  const rememberCurrentPath = useCallback(
    (path: string) => {
      if (!path || isSystemMode) return;
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
    [isSystemMode, selectedProjectId, userId],
  );

  // ディレクトリ読み込み（explorer API or filer path API or HF API を自動判定）
  const attachRecordTables = useCallback(
    async (data: ExplorerListResponse): Promise<ExplorerListResponse> => {
      if (
        !selectedProjectId ||
        data.current_path !== `_projects/project_${selectedProjectId}`
      ) {
        return data;
      }

      const records = await listProjectRecordTables(selectedProjectId);
      const recordFiles = records.tables.map((table) =>
        recordTableToExplorerFile(selectedProjectId, table),
      );

      return {
        ...data,
        files: [...recordFiles, ...data.files],
        total_items:
          data.directories.length + recordFiles.length + data.files.length,
      };
    },
    [selectedProjectId],
  );

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
                  // 途中で別リポに移っていたら捨てる
                  if (loadedRepoKeyRef.current === key) setHfCreatorMapping(m);
                });
              }
            } else {
              loadedRepoKeyRef.current = null;
              setHfCreatorMapping(null);
              setHfSearchQuery("");
            }
          } else if (useAbsoluteFilerPath) {
            const data = await filerBrowse(targetPath);
            setIsAbsoluteFilerPath(true);
            setIsHfMode(false);
            setIsHydrusMode(false);
            setBrowseDataState({
              success: true,
              current_path: data.current_path,
              parent_path: data.parent_path,
              can_go_up: data.can_go_up,
              directories: data.folders.map((f) => ({
                name: f.name,
                path: f.path,
                item_count: f.item_count,
              })),
              files: data.files.map((f) => ({
                name: f.name,
                path: f.path,
                type: f.type,
                size: f.size,
              })),
              total_items: data.folders.length + data.files.length,
            });
            setCurrentPath(data.current_path);
            rememberCurrentPath(data.current_path);
          } else {
            const data = await explorerList(targetPath);
            setIsAbsoluteFilerPath(false);
            setIsHfMode(false);
            setIsHydrusMode(false);
            setBrowseDataState(await attachRecordTables(data));
            setCurrentPath(data.current_path);
            rememberCurrentPath(data.current_path);
          }
          setSelectedItems(new Set());
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
    [attachRecordTables, rememberCurrentPath],
  );

  const navigate = useCallback(
    (path: string) => {
      fetchDirectory(path);
    },
    [fetchDirectory],
  );

  const goUp = useCallback(() => {
    // コンテキストルートより上には行かせない（管理者・システムモード・絶対パス閲覧時は制限なし）
    if (
      !isAdmin &&
      !isSystemMode &&
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
    isSystemMode,
    isAbsoluteFilerPath,
    isAdmin,
  ]);

  const refresh = useCallback(async () => {
    await fetchDirectory(currentPath);
  }, [fetchDirectory, currentPath]);

  // 選択
  const toggleSelect = useCallback((path: string) => {
    setSelectedItems((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  }, []);

  const selectAll = useCallback(() => {
    if (!browseData) return;
    const all = new Set([
      ...browseData.directories.map((d) => d.path),
      ...browseData.files.map((f) => f.path),
    ]);
    setSelectedItems(all);
  }, [browseData]);

  const clearSelection = useCallback(() => {
    setSelectedItems(new Set());
  }, []);

  // ブックマーク
  const refreshBookmarks = useCallback(async () => {
    try {
      const data = await explorerBookmarks();
      setBookmarks(data.success ? data.bookmarks : []);
    } catch {
      setBookmarks([]);
    }
  }, []);

  // タブ切り替え
  const setFilerTab = useCallback(
    (tab: FilerTab) => {
      activeFilerTabRef.current = tab;
      setFilerTabState(tab);
      setIsAbsoluteFilerPath(false);
      setIsSystemMode(false);
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
    [fetchDirectory, initialPathForTab],
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

  // システムモード（管理者専用）
  const enterSystemMode = useCallback(() => {
    setIsSystemMode(true);
    setIsAbsoluteFilerPath(false);
    setCurrentPath("");
    fetchDirectory("");
  }, [fetchDirectory]);

  const exitSystemMode = useCallback(() => {
    setIsSystemMode(false);
    setIsAbsoluteFilerPath(false);
    // 現在のタブのルートに戻る
    const restorePath = initialPathForTab(filerTab);
    if (restorePath) {
      fetchDirectory(restorePath);
    } else {
      fetchDirectory("");
    }
  }, [fetchDirectory, filerTab, initialPathForTab]);

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
    if (filerTab === "workspace" && !isSystemMode && !isAbsoluteFilerPath) {
      fetchDirectory(
        readLastPath("workspace", selectedProjectId) ??
          workspaceRoot(selectedProjectId),
      );
    }
    if (!initDoneRef.current) initDoneRef.current = true;
  }, [selectedProjectId]); // eslint-disable-line react-hooks/exhaustive-deps

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
        goUp,
        refresh,
        browseData,
        setBrowseData,
        loading,
        error,
        viewMode,
        setViewMode,
        selectedItems,
        toggleSelect,
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
        isSystemMode,
        isAbsoluteFilerPath,
        isHfMode,
        isHydrusMode,
        enterSystemMode,
        exitSystemMode,
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
