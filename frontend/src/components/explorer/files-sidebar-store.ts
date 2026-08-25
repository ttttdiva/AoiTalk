"use client";

import type {
  ExplorerBookmark,
  ExplorerBookmarkScope,
  ExplorerListResponse,
} from "@/lib/explorer-api";
import type { FilerTab } from "@/contexts/explorer-context";

export type FilesSidebarSnapshot = {
  currentPath: string;
  browseData: ExplorerListResponse | null;
  /** True while the canonical ExplorerProvider is fetching a directory. */
  loading: boolean;
  filerTab: FilerTab;
  focusedItemPath: string | null;
  /** The file opened by the editor, when the canvas focus is in CodeMirror. */
  editingFilePath: string | null;
  userId: string | null;
  selectedSpaceId?: string | null;
  selectedProjectId: string | null;
  /** Admin-only same-Space Project currently browsed by Files, if any. */
  filesTargetProjectId?: string | null;
  /** Project ids whose canonical `space_id` is the selected Space. */
  spaceProjectIds?: string[];
  /** Canonical Project root → Project id map for target execution. */
  spaceProjectTargetMap?: Record<string, string>;
  /** Principal/tab root used to reject stale listings during scope changes. */
  scopeRoot: string;
  scopeKey: string;
  isAdmin: boolean;
  isRemoteWorkspace: boolean;
  bookmarks: ExplorerBookmark[];
  bookmarkScope?: ExplorerBookmarkScope;
  navigate: (path: string) => void;
  selectProjectForPath?: (path: string) => Promise<boolean>;
  closeEditor: () => void;
  refreshBookmarks: () => Promise<void>;
};

/** A stable identity for one ExplorerProvider instance. */
export type FilesSidebarOwner = object;

const noop = () => undefined;
const EMPTY_SNAPSHOT: FilesSidebarSnapshot = {
  currentPath: "",
  browseData: null,
  loading: true,
  filerTab: "workspace",
  focusedItemPath: null,
  editingFilePath: null,
  userId: null,
  selectedSpaceId: null,
  selectedProjectId: null,
  filesTargetProjectId: null,
  spaceProjectIds: [],
  spaceProjectTargetMap: {},
  scopeRoot: "",
  scopeKey: "anon:workspace",
  isAdmin: false,
  isRemoteWorkspace: false,
  bookmarks: [],
  bookmarkScope: { scope: "personal" },
  navigate: noop,
  selectProjectForPath: async () => true,
  closeEditor: noop,
  refreshBookmarks: async () => undefined,
};

let snapshot = EMPTY_SNAPSHOT;
const listeners = new Set<() => void>();
let canonicalOwner: FilesSidebarOwner | null = null;

function emit() {
  listeners.forEach((listener) => listener());
}

export function subscribeFilesSidebar(listener: () => void) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function getFilesSidebarSnapshot() {
  return snapshot;
}

export function getFilesSidebarServerSnapshot() {
  return EMPTY_SNAPSHOT;
}

export function createFilesSidebarOwner(): FilesSidebarOwner {
  return {};
}

/** Claim the canonical bridge before the provider starts publishing. */
export function claimFilesSidebarOwner(owner: FilesSidebarOwner) {
  canonicalOwner = owner;
}

export function publishFilesSidebarState(
  owner: FilesSidebarOwner,
  next: Omit<FilesSidebarSnapshot, "navigate" | "closeEditor" | "refreshBookmarks"> &
    Pick<FilesSidebarSnapshot, "navigate" | "closeEditor" | "refreshBookmarks">,
) {
  // A route transition can briefly overlap two ExplorerProviders. The most
  // recently mounted provider owns the bridge; stale providers must not
  // publish or clear its state during cleanup.
  if (canonicalOwner && canonicalOwner !== owner) return;
  canonicalOwner = owner;
  if (
    snapshot.currentPath === next.currentPath &&
    snapshot.browseData === next.browseData &&
    snapshot.loading === next.loading &&
    snapshot.filerTab === next.filerTab &&
    snapshot.focusedItemPath === next.focusedItemPath &&
    snapshot.editingFilePath === next.editingFilePath &&
    snapshot.userId === next.userId &&
    snapshot.selectedSpaceId === next.selectedSpaceId &&
    snapshot.selectedProjectId === next.selectedProjectId &&
    snapshot.filesTargetProjectId === next.filesTargetProjectId &&
    snapshot.spaceProjectIds === next.spaceProjectIds &&
    snapshot.spaceProjectTargetMap === next.spaceProjectTargetMap &&
    snapshot.scopeRoot === next.scopeRoot &&
    snapshot.scopeKey === next.scopeKey &&
    snapshot.isAdmin === next.isAdmin &&
    snapshot.isRemoteWorkspace === next.isRemoteWorkspace &&
    snapshot.bookmarks === next.bookmarks &&
    snapshot.bookmarkScope === next.bookmarkScope &&
    snapshot.navigate === next.navigate &&
    snapshot.selectProjectForPath === next.selectProjectForPath &&
    snapshot.closeEditor === next.closeEditor &&
    snapshot.refreshBookmarks === next.refreshBookmarks
  ) {
    return;
  }
  snapshot = next;
  emit();
}

export function resetFilesSidebarStore(owner?: FilesSidebarOwner) {
  if (owner && canonicalOwner !== owner) return;
  canonicalOwner = null;
  snapshot = EMPTY_SNAPSHOT;
  emit();
}

export function getFilesSidebarOwnerForTests() {
  return canonicalOwner;
}
