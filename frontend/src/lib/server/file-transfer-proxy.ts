import { NextRequest, NextResponse } from "next/server";
import { proxyExplorerDownload } from "./explorer-download-proxy";
import { isClientRequestAbort } from "./proxy-cancellation";
import {
  NO_STORE_HEADERS,
  proxyRequestToPythonApi,
  type InternalPythonUser,
} from "./python-api-proxy";
import {
  decodeTransferBase64,
  decodeTransferJson,
  encodeTransferBase64,
  encodeTransferJson,
  FILE_TRANSFER_PROTOCOL_VERSION,
  MAX_COMPAT_DOWNLOAD_CHUNK_BYTES,
  MAX_COMPAT_METADATA_BYTES,
  MAX_COMPAT_REQUEST_BODY_BYTES,
  MAX_COMPAT_UPLOAD_FILE_BYTES,
  type FileTransferDownloadMetadata,
  type FileTransferMeta,
  type FileTransferRequest,
} from "@/lib/file-transfer-protocol";

const MAX_TRANSFER_ERROR_RESPONSE_BYTES = 1024 * 1024;
const MAX_TRANSFER_URL_BYTES = 8192;
const MAX_TRANSFER_STRING_BYTES = 4096;
// These guards cover only the request-envelope buffering/decoding phase. A
// successful download releases its slot when the NDJSON Response is returned;
// the response stream itself is bounded per chunk and is not admission-limited.
const MAX_BUFFERED_TRANSFER_REQUESTS_GLOBAL = 4;
const MAX_BUFFERED_TRANSFER_REQUESTS_PER_USER = 2;
const TRANSFER_BODY_IDLE_TIMEOUT_MS = 30_000;
const TRANSFER_BODY_ABSOLUTE_TIMEOUT_MS = 180_000;
const MAX_TRANSFER_METADATA_BASE64_LENGTH =
  Math.ceil(MAX_COMPAT_METADATA_BYTES / 3) * 4;
const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const SAFE_PROFILE_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
let activeBufferedTransferRequests = 0;
const activeBufferedTransferRequestsByUser = new Map<string, number>();

class TransferRequestError extends Error {
  readonly status: number;
  readonly detail: string;

  constructor(status: number, detail: string) {
    super(detail);
    this.name = "TransferRequestError";
    this.status = status;
    this.detail = detail;
  }
}

class TransferRequestTooLargeError extends TransferRequestError {
  constructor(detail = "File transfer request exceeds proxy limit") {
    super(413, detail);
    this.name = "TransferRequestTooLargeError";
  }
}

class TransferRequestTimeoutError extends TransferRequestError {
  constructor() {
    super(408, "File transfer request timed out");
    this.name = "TransferRequestTimeoutError";
  }
}

type TransferTarget = {
  canonicalUrl: URL;
  kind: "upload" | "save" | "explorer-download" | "remote-download";
  pathParts: string[];
  method: "GET" | "POST" | "PUT";
};

type RecordValue = Record<string, unknown>;

function isRecord(value: unknown): value is RecordValue {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function hasOnlyKeys(value: RecordValue, keys: readonly string[]): boolean {
  const allowed = new Set(keys);
  return Object.keys(value).every((key) => allowed.has(key));
}

function boundedString(value: unknown, name: string, max = MAX_TRANSFER_STRING_BYTES): string {
  if (typeof value !== "string" || value.length === 0 || value.length > max) {
    throw new TransferRequestError(400, `Invalid ${name}`);
  }
  if (value.includes("\u0000")) throw new TransferRequestError(400, `Invalid ${name}`);
  return value;
}

function boundedStringValue(
  value: unknown,
  name: string,
  max = MAX_TRANSFER_STRING_BYTES,
): string {
  if (typeof value !== "string" || value.length > max) {
    throw new TransferRequestError(400, `Invalid ${name}`);
  }
  if (value.includes("\u0000")) throw new TransferRequestError(400, `Invalid ${name}`);
  return value;
}

function parseTransferMeta(value: unknown): FileTransferMeta {
  if (!isRecord(value)) throw new TransferRequestError(400, "Invalid transfer metadata");
  const operation = value.operation;
  const url = boundedString(value.url, "transfer URL", MAX_TRANSFER_URL_BYTES);
  if (operation === "upload") {
    if (!hasOnlyKeys(value, ["operation", "url", "fileName", "fileType", "relativePath"])) {
      throw new TransferRequestError(400, "Invalid upload metadata");
    }
    return {
      operation,
      url,
      fileName: boundedString(value.fileName, "fileName"),
      fileType: boundedStringValue(value.fileType, "fileType", 512),
      relativePath:
        value.relativePath === undefined
          ? undefined
          : boundedStringValue(value.relativePath, "relativePath"),
    };
  }
  if (operation === "save") {
    if (!hasOnlyKeys(value, ["operation", "url", "path", "encoding"])) {
      throw new TransferRequestError(400, "Invalid save metadata");
    }
    return {
      operation,
      url,
      path: boundedString(value.path, "path"),
      encoding:
        value.encoding === undefined
          ? undefined
          : boundedStringValue(value.encoding, "encoding", 128),
    };
  }
  if (operation === "download") {
    if (!hasOnlyKeys(value, ["operation", "url", "paths", "fallbackFilename"])) {
      throw new TransferRequestError(400, "Invalid download metadata");
    }
    let paths: string[] | undefined;
    if (value.paths !== undefined) {
      if (!Array.isArray(value.paths) || value.paths.some((path) => typeof path !== "string")) {
        throw new TransferRequestError(400, "Invalid download paths");
      }
      paths = value.paths.map((path) => boundedString(path, "download path"));
    }
    return {
      operation,
      url,
      paths,
      fallbackFilename:
        value.fallbackFilename === undefined
          ? undefined
          : boundedStringValue(value.fallbackFilename, "fallbackFilename", 512),
    };
  }
  throw new TransferRequestError(400, "Invalid transfer operation");
}

function transferAdmissionUserKey(user: InternalPythonUser): string {
  const id = typeof user.id === "string" ? user.id.trim() : "";
  return id || "anonymous";
}

function acquireBufferedTransferSlot(user: InternalPythonUser): boolean {
  if (activeBufferedTransferRequests >= MAX_BUFFERED_TRANSFER_REQUESTS_GLOBAL) {
    return false;
  }
  const key = transferAdmissionUserKey(user);
  const activeForUser = activeBufferedTransferRequestsByUser.get(key) ?? 0;
  if (activeForUser >= MAX_BUFFERED_TRANSFER_REQUESTS_PER_USER) return false;
  activeBufferedTransferRequests += 1;
  activeBufferedTransferRequestsByUser.set(key, activeForUser + 1);
  return true;
}

function releaseBufferedTransferSlot(user: InternalPythonUser): void {
  activeBufferedTransferRequests = Math.max(0, activeBufferedTransferRequests - 1);
  const key = transferAdmissionUserKey(user);
  const activeForUser = activeBufferedTransferRequestsByUser.get(key) ?? 0;
  if (activeForUser <= 1) activeBufferedTransferRequestsByUser.delete(key);
  else activeBufferedTransferRequestsByUser.set(key, activeForUser - 1);
}

function decodedBase64Length(value: string): number {
  if (value.length % 4 !== 0) throw new TransferRequestError(400, "Invalid base64 payload");
  const padding = value.endsWith("==") ? 2 : value.endsWith("=") ? 1 : 0;
  return (value.length / 4) * 3 - padding;
}

function decodeBoundedData(value: unknown, name: string): Uint8Array {
  if (typeof value !== "string") throw new TransferRequestError(400, `Missing ${name}`);
  if (decodedBase64Length(value) > MAX_COMPAT_UPLOAD_FILE_BYTES) {
    throw new TransferRequestTooLargeError("File transfer payload exceeds 64 MiB");
  }
  try {
    const decoded = decodeTransferBase64(value);
    if (decoded.byteLength > MAX_COMPAT_UPLOAD_FILE_BYTES) {
      throw new TransferRequestTooLargeError("File transfer payload exceeds 64 MiB");
    }
    return decoded;
  } catch (error) {
    if (error instanceof TransferRequestTooLargeError) throw error;
    throw new TransferRequestError(400, `Invalid ${name}`);
  }
}

async function readBoundedRequestBody(
  request: NextRequest,
  maxBytes: number,
): Promise<Uint8Array> {
  const declared = request.headers.get("content-length");
  if (declared !== null) {
    if (!/^\d+$/.test(declared)) {
      throw new TransferRequestError(400, "Invalid content length");
    }
    const declaredBytes = Number(declared);
    if (!Number.isSafeInteger(declaredBytes) || declaredBytes > maxBytes) {
      throw new TransferRequestTooLargeError();
    }
  }
  if (!request.body) return new Uint8Array();

  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  const startedAt = Date.now();
  const readWithDeadline = async () => {
    const remainingAbsolute =
      TRANSFER_BODY_ABSOLUTE_TIMEOUT_MS - (Date.now() - startedAt);
    if (remainingAbsolute <= 0) throw new TransferRequestTimeoutError();
    let timeoutHandle: ReturnType<typeof setTimeout> | undefined;
    const readPromise = reader.read();
    // A timeout races a pending read; consume a later rejection after cancel
    // so a slow/disconnected stream cannot become an unhandled rejection.
    void readPromise.catch(() => undefined);
    const timeoutPromise = new Promise<never>((_, reject) => {
      timeoutHandle = setTimeout(
        () => reject(new TransferRequestTimeoutError()),
        Math.min(TRANSFER_BODY_IDLE_TIMEOUT_MS, remainingAbsolute),
      );
    });
    try {
      return await Promise.race([readPromise, timeoutPromise]);
    } finally {
      if (timeoutHandle !== undefined) clearTimeout(timeoutHandle);
    }
  };
  try {
    while (true) {
      const { done, value } = await readWithDeadline();
      if (done) break;
      if (!value) continue;
      total += value.byteLength;
      if (total > maxBytes) {
        try {
          await reader.cancel("request body exceeds limit");
        } catch {
          // Preserve the authoritative 413 result even if the client closed.
        }
        throw new TransferRequestTooLargeError();
      }
      chunks.push(value);
    }
  } catch (error) {
    await reader.cancel(error).catch(() => undefined);
    throw error;
  } finally {
    try {
      reader.releaseLock();
    } catch {
      // A timed-out stream may still be settling its cancellation promise.
    }
  }

  const body = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    body.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return body;
}

function parseTransferEnvelope(bytes: Uint8Array): FileTransferRequest {
  let text: string;
  try {
    text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    throw new TransferRequestError(400, "Invalid transfer request encoding");
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    throw new TransferRequestError(400, "Invalid transfer request JSON");
  }
  if (!isRecord(parsed) || !hasOnlyKeys(parsed, ["version", "meta", "data"])) {
    throw new TransferRequestError(400, "Invalid transfer request");
  }
  if (parsed.version !== FILE_TRANSFER_PROTOCOL_VERSION || typeof parsed.meta !== "string") {
    throw new TransferRequestError(400, "Invalid transfer protocol version");
  }
  return {
    version: FILE_TRANSFER_PROTOCOL_VERSION,
    meta: parsed.meta,
    data:
      parsed.data === undefined
        ? undefined
        : typeof parsed.data === "string"
          ? parsed.data
          : (() => {
              throw new TransferRequestError(400, "Invalid transfer data");
            })(),
  };
}

function decodeMeta(encoded: string): FileTransferMeta {
  if (encoded.length > MAX_TRANSFER_METADATA_BASE64_LENGTH) {
    throw new TransferRequestTooLargeError("Transfer metadata exceeds limit");
  }
  try {
    return parseTransferMeta(
      decodeTransferJson<unknown>(encoded, MAX_COMPAT_METADATA_BYTES),
    );
  } catch (error) {
    if (error instanceof TransferRequestError) throw error;
    throw new TransferRequestError(400, "Invalid transfer metadata encoding");
  }
}

function queryKeys(url: URL): string[] {
  return Array.from(url.searchParams.keys());
}

function singleQueryValue(url: URL, name: string, required: boolean): string | undefined {
  const values = url.searchParams.getAll(name);
  if (values.length > 1) {
    throw new TransferRequestError(400, `Duplicate query parameter: ${name}`);
  }
  if (required && values.length !== 1) {
    throw new TransferRequestError(400, `Missing query parameter: ${name}`);
  }
  return values[0];
}

function rejectUnknownQuery(url: URL, allowed: readonly string[]): void {
  const allowedSet = new Set(allowed);
  for (const key of queryKeys(url)) {
    if (!allowedSet.has(key)) {
      throw new TransferRequestError(403, "Transfer target query is not allowed");
    }
  }
}

function canonicalTargetUrl(origin: string, pathname: string, source: URL): URL {
  const target = new URL(pathname, origin);
  source.searchParams.forEach((value, key) => target.searchParams.append(key, value));
  return target;
}

function parseTransferTarget(
  meta: FileTransferMeta,
  request: NextRequest,
): TransferTarget {
  if (!meta.url.startsWith("/")) {
    throw new TransferRequestError(403, "Transfer target must be a relative URL");
  }
  let parsed: URL;
  try {
    parsed = new URL(meta.url, request.nextUrl.origin);
  } catch {
    throw new TransferRequestError(400, "Invalid transfer URL");
  }
  if (
    parsed.origin !== request.nextUrl.origin ||
    parsed.username ||
    parsed.password ||
    parsed.hash
  ) {
    throw new TransferRequestError(403, "Transfer target origin is not allowed");
  }

  let pathname: string;
  try {
    pathname = decodeURIComponent(parsed.pathname);
  } catch {
    throw new TransferRequestError(403, "Transfer target path is not allowed");
  }
  // A few callers build the same route through a helper that already carries
  // the `/api` prefix. Normalize that one harmless duplicate only; all other
  // paths remain subject to the exact allowlist below.
  if (pathname.startsWith("/api/python-proxy/api/")) {
    pathname = `/api/python-proxy/${pathname.slice("/api/python-proxy/api/".length)}`;
  }
  const prefix = "/api/python-proxy/";
  if (!pathname.startsWith(prefix) || pathname.includes("//")) {
    throw new TransferRequestError(403, "Transfer target is not allowed");
  }

  const explorerUpload = pathname === `${prefix}explorer/upload`;
  const explorerSave = pathname === `${prefix}explorer/save`;
  const explorerDownload = pathname === `${prefix}explorer/download`;
  const projectUploadMatch = pathname.match(
    new RegExp(`^${prefix}projects/([0-9a-f-]{36})/files/upload$`, "i"),
  );
  const projectUpload =
    projectUploadMatch !== null && UUID_PATTERN.test(projectUploadMatch[1]);
  const remoteDownloadMatch = pathname.match(
    new RegExp(`^${prefix}remote-servers/([^/]+)/workspace/download$`),
  );

  if (meta.operation === "upload" && (explorerUpload || projectUpload)) {
    if (parsed.searchParams.has("path")) singleQueryValue(parsed, "path", false);
    if (parsed.searchParams.has("overwrite")) {
      const overwrite = singleQueryValue(parsed, "overwrite", false);
      if (!overwrite || !/^(?:true|false)$/i.test(overwrite)) {
        throw new TransferRequestError(400, "Invalid overwrite query parameter");
      }
    }
    rejectUnknownQuery(parsed, ["path", "overwrite"]);
    return {
      canonicalUrl: canonicalTargetUrl(request.nextUrl.origin, pathname, parsed),
      kind: "upload",
      pathParts: pathname.slice(prefix.length).split("/"),
      method: "POST",
    };
  }

  if (meta.operation === "save" && explorerSave) {
    if (queryKeys(parsed).length > 0) {
      throw new TransferRequestError(400, "Save target must not include a query");
    }
    return {
      canonicalUrl: canonicalTargetUrl(request.nextUrl.origin, pathname, parsed),
      kind: "save",
      pathParts: ["explorer", "save"],
      method: "PUT",
    };
  }

  if (meta.operation === "download" && explorerDownload) {
    const method = meta.paths === undefined ? "GET" : "POST";
    if (method === "GET") {
      rejectUnknownQuery(parsed, ["path"]);
      const path = singleQueryValue(parsed, "path", true);
      if (!path) throw new TransferRequestError(400, "Missing query parameter: path");
    } else if (queryKeys(parsed).length > 0) {
      throw new TransferRequestError(400, "Selected download must not include a query");
    }
    return {
      canonicalUrl: canonicalTargetUrl(request.nextUrl.origin, pathname, parsed),
      kind: "explorer-download",
      pathParts: ["explorer", "download"],
      method,
    };
  }

  if (meta.operation === "download" && remoteDownloadMatch) {
    const profileId = remoteDownloadMatch[1];
    if (!SAFE_PROFILE_ID_PATTERN.test(profileId)) {
      throw new TransferRequestError(403, "Remote profile is not allowed");
    }
    rejectUnknownQuery(parsed, ["project_id", "path"]);
    const projectId = singleQueryValue(parsed, "project_id", true);
    if (!projectId || !UUID_PATTERN.test(projectId)) {
      throw new TransferRequestError(400, "Invalid project_id");
    }
    const path = singleQueryValue(parsed, "path", meta.paths === undefined);
    if (meta.paths === undefined && !path) {
      throw new TransferRequestError(400, "Missing query parameter: path");
    }
    return {
      canonicalUrl: canonicalTargetUrl(request.nextUrl.origin, pathname, parsed),
      kind: "remote-download",
      pathParts: pathname.slice(prefix.length).split("/"),
      method: meta.paths === undefined ? "GET" : "POST",
    };
  }

  throw new TransferRequestError(403, "Transfer target is not allowed");
}

function copyTransferHeaders(
  source: NextRequest,
  target: Headers,
  names: readonly string[],
): void {
  for (const name of names) {
    const value = source.headers.get(name);
    if (value) target.set(name, value);
  }
}

function makeSyntheticRequest(
  original: NextRequest,
  target: URL,
  method: "GET" | "POST" | "PUT",
  body?: BodyInit,
  contentType?: string,
): NextRequest {
  const headers = new Headers();
  if (contentType) headers.set("content-type", contentType);
  copyTransferHeaders(original, headers, [
    "idempotency-key",
    "x-asset-sha256",
    "if-match",
    "range",
    "if-none-match",
    "if-modified-since",
    "x-aoitalk-verification-run-id",
    "x-aoitalk-verification-harness",
    "x-aoitalk-verification-source",
    "x-aoitalk-verification-disposable",
    "x-aoitalk-verification-signature",
  ]);
  return new NextRequest(target, {
    method,
    headers,
    body,
    signal: original.signal,
  });
}

function transferForwardHeaders(): readonly string[] {
  return [
    "idempotency-key",
    "x-asset-sha256",
    "if-match",
    "x-aoitalk-verification-run-id",
    "x-aoitalk-verification-harness",
    "x-aoitalk-verification-source",
    "x-aoitalk-verification-disposable",
    "x-aoitalk-verification-signature",
  ];
}

function jsonRequestBody(value: unknown): string {
  return JSON.stringify(value);
}

async function executeTransferTarget(
  request: NextRequest,
  target: TransferTarget,
  meta: FileTransferMeta,
  data: Uint8Array | undefined,
  user: InternalPythonUser,
): Promise<NextResponse> {
  const authorityUser = { ...user, authoritySource: "web_session" as const };
  if (target.kind === "upload") {
    if (meta.operation !== "upload" || !data) {
      throw new TransferRequestError(400, "Upload data is required");
    }
    const mimeType = meta.fileType || "application/octet-stream";
    // Node's Buffer-backed Uint8Array may be a view into a larger slab. Copy
    // the exact decoded range before constructing the multipart Blob so slab
    // bytes can never leak into the uploaded file.
    const exactData = new Uint8Array(data.byteLength);
    exactData.set(data);
    const blob = new Blob([exactData.buffer as ArrayBuffer], { type: mimeType });
    const file = new File([blob], meta.fileName, { type: mimeType });
    const form = new FormData();
    form.append("file", file, meta.fileName);
    if (meta.relativePath !== undefined) {
      form.append("relative_path", meta.relativePath);
    }
    const synthetic = makeSyntheticRequest(
      request,
      target.canonicalUrl,
      "POST",
      form,
    );
    return proxyRequestToPythonApi(synthetic, {
      path: target.pathParts,
      user: authorityUser,
      forwardHeaders: transferForwardHeaders(),
    });
  }

  if (target.kind === "save") {
    if (meta.operation !== "save" || !data) {
      throw new TransferRequestError(400, "Save data is required");
    }
    let content: string;
    try {
      content = new TextDecoder("utf-8", { fatal: true }).decode(data);
    } catch {
      throw new TransferRequestError(400, "Save content must be valid UTF-8");
    }
    const synthetic = makeSyntheticRequest(
      request,
      target.canonicalUrl,
      "PUT",
      jsonRequestBody({
        path: meta.path,
        content,
        encoding: meta.encoding ?? "utf-8",
      }),
      "application/json",
    );
    return proxyRequestToPythonApi(synthetic, {
      path: target.pathParts,
      user: authorityUser,
      forwardHeaders: transferForwardHeaders(),
    });
  }

  const body =
    meta.operation === "download" && meta.paths !== undefined
      ? jsonRequestBody({ paths: meta.paths })
      : undefined;
  const synthetic = makeSyntheticRequest(
    request,
    target.canonicalUrl,
    target.method,
    body,
    body ? "application/json" : undefined,
  );
  if (target.kind === "explorer-download") {
    return proxyExplorerDownload(synthetic, authorityUser);
  }
  return proxyRequestToPythonApi(synthetic, {
    path: target.pathParts,
    user: authorityUser,
    forwardHeaders: transferForwardHeaders(),
    downstreamResponseLifetime: true,
  });
}

function headerFilename(response: Response): string | undefined {
  const disposition = response.headers.get("content-disposition") || "";
  const encoded = disposition.match(/filename\*\s*=\s*UTF-8''([^;]+)/i)?.[1];
  if (encoded) {
    try {
      return decodeURIComponent(encoded.replace(/^"|"$/g, ""));
    } catch {
      // Fall through to the quoted filename form.
    }
  }
  const plain = disposition.match(/filename\s*=\s*(?:"([^"]*)"|([^;]*))/i);
  const filename = plain?.[1] ?? plain?.[2]?.trim();
  return filename || undefined;
}

function downloadMetadata(
  response: Response,
  fallbackFilename: string | undefined,
): FileTransferDownloadMetadata {
  const filename = headerFilename(response) || fallbackFilename || "download";
  const mimeType =
    response.headers.get("content-type")?.split(";", 1)[0].trim() ||
    "application/octet-stream";
  const rawSize = response.headers.get("content-length");
  const parsedSize =
    rawSize !== null && /^\d+$/.test(rawSize) ? Number(rawSize) : undefined;
  const size =
    parsedSize !== undefined && Number.isSafeInteger(parsedSize) && parsedSize >= 0
      ? parsedSize
      : undefined;
  return size === undefined ? { filename, mimeType } : { filename, mimeType, size };
}

function ndjsonLine(value: unknown): Uint8Array {
  return new TextEncoder().encode(`${JSON.stringify(value)}\n`);
}

function streamDownloadResponse(
  response: NextResponse,
  fallbackFilename: string | undefined,
): NextResponse {
  const metadata = downloadMetadata(response, fallbackFilename);
  const reader = response.body?.getReader();
  let metadataSent = false;
  let sourceDone = reader === undefined;
  let finished = false;
  let sourceChunk: Uint8Array | undefined;
  let sourceOffset = 0;
  let total = 0;
  let readerReleased = false;

  const releaseReader = () => {
    if (reader && !readerReleased) {
      readerReleased = true;
      reader.releaseLock();
    }
  };

  const body = new ReadableStream<Uint8Array>({
    async pull(controller) {
      if (finished) return;
      try {
        if (!metadataSent) {
          metadataSent = true;
          controller.enqueue(
            ndjsonLine({
              type: "meta",
              value: encodeTransferJson(metadata),
            }),
          );
          return;
        }
        if (sourceChunk && sourceOffset < sourceChunk.byteLength) {
          const end = Math.min(
            sourceChunk.byteLength,
            sourceOffset + MAX_COMPAT_DOWNLOAD_CHUNK_BYTES,
          );
          const chunk = sourceChunk.subarray(sourceOffset, end);
          sourceOffset = end;
          total += chunk.byteLength;
          controller.enqueue(
            ndjsonLine({ type: "chunk", data: encodeTransferBase64(chunk) }),
          );
          return;
        }
        sourceChunk = undefined;
        sourceOffset = 0;
        if (!sourceDone && reader) {
          while (!sourceDone) {
            const next = await reader.read();
            if (next.done) {
              sourceDone = true;
              releaseReader();
              break;
            }
            // Empty upstream chunks are legal. Keep reading until there is a
            // real payload or an actual done=true signal; an empty chunk must
            // never be mistaken for a successful end frame.
            if (!next.value?.byteLength) continue;
            sourceChunk = next.value;
            // Emit the chunk during this same pull. Resolving a pull without
            // enqueueing can leave a consumer's next read pending forever.
            const end = Math.min(
              sourceChunk.byteLength,
              MAX_COMPAT_DOWNLOAD_CHUNK_BYTES,
            );
            const chunk = sourceChunk.subarray(0, end);
            sourceOffset = end;
            total += chunk.byteLength;
            controller.enqueue(
              ndjsonLine({ type: "chunk", data: encodeTransferBase64(chunk) }),
            );
            return;
          }
        }
        if (sourceDone) {
          finished = true;
          controller.enqueue(ndjsonLine({ type: "end", size: total }));
          controller.close();
        }
      } catch {
        if (finished) return;
        finished = true;
        await reader?.cancel().catch(() => undefined);
        releaseReader();
        // Keep the reason generic: upstream details may contain paths or
        // credentials, and the transfer client only needs the missing `end`
        // marker to treat this stream as unsuccessful.
        try {
          controller.enqueue(
            ndjsonLine({ type: "error", message: "Download stream interrupted" }),
          );
          controller.close();
        } catch {
          // The downstream may have cancelled between read() and enqueue().
        }
      }
    },
    async cancel(reason) {
      finished = true;
      await reader?.cancel(reason).catch(() => undefined);
      releaseReader();
    },
  });

  return new NextResponse(body, {
    status: response.status,
    headers: {
      ...NO_STORE_HEADERS,
      "content-type": "application/x-ndjson",
    },
  });
}

async function readBoundedResponseBody(
  response: Response,
  maxBytes: number,
): Promise<Uint8Array | undefined> {
  if (!response.body) return new Uint8Array();
  const declared = Number(response.headers.get("content-length"));
  if (Number.isFinite(declared) && declared > maxBytes) {
    await response.body.cancel();
    return undefined;
  }
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      if (!value) continue;
      total += value.byteLength;
      if (total > maxBytes) {
        await reader.cancel();
        return undefined;
      }
      chunks.push(value);
    }
  } catch {
    await reader.cancel().catch(() => undefined);
    throw new Error("upstream error response stream interrupted");
  } finally {
    reader.releaseLock();
  }
  const body = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    body.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return body;
}

async function preserveUpstreamError(response: NextResponse): Promise<NextResponse> {
  let body: Uint8Array | undefined;
  try {
    body = await readBoundedResponseBody(
      response,
      MAX_TRANSFER_ERROR_RESPONSE_BYTES,
    );
  } catch {
    return NextResponse.json(
      { detail: "Python APIに接続できません" },
      { status: 502, headers: NO_STORE_HEADERS },
    );
  }
  if (!body) {
    return NextResponse.json(
      { detail: "Python API error response exceeds proxy limit" },
      { status: 502, headers: NO_STORE_HEADERS },
    );
  }
  const headers = new Headers();
  for (const name of ["content-type", "cache-control", "etag", "vary"]) {
    const value = response.headers.get(name);
    if (value) headers.set(name, value);
  }
  return new NextResponse(new Blob([body.buffer as ArrayBuffer]), {
    status: response.status,
    headers,
  });
}

export async function proxyFileTransfer(
  request: NextRequest,
  user: InternalPythonUser,
): Promise<NextResponse> {
  if (!acquireBufferedTransferSlot(user)) {
    return NextResponse.json(
      { detail: "File transfer service is busy" },
      {
        status: 429,
        headers: { ...NO_STORE_HEADERS, "retry-after": "1" },
      },
    );
  }
  try {
    let envelope: FileTransferRequest;
    try {
      const body = await readBoundedRequestBody(
        request,
        MAX_COMPAT_REQUEST_BODY_BYTES,
      );
      envelope = parseTransferEnvelope(body);
    } catch (error) {
      if (isClientRequestAbort(error, request.signal)) {
        return new NextResponse(null, { status: 499, headers: NO_STORE_HEADERS });
      }
      if (error instanceof TransferRequestError) {
        return NextResponse.json(
          { detail: error.detail },
          { status: error.status, headers: NO_STORE_HEADERS },
        );
      }
      return NextResponse.json(
        { detail: "Invalid file transfer request" },
        { status: 400, headers: NO_STORE_HEADERS },
      );
    }

    try {
      const meta = decodeMeta(envelope.meta);
      const target = parseTransferTarget(meta, request);
      let data: Uint8Array | undefined;
      if (meta.operation === "upload" || meta.operation === "save") {
        data = decodeBoundedData(envelope.data, "transfer data");
      } else if (envelope.data !== undefined) {
        throw new TransferRequestError(400, "Download requests must not include data");
      }

      const upstream = await executeTransferTarget(request, target, meta, data, user);
      if (target.kind === "explorer-download" || target.kind === "remote-download") {
        if (!upstream.ok) return preserveUpstreamError(upstream);
        return streamDownloadResponse(
          upstream,
          meta.operation === "download" ? meta.fallbackFilename : undefined,
        );
      }
      return upstream;
    } catch (error) {
      if (isClientRequestAbort(error, request.signal)) {
        return new NextResponse(null, { status: 499, headers: NO_STORE_HEADERS });
      }
      if (error instanceof TransferRequestError) {
        return NextResponse.json(
          { detail: error.detail },
          { status: error.status, headers: NO_STORE_HEADERS },
        );
      }
      return NextResponse.json(
        { detail: "Invalid file transfer request" },
        { status: 400, headers: NO_STORE_HEADERS },
      );
    }
  } finally {
    releaseBufferedTransferSlot(user);
  }
}
