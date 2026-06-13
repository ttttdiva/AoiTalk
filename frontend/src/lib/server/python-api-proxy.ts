import { NextRequest, NextResponse } from "next/server";

export const NO_STORE_HEADERS = {
  "cache-control": "no-store, no-cache, must-revalidate, proxy-revalidate",
  pragma: "no-cache",
  expires: "0",
};

export type InternalPythonUser = {
  username?: string | null;
};

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

function copySearchParams(source: URLSearchParams, target: URL): void {
  source.forEach((value, key) => {
    target.searchParams.set(key, value);
  });
}

function isTextualContentType(contentType: string): boolean {
  return (
    contentType.startsWith("text/") ||
    contentType.includes("json") ||
    contentType.includes("xml") ||
    contentType.includes("javascript") ||
    contentType.includes("x-www-form-urlencoded")
  );
}

function isBinaryResponse(contentType: string, contentDisposition: string | null) {
  // ファイル添付（inline 含む）やテキスト系以外の content-type は
  // テキストデコードせずバイト列のまま転送する（PDF などの破損防止）。
  if (contentDisposition?.includes("filename")) return true;
  if (contentDisposition?.includes("attachment")) return true;
  if (!contentType) return false;
  return !isTextualContentType(contentType);
}

export async function proxyRequestToPythonApi(
  request: NextRequest,
  options: {
    path: string[];
    user: InternalPythonUser;
  },
): Promise<NextResponse> {
  const url = new URL(normalizeApiPath(options.path), getPythonApiBaseUrl());
  copySearchParams(request.nextUrl.searchParams, url);

  const headers: Record<string, string> = {};
  const contentType = request.headers.get("content-type");
  if (contentType) headers["content-type"] = contentType;

  const rangeHeader = request.headers.get("range");
  if (rangeHeader) headers["range"] = rangeHeader;

  const init: RequestInit = {
    method: request.method,
    headers: buildInternalPythonHeaders(options.user, headers),
    cache: "no-store",
  };

  if (request.method !== "GET" && request.method !== "HEAD") {
    init.body = contentType?.includes("multipart/form-data")
      ? await request.arrayBuffer()
      : await request.text();
  }

  try {
    const res = await fetch(url.toString(), init);
    const resContentType = res.headers.get("content-type") || "";
    const contentDisposition = res.headers.get("content-disposition");

    if (isBinaryResponse(resContentType, contentDisposition)) {
      const body = await res.arrayBuffer();
      const respHeaders: Record<string, string> = {
        "content-type": resContentType,
        "content-length": String(body.byteLength),
        ...NO_STORE_HEADERS,
      };
      if (contentDisposition) {
        respHeaders["content-disposition"] = contentDisposition;
      }
      const acceptRanges = res.headers.get("accept-ranges");
      if (acceptRanges) respHeaders["accept-ranges"] = acceptRanges;
      const contentRange = res.headers.get("content-range");
      if (contentRange) respHeaders["content-range"] = contentRange;
      return new NextResponse(body, {
        status: res.status,
        headers: respHeaders,
      });
    }

    const body = await res.text();
    return new NextResponse(body, {
      status: res.status,
      headers: {
        "content-type": resContentType || "application/json",
        ...NO_STORE_HEADERS,
      },
    });
  } catch {
    return NextResponse.json(
      { detail: "Python APIに接続できません" },
      { status: 502, headers: NO_STORE_HEADERS },
    );
  }
}
