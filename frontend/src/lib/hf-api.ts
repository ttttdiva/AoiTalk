/**
 * Huggingface / Hydrus ブラウザ用フロントエンドAPIクライアント
 * サーバー側のトークン管理に依存し、Cookie認証(JWTセッション)を流用。
 */

// ─── Types ───
export type RepoType = "model" | "dataset";

export interface HfAccount {
  id: string;
  username: string;
  label: string;
  source: "env";
}

export interface HfRepoInfo {
  id: string;
  name: string;
  owner: string;
  private: boolean;
  lastModified: string;
  type: RepoType;
  description?: string;
}

export interface HfFileEntry {
  path: string;
  name: string;
  size?: number;
  type: "file" | "directory";
  lastModified?: string;
  oid?: string;
  xetHash?: string;
  lfs?: { oid: string; size: number; pointerSize: number };
}

export type HfReferenceAddResponse =
  | {
      kind: "account";
      account: { id: string; username: string; label: string };
    }
  | {
      kind: "repository";
      repositories: Array<{
        repoId: string;
        repoType: RepoType;
        accountId?: string;
        path: string;
      }>;
    };

// ─── Helpers ───
async function jsonFetch<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, { credentials: "include", ...init });
  if (!res.ok) {
    const t = await res.text().catch(() => "");
    let detail = t || res.statusText;
    try {
      const body = JSON.parse(t) as { detail?: unknown };
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      // use raw response
    }
    throw new Error(detail || `HTTP ${res.status}`);
  }
  return (await res.json()) as T;
}

// ─── Huggingface ───
export async function hfListAccounts(): Promise<{ accounts: HfAccount[] }> {
  return jsonFetch("/api/huggingface/accounts");
}

export async function hfAddReference(value: string): Promise<HfReferenceAddResponse> {
  return jsonFetch("/api/huggingface/references", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ value }),
  });
}

export async function hfUploadFiles(
  path: string,
  files: FileList | File[] | Array<{ file: File; relativePath?: string }>,
): Promise<{
  success: boolean;
  successCount: number;
  failureCount: number;
  failures: Array<{ name: string; error: string }>;
}> {
  const form = new FormData();
  form.set("path", path);
  const entries = Array.from(
    files as ArrayLike<File | { file: File; relativePath?: string }>,
  );
  for (const entry of entries) {
    const file = entry instanceof File ? entry : entry.file;
    const relativePath = entry instanceof File
      ? file.webkitRelativePath || file.name
      : entry.relativePath || file.name;
    form.append("files", file);
    form.append("filePaths", relativePath);
  }
  return jsonFetch("/api/huggingface/upload", { method: "POST", body: form });
}

export async function hfListRepos(
  accountId: string,
): Promise<{ accountId: string; username: string; repos: HfRepoInfo[] }> {
  const qs = new URLSearchParams({ accountId });
  return jsonFetch(`/api/huggingface/repos?${qs}`);
}

export async function hfListTree(params: {
  accountId?: string;
  repoId: string;
  repoType: RepoType;
  path?: string;
}): Promise<{
  repoId: string;
  repoType: RepoType;
  path: string;
  entries: HfFileEntry[];
}> {
  const qs = new URLSearchParams();
  if (params.accountId) qs.set("accountId", params.accountId);
  qs.set("repoId", params.repoId);
  qs.set("repoType", params.repoType);
  if (params.path) qs.set("path", params.path);
  return jsonFetch(`/api/huggingface/tree?${qs}`);
}

export function hfFileUrl(params: {
  accountId?: string;
  repoId: string;
  repoType: RepoType;
  path: string;
  revision?: string;
}): string {
  const qs = new URLSearchParams();
  if (params.accountId) qs.set("accountId", params.accountId);
  qs.set("repoId", params.repoId);
  qs.set("repoType", params.repoType);
  qs.set("path", params.path);
  if (params.revision) qs.set("revision", params.revision);
  return `/api/huggingface/file?${qs}`;
}

export async function hfFetchText(params: {
  accountId?: string;
  repoId: string;
  repoType: RepoType;
  path: string;
}): Promise<{ success: boolean; text: string; truncated: boolean; size: number }> {
  const qs = new URLSearchParams();
  if (params.accountId) qs.set("accountId", params.accountId);
  qs.set("repoId", params.repoId);
  qs.set("repoType", params.repoType);
  qs.set("path", params.path);
  qs.set("mode", "text");
  return jsonFetch(`/api/huggingface/file?${qs}`);
}

// ─── Hydrus ───
export interface HydrusService {
  name: string;
  service_key: string;
  type?: number;
  type_pretty?: string;
  star_shape?: string;
  min_stars?: number;
  max_stars?: number;
}

export interface HydrusServicesResponse {
  services?: Record<string, HydrusService>;
  // v-style: services_keyed_to_services にマップの場合もある
  [key: string]: unknown;
}

export interface HydrusSearchResponse {
  file_ids: number[];
  total: number;
  page: number;
  per_page: number;
  total_pages: number;
}

export interface HydrusFileMetadata {
  file_id: number;
  hash?: string;
  size?: number;
  mime?: string;
  width?: number;
  height?: number;
  duration?: number;
  has_audio?: boolean;
  num_frames?: number;
  is_inbox?: boolean;
  is_local?: boolean;
  is_trashed?: boolean;
  time_modified?: number;
  tags?: Record<string, Record<string, { storage_tags?: Record<string, string[]> }>>;
  service_keys_to_statuses_to_tags?: Record<string, Record<string, string[]>>;
  service_keys_to_statuses_to_display_tags?: Record<string, Record<string, string[]>>;
  ratings?: Record<string, number | null>;
  [key: string]: unknown;
}

export interface HydrusTagSearchItem {
  value: string;
  count?: number;
}

export interface HydrusTagSearchResponse {
  tags: HydrusTagSearchItem[];
}

/** Python proxy 経由で Hydrus バックエンドを叩く */
async function hydrusFetch<T>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  return jsonFetch(`/api/python-proxy${path}`, init);
}

export async function hydrusHealth(): Promise<{ ok: boolean; error?: string }> {
  return hydrusFetch("/hydrus/health");
}

export async function hydrusGetServices(): Promise<HydrusServicesResponse> {
  return hydrusFetch("/hydrus/services");
}

export async function hydrusSearch(params: {
  tags: string[];
  page?: number;
  perPage?: number;
  fileSortType?: number;
  fileSortAsc?: boolean;
  signal?: AbortSignal;
}): Promise<HydrusSearchResponse> {
  const qs = new URLSearchParams();
  qs.set("tags", JSON.stringify(params.tags));
  if (params.page) qs.set("page", String(params.page));
  if (params.perPage) qs.set("per_page", String(params.perPage));
  if (params.fileSortType != null)
    qs.set("file_sort_type", String(params.fileSortType));
  if (params.fileSortAsc != null)
    qs.set("file_sort_asc", String(params.fileSortAsc));
  return hydrusFetch(`/hydrus/search?${qs}`, { signal: params.signal });
}

export async function hydrusGetMetadata(
  fileIds: number[],
  onlyBasic: boolean = false,
  signal?: AbortSignal,
): Promise<{ metadata: HydrusFileMetadata[] }> {
  const qs = new URLSearchParams();
  qs.set("file_ids", JSON.stringify(fileIds));
  qs.set("only_basic", onlyBasic ? "true" : "false");
  return hydrusFetch(`/hydrus/metadata?${qs}`, { signal });
}

export async function hydrusSearchTags(
  search: string,
  tagServiceKey?: string,
): Promise<HydrusTagSearchResponse> {
  const qs = new URLSearchParams({ search });
  if (tagServiceKey) qs.set("tag_service_key", tagServiceKey);
  return hydrusFetch(`/hydrus/tags/search?${qs}`);
}

export function hydrusThumbnailUrl(fileId: number): string {
  return `/api/python-proxy/hydrus/thumbnail/${fileId}`;
}

export function hydrusFileUrl(fileId: number): string {
  return `/api/python-proxy/hydrus/file/${fileId}`;
}

export async function hydrusSetRating(params: {
  fileId: number;
  ratingServiceKey: string;
  rating: number | null;
}): Promise<{ ok: boolean }> {
  return hydrusFetch("/hydrus/ratings/set", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      file_id: params.fileId,
      rating_service_key: params.ratingServiceKey,
      rating: params.rating,
    }),
  });
}
