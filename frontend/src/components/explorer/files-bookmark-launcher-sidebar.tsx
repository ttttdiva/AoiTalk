"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type DragEvent,
  type KeyboardEvent,
  type MouseEvent,
  type ReactNode,
} from "react";
import {
  Bookmark,
  ChevronDown,
  ChevronRight,
  ChevronUp,
  FileIcon,
  Folder,
  FolderPlus,
  MoreHorizontal,
  Pencil,
  Play,
  Trash2,
} from "lucide-react";
import { usePathname } from "next/navigation";
import type {
  ExplorerBookmark,
  ExplorerBookmarkScope,
  ExplorerLauncher,
} from "@/lib/explorer-api";
import { executeExplorerBookmark } from "@/lib/explorer-bookmark-navigation";
import {
  bookmarkNameMnemonic,
  buildExplorerBookmarkTree,
  countBookmarkDescendants,
  flattenExplorerBookmarkTree,
  isBookmarkDescendantOf,
  isExplorerBookmarkFolder,
  type ExplorerBookmarkTreeNode,
} from "@/lib/explorer-bookmark-tree";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuPortal,
  DropdownMenuItem,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  explorerAddBookmark,
  explorerAddLauncher,
  explorerErrorMessage,
  explorerLaunchers,
  explorerRemoveBookmark,
  explorerRemoveLauncher,
  explorerUpdateBookmark,
  explorerUpdateLauncher,
} from "@/lib/explorer-api";
import {
  readTagBookmarkRecords,
  removeTagBookmark,
  renameTagBookmark,
  reorderTagBookmarks,
  type HydrusTagBookmark,
} from "@/lib/hydrus/tag-store";
import { buildHfPath, isHfPath, parseHfPath } from "@/lib/hf/virtual-path";
import { requestFilesDownload, requestFilesOpen } from "@/lib/files-sidebar-events";
import { cn } from "@/lib/utils";
import { toast } from "sonner";
import { useIncrementalSearch } from "@/hooks/use-incremental-search";
import {
  getFilesSidebarServerSnapshot,
  getFilesSidebarSnapshot,
  subscribeFilesSidebar,
} from "./files-sidebar-store";

const DND_MIME = "application/x-explorer-paths";
const DND_BOOKMARK_MIME = "application/x-files-bookmark-id";
const defaultSelectProjectForPath = async (path: string): Promise<boolean> => {
  void path;
  return true;
};
type SidebarTab = "bookmarks" | "launchers";
type SidebarItem = ExplorerBookmark | ExplorerLauncher | HydrusTagBookmark;

function isTextInput(target: EventTarget | null): boolean {
  return target instanceof HTMLElement && (
    target.tagName === "INPUT" ||
    target.tagName === "TEXTAREA" ||
    target.isContentEditable
  );
}

function focusFilesRoot() {
  const root = document.querySelector<HTMLElement>('[data-shell-region="files-canvas"]');
  root?.focus({ preventScroll: true });
}

function itemId(item: SidebarItem): string {
  if ("tag" in item) return item.tag;
  return item.id ?? item.path;
}

function itemName(item: SidebarItem): string {
  return item.name;
}

function itemPath(item: SidebarItem): string {
  return "tag" in item ? item.tag : item.path;
}

function parentPath(path: string): string | null {
  if (isHfPath(path)) {
    const parsed = parseHfPath(path);
    if (!parsed || parsed.kind === "root") return "HF";
    const subPath = parsed.subPath ?? "";
    if (!subPath) return buildHfPath({ ...parsed, subPath: undefined });
    const segments = subPath.split("/").filter(Boolean);
    segments.pop();
    return buildHfPath({ ...parsed, subPath: segments.join("/") || undefined });
  }
  const normalized = path.replace(/[\\/]+$/, "");
  const index = Math.max(normalized.lastIndexOf("/"), normalized.lastIndexOf("\\"));
  return index > 0 ? normalized.slice(0, index) : index === 0 ? normalized.slice(0, 1) : null;
}

function isAbsolutePath(path: string): boolean {
  return /^[A-Za-z]:[\\/]/.test(path) || path.startsWith("/");
}

function normalizeScopePath(path: string): string {
  return path.replace(/\\/g, "/").replace(/\/+$/, "");
}

function workspaceProjectIdFromPath(path: string): string | null {
  const normalized = normalizeScopePath(path).replace(/^\/+/, "");
  if (normalized.startsWith("aoitalk-record-table:")) {
    return normalized.slice("aoitalk-record-table:".length).split(":", 1)[0] || null;
  }
  const match = normalized.match(/^_projects\/project_([^/]+)(?:\/|$)/);
  return match?.[1] ?? null;
}

/** Scope roots use slash separators, while HF uses a `|` virtual separator. */
function pathWithinScope(
  path: string,
  scopeRoot: string,
  sameSpaceProjectIds?: ReadonlySet<string>,
): boolean {
  if (!path || !scopeRoot) return false;
  if (scopeRoot === "HF") return isHfPath(path);
  if (sameSpaceProjectIds && scopeRoot.startsWith("_projects/project_")) {
    const projectId = workspaceProjectIdFromPath(path);
    return projectId !== null && sameSpaceProjectIds.has(projectId);
  }
  if (path.startsWith("aoitalk-record-table:")) {
    // Record tables are virtual Project Files and have no filesystem
    // extension. Keep them scoped to the owning project; User/HF/Hydrus must
    // never expose them as launchers.
    const projectId = path.slice("aoitalk-record-table:".length).split(":", 1)[0];
    const projectRootPrefix = "_projects/project_";
    return scopeRoot.startsWith(projectRootPrefix) &&
      scopeRoot.slice(projectRootPrefix.length).split("/", 1)[0] === projectId;
  }
  const normalizedPath = normalizeScopePath(path);
  const normalizedRoot = normalizeScopePath(scopeRoot);
  return normalizedPath === normalizedRoot || normalizedPath.startsWith(`${normalizedRoot}/`);
}

/**
 * Keep bookmark hierarchy intact while restricting Project Files to the
 * selected Space's canonical Project roots.  Structural folders have no
 * target Space of their own, so only ancestors of an allowed target remain.
 */
function filterBookmarksForWorkspace(
  bookmarks: ExplorerBookmark[],
  projectIds: ReadonlySet<string>,
): ExplorerBookmark[] {
  const allowedIds = new Set<string>();
  const byId = new Map<string, ExplorerBookmark>();
  for (const item of bookmarks) {
    if (item.id) byId.set(item.id, item);
    if (
      !isExplorerBookmarkFolder(item) &&
      workspaceProjectIdFromPath(item.path) !== null &&
      projectIds.has(workspaceProjectIdFromPath(item.path)!) &&
      item.id
    ) {
      allowedIds.add(item.id);
    }
  }
  for (const item of bookmarks) {
    if (!item.id || !allowedIds.has(item.id)) continue;
    let parentId = item.parent_id ?? null;
    const visited = new Set<string>();
    while (parentId && !visited.has(parentId)) {
      visited.add(parentId);
      allowedIds.add(parentId);
      parentId = byId.get(parentId)?.parent_id ?? null;
    }
  }
  return bookmarks.filter((item) => {
    if (item.id) return allowedIds.has(item.id);
    const projectId = workspaceProjectIdFromPath(item.path);
    return !isExplorerBookmarkFolder(item) && projectId !== null && projectIds.has(projectId);
  });
}

const OPENABLE_EXTENSIONS = new Set([
  "txt", "md", "json", "yaml", "yml", "csv", "py", "js", "ts", "tsx", "jsx",
  "html", "css", "xml", "log", "ini", "cfg", "sql", "bat", "cmd", "sh", "ps1", "vbs",
  "jpg", "jpeg", "png", "gif", "webp", "svg", "bmp", "mp4", "webm", "mov", "avi", "mkv",
  "mp3", "wav", "ogg", "flac", "aac", "m4a", "opus", "wma", "pdf", "doc", "docx", "xls", "xlsx", "dbtable",
]);

function launcherPathIsOpenable(path: string): boolean {
  if (path.startsWith("aoitalk-record-table:")) return true;
  const name = path.split(/[\\/|]/).filter(Boolean).pop() || path;
  const extension = name.includes(".") ? name.split(".").pop()!.toLowerCase() : "";
  return OPENABLE_EXTENSIONS.has(extension);
}

function sortedByOrder<T extends { sort_order?: number; created_at?: string }>(items: T[]): T[] {
  return [...items].sort((a, b) =>
    (a.sort_order ?? Number.MAX_SAFE_INTEGER) - (b.sort_order ?? Number.MAX_SAFE_INTEGER),
  );
}

function bookmarkScopeIdentity(scope: ExplorerBookmarkScope): string {
  return scope.scope === "shared" ? `shared:${scope.spaceId}` : "personal";
}

function parseDroppedPaths(event: DragEvent): string[] {
  const raw = event.dataTransfer.getData(DND_MIME);
  if (!raw) return [];
  try {
    const value: unknown = JSON.parse(raw);
    return Array.isArray(value)
      ? value.filter((path): path is string => typeof path === "string" && path.length > 0)
      : [];
  } catch {
    return [];
  }
}

function parseDroppedBookmarkId(event: DragEvent): string | null {
  const raw = event.dataTransfer.getData(DND_BOOKMARK_MIME);
  return raw?.trim() || null;
}

function renderBookmarkQuickLauncherItems(
  nodes: ExplorerBookmarkTreeNode[],
  onExecute: (item: ExplorerBookmark) => void,
): ReactNode {
  return nodes.map((node) => {
    const key = node.item.id ?? node.item.path;
    const mnemonic = bookmarkNameMnemonic(node.item.name) ?? undefined;
    if (isExplorerBookmarkFolder(node.item)) {
      return (
        <DropdownMenuSub key={key}>
          <DropdownMenuSubTrigger mnemonic={mnemonic}>
            {node.item.name}
          </DropdownMenuSubTrigger>
          <DropdownMenuSubContent
            data-files-bookmark-quick-launcher
            className="w-auto min-w-[12rem]"
          >
            {renderBookmarkQuickLauncherItems(node.children, onExecute)}
          </DropdownMenuSubContent>
        </DropdownMenuSub>
      );
    }
    return (
      <DropdownMenuItem
        key={key}
        mnemonic={mnemonic}
        onClick={() => onExecute(node.item)}
      >
        {node.item.name}
      </DropdownMenuItem>
    );
  });
}

/**
 * The explorer endpoints return a `success` flag as part of their JSON
 * contract.  A proxy or a test double can still return HTTP 200 with
 * `success: false` (or a malformed body), so callers must not treat the
 * response as a write success based on the status code alone.
 */
function assertApiSuccess<T>(response: T, action: string): T {
  const isObject = response !== null && typeof response === "object";
  const body = isObject ? response as { success?: unknown; error?: unknown; message?: unknown; detail?: unknown } : null;
  if (!body || body.success !== true) {
    const detail = [body?.error, body?.message, body?.detail].find(
      (value): value is string => typeof value === "string" && value.trim().length > 0,
    );
    throw new Error(detail ?? `${action}に失敗しました`);
  }
  return response;
}

function reportSidebarError(action: string, error: unknown) {
  const detail = explorerErrorMessage(error);
  // Keep the original error object for diagnostics (HTTP status/detail is
  // already normalized by explorer-api) while exposing a bounded message to
  // the user through the app-wide toaster.
  console.error(`[Filesサイドバー] ${action}:`, error);
  toast.error(`${action}: ${detail}`);
}

function reportSidebarGuard(message: string) {
  console.warn(`[Filesサイドバー] ${message}`);
  toast.info(message);
}

/**
 * Files workspace navigation. This component intentionally owns no Files
 * listing state: the ExplorerProvider remains the sole navigation owner while
 * this shell slot only presents durable bookmarks and launchers.
 */
export function FilesBookmarkLauncherSidebar() {
  const pathname = usePathname();
  const {
    currentPath,
    browseData,
    loading,
    filerTab,
    focusedItemPath,
    editingFilePath,
    userId,
    scopeRoot,
    scopeKey,
    filesTargetProjectId: bridgedFilesTargetProjectId = null,
    spaceProjectIds: bridgedSpaceProjectIds = [],
    isAdmin,
    isRemoteWorkspace,
    bookmarks,
    bookmarkScope: bridgedBookmarkScope = { scope: "personal" },
    navigate,
    selectProjectForPath: bridgedSelectProjectForPath,
    closeEditor,
    refreshBookmarks,
  } = useSyncExternalStore(
    subscribeFilesSidebar,
    getFilesSidebarSnapshot,
    getFilesSidebarServerSnapshot,
  );
  const spaceProjectIds = bridgedSpaceProjectIds;
  const filesTargetProjectId = bridgedFilesTargetProjectId;
  const bookmarkScope = bridgedBookmarkScope;
  // Older/SSR snapshots do not expose the optional navigation bridge.  Keep
  // personal/legacy collections executable while the canonical Explorer
  // provider is mounting, and use the real ProjectContext-backed function when
  // it is available.
  const selectProjectForPath = bridgedSelectProjectForPath ?? defaultSelectProjectForPath;
  const [launchers, setLaunchers] = useState<ExplorerLauncher[]>([]);
  const launcherDataIdentityRef = useRef<string | null>(null);
  const [activeTab, setActiveTab] = useState<SidebarTab>("bookmarks");
  const [hydrusBookmarks, setHydrusBookmarks] = useState<HydrusTagBookmark[]>([]);
  const [activeIndex, setActiveIndex] = useState(0);
  const [contextMenu, setContextMenu] = useState<{ item: SidebarItem; x: number; y: number } | null>(null);
  const [quickLauncherOpen, setQuickLauncherOpen] = useState(false);
  const quickLauncherAnchorRef = useRef<HTMLSpanElement>(null);
  const [expandedFolderIds, setExpandedFolderIds] = useState<Set<string>>(() => new Set());
  const listRef = useRef<HTMLDivElement>(null);
  const launcherPrincipalRef = useRef<string | null>(userId);
  const launcherScopeRef = useRef(scopeKey);
  const launcherCurrentPrincipalRef = useRef(userId);
  const launcherCurrentScopeRef = useRef(scopeKey);
  const launcherCurrentBookmarkScopeRef = useRef(bookmarkScope);
  const launcherRequestGenerationRef = useRef(0);
  // Keep the latest principal/scope available to in-flight request handlers;
  // a render may commit before the corresponding passive effect runs.
  launcherCurrentPrincipalRef.current = userId;
  launcherCurrentScopeRef.current = scopeKey;
  launcherCurrentBookmarkScopeRef.current = bookmarkScope;
  const pathnameIsFiles = pathname === "/filer" || pathname?.startsWith("/filer/") === true;
  const launcherEnabled = (filerTab === "workspace" || filerTab === "user") && !isRemoteWorkspace;
  const canUseGenericBookmarks = filerTab !== "hydrus" && !isRemoteWorkspace;
  const sameSpaceProjectIdSet = useMemo(
    () => new Set(spaceProjectIds),
    [spaceProjectIds],
  );
  const isSharedWorkspaceScope =
    filerTab === "workspace" && bookmarkScope.scope === "shared";
  const canonicalScopeProjectId = workspaceProjectIdFromPath(scopeRoot);
  const isPathInFilesScope = useCallback(
    (path: string) => {
      if (!isSharedWorkspaceScope) {
        return pathWithinScope(path, scopeRoot);
      }
      const pathProjectId = workspaceProjectIdFromPath(path);
      if (pathProjectId && canonicalScopeProjectId) {
        return (
          sameSpaceProjectIdSet.has(pathProjectId) &&
          (pathProjectId === canonicalScopeProjectId ||
            pathProjectId === filesTargetProjectId)
        );
      }
      return pathWithinScope(path, scopeRoot, sameSpaceProjectIdSet);
    },
    [
      canonicalScopeProjectId,
      filesTargetProjectId,
      isSharedWorkspaceScope,
      sameSpaceProjectIdSet,
      scopeRoot,
    ],
  );
  const launcherScopeIdentity = `${userId ?? ""}|${scopeKey}`;
  const scopeReady = !loading && !!browseData &&
    normalizeScopePath(browseData.current_path) === normalizeScopePath(currentPath) &&
    isPathInFilesScope(currentPath);

  const reloadLaunchers = useCallback(async () => {
    if (!launcherEnabled) {
      setLaunchers([]);
      return;
    }
    const requestPrincipal = launcherCurrentPrincipalRef.current;
    const requestScope = launcherCurrentScopeRef.current;
    const requestBookmarkScope = bookmarkScope;
    const requestBookmarkScopeIdentity = bookmarkScopeIdentity(requestBookmarkScope);
    const requestGeneration = launcherRequestGenerationRef.current;
    // A mutation callback from an older render can run after a Space switch.
    // Do not let that stale closure issue an A request while the live refs
    // describe B; otherwise the response would pass the generation check and
    // be committed under B's identity.
    if (
      bookmarkScopeIdentity(launcherCurrentBookmarkScopeRef.current) !==
      requestBookmarkScopeIdentity
    ) {
      return;
    }
    try {
      const data = assertApiSuccess(
        await explorerLaunchers(requestBookmarkScope),
        "ランチャー一覧の取得",
      );
      if (
        requestGeneration !== launcherRequestGenerationRef.current ||
        requestPrincipal !== launcherCurrentPrincipalRef.current ||
        requestScope !== launcherCurrentScopeRef.current ||
        requestBookmarkScopeIdentity !==
          bookmarkScopeIdentity(launcherCurrentBookmarkScopeRef.current)
      ) {
        // A response from an old user/scope must never overwrite the current
        // principal's launcher list.
        return;
      }
      if (!Array.isArray(data.launchers)) {
        throw new Error("ランチャー一覧の形式が不正です");
      }
      launcherDataIdentityRef.current = `${requestPrincipal ?? ""}|${requestScope}`;
      setLaunchers(sortedByOrder(data.launchers));
    } catch (error) {
      if (
        requestGeneration !== launcherRequestGenerationRef.current ||
        requestPrincipal !== launcherCurrentPrincipalRef.current ||
        requestScope !== launcherCurrentScopeRef.current
      ) {
        return;
      }
      // Preserve the last known list on a transient GET failure.  Clearing it
      // makes a network outage look like a successful deletion and also loses
      // the user's durable entries until the next reload.
      reportSidebarError("ランチャー一覧を取得できませんでした", error);
    }
  }, [bookmarkScope, launcherEnabled]);

  useEffect(() => {
    // A failed request for the next principal/Space must not leave the old
    // collection visible, while a transient retry for the same identity
    // should retain the last known list.
    let clearTimer: number | undefined;
    const principalChanged = launcherPrincipalRef.current !== userId;
    const scopeChanged = launcherScopeRef.current !== scopeKey;
    if (principalChanged || scopeChanged) {
      launcherPrincipalRef.current = userId;
      launcherScopeRef.current = scopeKey;
      launcherRequestGenerationRef.current += 1;
      // Defer the state write out of the effect body.  The visible list is
      // already filtered by the new scope, and this clears the old principal
      // before the corresponding GET result is committed.
      clearTimer = window.setTimeout(() => {
        launcherDataIdentityRef.current = null;
        setLaunchers([]);
      }, 0);
    }
    const timer = window.setTimeout(() => void reloadLaunchers(), 0);
    return () => {
      window.clearTimeout(timer);
      if (clearTimer !== undefined) window.clearTimeout(clearTimer);
    };
  }, [reloadLaunchers, scopeKey, userId]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setHydrusBookmarks(filerTab === "hydrus" ? readTagBookmarkRecords(userId) : []);
    }, 0);
    return () => window.clearTimeout(timer);
  }, [filerTab, userId]);

  useEffect(() => {
    const onHydrusChanged = (event: Event) => {
      const detail = (event as CustomEvent<{ userId?: string | null }>).detail;
      if ((detail?.userId ?? null) !== (userId ?? null)) return;
      setHydrusBookmarks(readTagBookmarkRecords(userId));
    };
    window.addEventListener("hydrus-tag-bookmarks-changed", onHydrusChanged);
    return () => window.removeEventListener("hydrus-tag-bookmarks-changed", onHydrusChanged);
  }, [userId]);

  const visibleBookmarks = useMemo(() => {
    if (filerTab === "hydrus") return hydrusBookmarks;
    if (filerTab === "workspace" && bookmarkScope.scope === "shared") {
      return sortedByOrder(filterBookmarksForWorkspace(bookmarks, sameSpaceProjectIdSet));
    }
    return sortedByOrder(
      bookmarks.filter(
        (item) => isExplorerBookmarkFolder(item) || pathWithinScope(item.path, scopeRoot),
      ),
    );
  }, [
    bookmarkScope.scope,
    bookmarks,
    filerTab,
    hydrusBookmarks,
    sameSpaceProjectIdSet,
    scopeRoot,
  ]);

  const bookmarkTree = useMemo(
    () => (filerTab === "hydrus" ? [] : buildExplorerBookmarkTree(visibleBookmarks as ExplorerBookmark[])),
    [filerTab, visibleBookmarks],
  );

  useEffect(() => {
    if (filerTab === "hydrus") return;
    setExpandedFolderIds((prev) => {
      const next = new Set(prev);
      let changed = false;
      for (const item of visibleBookmarks as ExplorerBookmark[]) {
        if (!isExplorerBookmarkFolder(item)) continue;
        const key = item.id ?? item.path;
        if (!next.has(key)) {
          next.add(key);
          changed = true;
        }
      }
      return changed ? next : prev;
    });
  }, [filerTab, visibleBookmarks]);

  const bookmarkFlatRows = useMemo(
    () => flattenExplorerBookmarkTree(bookmarkTree, expandedFolderIds),
    [bookmarkTree, expandedFolderIds],
  );

  const visibleLaunchers = useMemo(
    () => launcherDataIdentityRef.current === launcherScopeIdentity
      ? sortedByOrder(launchers.filter((item) => pathWithinScope(
        item.path,
        scopeRoot,
        bookmarkScope.scope === "shared" ? sameSpaceProjectIdSet : undefined,
      )))
      : [],
    [bookmarkScope.scope, launcherScopeIdentity, launchers, sameSpaceProjectIdSet, scopeRoot],
  );
  const visibleItems = activeTab === "launchers" && launcherEnabled
    ? visibleLaunchers
    : filerTab === "hydrus"
      ? visibleBookmarks
      : bookmarkFlatRows.map((row) => row.node.item);

  // 共通のインクリメンタル検索へ渡す検索対象。現在実際に選択可能な表示項目だけを
  // 対象にするため、折り畳まれたブックマーク子要素は含まれない。
  const searchItems = visibleItems.map((item) => ({
    path: itemId(item),
    name: itemName(item),
  }));

  useEffect(() => {
    if (!launcherEnabled && activeTab !== "bookmarks") {
      const timer = window.setTimeout(() => setActiveTab("bookmarks"), 0);
      return () => window.clearTimeout(timer);
    }
    return undefined;
  }, [activeTab, launcherEnabled]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setActiveIndex((index) => Math.max(0, Math.min(index, Math.max(visibleItems.length - 1, 0))));
    }, 0);
    return () => window.clearTimeout(timer);
  }, [visibleItems.length, activeTab, filerTab]);

  const focusListItem = useCallback((index: number) => {
    const next = Math.max(0, Math.min(index, Math.max(visibleItems.length - 1, 0)));
    setActiveIndex(next);
    window.requestAnimationFrame(() => {
      listRef.current?.querySelector<HTMLElement>(`[data-files-sidebar-index="${next}"]`)?.focus({ preventScroll: false });
    });
  }, [visibleItems.length]);

  const incrementalSearch = useIncrementalSearch({
    // Bookmark ↔ Launcher 切替、Filesタブ切替、scope / principal 変更で
    // 以前の検索条件を別リストへ持ち越さない。
    getContextKey: () =>
      `${activeTab}|${filerTab}|${scopeKey}|${userId ?? ""}`,
    getItems: () => searchItems,
    getActiveKey: () => searchItems[activeIndex]?.path ?? null,
    focusMatch: (item) => {
      const index = searchItems.findIndex((entry) => entry.path === item.path);
      if (index >= 0) focusListItem(index);
    },
  });

  const selectTab = useCallback((tab: SidebarTab, focus = true) => {
    if (tab === "launchers" && !launcherEnabled) return;
    setActiveTab(tab);
    setActiveIndex(0);
    if (focus) window.requestAnimationFrame(() => {
      const first = listRef.current?.querySelector<HTMLElement>('[data-files-sidebar-index="0"]');
      (first ?? listRef.current)?.focus();
    });
  }, [launcherEnabled]);

  const toggleFolderExpanded = useCallback((folderKey: string) => {
    setExpandedFolderIds((prev) => {
      const next = new Set(prev);
      if (next.has(folderKey)) next.delete(folderKey);
      else next.add(folderKey);
      return next;
    });
  }, []);

  const addBookmark = useCallback(async (path: string, name?: string, parentId?: string | null) => {
    if (!path) return false;
    if (!canUseGenericBookmarks) {
      reportSidebarGuard("このFilesタブではブックマーク登録を利用できません");
      return false;
    }
    if (!isPathInFilesScope(path) || (!isAdmin && isAbsolutePath(path)) || isHfPath(path) !== (filerTab === "hf")) {
      reportSidebarGuard("この場所はブックマーク登録の対象外です");
      return false;
    }
    if (!scopeReady) {
      reportSidebarGuard("Filesの一覧を読み込み中です。完了後にもう一度お試しください");
      return false;
    }
    const fallbackName = path.split(/[\\/|]/).filter(Boolean).pop() || "Folder";
    if (bookmarks.some((item) => item.path === path)) return false;
    try {
      if (parentId !== undefined) {
        assertApiSuccess(
          await explorerAddBookmark(
            name?.trim() || fallbackName,
            path,
            undefined,
            { parent_id: parentId },
            bookmarkScope,
          ),
          "ブックマーク登録",
        );
      } else {
        assertApiSuccess(
          await explorerAddBookmark(
            name?.trim() || fallbackName,
            path,
            undefined,
            undefined,
            bookmarkScope,
          ),
          "ブックマーク登録",
        );
      }
    } catch (error) {
      reportSidebarError("ブックマーク登録に失敗しました", error);
      return false;
    }
    try {
      await refreshBookmarks();
    } catch (error) {
      reportSidebarError("ブックマークは登録済みですが一覧更新に失敗しました", error);
    }
    return true;
  }, [bookmarks, bookmarkScope, canUseGenericBookmarks, filerTab, isAdmin, isPathInFilesScope, refreshBookmarks, scopeReady]);

  const createBookmarkFolder = useCallback(async (parentId: string | null) => {
    if (!canUseGenericBookmarks) {
      reportSidebarGuard("このFilesタブではブックマーク登録を利用できません");
      return;
    }
    const nextName = window.prompt("フォルダ名");
    if (nextName == null || !nextName.trim()) return;
    try {
      assertApiSuccess(
        await explorerAddBookmark(
          nextName.trim(),
          undefined,
          undefined,
          { kind: "folder", parent_id: parentId },
          bookmarkScope,
        ),
        "フォルダ作成",
      );
      await refreshBookmarks();
    } catch (error) {
      reportSidebarError("フォルダ作成に失敗しました", error);
    }
  }, [bookmarkScope, canUseGenericBookmarks, refreshBookmarks]);

  const moveBookmarkToParent = useCallback(async (bookmarkId: string, parentId: string | null) => {
    const item = bookmarks.find((entry) => entry.id === bookmarkId);
    if (!item?.id) return;
    const currentParent = item.parent_id ?? null;
    if (currentParent === parentId) return;
    if (parentId && isBookmarkDescendantOf(bookmarks, parentId, bookmarkId)) {
      reportSidebarGuard("自分自身または子フォルダへは移動できません");
      return;
    }
    try {
      assertApiSuccess(
        await explorerUpdateBookmark(bookmarkId, { parent_id: parentId }, bookmarkScope),
        "ブックマーク階層変更",
      );
      await refreshBookmarks();
    } catch (error) {
      reportSidebarError("ブックマークの移動に失敗しました", error);
    }
  }, [bookmarks, bookmarkScope, refreshBookmarks]);

  const addLauncher = useCallback(async (path: string, name?: string) => {
    if (!path) return false;
    if (!launcherEnabled) {
      reportSidebarGuard("このFilesタブではランチャー登録を利用できません");
      return false;
    }
    if (!isPathInFilesScope(path) || !browseData?.files.some((file) => file.path === path)) {
      reportSidebarGuard("このファイルはランチャー登録の対象外です");
      return false;
    }
    if (!scopeReady) {
      reportSidebarGuard("Filesの一覧を読み込み中です。完了後にもう一度お試しください");
      return false;
    }
    if (launchers.some((item) => item.path === path)) return false;
    const fallbackName = path.split(/[\\/|]/).filter(Boolean).pop() || "File";
    try {
      assertApiSuccess(
        await explorerAddLauncher(name?.trim() || fallbackName, path, undefined, bookmarkScope),
        "ランチャー登録",
      );
      await reloadLaunchers();
      return true;
    } catch (error) {
      reportSidebarError("ランチャー登録に失敗しました", error);
      return false;
    }
  }, [bookmarkScope, browseData?.files, isPathInFilesScope, launchers, launcherEnabled, reloadLaunchers, scopeReady]);

  const removeItem = useCallback(async (item: SidebarItem) => {
    try {
      if ("tag" in item) {
        setHydrusBookmarks(removeTagBookmark(item.tag, userId));
      } else if (activeTab === "bookmarks") {
        if (isExplorerBookmarkFolder(item as ExplorerBookmark)) {
          const descendantCount = item.id
            ? countBookmarkDescendants(bookmarks, item.id)
            : 0;
          const message = descendantCount > 0
            ? `「${item.name}」と配下の ${descendantCount} 件を削除します。よろしいですか？`
            : `「${item.name}」を削除します。よろしいですか？`;
          if (!window.confirm(message)) return;
        }
        assertApiSuccess(await explorerRemoveBookmark(item.path, bookmarkScope), "ブックマーク削除");
        await refreshBookmarks();
      } else if (item.id) {
        assertApiSuccess(await explorerRemoveLauncher(item.id, bookmarkScope), "ランチャー削除");
        await reloadLaunchers();
      }
    } catch (error) {
      reportSidebarError("項目の削除に失敗しました", error);
    }
  }, [activeTab, bookmarkScope, bookmarks, refreshBookmarks, reloadLaunchers, userId]);

  const renameItem = useCallback(async (item: SidebarItem) => {
    const nextName = window.prompt("表示名を変更", itemName(item));
    if (nextName == null || !nextName.trim()) return;
    try {
      if ("tag" in item) {
        setHydrusBookmarks(renameTagBookmark(item.tag, nextName, userId));
      } else if (activeTab === "bookmarks" && item.id) {
        assertApiSuccess(
          await explorerUpdateBookmark(item.id, { name: nextName.trim() }, bookmarkScope),
          "ブックマーク名変更",
        );
        await refreshBookmarks();
      } else if (activeTab === "launchers" && item.id) {
        assertApiSuccess(
          await explorerUpdateLauncher(item.id, { name: nextName.trim() }, bookmarkScope),
          "ランチャー名変更",
        );
        await reloadLaunchers();
      }
    } catch (error) {
      reportSidebarError("項目名の変更に失敗しました", error);
    }
  }, [activeTab, bookmarkScope, refreshBookmarks, reloadLaunchers, userId]);

  const reorder = useCallback(async (offset: -1 | 1) => {
    const item = visibleItems[activeIndex];
    if (!item) return;

    if (activeTab === "bookmarks" && !("tag" in item) && filerTab !== "hydrus") {
      const bookmark = item as ExplorerBookmark;
      const parentId = bookmark.parent_id ?? null;
      const siblings = sortedByOrder(
        (visibleBookmarks as ExplorerBookmark[]).filter(
          (entry) => (entry.parent_id ?? null) === parentId,
        ),
      );
      const siblingIndex = siblings.findIndex((entry) => entry.id === bookmark.id);
      const targetSiblingIndex = siblingIndex + offset;
      if (siblingIndex < 0 || targetSiblingIndex < 0 || targetSiblingIndex >= siblings.length) return;
      const reordered = [...siblings];
      [reordered[siblingIndex], reordered[targetSiblingIndex]] = [
        reordered[targetSiblingIndex],
        reordered[siblingIndex],
      ];
      try {
        await Promise.all(reordered.map(async (entry, index) => {
          if (!entry.id) return null;
          return assertApiSuccess(
            await explorerUpdateBookmark(entry.id, { sort_order: index }, bookmarkScope),
            "ブックマーク並び替え",
          );
        }));
        await refreshBookmarks();
      } catch (error) {
        reportSidebarError("項目の並び替えに失敗しました", error);
        return;
      }
      const flatTarget = bookmarkFlatRows.findIndex(
        (row) => row.node.item.id === reordered[targetSiblingIndex].id,
      );
      if (flatTarget >= 0) setActiveIndex(flatTarget);
      return;
    }

    const targetIndex = activeIndex + offset;
    if (targetIndex < 0 || targetIndex >= visibleItems.length) return;
    const reordered = [...visibleItems];
    [reordered[activeIndex], reordered[targetIndex]] = [reordered[targetIndex], reordered[activeIndex]];
    try {
      if ("tag" in item) {
        const next = reorderTagBookmarks(reordered.map((entry) => itemPath(entry)), userId);
        setHydrusBookmarks(next);
      } else if (activeTab === "launchers") {
        await Promise.all(reordered.map(async (entry, index) => {
          if ("tag" in entry || !entry.id) return null;
          return assertApiSuccess(
            await explorerUpdateLauncher(entry.id, { sort_order: index }, bookmarkScope),
            "ランチャー並び替え",
          );
        }));
        await reloadLaunchers();
      }
    } catch (error) {
      reportSidebarError("項目の並び替えに失敗しました", error);
      return;
    }
    setActiveIndex(targetIndex);
  }, [activeIndex, activeTab, bookmarkFlatRows, bookmarkScope, filerTab, refreshBookmarks, reloadLaunchers, userId, visibleBookmarks, visibleItems]);

  const runBookmark = useCallback((bookmark: ExplorerBookmark) => {
    void executeExplorerBookmark(bookmark, {
      closeEditor,
      navigate,
      focusFilesRoot,
      selectProjectForPath,
    }).catch((error: unknown) => {
      reportSidebarError("ブックマークを開けませんでした", error);
    });
  }, [closeEditor, navigate, selectProjectForPath]);

  const runLauncher = useCallback((launcher: ExplorerLauncher) => {
    void (async () => {
      // Launchers can target another Project in the selected Space just like
      // bookmarks.  Keep the generic Files navigation boundary intact and
      // use the dedicated ProjectContext-backed transition before opening the
      // stored path.
      if (!(await selectProjectForPath(launcher.path))) {
        reportSidebarGuard("このランチャーのProjectは現在のSpaceから利用できません");
        return;
      }
      if (launcherPathIsOpenable(launcher.path)) {
        requestFilesOpen(launcher.path, launcher.name);
      } else {
        requestFilesDownload(launcher.path);
      }
    })().catch((error: unknown) => {
      reportSidebarError("ランチャーを開けませんでした", error);
    });
  }, [selectProjectForPath]);

  const executeItem = useCallback((item: SidebarItem) => {
    if ("tag" in item) {
      window.dispatchEvent(new CustomEvent("hydrus-tag-bookmark-use", { detail: { tag: item.tag, userId: userId ?? null } }));
      focusFilesRoot();
      return;
    }
    if (activeTab === "bookmarks") {
      const bookmark = item as ExplorerBookmark;
      if (isExplorerBookmarkFolder(bookmark)) {
        toggleFolderExpanded(bookmark.id ?? bookmark.path);
        return;
      }
      runBookmark(bookmark);
    } else {
      runLauncher(item as ExplorerLauncher);
    }
  }, [activeTab, runBookmark, runLauncher, toggleFolderExpanded, userId]);

  const handleQuickLauncherExecute = useCallback((bookmark: ExplorerBookmark) => {
    runBookmark(bookmark);
    setQuickLauncherOpen(false);
  }, [runBookmark]);

  useEffect(() => {
    if (!quickLauncherOpen) return;
    const frame = window.requestAnimationFrame(() => {
      const menu = document.querySelector<HTMLElement>(
        '[data-testid="files-bookmark-quick-launcher-menu"][data-open]',
      );
      const firstItem = menu?.querySelector<HTMLElement>('[role="menuitem"]');
      (firstItem ?? menu)?.focus({ preventScroll: true });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [quickLauncherOpen]);

  const handleListKeyDown = useCallback((event: KeyboardEvent<HTMLDivElement>) => {
    // A forward Tab from the Files sidebar returns to the main Files canvas.
    // Keep reverse Tab untouched so the browser's normal focus order remains
    // available to callers that intentionally move backwards.
    if (
      event.key === "Tab" &&
      !event.shiftKey &&
      !event.ctrlKey &&
      !event.metaKey &&
      !event.altKey
    ) {
      event.preventDefault();
      focusFilesRoot();
    } else if (event.key === "ArrowDown") {
      event.preventDefault();
      focusListItem(activeIndex + 1);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      focusListItem(activeIndex - 1);
    } else if (event.key === "Enter") {
      event.preventDefault();
      const item = visibleItems[activeIndex];
      if (item) executeItem(item);
    } else if (event.key === "Escape") {
      event.preventDefault();
      focusFilesRoot();
    } else if (event.key === "Delete") {
      event.preventDefault();
      const item = visibleItems[activeIndex];
      if (item) void removeItem(item);
    } else if (event.altKey && event.key.toLowerCase() === "j" && activeTab === "launchers") {
      event.preventDefault();
      const item = visibleItems[activeIndex];
      const parent = item ? parentPath(itemPath(item)) : null;
      if (parent) {
        closeEditor();
        navigate(parent);
        focusFilesRoot();
      }
    } else if (
      !event.ctrlKey &&
      !event.metaKey &&
      !event.altKey &&
      event.key.length === 1 &&
      event.key !== " " &&
      !event.nativeEvent.isComposing
    ) {
      // ブックマーク／ランチャー一覧上での通常文字入力は、メインFiles一覧と
      // 同じ検索規則（Migemo 含む）でインクリメンタル検索に使う。
      event.preventDefault();
      incrementalSearch.handleCharacter(event.key);
    }
  }, [activeIndex, activeTab, closeEditor, executeItem, focusListItem, incrementalSearch, navigate, removeItem, visibleItems]);

  // Workspace navigation is rendered through the shared shell's external
  // slot.  React's delegated key listener is not guaranteed to observe the
  // browser's native Tab traversal from a slotted option, so claim only the
  // forward-Tab contract at the concrete list boundary as a native listener.
  // A callback ref installs/removes it with the final slotted DOM node;
  // other keys continue through the existing React handler unchanged.
  const handleForwardTab = useCallback((event: globalThis.KeyboardEvent) => {
    if (
      event.key !== "Tab" ||
      event.shiftKey ||
      event.ctrlKey ||
      event.metaKey ||
      event.altKey
    ) {
      return;
    }
    event.preventDefault();
    focusFilesRoot();
  }, []);
  const listNodeRef = useCallback((node: HTMLDivElement | null) => {
    const previous = listRef.current;
    if (previous) previous.removeEventListener("keydown", handleForwardTab);
    listRef.current = node;
    if (node) node.addEventListener("keydown", handleForwardTab);
  }, [handleForwardTab]);
  useEffect(() => {
    const handleDocumentForwardTab = (event: globalThis.KeyboardEvent) => {
      const target = event.target;
      if (!(target instanceof Element) || !target.closest("[data-files-sidebar-list]")) return;
      handleForwardTab(event);
    };
    // The slotted navigation can be replaced during shell hydration; a
    // document capture boundary keeps this one narrowly-scoped contract
    // attached without relying on a particular list node's lifetime.
    document.addEventListener("keydown", handleDocumentForwardTab, true);
    return () => document.removeEventListener("keydown", handleDocumentForwardTab, true);
  }, [handleForwardTab]);

  // Files-only global shortcuts. Input/editor fields retain their normal
  // browser semantics, except Ctrl+D is intentionally claimed by Files.
  useEffect(() => {
    if (!pathnameIsFiles) return;
    const onKeyDown = (event: globalThis.KeyboardEvent) => {
      const key = event.key.toLowerCase();
      const primary = event.ctrlKey || event.metaKey;
      const inputFocused = isTextInput(event.target);
      // Ctrl+D is claimed by Files even from an editor/input so the browser's
      // Add Bookmark dialog cannot win. The current Files folder remains the
      // registration target while CodeMirror has focus.
      if (primary && !event.shiftKey && key === "d") {
        event.preventDefault();
        void addBookmark(currentPath);
        return;
      }
      // Ctrl+Shift+D follows the same no-browser-default rule. When an editor
      // owns focus, its path is the canonical focused-file fallback.
      if (primary && event.shiftKey && key === "d") {
        event.preventDefault();
        if (!launcherEnabled) {
          reportSidebarGuard("このFilesタブではランチャー登録を利用できません");
          return;
        }
        const candidatePath = focusedItemPath ?? editingFilePath;
        if (!candidatePath) return;
        const candidate = browseData?.files.find((file) => file.path === candidatePath);
        if (!candidate && !editingFilePath) return;
        void addLauncher(candidatePath, candidate?.name);
        return;
      }
      // Alt+Q/E are explicit Files navigation escapes and remain available
      // while an editor input is open; other shortcuts respect text editing.
      if (inputFocused && !(event.altKey && (key === "q" || key === "e" || key === "a"))) return;
      // Ctrl+J: ブックマーク／ランチャー一覧が検索条件を持っている時だけ
      // 「次の一致」として claim する。claim しない場合は preventDefault せず
      // グローバルの Ctrl+J（チャット入力欄フォーカス）へ譲る。
      if (primary && !event.altKey && !event.shiftKey && key === "j") {
        const target = event.target;
        const ownsList =
          target instanceof Element &&
          !!target.closest("[data-files-sidebar-list]");
        if (!ownsList || !incrementalSearch.hasActiveSearch()) return;
        event.preventDefault();
        incrementalSearch.focusNextMatch();
        return;
      }
      if (event.altKey && key === "q") {
        event.preventDefault();
        selectTab("bookmarks");
      } else if (event.altKey && key === "e") {
        if (!launcherEnabled) return;
        event.preventDefault();
        selectTab("launchers");
      } else if (event.altKey && key === "a" && filerTab !== "hydrus") {
        event.preventDefault();
        setQuickLauncherOpen(true);
      }
    };
    window.addEventListener("keydown", onKeyDown, true);
    return () => window.removeEventListener("keydown", onKeyDown, true);
  }, [addBookmark, addLauncher, browseData?.files, canUseGenericBookmarks, currentPath, editingFilePath, filerTab, focusedItemPath, incrementalSearch, launcherEnabled, pathnameIsFiles, scopeReady, scopeRoot, selectTab]);

  const handleDrop = useCallback(async (event: DragEvent<HTMLElement>, parentId?: string | null) => {
    event.preventDefault();
    event.stopPropagation();
    const types = event.dataTransfer.types;
    const bookmarkId = types.includes(DND_BOOKMARK_MIME)
      ? parseDroppedBookmarkId(event)
      : null;
    if (bookmarkId) {
      if (parentId !== undefined) {
        await moveBookmarkToParent(bookmarkId, parentId);
      } else {
        await moveBookmarkToParent(bookmarkId, null);
      }
      return;
    }
    const paths = parseDroppedPaths(event);
    if (paths.length === 0) return;
    if (!browseData) {
      reportSidebarGuard("Filesの一覧を読み込み中です。完了後にもう一度お試しください");
      return;
    }
    if (!scopeReady) {
      reportSidebarGuard("Filesの一覧を読み込み中です。完了後にもう一度お試しください");
      return;
    }
    const directoryPaths = new Set(browseData.directories.map((entry) => entry.path));
    const filePaths = new Set(browseData.files.map((entry) => entry.path));
    const allDirectories = paths.every((path) => directoryPaths.has(path));
    const allFiles = paths.every((path) => filePaths.has(path));
    if (activeTab === "bookmarks" && allDirectories && canUseGenericBookmarks) {
      await Promise.all(
        paths.map((path) =>
          addBookmark(
            path,
            browseData.directories.find((entry) => entry.path === path)?.name,
            parentId,
          ),
        ),
      );
    } else if (activeTab === "launchers" && allFiles && launcherEnabled) {
      await Promise.all(paths.map((path) => addLauncher(path, browseData.files.find((entry) => entry.path === path)?.name)));
    }
  }, [activeTab, addBookmark, addLauncher, browseData, canUseGenericBookmarks, launcherEnabled, moveBookmarkToParent, scopeReady]);

  const onItemContextMenu = (event: MouseEvent, item: SidebarItem) => {
    event.preventDefault();
    event.stopPropagation();
    setContextMenu({ item, x: event.clientX, y: event.clientY });
  };

  return (
    <aside
      className="flex h-full min-h-0 w-full flex-col rounded-lg border border-sidebar-border bg-sidebar-accent/20 p-2"
      data-shell-region="files-bookmark-launcher-sidebar"
      data-testid="files-bookmark-launcher-sidebar"
      aria-label="Filesブックマークとランチャー"
      onClick={() => contextMenu && setContextMenu(null)}
      onDragOver={(event) => {
        const types = event.dataTransfer.types;
        if (!types.includes(DND_MIME) && !types.includes(DND_BOOKMARK_MIME)) return;
        event.preventDefault();
        event.dataTransfer.dropEffect = types.includes(DND_BOOKMARK_MIME) ? "move" : "copy";
      }}
      onDrop={(event) => void handleDrop(event)}
    >
      <DropdownMenu open={quickLauncherOpen} onOpenChange={setQuickLauncherOpen}>
        <DropdownMenuTrigger
          className="sr-only"
          aria-hidden
          tabIndex={-1}
          data-testid="files-bookmark-quick-launcher-trigger"
        >
          ブックマーククイックランチャー
        </DropdownMenuTrigger>
        <DropdownMenuPortal keepMounted>
          <span
            ref={quickLauncherAnchorRef}
            aria-hidden="true"
            className="pointer-events-none fixed top-1/2 left-1/2 h-0 w-0"
            data-files-bookmark-quick-launcher-anchor
          />
        </DropdownMenuPortal>
        <DropdownMenuContent
          anchor={quickLauncherAnchorRef}
          positionMethod="fixed"
          align="center"
          sideOffset={({ side, anchor, positioner }) => {
            // The zero-sized fixed anchor marks the viewport center. Offset by
            // the measured popup (and anchor) size so both preferred and
            // collision-flipped placements stay centered without viewport math.
            const vertical = side === "top" || side === "bottom";
            const anchorSize = vertical ? anchor.height : anchor.width;
            const positionerSize = vertical ? positioner.height : positioner.width;
            return -(anchorSize + positionerSize) / 2;
          }}
          data-files-bookmark-quick-launcher
          className="w-auto min-w-[12rem]"
          data-testid="files-bookmark-quick-launcher-menu"
          tabIndex={-1}
        >
          {renderBookmarkQuickLauncherItems(bookmarkTree, handleQuickLauncherExecute)}
        </DropdownMenuContent>
      </DropdownMenu>
      <div className="flex items-center gap-1 border-b border-sidebar-border pb-2" role="tablist" aria-label="Filesサイドバー">
        <button type="button" role="tab" aria-selected={activeTab === "bookmarks"} className={cn("flex flex-1 items-center justify-center gap-1 rounded px-2 py-1.5 text-xs", activeTab === "bookmarks" && "bg-sidebar-accent font-semibold")} onClick={() => selectTab("bookmarks")}>
          <Bookmark className="size-3.5" /> ブックマーク
        </button>
        {launcherEnabled && (
          <button type="button" role="tab" aria-selected={activeTab === "launchers"} className={cn("flex flex-1 items-center justify-center gap-1 rounded px-2 py-1.5 text-xs", activeTab === "launchers" && "bg-sidebar-accent font-semibold")} onClick={() => selectTab("launchers")}>
            <Play className="size-3.5" /> ランチャー
          </button>
        )}
      </div>
      <div className="min-h-0 flex-1 overflow-auto py-2" ref={listNodeRef} role="listbox" aria-label={activeTab === "bookmarks" ? "ブックマーク一覧" : "ランチャー一覧"} onKeyDown={handleListKeyDown} tabIndex={0} data-files-sidebar-list>
        {activeTab === "bookmarks" && filerTab !== "hydrus"
          ? bookmarkFlatRows.map((row, index) => {
            const item = row.node.item;
            const folder = isExplorerBookmarkFolder(item);
            const folderKey = item.id ?? item.path;
            const expanded = folder && expandedFolderIds.has(folderKey);
            return (
              <button
                key={itemId(item)}
                type="button"
                role="option"
                aria-selected={index === activeIndex}
                aria-expanded={folder ? expanded : undefined}
                tabIndex={index === activeIndex ? 0 : -1}
                data-files-sidebar-index={index}
                draggable={Boolean(item.id)}
                className={cn(
                  "group flex w-full min-w-0 items-center gap-1.5 rounded px-2 py-1.5 text-left text-xs hover:bg-sidebar-accent",
                  index === activeIndex && "bg-sidebar-accent/70",
                )}
                style={{ paddingLeft: `${8 + row.depth * 12}px` }}
                onFocus={() => setActiveIndex(index)}
                onClick={(event) => { event.stopPropagation(); setActiveIndex(index); executeItem(item); }}
                onContextMenu={(event) => onItemContextMenu(event, item)}
                onDragStart={(event) => {
                  if (!item.id) return;
                  event.dataTransfer.setData(DND_BOOKMARK_MIME, item.id);
                  event.dataTransfer.effectAllowed = "move";
                }}
                onDragOver={(event) => {
                  const types = event.dataTransfer.types;
                  if (!folder || (!types.includes(DND_BOOKMARK_MIME) && !types.includes(DND_MIME))) return;
                  event.preventDefault();
                  event.stopPropagation();
                  event.dataTransfer.dropEffect = types.includes(DND_BOOKMARK_MIME) ? "move" : "copy";
                }}
                onDrop={(event) => {
                  if (!folder || !item.id) return;
                  void handleDrop(event, item.id);
                }}
                title={folder ? item.name : itemPath(item)}
              >
                {folder ? (
                  expanded
                    ? <ChevronDown className="size-3 shrink-0 text-sidebar-foreground/60" />
                    : <ChevronRight className="size-3 shrink-0 text-sidebar-foreground/60" />
                ) : null}
                {folder
                  ? <Folder className="size-3.5 shrink-0 text-amber-500" />
                  : <Folder className="size-3.5 shrink-0 text-yellow-500" />}
                <span className="min-w-0 flex-1 truncate">{itemName(item)}</span>
                {index === activeIndex && <span className="sr-only">選択中</span>}
              </button>
            );
          })
          : visibleItems.map((item, index) => (
            <button
              key={itemId(item)}
              type="button"
              role="option"
              aria-selected={index === activeIndex}
              tabIndex={index === activeIndex ? 0 : -1}
              data-files-sidebar-index={index}
              className={cn("group flex w-full min-w-0 items-center gap-1.5 rounded px-2 py-1.5 text-left text-xs hover:bg-sidebar-accent", index === activeIndex && "bg-sidebar-accent/70")}
              onFocus={() => setActiveIndex(index)}
              onClick={(event) => { event.stopPropagation(); setActiveIndex(index); executeItem(item); }}
              onContextMenu={(event) => onItemContextMenu(event, item)}
              title={itemPath(item)}
            >
              {"tag" in item ? <Bookmark className="size-3.5 shrink-0 text-amber-400" /> : activeTab === "bookmarks" ? <Folder className="size-3.5 shrink-0 text-yellow-500" /> : <FileIcon className="size-3.5 shrink-0 text-blue-400" />}
              <span className="min-w-0 flex-1 truncate">{itemName(item)}</span>
              {index === activeIndex && <span className="sr-only">選択中</span>}
            </button>
          ))}
        {visibleItems.length === 0 && <p className="px-2 py-4 text-center text-xs text-sidebar-foreground/55">{activeTab === "bookmarks" ? "ブックマークはありません" : "ランチャーはありません"}</p>}
      </div>
      <div className="flex items-center justify-end gap-0.5 border-t border-sidebar-border pt-1" aria-label="並び替え">
        {activeTab === "bookmarks" && filerTab !== "hydrus" && canUseGenericBookmarks && (
          <button
            type="button"
            className="mr-auto flex items-center gap-1 rounded px-1.5 py-1 text-[10px] text-sidebar-foreground/70 hover:bg-sidebar-accent"
            onClick={() => void createBookmarkFolder(null)}
            title="フォルダを作成"
            aria-label="フォルダを作成"
          >
            <FolderPlus className="size-3.5" />
            フォルダを作成
          </button>
        )}
        <button type="button" className="rounded p-1 text-sidebar-foreground/60 hover:bg-sidebar-accent disabled:opacity-30" disabled={activeIndex <= 0} onClick={() => void reorder(-1)} title="上へ"><ChevronUp className="size-3.5" /></button>
        <button type="button" className="rounded p-1 text-sidebar-foreground/60 hover:bg-sidebar-accent disabled:opacity-30" disabled={activeIndex >= visibleItems.length - 1} onClick={() => void reorder(1)} title="下へ"><ChevronDown className="size-3.5" /></button>
        <span className="ml-1 text-[10px] text-sidebar-foreground/45">↑↓で並び替え</span>
      </div>
      {contextMenu && (
        <div className="fixed z-[100] min-w-32 rounded-md border border-border bg-popover p-1 text-popover-foreground shadow-md" style={{ left: contextMenu.x, top: contextMenu.y }} onClick={(event) => event.stopPropagation()}>
          {activeTab === "bookmarks" && filerTab !== "hydrus" && canUseGenericBookmarks && (
            <button
              type="button"
              className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-xs hover:bg-accent"
              onClick={() => {
                const parentId = !("tag" in contextMenu.item) && isExplorerBookmarkFolder(contextMenu.item as ExplorerBookmark)
                  ? contextMenu.item.id ?? null
                  : null;
                void createBookmarkFolder(parentId);
                setContextMenu(null);
              }}
            >
              <FolderPlus className="size-3" />フォルダを作成
            </button>
          )}
          <button type="button" className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-xs hover:bg-accent" onClick={() => { void renameItem(contextMenu.item); setContextMenu(null); }}><Pencil className="size-3" />名前変更</button>
          <button type="button" className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-xs text-destructive hover:bg-accent" onClick={() => { void removeItem(contextMenu.item); setContextMenu(null); }}><Trash2 className="size-3" />削除</button>
          <button type="button" className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-xs hover:bg-accent" onClick={() => setContextMenu(null)}><MoreHorizontal className="size-3" />閉じる</button>
        </div>
      )}
    </aside>
  );
}
