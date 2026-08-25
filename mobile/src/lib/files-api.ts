import * as FileSystem from "expo-file-system/legacy";
import { Directory, Paths } from "expo-file-system";
import { fetchApi, getBaseUrl } from "./api-client";
import { getToken, getTokenAuthScope } from "./auth";
import {
  downloadFileToDevice,
  type FilesDownloadResult,
} from "./files-download";
import type {
  FilesBookmark,
  FilesEntry,
  FilesEntryMetadata,
  FilesMediaKind,
  FilesMediaSource,
  FilesScope,
  FilesSource,
  FilesUploadInput,
} from "./files-types";

export type {
  FilesBookmark,
  FilesEntry,
  FilesEntryMetadata,
  FilesMediaKind,
  FilesMediaSource,
  FilesScope,
  FilesSource,
  FilesUploadInput,
} from "./files-types";

/**
 * Identity of the bookmark collection used by the Files API.
 *
 * Bookmark collection ownership is no longer inferred from the authenticated
 * user or the current project.  Callers must state whether they are reading
 * the selected Space's shared collection or the authenticated user's personal
 * collection for every operation.
 */
export type FilesBookmarkScope =
  | { scope: "shared"; spaceId: string }
  | { scope: "personal" };

function bookmarkCollectionUrl(collection: FilesBookmarkScope): string {
  if (collection.scope === "shared") {
    if (!collection.spaceId) {
      throw new Error("共有ブックマークにはSpaceを指定してください");
    }
    return `/api/spaces/${encodeURIComponent(
      collection.spaceId,
    )}/explorer/bookmarks`;
  }
  // Personal bookmarks intentionally retain the legacy endpoint so existing
  // user-owned data remains compatible during the Space migration.
  return "/api/explorer/bookmarks";
}

type ExplorerListPayload = {
  success: boolean;
  current_path: string;
  parent_path?: string | null;
  can_go_up?: boolean;
  is_admin_mode?: boolean;
  directories: Array<{
    name: string;
    path: string;
    item_count?: number;
    modified_at?: string;
  }>;
  files: Array<{
    name: string;
    path: string;
    type?: string;
    extension?: string;
    size_bytes?: number;
    modified_at?: string;
  }>;
};

type ExplorerContentPayload = {
  success: boolean;
  content?: string;
  error?: string;
};

// ローカルは scope を問わず単一の user ディレクトリだけを使う。
// （旧: workspace/user の2区分。workspace 区分は廃止し user へ集約した。）
const LOCAL_ROOT_DIR = "user";
const DEFAULT_MEMO_FILE = "memo.md";
const DEFAULT_MEMO_CONTENT = "# memo\n\n- ここにメモを書けます。\n";
const TEXT_EXTENSIONS = new Set([
  ".md",
  ".markdown",
  ".txt",
  ".json",
  ".yaml",
  ".yml",
  ".toml",
  ".ini",
  ".csv",
  ".tsv",
  ".py",
  ".ts",
  ".tsx",
  ".js",
  ".jsx",
  ".css",
  ".scss",
  ".html",
  ".xml",
  ".sql",
  ".sh",
  ".bat",
  ".ps1",
  ".env",
]);
const IMAGE_EXTENSIONS = new Set([".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"]);
const VIDEO_EXTENSIONS = new Set([".mp4", ".webm", ".mov", ".avi", ".mkv"]);
const AUDIO_EXTENSIONS = new Set([
  ".mp3",
  ".wav",
  ".ogg",
  ".flac",
  ".aac",
  ".m4a",
  ".opus",
  ".wma",
]);
const metadataCache = new Map<string, FilesEntryMetadata>();
const metadataInFlight = new Map<string, Promise<FilesEntryMetadata>>();
const thumbnailCache = new Map<string, Promise<FilesMediaSource>>();

export function clearFilesApiCaches(): void {
  metadataCache.clear();
  metadataInFlight.clear();
  thumbnailCache.clear();
}

export function filesThumbnailCacheKey(
  entry: FilesEntry,
  size: number,
  token: string | null,
): string {
  return JSON.stringify([
    getTokenAuthScope(token),
    token ?? "",
    entry.source,
    entry.path,
    size,
  ]);
}

// ローカルの root は scope を問わず常に user ディレクトリを指す。
// 引数 scope は呼び出し側の互換のため残すが、解決先には影響しない。
function getLocalRootUri(_scope: FilesScope = "user"): string {
  const baseDir = FileSystem.documentDirectory || FileSystem.cacheDirectory;
  if (!baseDir) {
    throw new Error("ローカルファイル用ディレクトリを取得できません");
  }
  return `${baseDir}${LOCAL_ROOT_DIR}/`;
}

function joinLocalUri(
  parentUri: string,
  name: string,
  isDirectory = false,
): string {
  return `${parentUri}${name}${isDirectory ? "/" : ""}`;
}

function getServerChildPath(parentPath: string, name: string): string {
  const trimmedParent = parentPath.replace(/\/+$/, "");
  const trimmedName = name.replace(/^\/+/, "");
  if (!trimmedParent) return trimmedName;
  return `${trimmedParent}/${trimmedName}`;
}

function projectRelativeDirectory(parentPath: string, projectId: string): string {
  const normalized = parentPath.replace(/\\/g, "/").replace(/^\/+/, "");
  const projectRoot = `_projects/project_${projectId}`;
  if (!normalized || normalized === projectRoot) return "";
  if (!normalized.startsWith(`${projectRoot}/`)) {
    throw new Error("現在のフォルダーは選択中プロジェクトの領域ではありません");
  }
  return normalized.slice(projectRoot.length + 1);
}

function getExtension(name: string): string {
  const dotIndex = name.lastIndexOf(".");
  return dotIndex >= 0 ? name.slice(dotIndex).toLowerCase() : "";
}

function inferMimeType(name: string): string | undefined {
  const ext = getExtension(name);
  if (!ext) return undefined;
  if (TEXT_EXTENSIONS.has(ext)) return "text/plain";
  if ([".png"].includes(ext)) return "image/png";
  if ([".jpg", ".jpeg"].includes(ext)) return "image/jpeg";
  if ([".gif"].includes(ext)) return "image/gif";
  if ([".webp"].includes(ext)) return "image/webp";
  if ([".mp4"].includes(ext)) return "video/mp4";
  if ([".webm"].includes(ext)) return "video/webm";
  if ([".mp3"].includes(ext)) return "audio/mpeg";
  if ([".wav"].includes(ext)) return "audio/wav";
  if ([".pdf"].includes(ext)) return "application/pdf";
  return undefined;
}

export function getFilesMediaKind(entry: FilesEntry): FilesMediaKind {
  if (entry.type !== "file") return "other";
  const mime = entry.mimeType || "";
  const ext = entry.extension || getExtension(entry.name);
  if (mime.startsWith("image/") || IMAGE_EXTENSIONS.has(ext)) return "image";
  if (mime.startsWith("video/") || VIDEO_EXTENSIONS.has(ext)) return "video";
  if (mime.startsWith("audio/") || AUDIO_EXTENSIONS.has(ext)) return "audio";
  if (mime.includes("pdf") || ext === ".pdf") return "pdf";
  if (TEXT_EXTENSIONS.has(ext)) return "text";
  return "other";
}

function toIsoString(timestampSeconds?: number | null): string | null {
  if (!timestampSeconds) return null;
  return new Date(timestampSeconds * 1000).toISOString();
}

// 旧ローカル workspace 区分（documentDirectory/workspace/）を一度だけ user へ集約する。
// - 自動生成された既定 memo.md（内容が DEFAULT_MEMO_CONTENT と一致）は移行せず破棄する。
// - それ以外のユーザーファイル・フォルダーは user 直下へ移動し、名前衝突時は " (n)" を付与する。
// - workspace ディレクトリが無ければ何もしない（冪等）。
let localWorkspaceMigration: Promise<void> | null = null;

function ensureLocalWorkspaceMigration(): Promise<void> {
  if (!localWorkspaceMigration) {
    localWorkspaceMigration = migrateLocalWorkspaceToUser().catch(() => {
      // 移行失敗はローカル閲覧を止めない。次回アクセス時に再試行できるよう解除する。
      localWorkspaceMigration = null;
    });
  }
  return localWorkspaceMigration;
}

async function uniqueLocalMoveTarget(
  parentUri: string,
  name: string,
  isDirectory: boolean,
): Promise<string> {
  const dotIndex = isDirectory ? -1 : name.lastIndexOf(".");
  const base = dotIndex > 0 ? name.slice(0, dotIndex) : name;
  const extension = dotIndex > 0 ? name.slice(dotIndex) : "";
  let candidate = joinLocalUri(parentUri, name, isDirectory);
  let index = 1;
  while ((await FileSystem.getInfoAsync(candidate)).exists) {
    candidate = joinLocalUri(
      parentUri,
      `${base} (${index})${extension}`,
      isDirectory,
    );
    index += 1;
  }
  return candidate;
}

// テスト容易性のため公開する。通常は ensureLocalWorkspaceMigration 経由で一度だけ実行される。
export async function migrateLocalWorkspaceToUser(): Promise<void> {
  const baseDir = FileSystem.documentDirectory || FileSystem.cacheDirectory;
  if (!baseDir) return;
  const workspaceUri = `${baseDir}workspace/`;
  const workspaceInfo = await FileSystem.getInfoAsync(workspaceUri);
  if (!workspaceInfo.exists || !workspaceInfo.isDirectory) return;

  const userRoot = getLocalRootUri("user");
  const userInfo = await FileSystem.getInfoAsync(userRoot);
  if (!userInfo.exists) {
    await FileSystem.makeDirectoryAsync(userRoot, { intermediates: true });
  }

  const names = await FileSystem.readDirectoryAsync(workspaceUri);
  for (const name of names) {
    const childUri = joinLocalUri(workspaceUri, name);
    const childInfo = await FileSystem.getInfoAsync(childUri);
    const isDirectory = Boolean(childInfo.isDirectory);
    // 自動生成された既定 memo.md はユーザーデータではないため移行しない。
    if (!isDirectory && name === DEFAULT_MEMO_FILE) {
      const content = await FileSystem.readAsStringAsync(childUri).catch(
        () => null,
      );
      if (content === DEFAULT_MEMO_CONTENT) continue;
    }
    const target = await uniqueLocalMoveTarget(userRoot, name, isDirectory);
    await FileSystem.moveAsync({ from: childUri, to: target });
  }

  await FileSystem.deleteAsync(workspaceUri, { idempotent: true });
}

async function ensureLocalWorkspace(scope: FilesScope): Promise<string> {
  await ensureLocalWorkspaceMigration();
  const rootUri = getLocalRootUri(scope);
  const rootInfo = await FileSystem.getInfoAsync(rootUri);
  if (!rootInfo.exists) {
    await FileSystem.makeDirectoryAsync(rootUri, { intermediates: true });
  }

  const entries = await FileSystem.readDirectoryAsync(rootUri);
  if (entries.length === 0) {
    await FileSystem.writeAsStringAsync(
      joinLocalUri(rootUri, DEFAULT_MEMO_FILE),
      DEFAULT_MEMO_CONTENT,
    );
  }

  return rootUri;
}

async function listLocalDirectory(
  path?: string,
  scope: FilesScope = "workspace",
): Promise<{
  currentPath: string;
  parentPath: string | null;
  canGoUp: boolean;
  isAdminMode: boolean;
  items: FilesEntry[];
}> {
  const rootUri = await ensureLocalWorkspace(scope);
  const currentPath = path || rootUri;
  const children = new Directory(currentPath).list();
  const parentPath = getLocalParentDirectory(currentPath);

  // Directory.list() は種類を含む一覧を一度のnative呼び出しで返す。
  // 全項目への getInfoAsync() は行わず、サイズ等は表示中の行から遅延取得する。
  const entries: FilesEntry[] = children.map((child) => {
    const isDirectory = child instanceof Directory;
    return {
      name: child.name,
      path: child.uri,
      type: isDirectory ? "directory" : "file",
      modifiedAt: null,
      mimeType: isDirectory ? undefined : inferMimeType(child.name),
      extension: getExtension(child.name),
      source: "local",
    };
  });

  entries.sort((a, b) => {
    if (a.type !== b.type) {
      return a.type === "directory" ? -1 : 1;
    }
    return a.name.localeCompare(b.name);
  });

  return {
    currentPath,
    parentPath,
    canGoUp: parentPath !== null,
    isAdminMode: false,
    items: entries,
  };
}

async function listServerDirectory(path = ""): Promise<{
  currentPath: string;
  parentPath: string | null;
  canGoUp: boolean;
  isAdminMode: boolean;
  items: FilesEntry[];
}> {
  const payload = await fetchApi<ExplorerListPayload>(
    `/api/explorer/list${path ? `?path=${encodeURIComponent(path)}` : ""}`,
  );

  if (!payload.success) {
    throw new Error("サーバーファイル一覧の取得に失敗しました");
  }

  const directories: FilesEntry[] = (payload.directories || []).map(
    (entry) => ({
      name: entry.name,
      path: entry.path,
      type: "directory",
      modifiedAt: entry.modified_at ?? null,
      source: "server",
    }),
  );

  const files: FilesEntry[] = (payload.files || []).map((entry) => ({
    name: entry.name,
    path: entry.path,
    type: "file",
    size: entry.size_bytes,
    modifiedAt: entry.modified_at ?? null,
    mimeType: inferMimeType(entry.name),
    extension: entry.extension || getExtension(entry.name),
    source: "server",
  }));

  return {
    currentPath: payload.current_path || path,
    parentPath: payload.parent_path ?? null,
    canGoUp: payload.can_go_up === true,
    isAdminMode: payload.is_admin_mode === true,
    items: [...directories, ...files],
  };
}

async function readLocalTextFile(path: string): Promise<string> {
  return FileSystem.readAsStringAsync(path);
}

async function readServerTextFile(path: string): Promise<string> {
  const payload = await fetchApi<ExplorerContentPayload>(
    `/api/explorer/content?path=${encodeURIComponent(path)}`,
  );
  if (!payload.success) {
    throw new Error(payload.error || "ファイル読み込みに失敗しました");
  }
  return payload.content || "";
}

async function saveLocalTextFile(path: string, content: string): Promise<void> {
  await FileSystem.writeAsStringAsync(path, content);
}

async function saveServerTextFile(
  path: string,
  content: string,
): Promise<void> {
  await fetchApi("/api/explorer/save", {
    method: "PUT",
    body: JSON.stringify({ path, content, encoding: "utf-8" }),
  });
}

async function createLocalTextFile(
  parentPath: string,
  name: string,
): Promise<string> {
  const fileName = name.includes(".") ? name : `${name}.md`;
  const nextPath = joinLocalUri(parentPath, fileName);
  await FileSystem.writeAsStringAsync(nextPath, "");
  return nextPath;
}

async function createServerTextFile(
  parentPath: string,
  name: string,
): Promise<string> {
  const fileName = name.includes(".") ? name : `${name}.md`;
  const nextPath = getServerChildPath(parentPath, fileName);
  await saveServerTextFile(nextPath, "");
  return nextPath;
}

async function createLocalFolder(
  parentPath: string,
  name: string,
): Promise<string> {
  const nextPath = joinLocalUri(parentPath, name, true);
  await FileSystem.makeDirectoryAsync(nextPath, { intermediates: true });
  return nextPath;
}

async function uniqueLocalTargetPath(
  parentPath: string,
  name: string,
  isDirectory = false,
): Promise<string> {
  const extensionIndex = isDirectory ? -1 : name.lastIndexOf(".");
  const base = extensionIndex > 0 ? name.slice(0, extensionIndex) : name;
  const extension = extensionIndex > 0 ? name.slice(extensionIndex) : "";
  let candidate = joinLocalUri(parentPath, name, isDirectory);
  let index = 1;
  while ((await FileSystem.getInfoAsync(candidate)).exists) {
    candidate = joinLocalUri(
      parentPath,
      `${base}-${index}${extension}`,
      isDirectory,
    );
    index += 1;
  }
  return candidate;
}

async function uploadLocalFile(
  parentPath: string,
  file: FilesUploadInput,
): Promise<string> {
  const targetPath = await uniqueLocalTargetPath(parentPath, file.name);
  await FileSystem.copyAsync({ from: file.uri, to: targetPath });
  return targetPath;
}

async function uploadServerProjectFile(
  projectId: string,
  parentPath: string,
  file: FilesUploadInput,
): Promise<void> {
  const baseUrl = await getBaseUrl();
  const token = await getToken();
  const formData = new FormData();
  formData.append("file", {
    uri: file.uri,
    name: file.name,
    type: file.mimeType || inferMimeType(file.name) || "application/octet-stream",
  } as unknown as Blob);
  const directory = projectRelativeDirectory(parentPath, projectId);

  const response = await fetch(
    `${baseUrl}/api/projects/${encodeURIComponent(projectId)}/files/upload?path=${encodeURIComponent(directory)}`,
    {
      method: "POST",
      headers: token ? { Authorization: `Bearer ${token}` } : undefined,
      body: formData,
    },
  );
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(`Upload failed: ${response.status} ${text.slice(0, 300)}`);
  }
}

async function createServerFolder(
  parentPath: string,
  name: string,
): Promise<string> {
  const payload = await fetchApi<{ success?: boolean; path?: string }>(
    "/api/explorer/mkdir",
    {
      method: "POST",
      body: JSON.stringify({ path: parentPath, name }),
    },
  );
  return payload.path || getServerChildPath(parentPath, name);
}

function getLocalParentDirectory(path: string): string | null {
  const normalized = path.endsWith("/") ? path.slice(0, -1) : path;
  const lastSlash = normalized.lastIndexOf("/");
  if (lastSlash < 0) return null;
  const parent = normalized.slice(0, lastSlash + 1);
  const root = getLocalRootUri();
  if (!path.startsWith(root)) return null;
  return parent.length >= root.length ? parent : null;
}

function getLocalEntryName(path: string): string {
  const normalized = path.endsWith("/") ? path.slice(0, -1) : path;
  const lastSlash = normalized.lastIndexOf("/");
  return lastSlash >= 0 ? normalized.slice(lastSlash + 1) : normalized;
}

function getLocalDirectoryPrefix(path: string): string {
  return path.endsWith("/") ? path : `${path}/`;
}

/**
 * Validate and normalize a local file/folder name before it is joined to a URI.
 *
 * Names are deliberately treated as a single path segment.  A local rename
 * must not accidentally turn `a/b` into a nested move, and an empty segment
 * would otherwise resolve to the parent directory.  The remote endpoint keeps
 * its existing validation/contract and does not use this helper.
 */
export function normalizeLocalEntryName(name: string): string {
  if (/[\u0000-\u001f\u007f-\u009f]/.test(name)) {
    throw new Error("名前に制御文字を含めることはできません");
  }
  const normalized = name.trim();
  if (!normalized) {
    throw new Error("名前を入力してください");
  }
  if (normalized === "." || normalized === "..") {
    throw new Error("この名前は使用できません");
  }
  if (/[\\/]/.test(normalized)) {
    throw new Error("名前にフォルダー区切りを含めることはできません");
  }
  return normalized;
}

function decodeLocalEntryName(path: string): string {
  const encodedName = getLocalEntryName(path);
  try {
    // Directory.list() returns a URI whose final segment is percent-encoded,
    // while `entry.name` is decoded.  Decode only that segment; decoding the
    // whole URI first could turn an encoded slash into a path separator.
    return decodeURIComponent(encodedName);
  } catch {
    throw new Error("ローカルパスの名前を読み取れません");
  }
}

function joinLocalEncodedUri(
  parentUri: string,
  name: string,
  isDirectory = false,
): string {
  // Expo's Paths.join encodes URI path segments and keeps `#`, `?`, `%`,
  // spaces, and non-ASCII names out of URL syntax.  Keep a small fallback for
  // older Expo runtimes/test doubles that do not expose Paths at runtime.
  let joined: string;
  if (typeof Paths?.join === "function") {
    joined = Paths.join(parentUri, name);
  } else {
    let encodedName: string;
    try {
      encodedName = encodeURIComponent(name);
    } catch {
      throw new Error("名前に不正な文字が含まれています");
    }
    joined = `${parentUri.replace(/\/+$/, "")}/${encodedName}`;
  }
  return isDirectory && !joined.endsWith("/") ? `${joined}/` : joined;
}

async function renameLocalEntry(
  path: string,
  newName: string,
): Promise<string> {
  const parent = getLocalParentDirectory(path);
  if (!parent) {
    throw new Error("ルートは名前変更できません");
  }
  const normalizedName = normalizeLocalEntryName(newName);
  const targetInfo = await FileSystem.getInfoAsync(path);
  if (!targetInfo.exists) {
    throw new Error("名前変更する項目が見つかりません");
  }
  const currentName = normalizeLocalEntryName(decodeLocalEntryName(path));
  if (normalizedName === currentName) {
    // Android の moveAsync は同一 URI を拒否する実装があるため、同名
    // 保存は冪等な no-op とする。
    return path;
  }
  const nextPath = joinLocalEncodedUri(
    parent,
    normalizedName,
    Boolean(targetInfo.isDirectory),
  );
  const destinationInfo = await FileSystem.getInfoAsync(nextPath);
  if (destinationInfo.exists) {
    throw new Error("同じ名前の項目が既に存在します");
  }
  await FileSystem.moveAsync({ from: path, to: nextPath });
  return nextPath;
}

async function renameServerEntry(
  path: string,
  newName: string,
): Promise<string> {
  const payload = await fetchApi<{ success?: boolean; new_path?: string }>(
    "/api/explorer/rename",
    {
      method: "POST",
      body: JSON.stringify({ path, new_name: newName }),
    },
  );
  return payload.new_path || path;
}

async function moveLocalEntry(
  path: string,
  destinationPath: string,
): Promise<string> {
  const sourceInfo = await FileSystem.getInfoAsync(path);
  if (!sourceInfo.exists) {
    throw new Error("移動元が見つかりません");
  }
  const destinationInfo = await FileSystem.getInfoAsync(destinationPath);
  if (!destinationInfo.exists || !destinationInfo.isDirectory) {
    throw new Error("移動先はフォルダーである必要があります");
  }
  if (
    sourceInfo.isDirectory &&
    getLocalDirectoryPrefix(destinationPath).startsWith(
      getLocalDirectoryPrefix(path),
    )
  ) {
    throw new Error("フォルダーを自分自身の中へ移動できません");
  }

  const nextPath = joinLocalUri(
    destinationPath,
    getLocalEntryName(path),
    sourceInfo.isDirectory,
  );
  if ((await FileSystem.getInfoAsync(nextPath)).exists) {
    throw new Error("移動先に同名の項目があります");
  }
  await FileSystem.moveAsync({ from: path, to: nextPath });
  return nextPath;
}

async function moveServerEntry(
  path: string,
  destinationPath: string,
): Promise<string> {
  const payload = await fetchApi<{ success?: boolean; new_path?: string }>(
    "/api/explorer/move",
    {
      method: "POST",
      body: JSON.stringify({ src: path, dest: destinationPath }),
    },
  );
  return payload.new_path || path;
}

async function copyLocalDirectory(sourcePath: string, targetPath: string) {
  await FileSystem.makeDirectoryAsync(targetPath, { intermediates: true });
  const names = await FileSystem.readDirectoryAsync(sourcePath);
  await Promise.all(
    names.map(async (name) => {
      const childSource = joinLocalUri(sourcePath, name);
      const childInfo = await FileSystem.getInfoAsync(childSource);
      const childTarget = joinLocalUri(targetPath, name, childInfo.isDirectory);
      if (childInfo.isDirectory) {
        await copyLocalDirectory(childSource, childTarget);
      } else {
        await FileSystem.copyAsync({ from: childSource, to: childTarget });
      }
    }),
  );
}

async function copyLocalEntry(
  path: string,
  destinationPath: string,
): Promise<string> {
  const sourceInfo = await FileSystem.getInfoAsync(path);
  if (!sourceInfo.exists) {
    throw new Error("コピー元が見つかりません");
  }
  const destinationInfo = await FileSystem.getInfoAsync(destinationPath);
  if (!destinationInfo.exists || !destinationInfo.isDirectory) {
    throw new Error("コピー先はフォルダーである必要があります");
  }
  if (
    sourceInfo.isDirectory &&
    getLocalDirectoryPrefix(destinationPath).startsWith(
      getLocalDirectoryPrefix(path),
    )
  ) {
    throw new Error("フォルダーを自分自身の中へコピーできません");
  }

  const nextPath = await uniqueLocalTargetPath(
    destinationPath,
    getLocalEntryName(path),
    sourceInfo.isDirectory,
  );
  if (sourceInfo.isDirectory) {
    await copyLocalDirectory(path, nextPath);
  } else {
    await FileSystem.copyAsync({ from: path, to: nextPath });
  }
  return nextPath;
}

async function copyServerEntry(
  path: string,
  destinationPath: string,
): Promise<string> {
  const payload = await fetchApi<{ success?: boolean; new_path?: string }>(
    "/api/explorer/copy",
    {
      method: "POST",
      body: JSON.stringify({ src: path, dest: destinationPath }),
    },
  );
  return payload.new_path || path;
}

async function deleteLocalEntry(path: string): Promise<void> {
  await FileSystem.deleteAsync(path, { idempotent: true });
}

async function deleteServerEntry(path: string): Promise<void> {
  await fetchApi(`/api/explorer/delete?path=${encodeURIComponent(path)}`, {
    method: "DELETE",
  });
}

async function getServerMediaUrl(path: string): Promise<string> {
  const baseUrl = await getBaseUrl();
  return `${baseUrl}/api/explorer/serve?path=${encodeURIComponent(path)}`;
}

async function getServerThumbnailUrl(path: string, kind: FilesMediaKind, size = 320) {
  const baseUrl = await getBaseUrl();
  if (kind === "video") {
    return `${baseUrl}/api/explorer/video-thumbnail?path=${encodeURIComponent(path)}`;
  }
  return `${baseUrl}/api/explorer/image-thumbnail?path=${encodeURIComponent(path)}&size=${encodeURIComponent(String(size))}`;
}

async function getAuthHeaders(): Promise<Record<string, string> | undefined> {
  const token = await getToken();
  return token ? { Authorization: `Bearer ${token}` } : undefined;
}

function hashPath(value: string): string {
  let hash = 0;
  for (let index = 0; index < value.length; index += 1) {
    hash = (hash * 31 + value.charCodeAt(index)) >>> 0;
  }
  return hash.toString(16);
}

function cacheNameFor(entry: FilesEntry, authScope: string): string {
  const ext = entry.extension || getExtension(entry.name) || ".bin";
  const safeExt = ext.startsWith(".") ? ext : `.${ext}`;
  return `filer-${hashPath(`${authScope}:${entry.source}:${entry.path}`)}${safeExt}`;
}

async function downloadServerMediaToCache(
  entry: FilesEntry,
): Promise<string> {
  const cacheRoot = FileSystem.cacheDirectory;
  if (!cacheRoot) throw new Error("メディアキャッシュを利用できません");
  const token = await getToken();
  const targetUri = `${cacheRoot}${cacheNameFor(entry, getTokenAuthScope(token))}`;
  const info = await FileSystem.getInfoAsync(targetUri);
  if (info.exists) return targetUri;

  const url = await getServerMediaUrl(entry.path);
  const headers = token ? { Authorization: `Bearer ${token}` } : undefined;
  try {
    const result = await FileSystem.downloadAsync(
      url,
      targetUri,
      headers ? { headers } : undefined,
    );
    if (result.status < 200 || result.status >= 300) {
      throw new Error(`サーバーがエラーを返しました (${result.status})`);
    }
    return result.uri;
  } catch (error) {
    await FileSystem.deleteAsync(targetUri, { idempotent: true }).catch(() => {});
    throw error;
  }
}

async function downloadServerFileForExport(entry: FilesEntry): Promise<string> {
  const cacheRoot = FileSystem.cacheDirectory;
  if (!cacheRoot) throw new Error("ファイル保存用キャッシュを利用できません");
  const token = await getToken();
  const baseName = cacheNameFor(entry, getTokenAuthScope(token));
  const extension = getExtension(baseName);
  const stem = extension ? baseName.slice(0, -extension.length) : baseName;
  const targetUri = `${cacheRoot}${stem}-download-${Date.now()}-${Math.random()
    .toString(16)
    .slice(2)}${extension}`;
  const url = await getServerMediaUrl(entry.path);
  const headers = token ? { Authorization: `Bearer ${token}` } : undefined;

  try {
    const result = await FileSystem.downloadAsync(
      url,
      targetUri,
      headers ? { headers } : undefined,
    );
    if (result.status < 200 || result.status >= 300) {
      throw new Error(`サーバーがエラーを返しました (${result.status})`);
    }
    return result.uri;
  } catch (error) {
    await FileSystem.deleteAsync(targetUri, { idempotent: true }).catch(() => {});
    throw error;
  }
}

export function isTextEntry(entry: FilesEntry): boolean {
  if (entry.type !== "file") return false;
  const extension = entry.extension || getExtension(entry.name);
  return TEXT_EXTENSIONS.has(extension);
}

export function formatDisplayPath(
  source: FilesSource,
  path: string,
  scope: FilesScope = "workspace",
): string {
  if (!path) {
    return "/";
  }

  if (source === "local") {
    const root = getLocalRootUri(scope);
    return path.startsWith(root) ? `/${path.slice(root.length)}` : path;
  }

  return path.startsWith("/") ? path : `/${path}`;
}

export function getParentPath(
  source: FilesSource,
  path: string,
  scope: FilesScope = "workspace",
): string | null {
  if (!path) return null;

  if (source === "local") {
    const parent = getLocalParentDirectory(path);
    const root = getLocalRootUri(scope);
    if (!parent || parent === path) return null;
    return path === root ? null : parent;
  }

  const normalized = path.replace(/\/+$/, "");
  if (!normalized) return null;
  const slashIndex = normalized.lastIndexOf("/");
  if (slashIndex < 0) return "";
  return normalized.slice(0, slashIndex);
}

export const filesApi = {
  async list(
    source: FilesSource,
    path?: string,
    scope: FilesScope = "workspace",
  ) {
    return source === "local"
      ? listLocalDirectory(path, scope)
      : listServerDirectory(path);
  },

  async readText(source: FilesSource, path: string) {
    return source === "local"
      ? readLocalTextFile(path)
      : readServerTextFile(path);
  },

  async saveText(source: FilesSource, path: string, content: string) {
    if (source === "local") {
      await saveLocalTextFile(path, content);
    } else {
      await saveServerTextFile(path, content);
    }
  },

  async createTextFile(source: FilesSource, parentPath: string, name: string) {
    return source === "local"
      ? createLocalTextFile(parentPath, name)
      : createServerTextFile(parentPath, name);
  },

  async createFolder(source: FilesSource, parentPath: string, name: string) {
    return source === "local"
      ? createLocalFolder(parentPath, name)
      : createServerFolder(parentPath, name);
  },

  async upload(
    source: FilesSource,
    parentPath: string,
    file: FilesUploadInput,
    options?: { projectId?: string | null },
  ) {
    if (source === "local") {
      return uploadLocalFile(parentPath, file);
    }
    if (!options?.projectId) {
      throw new Error("サーバーへアップロードするにはプロジェクト選択が必要です");
    }
    await uploadServerProjectFile(options.projectId, parentPath, file);
    return null;
  },

  async rename(source: FilesSource, path: string, newName: string) {
    return source === "local"
      ? renameLocalEntry(path, newName)
      : renameServerEntry(path, newName);
  },

  async move(source: FilesSource, path: string, destinationPath: string) {
    return source === "local"
      ? moveLocalEntry(path, destinationPath)
      : moveServerEntry(path, destinationPath);
  },

  async copy(source: FilesSource, path: string, destinationPath: string) {
    return source === "local"
      ? copyLocalEntry(path, destinationPath)
      : copyServerEntry(path, destinationPath);
  },

  async remove(source: FilesSource, path: string) {
    if (source === "local") {
      await deleteLocalEntry(path);
    } else {
      await deleteServerEntry(path);
    }
  },

  async download(entry: FilesEntry): Promise<FilesDownloadResult> {
    let temporaryUri: string | null = null;
    try {
      return await downloadFileToDevice(entry, async () => {
        if (entry.source === "local") {
          const info = await FileSystem.getInfoAsync(entry.path);
          if (!info.exists || info.isDirectory) {
            throw new Error("ローカルファイルが見つかりません");
          }
          return entry.path;
        }
        temporaryUri = await downloadServerFileForExport(entry);
        return temporaryUri;
      });
    } finally {
      if (temporaryUri) {
        await FileSystem.deleteAsync(temporaryUri, { idempotent: true }).catch(
          () => {},
        );
      }
    }
  },

  getMetadata(entry: FilesEntry): Promise<FilesEntryMetadata> {
    if (entry.source !== "local" || entry.type === "directory") {
      return Promise.resolve({ size: entry.size, modifiedAt: entry.modifiedAt });
    }
    const key = `${entry.source}:${entry.path}`;
    const cached = metadataCache.get(key);
    if (cached) return Promise.resolve(cached);
    const running = metadataInFlight.get(key);
    if (running) return running;

    const flight = FileSystem.getInfoAsync(entry.path)
      .then((info) => {
        const metadata = {
          size: info.exists && !info.isDirectory ? info.size : undefined,
          modifiedAt: info.exists ? toIsoString(info.modificationTime) : null,
        };
        metadataCache.set(key, metadata);
        return metadata;
      })
      .finally(() => {
        if (metadataInFlight.get(key) === flight) metadataInFlight.delete(key);
      });
    metadataInFlight.set(key, flight);
    return flight;
  },

  getMediaSource(
    entry: FilesEntry,
    options?: { thumbnail?: boolean; size?: number },
  ): Promise<FilesMediaSource> {
    const kind = getFilesMediaKind(entry);
    if (!options?.thumbnail) {
      return (async () => {
        if (entry.source === "local") return { uri: entry.path };
        const headers = await getAuthHeaders();
        const uri = await getServerMediaUrl(entry.path);
        return headers ? { uri, headers } : { uri };
      })();
    }

    // 元画像を一覧へ渡すとAndroidがタブ遷移直後にフル画像をdecodeするため、
    // ローカルは軽量アイコンを使う。サーバーthumbnail要求はlocation単位で共有する。
    if (entry.source === "local") {
      return Promise.reject(new Error("ローカル一覧では元画像をサムネイルに使用しません"));
    }
    return (async () => {
      const token = await getToken();
      // token本体もkeyに含め、同一userのrefresh後に古いAuthorizationを再利用しない。
      const key = filesThumbnailCacheKey(entry, options.size ?? 320, token);
      const cached = thumbnailCache.get(key);
      if (cached) return cached;
      const flight = (async () => {
        const headers = token ? { Authorization: `Bearer ${token}` } : undefined;
        const uri = await getServerThumbnailUrl(entry.path, kind, options.size);
        return headers ? { uri, headers } : { uri };
      })().catch((error) => {
        if (thumbnailCache.get(key) === flight) thumbnailCache.delete(key);
        throw error;
      });
      thumbnailCache.set(key, flight);
      return flight;
    })();
  },

  async getPlayableUri(entry: FilesEntry) {
    if (entry.source === "local") return entry.path;
    return downloadServerMediaToCache(entry);
  },

  async listBookmarks(
    collection: FilesBookmarkScope,
  ): Promise<{ success?: boolean; bookmarks: FilesBookmark[] }> {
    return fetchApi(bookmarkCollectionUrl(collection));
  },

  async addBookmark(
    name: string,
    path: string,
    icon = "📁",
    collection: FilesBookmarkScope,
  ) {
    return fetchApi<{ success?: boolean; bookmark?: FilesBookmark }>(
      bookmarkCollectionUrl(collection),
      {
        method: "POST",
        body: JSON.stringify({ name, path, icon }),
      },
    );
  },

  async removeBookmark(path: string, collection: FilesBookmarkScope) {
    return fetchApi<{ success?: boolean }>(bookmarkCollectionUrl(collection), {
      method: "DELETE",
      body: JSON.stringify({ path }),
    });
  },
};
