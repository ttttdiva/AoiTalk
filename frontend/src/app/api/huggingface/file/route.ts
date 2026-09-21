import { isIP } from "node:net";
import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import { resolveTokenForUser } from "@/lib/hf/account";
import { buildAuthHeaders, buildFileUrl, type RepoType } from "@/lib/hf/client";
import { getMediaType } from "@/lib/hf/api-utils";

const MAX_TEXT_BYTES = 1024 * 1024;
const MAX_REDIRECT_HOPS = 5;
const HF_CANONICAL_ORIGIN = "https://huggingface.co";
const PRIVATE_HEADERS = { "Cache-Control": "private, no-store" };

const FOLLOWABLE_REDIRECT_STATUSES = new Set([301, 302, 303, 307, 308]);
const HF_REDIRECT_EXACT_HOSTS = new Set([
  // Canonical Hub / existing reviewed endpoints.
  "huggingface.co",
  "cdn-lfs.huggingface.co",
  "cdn-lfs.hf.co",
  "hf.co",
  // Official regional LFS endpoints.
  "cdn-lfs-us-1.hf.co",
  "cdn-lfs-eu-1.hf.co",
  // Official EU Xet transfer endpoint.
  "transfer.xethub-eu.hf.co",
]);

/** Dedicated HF-owned zones. Only one DNS label below each parent is allowed. */
const HF_REDIRECT_SINGLE_LABEL_FAMILIES = [
  "xethub.hf.co",
  "aws.cdn.hf.co",
  "gcp.cdn.hf.co",
] as const;
const HOST_LABEL_RE = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/;
const CONTROL_CHARACTER_RE = /[\u0000-\u001f\u007f-\u009f]/;
const INVALID_PERCENT_ESCAPE_RE = /%(?![0-9a-fA-F]{2})/;

class HfProxyFetchError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "HfProxyFetchError";
  }
}

function hasSafeDnsHostnameShape(hostname: string): boolean {
  if (
    !hostname ||
    hostname.length > 253 ||
    hostname.endsWith(".") ||
    hostname.includes("..")
  ) {
    return false;
  }
  return hostname.split(".").every((label) => HOST_LABEL_RE.test(label));
}

function normalizedHostname(url: URL): string {
  return url.hostname.toLowerCase();
}

function isCanonicalHuggingFaceOrigin(url: URL): boolean {
  return (
    url.protocol === "https:" &&
    normalizedHostname(url) === "huggingface.co" &&
    (url.port === "" || url.port === "443") &&
    url.origin === HF_CANONICAL_ORIGIN
  );
}

function isSingleLabelChildOf(hostname: string, parent: string): boolean {
  const suffix = `.${parent}`;
  if (!hostname.endsWith(suffix)) return false;
  const child = hostname.slice(0, -suffix.length);
  return Boolean(child) && !child.includes(".") && HOST_LABEL_RE.test(child);
}

function isAllowedHuggingFaceRedirectHost(hostname: string): boolean {
  if (HF_REDIRECT_EXACT_HOSTS.has(hostname)) return true;
  return HF_REDIRECT_SINGLE_LABEL_FAMILIES.some((parent) =>
    isSingleLabelChildOf(hostname, parent),
  );
}

function assertSafeHuggingFaceDestination(
  url: URL,
  options: { canonicalInitial: boolean },
): void {
  if (url.protocol !== "https:") {
    throw new HfProxyFetchError("HF redirect rejected: HTTPS is required");
  }
  if (url.username || url.password) {
    throw new HfProxyFetchError(
      "HF redirect rejected: URL credentials are not allowed",
    );
  }
  if (url.hash) {
    throw new HfProxyFetchError(
      "HF redirect rejected: URL fragments are not allowed",
    );
  }
  if (url.port && url.port !== "443") {
    throw new HfProxyFetchError(
      "HF redirect rejected: unexpected destination port",
    );
  }

  const hostname = normalizedHostname(url);
  const ipCandidate = hostname.replace(/^\[|\]$/g, "");
  if (isIP(ipCandidate) !== 0) {
    throw new HfProxyFetchError(
      "HF redirect rejected: IP destinations are not allowed",
    );
  }
  if (!hasSafeDnsHostnameShape(hostname)) {
    throw new HfProxyFetchError(
      "HF redirect rejected: invalid destination hostname",
    );
  }

  if (options.canonicalInitial) {
    if (!isCanonicalHuggingFaceOrigin(url)) {
      throw new HfProxyFetchError(
        "HF initial request rejected: canonical huggingface.co is required",
      );
    }
    return;
  }
  if (!isAllowedHuggingFaceRedirectHost(hostname)) {
    throw new HfProxyFetchError(
      "HF redirect rejected: destination host is not allowlisted",
    );
  }
}

function parseInitialUrl(rawUrl: string): URL {
  if (
    !rawUrl ||
    CONTROL_CHARACTER_RE.test(rawUrl) ||
    rawUrl.trim() !== rawUrl ||
    INVALID_PERCENT_ESCAPE_RE.test(rawUrl)
  ) {
    throw new HfProxyFetchError(
      "HF initial request rejected: malformed canonical URL",
    );
  }
  let currentUrl: URL;
  try {
    currentUrl = new URL(rawUrl);
  } catch {
    throw new HfProxyFetchError(
      "HF initial request rejected: malformed canonical URL",
    );
  }
  assertSafeHuggingFaceDestination(currentUrl, { canonicalInitial: true });
  return currentUrl;
}

function parseRedirectLocation(location: string, currentUrl: URL): URL {
  if (
    !location ||
    CONTROL_CHARACTER_RE.test(location) ||
    location.trim() !== location ||
    location.includes("#") ||
    INVALID_PERCENT_ESCAPE_RE.test(location)
  ) {
    throw new HfProxyFetchError(
      "HF redirect rejected: malformed Location header",
    );
  }

  let nextUrl: URL;
  try {
    nextUrl = new URL(location, currentUrl);
  } catch {
    throw new HfProxyFetchError(
      "HF redirect rejected: malformed Location header",
    );
  }
  assertSafeHuggingFaceDestination(nextUrl, { canonicalInitial: false });
  return nextUrl;
}

async function discardResponseBody(response: Response): Promise<void> {
  try {
    await response.body?.cancel();
  } catch {
    // Intermediate bodies are never exposed. Cleanup is best-effort.
  }
}

function authorizationHeaderForToken(token?: string): string | null {
  if (!token) return null;
  return new Headers(buildAuthHeaders(token)).get("authorization");
}

function buildHopHeaders(params: {
  range: string | null;
  authorization: string | null;
  allowAuthorization: boolean;
}): Headers {
  const headers = new Headers();
  if (params.range) headers.set("Range", params.range);
  if (params.allowAuthorization && params.authorization) {
    headers.set("Authorization", params.authorization);
  }
  return headers;
}

async function fetchHuggingFaceFileWithRedirects(params: {
  initialUrl: string;
  token?: string;
  range: string | null;
  signal: AbortSignal;
}): Promise<Response> {
  let currentUrl = parseInitialUrl(params.initialUrl);
  const authorization = authorizationHeaderForToken(params.token);
  let authorizationBoundaryCrossed = false;
  const visitedUrls = new Set<string>();

  // requestIndex 0 is the canonical request; at most five redirects follow.
  for (
    let requestIndex = 0;
    requestIndex <= MAX_REDIRECT_HOPS;
    requestIndex += 1
  ) {
    const currentIdentity = currentUrl.href;
    if (visitedUrls.has(currentIdentity)) {
      throw new HfProxyFetchError("HF redirect rejected: redirect loop");
    }
    visitedUrls.add(currentIdentity);

    const allowAuthorization =
      !authorizationBoundaryCrossed && isCanonicalHuggingFaceOrigin(currentUrl);

    const upstream = await fetch(currentUrl.toString(), {
      headers: buildHopHeaders({
        range: params.range,
        authorization,
        allowAuthorization,
      }),
      redirect: "manual",
      signal: params.signal,
    });

    const isRedirect = upstream.status >= 300 && upstream.status < 400;
    if (!isRedirect) return upstream;

    if (!FOLLOWABLE_REDIRECT_STATUSES.has(upstream.status)) {
      await discardResponseBody(upstream);
      throw new HfProxyFetchError(
        "HF redirect rejected: unsupported redirect status",
      );
    }
    if (requestIndex >= MAX_REDIRECT_HOPS) {
      await discardResponseBody(upstream);
      throw new HfProxyFetchError(
        "HF redirect rejected: redirect hop limit exceeded",
      );
    }

    let nextUrl: URL;
    try {
      nextUrl = parseRedirectLocation(
        upstream.headers.get("location") || "",
        currentUrl,
      );
    } catch (error) {
      await discardResponseBody(upstream);
      throw error;
    }
    if (visitedUrls.has(nextUrl.href)) {
      await discardResponseBody(upstream);
      throw new HfProxyFetchError("HF redirect rejected: redirect loop");
    }

    if (!isCanonicalHuggingFaceOrigin(nextUrl)) {
      authorizationBoundaryCrossed = true;
    }
    await discardResponseBody(upstream);
    currentUrl = nextUrl;
  }

  throw new HfProxyFetchError(
    "HF redirect rejected: redirect resolution failed",
  );
}

/**
 * HFファイルをサーバー側でフェッチしてストリーミング返却する。
 * - トークンをクライアントに露出させない
 * - Range ヘッダを通して動画・音声シークに対応
 * - text モードならテキストとして返す
 */
export async function GET(request: NextRequest) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json(
      { detail: "認証が必要です" },
      { status: 401, headers: PRIVATE_HEADERS },
    );
  }

  const sp = request.nextUrl.searchParams;
  const accountId = sp.get("accountId");
  const repoId = sp.get("repoId");
  const repoType = (sp.get("repoType") || "model") as RepoType;
  const path = sp.get("path");
  const revision = sp.get("revision") || "main";
  const mode = sp.get("mode"); // "text" | null

  if (!repoId || !path) {
    return NextResponse.json(
      { detail: "repoId, path は必須" },
      { status: 400, headers: PRIVATE_HEADERS },
    );
  }
  if (repoType !== "model" && repoType !== "dataset") {
    return NextResponse.json(
      { detail: "repoType 不正" },
      { status: 400, headers: PRIVATE_HEADERS },
    );
  }

  let resolved: Awaited<ReturnType<typeof resolveTokenForUser>> = null;
  try {
    resolved = accountId
      ? await resolveTokenForUser(String(user.id), accountId)
      : null;
  } catch {
    return NextResponse.json(
      { detail: "HFアカウントを解決できませんでした" },
      { status: 503, headers: PRIVATE_HEADERS },
    );
  }
  if (accountId && !resolved) {
    return NextResponse.json(
      { detail: "HFアカウントへのアクセス権がありません" },
      { status: 403, headers: PRIVATE_HEADERS },
    );
  }
  const token = resolved?.token;

  const range = request.headers.get("range");
  let upstream: Response;
  try {
    const url = buildFileUrl(repoId, path, repoType, revision);
    upstream = await fetchHuggingFaceFileWithRedirects({
      initialUrl: url,
      token,
      range,
      signal: request.signal,
    });
  } catch {
    return NextResponse.json(
      { detail: "HFフェッチ失敗" },
      { status: 502, headers: PRIVATE_HEADERS },
    );
  }

  if (mode === "text") {
    // テキストプレビュー用
    if (!upstream.ok) {
      // Do not reflect provider-controlled error bodies: approved CDN error
      // pages can contain signed URLs or other credentials.  Preserve the
      // upstream status while keeping those details out of the browser.
      await discardResponseBody(upstream);
      return NextResponse.json(
        { detail: `HF ${upstream.status}` },
        { status: upstream.status, headers: PRIVATE_HEADERS },
      );
    }
    // Read only a bounded prefix.  Calling arrayBuffer() first would allow a
    // malicious/large text response to consume unbounded server memory before
    // the 1 MB preview limit is applied.
    const contentLengthHeader = upstream.headers.get("content-length");
    const parsedLength =
      contentLengthHeader === null ? NaN : Number(contentLengthHeader);
    const declaredLength =
      Number.isFinite(parsedLength) && parsedLength >= 0 ? parsedLength : null;
    if (
      declaredLength !== null &&
      declaredLength > MAX_TEXT_BYTES &&
      !upstream.body
    ) {
      return NextResponse.json(
        { detail: "テキストプレビューが大きすぎます" },
        { status: 413, headers: PRIVATE_HEADERS },
      );
    }
    const chunks: Uint8Array[] = [];
    let total = 0;
    let truncated = declaredLength !== null && declaredLength > MAX_TEXT_BYTES;
    if (upstream.body) {
      const reader = upstream.body.getReader();
      try {
        while (!truncated || total < MAX_TEXT_BYTES) {
          const { done, value } = await reader.read();
          if (done) break;
          const remaining = MAX_TEXT_BYTES - total;
          if (value.byteLength > remaining) {
            if (remaining > 0) chunks.push(value.slice(0, remaining));
            total = MAX_TEXT_BYTES;
            truncated = true;
            break;
          }
          chunks.push(value);
          total += value.byteLength;
          // A response without Content-Length may be exactly the limit; read
          // one more chunk to distinguish exact from truncated content.
          if (total >= MAX_TEXT_BYTES) {
            if (declaredLength !== null && declaredLength > MAX_TEXT_BYTES) {
              truncated = true;
              break;
            }
            // Even an absent or lying Content-Length must not make us retain
            // an oversized response.  Probe one more chunk before deciding
            // whether the prefix is complete.
            const next = await reader.read();
            if (!next.done) truncated = true;
            break;
          }
        }
      } finally {
        if (truncated) await reader.cancel().catch(() => undefined);
        reader.releaseLock();
      }
    }
    const buf = new Uint8Array(total);
    let offset = 0;
    for (const chunk of chunks) {
      buf.set(chunk, offset);
      offset += chunk.byteLength;
    }
    const text = new TextDecoder("utf-8", { fatal: false }).decode(buf);
    return NextResponse.json(
      {
        success: true,
        text,
        truncated,
        // Report bytes actually read rather than trusting a missing or
        // malicious Content-Length.  This also keeps a null/empty response from
        // claiming bytes that were never delivered.
        size: total,
      },
      { headers: PRIVATE_HEADERS },
    );
  }

  // バイナリ / メディアストリーム
  const upstreamCT =
    upstream.headers.get("content-type") || inferContentType(path);
  const respHeaders: Record<string, string> = {
    "content-type": upstreamCT,
    // User-owned integrations must not be stored by a shared browser/proxy
    // cache.  Even public repository responses are scoped by the session.
    ...PRIVATE_HEADERS,
  };
  const cl = upstream.headers.get("content-length");
  const contentEncoding = upstream.headers.get("content-encoding");
  if (cl && !contentEncoding) respHeaders["content-length"] = cl;
  const ar = upstream.headers.get("accept-ranges");
  if (ar && !contentEncoding) respHeaders["accept-ranges"] = ar;
  const cr = upstream.headers.get("content-range");
  if (cr && !contentEncoding) respHeaders["content-range"] = cr;

  return new NextResponse(upstream.body, {
    status: upstream.status,
    headers: respHeaders,
  });
}

function inferContentType(path: string): string {
  const t = getMediaType(path);
  const ext = path.slice(path.lastIndexOf(".") + 1).toLowerCase();
  if (t === "image") {
    if (ext === "svg") return "image/svg+xml";
    if (ext === "jpg" || ext === "jpeg") return "image/jpeg";
    return `image/${ext}`;
  }
  if (t === "video") return `video/${ext === "mov" ? "quicktime" : ext}`;
  if (t === "audio") return `audio/${ext}`;
  if (t === "text") return "text/plain; charset=utf-8";
  return "application/octet-stream";
}
