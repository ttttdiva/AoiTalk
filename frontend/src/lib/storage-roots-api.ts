/** Additional mounted Files roots; no host path is used as a client file id. */
export interface StorageRootInfo {
  id: string; name: string; configuration_revision: string; root_path?: string; read_only: boolean; enabled: boolean;
  external: boolean; shared?: boolean; project_ids: string[]; user_ids?: string[];
  online: boolean | null; status: "checking" | "online" | "offline" | "disabled";
  can_write: boolean; identity?: string; marker_name?: string;
  error?: {code: string; message: string};
}
export interface StorageCatalog {
  is_admin: boolean; revision: string | null; roots: StorageRootInfo[];
  configuration_error?: {code: string; message: string};
}
export interface StorageEntry {
  name: string; path: string; is_directory: boolean; size_bytes: number | null;
  etag: string; mime_type: string; modified_at: string;
}
export interface StorageListing {
  entries: StorageEntry[]; path: string; parent_path: string | null;
  skipped_entries: number; truncated: boolean;
}
export interface StorageText {path: string; content: string; etag: string}
export interface UploadSession {upload_id: string; received: number; chunk_bytes: number; size?: number}
export type RootInput = Pick<StorageRootInfo, "id" | "name" | "read_only" | "enabled" | "external" | "project_ids"> & {
  root_path: string; shared: boolean; user_ids: string[];
};
export class StorageApiError extends Error {
  constructor(message: string, public code: string, public status: number, public outcomeUnknown = false) {super(message);}
}
export function storageEndpoint(id: string, resource: string) {
  return `/storage/roots/${encodeURIComponent(id)}/${resource}`;
}
async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 25000);
  try {
    const response = await fetch(`/api/python-proxy${path}`, {
      credentials: "include", cache: "no-store", ...options, signal: controller.signal,
    });
    const value = await response.json().catch(() => null);
    if (!response.ok) {
      const detail = value?.detail;
      throw new StorageApiError(typeof detail === "string" ? detail : detail?.message || `HTTP ${response.status}`,
        detail?.code || "storage_request_failed", response.status, detail?.outcome_unknown === true);
    }
    return value as T;
  } catch (error) {
    if (controller.signal.aborted) {
      throw new StorageApiError("ストレージへの通信がタイムアウトしました。更新操作の結果は再読み込みで確認してください", "storage_timeout", 0, options.method !== undefined && options.method !== "GET");
    }
    throw error;
  } finally {clearTimeout(timer);}
}
function json<T>(path: string, method: string, body: unknown) {
  return request<T>(path, {method, headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
}
export const storageRootsApi = {
  catalog: () => request<StorageCatalog>("/storage/roots"),
  status: (id: string) => request<StorageRootInfo>(storageEndpoint(id, "status")),
  saveRoot: (revision: string, root: RootInput) => json<{revision: string}>("/storage/roots", "PUT", {revision, root}),
  enroll: (id: string) => request(storageEndpoint(id, "enroll"), {method: "POST"}),
  list: (id: string, path = "") => request<StorageListing>(storageEndpoint(id, `files?path=${encodeURIComponent(path)}`)),
  text: (id: string, path: string) => request<StorageText>(storageEndpoint(id, `text?path=${encodeURIComponent(path)}`)),
  saveText: (id: string, path: string, content: string, etag: string | null) => json<StorageText>(storageEndpoint(id, "text"), "PUT", {path, content, etag}),
  mkdir: (id: string, path: string) => json(storageEndpoint(id, "directories"), "POST", {path}),
  move: (id: string, source: string, destination: string) => json(storageEndpoint(id, "move"), "POST", {source, destination}),
  trash: (id: string, path: string) => json<{trash_id: string}>(storageEndpoint(id, "trash"), "POST", {path}),
  restore: (id: string, trashId: string) => request(storageEndpoint(id, `trash/${encodeURIComponent(trashId)}/restore`), {method: "POST"}),
  startUpload: (id: string, path: string, size: number) => json<UploadSession>(storageEndpoint(id, "uploads"), "POST", {path, size}),
  uploadStatus: (id: string, uid: string) => request<UploadSession>(storageEndpoint(id, `uploads/${encodeURIComponent(uid)}`)),
  chunk: (id: string, uid: string, offset: number, data: Blob) => request<UploadSession>(storageEndpoint(id, `uploads/${encodeURIComponent(uid)}?offset=${offset}`), {method: "PUT", headers: {"Content-Type": "application/octet-stream"}, body: data}),
  finishUpload: (id: string, uid: string) => request(storageEndpoint(id, `uploads/${encodeURIComponent(uid)}/complete`), {method: "POST"}),
  cancelUpload: (id: string, uid: string) => request(storageEndpoint(id, `uploads/${encodeURIComponent(uid)}`), {method: "DELETE"}),
  downloadUrl: (id: string, path: string) => `/api/python-proxy${storageEndpoint(id, `download?path=${encodeURIComponent(path)}`)}`,
};
export const storageStatusLabel = (root: StorageRootInfo) => !root.enabled ? "無効" : root.status === "online" ? "接続中" : root.status === "checking" ? "確認中" : "オフライン";
