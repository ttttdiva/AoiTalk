import { NextRequest, NextResponse } from "next/server";

export const NO_STORE_HEADERS = {
  "cache-control": "no-store, no-cache, must-revalidate, proxy-revalidate",
  pragma: "no-cache",
  expires: "0",
};

export type InternalPythonUser = {
  id?: string | null;
  username?: string | null;
};

const FORWARDABLE_REQUEST_HEADERS = new Set(["idempotency-key"]);

const ENTERPRISE_DISABLED_API_ROOTS = [
  "/api/mobile",
  "/api/sync",
  "/api/story",
  "/api/scenarios",
  "/api/trpg",
  "/api/hydrus",
  "/api/comfyui",
  "/api/crawler",
  "/api/remote-servers",
] as const;

function isEnterpriseProfile(): boolean {
  return (
    process.env.AIVTUBER_ENV?.trim().toLowerCase() === "enterprise" ||
    process.env.AOITALK_PROFILE?.trim().toLowerCase() === "enterprise"
  );
}

export function getPythonApiBaseUrl(): string {
  return process.env.PYTHON_API_URL || "http://127.0.0.1:3000";
}

export function getInternalApiKey(): string {
  const key = process.env.INTERNAL_API_KEY;
  if (!key) {
    // service_manager.py が Next.js 起動時に INTERNAL_API_KEY を環境変数として
    // 注入する。未設定で起動された場合は構成不備として即座に失敗させる。
    throw new Error(
      "INTERNAL_API_KEY is not set. Start the frontend via service_manager " +
        "or export INTERNAL_API_KEY before `npm run start`.",
    );
  }
  return key;
}

export function buildInternalPythonHeaders(
  user: InternalPythonUser,
  headers?: HeadersInit,
): Headers {
  const nextHeaders = new Headers(headers);
  nextHeaders.set("x-internal-auth", getInternalApiKey());
  if (user.id) {
    nextHeaders.set("x-forwarded-user-id", user.id);
  }
  if (user.username) {
    nextHeaders.set("x-forwarded-user", user.username);
  }
  return nextHeaders;
}

export async function fetchPythonApi(
  apiPath: string,
  init: RequestInit & { user: InternalPythonUser },
): Promise<Response> {
  const { user, headers, ...requestInit } = init;
  const url = new URL(apiPath, getPythonApiBaseUrl());
  return fetch(url.toString(), {
    ...requestInit,
    cache: "no-store",
    headers: buildInternalPythonHeaders(user, headers),
  });
}

function normalizeApiPath(pathParts: string[]): string {
  const rawPath = pathParts.join("/");
  const apiPath = rawPath.startsWith("api/") ? rawPath.slice(4) : rawPath;
  return `/api/${apiPath}`;
}

function isEnterpriseDisabledApiPath(apiPath: string): boolean {
  return (
    isEnterpriseProfile() &&
    ENTERPRISE_DISABLED_API_ROOTS.some(
      (root) => apiPath === root || apiPath.startsWith(`${root}/`),
    )
  );
}

function copySearchParams(source: URLSearchParams, target: URL): void {
  source.forEach((value, key) => {
    target.searchParams.set(key, value);
  });
}

// GET/HEAD レスポンスでは、バックエンドの ETag / Cache-Control を透過して
// ブラウザの条件付き GET（If-None-Match → 304）を成立させる。
// 低帯域環境では 200 全文の代わりに 304（空ボディ）で済み、帯域を節約できる。
function buildResponseCacheHeaders(
  res: Response,
  isGet: boolean,
): Record<string, string> {
  if (!isGet) return { ...NO_STORE_HEADERS };
  const headers: Record<string, string> = {};
  const cacheControl = res.headers.get("cache-control");
  // バックエンドが明示的に検証キャッシュを許可した応答だけ保存可能にする。
  // ヘッダーが無い任意 GET を一律保存すると、動的状態や機密レスポンスまで
  // ブラウザキャッシュへ残るため、従来どおり no-store を既定にする。
  headers["cache-control"] = cacheControl ?? NO_STORE_HEADERS["cache-control"];
  const etag = res.headers.get("etag");
  if (etag) headers["etag"] = etag;
  const lastModified = res.headers.get("last-modified");
  if (lastModified) headers["last-modified"] = lastModified;
  const vary = res.headers.get("vary");
  if (vary) headers["vary"] = vary;
  return headers;
}

function buildResponseSecurityHeaders(res: Response): Record<string, string> {
  const forwarded: Record<string, string> = {};
  for (const name of [
    "content-security-policy",
    "content-security-policy-report-only",
    "x-content-type-options",
    "referrer-policy",
    "permissions-policy",
    "cross-origin-resource-policy",
  ]) {
    const value = res.headers.get(name);
    if (value) forwarded[name] = value;
  }
  return forwarded;
}

const FORWARDED_RESPONSE_BODY_HEADERS = [
  "content-length",
  "content-disposition",
  "accept-ranges",
  "content-range",
] as const;

function buildResponseHeaders(
  res: Response,
  isGet: boolean,
): Record<string, string> {
  const contentType = res.headers.get("content-type");
  const contentEncoding = res.headers.get("content-encoding");
  const contentDisposition = res.headers.get("content-disposition");
  const isFileResponse =
    contentDisposition?.includes("filename") === true ||
    contentDisposition?.includes("attachment") === true;
  const headers: Record<string, string> = {
    // FastAPI normally sends content-type. Keep the historical JSON fallback
    // for error responses that omit it, while preserving a missing type on
    // file responses (the old buffered binary branch did the same).
    "content-type": contentType || (isFileResponse ? "" : "application/json"),
    ...buildResponseCacheHeaders(res, isGet),
    ...buildResponseSecurityHeaders(res),
  };
  for (const name of FORWARDED_RESPONSE_BODY_HEADERS) {
    // Node's fetch transparently decodes gzip/deflate responses while keeping
    // the upstream compressed Content-Length header. Forwarding that stale
    // length makes Next truncate the decoded stream (notably /api/spaces),
    // yielding invalid JSON and hiding the Space selector. Let Next calculate
    // the length for decoded responses instead.
    if (name === "content-length" && contentEncoding) continue;
    const value = res.headers.get(name);
    if (value) headers[name] = value;
  }
  return headers;
}

function isReadableStreamBody(
  body: BodyInit,
): body is ReadableStream<Uint8Array> {
  return (
    typeof body === "object" &&
    body !== null &&
    typeof (body as { getReader?: unknown }).getReader === "function"
  );
}

export async function proxyRequestToPythonApi(
  request: NextRequest,
  options: {
    path: string[];
    user: InternalPythonUser;
    /**
     * Optional pre-read body supplied by a route-specific bounded proxy. When
     * present, this replaces the incoming request stream after the caller has
     * enforced its own ReadableStream byte limit.
     */
    body?: BodyInit;
    /**
     * Byte length for a pre-read bounded body. Route-specific proxies must
     * supply this rather than trusting the client Content-Length header; the
     * Python Live Voice endpoints require an explicit length to reject
     * unbounded/chunked payloads safely.
     */
    contentLength?: number;
    /**
     * Request headers that this route explicitly opts into forwarding.  The
     * names are intersected with a strict allowlist below; arbitrary client
     * headers must never cross the internal API boundary.
     */
    forwardHeaders?: readonly string[];
  },
): Promise<NextResponse> {
  const apiPath = normalizeApiPath(options.path);
  if (isEnterpriseDisabledApiPath(apiPath)) {
    return NextResponse.json(
      { detail: "Not Found" },
      { status: 404, headers: NO_STORE_HEADERS },
    );
  }

  const url = new URL(apiPath, getPythonApiBaseUrl());
  copySearchParams(request.nextUrl.searchParams, url);

  const isGet = request.method === "GET" || request.method === "HEAD";

  const headers: Record<string, string> = {};
  const contentType = request.headers.get("content-type");
  if (contentType) headers["content-type"] = contentType;
  const isMultipart = contentType?.includes("multipart/form-data") === true;
  if (isMultipart) {
    const contentLength = request.headers.get("content-length");
    if (contentLength) headers["content-length"] = contentLength;
  }
  if (
    options.contentLength !== undefined &&
    Number.isSafeInteger(options.contentLength) &&
    options.contentLength >= 0
  ) {
    // The body has already been read and bounded by the caller. Never copy a
    // client-declared value here: it may be absent, stale, or intentionally
    // misleading.
    headers["content-length"] = String(options.contentLength);
  }

  const rangeHeader = request.headers.get("range");
  if (rangeHeader) headers["range"] = rangeHeader;

  for (const requestedName of options.forwardHeaders ?? []) {
    const name = requestedName.trim().toLowerCase();
    if (!FORWARDABLE_REQUEST_HEADERS.has(name)) continue;
    const value = request.headers.get(name);
    if (value) headers[name] = value;
  }

  // OAuth callback の postMessage 先には、クライアント申告の Origin ではなく
  // この Next.js リクエスト自身の origin を渡す。
  headers["x-forwarded-origin"] = request.nextUrl.origin;

  // 条件付き GET のヘッダーをバックエンドへ転送し、304 を成立させる。
  if (isGet) {
    const ifNoneMatch = request.headers.get("if-none-match");
    if (ifNoneMatch) headers["if-none-match"] = ifNoneMatch;
    const ifModifiedSince = request.headers.get("if-modified-since");
    if (ifModifiedSince) headers["if-modified-since"] = ifModifiedSince;
  }

  let internalHeaders: Headers;
  try {
    internalHeaders = buildInternalPythonHeaders(options.user, headers);
  } catch (error) {
    // Configuration failures happen while constructing the internal auth
    // headers, before the network try/catch below. Return a structured response
    // rather than allowing Next.js to render an unhelpful 500 page. The error
    // text is intentionally not echoed because startup diagnostics can contain
    // deployment paths or other sensitive values.
    const message = error instanceof Error ? error.message.toLowerCase() : "";
    if (message.includes("internal_api_key") || message.includes("internal api key")) {
      return NextResponse.json(
        {
          detail: "Internal Python API authentication is not configured",
          code: "internal_api_not_configured",
        },
        { status: 503, headers: NO_STORE_HEADERS },
      );
    }
    return NextResponse.json(
      { detail: "Python API proxy configuration is unavailable", code: "proxy_not_configured" },
      { status: 503, headers: NO_STORE_HEADERS },
    );
  }

  const init: RequestInit = {
    method: request.method,
    headers: internalHeaders,
    cache: "no-store",
    signal: request.signal,
  };

  if (request.method !== "GET" && request.method !== "HEAD") {
    if (options.body !== undefined) {
      init.body = options.body;
    } else {
      // Keep every incoming body streamed. The generic proxy handles JSON,
      // SDP, multipart uploads, and arbitrary binary requests alike; reading
      // request.text()/arrayBuffer() here would make a client-controlled body
      // an unbounded allocation in the Next.js process.
      init.body = request.body;
    }
    if (
      init.body !== undefined &&
      init.body !== null &&
      isReadableStreamBody(init.body)
    ) {
      // Node's Fetch implementation requires duplex for a streaming request
      // body. This also preserves backpressure from FastAPI to the client.
      (init as RequestInit & { duplex: "half" }).duplex = "half";
    }
  }

  try {
    const res = await fetch(url.toString(), init);

    // 304 Not Modified はボディを持たないため、キャッシュ用ヘッダーのみで返す。
    if (res.status === 304) {
      return new NextResponse(null, {
        status: 304,
        headers: {
          ...buildResponseCacheHeaders(res, isGet),
          ...buildResponseSecurityHeaders(res),
        },
      });
    }

    const hasNoBody =
      request.method === "HEAD" || [204, 205].includes(res.status);
    return new NextResponse(hasNoBody ? null : res.body, {
      status: res.status,
      headers: buildResponseHeaders(res, isGet),
    });
  } catch (error) {
    // A disconnected browser request must stay cancelled all the way through
    // the proxy.  Do not manufacture a 502 response after the client has gone
    // away; Next/Fetch will terminate the downstream stream and the upstream
    // request already carries the same AbortSignal.
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
