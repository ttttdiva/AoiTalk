/**
 * File Explorer API Client
 * All endpoints proxy through /api/python-proxy/ → Python FastAPI
 */

import {
  getRemoteWorkspaceContent,
  getRemoteWorkspaceInfo,
  getRemoteWorkspacePreview,
  remoteWorkspaceDownloadBatchUrl,
  remoteWorkspaceDownloadUrl,
  searchRemoteWorkspace,
} from "@/lib/remote-servers";
import { hfServeUrl, isHfPath, parseHfPath } from "@/lib/hf/virtual-path";
import { isHydrusPath } from "@/lib/hydrus/virtual-path";
import { getFileServeUrl } from "@/lib/explorer-serve-url";

// ─── Types ───

export interface ExplorerDirectory {
  name: string;
  path: string;
  item_count?: number;
  modified_at?: string;
  is_shortcut?: boolean;
  shortcut_path?: string;
  /** 代表サムネ用ファイルパス（相対 or 絶対）。未設定なら undefined */
  preview_path?: string;
  /** `.folder-thumb` で明示設定されている場合のみ true（解除メニュー表示用） */
  has_explicit_thumb?: boolean;
}

export interface ExplorerFile {
  name: string;
  path: string;
  type: string;
  size?: number;
  modified_at?: string;
  extension?: string;
  is_shortcut?: boolean;
  shortcut_target?: string;
  virtual_kind?: "record_table";
  project_id?: string;
  record_table_id?: string;
  row_count?: number;
  description?: string | null;
}

export interface ExplorerListResponse {
  success: boolean;
  current_path: string;
  parent_path: string | null;
  can_go_up: boolean;
  directories: ExplorerDirectory[];
  files: ExplorerFile[];
  total_items: number;
  is_admin_mode?: boolean;
}

export interface TreeNode {
  name: string;
  path: string;
  children?: TreeNode[];
}

export interface ExplorerTreeResponse {
  success: boolean;
  tree: TreeNode;
}

export interface ExplorerBookmark {
  id?: string;
  user_id?: string;
  space_id?: string | null;
  name: string;
  path: string;
  icon?: string;
  kind?: "bookmark" | "folder";
  parent_id?: string | null;
  sort_order?: number;
  created_at?: string;
  updated_at?: string;
}

/**
 * Persistence scope for Files bookmarks and launchers.
 *
 * Project Files use the selected Space as their durable ownership boundary;
 * User Files and the legacy/private sources continue to use the authenticated
 * principal's personal collection.  Keep this value explicit on every API
 * operation so an item id/path can never silently select another collection.
 */
export type ExplorerBookmarkScope =
  | { scope: "shared"; spaceId: string }
  | { scope: "personal" };

/** A scope-aware file launcher shown by the Files workspace sidebar. */
export interface ExplorerLauncher {
  id?: string;
  user_id?: string;
  space_id?: string | null;
  name: string;
  path: string;
  icon?: string;
  sort_order?: number;
  created_at?: string;
  updated_at?: string;
}

export interface StorageContext {
  type: string; // "personal" | "project" | "legacy"
  id: string | null;
  name: string;
  icon?: string;
}

export interface StorageContextsResponse {
  success: boolean;
  contexts: StorageContext[];
  current_context: { type: string; id: string | null };
  is_admin: boolean;
}

export interface FilePreview {
  success: boolean;
  type: "text" | "image" | "office" | "binary";
  content?: string;
  data_url?: string;
  mime_type?: string;
  truncated?: boolean;
  extension?: string;
  message?: string;
}

export interface FileInfo {
  success: boolean;
  name: string;
  path: string;
  is_directory: boolean;
  size_bytes: number;
  size_display: string;
  created_at: string;
  modified_at: string;
}

// ─── API Helpers ───

function safeApiErrorFallback(status: number, text: string): string {
  const normalized = text
    .replace(/[\u0000-\u001f\u007f]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  if (!normalized || /<\/?(?:html|body|head)\b|<!doctype\b/i.test(normalized)) {
    return `HTTP ${status}`;
  }
  return normalized.length > 500 ? `${normalized.slice(0, 499)}…` : normalized;
}

function extractApiErrorMessage(status: number, text: string): string {
  if (!text) return `HTTP ${status}`;
  try {
    const json = JSON.parse(text) as { detail?: unknown; error?: unknown };
    const message = json.detail ?? json.error;
    if (typeof message === "string" && message.trim()) {
      return safeApiErrorFallback(status, message);
    }
  } catch {
    // Use a bounded plain-text fallback below.
  }
  return safeApiErrorFallback(status, text);
}

export function explorerErrorMessage(error: unknown): string {
  if (error instanceof Error && error.message.trim()) return error.message;
  if (typeof error === "string" && error.trim()) return error;
  return "不明なエラーです";
}

async function pyFetch<T = unknown>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const res = await fetch(`/api/python-proxy${path}`, {
    credentials: "include",
    cache: "no-store",
    ...init,
    headers: {
      ...(init?.headers || {}),
    },
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(extractApiErrorMessage(res.status, text));
  }
  return res.json();
}

async function pyFetchJson<T = unknown>(
  path: string,
  body: unknown,
): Promise<T> {
  return pyFetch<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

// ─── Explorer APIs ───

// ─── Memo APIs ───

export async function getMemo(): Promise<{
  success: boolean;
  content: string;
}> {
  return pyFetch("/explorer/memo");
}

export async function saveMemo(content: string): Promise<{ success: boolean }> {
  return pyFetch("/explorer/memo", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content }),
  });
}

// ─── Explorer APIs ───

/**
 * Python 側の一覧 API はサイズを `size_bytes` で返すため、
 * UI が参照する `ExplorerFile.size` へ寄せる。
 */
export function normalizeExplorerListResponse(
  data: ExplorerListResponse,
): ExplorerListResponse {
  if (!Array.isArray(data?.files)) return data;
  return {
    ...data,
    files: data.files.map((file) => {
      const raw = file as ExplorerFile & { size_bytes?: unknown };
      if (typeof raw.size === "number") return file;
      if (typeof raw.size_bytes !== "number") return file;
      return { ...file, size: raw.size_bytes };
    }),
  };
}

export async function explorerList(
  path: string = "",
): Promise<ExplorerListResponse> {
  const params = path ? `?path=${encodeURIComponent(path)}` : "";
  return normalizeExplorerListResponse(
    await pyFetch<ExplorerListResponse>(`/explorer/list${params}`),
  );
}

export async function explorerTree(
  root: string = "",
): Promise<ExplorerTreeResponse> {
  const params = root ? `?root=${encodeURIComponent(root)}` : "";
  return pyFetch(`/explorer/tree${params}`);
}

function parseProjectWorkspacePath(
  path: string,
): { projectId: string; relativePath: string } | null {
  if (/^[\\/]|^[A-Za-z]:[\\/]/.test(path)) return null;
  const normalized = path.replace(/\\/g, "/").replace(/^\/+|\/+$/g, "");
  const match = normalized.match(
    /^_projects\/project_([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:\/(.*))?$/i,
  );
  if (!match) return null;
  return { projectId: match[1], relativePath: match[2] ?? "" };
}

export async function explorerMkdir(path: string, name: string) {
  const projectPath = parseProjectWorkspacePath(path);
  if (projectPath) {
    return pyFetchJson<{ success: boolean; name: string; path: string }>(
      `/projects/${encodeURIComponent(projectPath.projectId)}/files/folders`,
      { path: projectPath.relativePath, name },
    );
  }
  return pyFetchJson<{ success: boolean; name: string; path: string }>(
    "/explorer/mkdir",
    { path, name },
  );
}

export interface ExplorerUploadFile {
  file: File;
  relativePath?: string;
}

type ExplorerUploadSource = File | ExplorerUploadFile;

export interface ExplorerUploadFailure {
  name: string;
  relativePath?: string;
  status?: number;
  message: string;
}

export interface ExplorerUploadBatchResult {
  totalCount: number;
  successCount: number;
  failureCount: number;
  results: unknown[];
  failures: ExplorerUploadFailure[];
}

export class ExplorerUploadError extends Error {
  batchResult: ExplorerUploadBatchResult;

  constructor(batchResult: ExplorerUploadBatchResult) {
    super(
      batchResult.successCount > 0
        ? `${batchResult.failureCount}件のアップロードに失敗しました`
        : "アップロードに失敗しました",
    );
    this.name = "ExplorerUploadError";
    this.batchResult = batchResult;
  }
}

function normalizeUploadFile(item: ExplorerUploadSource): ExplorerUploadFile {
  if ("file" in item) return item;
  return {
    file: item,
    relativePath: item.webkitRelativePath || item.name,
  };
}

async function uploadErrorMessage(res: Response): Promise<string> {
  const text = await res.text().catch(() => "");
  if (!text) return `HTTP ${res.status}`;
  try {
    const json = JSON.parse(text) as { detail?: unknown; error?: unknown };
    const message = json.detail ?? json.error;
    if (typeof message === "string" && message.trim()) {
      return safeApiErrorFallback(res.status, message);
    }
  } catch {
    // Use a bounded plain-text fallback below.
  }
  return safeApiErrorFallback(res.status, text);
}

function splitUploadRelativePath(
  relativePath: string | undefined,
  fallbackName: string,
): { directory: string; filename: string } {
  const normalized = (relativePath || fallbackName).replace(/\\/g, "/");
  const separator = normalized.lastIndexOf("/");
  if (separator < 0) {
    return { directory: "", filename: normalized || fallbackName };
  }
  return {
    directory: normalized.slice(0, separator),
    filename: normalized.slice(separator + 1) || fallbackName,
  };
}

export async function explorerUpload(
  path: string,
  files: FileList | ExplorerUploadSource[],
) {
  const items = Array.from(files).map(normalizeUploadFile);
  const projectPath = parseProjectWorkspacePath(path);
  const results = [];
  const failures: ExplorerUploadFailure[] = [];

  for (const item of items) {
    try {
      const uploadRelativePath = splitUploadRelativePath(
        item.relativePath,
        item.file.name,
      );
      const formData = new FormData();
      formData.append(
        "file",
        item.file,
        projectPath ? uploadRelativePath.filename : item.file.name,
      );
      if (!projectPath && item.relativePath) {
        formData.append("relative_path", item.relativePath);
      }

      const uploadPath = projectPath
        ? [projectPath.relativePath, uploadRelativePath.directory]
            .filter(Boolean)
            .join("/")
        : path;
      const url = projectPath
        ? `/api/python-proxy/projects/${encodeURIComponent(projectPath.projectId)}/files/upload?path=${encodeURIComponent(uploadPath)}`
        : `/api/python-proxy/explorer/upload?path=${encodeURIComponent(path)}`;
      const res = await fetch(url, {
        method: "POST",
        credentials: "include",
        body: formData,
      });
      if (!res.ok) {
        failures.push({
          name: item.file.name,
          relativePath: item.relativePath,
          status: res.status,
          message: await uploadErrorMessage(res),
        });
        continue;
      }
      results.push(await res.json());
    } catch (error) {
      failures.push({
        name: item.file.name,
        relativePath: item.relativePath,
        message:
          error instanceof Error ? error.message : "Upload request failed",
      });
    }
  }

  const batchResult: ExplorerUploadBatchResult = {
    totalCount: items.length,
    successCount: results.length,
    failureCount: failures.length,
    results,
    failures,
  };

  if (failures.length > 0) {
    throw new ExplorerUploadError(batchResult);
  }
  return batchResult;
}

export function explorerDownloadUrl(path: string): string {
  const remote = parseRemoteWorkspacePath(path);
  if (remote) {
    return remoteWorkspaceDownloadUrl(remote.profileId, remote.projectId, remote.relativePath);
  }
  return `/api/python-proxy/explorer/download?path=${encodeURIComponent(path)}`;
}

export type RemoteWorkspacePath = {
  profileId: string;
  projectId: string;
  relativePath: string;
};

export function parseRemoteWorkspacePath(path: string): RemoteWorkspacePath | null {
  const match = path.match(/^remote:\/\/([^/]+)\/([^/]+)(?:\/(.*))?$/);
  if (!match) return null;
  return { profileId: match[1], projectId: match[2], relativePath: match[3] ?? "" };
}

function isUnsupportedDownloadPath(path: string): boolean {
  return (
    path.startsWith("aoitalk-record-table:") ||
    isHfPath(path) ||
    isHydrusPath(path)
  );
}

function assertDownloadPaths(paths: string[]): void {
  if (paths.some((path) => typeof path !== "string" || !path.trim())) {
    throw new Error("ダウンロード対象のパスが不正です");
  }
  const hasRemotePrefix = paths.some((path) => path.startsWith("remote://"));
  const hasLocalPath = paths.some((path) => !path.startsWith("remote://"));
  if (hasRemotePrefix && hasLocalPath) {
    throw new Error("リモートファイルとローカルファイルは同時にダウンロードできません");
  }
}

function downloadFilenameFromDisposition(
  disposition: string | null,
  fallback: string,
): string {
  if (!disposition) return fallback;
  const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
  if (encoded) {
    try {
      const decoded = decodeURIComponent(encoded).trim();
      if (decoded) return decoded;
    } catch {
      // fall through to filename below
    }
  }
  const filename = disposition.match(/filename\s*=\s*(?:"([^"]*)"|([^;]*))/i);
  const value = (filename?.[1] ?? filename?.[2] ?? "").trim();
  return value || fallback;
}

function fallbackDownloadFilename(value: string, fallback = "download"): string {
  const withoutQuery = value.split(/[?#]/, 1)[0];
  const trimmed = withoutQuery.replace(/[\\/]+$/, "");
  const basename = trimmed.split(/[/\\]/).pop()?.trim() || "";
  if (!basename) return fallback;
  try {
    return decodeURIComponent(basename);
  } catch {
    return basename;
  }
}

async function preflightDownload(
  url: string,
  init: RequestInit,
): Promise<string | null> {
  const res = await fetch(url, {
    credentials: "include",
    cache: "no-store",
    ...init,
    headers: {
      "x-aoitalk-download-preflight": "1",
      ...(init.headers || {}),
    },
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(extractApiErrorMessage(res.status, text));
  }

  // The preflight only validates authorization and captures response headers.
  // Never materialize the download body in the browser; native navigation below
  // owns the actual streaming response.
  try {
    await res.body?.cancel();
  } catch {
    // A response may have no cancellable body (or already be closed).  The
    // native download should still proceed after a successful preflight.
  }
  return res.headers.get("Content-Disposition");
}

function createNativeDownloadFrame(): HTMLIFrameElement {
  const target = `aoitalk-download-frame-${++downloadFrameCounter}`;
  const iframe = document.createElement("iframe");
  iframe.name = target;
  iframe.width = "1";
  iframe.height = "1";
  iframe.setAttribute("aria-hidden", "true");
  iframe.style.position = "fixed";
  iframe.style.left = "-10000px";
  iframe.style.top = "0";
  iframe.style.width = "1px";
  iframe.style.height = "1px";
  iframe.style.border = "0";
  return iframe;
}

type NativeDownloadContext = {
  iframe: HTMLIFrameElement;
  form?: HTMLFormElement;
};

const activeNativeDownloads = new Set<NativeDownloadContext>();
let pagehideCleanupRegistered = false;

function cleanupNativeDownloadsOnPageHide(): void {
  for (const context of activeNativeDownloads) {
    context.form?.remove();
    context.iframe.remove();
  }
  activeNativeDownloads.clear();
  pagehideCleanupRegistered = false;
  window.removeEventListener("pagehide", cleanupNativeDownloadsOnPageHide);
}

function retainNativeDownload(context: NativeDownloadContext): void {
  activeNativeDownloads.add(context);
  if (pagehideCleanupRegistered) return;
  pagehideCleanupRegistered = true;
  window.addEventListener("pagehide", cleanupNativeDownloadsOnPageHide);
}

function releaseNativeDownload(context: NativeDownloadContext): void {
  activeNativeDownloads.delete(context);
  context.form?.remove();
  context.iframe.remove();
}

function triggerNativeAnchorDownload(
  url: string,
  filename: string,
  useDownloadAttribute: boolean,
): void {
  const iframe = createNativeDownloadFrame();
  const context: NativeDownloadContext = { iframe };
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.target = iframe.name;
  if (useDownloadAttribute) anchor.download = filename;
  anchor.hidden = true;
  anchor.setAttribute("aria-hidden", "true");
  document.body.append(iframe, anchor);
  retainNativeDownload(context);
  let submitted = false;
  try {
    anchor.click();
    submitted = true;
  } finally {
    anchor.remove();
    if (!submitted) releaseNativeDownload(context);
  }
}

let downloadFrameCounter = 0;

function triggerNativeFormDownload(paths: string[], action: string): void {
  const iframe = createNativeDownloadFrame();
  const target = iframe.name;

  const form = document.createElement("form");
  form.method = "POST";
  form.action = action;
  form.target = target;
  form.enctype = "application/x-www-form-urlencoded";
  form.hidden = true;

  const input = document.createElement("input");
  input.type = "hidden";
  input.name = "paths";
  input.value = JSON.stringify(paths);
  form.appendChild(input);

  document.body.append(iframe, form);
  const context: NativeDownloadContext = { iframe, form };
  retainNativeDownload(context);
  let submitted = false;
  try {
    form.submit();
    submitted = true;
  } finally {
    // Keep the browsing context for the full native transfer.  A fixed timer
    // can cancel a large ZIP before its response headers arrive; pagehide is
    // the only safe lifecycle boundary available to this client-side helper.
    if (!submitted) releaseNativeDownload(context);
  }
}

/**
 * Validate a download URL without buffering its response, then let the
 * browser's native download machinery stream the resource into a hidden
 * same-origin anchor.
 */
export async function explorerDownloadResource(
  url: string,
  fallbackFilename?: string,
): Promise<void> {
  const fallback =
    fallbackFilename?.trim() || fallbackDownloadFilename(url, "download");
  const disposition = await preflightDownload(url, { method: "GET" });
  const filename = downloadFilenameFromDisposition(disposition, fallback);
  // Virtual resources do not all guarantee Content-Disposition; preserve the
  // caller-provided fallback only when the preflight did not supply one.
  triggerNativeAnchorDownload(url, filename, !disposition?.trim());
}

export async function explorerDownloadPaths(paths: string[]): Promise<void> {
  if (paths.length === 0) return;
  assertDownloadPaths(paths);

  if (paths.length === 1 && isHfPath(paths[0])) {
    const url = hfServeUrl(paths[0]);
    if (!url) throw new Error("HFファイルのパスが不正です");
    const subPath = parseHfPath(paths[0])?.subPath ?? paths[0];
    await explorerDownloadResource(url, fallbackDownloadFilename(subPath));
    return;
  }

  if (paths.length === 1 && isHydrusPath(paths[0])) {
    const url = getFileServeUrl(paths[0]);
    if (!url) throw new Error("Hydrusファイルのパスが不正です");
    await explorerDownloadResource(url, fallbackDownloadFilename(paths[0]));
    return;
  }

  if (paths.some((path) => isUnsupportedDownloadPath(path))) {
    throw new Error("この種類の仮想ファイルは専用のダウンロード経路を使用してください");
  }

  const remotePaths = paths.map((path) => parseRemoteWorkspacePath(path));
  if (remotePaths.some((path) => path === null)) {
    // A remote:// prefix that does not contain the profile/project segments
    // must never fall through to the local explorer endpoint.
    if (paths.some((path) => path.startsWith("remote://"))) {
      throw new Error("リモートファイルのパスが不正です");
    }
  }

  if (paths.length === 1) {
    const path = paths[0];
    const url = explorerDownloadUrl(path);
    const fallback = fallbackDownloadFilename(path);
    const remote = remotePaths[0];
    // Remote downloads expose a HEAD route so authorization and the
    // Content-Disposition header can be checked without transferring the
    // file body through the browser. Local explorer GET intentionally remains
    // unchanged because its endpoint does not implement HEAD.
    const disposition = await preflightDownload(url, {
      method: remote ? "HEAD" : "GET",
    });
    const filename = downloadFilenameFromDisposition(disposition, fallback);
    // Explorer responses carry Content-Disposition.  Avoid a download
    // attribute here so a race that returns a JSON error cannot be saved as a
    // misleading file; the server header remains the filename source of truth.
    triggerNativeAnchorDownload(url, filename, false);
    return;
  }

  if (remotePaths.every((path): path is RemoteWorkspacePath => path !== null)) {
    const [{ profileId, projectId }] = remotePaths;
    if (
      remotePaths.some(
        (path) => path.profileId !== profileId || path.projectId !== projectId,
      )
    ) {
      throw new Error("異なるリモートプロファイルまたはプロジェクトは同時にダウンロードできません");
    }
    const relativePaths = remotePaths.map((path) => path.relativePath);
    const action = remoteWorkspaceDownloadBatchUrl(profileId, projectId);
    await preflightDownload(action, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paths: relativePaths }),
    });
    triggerNativeFormDownload(relativePaths, action);
    return;
  }

  await preflightDownload("/api/python-proxy/explorer/download", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ paths }),
  });
  triggerNativeFormDownload(paths, "/api/python-proxy/explorer/download");
}

export async function explorerRename(path: string, newName: string) {
  const projectPath = parseProjectWorkspacePath(path);
  const result = await pyFetchJson<{
    success: boolean;
    new_name: string;
    new_path: string;
  }>(
    projectPath
      ? `/projects/${encodeURIComponent(projectPath.projectId)}/files/rename`
      : "/explorer/rename",
    projectPath
      ? { path: projectPath.relativePath, new_name: newName }
      : { path, new_name: newName },
  );
  return projectPath
    ? {
        ...result,
        new_path: result.new_path
          ? `_projects/project_${projectPath.projectId}/${result.new_path}`
          : result.new_path,
      }
    : result;
}

export async function explorerMove(src: string, dest: string) {
  const srcProject = parseProjectWorkspacePath(src);
  const destProject = parseProjectWorkspacePath(dest);
  if (
    srcProject &&
    destProject &&
    srcProject.projectId === destProject.projectId
  ) {
    const result = await pyFetchJson<{ success: boolean; new_path: string }>(
      `/projects/${encodeURIComponent(srcProject.projectId)}/files/move`,
      { src: srcProject.relativePath, dest: destProject.relativePath },
    );
    return {
      ...result,
      new_path: result.new_path
        ? `_projects/project_${srcProject.projectId}/${result.new_path}`
        : result.new_path,
    };
  }
  return pyFetchJson<{ success: boolean; new_path: string }>("/explorer/move", {
    src,
    dest,
  });
}

export async function explorerCopy(src: string, dest: string) {
  const srcProject = parseProjectWorkspacePath(src);
  const destProject = parseProjectWorkspacePath(dest);
  const sameProject =
    srcProject &&
    destProject &&
    srcProject.projectId === destProject.projectId;
  const result = await pyFetchJson<{
    success: boolean;
    new_path: string;
    new_name: string;
  }>(
    sameProject
      ? `/projects/${encodeURIComponent(srcProject.projectId)}/files/copy`
      : "/explorer/copy",
    sameProject
      ? { src: srcProject.relativePath, dest: destProject.relativePath }
      : { src, dest },
  );
  return sameProject
    ? {
        ...result,
        new_path: result.new_path
          ? `_projects/project_${srcProject.projectId}/${result.new_path}`
          : result.new_path,
      }
    : result;
}

export async function explorerArchive(paths: string[], dest: string) {
  const projectPaths = paths.map(parseProjectWorkspacePath);
  const destProject = parseProjectWorkspacePath(dest);
  const projectId = destProject?.projectId;
  const sameProject =
    Boolean(projectId) &&
    projectPaths.every((path) => path?.projectId === projectId);
  const result = await pyFetchJson<{
    success: boolean;
    message: string;
    archive_name: string;
    archive_path: string;
    count: number;
  }>(
    sameProject
      ? `/projects/${encodeURIComponent(projectId!)}/files/archive`
      : "/explorer/archive",
    sameProject
      ? {
          paths: projectPaths.map((path) => path!.relativePath),
          dest: destProject!.relativePath,
        }
      : { paths, dest },
  );
  return sameProject
    ? {
        ...result,
        archive_path: result.archive_path
          ? `_projects/project_${projectId}/${result.archive_path}`
          : result.archive_path,
      }
    : result;
}

export async function explorerExtract(paths: string[], dest: string) {
  const projectPaths = paths.map(parseProjectWorkspacePath);
  const destProject = parseProjectWorkspacePath(dest);
  const projectId = destProject?.projectId;
  const sameProject =
    Boolean(projectId) &&
    projectPaths.every((path) => path?.projectId === projectId);
  const result = await pyFetchJson<{
    success: boolean;
    message: string;
    extracted: { archive_name: string; path: string; name: string }[];
  }>(
    sameProject
      ? `/projects/${encodeURIComponent(projectId!)}/files/extract`
      : "/explorer/extract",
    sameProject
      ? {
          paths: projectPaths.map((path) => path!.relativePath),
          dest: destProject!.relativePath,
        }
      : { paths, dest },
  );
  return sameProject
    ? {
        ...result,
        extracted: result.extracted.map((item) => ({
          ...item,
          path: item.path
            ? `_projects/project_${projectId}/${item.path}`
            : item.path,
        })),
      }
    : result;
}

/** 削除時にバックエンドが返すゴミ箱情報。null は復元不能な物理削除。 */
export interface ExplorerTrashInfo {
  token: string;
  original_path: string;
  name: string;
  is_directory: boolean;
}

export async function explorerDelete(path: string) {
  const projectPath = parseProjectWorkspacePath(path);
  const endpoint = projectPath
    ? `/projects/${encodeURIComponent(projectPath.projectId)}/files?path=${encodeURIComponent(projectPath.relativePath)}`
    : `/explorer/delete?path=${encodeURIComponent(path)}`;
  return pyFetch<{
    success: boolean;
    message: string;
    trash?: ExplorerTrashInfo | null;
  }>(endpoint, {
    method: "DELETE",
  });
}

/** ゴミ箱トークンから削除を取り消す（30日経過や衝突時は 404 系）。 */
export async function explorerRestore(token: string, originalPath?: string) {
  const projectPath = originalPath
    ? parseProjectWorkspacePath(originalPath)
    : null;
  const result = await pyFetchJson<{
    success: boolean;
    restored_path: string;
    name: string;
  }>(
    projectPath
      ? `/projects/${encodeURIComponent(projectPath.projectId)}/files/restore`
      : "/explorer/restore",
    { token },
  );
  return projectPath
    ? {
        ...result,
        restored_path: result.restored_path
          ? `_projects/project_${projectPath.projectId}/${result.restored_path}`
          : result.restored_path,
      }
    : result;
}

export async function explorerInfo(path: string): Promise<FileInfo> {
  const remote = parseRemoteWorkspacePath(path);
  if (remote) {
    return getRemoteWorkspaceInfo(remote.profileId, remote.projectId, remote.relativePath) as unknown as Promise<FileInfo>;
  }
  return pyFetch(`/explorer/info?path=${encodeURIComponent(path)}`);
}

export async function explorerPreview(path: string): Promise<FilePreview> {
  const remote = parseRemoteWorkspacePath(path);
  if (remote) {
    return getRemoteWorkspacePreview(remote.profileId, remote.projectId, remote.relativePath) as unknown as Promise<FilePreview>;
  }
  return pyFetch(`/explorer/preview?path=${encodeURIComponent(path)}`);
}

// ─── Editor ───

export async function explorerSave(
  path: string,
  content: string,
  encoding?: string,
): Promise<{
  success: boolean;
  message: string;
  size_bytes?: number;
  modified_at?: string;
}> {
  const projectPath = parseProjectWorkspacePath(path);
  if (projectPath) {
    const separator = projectPath.relativePath.lastIndexOf("/");
    const directory =
      separator >= 0 ? projectPath.relativePath.slice(0, separator) : "";
    const filename =
      separator >= 0
        ? projectPath.relativePath.slice(separator + 1)
        : projectPath.relativePath;
    if (!filename) {
      throw new Error("保存するファイル名がありません");
    }

    const file = new File([content], filename, {
      type: `text/plain;charset=${encoding || "utf-8"}`,
    });
    await explorerUpload(
      `_projects/project_${projectPath.projectId}${directory ? `/${directory}` : ""}`,
      [file],
    );
    return {
      success: true,
      message: "ファイルを保存しました",
      size_bytes: file.size,
    };
  }

  return pyFetch("/explorer/save", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path, content, encoding }),
  });
}

export interface FullContentResponse {
  success: boolean;
  content: string;
  path: string;
  name: string;
  extension: string;
  size_bytes: number;
  modified_at: string;
  error?: string;
}

export async function explorerFullContent(
  path: string,
): Promise<FullContentResponse> {
  const remote = parseRemoteWorkspacePath(path);
  if (remote) {
    return getRemoteWorkspaceContent(remote.profileId, remote.projectId, remote.relativePath) as unknown as Promise<FullContentResponse>;
  }
  return pyFetch(`/explorer/content?path=${encodeURIComponent(path)}`);
}

export interface SearchResult {
  name: string;
  path: string;
  kind?: "directory" | "file";
  type?: string;
  extension?: string;
  size_bytes?: number;
  size_display?: string;
  item_count?: number | null;
  modified_at?: string;
  icon?: string;
}

export interface ExplorerSearchResponse {
  success: boolean;
  results: SearchResult[];
  total: number;
  total_returned?: number;
  root_path?: string;
  truncated?: boolean;
  query: string;
}

export interface ExplorerSearchOptions {
  /** ファイル名・フォルダ名を正規表現として照合する。 */
  regex?: boolean;
}

export async function explorerSearch(
  query: string,
  root?: string,
  limit?: number,
  options?: ExplorerSearchOptions,
): Promise<ExplorerSearchResponse> {
  const remote = parseRemoteWorkspacePath(root ?? "");
  if (remote) {
    const results = await searchRemoteWorkspace(
      remote.profileId,
      remote.projectId,
      query,
      remote.relativePath,
      limit ?? 50,
    );
    return {
      success: true,
      results: results as unknown as SearchResult[],
      total: results.length,
      query,
      root_path: root,
    };
  }
  const params = new URLSearchParams({ q: query });
  if (root) params.set("root", root);
  if (limit) params.set("limit", String(limit));
  if (options?.regex) params.set("regex", "true");
  return pyFetch(`/explorer/search?${params}`);
}

// ─── OGP ───

export interface OgpData {
  success: boolean;
  title?: string;
  description?: string;
  image?: string;
  url: string;
  favicon?: string;
  embed_type?: "x-post";
  embed_html?: string;
  provider_name?: string;
  error?: string;
}

export async function fetchOgp(url: string): Promise<OgpData> {
  return pyFetch(`/ogp?url=${encodeURIComponent(url)}`);
}

// ─── Bookmarks ───

/**
 * Resolve the canonical endpoint for one persistence scope.
 *
 * Shared collections are rooted at the Space resource (`/api/spaces/{id}`),
 * while personal collections retain the legacy `/api/explorer` route.  This
 * makes the Space identity part of the URL for every GET/POST/PATCH/DELETE
 * operation instead of relying on an item id or an implicit server default.
 */
export function explorerBookmarkScopedPath(
  resource: "bookmarks" | "launchers",
  scope: ExplorerBookmarkScope = { scope: "personal" },
): string {
  return scope.scope === "shared"
    ? `/spaces/${encodeURIComponent(scope.spaceId)}/explorer/${resource}`
    : `/explorer/${resource}`;
}

export async function explorerBookmarks(
  scope: ExplorerBookmarkScope = { scope: "personal" },
): Promise<{
  success: boolean;
  bookmarks: ExplorerBookmark[];
}> {
  return pyFetch(explorerBookmarkScopedPath("bookmarks", scope));
}

export type ExplorerAddBookmarkOptions = {
  kind?: "bookmark" | "folder";
  parent_id?: string | null;
  icon?: string;
};

export async function explorerAddBookmark(
  name: string,
  path?: string,
  icon?: string,
  options?: ExplorerAddBookmarkOptions,
  scope: ExplorerBookmarkScope = { scope: "personal" },
) {
  const body: Record<string, unknown> = { name };
  if (path !== undefined) body.path = path;
  const resolvedIcon = options?.icon ?? icon;
  if (resolvedIcon !== undefined) body.icon = resolvedIcon;
  if (options?.kind !== undefined) body.kind = options.kind;
  if (options?.parent_id !== undefined) body.parent_id = options.parent_id;
  return pyFetchJson<{ success: boolean }>(
    explorerBookmarkScopedPath("bookmarks", scope),
    body,
  );
}

/** Update bookmark presentation/order without changing its target path. */
export async function explorerUpdateBookmark(
  id: string,
  patch: { name?: string; sort_order?: number; parent_id?: string | null },
  scope: ExplorerBookmarkScope = { scope: "personal" },
): Promise<{ success: boolean; bookmark?: ExplorerBookmark }> {
  return pyFetch(
    `${explorerBookmarkScopedPath("bookmarks", scope)}/${encodeURIComponent(id)}`,
    {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
    },
  );
}

export async function explorerRemoveBookmark(
  path: string,
  scope: ExplorerBookmarkScope = { scope: "personal" },
) {
  return pyFetch<{ success: boolean }>(
    explorerBookmarkScopedPath("bookmarks", scope),
    {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    },
  );
}

export async function explorerLaunchers(
  scope: ExplorerBookmarkScope = { scope: "personal" },
): Promise<{
  success: boolean;
  launchers: ExplorerLauncher[];
}> {
  return pyFetch(explorerBookmarkScopedPath("launchers", scope));
}

export async function explorerAddLauncher(
  name: string,
  path: string,
  icon?: string,
  scope: ExplorerBookmarkScope = { scope: "personal" },
): Promise<{ success: boolean; launcher?: ExplorerLauncher }> {
  return pyFetchJson(
    explorerBookmarkScopedPath("launchers", scope),
    { name, path, icon },
  );
}

export async function explorerUpdateLauncher(
  id: string,
  patch: { name?: string; sort_order?: number },
  scope: ExplorerBookmarkScope = { scope: "personal" },
): Promise<{ success: boolean; launcher?: ExplorerLauncher }> {
  return pyFetch(
    `${explorerBookmarkScopedPath("launchers", scope)}/${encodeURIComponent(id)}`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    },
  );
}

export async function explorerRemoveLauncher(
  id: string,
  scope: ExplorerBookmarkScope = { scope: "personal" },
) {
  return pyFetch<{ success: boolean }>(
    `${explorerBookmarkScopedPath("launchers", scope)}/${encodeURIComponent(id)}`,
    { method: "DELETE" },
  );
}

// ─── Folder Thumbnail ───

/** フォルダの代表サムネを明示的に設定する（.folder-thumb 書き込み） */
export async function explorerSetFolderThumbnail(
  folder_path: string,
  target_path: string,
) {
  return pyFetchJson<{ success: boolean; folder_path: string; stored: string }>(
    "/explorer/folder-thumbnail",
    { folder_path, target_path },
  );
}

/** フォルダのサムネ明示設定を解除する（.folder-thumb 削除） */
export async function explorerClearFolderThumbnail(folder_path: string) {
  const qs = `?folder_path=${encodeURIComponent(folder_path)}`;
  return pyFetch<{ success: boolean; folder_path: string }>(
    `/explorer/folder-thumbnail${qs}`,
    { method: "DELETE" },
  );
}

// ─── Storage Contexts ───

export async function storageContexts(): Promise<StorageContextsResponse> {
  return pyFetch("/storage/contexts");
}

// ─── Absolute Filer Paths ───

export interface FilerBrowseResponse {
  folders: {
    name: string;
    path: string;
    item_count?: number;
    thumbnail?: string;
  }[];
  files: { name: string; path: string; type: string; size?: number }[];
  current_path: string;
  parent_path: string | null;
  can_go_up: boolean;
}

export async function filerBrowse(
  path: string = "",
): Promise<FilerBrowseResponse> {
  const params = path ? `?path=${encodeURIComponent(path)}` : "";
  return pyFetch(`/filer/browse${params}`);
}
