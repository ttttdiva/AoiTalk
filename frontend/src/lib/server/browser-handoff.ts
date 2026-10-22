import { NextResponse, type NextRequest } from "next/server";
import { countCookieOccurrences } from "@/lib/cookie-guard";
import {
  canonicalRequestPathname,
  gatewayEntryProofHeadersPresent,
  verifyGatewayEntryProof,
  type GatewayEntry,
} from "@/lib/server/gateway-entry-proof";

export const BROWSER_HANDOFF_VERSION = "v1";
export const BROWSER_HANDOFF_TTL_SECONDS = 60;
export const SOURCE_ORIGIN = "https://az-vnetspace.com";
export const DESTINATION_ORIGIN = "https://nk-h01.f5.si";
// The gateway contract is fixed at /at.  Keeping this independent of a
// caller-controlled build setting makes a mismatched Next basePath fail closed
// at the proof check rather than silently widening the handoff surface.
export const HANDOFF_BASE_PATH = "/at";

export const HANDOFF_START_PATH = `${HANDOFF_BASE_PATH}/api/auth/handoff/start`;
export const HANDOFF_AUTHORIZE_PATH = `${HANDOFF_BASE_PATH}/api/auth/handoff/authorize`;
export const HANDOFF_CALLBACK_PATH = `${HANDOFF_BASE_PATH}/api/auth/handoff/callback`;

/** Each transaction gets its own host-only cookie so two tabs cannot clobber one another. */
export const HANDOFF_BINDING_COOKIE_PREFIX = "__Secure-aoitalk_handoff_binding_";

const SAFE_STATE_RE = /^[A-Za-z0-9_-]{43}$/u;
const MAX_RETURN_PATH_LENGTH = 2048;
const FORBIDDEN_RETURN_PREFIXES = [
  "/login",
  "/reset-password",
  "/change-password",
  "/api/",
  "/_next/",
  "/favicon.ico",
];

export const AUTH_NO_STORE_HEADERS = {
  "cache-control": "no-store, no-cache, must-revalidate, proxy-revalidate",
  pragma: "no-cache",
  expires: "0",
  "referrer-policy": "no-referrer",
  "x-content-type-options": "nosniff",
};

export function handoffBindingCookieName(state: string): string | null {
  return SAFE_STATE_RE.test(state)
    ? `${HANDOFF_BINDING_COOKIE_PREFIX}${state}`
    : null;
}

export function isValidHandoffValue(value: string): boolean {
  return SAFE_STATE_RE.test(value);
}

export function fixedHandoffPath(path: string): string {
  if (!path.startsWith("/")) return `${HANDOFF_BASE_PATH}/${path}`;
  return path === HANDOFF_BASE_PATH || path.startsWith(`${HANDOFF_BASE_PATH}/`)
    ? path
    : `${HANDOFF_BASE_PATH}${path}`;
}

export function verifyHandoffGateway(
  request: NextRequest,
  entry: GatewayEntry,
  method: string,
  pathname: string,
): boolean {
  return verifyGatewayEntryProof(request, {
    entry,
    method,
    pathname,
  });
}

/** Return an asset-free authentication failure. */
export function handoffForbidden(status = 403): NextResponse {
  return new NextResponse(null, {
    status,
    headers: AUTH_NO_STORE_HEADERS,
  });
}

export function handoffMethodNotAllowed(): NextResponse {
  return new NextResponse(null, {
    status: 405,
    headers: {
      ...AUTH_NO_STORE_HEADERS,
      allow: "GET",
    },
  });
}

export function handoffUnsupportedMethod(
  request: NextRequest,
  entry: GatewayEntry,
  pathname: string,
): NextResponse {
  if (
    !verifyGatewayEntryProof(request, {
      entry,
      method: request.method,
      pathname,
    })
  ) {
    return handoffForbidden();
  }
  return handoffMethodNotAllowed();
}

export function handoffRedirect(location: string): NextResponse {
  const resolvedLocation =
    location.startsWith("/") && !location.startsWith("//")
      ? new URL(location, DESTINATION_ORIGIN).toString()
      : location;
  return new NextResponse(null, {
    status: 303,
    headers: {
      ...AUTH_NO_STORE_HEADERS,
      location: resolvedLocation,
    },
  });
}

/** Add the same cache/referrer policy to every authentication response. */
export function authResponse<T extends NextResponse>(response: T): T {
  for (const [name, value] of Object.entries(AUTH_NO_STORE_HEADERS)) {
    response.headers.set(name, value);
  }
  return response;
}

/**
 * Login/reset endpoints remain usable on the LAN, where no gateway marker is
 * present.  Any marked request is handled as a gateway browser request and
 * is denied, including malformed marker sets, so a failed proof cannot fall
 * through to an HTML login surface.
 */
export function denyGatewayAuthFallback(request: NextRequest): NextResponse | null {
  if (!gatewayEntryProofHeadersPresent(request.headers)) return null;
  // The source gateway is the normal AoiTalk login surface.  Only a valid
  // destination proof turns login/reset into a screenless denial; malformed
  // marker sets still fail closed.
  try {
    const pathname = canonicalRequestPathname(request);
    if (!pathname) return handoffForbidden();
    const source = verifyGatewayEntryProof(request, {
      entry: "source",
      method: request.method,
      pathname,
    });
    if (source) return null;
    return handoffForbidden();
  } catch {
    return handoffForbidden();
  }
}

/**
 * Accept only an app-relative destination.  The caller may provide either a
 * base-path URL (`/at/chat`) or an app path (`/chat`); it is normalized to the
 * configured public base path before storage.
 */
export function safeHandoffReturnPath(raw: string | null | undefined): string {
  if (typeof raw !== "string" || !raw || raw.length > MAX_RETURN_PATH_LENGTH) {
    return `${HANDOFF_BASE_PATH}/chat`;
  }
  const trimmed = raw.trim();
  if (!trimmed || !trimmed.startsWith("/") || trimmed.startsWith("//")) {
    return `${HANDOFF_BASE_PATH}/chat`;
  }
  try {
    const parsed = new URL(trimmed, DESTINATION_ORIGIN);
    if (parsed.origin !== DESTINATION_ORIGIN || parsed.pathname.startsWith("//")) {
      return `${HANDOFF_BASE_PATH}/chat`;
    }
    const appPath =
      parsed.pathname === HANDOFF_BASE_PATH
        ? "/"
        : parsed.pathname.startsWith(`${HANDOFF_BASE_PATH}/`)
          ? parsed.pathname.slice(HANDOFF_BASE_PATH.length) || "/"
          : parsed.pathname;
    if (
      !appPath.startsWith("/") ||
      appPath.startsWith("//") ||
      FORBIDDEN_RETURN_PREFIXES.some(
        (prefix) => appPath === prefix || appPath.startsWith(prefix),
      )
    ) {
      return `${HANDOFF_BASE_PATH}/chat`;
    }
    const normalized = `${
      appPath === "/" ? HANDOFF_BASE_PATH : `${HANDOFF_BASE_PATH}${appPath}`
    }${parsed.search}`;
    return normalized.length <= MAX_RETURN_PATH_LENGTH
      ? normalized
      : `${HANDOFF_BASE_PATH}/chat`;
  } catch {
    return `${HANDOFF_BASE_PATH}/chat`;
  }
}

export function readSingleQueryValue(
  request: NextRequest,
  key: string,
): string | null {
  const values = request.nextUrl.searchParams.getAll(key);
  return values.length === 1 ? values[0] ?? null : null;
}

export function readSingleCookie(
  request: NextRequest,
  cookieName: string,
): string | null {
  if (countCookieOccurrences(request.headers.get("cookie"), cookieName) !== 1) {
    return null;
  }
  const values = request.cookies.getAll(cookieName);
  return values.length === 1 && values[0]?.value ? values[0].value : null;
}

export function setBindingCookie(
  response: NextResponse,
  state: string,
  binding: string,
): boolean {
  const name = handoffBindingCookieName(state);
  if (!name || !isValidHandoffValue(binding)) return false;
  response.cookies.set({
    name,
    value: binding,
    httpOnly: true,
    secure: true,
    sameSite: "lax",
    path: HANDOFF_CALLBACK_PATH,
    maxAge: BROWSER_HANDOFF_TTL_SECONDS,
  });
  return true;
}

export function clearBindingCookie(
  response: NextResponse,
  state: string,
): void {
  const name = handoffBindingCookieName(state);
  if (!name) return;
  response.cookies.set({
    name,
    value: "",
    httpOnly: true,
    secure: true,
    sameSite: "lax",
    path: HANDOFF_CALLBACK_PATH,
    maxAge: 0,
  });
}

export function sourceAuthorizeLocation(state: string): string {
  const url = new URL(HANDOFF_AUTHORIZE_PATH, SOURCE_ORIGIN);
  url.searchParams.set("state", state);
  return url.toString();
}

export function destinationCallbackLocation(code: string, state: string): string {
  const url = new URL(HANDOFF_CALLBACK_PATH, DESTINATION_ORIGIN);
  // Worker callback rewriting is deliberately exact: only these two keys,
  // in this order, are allowed to cross the public gateway boundary.
  url.search = `?code=${encodeURIComponent(code)}&state=${encodeURIComponent(state)}`;
  return url.toString();
}

