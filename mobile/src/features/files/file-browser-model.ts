import {
  canRouteServerFileTransfer,
  getFilesMediaKind,
  getParentPath,
  isTextEntry,
  type FilesEntry,
  type FilesScope,
  type FilesSource,
} from "../../lib/files-api";

export const SOURCE_LABELS: Record<FilesSource, string> = {
  local: "ローカル",
  server: "サーバー",
};

export const SCOPE_LABELS: Record<FilesScope, string> = {
  workspace: "ワークスペース",
  user: "ユーザー",
};

const FILE_ICONS: Record<string, string> = {
  directory: "folder",
  image: "file-image-outline",
  video: "file-video-outline",
  audio: "file-music-outline",
  pdf: "file-pdf-box",
  text: "file-document-edit-outline",
  default: "file-outline",
};

export type LocationKey = `${FilesSource}:${FilesScope}`;
export type LocationState = Record<LocationKey, string>;
export type HistoryState = Record<LocationKey, string[]>;
export type LocationMeta = {
  parentPath: string | null;
  canGoUp: boolean;
  isAdminMode: boolean;
};
export type LocationMetaState = Record<LocationKey, LocationMeta>;
export type ClipboardOperation = "copy" | "move";
export type ClipboardState = {
  operation: ClipboardOperation;
  entries: FilesEntry[];
  source: FilesSource;
  scope: FilesScope;
  authScope?: string;
  sourcePath?: string;
  projectId?: string | null;
  projectRoot: string | null;
};
export type FilesPressAction =
  | "open"
  | "start-selection"
  | "toggle-selection";
export type ViewMode = "grid" | "list";
export type AudioState = {
  track: FilesEntry | null;
  playlist: FilesEntry[];
  index: number;
  scope: FilesScope;
  rootPath: string;
  loading: boolean;
  playing: boolean;
  positionMillis: number;
  durationMillis: number;
};

export const initialPaths: LocationState = {
  "local:workspace": "",
  "local:user": "",
  "server:workspace": "",
  "server:user": "",
};

export const initialHistories: HistoryState = {
  "local:workspace": [],
  "local:user": [],
  "server:workspace": [],
  "server:user": [],
};

export const initialLocationMetas: LocationMetaState = {
  "local:workspace": { parentPath: null, canGoUp: false, isAdminMode: false },
  "local:user": { parentPath: null, canGoUp: false, isAdminMode: false },
  "server:workspace": { parentPath: null, canGoUp: false, isAdminMode: false },
  "server:user": { parentPath: null, canGoUp: false, isAdminMode: false },
};

export function locationKey(
  source: FilesSource,
  scope: FilesScope,
): LocationKey {
  return `${source}:${scope}`;
}

export function resolveFilesHomePath(options: {
  source: FilesSource;
  scope: FilesScope;
  currentPath: string;
  serverRootPath: string;
}): string {
  if (options.source === "server") return options.serverRootPath;

  let cursor = options.currentPath;
  while (cursor) {
    const parent = getParentPath(options.source, cursor, options.scope);
    if (parent == null) return cursor;
    cursor = parent;
  }
  return options.currentPath;
}

/** Segment-aware comparison: project_a is not the parent of project_ab. */
export function isFilesPathWithinRoot(path: string, root: string): boolean {
  const normalize = (value: string) => value.replace(/\\/g, "/").replace(/^\/+|\/+$/g, "");
  const normalized = normalize(path);
  const prefix = normalize(root);
  return Boolean(prefix) && !normalized.split("/").includes("..") &&
    (normalized === prefix || normalized.startsWith(`${prefix}/`));
}

/** A visible Up control must always have a usable, authorized destination. */
export function resolveFilesParentPath(options: {
  source: FilesSource;
  scope: FilesScope;
  currentPath: string;
  isAdmin: boolean;
  projectRoot: string;
  parentPath: string | null;
  canGoUp: boolean;
}): string | null {
  const workspace = options.source === "server" && options.scope === "workspace";
  if (workspace && options.isAdmin) {
    const current = options.currentPath.replace(/\\/g, "/").replace(/^\/+|\/+$/g, "");
    if (!current) return null;
    // Some project-root listings omit parent metadata. An administrator can
    // still return through _projects to the administrator root.
    return options.parentPath ?? current.split("/").slice(0, -1).join("/");
  }
  if (!options.canGoUp || options.parentPath == null) return null;
  if (workspace && !isFilesPathWithinRoot(options.parentPath, options.projectRoot)) return null;
  return options.parentPath;
}

export function canMutateFilesLocation(options: {
  source: FilesSource;
  staleActive: boolean;
  offline: boolean;
  authenticated: boolean;
  activePath: string;
  isAdminMode: boolean;
}): boolean {
  // A stale cache marker only describes a server listing. It must never make
  // the device-local user directory read-only after switching back to Local.
  const serverWriteBlocked =
    options.source === "server" && (options.staleActive || options.offline);

  return (
    !serverWriteBlocked &&
    (options.source === "local" || options.authenticated) &&
    (Boolean(options.activePath) ||
      (options.source === "server" && options.isAdminMode))
  );
}

/**
 * Return the stable identity used by selection state for one entry.
 *
 * Paths are only unique within a source, so include the source as well.  The
 * location (scope/path/project/auth) is tracked by the screen and clears the
 * selection when it changes.
 */
export function fileEntrySelectionKey(entry: FilesEntry): string {
  return `${entry.source}:${entry.path}`;
}

/** Resolve the tap/long-press contract used by the mobile file list. */
export function resolveFilesPressAction(options: {
  wasLongPress: boolean;
  selectionMode: boolean;
}): FilesPressAction {
  if (options.wasLongPress) return "start-selection";
  return options.selectionMode ? "toggle-selection" : "open";
}

export function startFileSelection(entry: FilesEntry): FilesEntry[] {
  return [entry];
}

export function fileSelectionCount(selected: readonly FilesEntry[]): number {
  return selected.length;
}

/** Toggle an entry while preserving the order in which entries were selected. */
export function toggleFileSelection(
  selected: readonly FilesEntry[],
  entry: FilesEntry,
): FilesEntry[] {
  const key = fileEntrySelectionKey(entry);
  if (selected.some((candidate) => fileEntrySelectionKey(candidate) === key)) {
    return selected.filter(
      (candidate) => fileEntrySelectionKey(candidate) !== key,
    );
  }
  return [...selected, entry];
}

/**
 * Reconcile selection against a refreshed listing.  This drops entries that
 * disappeared while retaining the latest metadata for entries still present.
 */
export function reconcileFileSelection(
  selected: readonly FilesEntry[],
  available: readonly FilesEntry[],
): FilesEntry[] {
  const latestByKey = new Map(
    available.map((entry) => [fileEntrySelectionKey(entry), entry]),
  );
  return selected
    .map((entry) => latestByKey.get(fileEntrySelectionKey(entry)))
    .filter((entry): entry is FilesEntry => Boolean(entry));
}

export function dedupeFileEntries(entries: readonly FilesEntry[]): FilesEntry[] {
  const seen = new Set<string>();
  return entries.filter((entry) => {
    const key = fileEntrySelectionKey(entry);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

/** A source/path boundary key for invalidating selection on navigation. */
export function filesLocationIdentityKey(options: {
  source: FilesSource;
  scope: FilesScope;
  path: string;
  authScope?: string;
  projectId?: string | null;
}): string {
  return JSON.stringify([
    options.source,
    options.scope,
    options.path,
    options.authScope ?? "",
    options.projectId ?? "",
  ]);
}

/** Validate every clipboard entry against a destination before mutation. */
export function canRouteClipboardEntries(
  clipboard: ClipboardState | null,
  destinationPath: string,
): boolean {
  if (!clipboard || clipboard.entries.length === 0 || !destinationPath) {
    return false;
  }
  return clipboard.entries.every(
    (entry) =>
      entry.source === clipboard.source &&
      canRouteServerFileTransfer(entry.path, destinationPath),
  );
}

/**
 * Return every directory listing affected by a clipboard operation.
 *
 * A move changes both the captured source directory and the destination;
 * copying changes only the destination.  Keeping this as pure logic makes
 * the cross-directory cache contract explicit and easy to regression-test.
 */
export function getClipboardAffectedPaths(
  clipboard: ClipboardState,
  destinationPath: string,
): string[] {
  const affected = new Set<string>();
  if (clipboard.operation === "move" && clipboard.sourcePath) {
    affected.add(clipboard.sourcePath);
  }
  if (destinationPath) affected.add(destinationPath);
  return [...affected];
}

export function getFileIcon(entry: FilesEntry): string {
  if (entry.type === "directory") return FILE_ICONS.directory;
  const kind = getFilesMediaKind(entry);
  if (kind === "image") return FILE_ICONS.image;
  if (kind === "video") return FILE_ICONS.video;
  if (kind === "audio") return FILE_ICONS.audio;
  if (kind === "pdf") return FILE_ICONS.pdf;
  if (isTextEntry(entry)) return FILE_ICONS.text;
  return FILE_ICONS.default;
}

export function formatSize(bytes?: number): string {
  if (!bytes) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) {
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

export function formatScopedServerPath(
  path: string,
  rootPath: string,
): string {
  if (!path || path === rootPath) return "/";
  if (path === "__drives__") return "/drives";
  if (/^[A-Za-z]:[\\/]/.test(path)) return path.replace(/\\/g, "/");
  const normalizedRoot = rootPath.replace(/\/+$/, "");
  const relative =
    normalizedRoot && path.startsWith(normalizedRoot)
      ? path.slice(normalizedRoot.length).replace(/^\/+/, "")
      : path.replace(/^\/+/, "");
  return relative ? `/${relative}` : "/";
}

export function formatTime(ms?: number): string {
  if (!ms || ms < 0) return "0:00";
  const totalSeconds = Math.floor(ms / 1000);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes}:${seconds.toString().padStart(2, "0")}`;
}

export function isViewableMedia(entry: FilesEntry): boolean {
  const kind = getFilesMediaKind(entry);
  return kind === "image" || kind === "video";
}

export function isAudioEntry(entry: FilesEntry): boolean {
  return getFilesMediaKind(entry) === "audio";
}

export type FilesOpenKind =
  | "directory"
  | "audio"
  | "media"
  | "text"
  | "unsupported";

export function resolveFilesOpenKind(entry: FilesEntry): FilesOpenKind {
  if (entry.type === "directory") return "directory";
  if (isAudioEntry(entry)) return "audio";
  if (isViewableMedia(entry)) return "media";
  if (isTextEntry(entry)) return "text";
  return "unsupported";
}

export function sortAudioEntries(entries: FilesEntry[]): FilesEntry[] {
  const seen = new Set<string>();
  return entries
    .filter((entry) => {
      if (seen.has(entry.path)) return false;
      seen.add(entry.path);
      return true;
    })
    .sort((a, b) => a.path.localeCompare(b.path, undefined, { numeric: true }));
}
