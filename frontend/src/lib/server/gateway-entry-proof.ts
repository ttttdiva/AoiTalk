import { createHmac, timingSafeEqual } from "node:crypto";

/**
 * The gateway installs these headers only after validating its own route and
 * activation boundary.  They are a transport proof, not a browser supplied
 * origin hint; callers must fail closed when the proof is missing or invalid.
 */
export const GATEWAY_ENTRY_HEADER = "x-aoitalk-gateway-entry";
export const GATEWAY_TIMESTAMP_HEADER = "x-aoitalk-gateway-timestamp";
export const GATEWAY_SIGNATURE_HEADER = "x-aoitalk-gateway-signature";
export const GATEWAY_ENTRY_PROOF_VERSION = "aoitalk-browser-handoff-v1";
export const GATEWAY_ENTRY_PROOF_MAX_SKEW_SECONDS = 30;
export const GATEWAY_FIXED_BASE_PATH = "/at";

export type GatewayEntry = "source" | "destination";

export type GatewayEntryProofOptions = {
  entry: GatewayEntry;
  /** Expected method for this fixed route. */
  method: string;
  /** Expected pathname, including the public `/at` base path when configured. */
  pathname: string;
  /** Unix seconds used by tests; production callers should omit it. */
  now?: number;
};

function readSingleHeader(headers: Headers, name: string): string | null {
  const value = headers.get(name);
  // Fetch Headers combines repeated fields with a comma.  No valid marker
  // value contains a comma, so this also rejects duplicate spoof attempts.
  if (!value || value !== value.trim() || value.includes(",")) return null;
  return value;
}

type ParsedRequestTarget = {
  pathname: string;
  pathAndQuery: string;
  absolute: boolean;
};

function hasCompatibleConfiguredBasePath(): boolean {
  const rawConfigured =
    process.env.AOITALK_PUBLIC_BASE_PATH?.trim() ||
    process.env.NEXT_PUBLIC_BASE_PATH?.trim() ||
    "";
  if (!rawConfigured) return true;
  return rawConfigured.replace(/\/+$/u, "") === GATEWAY_FIXED_BASE_PATH;
}

function canonicalizePathname(pathname: string): string | null {
  if (!hasCompatibleConfiguredBasePath()) return null;
  if (!pathname.startsWith("/") || pathname.startsWith("//") || pathname.includes("\\")) {
    return null;
  }
  if (
    pathname === GATEWAY_FIXED_BASE_PATH ||
    pathname.startsWith(`${GATEWAY_FIXED_BASE_PATH}/`)
  ) {
    return pathname;
  }
  return `${GATEWAY_FIXED_BASE_PATH}${pathname}`;
}

function canonicalizeSearch(search: string): string {
  const params = new URLSearchParams(search.startsWith("?") ? search.slice(1) : search);
  // Next removes only this transport parameter before handing the request to
  // the route. URLSearchParams preserves business-pair order and duplicates
  // while normalizing equivalent percent/space encodings on both sides.
  params.delete("_rsc");
  const serialized = params.toString();
  return serialized ? `?${serialized}` : "";
}

function parseRequestTarget(raw: string): ParsedRequestTarget | null {
  if (
    !raw ||
    raw.includes("\\") ||
    raw.includes("#") ||
    /[\u0000-\u0020\u007f]/u.test(raw)
  ) {
    return null;
  }

  let target = raw;
  let absolute = false;
  if (!raw.startsWith("/")) {
    // Next adapters normally expose an absolute URL, but some route/proxy
    // paths expose only an origin-relative target.  Accept only http(s)
    // absolute URLs and never use their host for authorization.
    if (!/^https?:\/\//iu.test(raw)) return null;
    absolute = true;
    const authorityStart = raw.indexOf("://") + 3;
    const delimiter = raw.slice(authorityStart).search(/[/?#]/u);
    const authorityEnd = delimiter < 0 ? raw.length : authorityStart + delimiter;
    const authority = raw.slice(authorityStart, authorityEnd);
    if (!authority || authority.includes("@")) return null;
    try {
      const parsed = new URL(raw);
      if (
        (parsed.protocol !== "http:" && parsed.protocol !== "https:") ||
        parsed.username ||
        parsed.password ||
        !parsed.hostname
      ) {
        return null;
      }
    } catch {
      return null;
    }
    target = raw.slice(authorityEnd) || "/";
    if (target.startsWith("?")) target = `/${target}`;
  }

  if (!target.startsWith("/") || target.startsWith("//")) return null;
  const queryIndex = target.indexOf("?");
  const pathname = target.slice(0, queryIndex < 0 ? target.length : queryIndex);
  const search = queryIndex < 0 ? "" : target.slice(queryIndex);
  const canonicalPathname = canonicalizePathname(pathname);
  if (!canonicalPathname) return null;
  return {
    pathname: canonicalPathname,
    pathAndQuery: `${canonicalPathname}${canonicalizeSearch(search)}`,
    absolute,
  };
}

export function normalizedRequestTarget(request: Request): ParsedRequestTarget | null {
  return parseRequestTarget(request.url);
}

export function canonicalRequestPathname(request: Request): string | null {
  return normalizedRequestTarget(request)?.pathname ?? null;
}

export function requestUrlIsAbsolute(request: Request): boolean {
  return normalizedRequestTarget(request)?.absolute === true;
}

export function normalizeGatewayPathname(pathname: string): string | null {
  return canonicalizePathname(pathname);
}

export function gatewayEntryProofHeadersPresent(headers: Headers): boolean {
  return [
    GATEWAY_ENTRY_HEADER,
    GATEWAY_TIMESTAMP_HEADER,
    GATEWAY_SIGNATURE_HEADER,
  ].some((name) => headers.has(name));
}

/** Preserve the encoded path and query used by the gateway signature. */
export function rawPathAndQuery(request: Request): string | null {
  return normalizedRequestTarget(request)?.pathAndQuery ?? null;
}

function decodeBase64Url(value: string): Buffer | null {
  if (!/^[A-Za-z0-9_-]{43}$/u.test(value)) return null;
  try {
    const decoded = Buffer.from(value.replace(/-/gu, "+").replace(/_/gu, "/"), "base64");
    return decoded.length === 32 ? decoded : null;
  } catch {
    return null;
  }
}

/**
 * Verify the gateway's HMAC proof over the exact request path/query and
 * method.  The expected pathname is checked independently so a valid marker
 * copied from another handoff endpoint cannot authorize this route.
 */
export function verifyGatewayEntryProof(
  request: Request,
  options: GatewayEntryProofOptions,
): boolean {
  const secret = process.env.GATEWAY_SUPERVISOR_ACTIVATION_SECRET?.trim();
  if (!secret) return false;

  const entry = readSingleHeader(request.headers, GATEWAY_ENTRY_HEADER);
  const timestampValue = readSingleHeader(request.headers, GATEWAY_TIMESTAMP_HEADER);
  const signatureValue = readSingleHeader(request.headers, GATEWAY_SIGNATURE_HEADER);
  if (entry !== options.entry || !timestampValue || !signatureValue) return false;

  const method = request.method.toUpperCase();
  const expectedMethod = options.method.toUpperCase();
  if (method !== expectedMethod) return false;

  const normalizedTarget = normalizedRequestTarget(request);
  if (!normalizedTarget) return false;
  const rawPath = normalizedTarget.pathAndQuery;
  const expectedPath = normalizeGatewayPathname(options.pathname);
  if (!expectedPath || normalizedTarget.pathname !== expectedPath) return false;

  // Require a canonical, integer Unix-seconds representation.  This avoids
  // accepting alternate spellings which could be signed by an intermediary.
  if (!/^\d{1,16}$/u.test(timestampValue)) return false;
  const timestamp = Number(timestampValue);
  if (!Number.isSafeInteger(timestamp)) return false;
  const now = options.now ?? Math.floor(Date.now() / 1000);
  if (!Number.isSafeInteger(now) || Math.abs(now - timestamp) > GATEWAY_ENTRY_PROOF_MAX_SKEW_SECONDS) {
    return false;
  }

  const supplied = decodeBase64Url(signatureValue);
  if (!supplied) return false;
  const payload = `${GATEWAY_ENTRY_PROOF_VERSION}\n${entry}\n${timestampValue}\n${method}\n${rawPath}`;
  const expected = createHmac("sha256", secret).update(payload, "utf8").digest();
  return supplied.length === expected.length && timingSafeEqual(supplied, expected);
}

