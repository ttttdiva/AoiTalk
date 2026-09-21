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
import { toast } from "sonner";
import { useCurrentUserId } from "@/components/providers/swr-global-provider";
import {
  explorerList,
  explorerBookmarks,
  storageContexts,
  filerBrowse,
  type ExplorerListResponse,
  type ExplorerBookmark,
  type ExplorerBookmarkScope,
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
  isRemoteFilerPath,
  resolveFilerCapabilities,
  type FilerCapabilities,
} from "@/lib/explorer/filer-capabilities";
import {
  clearFilerUndoHistory,
  registerExplorerDeleteViewHandlers,
  registerHydrusViewHandlers,
  type ExplorerDeleteViewOperation,
} from "@/lib/explorer/filer-operations";
import { parseHydrusFileId } from "@/lib/hydrus/virtual-path";
import {
  DEFAULT_SORT_DIR,
  DEFAULT_SORT_KEY,
  isSortDir,
  isSortKey,
  type SortDir,
  type SortKey,
} from "@/lib/explorer-sort";
import {
  claimFilesSidebarOwner,
  createFilesSidebarOwner,
  publishFilesSidebarState,
  resetFilesSidebarStore,
  type FilesSidebarOwner,
} from "@/components/explorer/files-sidebar-store";

export type ViewMode = "grid" | "list";
export type FilerTab = "workspace" | "user" | "hf" | "hydrus";

/**
 * In-memory view snapshot associated with a directory navigation history
 * entry.  Paths are used as item identities so the snapshot survives sort
 * changes and directory contents being re-rendered in a different order.
 */
export type ExplorerHistoryEntry = {
  path: string;
  focusedItemPath: string | null;
  selectedItemPaths: string[];
};

export type ExplorerHistoryScrollRestore = {
  navigationEpoch: number;
  directoryPath: string;
  focusedItemPath: string;
};

/**
 * Bookmark collections loaded for the authenticated principal and, when the
 * active Files context is a local Project workspace, its selected Space.
 *
 * Keep the provenance on the shared collection instead of flattening the two
 * arrays.  The sidebar uses that provenance to route CRUD operations to the
 * collection that owns an item; `bookmarks` below remains the compatibility
 * view for callers that only need the active collection.
 */
export type ExplorerBookmarkCollections = {
  personal: ExplorerBookmark[];
  shared: { spaceId: string; bookmarks: ExplorerBookmark[] } | null;
};

/** Stable user-facing labels for the Files source switcher.
 *
 * Keep the persisted/API tab IDs above unchanged: these labels are presentation
 * only and intentionally distinguish project-scoped files from personal files.
 */
export const FILER_TAB_LABELS: Record<FilerTab, string> = {
  workspace: "Project Files",
  user: "User Files",
  hf: "HF",
  hydrus: "Hydrus",
};

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
  refresh: () => Promise<boolean>;
  /** One-shot signal for the Files UI to scroll a restored item into view. */
  historyScrollRestore: ExplorerHistoryScrollRestore | null;

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
  selectAll: (paths?: string[]) => void;
  clearSelection: (paths?: string[]) => void;

  // Clipboard
  clipboard: ClipboardState | null;
  setClipboard: (cb: ClipboardState | null) => void;

  // Bookmarks
  bookmarks: ExplorerBookmark[];
  bookmarkCollections: ExplorerBookmarkCollections;
  refreshBookmarks: (owner?: ExplorerBookmarkScope) => Promise<void>;
  /** Scope sent to every bookmark/launcher API operation. */
  bookmarkScope: ExplorerBookmarkScope;

  // Filer tabs
  filerTab: FilerTab;
  setFilerTab: (tab: FilerTab) => void;
  homeRootPath: string;
  contextRootPath: string;

  // Storage Context (legacy)
  storageCtx: StorageContext | null;
  storageCtxList: StorageContext[];
  isAdmin: boolean;

  isAbsoluteFilerPath: boolean;
  isRemoteWorkspace: boolean;
  isHfMode: boolean;
  isHydrusMode: boolean;
  /** Authenticated principal used to namespace HF/Hydrus browser state. */
  userId: string | null;

  /** タブ / パス種別ごとの破壊的操作の可否（削除・リネーム・移動・作成） */
  capabilities: FilerCapabilities;

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

// ブックマーク一覧の SWR キャッシュキー。個人/Space と principal を後続の
// tuple 要素に含め、同じブラウザでのユーザー/Space 切り替え時にキャッシュを
// 共有しない。取得タイミングは従来どおり手動 revalidate で駆動する。
const BOOKMARKS_SWR_KEY = "explorer/bookmarks";
const EMPTY_BOOKMARKS: ExplorerBookmark[] = [];

function bookmarkScopeIdentity(scope: ExplorerBookmarkScope): string {
  return scope.scope === "shared"
    ? `shared:${scope.spaceId}`
    : "personal:";
}

export function useExplorer() {
  const ctx = useContext(ExplorerContext);
  if (!ctx) throw new Error("useExplorer must be used within ExplorerProvider");
  return ctx;
}

// 絶対パス閲覧判定: Windows/Unix形式の絶対パス
function isAbsolutePath(p: string): boolean {
  if (!p) return false;
  if (/^[A-Za-z]:[\\/]/.test(p)) return true;
  // POSIX-rooted and Windows UNC paths are both valid local Files targets.
  return /^[/\\]{1,2}/.test(p);
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
  return `${FILER_PATH_STORAGE_PREFIX}:${tab}:${ownerId || "anonymous"}`;
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

function workspaceProjectIdFromPath(path: string): string | null {
  const normalized = path.replace(/\\/g, "/").replace(/^\/+|\/+$/g, "");
  if (normalized.startsWith("aoitalk-record-table:")) {
    return normalized.slice("aoitalk-record-table:".length).split(":", 1)[0] || null;
  }
  const match = normalized.match(/^_projects\/project_([^/]+)(?:\/|$)/);
  return match?.[1] ?? null;
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

function normalizeExplorerPath(path: string): string {
  return path.replace(/\\/g, "/").replace(/^\/+|\/+$/g, "");
}

function explorerParentPath(path: string): string {
  const normalized = normalizeExplorerPath(path);
  const index = normalized.lastIndexOf("/");
  return index < 0 ? "" : normalized.slice(0, index);
}

function historyEntriesEqual(
  left: ExplorerHistoryEntry | null | undefined,
  right: ExplorerHistoryEntry | null | undefined,
): boolean {
  if (left === right) return true;
  if (!left || !right) return false;
  if (
    normalizeExplorerPath(left.path) !== normalizeExplorerPath(right.path) ||
    left.focusedItemPath !== right.focusedItemPath ||
    left.selectedItemPaths.length !== right.selectedItemPaths.length
  ) {
    return false;
  }
  return left.selectedItemPaths.every(
    (path, index) => path === right.selectedItemPaths[index],
  );
}

function cloneHistoryEntry(entry: ExplorerHistoryEntry): ExplorerHistoryEntry {
  return {
    path: entry.path,
    focusedItemPath: entry.focusedItemPath,
    selectedItemPaths: [...entry.selectedItemPaths],
  };
}

function emptyHistoryEntry(path: string): ExplorerHistoryEntry {
  return {
    path,
    focusedItemPath: null,
    selectedItemPaths: [],
  };
}

type DirectoryFetchRequest = {
  path: string;
  /** Principal whose session authorized the request. */
  principalId: string | null;
  generation: number;
  /** Navigation identity that initiated this request. */
  navigationEpoch: number;
  /** Optional view snapshot to restore after this request succeeds. */
  restoreViewState: ExplorerHistoryEntry | null;
};

type ActiveDirectoryFetch = {
  generation: number;
  promise: Promise<void>;
};

type LogicalNavigationHistorySnapshot = {
  back: ExplorerHistoryEntry[];
  forward: ExplorerHistoryEntry[];
};

type LogicalNavigationCursor = {
  entry: ExplorerHistoryEntry;
  navigationEpoch: number;
  pending: boolean;
  /** Stack state before the pending intent, used to roll back failures. */
  historyBefore?: LogicalNavigationHistorySnapshot;
};

export function ExplorerProvider({ children }: { children: React.ReactNode }) {
  const {
    selectedProjectId,
    selectedProject,
    selectedSpaceId,
    participatingProjects = [],
    accessibleProjects = [],
    setSelectedProjectId,
  } = useProject();
  // AppLayout already resolved the session before mounting the explorer.  Use
  // that value as the first principal instead of waiting for a second
  // `/api/auth/status` round-trip; the latter used to leave User Files data
  // tagged as anonymous when the initial listing resolved first.
  const sessionUserId = useCurrentUserId();

  const [currentPath, setCurrentPath] = useState("");
  const [filesTargetProjectId, setFilesTargetProjectId] = useState<string | null>(null);
  const [browseDataState, setBrowseDataState] =
    useState<ExplorerListResponse | null>(null);
  // Principal that produced browseDataState.  Rendering is gated below so a
  // logout/login switch cannot flash the previous user's directory.
  const [dataPrincipalId, setDataPrincipalId] = useState<string | null>(null);
  const [hfCreatorMapping, setHfCreatorMapping] =
    useState<CreatorMapping | null>(null);
  const [hfSearchQuery, setHfSearchQuery] = useState<string>("");
  // Hydrus は検索結果のページキャッシュを持ち refresh で削除が反映されないため、
  // 削除済み file_id を表示側で保持して一覧から除外する。
  const [hydrusHiddenFileIds, setHydrusHiddenFileIds] = useState<Set<number>>(
    () => new Set(),
  );
  // 通常の Files 削除中に一覧から先行して隠す tombstone。Hydrus/HF は
  // 専用の表示キャッシュを持つため、この state を適用しない。
  const pendingDeletedPathsRef = useRef<Set<string>>(new Set());
  const committedDeletedPathsRef = useRef<Set<string>>(new Set());
  // Multiple delete operations may overlap on the same path.  Keep every
  // operation owner so that settling one operation cannot clear another
  // operation's pending tombstone.
  const pendingDeleteOwnersRef = useRef<Map<string, Set<symbol>>>(new Map());
  // Opaque delete handles capture this generation.  Principal/tab changes and
  // unmount advance it so late API completions become no-ops.
  const deleteScopeGenerationRef = useRef(0);
  const [pendingDeletedPaths, setPendingDeletedPaths] = useState<Set<string>>(
    () => new Set(pendingDeletedPathsRef.current),
  );
  const [committedDeletedPaths, setCommittedDeletedPaths] =
    useState<Set<string>>(() => new Set(committedDeletedPathsRef.current));
  const [deleteScopeVersion, setDeleteScopeVersion] = useState(0);
  const loadedRepoKeyRef = useRef<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!filesTargetProjectId) return;
    if (workspaceProjectIdFromPath(currentPath) !== filesTargetProjectId) {
      setFilesTargetProjectId(null);
    }
  }, [currentPath, filesTargetProjectId]);
  const [viewMode, setViewModeState] = useState<ViewMode>("grid");
  const [sortKey, setSortKeyState] = useState<SortKey>(DEFAULT_SORT_KEY);
  const [sortDir, setSortDirState] = useState<SortDir>(DEFAULT_SORT_DIR);
  const [selectedItems, setSelectedItems] = useState<Set<string>>(new Set());
  const [focusedItemPath, setFocusedItemPath] = useState<string | null>(null);
  const [historyScrollRestore, setHistoryScrollRestore] =
    useState<ExplorerHistoryScrollRestore | null>(null);
  // Keep the committed view synchronously available to navigation handlers.
  // State setters are asynchronous, and a select/open gesture can invoke
  // `navigate` before React has committed the selection render.
  const currentPathRef = useRef(currentPath);
  const selectedItemsRef = useRef(selectedItems);
  const focusedItemPathRef = useRef(focusedItemPath);
  useEffect(() => {
    currentPathRef.current = currentPath;
    selectedItemsRef.current = selectedItems;
    focusedItemPathRef.current = focusedItemPath;
  }, [currentPath, focusedItemPath, selectedItems]);
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
  const historyBackRef = useRef<ExplorerHistoryEntry[]>([]);
  const historyForwardRef = useRef<ExplorerHistoryEntry[]>([]);
  // Unlike currentPathRef, this cursor advances synchronously with a user
  // navigation intent.  It is authoritative while a request is pending so
  // rapid A→B→C and Back/Forward gestures cannot duplicate or skip entries.
  const logicalNavigationCursorRef = useRef<LogicalNavigationCursor | null>(null);
  const [userId, setUserId] = useState<string | null>(() =>
    sessionUserId === undefined ? null : sessionUserId,
  );
  const [hfOwnedAccountIds, setHfOwnedAccountIds] = useState<Set<string>>(
    () => new Set(),
  );
  const previousUserIdRef = useRef<string | null>(null);
  const activeFetchRef = useRef<ActiveDirectoryFetch | null>(null);
  const pendingFetchRef = useRef<DirectoryFetchRequest | null>(null);
  const principalGenerationRef = useRef(0);
  // Incremented synchronously for every user-visible navigation.  Rename
  // refreshes capture the epoch at invocation time; a later navigation can
  // therefore invalidate the stale refresh before it reaches the queue.
  const navigationEpochRef = useRef(0);
  const [, setNavigationEpoch] = useState(0);
  const userFilesRecoveryKeyRef = useRef<string | null>(null);
  const initDoneRef = useRef(false);
  const filesSidebarOwnerRef = useRef<FilesSidebarOwner | null>(null);
  if (!filesSidebarOwnerRef.current) {
    filesSidebarOwnerRef.current = createFilesSidebarOwner();
  }

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

  // HF 検索クエリで絞り込んだ browseData（HFモード時のみフィルタ）。
  // Hydrus モードでは削除済み file_id を除外する。通常の Files では
  // 削除中 tombstone を適用し、API refresh 完了を待たずに表示を確定する。
  const browseData = useMemo<ExplorerListResponse | null>(() => {
    if (!browseDataState || dataPrincipalId !== userId) return null;
    if (isHydrusMode) {
      if (hydrusHiddenFileIds.size === 0) return browseDataState;
      const files = browseDataState.files.filter((file) => {
        const fileId = parseHydrusFileId(file.path);
        return fileId === null || !hydrusHiddenFileIds.has(fileId);
      });
      return {
        ...browseDataState,
        files,
        total_items: browseDataState.directories.length + files.length,
      };
    }
    if (isHfMode) {
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
    }
    if (pendingDeletedPaths.size === 0 && committedDeletedPaths.size === 0) {
      return browseDataState;
    }
    const hiddenPaths = new Set([
      ...pendingDeletedPaths,
      ...committedDeletedPaths,
    ]);
    const directories = browseDataState.directories.filter(
      (directory) => !hiddenPaths.has(directory.path),
    );
    const files = browseDataState.files.filter(
      (file) => !hiddenPaths.has(file.path),
    );
    return {
      ...browseDataState,
      directories,
      files,
      total_items: directories.length + files.length,
    };
  }, [
    browseDataState,
    dataPrincipalId,
    userId,
    isHfMode,
    isHydrusMode,
    hydrusHiddenFileIds,
    pendingDeletedPaths,
    hfSearchQuery,
    hfCreatorMapping,
    committedDeletedPaths,
  ]);

  const setBrowseDataForPrincipal = useCallback(
    (
      data: ExplorerListResponse | null,
      principalId: string | null = userId,
    ) => {
      // A successful delete is represented by a committed tombstone until a
      // current-directory response proves that the server no longer returns
      // the path.  A stale response that still contains it therefore cannot
      // resurrect the item, while a later response which omits it clears the
      // tombstone and lets future refreshes become authoritative again.
      if (data && principalId === userId) {
        const currentDirectory = normalizeExplorerPath(data.current_path);
        const responsePaths = new Set([
          ...data.directories.map((directory) => normalizeExplorerPath(directory.path)),
          ...data.files.map((file) => normalizeExplorerPath(file.path)),
        ]);
        const nextCommitted = new Set(committedDeletedPathsRef.current);
        let changed = false;
        for (const path of nextCommitted) {
          if (
            explorerParentPath(path) === currentDirectory &&
            !responsePaths.has(normalizeExplorerPath(path))
          ) {
            nextCommitted.delete(path);
            changed = true;
          }
        }
        if (changed) {
          committedDeletedPathsRef.current = nextCommitted;
          setCommittedDeletedPaths(nextCommitted);
        }
      }
      setBrowseDataState(data);
      setDataPrincipalId(principalId);
    },
    [userId],
  );

  const setBrowseData = setBrowseDataForPrincipal;

  // Hydrus 削除/復元の表示反映。filer-operations から呼ばれる。
  const pruneHydrusFiles = useCallback((fileIds: number[]) => {
    if (fileIds.length === 0) return;
    setHydrusHiddenFileIds((prev) => {
      const next = new Set(prev);
      for (const id of fileIds) next.add(id);
      return next;
    });
  }, []);

  const restoreHydrusFiles = useCallback((fileIds: number[]) => {
    if (fileIds.length === 0) return;
    setHydrusHiddenFileIds((prev) => {
      const next = new Set(prev);
      for (const id of fileIds) next.delete(id);
      return next;
    });
  }, []);

  const invalidateExplorerDeleteScope = useCallback(
    (clearState = true) => {
      deleteScopeGenerationRef.current += 1;
      pendingDeleteOwnersRef.current.clear();
      pendingDeletedPathsRef.current = new Set();
      committedDeletedPathsRef.current = new Set();
      if (clearState) {
        setPendingDeletedPaths(new Set());
        setCommittedDeletedPaths(new Set());
        setDeleteScopeVersion((version) => version + 1);
      }
    },
    [],
  );

  const noopDeleteOperation = useCallback((): ExplorerDeleteViewOperation => ({
    commit: () => undefined,
    rollback: () => undefined,
  }), []);

  const hideExplorerDeletePaths = useCallback(
    (
      paths: string[],
      expectedScopeGeneration = deleteScopeGenerationRef.current,
    ): ExplorerDeleteViewOperation => {
      if (
        paths.length === 0 ||
        expectedScopeGeneration !== deleteScopeGenerationRef.current
      ) {
        return noopDeleteOperation();
      }
      const owner = Symbol("explorer-delete");
      const ownedPaths = new Set<string>();
      const nextPending = new Set(pendingDeletedPathsRef.current);
      for (const path of paths) {
        // A committed tombstone already keeps this path hidden.  Otherwise
        // every overlapping operation records its own owner so one
        // operation cannot settle another operation's pending state.
        if (committedDeletedPathsRef.current.has(path)) continue;
        let owners = pendingDeleteOwnersRef.current.get(path);
        if (!owners) {
          owners = new Set<symbol>();
          pendingDeleteOwnersRef.current.set(path, owners);
        }
        owners.add(owner);
        ownedPaths.add(path);
        nextPending.add(path);
      }
      if (ownedPaths.size > 0) {
        pendingDeletedPathsRef.current = nextPending;
        setPendingDeletedPaths(nextPending);
      }

      const settle = (pathsToSettle: string[], commit: boolean) => {
        if (expectedScopeGeneration !== deleteScopeGenerationRef.current) return;
        const requested = new Set(pathsToSettle);
        const settled = [...ownedPaths].filter((path) => requested.has(path));
        if (settled.length === 0) return;
        for (const path of settled) {
          ownedPaths.delete(path);
          const owners = pendingDeleteOwnersRef.current.get(path);
          if (owners) {
            owners.delete(owner);
            if (owners.size === 0) pendingDeleteOwnersRef.current.delete(path);
          }
        }
        const nextPendingState = new Set(pendingDeletedPathsRef.current);
        const nextCommittedState = new Set(committedDeletedPathsRef.current);
        for (const path of settled) {
          if (commit) {
            // A successful owner commits the path even while another owner is
            // still in flight.  The committed tombstone is authoritative, so
            // no pending tombstone is needed after this point.
            nextCommittedState.add(path);
            nextPendingState.delete(path);
            continue;
          }
          const owners = pendingDeleteOwnersRef.current.get(path);
          // Only the final rollback of an uncommitted path may reveal it.
          if (!owners?.size && !nextCommittedState.has(path)) {
            nextPendingState.delete(path);
          }
        }
        pendingDeletedPathsRef.current = nextPendingState;
        committedDeletedPathsRef.current = nextCommittedState;
        setPendingDeletedPaths(nextPendingState);
        if (commit) setCommittedDeletedPaths(nextCommittedState);
        if (!commit) return;

        const deletedPaths = new Set(settled);
        setBrowseDataState((previous) => {
          if (!previous) return previous;
          const directories = previous.directories.filter(
            (directory) => !deletedPaths.has(directory.path),
          );
          const files = previous.files.filter(
            (file) => !deletedPaths.has(file.path),
          );
          if (
            directories.length === previous.directories.length &&
            files.length === previous.files.length
          ) {
            return previous;
          }
          return {
            ...previous,
            directories,
            files,
            total_items: directories.length + files.length,
          };
        });
      };

      return {
        commit: (pathsToCommit) => settle(pathsToCommit, true),
        rollback: (pathsToRollback) => settle(pathsToRollback, false),
      };
    },
    [noopDeleteOperation],
  );

  const restoreExplorerDeletePaths = useCallback(
    (
      paths: string[],
      expectedScopeGeneration = deleteScopeGenerationRef.current,
    ) => {
      if (
        paths.length === 0 ||
        expectedScopeGeneration !== deleteScopeGenerationRef.current
      ) {
        return;
      }
      const nextCommitted = new Set(committedDeletedPathsRef.current);
      let changed = false;
      for (const path of paths) {
        if (nextCommitted.delete(path)) changed = true;
      }
      if (changed) {
        committedDeletedPathsRef.current = nextCommitted;
        setCommittedDeletedPaths(nextCommitted);
      }
    },
    [],
  );

  useEffect(() => {
    const registrationGeneration = deleteScopeGenerationRef.current;
    const unregister = registerHydrusViewHandlers({
      prune: (fileIds) => {
        if (registrationGeneration !== deleteScopeGenerationRef.current) return;
        pruneHydrusFiles(fileIds);
      },
      restore: (fileIds) => {
        if (registrationGeneration !== deleteScopeGenerationRef.current) return;
        restoreHydrusFiles(fileIds);
      },
    });
    return unregister;
  }, [
    deleteScopeVersion,
    pruneHydrusFiles,
    restoreHydrusFiles,
  ]);

  useEffect(() => {
    const registrationGeneration = deleteScopeGenerationRef.current;
    const handlers = {
      hide: (paths: string[]) =>
        hideExplorerDeletePaths(paths, registrationGeneration),
      restore: (paths: string[]) =>
        restoreExplorerDeletePaths(paths, registrationGeneration),
    };
    const unregister = registerExplorerDeleteViewHandlers(handlers);
    return () => {
      // Do not let a handle retained by an unmounted Provider touch a future
      // Provider.  State setters are intentionally skipped during unmount.
      if (deleteScopeGenerationRef.current === registrationGeneration) {
        invalidateExplorerDeleteScope(false);
      }
      unregister();
    };
  }, [
    hideExplorerDeletePaths,
    invalidateExplorerDeleteScope,
    restoreExplorerDeletePaths,
    deleteScopeVersion,
  ]);

  const clearNavigationHistory = useCallback(() => {
    historyBackRef.current = [];
    historyForwardRef.current = [];
    logicalNavigationCursorRef.current = null;
    setHistoryScrollRestore(null);
    // Navigation history is scoped to the active source/project.  Clear the
    // associated virtual view state at the same boundary so a tab or project
    // switch cannot briefly render the previous source's focused item.
    if (
      selectedItemsRef.current.size > 0 ||
      focusedItemPathRef.current !== null
    ) {
      const nextSelection = new Set<string>();
      selectedItemsRef.current = nextSelection;
      focusedItemPathRef.current = null;
      setSelectedItems(nextSelection);
      setFocusedItemPath(null);
    }
    selectionAnchorPathRef.current = null;
    previousShiftRangeRef.current = new Set();
  }, []);

  const captureCurrentView = useCallback(
    (): ExplorerHistoryEntry => {
      const logicalCursor = logicalNavigationCursorRef.current;
      if (logicalCursor?.pending) {
        return cloneHistoryEntry(logicalCursor.entry);
      }
      return {
        path:
          currentPathRef.current || logicalCursor?.entry.path || "",
        focusedItemPath: focusedItemPathRef.current,
        selectedItemPaths: Array.from(selectedItemsRef.current),
      };
    },
    [],
  );

  const captureStableHistoryBefore = useCallback(
    (): LogicalNavigationHistorySnapshot => {
      // A burst of intents is one logical transaction until the latest
      // request commits.  Reuse the first intent's snapshot so a failure in a
      // later queued request rolls the entire burst back atomically.
      const logicalCursor = logicalNavigationCursorRef.current;
      const source =
        logicalCursor?.pending && logicalCursor.historyBefore
          ? logicalCursor.historyBefore
          : {
              back: historyBackRef.current,
              forward: historyForwardRef.current,
            };
      return {
        back: source.back.map(cloneHistoryEntry),
        forward: source.forward.map(cloneHistoryEntry),
      };
    },
    [],
  );

  // A logout/login switch must not leave the previous user's in-memory HF or
  // Hydrus data visible while the new principal is loading. Persistent state is
  // namespaced by user id; in-memory state is cleared immediately here.
  useEffect(() => {
    const previous = previousUserIdRef.current;
    if (previous !== userId) {
      principalGenerationRef.current += 1;
      // A principal switch is also a navigation identity change.  Rename
      // refresh callbacks may outlive the session transition; advancing the
      // epoch makes their captured refresh epoch stale before they can
      // restore a path/selection from the previous user or HF account.
      const nextNavigationEpoch = navigationEpochRef.current + 1;
      navigationEpochRef.current = nextNavigationEpoch;
      setNavigationEpoch(nextNavigationEpoch);
      pendingFetchRef.current = null;
      setBrowseDataState(null);
      setDataPrincipalId(null);
      setLoading(false);
      setCurrentPath("");
      currentPathRef.current = "";
      setHydrusHiddenFileIds(new Set());
      invalidateExplorerDeleteScope();
      setSelectedItems(new Set());
      selectedItemsRef.current = new Set();
      setFocusedItemPath(null);
      focusedItemPathRef.current = null;
      setHfCreatorMapping(null);
      setHfSearchQuery("");
      clearNavigationHistory();
      clearFilerUndoHistory();
      loadedRepoKeyRef.current = null;
      setHfOwnedAccountIds(new Set());
    }
    previousUserIdRef.current = userId;
  }, [clearNavigationHistory, invalidateExplorerDeleteScope, userId]);

  // Account ownership is resolved from the authenticated user's DB-backed
  // account list; the opaque account id is never trusted from the URL alone.
  useEffect(() => {
    let cancelled = false;
    if (!userId) {
      setHfOwnedAccountIds(new Set());
      return () => {
        cancelled = true;
      };
    }
    const refreshAccounts = () => {
      void fetch("/api/huggingface/accounts", { credentials: "include" })
        .then(async (response) => {
          if (!response.ok) return [];
          const body = (await response.json().catch(() => ({}))) as {
            accounts?: Array<{ id?: unknown }>;
          };
          return Array.isArray(body.accounts)
            ? body.accounts.flatMap((account) =>
                typeof account.id === "string" ? [account.id] : [],
              )
            : [];
        })
        .then((ids) => {
          if (!cancelled) setHfOwnedAccountIds(new Set(ids));
        })
        .catch(() => {
          if (!cancelled) setHfOwnedAccountIds(new Set());
        });
    };
    refreshAccounts();
    window.addEventListener("aoitalk:hf-accounts-changed", refreshAccounts);
    return () => {
      cancelled = true;
      window.removeEventListener("aoitalk:hf-accounts-changed", refreshAccounts);
    };
  }, [userId]);

  // 現在位置が絶対パスでも失わない、タブ固有のホーム遷移先。
  const homeRootPath = useMemo(() => {
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
  }, [filerTab, selectedProject, selectedProjectId, userId]);

  // パンくず・上位移動境界用。絶対パス閲覧中は従来どおり境界を設けない。
  const contextRootPath = isAbsoluteFilerPath ? "" : homeRootPath;
  // 選択プロジェクトではなく、実際に表示中のパスでremote/localを区別する。
  // remote project選択中でもlocal absolute bookmarkへ移動した場合はlocal操作を許可する。
  const isRemoteWorkspace = isRemoteFilerPath(currentPath);

  // Project Files are shared by the selected Space.  User Files and legacy
  // virtual/remote sources intentionally retain the personal collection (or
  // disable durable bookmark UI entirely in the sidebar).  Do not infer a
  // Space from a `_projects/...` path: selectedSpaceId is the canonical
  // ProjectContext identity.
  const bookmarkScope = useMemo<ExplorerBookmarkScope>(() => {
    if (
      filerTab === "workspace" &&
      selectedSpaceId &&
      !isRemoteWorkspace &&
      selectedProject?.source !== "remote"
    ) {
      return { scope: "shared", spaceId: selectedSpaceId };
    }
    return { scope: "personal" };
  }, [filerTab, isRemoteWorkspace, selectedProject?.source, selectedSpaceId]);

  // The sidebar needs the complete set of Project roots in the selected
  // Space.  Keep this derived solely from ProjectContext's authoritative
  // Use the server-authorized accessible Project list as the primary source so
  // admins can open shared targets in Projects where they are not explicit
  // members.  Regular users still receive only ACL-filtered rows from the
  // backend projection; this list is an execution allowlist, not a substitute
  // for server-side ProjectRepository.has_permission checks.
  const spaceProjectIds = useMemo(
    () => {
      if (!selectedSpaceId) return [];
      const candidates = [...accessibleProjects, ...participatingProjects];
      return Array.from(
        new Set(
          candidates
            .filter((project) => project.space_id === selectedSpaceId)
            .map((project) => project.id),
        ),
      );
    },
    [accessibleProjects, participatingProjects, selectedSpaceId],
  );
  const spaceProjectTargetMap = useMemo(
    () =>
      Object.fromEntries(
        spaceProjectIds.map((projectId) => [workspaceRoot(projectId), projectId]),
      ),
    [spaceProjectIds],
  );

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
        return readLastPath(tab, uid) ?? HF_PREFIX;
      }
      return null;
    },
    [selectedProject, selectedProjectId, userId],
  );

  const rememberCurrentPath = useCallback(
    (path: string, principalId: string | null = userId) => {
      if (!path || isAbsolutePath(path)) return;
      if (isHfPath(path)) {
        writeLastPath("hf", path, principalId);
        return;
      }
      // Derive the workspace owner from the path itself.  A shared bookmark
      // may temporarily open an admin-only Project while the canonical
      // ProjectContext selection remains on a participating Project; using
      // selectedProjectId here would persist that cross-Project path under
      // the wrong restore key.
      const pathProjectId = workspaceProjectIdFromPath(path);
      if (pathProjectId) {
        const isAllowedProjectPath =
          pathProjectId === selectedProjectId ||
          accessibleProjects.some(
            (project) =>
              project.id === pathProjectId &&
              project.space_id === selectedSpaceId,
          );
        if (isAllowedProjectPath) {
          writeLastPath("workspace", path, pathProjectId);
        }
        return;
      }
      if (principalId && path.startsWith(userRoot(principalId))) {
        writeLastPath("user", path, principalId);
        return;
      }
      const activeTab = activeFilerTabRef.current;
      writeLastPath(
        activeTab,
        path,
        activeTab === "user" ? principalId : selectedProjectId,
      );
    },
    [accessibleProjects, selectedProjectId, selectedSpaceId, userId],
  );

  const bumpNavigationEpoch = useCallback(() => {
    const next = navigationEpochRef.current + 1;
    navigationEpochRef.current = next;
    // Refresh callbacks capture the render-time epoch.  Publish the new
    // value so callbacks created after navigation observe it as well.
    setNavigationEpoch(next);
    return next;
  }, []);

  // ディレクトリ読み込み（explorer API or filer path API or HF API を自動判定）
  const fetchDirectory = useCallback(
    async (
      path: string,
      requestedPrincipalId: string | null = userId,
      requestedNavigationEpoch: number = navigationEpochRef.current,
      restoreViewState: ExplorerHistoryEntry | null = null,
    ) => {
      const generation = principalGenerationRef.current;
      const request: DirectoryFetchRequest = {
        path,
        principalId: requestedPrincipalId,
        generation,
        navigationEpoch: requestedNavigationEpoch,
        restoreViewState,
      };
      // A restore signal describes exactly one completed request.  Clear any
      // prior signal as soon as a newer request supersedes it; a successful
      // restore below will publish a fresh, request-scoped signal.
      setHistoryScrollRestore(null);
      const activeFetch = activeFetchRef.current;
      if (activeFetch?.generation === generation) {
        const pending = pendingFetchRef.current;
        if (
          !pending ||
          pending.path !== request.path ||
          pending.principalId !== request.principalId ||
          pending.generation !== request.generation ||
          pending.navigationEpoch !== request.navigationEpoch ||
          !historyEntriesEqual(pending.restoreViewState, request.restoreViewState)
        ) {
          pendingFetchRef.current = request;
        }
        return activeFetch.promise;
      }

      // `run()` can finish synchronously when its request is already stale
      // (there is no awaited network operation in that branch).  Keep this
      // guard so the post-start registration cannot resurrect a completed
      // promise after the stale branch has released the active slot.
      let completedSynchronously = false;
      const run = async () => {
        const rollbackPendingNavigation = (requestNavigationEpoch: number) => {
          const logicalCursor = logicalNavigationCursorRef.current;
          if (
            !logicalCursor?.pending ||
            logicalCursor.navigationEpoch !== requestNavigationEpoch
          ) {
            return;
          }
          const historyBefore = logicalCursor.historyBefore;
          if (historyBefore) {
            historyBackRef.current = historyBefore.back.map(cloneHistoryEntry);
            historyForwardRef.current = historyBefore.forward.map(cloneHistoryEntry);
          }
          logicalNavigationCursorRef.current = {
            entry: {
              path: currentPathRef.current,
              focusedItemPath: focusedItemPathRef.current,
              selectedItemPaths: Array.from(selectedItemsRef.current),
            },
            navigationEpoch: requestNavigationEpoch,
            pending: false,
          };
        };
        let nextRequest: DirectoryFetchRequest | null = request;
        while (nextRequest !== null) {
          const targetPath = nextRequest.path;
          const targetPrincipalId = nextRequest.principalId;
          const requestGeneration: number = nextRequest.generation;
          const requestNavigationEpoch = nextRequest.navigationEpoch;
          const requestRestoreViewState = nextRequest.restoreViewState;
          nextRequest = null;
          if (requestNavigationEpoch !== navigationEpochRef.current) {
            // A request can become stale while it is queued (for example,
            // A→B navigation followed by a tab switch that has no directory
            // request).  Drain only a pending request from the current epoch;
            // otherwise release the completed active slot so future fetches
            // cannot remain permanently short-circuited by the stale promise.
            const pending = pendingFetchRef.current;
            if (
              pending?.generation === requestGeneration &&
              pending.navigationEpoch === navigationEpochRef.current
            ) {
              pendingFetchRef.current = null;
              nextRequest = pending;
              continue;
            }
            if (pending?.generation === requestGeneration) {
              pendingFetchRef.current = null;
            }
            if (activeFetchRef.current?.generation === requestGeneration) {
              activeFetchRef.current = null;
              setLoading(false);
            }
            continue;
          }
          setLoading(true);
          setError(null);

          const useHf = isHfPath(targetPath);
          const useAbsoluteFilerPath = !useHf && isAbsolutePath(targetPath);

          try {
            let successfulData: ExplorerListResponse | null = null;
            if (useHf) {
              const data = await hfExplorerList(targetPath);
              if (
                requestGeneration !== principalGenerationRef.current ||
                requestNavigationEpoch !== navigationEpochRef.current
              ) continue;
              setIsAbsoluteFilerPath(false);
              setIsHfMode(true);
              setIsHydrusMode(false);
              setBrowseDataForPrincipal(data, targetPrincipalId);
              setCurrentPath(data.current_path);
              currentPathRef.current = data.current_path;
              rememberCurrentPath(data.current_path, targetPrincipalId);
              successfulData = data;

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
                    if (
                      requestGeneration === principalGenerationRef.current &&
                      loadedRepoKeyRef.current === key
                    ) {
                      setHfCreatorMapping(m);
                    }
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
              if (
                requestGeneration !== principalGenerationRef.current ||
                requestNavigationEpoch !== navigationEpochRef.current
              ) continue;
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
                  size:
                    typeof item.size_bytes === "number"
                      ? item.size_bytes
                      : typeof item.size === "number"
                        ? item.size
                        : undefined,
                  modified_at: typeof item.modified_at === "string" ? item.modified_at : undefined,
                })),
                total_items: remoteData.total_items ?? 0,
              };
              setIsAbsoluteFilerPath(true);
              setIsHfMode(false);
              setIsHydrusMode(false);
              setBrowseDataForPrincipal(data, targetPrincipalId);
              setCurrentPath(data.current_path);
              currentPathRef.current = data.current_path;
              rememberCurrentPath(data.current_path, targetPrincipalId);
              successfulData = data;
            } else if (useAbsoluteFilerPath && isAdmin) {
              const data = await explorerList(targetPath);
              if (
                requestGeneration !== principalGenerationRef.current ||
                requestNavigationEpoch !== navigationEpochRef.current
              ) continue;
              setIsAbsoluteFilerPath(true);
              setIsHfMode(false);
              setIsHydrusMode(false);
              setBrowseDataForPrincipal(data, targetPrincipalId);
              setCurrentPath(data.current_path);
              currentPathRef.current = data.current_path;
              rememberCurrentPath(data.current_path, targetPrincipalId);
              successfulData = data;
            } else if (useAbsoluteFilerPath) {
              await filerBrowse(targetPath);
              if (
                requestGeneration !== principalGenerationRef.current ||
                requestNavigationEpoch !== navigationEpochRef.current
              ) continue;
              throw new Error("absolute path access denied");
            } else {
              const data = await explorerList(targetPath);
              if (
                requestGeneration !== principalGenerationRef.current ||
                requestNavigationEpoch !== navigationEpochRef.current
              ) continue;
              setIsAbsoluteFilerPath(false);
              setIsHfMode(false);
              setIsHydrusMode(false);
              setBrowseDataForPrincipal(data, targetPrincipalId);
              setCurrentPath(data.current_path);
              currentPathRef.current = data.current_path;
              rememberCurrentPath(data.current_path, targetPrincipalId);
              successfulData = data;
            }
            if (!successfulData) {
              // All successful directory branches above provide a response;
              // retain the existing selection if a future source adds a
              // branch that does not return one.
              rollbackPendingNavigation(requestNavigationEpoch);
              logicalNavigationCursorRef.current = {
                entry: {
                  path: currentPathRef.current,
                  focusedItemPath: focusedItemPathRef.current,
                  selectedItemPaths: Array.from(selectedItemsRef.current),
                },
                navigationEpoch: requestNavigationEpoch,
                pending: false,
              };
              setHistoryScrollRestore(null);
              continue;
            }
            const restoreViewState = requestRestoreViewState;
            const canRestoreViewState =
              restoreViewState !== null &&
              normalizeExplorerPath(restoreViewState.path) ===
                normalizeExplorerPath(successfulData.current_path);
            if (canRestoreViewState && restoreViewState) {
              const canonicalItems = new Map<string, string>();
              const hiddenRestorePaths = new Set([
                ...pendingDeletedPathsRef.current,
                ...committedDeletedPathsRef.current,
              ].map(normalizeExplorerPath));
              for (const item of [
                ...successfulData.directories,
                ...successfulData.files,
              ]) {
                const normalizedPath = normalizeExplorerPath(item.path);
                if (!hiddenRestorePaths.has(normalizedPath)) {
                  canonicalItems.set(normalizedPath, item.path);
                }
              }
              const restoredSelectedPaths: string[] = [];
              for (const path of restoreViewState.selectedItemPaths) {
                const canonicalPath = canonicalItems.get(normalizeExplorerPath(path));
                if (canonicalPath && !restoredSelectedPaths.includes(canonicalPath)) {
                  restoredSelectedPaths.push(canonicalPath);
                }
              }
              const restoredFocusedPath = restoreViewState.focusedItemPath
                ? canonicalItems.get(
                    normalizeExplorerPath(restoreViewState.focusedItemPath),
                  ) ?? null
                : null;
              const nextFocusedPath =
                restoredFocusedPath ?? restoredSelectedPaths[0] ?? null;
              const nextSelectedItems = new Set(restoredSelectedPaths);
              selectedItemsRef.current = nextSelectedItems;
              focusedItemPathRef.current = nextFocusedPath;
              setSelectedItems(nextSelectedItems);
              setFocusedItemPath(nextFocusedPath);
              selectionAnchorPathRef.current = nextFocusedPath;
              previousShiftRangeRef.current = new Set();
              logicalNavigationCursorRef.current = {
                entry: {
                  path: successfulData.current_path,
                  focusedItemPath: nextFocusedPath,
                  selectedItemPaths: [...restoredSelectedPaths],
                },
                navigationEpoch: requestNavigationEpoch,
                pending: false,
              };
              setHistoryScrollRestore(
                nextFocusedPath
                  ? {
                      navigationEpoch: requestNavigationEpoch,
                      directoryPath: successfulData.current_path,
                      focusedItemPath: nextFocusedPath,
                    }
                  : null,
              );
            } else {
              selectedItemsRef.current = new Set();
              focusedItemPathRef.current = null;
              setSelectedItems(new Set());
              setFocusedItemPath(null);
              selectionAnchorPathRef.current = null;
              previousShiftRangeRef.current = new Set();
              logicalNavigationCursorRef.current = {
                entry: emptyHistoryEntry(successfulData.current_path),
                navigationEpoch: requestNavigationEpoch,
                pending: false,
              };
              setHistoryScrollRestore(null);
            }
          } catch {
            if (
              requestGeneration === principalGenerationRef.current &&
              requestNavigationEpoch === navigationEpochRef.current
            ) {
              setError("ディレクトリの読み込みに失敗しました");
              rollbackPendingNavigation(requestNavigationEpoch);
            }
          } finally {
            if (
              requestGeneration === principalGenerationRef.current &&
              requestNavigationEpoch === navigationEpochRef.current
            ) {
              setLoading(false);
            }
            const active = activeFetchRef.current;
            if (active?.generation !== requestGeneration) {
              // A newer principal owns the active slot. Its request must not
              // be delayed or have its pending navigation consumed by this
              // stale request.
              nextRequest = null;
            } else {
              const pending = pendingFetchRef.current;
              if (pending?.generation === requestGeneration) {
                pendingFetchRef.current = null;
                nextRequest = pending;
              } else {
                pendingFetchRef.current = null;
                nextRequest = null;
                activeFetchRef.current = null;
              }
            }
          }
        }
        completedSynchronously = true;
      };
      const promise = run();
      if (!completedSynchronously) {
        activeFetchRef.current = { generation, promise };
      }
      return promise;
    },
    [
      isAdmin,
      rememberCurrentPath,
      selectedProject,
      setBrowseDataForPrincipal,
      userId,
    ],
  );

  const navigate = useCallback(
    (path: string) => {
      const currentView = captureCurrentView();
      const historyBefore = captureStableHistoryBefore();
      if (
        currentView.path &&
        normalizeExplorerPath(currentView.path) !== normalizeExplorerPath(path)
      ) {
        historyBackRef.current = [
          ...historyBackRef.current,
          currentView,
        ];
        historyForwardRef.current = [];
      }
      const navigationEpoch = bumpNavigationEpoch();
      logicalNavigationCursorRef.current = {
        entry: emptyHistoryEntry(path),
        navigationEpoch,
        pending: true,
        historyBefore,
      };
      fetchDirectory(path, userId, navigationEpoch);
    },
    [
      bumpNavigationEpoch,
      captureCurrentView,
      captureStableHistoryBefore,
      fetchDirectory,
      userId,
    ],
  );

  const goBack = useCallback(() => {
    const previousEntry = historyBackRef.current.at(-1);
    if (!previousEntry) return;

    const historyBefore = captureStableHistoryBefore();
    historyBackRef.current = historyBackRef.current.slice(0, -1);
    const currentView = captureCurrentView();
    if (
      currentView.path &&
      normalizeExplorerPath(currentView.path) !==
        normalizeExplorerPath(previousEntry.path)
    ) {
      historyForwardRef.current = [
        currentView,
        ...historyForwardRef.current,
      ];
    }
    const navigationEpoch = bumpNavigationEpoch();
    logicalNavigationCursorRef.current = {
      entry: cloneHistoryEntry(previousEntry),
      navigationEpoch,
      pending: true,
      historyBefore,
    };
    fetchDirectory(
      previousEntry.path,
      userId,
      navigationEpoch,
      previousEntry,
    );
  }, [
    bumpNavigationEpoch,
    captureCurrentView,
    captureStableHistoryBefore,
    fetchDirectory,
    userId,
  ]);

  const goForward = useCallback(() => {
    const nextEntry = historyForwardRef.current[0];
    if (!nextEntry) return;

    const historyBefore = captureStableHistoryBefore();
    historyForwardRef.current = historyForwardRef.current.slice(1);
    const currentView = captureCurrentView();
    if (
      currentView.path &&
      normalizeExplorerPath(currentView.path) !== normalizeExplorerPath(nextEntry.path)
    ) {
      historyBackRef.current = [
        ...historyBackRef.current,
        currentView,
      ];
    }
    const navigationEpoch = bumpNavigationEpoch();
    logicalNavigationCursorRef.current = {
      entry: cloneHistoryEntry(nextEntry),
      navigationEpoch,
      pending: true,
      historyBefore,
    };
    fetchDirectory(nextEntry.path, userId, navigationEpoch, nextEntry);
  }, [
    bumpNavigationEpoch,
    captureCurrentView,
    captureStableHistoryBefore,
    fetchDirectory,
    userId,
  ]);

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

  const refreshNavigationEpoch = navigationEpochRef.current;
  const refresh = useCallback(async () => {
    // A rename started in directory A may resolve after the user navigates to
    // B.  The render-time epoch is intentionally captured here; the callback
    // then becomes a no-op when the live navigation identity has advanced.
    if (navigationEpochRef.current !== refreshNavigationEpoch) return false;
    await fetchDirectory(currentPath, userId, refreshNavigationEpoch);
    return navigationEpochRef.current === refreshNavigationEpoch;
  }, [currentPath, fetchDirectory, refreshNavigationEpoch, userId]);

  // 選択
  const selectItem = useCallback((path: string) => {
    const next = new Set([path]);
    selectedItemsRef.current = next;
    focusedItemPathRef.current = path;
    setSelectedItems(next);
    setFocusedItemPath(path);
    selectionAnchorPathRef.current = path;
    previousShiftRangeRef.current = new Set();
  }, []);

  const toggleSelect = useCallback((path: string) => {
    const next = new Set(selectedItemsRef.current);
    if (next.has(path)) next.delete(path);
    else next.add(path);
    selectedItemsRef.current = next;
    focusedItemPathRef.current = path;
    setSelectedItems(next);
    setFocusedItemPath(path);
    selectionAnchorPathRef.current = path;
    previousShiftRangeRef.current = new Set();
  }, []);

  const selectRange = useCallback(
    (path: string, orderedPaths: string[], additive = false) => {
      const result = buildExplorerRangeSelection({
        orderedPaths,
        anchorPath: selectionAnchorPathRef.current,
        targetPath: path,
        selectedPaths: selectedItemsRef.current,
        previousShiftRange: previousShiftRangeRef.current,
        additive,
      });
      selectedItemsRef.current = result.selectedPaths;
      focusedItemPathRef.current = path;
      setSelectedItems(result.selectedPaths);
      selectionAnchorPathRef.current = result.anchorPath;
      previousShiftRangeRef.current = result.shiftRange;
      setFocusedItemPath(path);
    },
    [],
  );

  const selectAll = useCallback((paths?: string[]) => {
    if (!browseData && !paths) return;
    const allPaths =
      paths ??
      [
        ...(browseData?.directories ?? []).map((d) => d.path),
        ...(browseData?.files ?? []).map((f) => f.path),
      ];
    const all = new Set(allPaths);
    const nextFocusedPath =
      focusedItemPathRef.current && all.has(focusedItemPathRef.current)
        ? focusedItemPathRef.current
        : allPaths[0] ?? null;
    selectedItemsRef.current = all;
    focusedItemPathRef.current = nextFocusedPath;
    setSelectedItems(all);
    setFocusedItemPath(nextFocusedPath);
    selectionAnchorPathRef.current =
      nextFocusedPath;
    previousShiftRangeRef.current = new Set();
  }, [browseData]);

  const clearSelection = useCallback((paths?: string[]) => {
    if (paths === undefined) {
      const next = new Set<string>();
      selectedItemsRef.current = next;
      focusedItemPathRef.current = null;
      setSelectedItems(next);
      setFocusedItemPath(null);
      selectionAnchorPathRef.current = null;
      previousShiftRangeRef.current = new Set();
      return;
    }
    if (paths.length === 0) return;
    const deletedPaths = new Set(paths);
    const nextSelected = new Set(selectedItemsRef.current);
    for (const path of deletedPaths) nextSelected.delete(path);
    const nextFocused =
      focusedItemPathRef.current && deletedPaths.has(focusedItemPathRef.current)
        ? null
        : focusedItemPathRef.current;
    selectedItemsRef.current = nextSelected;
    focusedItemPathRef.current = nextFocused;
    setSelectedItems(nextSelected);
    setFocusedItemPath(nextFocused);
    if (
      selectionAnchorPathRef.current &&
      deletedPaths.has(selectionAnchorPathRef.current)
    ) {
      selectionAnchorPathRef.current = null;
    }
    previousShiftRangeRef.current = new Set(
      [...previousShiftRangeRef.current].filter(
        (path) => !deletedPaths.has(path),
      ),
    );
  }, []);

  // ブックマーク（取得・キャッシュ・重複排除を SWR に委譲）。
  //
  // Project Files の表示中は Space 共有コレクションを主コレクションと
  // して扱いながら、個人コレクションも常に同時に取得する。これにより、
  // 外部/local パスを個人所有として表示・操作でき、Space の共有境界を
  // 越えて個人項目を誤って扱うこともない。
  // 取得失敗を空配列へ変換すると、登録直後の一時的なGET失敗が既存項目を
  // 消えたように見せてしまう。例外をSWRへ返し、前回データを保持させる。
  type BookmarkSWRKey = readonly [
    typeof BOOKMARKS_SWR_KEY,
    string,
    string,
    string | null,
  ];
  // `null` is a valid principal in the auth-disabled development runtime.
  // Keep the cache identity distinct from authenticated UUIDs while still
  // allowing that runtime to use the personal/default collection.  During the
  // initial `undefined` shell hand-off, defer fetching until the provider has
  // supplied an explicit value so an anonymous first render cannot race the
  // authenticated principal.
  const bookmarkPrincipalReady = sessionUserId !== undefined || userId !== null;
  const bookmarkPrincipalIdentity = userId ?? "anonymous";
  const bookmarksCurrentPrincipalRef = useRef(bookmarkPrincipalIdentity);
  const bookmarksCurrentSharedIdentityRef = useRef<string | null>(
    bookmarkScope.scope === "shared"
      ? bookmarkScopeIdentity(bookmarkScope)
      : null,
  );
  // Keep the latest principal and active Space available to an old
  // mutate/fetcher closure even in the render→effect window during an auth or
  // Space switch. Personal responses intentionally do not depend on Space.
  bookmarksCurrentPrincipalRef.current = bookmarkPrincipalIdentity;
  bookmarksCurrentSharedIdentityRef.current =
    bookmarkScope.scope === "shared" ? bookmarkScopeIdentity(bookmarkScope) : null;

  const bookmarksFetcher = useCallback(
    async (key: BookmarkSWRKey): Promise<ExplorerBookmark[]> => {
      const requestPrincipal = key[1];
      const requestScopeIdentity = key[2];
      const requestSpaceId = key[3];
      const requestScope: ExplorerBookmarkScope =
        requestScopeIdentity.startsWith("shared:") && requestSpaceId
          ? { scope: "shared", spaceId: requestSpaceId }
          : { scope: "personal" };
      const isCurrentRequest = () =>
        requestPrincipal === bookmarksCurrentPrincipalRef.current &&
        (requestScope.scope !== "shared" ||
          requestScopeIdentity === bookmarksCurrentSharedIdentityRef.current);

      if (!isCurrentRequest()) return [];
      try {
        const data = await explorerBookmarks(requestScope);
        if (!data.success) {
          throw new Error("ブックマーク一覧の取得に失敗しました");
        }
        if (!Array.isArray(data.bookmarks)) {
          throw new Error("ブックマーク一覧の形式が不正です");
        }
        // A principal/Space switch while the request was in flight must not
        // write the old response into the new user's or Space's UI/cache.
        if (!isCurrentRequest()) return [];
        return data.bookmarks;
      } catch (error) {
        if (!isCurrentRequest()) return [];
        console.error("[Files] ブックマーク一覧の取得に失敗しました:", error);
        toast.error(
          `ブックマーク一覧を取得できませんでした: ${
            error instanceof Error ? error.message : "不明なエラーです"
          }`,
        );
        throw error;
      }
    },
    [],
  );

  const bookmarksScopeIdentity = bookmarkScopeIdentity(bookmarkScope);
  const personalBookmarksSWRKey = bookmarkPrincipalReady
    ? ([BOOKMARKS_SWR_KEY, bookmarkPrincipalIdentity, "personal:", null] as const)
    : null;
  // Only the active local Project Space has a shared collection in this
  // provider. Remote, User, HF, and Hydrus tabs intentionally expose null.
  const sharedBookmarksSWRKey =
    bookmarkPrincipalReady && bookmarkScope.scope === "shared"
      ? ([
          BOOKMARKS_SWR_KEY,
          bookmarkPrincipalIdentity,
          bookmarksScopeIdentity,
          bookmarkScope.spaceId,
        ] as const)
      : null;
  const swrOptions = {
    // 取得タイミングを従来実装（refreshBookmarks 呼び出し）に一致させるため、
    // SWR の自動 revalidation は全て無効化し、全ての取得を refreshBookmarks
    // または下記の scope 切り替え effect 経由にする。
    revalidateOnMount: false,
    revalidateOnFocus: false,
    revalidateOnReconnect: false,
    revalidateIfStale: false,
    keepPreviousData: false,
    dedupingInterval: 0,
  } as const;
  const { data: personalBookmarksData, mutate: mutatePersonalBookmarks } = useSWR<
    ExplorerBookmark[]
  >(personalBookmarksSWRKey, bookmarksFetcher, swrOptions);
  const { data: sharedBookmarksData, mutate: mutateSharedBookmarks } = useSWR<
    ExplorerBookmark[]
  >(sharedBookmarksSWRKey, bookmarksFetcher, swrOptions);

  const personalBookmarks = personalBookmarksData ?? EMPTY_BOOKMARKS;
  const sharedBookmarks = sharedBookmarksData ?? EMPTY_BOOKMARKS;
  const bookmarkCollections = useMemo<ExplorerBookmarkCollections>(
    () => ({
      personal: personalBookmarks,
      shared:
        bookmarkScope.scope === "shared"
          ? { spaceId: bookmarkScope.spaceId, bookmarks: sharedBookmarks }
          : null,
    }),
    [bookmarkScope, personalBookmarks, sharedBookmarks],
  );
  // `bookmarks` remains the compatibility view used by existing callers. Its
  // owner is explicit in `bookmarkScope`; the bridge additionally publishes
  // both collections above for owner-aware CRUD in the sidebar.
  const bookmarks =
    bookmarkScope.scope === "shared" ? sharedBookmarks : personalBookmarks;

  // Start empty so the first authenticated render revalidates both required
  // collections. Subsequent renders are deduplicated by principal/Space
  // identity rather than by callback identity.
  const bookmarksPersonalRevalidatedPrincipalRef = useRef<string | null>(null);
  const bookmarksSharedRevalidatedIdentityRef = useRef<string | null>(null);
  useEffect(() => {
    if (!bookmarkPrincipalReady) {
      bookmarksPersonalRevalidatedPrincipalRef.current = null;
      bookmarksSharedRevalidatedIdentityRef.current = null;
      return;
    }

    if (
      bookmarksPersonalRevalidatedPrincipalRef.current !== bookmarkPrincipalIdentity
    ) {
      bookmarksPersonalRevalidatedPrincipalRef.current = bookmarkPrincipalIdentity;
      // Personal bookmarks are fetched for every authenticated principal,
      // including while the active tab is a shared Project workspace. The
      // explicit anonymous identity is also used by auth-disabled development.
      void mutatePersonalBookmarks().catch((error: unknown) => {
        console.error("[Files] principal変更後の個人ブックマーク取得に失敗しました:", error);
        toast.error(
          `ブックマーク一覧を取得できませんでした: ${
            error instanceof Error ? error.message : "不明なエラーです"
          }`,
        );
      });
    }

    const sharedIdentity =
      bookmarkScope.scope === "shared"
        ? `${bookmarkPrincipalIdentity}|${bookmarksScopeIdentity}`
        : null;
    if (!sharedIdentity) {
      bookmarksSharedRevalidatedIdentityRef.current = null;
      return;
    }
    if (bookmarksSharedRevalidatedIdentityRef.current === sharedIdentity) return;
    bookmarksSharedRevalidatedIdentityRef.current = sharedIdentity;
    void mutateSharedBookmarks().catch((error: unknown) => {
      console.error("[Files] Space変更後の共有ブックマーク取得に失敗しました:", error);
      toast.error(
        `ブックマーク一覧を取得できませんでした: ${
          error instanceof Error ? error.message : "不明なエラーです"
        }`,
      );
    });
  }, [
    bookmarkScope,
    bookmarksScopeIdentity,
    mutatePersonalBookmarks,
    mutateSharedBookmarks,
    bookmarkPrincipalIdentity,
    bookmarkPrincipalReady,
  ]);

  // Revalidate one owning collection.  Callers mutating a personal item while
  // Project Files are active must pass `{ scope: "personal" }`; omitting the
  // owner preserves the historical active-collection behavior.
  const refreshBookmarks = useCallback(
    async (owner: ExplorerBookmarkScope = bookmarkScope): Promise<void> => {
      if (!bookmarkPrincipalReady) return;
      if (owner.scope === "personal") {
        await mutatePersonalBookmarks();
        return;
      }
      if (
        bookmarkScope.scope !== "shared" ||
        owner.spaceId !== bookmarkScope.spaceId
      ) {
        // The provider only mounts the selected Space's shared collection. Do
        // not accidentally refresh a different Space's endpoint during a
        // transition; its provider will revalidate once it becomes active.
        return;
      }
      await mutateSharedBookmarks();
    },
    [
      bookmarkPrincipalReady,
      bookmarkScope,
      mutatePersonalBookmarks,
      mutateSharedBookmarks,
    ],
  );

  // タブ / パス種別ごとの操作可否。削除・リネーム・移動の判定はここに一元化する。
  const capabilities = useMemo(
    () => {
      const parsedHf = isHfMode ? parseHfPath(currentPath) : null;
      const hfOwnAccount = Boolean(
        parsedHf?.kind === "repo" &&
          parsedHf.accountId &&
          hfOwnedAccountIds.has(parsedHf.accountId),
      );
      return resolveFilerCapabilities({
        filerTab,
        isAbsoluteFilerPath,
        isRemoteWorkspace,
        isHfMode,
        isHydrusMode,
        isAdmin,
        hfOwnAccount,
      });
    },
    [
      filerTab,
      currentPath,
      isAbsoluteFilerPath,
      isRemoteWorkspace,
      isHfMode,
      isHydrusMode,
      isAdmin,
      hfOwnedAccountIds,
    ],
  );

  // タブ切り替え
  const setFilerTab = useCallback(
    (tab: FilerTab) => {
      // タブをまたいだ Undo は復元先が食い違うためスタックを全消去する
      clearFilerUndoHistory();
      setHydrusHiddenFileIds(new Set());
      invalidateExplorerDeleteScope();
      clearNavigationHistory();
      activeFilerTabRef.current = tab;
      setFilerTabState(tab);
      setIsAbsoluteFilerPath(false);
      writeLocalStorage(FILER_TAB_STORAGE_KEY, tab);
      const navigationEpoch = bumpNavigationEpoch();
      const restorePath = initialPathForTab(tab);
      if (tab === "workspace" && restorePath) {
        setIsHfMode(false);
        setIsHydrusMode(false);
        fetchDirectory(restorePath, userId, navigationEpoch);
      } else if (tab === "user" && restorePath) {
        setIsHfMode(false);
        setIsHydrusMode(false);
        fetchDirectory(restorePath, userId, navigationEpoch);
      } else if (tab === "hf") {
        setIsHfMode(true);
        setIsHydrusMode(false);
        fetchDirectory(restorePath ?? HF_PREFIX, userId, navigationEpoch);
      } else if (tab === "hydrus") {
        setIsHfMode(false);
        setIsHydrusMode(true);
        // Hydrus は ExplorerListResponse にマップしにくいので、
        // browseData を空にして専用検索UI（filer/page.tsx 側）から駆動する
        setBrowseDataForPrincipal({
          success: true,
          current_path: "Hydrus",
          parent_path: null,
          can_go_up: false,
          directories: [],
          files: [],
          total_items: 0,
        });
        setCurrentPath("Hydrus");
        currentPathRef.current = "Hydrus";
        setLoading(false);
      }
    },
    [
      bumpNavigationEpoch,
      clearNavigationHistory,
      fetchDirectory,
      initialPathForTab,
      invalidateExplorerDeleteScope,
      setBrowseDataForPrincipal,
      userId,
    ],
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
        // Auth status is authoritative when it returns a principal.  If the
        // app shell already supplied one, keep that scope while the context
        // fallback is queried so a transient unauthenticated response cannot
        // blank User Files during startup.
        if (!sessionUserId) {
          setUserId(null);
          setIsAdmin(false);
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
        if (!sessionUserId) {
          setUserId(null);
          setIsAdmin(false);
        }
      }
    } catch {
      /* ignore */
    }
    if (sessionUserId) {
      // The app shell resolved this principal before mounting the provider;
      // retain it when both client-side auth fallbacks are temporarily
      // unavailable instead of clearing a valid User Files scope.
      setUserId(sessionUserId);
      setIsAdmin(false);
      return { userId: sessionUserId, isAdmin: false };
    }
    setUserId(null);
    setIsAdmin(false);
    return null;
  }, [sessionUserId]);

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
      const navigationEpoch = bumpNavigationEpoch();
      fetchDirectory(
        selectedProject?.source === "remote"
          ? readLastPath("workspace", selectedProjectId) ??
            remoteWorkspacePath(
              selectedProject.remote_server_id ?? "",
              selectedProject.resource_id ?? "",
            )
          : readLastPath("workspace", selectedProjectId) ??
            workspaceRoot(selectedProjectId),
        userId,
        navigationEpoch,
      );
    }
    if (!initDoneRef.current) initDoneRef.current = true;
  }, [
    clearNavigationHistory,
    bumpNavigationEpoch,
    selectedProject,
    selectedProjectId,
    userId,
    filerTab,
    fetchDirectory,
  ]);

  // 初期化
  useEffect(() => {
    (async () => {
      const userInfo = await fetchUserInfo();

      // 保存されていたタブを復元
      const savedTab = readLocalStorage(FILER_TAB_STORAGE_KEY);
      const tab: FilerTab = isFilerTab(savedTab) ? savedTab : "workspace";
      activeFilerTabRef.current = tab;

      const uid = userInfo?.userId || null;

      if (tab === "user" && uid) {
        // The user-id state update above schedules the recovery effect below.
        // Let that effect own the first User Files request so a stale closure
        // from this mount cannot tag the response as anonymous.
        initDoneRef.current = true;
      } else if (tab === "workspace" && selectedProjectId) {
        await fetchDirectory(
          readLastPath("workspace", selectedProjectId) ??
            workspaceRoot(selectedProjectId),
          uid,
        );
        initDoneRef.current = true;
      } else if (tab === "hf") {
        setIsHfMode(true);
        await fetchDirectory(readLastPath("hf", uid) ?? HF_PREFIX, uid);
        initDoneRef.current = true;
      } else if (tab === "hydrus") {
        setIsHydrusMode(true);
        setBrowseDataForPrincipal({
          success: true,
          current_path: "Hydrus",
          parent_path: null,
          can_go_up: false,
          directories: [],
          files: [],
          total_items: 0,
        }, uid);
        setCurrentPath("Hydrus");
        currentPathRef.current = "Hydrus";
        setLoading(false);
        initDoneRef.current = true;
      }
      // workspace tab で selectedProjectId がまだnullの場合は
      // selectedProjectId の useEffect で初期化される
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Auth/session state and the persisted filer tab become available on
  // different ticks. If the User Files listing was attempted before the
  // principal was ready (or a prior-generation request was cancelled), issue
  // one initial-directory request for this principal generation. Do not watch
  // browseData/currentPath here: administrators may intentionally navigate to
  // an absolute path, and that navigation must never be redirected back to
  // the User Files root by this recovery path.
  useEffect(() => {
    if (filerTab !== "user" || !userId) return;
    const recoveryKey = `${userId}:${principalGenerationRef.current}`;
    if (userFilesRecoveryKeyRef.current === recoveryKey) return;
    userFilesRecoveryKeyRef.current = recoveryKey;
    const expectedPath = initialPathForTab("user", userId);
    if (!expectedPath) return;
    void fetchDirectory(expectedPath, userId);
  }, [
    fetchDirectory,
    filerTab,
    initialPathForTab,
    userId,
  ]);

  // The shared shell renders its registered slot outside this provider's
  // subtree. Publish a narrow, action-bearing snapshot to the external bridge
  // consumed by FilesBookmarkLauncherSidebar so the sidebar never calls
  // useExplorer/useProject across that boundary.
  const projectNavigationStateRef = useRef<{
    selectedSpaceId: string | null;
    selectedProjectId: string | null;
    currentPath: string;
    browseData: ExplorerListResponse | null;
    loading: boolean;
  }>({
    selectedSpaceId,
    selectedProjectId,
    currentPath,
    browseData,
    loading,
  });
  projectNavigationStateRef.current = {
    selectedSpaceId,
    selectedProjectId,
    currentPath,
    browseData,
    loading,
  };

  const selectProjectForPath = useCallback(
    async (path: string): Promise<boolean> => {
      if (bookmarkScope.scope !== "shared" || filerTab !== "workspace") {
        return true;
      }
      const normalizedPath = path.replace(/\\/g, "/").replace(/^\/+|\/+$/g, "");
      const targetRoot = Object.keys(spaceProjectTargetMap).find((root) => {
        const normalizedRoot = root.replace(/\\/g, "/").replace(/^\/+|\/+$/g, "");
        return normalizedPath === normalizedRoot || normalizedPath.startsWith(`${normalizedRoot}/`);
      });
      const targetProjectId = targetRoot
        ? spaceProjectTargetMap[targetRoot]
        : workspaceProjectIdFromPath(path);
      if (!targetProjectId || !spaceProjectIds.includes(targetProjectId)) {
        return false;
      }
      if (projectNavigationStateRef.current.selectedSpaceId !== bookmarkScope.spaceId) {
        return false;
      }
      const targetProject =
        accessibleProjects.find((project) => project.id === targetProjectId) ??
        participatingProjects.find((project) => project.id === targetProjectId);
      if (!targetProject) return false;
      const targetIsParticipating = participatingProjects.some(
        (project) => project.id === targetProjectId,
      );
      const expectedRoot = workspaceRoot(targetProjectId);
      if (!targetIsParticipating) {
        // Admin-only local Projects are valid shared Files targets, but they
        // must not become the canonical header Project.  Fetch the root
        // directly and let the caller navigate to the stored child path.
        clearNavigationHistory();
        setFilesTargetProjectId(null);
        const navigationEpoch = bumpNavigationEpoch();
        await fetchDirectory(expectedRoot, userId, navigationEpoch);
        const deadline = Date.now() + 3_000;
        while (Date.now() < deadline) {
          const state = projectNavigationStateRef.current;
          const statePath = state.currentPath
            .replace(/\\/g, "/")
            .replace(/^\/+|\/+$/g, "");
          const root = expectedRoot
            .replace(/\\/g, "/")
            .replace(/^\/+|\/+$/g, "");
          const browsePath = state.browseData?.current_path
            ?.replace(/\\/g, "/")
            .replace(/^\/+|\/+$/g, "");
          if (
            state.selectedSpaceId === bookmarkScope.spaceId &&
            state.selectedProjectId === selectedProjectId &&
            !state.loading &&
            (statePath === root || statePath.startsWith(`${root}/`)) &&
            browsePath === statePath
          ) {
            setFilesTargetProjectId(targetProjectId);
            return true;
          }
          await new Promise<void>((resolve) => window.setTimeout(resolve, 20));
        }
        return false;
      }
      setFilesTargetProjectId(null);
      const normalizedExpectedRoot = expectedRoot
        .replace(/\\/g, "/")
        .replace(/^\/+|\/+$/g, "");
      const currentState = projectNavigationStateRef.current;
      const currentStatePath = currentState.currentPath
        .replace(/\\/g, "/")
        .replace(/^\/+|\/+$/g, "");
      const currentBrowsePath = currentState.browseData?.current_path
        ?.replace(/\\/g, "/")
        .replace(/^\/+|\/+$/g, "");
      const targetRootAlreadyLoaded =
        currentState.selectedSpaceId === bookmarkScope.spaceId &&
        currentState.selectedProjectId === targetProjectId &&
        !currentState.loading &&
        (currentStatePath === normalizedExpectedRoot ||
          currentStatePath.startsWith(`${normalizedExpectedRoot}/`)) &&
        currentBrowsePath === currentStatePath;
      if (selectedProjectId === targetProjectId && !targetRootAlreadyLoaded) {
        // The user may have opened a personal absolute bookmark while keeping
        // the same Project selected.  In that state the normal ProjectContext
        // setter is a no-op, so explicitly restore the canonical root before
        // waiting below; otherwise shared bookmarks in this Project time out.
        clearNavigationHistory();
        const navigationEpoch = bumpNavigationEpoch();
        await fetchDirectory(expectedRoot, userId, navigationEpoch);
      }
      if (selectedProjectId !== targetProjectId) {
        // Always use ProjectContext's canonical setter.  The provider then
        // owns the Space synchronization and root fetch lifecycle.
        clearNavigationHistory();
        setSelectedProjectId(targetProjectId);
      }

      const deadline = Date.now() + 3_000;
      while (Date.now() < deadline) {
        const state = projectNavigationStateRef.current;
        const statePath = state.currentPath.replace(/\\/g, "/").replace(/^\/+|\/+$/g, "");
        const root = expectedRoot.replace(/\\/g, "/").replace(/^\/+|\/+$/g, "");
        const browsePath = state.browseData?.current_path
          ?.replace(/\\/g, "/")
          .replace(/^\/+|\/+$/g, "");
        if (
          state.selectedSpaceId === bookmarkScope.spaceId &&
          state.selectedProjectId === targetProjectId &&
          !state.loading &&
          (statePath === root || statePath.startsWith(`${root}/`)) &&
          browsePath === statePath
        ) {
          return true;
        }
        await new Promise<void>((resolve) => window.setTimeout(resolve, 20));
      }
      return false;
    },
    [
      bookmarkScope,
      bumpNavigationEpoch,
      clearNavigationHistory,
      fetchDirectory,
      filerTab,
      accessibleProjects,
      participatingProjects,
      selectedProjectId,
      setSelectedProjectId,
      spaceProjectIds,
      spaceProjectTargetMap,
      userId,
    ],
  );

  const sidebarScopeRoot = filerTab === "hf"
    ? HF_PREFIX
    : filerTab === "hydrus"
      ? "Hydrus"
      : homeRootPath;
  const sidebarScopeKey = `${filerTab}:${bookmarkScope.scope}:${bookmarkScope.scope === "shared" ? bookmarkScope.spaceId : ""}:${userId ?? ""}`;
  useEffect(() => {
    const owner = filesSidebarOwnerRef.current;
    if (!owner) return;
    claimFilesSidebarOwner(owner);
    return () => resetFilesSidebarStore(owner);
  }, []);
  useEffect(() => {
    const owner = filesSidebarOwnerRef.current;
    if (!owner) return;
    publishFilesSidebarState(owner, {
      currentPath,
      browseData,
      loading,
      filerTab,
      focusedItemPath,
      editingFilePath: editingFile?.path ?? null,
      userId,
      selectedSpaceId,
      selectedProjectId,
      filesTargetProjectId,
      spaceProjectIds,
      spaceProjectTargetMap,
      scopeRoot: sidebarScopeRoot,
      scopeKey: sidebarScopeKey,
      isAdmin,
      isRemoteWorkspace,
      bookmarks,
      bookmarkCollections,
      bookmarkScope,
      navigate,
      selectProjectForPath,
      closeEditor,
      refreshBookmarks,
    });
  }, [
    bookmarks,
    browseData,
    closeEditor,
    currentPath,
    filesTargetProjectId,
    filerTab,
    focusedItemPath,
    editingFile,
    isAdmin,
    isRemoteWorkspace,
    bookmarkScope,
    bookmarkCollections,
    navigate,
    refreshBookmarks,
    selectProjectForPath,
    selectedSpaceId,
    selectedProjectId,
    spaceProjectIds,
    spaceProjectTargetMap,
    sidebarScopeKey,
    sidebarScopeRoot,
    userId,
    loading,
  ]);

  return (
    <ExplorerContext.Provider
      value={{
        currentPath,
        navigate,
        goBack,
        goForward,
        goUp,
        refresh,
        historyScrollRestore,
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
        bookmarkCollections,
        refreshBookmarks,
        bookmarkScope,
        filerTab,
        setFilerTab,
        homeRootPath,
        contextRootPath,
        storageCtx,
        storageCtxList,
        isAdmin,
        isAbsoluteFilerPath,
        isRemoteWorkspace,
        isHfMode,
        isHydrusMode,
        userId,
        capabilities,
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
