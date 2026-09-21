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

import * as FileSystem from "expo-file-system/legacy";
import { fetchApiAtServerFingerprint, getBaseUrl, getConfiguredApiServerFingerprint } from "./api-client";
import { getToken, getTokenAuthScope } from "./auth";
import { downloadFileToDevice } from "./files-download";

/** Decode only one bounded transfer chunk; no whole-file base64 materialization. */
export function decodeStorageChunk(base64: string): Uint8Array {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  const padding = base64.endsWith("==") ? 2 : base64.endsWith("=") ? 1 : 0;
  const payload = padding ? base64.slice(0,-padding) : base64;
  if (base64.length % 4 !== 0 || /[^A-Za-z0-9+/]/.test(payload)) throw new Error("ファイルchunkが無効です");
  const bytes = new Uint8Array(base64.length / 4 * 3 - (base64.endsWith("==") ? 2 : base64.endsWith("=") ? 1 : 0));
  let output = 0;
  for (let i = 0; i < base64.length; i += 4) {
    const value = alphabet.indexOf(base64[i]) << 18 | alphabet.indexOf(base64[i+1]) << 12 |
      (base64[i+2] === "=" ? 0 : alphabet.indexOf(base64[i+2])) << 6 | (base64[i+3] === "=" ? 0 : alphabet.indexOf(base64[i+3]));
    if (output < bytes.length) bytes[output++] = value >> 16 & 255;
    if (output < bytes.length) bytes[output++] = value >> 8 & 255;
    if (output < bytes.length) bytes[output++] = value & 255;
  }
  return bytes;
}

export async function createStorageRootsClient() {
  const fingerprint = await getConfiguredApiServerFingerprint();
  const authScope = getTokenAuthScope(await getToken());
  async function assertIdentity() {
    if (await getConfiguredApiServerFingerprint() !== fingerprint || getTokenAuthScope(await getToken()) !== authScope) {
      throw new Error("接続サーバーまたはアカウントが変更されました。ストレージを再選択してください");
    }
  }
  async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
    await assertIdentity();
    try {const result = await fetchApiAtServerFingerprint<T>(fingerprint, `/api${path}`, options, 25000); await assertIdentity(); return result;}
    catch(error) {
      const body = error && typeof error === "object" && "responseBody" in error ? (error as {responseBody: unknown}).responseBody : undefined;
      if (typeof body === "string") {
        let detail: {message?: string; code?: string; outcome_unknown?: boolean} | undefined;
        try {detail = JSON.parse(body)?.detail;} catch { /* preserve the original error */ }
        if (detail?.message) throw new StorageApiError(detail.message, detail.code || "storage_request_failed", 0, detail.outcome_unknown);
      }
      throw error;
    }
  }
  const json = <T>(path: string, method: string, body: unknown) => request<T>(path, {method, headers: {"Content-Type":"application/json"}, body: JSON.stringify(body)});
  const client = {
    fingerprint,
    catalog: () => request<StorageCatalog>("/storage/roots"),
    status: (id: string) => request<StorageRootInfo>(storageEndpoint(id,"status")),
    saveRoot: (revision: string, root: RootInput) => json("/storage/roots","PUT",{revision,root}),
    enroll: (id: string) => request(storageEndpoint(id,"enroll"),{method:"POST"}),
    list: (id: string, path = "") => request<StorageListing>(storageEndpoint(id,`files?path=${encodeURIComponent(path)}`)),
    text: (id: string, path: string) => request<StorageText>(storageEndpoint(id,`text?path=${encodeURIComponent(path)}`)),
    saveText: (id: string, path: string, content: string, etag: string | null) => json<StorageText>(storageEndpoint(id,"text"),"PUT",{path,content,etag}),
    mkdir: (id: string, path: string) => json(storageEndpoint(id,"directories"),"POST",{path}),
    move: (id: string, source: string, destination: string) => json(storageEndpoint(id,"move"),"POST",{source,destination}),
    trash: (id: string, path: string) => json<{trash_id:string}>(storageEndpoint(id,"trash"),"POST",{path}),
    restore: (id: string, tid: string) => request(storageEndpoint(id,`trash/${encodeURIComponent(tid)}/restore`),{method:"POST"}),
    startUpload: (id: string, path: string, size: number) => json<UploadSession>(storageEndpoint(id,"uploads"),"POST",{path,size}),
    uploadStatus: (id: string, uid: string) => request<UploadSession>(storageEndpoint(id,`uploads/${encodeURIComponent(uid)}`)),
    chunk: async (id: string, uid: string, offset: number, uri: string, length: number) => {
      if (length < 1 || length > 4*1024*1024) throw new Error("chunkサイズが無効です");
      const encoded = await FileSystem.readAsStringAsync(uri,{encoding:FileSystem.EncodingType.Base64,position:offset,length});
      const bytes = decodeStorageChunk(encoded);
      return request<UploadSession>(storageEndpoint(id,`uploads/${encodeURIComponent(uid)}?offset=${offset}`),{method:"PUT",headers:{"Content-Type":"application/octet-stream"},body:bytes.buffer as ArrayBuffer});
    },
    finishUpload: (id: string, uid: string) => request(storageEndpoint(id,`uploads/${encodeURIComponent(uid)}/complete`),{method:"POST"}),
    cancelUpload: (id: string, uid: string) => request(storageEndpoint(id,`uploads/${encodeURIComponent(uid)}`),{method:"DELETE"}),
    download: async (id: string, entry: StorageEntry) => {
      let cached: string | null = null;
      try {
        return await downloadFileToDevice({name:entry.name,path:entry.path,type:"file",source:"server",size:entry.size_bytes ?? undefined,mimeType:entry.mime_type}, async () => {
          await client.status(id); // Resolve/refresh authentication through the existing client.
          const base = await getBaseUrl();
          const token = await getToken();
          await assertIdentity();
          if (!token || !FileSystem.cacheDirectory) throw new Error("ファイル取得の準備ができません");
          const safeName = entry.name.replace(/[^a-zA-Z0-9._-]/g,"_").slice(-100) || "download";
          cached = `${FileSystem.cacheDirectory}storage-${Date.now()}-${Math.random().toString(16).slice(2)}-${safeName}`;
          const response = await FileSystem.downloadAsync(`${base}/api${storageEndpoint(id,`download?path=${encodeURIComponent(entry.path)}`)}`,cached,{headers:{Authorization:`Bearer ${token}`},sessionType:FileSystem.FileSystemSessionType.FOREGROUND});
          await assertIdentity();
          if(response.status!==200) throw new Error(`ファイル取得に失敗しました (HTTP ${response.status})`);
          return response.uri;
        });
      } finally {
        if(cached) await FileSystem.deleteAsync(cached,{idempotent:true}).catch(()=>undefined);
      }
    },
  };
  return client;
}
export type StorageRootsClient = Awaited<ReturnType<typeof createStorageRootsClient>>;
export const storageStatusLabel = (root: StorageRootInfo) => !root.enabled ? "無効" : root.status === "online" ? "接続中" : root.status === "checking" ? "確認中" : "オフライン";
