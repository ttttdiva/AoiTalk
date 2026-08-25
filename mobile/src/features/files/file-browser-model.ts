import {
  getFilesMediaKind,
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
  entry: FilesEntry;
  source: FilesSource;
  scope: FilesScope;
  projectRoot: string | null;
};
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
