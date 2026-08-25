import { NextRequest, NextResponse } from "next/server";
import {
  fetchPythonApi,
  NO_STORE_HEADERS,
  type InternalPythonUser,
} from "@/lib/server/python-api-proxy";

const FORWARDED_RESPONSE_HEADERS = [
  "content-type",
  "content-length",
  "content-disposition",
  "accept-ranges",
  "content-range",
  "etag",
  "last-modified",
  "x-content-type-options",
] as const;

// Selected-download requests are JSON path lists. Bound the route-specific
// buffering before forwarding to FastAPI instead of calling request.text()
// on an untrusted body.
export const MAX_EXPLORER_DOWNLOAD_REQUEST_BYTES = 256 * 1024;

class RequestBodyTooLargeError extends Error {
  constructor() {
    super("Explorer download request body is too large");
    this.name = "RequestBodyTooLargeError";
  }
}

class InvalidExplorerDownloadFormError extends Error {
  constructor() {
    super("Explorer download form is invalid");
    this.name = "InvalidExplorerDownloadFormError";
  }
}

async function readBoundedRequestBody(
  request: NextRequest,
  maxBytes: number,
): Promise<Uint8Array> {
  const declaredLength = Number(request.headers.get("content-length"));
  if (Number.isFinite(declaredLength) && declaredLength > maxBytes) {
    throw new RequestBodyTooLargeError();
  }
  if (!request.body) return new Uint8Array();

  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      if (!value) continue;
      total += value.byteLength;
      if (total > maxBytes) {
        // Cancellation is best effort. Some request implementations reject
        // cancel() after the client has already disconnected, but the body is
        // still over the route cap and must retain its 413 response semantics.
        try {
          await reader.cancel("request body exceeds limit");
        } catch {
          // Keep the size error below as the authoritative result.
        }
        throw new RequestBodyTooLargeError();
      }
      chunks.push(value);
    }
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

function isFormUrlEncoded(contentType: string | null): boolean {
  return (
    contentType?.split(";", 1)[0].trim().toLowerCase() ===
    "application/x-www-form-urlencoded"
  );
}

function formBodyToJson(bytes: Uint8Array): Uint8Array {
  let formText: string;
  try {
    formText = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    throw new InvalidExplorerDownloadFormError();
  }

  const form = new URLSearchParams(formText);
  const pathValues = form.getAll("paths");
  if (pathValues.length !== 1) {
    throw new InvalidExplorerDownloadFormError();
  }

  let paths: unknown;
  try {
    paths = JSON.parse(pathValues[0]);
  } catch {
    throw new InvalidExplorerDownloadFormError();
  }
  if (
    !Array.isArray(paths) ||
    paths.some((path) => typeof path !== "string")
  ) {
    throw new InvalidExplorerDownloadFormError();
  }

  return new TextEncoder().encode(JSON.stringify({ paths }));
}

export async function proxyExplorerDownload(
  request: NextRequest,
  user: InternalPythonUser,
): Promise<NextResponse> {
  const method = request.method;
  const isGetOrHead = method === "GET" || method === "HEAD";
  const contentType = request.headers.get("content-type");
  const requestHeaders: Record<string, string> = {};

  for (const name of [
    "content-type",
    "range",
    "if-none-match",
    "if-modified-since",
  ]) {
    const value = request.headers.get(name);
    if (value) requestHeaders[name] = value;
  }

  try {
    let body: BodyInit | undefined;
    if (!isGetOrHead) {
      const bytes = await readBoundedRequestBody(
        request,
        MAX_EXPLORER_DOWNLOAD_REQUEST_BYTES,
      );
      const bodyBytes =
        method === "POST" && isFormUrlEncoded(contentType)
          ? formBodyToJson(bytes)
          : bytes;
      if (method === "POST" && isFormUrlEncoded(contentType)) {
        requestHeaders["content-type"] = "application/json";
      }
      // Blob is a standards-compliant BodyInit across the Node/Next TypeScript
      // versions in use here (Uint8Array itself is not included in older DOM
      // lib definitions even though undici accepts it at runtime).
      body = new Blob([bodyBytes.buffer as ArrayBuffer]);
      requestHeaders["content-length"] = String(bodyBytes.byteLength);
    }
    const response = await fetchPythonApi(
      `/api/explorer/download${request.nextUrl.search}`,
      {
        method,
        user,
        headers: requestHeaders,
        body,
        signal: request.signal,
      },
    );
    const responseHeaders: Record<string, string> = { ...NO_STORE_HEADERS };
    for (const name of FORWARDED_RESPONSE_HEADERS) {
      const value = response.headers.get(name);
      if (value) responseHeaders[name] = value;
    }
    const hasNoBody =
      method === "HEAD" || [204, 205, 304].includes(response.status);

    return new NextResponse(hasNoBody ? null : response.body, {
      status: response.status,
      headers: responseHeaders,
    });
  } catch (error) {
    if (error instanceof RequestBodyTooLargeError) {
      return NextResponse.json(
        { detail: "ダウンロード対象の一覧が大きすぎます" },
        { status: 413, headers: NO_STORE_HEADERS },
      );
    }
    if (error instanceof InvalidExplorerDownloadFormError) {
      return NextResponse.json(
        { detail: "paths フィールドが不正です" },
        { status: 400, headers: NO_STORE_HEADERS },
      );
    }
    if (
      request.signal.aborted ||
      (error instanceof DOMException && error.name === "AbortError") ||
      (error instanceof Error && error.name === "AbortError")
    ) {
      throw error;
    }
    return NextResponse.json(
      { detail: "Python APIに接続できません" },
      { status: 502, headers: NO_STORE_HEADERS },
    );
  }
}
