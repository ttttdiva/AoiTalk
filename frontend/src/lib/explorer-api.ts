/**
 * File Explorer API Client
 * All endpoints proxy through /api/python-proxy/ → Python FastAPI
 */

import type { ConfirmOptions } from "@/hooks/use-confirm";

import { readFileTransferCompatibilityPreference } from "@/lib/file-transfer-preference";
import {
  downloadFileTransfer,
  FileTransferNativeRequiredError,
  saveFileTransfer,
  triggerFileTransferDownload,
  uploadFileTransfer,
  type FileTransferSaveMeta,
} from "@/lib/file-transfer-client";
import { MAX_COMPAT_UPLOAD_FILE_BYTES } from "@/lib/file-transfer-protocol";
import { withBasePath, withoutBasePath } from "@/lib/base-path";

import {
  getRemoteWorkspaceContent,
  getRemoteWorkspaceInfo,
  getRemoteWorkspacePreview,
  remoteWorkspaceDownloadBatchUrl,
  remoteWorkspaceDownloadUrl,
  searchRemoteWorkspace,
} from "@/lib/remote-servers";

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
  /** Optional file metadata returned for regular files by get_file_info(). */
  type?: string;
  extension?: string;
  icon?: string;
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
    const detail = json.detail ?? json.error;
    const message = detail && typeof detail === "object" && "message" in detail
      ? (detail as { message?: unknown }).message
      : detail;
    if (typeof message === "string" && message.trim()) {
      return safeApiErrorFallback(status, message);
    }
  } catch {
    // Use a bounded plain-text fallback below.
  }
  return safeApiErrorFallback(status, text);
}

function extractApiErrorCode(text: string): string | undefined {
  try {
    const json = JSON.parse(text) as { detail?: unknown };
    if (!json.detail || typeof json.detail !== "object" || !("code" in json.detail)) {
      return undefined;
    }
    const code = (json.detail as { code?: unknown }).code;
    return typeof code === "string" ? code : undefined;
  } catch {
    return undefined;
  }
}

/** Structured failure metadata for callers that need to distinguish API conflicts. */
export class ExplorerApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
    public readonly code: string | undefined,
  ) {
    super(message);
    this.name = "ExplorerApiError";
  }
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
    throw new ExplorerApiError(
      res.status,
      extractApiErrorMessage(res.status, text),
      extractApiErrorCode(text),
    );
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

function isProjectWorkspaceNamespacePath(path: string): boolean {
  const normalized = path.replace(/\\/g, "/").replace(/^\/+|\/+$/g, "");
  return /^_projects(?:\/|$)/i.test(normalized);
}

function resolveProjectMutationPath(
  path: string,
  operation: "移動" | "コピー",
  allowRoot = false,
): { projectId: string; relativePath: string } | null {
  const projectPath = parseProjectWorkspacePath(path);
  if (!projectPath && isProjectWorkspaceNamespacePath(path)) {
    throw new Error(
      `プロジェクトファイルの${operation}には _projects/project_<uuid> 形式のパスが必要です`,
    );
  }
  if (projectPath) {
    if (!allowRoot && !projectPath.relativePath) {
      throw new Error(`プロジェクトルート自体は${operation}できません`);
    }
    return projectPath;
  }
  return null;
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
  skippedCount?: number;
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
  return extractApiErrorMessage(res.status, text);
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

export interface ExplorerUploadOptions {
  confirmOverwrite?: (options: ConfirmOptions) => Promise<boolean>;
  /** Only for an intentional editor save, never for new files or imports. */
  overwrite?: boolean;
}

export async function explorerUpload(
  path: string,
  files: FileList | ExplorerUploadSource[],
  options: ExplorerUploadOptions = {},
) {
  const items = Array.from(files).map(normalizeUploadFile);
  const projectPath = parseProjectWorkspacePath(path);
  const compatibilityEnabled = readFileTransferCompatibilityPreference();
  const itemResults: Array<unknown | undefined> = new Array(items.length);
  const itemFailures: Array<ExplorerUploadFailure | undefined> = new Array(items.length);
  const skippedItems = new Set<number>();
  const conflicts: Array<{
    index: number;
    item: ExplorerUploadFile;
    send: (overwrite: boolean) => Promise<Response>;
  }> = [];

  for (const [index, item] of items.entries()) {
    try {
      const uploadRelativePath = splitUploadRelativePath(
        item.relativePath,
        item.file.name,
      );
      const nativeFormData = new FormData();
      nativeFormData.append(
        "file",
        item.file,
        projectPath ? uploadRelativePath.filename : item.file.name,
      );
      if (!projectPath && item.relativePath) {
        nativeFormData.append("relative_path", item.relativePath);
      }

      const uploadPath = projectPath
        ? [projectPath.relativePath, uploadRelativePath.directory]
            .filter(Boolean)
            .join("/")
        : path;
      const url = projectPath
        ? `/api/python-proxy/projects/${encodeURIComponent(projectPath.projectId)}/files/upload?path=${encodeURIComponent(uploadPath)}`
        : `/api/python-proxy/explorer/upload?path=${encodeURIComponent(path)}`;
      const send = async (overwrite: boolean) => {
        const requestUrl = overwrite ? `${url}&overwrite=true` : url;
        if (
          compatibilityEnabled &&
          item.file.size <= MAX_COMPAT_UPLOAD_FILE_BYTES
        ) {
          return uploadFileTransfer(
            {
              operation: "upload",
              url: requestUrl,
              fileName: projectPath
                ? uploadRelativePath.filename
                : item.file.name,
              fileType: item.file.type,
              ...(projectPath || !item.relativePath
                ? {}
                : { relativePath: item.relativePath }),
            },
            item.file,
          );
        }

        return fetch(requestUrl, {
          method: "POST",
          credentials: "include",
          body: nativeFormData,
        });
      };
      const res = await send(options.overwrite === true);
      if (res.status === 409 && options.overwrite !== true) {
        const conflict = await res.clone().json().catch(() => null);
        if (conflict?.detail?.code === "already_exists") {
          if (options.confirmOverwrite) {
            conflicts.push({ index, item, send });
            continue;
          }
        }
      }
      if (!res.ok) {
        itemFailures[index] = {
          name: item.file.name,
          relativePath: item.relativePath,
          status: res.status,
          message: await uploadErrorMessage(res),
        };
        continue;
      }
      itemResults[index] = await res.json();
    } catch (error) {
      itemFailures[index] = {
        name: item.file.name,
        relativePath: item.relativePath,
        message:
          error instanceof Error ? error.message : "Upload request failed",
      };
    }
  }

  if (conflicts.length > 0 && options.confirmOverwrite) {
    let approved = false;
    let confirmationFailed = false;
    try {
      const fileList = conflicts
        .map(({ item }) => `• ${item.relativePath || item.file.name}`)
        .join("\n");
      approved = await options.confirmOverwrite({
        title: "同名ファイルを一括確認",
        description: `${conflicts.length}件の同名ファイルが見つかりました。すべて上書きするか、すべてスキップするか選択してください。\n\n${fileList}`,
        confirmLabel: `すべて上書き (${conflicts.length}件)`,
        cancelLabel: "すべてスキップ",
        destructive: true,
      });
    } catch (error) {
      confirmationFailed = true;
      const message =
        error instanceof Error ? error.message : "Upload request failed";
      for (const { index, item } of conflicts) {
        itemFailures[index] = {
          name: item.file.name,
          relativePath: item.relativePath,
          message,
        };
      }
    }

    if (!confirmationFailed) {
      if (!approved) {
        for (const { index } of conflicts) skippedItems.add(index);
      } else {
        for (const { index, item, send } of conflicts) {
          try {
            const res = await send(true);
            if (!res.ok) {
              itemFailures[index] = {
                name: item.file.name,
                relativePath: item.relativePath,
                status: res.status,
                message: await uploadErrorMessage(res),
              };
              continue;
            }
            itemResults[index] = await res.json();
          } catch (error) {
            itemFailures[index] = {
              name: item.file.name,
              relativePath: item.relativePath,
              message:
                error instanceof Error ? error.message : "Upload request failed",
            };
          }
        }
      }
    }
  }

  const results = itemResults.filter((result) => result !== undefined);
  const failures = itemFailures.filter(
    (failure): failure is ExplorerUploadFailure => failure !== undefined,
  );
  const batchResult: ExplorerUploadBatchResult = {
    totalCount: items.length,
    successCount: results.length,
    failureCount: failures.length,
    skippedCount: skippedItems.size,
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
  return path.startsWith("aoitalk-record-table:");
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
  options: Pick<
    ExplorerDownloadResourceOptions,
    "expectedContentType" | "requireAttachment" | "rejectRedirects"
  > = {},
): Promise<string | null> {
  const res = await fetch(url, {
    credentials: "include",
    cache: "no-store",
    ...init,
    ...(options.rejectRedirects ? { redirect: "manual" as const } : {}),
    headers: {
      "x-aoitalk-download-preflight": "1",
      ...(init.headers || {}),
    },
  });
  if (
    options.rejectRedirects &&
    (res.redirected || res.type === "opaqueredirect" || (res.status >= 300 && res.status < 400))
  ) {
    try {
      await res.body?.cancel();
    } catch {
      // The authentication redirect is already sufficient to reject the response.
    }
    throw new Error("ダウンロード先が転送されました。ログイン状態を確認して再試行してください");
  }
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(extractApiErrorMessage(res.status, text));
  }

  const disposition = res.headers.get("Content-Disposition");
  if (options.expectedContentType) {
    const contentType = res.headers
      .get("Content-Type")
      ?.split(";", 1)[0]
      .trim()
      .toLowerCase();
    if (contentType !== options.expectedContentType.toLowerCase()) {
      try {
        await res.body?.cancel();
      } catch {
        // Keep the response validation error as the user-visible result.
      }
      throw new Error("ZIPを取得できませんでした。ログイン状態を確認して再試行してください");
    }
  }
  if (
    options.requireAttachment &&
    !/^\s*attachment(?:\s*;|\s*$)/i.test(disposition || "")
  ) {
    try {
      await res.body?.cancel();
    } catch {
      // Keep the response validation error as the user-visible result.
    }
    throw new Error("ダウンロード応答を確認できませんでした。ログイン状態を確認して再試行してください");
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
  return disposition;
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
  // submit() bypasses the submit event used by the base-path bootstrap.
  form.action = withBasePath(action);
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

function isCompatibilityDownloadUrl(value: string): boolean {
  try {
    let pathname: string;
    if (value.startsWith("/") && !value.startsWith("//")) {
      const hashIndex = value.indexOf("#");
      const withoutHash = hashIndex >= 0 ? value.slice(0, hashIndex) : value;
      const queryIndex = withoutHash.indexOf("?");
      pathname = queryIndex >= 0 ? withoutHash.slice(0, queryIndex) : withoutHash;
    } else {
      const parsed = new URL(value, window.location.origin);
      if (parsed.origin !== window.location.origin) return false;
      pathname = parsed.pathname;
    }
    pathname = withoutBasePath(pathname);
    return (
      pathname === "/api/python-proxy/explorer/download" ||
      /^\/api\/python-proxy\/remote-servers\/[^/]+\/workspace\/download$/u.test(
        pathname,
      )
    );
  } catch {
    return false;
  }
}

function canonicalCompatibilityDownloadUrl(value: string): string {
  if (value.startsWith("/") && !value.startsWith("//")) {
    const hashIndex = value.indexOf("#");
    const withoutHash = hashIndex >= 0 ? value.slice(0, hashIndex) : value;
    const queryIndex = withoutHash.indexOf("?");
    const pathname = queryIndex >= 0 ? withoutHash.slice(0, queryIndex) : withoutHash;
    const search = queryIndex >= 0 ? withoutHash.slice(queryIndex) : "";
    return `${withoutBasePath(pathname)}${search}`;
  }
  const parsed = new URL(value, window.location.origin);
  return `${withoutBasePath(parsed.pathname)}${parsed.search}`;
}

const activeCompatibilityDownloads = new Set<AbortController>();
let compatibilityDownloadPagehideRegistered = false;

function cleanupCompatibilityDownloadsOnPageHide(): void {
  for (const controller of activeCompatibilityDownloads) controller.abort();
  activeCompatibilityDownloads.clear();
  compatibilityDownloadPagehideRegistered = false;
  window.removeEventListener("pagehide", cleanupCompatibilityDownloadsOnPageHide);
}

function retainCompatibilityDownload(controller: AbortController): void {
  activeCompatibilityDownloads.add(controller);
  if (compatibilityDownloadPagehideRegistered) return;
  compatibilityDownloadPagehideRegistered = true;
  window.addEventListener("pagehide", cleanupCompatibilityDownloadsOnPageHide);
}

function releaseCompatibilityDownload(controller: AbortController): void {
  activeCompatibilityDownloads.delete(controller);
  if (compatibilityDownloadPagehideRegistered && activeCompatibilityDownloads.size === 0) {
    compatibilityDownloadPagehideRegistered = false;
    window.removeEventListener("pagehide", cleanupCompatibilityDownloadsOnPageHide);
  }
}

async function compatibilityDownload(
  url: string,
  paths?: string[],
  fallbackFilename?: string,
): Promise<boolean> {
  const controller = new AbortController();
  retainCompatibilityDownload(controller);
  try {
    try {
      const result = await downloadFileTransfer(
        {
          operation: "download",
          url: canonicalCompatibilityDownloadUrl(url),
          ...(paths ? { paths } : {}),
          ...(fallbackFilename ? { fallbackFilename } : {}),
        },
        { signal: controller.signal },
      );
      if (controller.signal.aborted) {
        throw new DOMException("The download was aborted", "AbortError");
      }
      triggerFileTransferDownload(result);
      return true;
    } catch (error) {
      if (error instanceof FileTransferNativeRequiredError) return false;
      throw error;
    }
  } finally {
    releaseCompatibilityDownload(controller);
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
  options: ExplorerDownloadResourceOptions = {},
): Promise<void> {
  const fallback =
    fallbackFilename?.trim() || fallbackDownloadFilename(url, "download");
  const requiresPreflightContract = Boolean(
    options.expectedContentType || options.requireAttachment || options.rejectRedirects || options.signal,
  );
  if (
    !requiresPreflightContract &&
    readFileTransferCompatibilityPreference() &&
    isCompatibilityDownloadUrl(url)
  ) {
    if (await compatibilityDownload(url, undefined, fallback)) return;
  }
  if (options.signal?.aborted) {
    throw new DOMException("The download was aborted", "AbortError");
  }
  const disposition = await preflightDownload(
    url,
    { method: "GET", signal: options.signal },
    options,
  );
  if (options.signal?.aborted) {
    throw new DOMException("The download was aborted", "AbortError");
  }
  const filename = downloadFilenameFromDisposition(disposition, fallback);
  // Virtual resources do not all guarantee Content-Disposition; preserve the
  // caller-provided fallback only when the preflight did not supply one.
  triggerNativeAnchorDownload(url, filename, !disposition?.trim());
}

export interface ExplorerDownloadResourceOptions {
  /** Require the final response's media type after ignoring parameters. */
  expectedContentType?: string;
  /** Require Content-Disposition to begin with the attachment token. */
  requireAttachment?: boolean;
  /** Reject fetch-followed redirects without exposing their destination URL. */
  rejectRedirects?: boolean;
  /** Cancel the preflight before it starts or before native navigation begins. */
  signal?: AbortSignal;
}

export async function explorerDownloadPaths(paths: string[]): Promise<void> {
  if (paths.length === 0) return;
  assertDownloadPaths(paths);
  const compatibilityEnabled = readFileTransferCompatibilityPreference();

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
    if (compatibilityEnabled) {
      if (await compatibilityDownload(url, undefined, fallback)) return;
    }
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
    if (compatibilityEnabled) {
      if (await compatibilityDownload(action, relativePaths)) return;
    }
    await preflightDownload(action, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paths: relativePaths }),
    });
    triggerNativeFormDownload(relativePaths, action);
    return;
  }

  if (compatibilityEnabled) {
    if (await compatibilityDownload("/api/python-proxy/explorer/download", paths)) {
      return;
    }
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

export interface ExplorerMoveOptions {
  overwrite?: boolean;
}

export interface ExplorerMoveResult {
  success: boolean;
  new_path: string;
  /** True when the move replaced a pre-existing destination entry. */
  overwrote_existing?: boolean;
  /** False when the previous destination content cannot be recovered by Undo. */
  undoable?: boolean;
  /** Human-readable limitation for an irreversible overwrite move. */
  undo_limitation?: string | null;
}

export async function explorerMove(
  src: string,
  dest: string,
  options: ExplorerMoveOptions = {},
) {
  const overwrite = options.overwrite === true ? { overwrite: true } : {};
  const srcProject = resolveProjectMutationPath(src, "移動");
  const destProject = resolveProjectMutationPath(dest, "移動", true);
  const sameProject =
    srcProject &&
    destProject &&
    srcProject.projectId.toLowerCase() === destProject.projectId.toLowerCase();
  if (sameProject) {
    const result = await pyFetchJson<ExplorerMoveResult>(
      `/projects/${encodeURIComponent(srcProject.projectId)}/files/move`,
      {
        src: srcProject.relativePath,
        dest: destProject.relativePath,
        ...overwrite,
      },
    );
    return {
      ...result,
      new_path: result.new_path
        ? `_projects/project_${srcProject.projectId}/${result.new_path}`
        : result.new_path,
    };
  }
  return pyFetchJson<ExplorerMoveResult>("/explorer/move", {
    src,
    dest,
    ...overwrite,
  });
}

export async function explorerCopy(src: string, dest: string) {
  const srcProject = resolveProjectMutationPath(src, "コピー");
  const destProject = resolveProjectMutationPath(dest, "コピー", true);
  const sameProject =
    srcProject &&
    destProject &&
    srcProject.projectId.toLowerCase() === destProject.projectId.toLowerCase();
  const result = await pyFetchJson<{
    success: boolean;
    new_path: string;
    new_name: string;
  }>(
    sameProject
      ? `/projects/${encodeURIComponent(srcProject.projectId)}/files/copy`
      : "/explorer/copy",
    sameProject
      ? {
          src: srcProject.relativePath,
          dest: destProject.relativePath,
        }
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
      { overwrite: true },
    );
    return {
      success: true,
      message: "ファイルを保存しました",
      size_bytes: file.size,
    };
  }

  const compatibilityEnabled = readFileTransferCompatibilityPreference();
  const encodedContent = compatibilityEnabled
    ? new TextEncoder().encode(content)
    : undefined;
  if (
    compatibilityEnabled &&
    encodedContent &&
    encodedContent.byteLength <= MAX_COMPAT_UPLOAD_FILE_BYTES
  ) {
    const meta: FileTransferSaveMeta = {
      operation: "save",
      url: "/api/python-proxy/explorer/save",
      path,
      ...(encoding !== undefined ? { encoding } : {}),
    };
    const response = await saveFileTransfer(meta, content);
    if (!response.ok) throw new Error(await uploadErrorMessage(response));
    return (await response.json()) as {
      success: boolean;
      message: string;
      size_bytes?: number;
      modified_at?: string;
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
  /** 本文検索（mode=content）時の一致行数。 */
  match_count?: number;
  /** 本文検索（mode=content）時の一致行（先頭数件）。 */
  matches?: { line: number; text: string }[];
}

export interface ExplorerSearchResponse {
  success: boolean;
  results: SearchResult[];
  total: number;
  total_returned?: number;
  root_path?: string;
  truncated?: boolean;
  query: string;
  /** 本文検索の打ち切り理由（results=件数上限 / scan=走査上限 / bytes=読取量上限）。 */
  limit_reason?: "results" | "scan" | "bytes" | null;
  scanned_files?: number;
  skipped_large_files?: number;
}

export type ExplorerSearchMode = "name" | "content";

export interface ExplorerSearchOptions {
  /** ファイル名・フォルダ名（本文検索時は本文の各行）を正規表現として照合する。 */
  regex?: boolean;
  /** name=名前検索（既定）、content=テキストファイル本文の文字列検索。 */
  mode?: ExplorerSearchMode;
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
  if (options?.mode === "content") params.set("mode", "content");
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
