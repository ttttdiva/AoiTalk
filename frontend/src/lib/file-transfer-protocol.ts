/**
 * Wire contract for the optional browser transfer compatibility route.
 *
 * This module is intentionally free of Next.js/server imports so the client
 * can use the exact same envelope and strict base64 rules as the route.
 */

export const FILE_TRANSFER_ENDPOINT = "/api/file-transfer" as const;
export const FILE_TRANSFER_PROTOCOL_VERSION = 1 as const;
export const MAX_COMPAT_UPLOAD_FILE_BYTES = 64 * 1024 * 1024;
export const MAX_COMPAT_REQUEST_BODY_BYTES = 90 * 1024 * 1024;
export const MAX_COMPAT_METADATA_BYTES = 256 * 1024;
export const MAX_COMPAT_DOWNLOAD_CHUNK_BYTES = 64 * 1024;
export const MAX_COMPAT_DOWNLOAD_FILE_BYTES = 64 * 1024 * 1024;

export type FileTransferMeta =
  | {
      operation: "upload";
      url: string;
      fileName: string;
      fileType: string;
      relativePath?: string;
    }
  | {
      operation: "download";
      url: string;
      paths?: string[];
      fallbackFilename?: string;
      responseContract?: {
        expectedContentType: "application/zip";
        requireAttachment: true;
        rejectRedirects: true;
      };
    }
  | {
      operation: "save";
      url: string;
      path: string;
      encoding?: string;
    };

export type FileTransferRequest = {
  version: typeof FILE_TRANSFER_PROTOCOL_VERSION;
  meta: string;
  data?: string;
};

export type FileTransferDownloadMetadata = {
  filename: string;
  mimeType: string;
  size?: number;
  attachment?: true;
};

export type FileTransferDownloadLine =
  | { type: "meta"; value: string }
  | { type: "chunk"; data: string }
  | { type: "end"; size: number }
  | { type: "error"; message: string };

function base64FromBinary(binary: string): string {
  if (typeof btoa === "function") return btoa(binary);
  // `btoa` is available in the browser and in the Node runtime used by the
  // Next route. Keep the fallback lazy so this module stays browser-safe.
  if (typeof Buffer === "undefined") throw new Error("base64 encoder unavailable");
  return Buffer.from(binary, "binary").toString("base64");
}

function binaryFromBase64(value: string): string {
  if (typeof atob === "function") return atob(value);
  if (typeof Buffer === "undefined") throw new Error("base64 decoder unavailable");
  return Buffer.from(value, "base64").toString("binary");
}

function hasNodeBuffer(): boolean {
  return typeof Buffer !== "undefined" && typeof Buffer.from === "function";
}

function isBase64Character(code: number): boolean {
  return (
    (code >= 0x41 && code <= 0x5a) ||
    (code >= 0x61 && code <= 0x7a) ||
    (code >= 0x30 && code <= 0x39) ||
    code === 0x2b ||
    code === 0x2f
  );
}

function base64Sextet(code: number): number {
  if (code >= 0x41 && code <= 0x5a) return code - 0x41;
  if (code >= 0x61 && code <= 0x7a) return code - 0x61 + 26;
  if (code >= 0x30 && code <= 0x39) return code - 0x30 + 52;
  if (code === 0x2b) return 62;
  if (code === 0x2f) return 63;
  return -1;
}

function assertCanonicalBase64Shape(value: string): void {
  if (value.length % 4 !== 0) throw new Error("invalid base64");
  let padding = 0;
  if (value.endsWith("=")) padding += 1;
  if (value.endsWith("==")) padding += 1;
  const contentLength = value.length - padding;
  for (let index = 0; index < contentLength; index += 1) {
    if (!isBase64Character(value.charCodeAt(index))) {
      throw new Error("invalid base64");
    }
  }
  for (let index = contentLength; index < value.length; index += 1) {
    if (value.charCodeAt(index) !== 0x3d) throw new Error("invalid base64");
  }
  if (padding > 2 || (padding === 1 && contentLength % 4 !== 3) || (padding === 2 && contentLength % 4 !== 2)) {
    throw new Error("invalid base64");
  }
  // Canonical base64 has zero in the unused low bits of the final sextet.
  if (padding === 1 && (base64Sextet(value.charCodeAt(contentLength - 1)) & 0x03) !== 0) {
    throw new Error("invalid base64");
  }
  if (padding === 2 && (base64Sextet(value.charCodeAt(contentLength - 1)) & 0x0f) !== 0) {
    throw new Error("invalid base64");
  }
}

/** Encode bytes using the standard RFC 4648 base64 alphabet. */
export function encodeTransferBase64(bytes: Uint8Array): string {
  if (hasNodeBuffer()) {
    return Buffer.from(
      bytes.buffer as ArrayBuffer,
      bytes.byteOffset,
      bytes.byteLength,
    ).toString("base64");
  }
  // Avoid a spread or a single String.fromCharCode call for large uploads:
  // both can exceed the JS argument/stack limit around a few MiB.
  const pieces: string[] = [];
  // A multiple of three keeps chunk boundaries from introducing padding.
  const pieceSize = 0x7ffe;
  for (let offset = 0; offset < bytes.byteLength; offset += pieceSize) {
    const end = Math.min(bytes.byteLength, offset + pieceSize);
    let piece = "";
    for (let index = offset; index < end; index += 1) {
      piece += String.fromCharCode(bytes[index]);
    }
    pieces.push(base64FromBinary(piece));
  }
  return pieces.join("");
}

/**
 * Decode only canonical standard base64. Whitespace, URL-safe alphabet,
 * malformed padding, and non-zero unused bits are rejected.
 */
export function decodeTransferBase64(value: string): Uint8Array {
  if (typeof value !== "string") {
    throw new Error("invalid base64");
  }
  assertCanonicalBase64Shape(value);
  if (hasNodeBuffer()) {
    const decoded = Buffer.from(value, "base64");
    const bytes = new Uint8Array(
      decoded.buffer as ArrayBuffer,
      decoded.byteOffset,
      decoded.byteLength,
    );
    if (encodeTransferBase64(bytes) !== value) throw new Error("invalid base64");
    return bytes;
  }
  const binary = binaryFromBase64(value);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  // atob accepts non-canonical trailing bits (e.g. `AB==`). Re-encoding also
  // catches those cases while preserving an exact canonical string check.
  if (encodeTransferBase64(bytes) !== value) throw new Error("invalid base64");
  return bytes;
}

export function encodeTransferJson(value: unknown): string {
  return encodeTransferBase64(new TextEncoder().encode(JSON.stringify(value)));
}

export function decodeTransferJson<T>(value: string, maxBytes?: number): T {
  const bytes = decodeTransferBase64(value);
  if (maxBytes !== undefined && bytes.byteLength > maxBytes) {
    throw new Error("metadata exceeds limit");
  }
  return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes)) as T;
}
