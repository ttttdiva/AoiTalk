/**
 * File Explorer API Client
 * All endpoints proxy through /api/python-proxy/ → Python FastAPI
 */

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
  size_display?: string;
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

export interface GitStatusResponse {
  success: boolean;
  clean: boolean;
  changes: { path: string; status: string }[];
  total_changes: number;
}

export interface GitCommitResponse {
  success: boolean;
  commit_hash?: string;
  message: string;
}

export interface GitLogEntry {
  hash: string;
  short_hash: string;
  message: string;
  author: string;
  date: string;
}

export interface GitLogResponse {
  commits: GitLogEntry[];
  total: number;
}

// ─── API Helpers ───

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
    throw new Error(`API Error ${res.status}: ${text}`);
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

export async function explorerList(
  path: string = "",
): Promise<ExplorerListResponse> {
  const params = path ? `?path=${encodeURIComponent(path)}` : "";
  return pyFetch(`/explorer/list${params}`);
}

export async function explorerTree(
  root: string = "",
): Promise<ExplorerTreeResponse> {
  const params = root ? `?root=${encodeURIComponent(root)}` : "";
  return pyFetch(`/explorer/tree${params}`);
}

export async function explorerMkdir(path: string, name: string) {
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
    if (typeof message === "string" && message.trim()) return message;
  } catch {
    // use raw text below
  }
  return text;
}

export async function explorerUpload(
  path: string,
  files: FileList | ExplorerUploadSource[],
) {
  const items = Array.from(files).map(normalizeUploadFile);
  const results = [];
  const failures: ExplorerUploadFailure[] = [];

  for (const item of items) {
    try {
      const formData = new FormData();
      formData.append("file", item.file, item.file.name);
      if (item.relativePath) {
        formData.append("relative_path", item.relativePath);
      }

      // pathはFastAPI側でクエリパラメータとして受け取るためURLに付与
      const url = `/api/python-proxy/explorer/upload?path=${encodeURIComponent(path)}`;
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
  return `/api/python-proxy/explorer/download?path=${encodeURIComponent(path)}`;
}

export async function explorerRename(path: string, newName: string) {
  return pyFetchJson<{ success: boolean; new_name: string; new_path: string }>(
    "/explorer/rename",
    { path, new_name: newName },
  );
}

export async function explorerMove(src: string, dest: string) {
  return pyFetchJson<{ success: boolean; new_path: string }>("/explorer/move", {
    src,
    dest,
  });
}

export async function explorerCopy(src: string, dest: string) {
  return pyFetchJson<{ success: boolean; new_path: string; new_name: string }>(
    "/explorer/copy",
    { src, dest },
  );
}

export async function explorerDelete(path: string) {
  return pyFetch<{ success: boolean; message: string }>(
    `/explorer/delete?path=${encodeURIComponent(path)}`,
    {
      method: "DELETE",
    },
  );
}

export async function explorerInfo(path: string): Promise<FileInfo> {
  return pyFetch(`/explorer/info?path=${encodeURIComponent(path)}`);
}

export async function explorerPreview(path: string): Promise<FilePreview> {
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
  return pyFetch(`/explorer/content?path=${encodeURIComponent(path)}`);
}

export interface SearchResult {
  name: string;
  path: string;
  type: string;
  extension: string;
  size_bytes?: number;
  size_display?: string;
  icon?: string;
}

export interface ExplorerSearchResponse {
  success: boolean;
  results: SearchResult[];
  total: number;
  query: string;
}

export async function explorerSearch(
  query: string,
  root?: string,
  limit?: number,
): Promise<ExplorerSearchResponse> {
  const params = new URLSearchParams({ q: query });
  if (root) params.set("root", root);
  if (limit) params.set("limit", String(limit));
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

export async function explorerBookmarks(): Promise<{
  success: boolean;
  bookmarks: ExplorerBookmark[];
}> {
  return pyFetch("/explorer/bookmarks");
}

export async function explorerAddBookmark(
  name: string,
  path: string,
  icon?: string,
) {
  return pyFetchJson<{ success: boolean }>("/explorer/bookmarks", {
    name,
    path,
    icon,
  });
}

export async function explorerRemoveBookmark(path: string) {
  return pyFetch<{ success: boolean }>("/explorer/bookmarks", {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path }),
  });
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

// ─── Git ───

export async function gitStatus(
  storageContext: string,
  contextId?: string,
): Promise<GitStatusResponse> {
  return pyFetchJson("/git/status", {
    storage_context: storageContext,
    context_id: contextId,
  });
}

export async function gitCommit(
  message: string,
  storageContext: string,
  contextId?: string,
): Promise<GitCommitResponse> {
  return pyFetchJson("/git/commit", {
    message,
    storage_context: storageContext,
    context_id: contextId,
  });
}

export async function gitLog(
  storageContext: string,
  contextId?: string,
  limit: number = 20,
): Promise<GitLogResponse> {
  return pyFetchJson("/git/log", {
    storage_context: storageContext,
    context_id: contextId,
    limit,
  });
}
