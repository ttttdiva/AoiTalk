import { withBasePath } from "@/lib/base-path";

import {
  decodeTransferBase64,
  decodeTransferJson,
  encodeTransferBase64,
  encodeTransferJson,
  FILE_TRANSFER_ENDPOINT,
  FILE_TRANSFER_PROTOCOL_VERSION,
  MAX_COMPAT_DOWNLOAD_CHUNK_BYTES,
  MAX_COMPAT_DOWNLOAD_FILE_BYTES,
  MAX_COMPAT_METADATA_BYTES,
  MAX_COMPAT_UPLOAD_FILE_BYTES,
  type FileTransferDownloadMetadata,
  type FileTransferMeta,
} from "@/lib/file-transfer-protocol";

export type FileTransferSaveMeta = Extract<
  FileTransferMeta,
  { operation: "save" }
>;

export type FileTransferClientMeta = FileTransferMeta;

export type FileTransferRequestOptions = {
  signal?: AbortSignal;
};

export type FileTransferDownloadResult = {
  blob: Blob;
  filename: string;
  mimeType: string;
};

/** Signals that the validated resource is outside the in-memory compatibility
 * path and must use the existing native streaming download. */
export class FileTransferNativeRequiredError extends Error {
  constructor(message = "download exceeds compatibility size limit") {
    super(message);
    this.name = "FileTransferNativeRequiredError";
  }
}

type ClientDownloadMetadata = FileTransferDownloadMetadata;

const BLOB_DOWNLOAD_REVOKE_DELAY_MS = 60_000;

// A metadata line is the largest valid NDJSON line: 256KiB of decoded JSON
// expands to at most four base64 characters per three bytes.  Keep a small
// bounded allowance for the JSON envelope so a response without a newline
// cannot grow the pending text indefinitely.
const MAX_DOWNLOAD_LINE_CHARS =
  Math.ceil((MAX_COMPAT_METADATA_BYTES * 4) / 3) + 1024;

async function readFileBytes(file: File): Promise<Uint8Array> {
  if (typeof file.arrayBuffer === "function") {
    return new Uint8Array(await file.arrayBuffer());
  }
  return new Promise<Uint8Array>((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error ?? new Error("file read failed"));
    reader.onload = () => {
      if (!(reader.result instanceof ArrayBuffer)) {
        reject(new Error("file read failed"));
        return;
      }
      resolve(new Uint8Array(reader.result));
    };
    reader.readAsArrayBuffer(file);
  });
}

function responseErrorMessage(status: number, text: string): string {
  const normalized = text
    .replace(/[\u0000-\u001f\u007f]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  if (
    !normalized ||
    /<\/?(?:html|body|head)\b|<!doctype\b/i.test(normalized)
  ) {
    return `HTTP ${status}`;
  }
  try {
    const parsed = JSON.parse(text) as { detail?: unknown; error?: unknown };
    const detail = parsed.detail ?? parsed.error;
    if (typeof detail === "string" && detail.trim()) return detail.trim();
    if (detail && typeof detail === "object" && "message" in detail) {
      const message = (detail as { message?: unknown }).message;
      if (typeof message === "string" && message.trim()) return message.trim();
    }
  } catch {
    // The bounded plain-text value below is the fallback for non-JSON errors.
  }
  return normalized.length > 500 ? `${normalized.slice(0, 499)}…` : normalized;
}

/**
 * Post one compatibility envelope.  The endpoint is the only URL that is
 * base-path-adjusted; URLs inside metadata remain the canonical server paths
 * consumed by the route.
 */
export async function postFileTransfer(
  meta: FileTransferClientMeta,
  data?: Uint8Array,
  options: FileTransferRequestOptions = {},
): Promise<Response> {
  const body: {
    version: typeof FILE_TRANSFER_PROTOCOL_VERSION;
    meta: string;
    data?: string;
  } = {
    version: FILE_TRANSFER_PROTOCOL_VERSION,
    meta: encodeTransferJson(meta),
  };
  if (data !== undefined) body.data = encodeTransferBase64(data);

  return fetch(withBasePath(FILE_TRANSFER_ENDPOINT), {
    method: "POST",
    credentials: "include",
    cache: "no-store",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal: options.signal,
  });
}

export async function uploadFileTransfer(
  meta: Extract<FileTransferMeta, { operation: "upload" }>,
  file: File,
  options: FileTransferRequestOptions = {},
): Promise<Response> {
  if (file.size > MAX_COMPAT_UPLOAD_FILE_BYTES) {
    throw new Error("compatibility upload size limit exceeded");
  }
  const bytes = await readFileBytes(file);
  return postFileTransfer(meta, bytes, options);
}

export async function saveFileTransfer(
  meta: FileTransferSaveMeta,
  content: string,
  options: FileTransferRequestOptions = {},
): Promise<Response> {
  const bytes = new TextEncoder().encode(content);
  if (bytes.byteLength > MAX_COMPAT_UPLOAD_FILE_BYTES) {
    throw new Error("compatibility save size limit exceeded");
  }
  return postFileTransfer(meta, bytes, options);
}

function assertDownloadMetadata(value: unknown): ClientDownloadMetadata {
  if (!value || typeof value !== "object") {
    throw new Error("invalid download metadata");
  }
  const metadata = value as Partial<ClientDownloadMetadata>;
  if (
    typeof metadata.filename !== "string" ||
    !metadata.filename.trim() ||
    typeof metadata.mimeType !== "string"
  ) {
    throw new Error("invalid download metadata");
  }
  if (
    metadata.size !== undefined &&
    (typeof metadata.size !== "number" ||
      !Number.isSafeInteger(metadata.size) ||
      metadata.size < 0)
  ) {
    throw new Error("invalid download metadata");
  }
  return {
    filename: metadata.filename,
    mimeType: metadata.mimeType,
    ...(metadata.size !== undefined ? { size: metadata.size } : {}),
  };
}

function assertDownloadEndSize(value: unknown, total: number): void {
  if (
    typeof value !== "number" ||
    !Number.isSafeInteger(value) ||
    value < 0 ||
    value !== total
  ) {
    throw new Error("download size mismatch");
  }
}

function parseDownloadLine(
  line: string,
  state: {
    metadata?: ClientDownloadMetadata;
    parts: BlobPart[];
    total: number;
    ended: boolean;
  },
): void {
  if (!line.trim()) return;
  let value: unknown;
  try {
    value = JSON.parse(line);
  } catch {
    throw new Error("invalid download response");
  }
  if (!value || typeof value !== "object") {
    throw new Error("invalid download response");
  }
  const entry = value as {
    type?: unknown;
    value?: unknown;
    data?: unknown;
    size?: unknown;
    message?: unknown;
  };
  if (state.ended) throw new Error("download response has trailing data");

  if (entry.type === "meta") {
    if (state.metadata) throw new Error("duplicate download metadata");
    if (typeof entry.value !== "string") throw new Error("invalid download metadata");
    state.metadata = assertDownloadMetadata(
      decodeTransferJson<ClientDownloadMetadata>(
        entry.value,
        MAX_COMPAT_METADATA_BYTES,
      ),
    );
    if (
      state.metadata.size !== undefined &&
      state.metadata.size > MAX_COMPAT_DOWNLOAD_FILE_BYTES
    ) {
      throw new FileTransferNativeRequiredError();
    }
    return;
  }

  if (entry.type === "chunk") {
    if (!state.metadata) throw new Error("download metadata is missing");
    if (typeof entry.data !== "string") throw new Error("invalid download chunk");
    const bytes = decodeTransferBase64(entry.data);
    if (bytes.byteLength > MAX_COMPAT_DOWNLOAD_CHUNK_BYTES) {
      throw new Error("download chunk exceeds limit");
    }
    if (state.total + bytes.byteLength > MAX_COMPAT_DOWNLOAD_FILE_BYTES) {
      throw new FileTransferNativeRequiredError();
    }
    const blobBytes = new ArrayBuffer(bytes.byteLength);
    new Uint8Array(blobBytes).set(bytes);
    state.parts.push(blobBytes);
    state.total += bytes.byteLength;
    if (!Number.isSafeInteger(state.total)) throw new Error("download is too large");
    return;
  }

  if (entry.type === "end") {
    if (!state.metadata) throw new Error("download metadata is missing");
    assertDownloadEndSize(entry.size, state.total);
    state.ended = true;
    return;
  }

  if (entry.type === "error") {
    const message = typeof entry.message === "string" ? entry.message.trim() : "";
    throw new Error(message || "file transfer failed");
  }

  throw new Error("invalid download response");
}

async function responseError(response: Response): Promise<Error> {
  const text = await response.text().catch(() => "");
  return new Error(responseErrorMessage(response.status, text));
}

/**
 * Read the strict NDJSON download stream completely before returning a Blob.
 * This prevents a truncated or malformed response from being offered as a
 * successful browser download.
 */
export async function downloadFileTransfer(
  meta: Extract<FileTransferMeta, { operation: "download" }>,
  options: FileTransferRequestOptions = {},
): Promise<FileTransferDownloadResult> {
  const response = await postFileTransfer(meta, undefined, options);
  if (!response.ok) throw await responseError(response);
  if (!response.body) throw new Error("download response has no body");

  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8", { fatal: true });
  const state: {
    metadata?: ClientDownloadMetadata;
    parts: BlobPart[];
    total: number;
    ended: boolean;
  } = { parts: [], total: 0, ended: false };
  let pending = "";

  try {
    while (true) {
      const result = await reader.read();
      if (result.done) break;
      pending += decoder.decode(result.value, { stream: true });
      let newline = pending.indexOf("\n");
      while (newline >= 0) {
        const line = pending.slice(0, newline).replace(/\r$/u, "");
        if (line.length > MAX_DOWNLOAD_LINE_CHARS) {
          throw new Error("download response line exceeds limit");
        }
        pending = pending.slice(newline + 1);
        parseDownloadLine(line, state);
        newline = pending.indexOf("\n");
      }
      if (pending.length > MAX_DOWNLOAD_LINE_CHARS) {
        throw new Error("download response line exceeds limit");
      }
    }
    pending += decoder.decode();
    if (pending.length > MAX_DOWNLOAD_LINE_CHARS) {
      throw new Error("download response line exceeds limit");
    }
    if (pending) parseDownloadLine(pending.replace(/\r$/u, ""), state);
    if (!state.metadata) throw new Error("download metadata is missing");
    if (!state.ended) throw new Error("download stream ended unexpectedly");

    return {
      blob: new Blob(state.parts, { type: state.metadata.mimeType }),
      filename: state.metadata.filename,
      mimeType: state.metadata.mimeType,
    };
  } catch (error) {
    try {
      await reader.cancel(error);
    } catch {
      // The original parse/network error is more useful to the caller.
    }
    throw error;
  } finally {
    reader.releaseLock();
  }
}

type BlobDownloadContext = {
  objectUrl: string;
  timer?: number;
};

const activeBlobDownloads = new Set<BlobDownloadContext>();
let blobDownloadPagehideRegistered = false;

function cleanupBlobDownload(context: BlobDownloadContext): void {
  if (context.timer !== undefined) window.clearTimeout(context.timer);
  activeBlobDownloads.delete(context);
  URL.revokeObjectURL(context.objectUrl);
  if (blobDownloadPagehideRegistered && activeBlobDownloads.size === 0) {
    blobDownloadPagehideRegistered = false;
    window.removeEventListener("pagehide", cleanupBlobDownloadsOnPageHide);
  }
}

function cleanupBlobDownloadsOnPageHide(): void {
  for (const context of Array.from(activeBlobDownloads)) cleanupBlobDownload(context);
}

function retainBlobDownload(context: BlobDownloadContext): void {
  activeBlobDownloads.add(context);
  if (blobDownloadPagehideRegistered) return;
  blobDownloadPagehideRegistered = true;
  window.addEventListener("pagehide", cleanupBlobDownloadsOnPageHide);
}

/** Trigger a browser download for a fully validated compatibility Blob. */
export function triggerFileTransferDownload(
  result: FileTransferDownloadResult,
): void {
  if (typeof URL.createObjectURL !== "function") {
    throw new Error("browser download is unavailable");
  }
  const objectUrl = URL.createObjectURL(result.blob);
  const anchor = document.createElement("a");
  anchor.href = objectUrl;
  anchor.download = result.filename;
  anchor.hidden = true;
  anchor.setAttribute("aria-hidden", "true");
  document.body.append(anchor);
  const context: BlobDownloadContext = { objectUrl };
  retainBlobDownload(context);
  try {
    anchor.click();
  } finally {
    anchor.remove();
    context.timer = window.setTimeout(
      () => cleanupBlobDownload(context),
      BLOB_DOWNLOAD_REVOKE_DELAY_MS,
    );
  }
}
