/**
 * Huggingface / Hydrus ブラウザ用フロントエンドAPIクライアント
 * サーバー側のトークン管理に依存し、Cookie認証(JWTセッション)を流用。
 */

// ─── Types ───
export type RepoType = "model" | "dataset";

export interface HfUploadFailure {
  name: string;
  relativePath?: string;
  error: string;
  status?: number;
}

export interface HfUploadBatchResult {
  success: boolean;
  successCount: number;
  failureCount: number;
  failures: HfUploadFailure[];
}

export class HfUploadError extends Error {
  batchResult: HfUploadBatchResult;
  status?: number;

  constructor(
    batchResult: HfUploadBatchResult,
    message?: string,
    status?: number,
  ) {
    super(message || "HFへのアップロードに失敗しました");
    this.name = "HfUploadError";
    this.status = status;
    this.batchResult = {
      ...batchResult,
      failures: batchResult.failures.map((failure) => ({
        ...failure,
        status: failure.status ?? status,
      })),
    };
  }
}

export interface HfAccount {
  id: string;
  username: string;
  label: string;
  source: "db";
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
  // Integration responses are principal-scoped and may contain private repo
  // metadata.  Opt out of browser/Next shared caching by default.
  const res = await fetch(url, { cache: "no-store", credentials: "include", ...init });
  if (!res.ok) {
    const t = await res.text().catch(() => "");
    let detail = res.statusText || `HTTP ${res.status}`;
    try {
      const body = JSON.parse(t) as { detail?: unknown; error?: unknown };
      const message = body.detail ?? body.error;
      if (typeof message === "string" && message.trim()) detail = message;
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

export async function hfDeleteAccount(accountId: string): Promise<{ success: boolean }> {
  const qs = new URLSearchParams({ accountId });
  return jsonFetch(`/api/huggingface/accounts?${qs}`, { method: "DELETE" });
}

export async function hfAddReference(
  value: string,
): Promise<HfReferenceAddResponse> {
  return jsonFetch("/api/huggingface/references", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ value }),
  });
}

export async function hfUploadFiles(
  path: string,
  files: FileList | File[] | Array<{ file: File; relativePath?: string }>,
): Promise<HfUploadBatchResult> {
  const form = new FormData();
  form.set("path", path);
  const entries = Array.from(
    files as ArrayLike<File | { file: File; relativePath?: string }>,
  );
  for (const entry of entries) {
    const file = entry instanceof File ? entry : entry.file;
    const relativePath =
      entry instanceof File
        ? file.webkitRelativePath || file.name
        : entry.relativePath || file.name;
    form.append("files", file);
    form.append("filePaths", relativePath);
  }
  const response = await fetch("/api/huggingface/upload", {
    cache: "no-store",
    credentials: "include",
    method: "POST",
    body: form,
  });
  const text = await response.text().catch(() => "");
  let body: unknown = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    // fall through to the generic response error below
  }

  if (!response.ok) {
    if (
      body &&
      typeof body === "object" &&
      Array.isArray((body as { failures?: unknown }).failures) &&
      typeof (body as { successCount?: unknown }).successCount === "number" &&
      typeof (body as { failureCount?: unknown }).failureCount === "number"
    ) {
      const result = body as HfUploadBatchResult & { detail?: unknown };
      throw new HfUploadError(
        result,
        typeof result.detail === "string" && result.detail.trim()
          ? result.detail
          : undefined,
        response.status,
      );
    }

    let detail = response.statusText || `HTTP ${response.status}`;
    if (
      body &&
      typeof body === "object" &&
      (typeof (body as { detail?: unknown }).detail === "string" ||
        typeof (body as { error?: unknown }).error === "string")
    ) {
      detail =
        (body as { detail?: string; error?: string }).detail ||
        (body as { detail?: string; error?: string }).error ||
        detail;
    }
    throw new Error(detail || `HTTP ${response.status}`);
  }

  return body as HfUploadBatchResult;
}

export interface HfDeleteItem {
  /** HF 仮想パス（`HF|<accountId>|<repoType>|<repoId>|<subPath>`） */
  path: string;
  /** ディレクトリなら true（サーバー側で配下ファイルを再帰列挙する） */
  isDirectory?: boolean;
}

/**
 * HF リポジトリ上のファイル / ディレクトリを削除する。
 * HF 側にゴミ箱は無く元に戻せないため、呼び出し側で確認ダイアログを挟むこと。
 */
export async function hfDeleteFiles(items: HfDeleteItem[]): Promise<{
  success: boolean;
  deletedCount: number;
}> {
  return jsonFetch("/api/huggingface/delete", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ items }),
  });
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
}): Promise<{
  success: boolean;
  text: string;
  truncated: boolean;
  size: number;
}> {
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
  tags?: Record<
    string,
    Record<string, { storage_tags?: Record<string, string[]> }>
  >;
  service_keys_to_statuses_to_tags?: Record<string, Record<string, string[]>>;
  service_keys_to_statuses_to_display_tags?: Record<
    string,
    Record<string, string[]>
  >;
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
async function hydrusFetch<T>(path: string, init?: RequestInit): Promise<T> {
  return jsonFetch(`/api/python-proxy${path}`, init);
}

export async function hydrusHealth(): Promise<{ ok: boolean; error?: string }> {
  return hydrusFetch("/hydrus/health");
}

export interface HydrusUserSettingsResponse {
  configured: boolean;
  apiUrl: string | null;
  displayName?: string | null;
}

/** Hydrus接続設定（access keyはAPIレスポンスへ含めない）。 */
export async function hydrusGetSettings(): Promise<HydrusUserSettingsResponse> {
  return jsonFetch("/api/hydrus/settings");
}

export async function hydrusSaveSettings(params: {
  apiUrl: string;
  accessKey: string;
  displayName?: string;
}): Promise<{ success: boolean }> {
  return jsonFetch("/api/hydrus/settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(params),
  });
}

export async function hydrusDeleteSettings(): Promise<{ success: boolean }> {
  return jsonFetch("/api/hydrus/settings", { method: "DELETE" });
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

/** Hydrus 削除 / 復元の応答（count は実際に処理された件数）。 */
export interface HydrusFileMutationResponse {
  ok: boolean;
  count: number;
}

/** Hydrus のファイルを削除する（Hydrus 側の trash に入るため undelete で戻せる）。 */
export async function hydrusDeleteFiles(
  fileIds: number[],
  reason?: string,
): Promise<HydrusFileMutationResponse> {
  return hydrusFetch("/hydrus/files/delete", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      file_ids: fileIds,
      ...(reason ? { reason } : {}),
    }),
  });
}

/** Hydrus の trash からファイルを復元する。 */
export async function hydrusUndeleteFiles(
  fileIds: number[],
): Promise<HydrusFileMutationResponse> {
  return hydrusFetch("/hydrus/files/undelete", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ file_ids: fileIds }),
  });
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
