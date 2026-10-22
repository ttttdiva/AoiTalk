import { jwtVerify } from "jose";
import type { JWTPayload } from "jose";
import { NextRequest, NextResponse } from "next/server";
import { eq } from "drizzle-orm";
import { db } from "@/db";
import { users } from "@/db/schema";
import { countCookieOccurrences } from "@/lib/cookie-guard";
import { withBasePath, withoutBasePath } from "@/lib/base-path";
import {
  clearCookieAtAllPaths,
  SESSION_COOKIE_NAME,
} from "@/lib/session-cookie";
import {
  HANDOFF_BASE_PATH,
  HANDOFF_START_PATH,
  DESTINATION_ORIGIN,
  SOURCE_ORIGIN,
  handoffForbidden,
  handoffRedirect,
  safeHandoffReturnPath,
} from "@/lib/server/browser-handoff";
import {
  canonicalRequestPathname,
  gatewayEntryProofHeadersPresent,
  requestUrlIsAbsolute,
  verifyGatewayEntryProof,
} from "@/lib/server/gateway-entry-proof";

const PUBLIC_PATHS = [
  "/login",
  "/reset-password",
  "/api/auth/login",
  "/api/auth/login-form",
  "/api/auth/reset-password",
  "/api/auth/status",
];
const CHANGE_PASSWORD_PATH = "/change-password";
const STATIC_PATHS = [
  "/_next/static",
  "/_next/image",
  "/favicon.ico",
  "/notifications-sw.js",
];
const STATIC_PATH_PREFIXES = [
  "/_next/static/",
  "/_next/image/",
  "/images/ui/",
];

function isStaticAssetPath(pathname: string): boolean {
  return (
    STATIC_PATHS.includes(pathname) ||
    STATIC_PATH_PREFIXES.some((prefix) => pathname.startsWith(prefix))
  );
}

function isPublicPath(pathname: string): boolean {
  return PUBLIC_PATHS.some(
    (path) => pathname === path || pathname.startsWith(`${path}/`),
  );
}

function isChangePasswordPath(pathname: string): boolean {
  return (
    pathname === CHANGE_PASSWORD_PATH ||
    pathname.startsWith(`${CHANGE_PASSWORD_PATH}/`)
  );
}

function isDestinationAuthPath(pathname: string): boolean {
  const appPath = withoutBasePath(pathname);
  return (
    appPath === "/login" ||
    appPath.startsWith("/login/") ||
    appPath === "/reset-password" ||
    appPath.startsWith("/reset-password/")
  );
}

function requestPathname(request: NextRequest): string | null {
  return canonicalRequestPathname(request);
}

function hasValidDestinationProof(request: NextRequest): boolean {
  const pathname = requestPathname(request);
  return (
    pathname !== null &&
    verifyGatewayEntryProof(request, {
      entry: "destination",
      method: request.method,
      pathname,
    })
  );
}

function hasValidSourceProof(request: NextRequest): boolean {
  const pathname = requestPathname(request);
  return (
    pathname !== null &&
    verifyGatewayEntryProof(request, {
      entry: "source",
      method: request.method,
      pathname,
    })
  );
}

function destinationStartRedirect(request: NextRequest): NextResponse {
  const rawNext = `${withoutBasePath(request.nextUrl.pathname)}${request.nextUrl.search}`;
  const start = new URL(HANDOFF_START_PATH, DESTINATION_ORIGIN);
  start.searchParams.set("next", safeHandoffReturnPath(rawNext));
  return handoffRedirect(start.toString());
}

function isWebSocketPath(pathname: string): boolean {
  const websocketPath = `${HANDOFF_BASE_PATH}/ws`;
  return (
    pathname === "/ws" ||
    pathname.startsWith("/ws/") ||
    pathname === websocketPath ||
    pathname.startsWith(`${websocketPath}/`)
  );
}

function isRscOrPrefetchRequest(request: NextRequest): boolean {
  const purpose = request.headers.get("purpose")?.trim().toLowerCase();
  const secPurpose = request.headers.get("sec-purpose")?.trim().toLowerCase();
  if (purpose === "prefetch" || secPurpose === "prefetch") return true;
  if (
    request.headers.has("rsc") ||
    request.headers.has("next-router-prefetch") ||
    request.headers.has("next-router-state-tree") ||
    request.headers.has("x-middleware-prefetch") ||
    request.nextUrl.searchParams.has("_rsc")
  ) {
    return true;
  }
  const accept = request.headers.get("accept")?.toLowerCase() ?? "";
  return accept.includes("text/x-component");
}

function isAutomaticHandoffNavigation(
  request: NextRequest,
  appPathname: string,
): boolean {
  if (
    request.method !== "GET" ||
    request.headers.has("upgrade") ||
    isWebSocketPath(appPathname) ||
    isRscOrPrefetchRequest(request)
  ) {
    return false;
  }

  // Modern browsers identify a top-level document navigation.  Once any
  // Fetch Metadata header is present, require the complete navigate/document
  // pair so component fetches and prefetches cannot trigger cross-site auth.
  const fetchMode = request.headers.get("sec-fetch-mode")?.trim().toLowerCase();
  const fetchDest = request.headers.get("sec-fetch-dest")?.trim().toLowerCase();
  const hasFetchMetadata = [
    "sec-fetch-mode",
    "sec-fetch-dest",
    "sec-fetch-site",
    "sec-fetch-user",
  ].some((name) => request.headers.has(name));
  if (hasFetchMetadata) {
    return fetchMode === "navigate" && fetchDest === "document";
  }

  // Keep compatibility with bare GET probes and older clients that omit
  // Fetch Metadata entirely.  Explicit RSC/prefetch signals were rejected
  // above even when those clients do not send Sec-Fetch headers.
  return true;
}

function getRequiredJwtSecret(): Uint8Array {
  const secret = process.env.NEXTAUTH_SECRET?.trim();
  if (!secret) {
    throw new Error("NEXTAUTH_SECRET is required for AoiTalk session signing");
  }
  return new TextEncoder().encode(secret);
}

const SECRET = getRequiredJwtSecret();

function redirectToLogin(request: NextRequest) {
  const nextPath = `${withoutBasePath(request.nextUrl.pathname)}${request.nextUrl.search}`;
  const loginPath = withBasePath("/login");
  const loginUrl = new URL(loginPath, "https://aoitalk.invalid");
  if (nextPath !== "/login") loginUrl.searchParams.set("next", nextPath);
  const location = `${loginUrl.pathname}${loginUrl.search}`;

  // A verified source proof identifies the fixed public source origin.  A
  // normal absolute LAN request keeps its own origin; a relative adapter
  // request uses a relative Location so no synthetic host is exposed.
  if (hasValidSourceProof(request)) {
    return NextResponse.redirect(new URL(location, SOURCE_ORIGIN));
  }
  if (requestUrlIsAbsolute(request)) {
    try {
      return NextResponse.redirect(new URL(location, request.url));
    } catch {
      // Fall through to the relative response below.
    }
  }
  return new NextResponse(null, {
    status: 307,
    headers: { location },
  });
}

function rejectInvalidSession(request: NextRequest) {
  const response = redirectToLogin(request);
  clearCookieAtAllPaths(
    response,
    SESSION_COOKIE_NAME,
    request.nextUrl.protocol === "https:",
  );
  return response;
}

export async function proxy(request: NextRequest) {
  const pathname = withoutBasePath(request.nextUrl.pathname);
  const hasGatewayMarkers = gatewayEntryProofHeadersPresent(request.headers);
  const destinationProof = hasValidDestinationProof(request);
  const sourceProof = hasValidSourceProof(request);

  // A gateway-marked browser must never fall back to the DDNS login/reset
  // page.  Invalid marker sets fail closed as well; requests without markers
  // retain the existing LAN login behavior.
  if (
    isDestinationAuthPath(request.nextUrl.pathname) &&
    hasGatewayMarkers &&
    (!sourceProof || destinationProof)
  ) {
    return handoffForbidden();
  }

  // A marker on a normal protected page is still an authenticated transport
  // boundary.  Reject a malformed/spoofed marker instead of redirecting it to
  // /login, which would expose the DDNS login UI through a fallback path.
  if (hasGatewayMarkers && !destinationProof && !sourceProof) {
    return handoffForbidden();
  }

  // This asset-free relay clears OAuth query before its authenticated POST.
  // Redirecting it to login would copy the one-time code into a second URL.
  if (pathname === "/operations/media/oauth/callback") return NextResponse.next();

  // 公開パス、API、明示した静的ファイルはスキップ
  if (
    isPublicPath(pathname) ||
    pathname.startsWith("/api/") ||
    isStaticAssetPath(pathname)
  ) {
    return NextResponse.next();
  }

  // セッションCookie確認。FastAPI の旧Cookieなど、Next.js JWTではない値は通さない。
  const sessionCookies = request.cookies.getAll(SESSION_COOKIE_NAME);
  if (
    request.headers.has("authorization") ||
    countCookieOccurrences(request.headers.get("cookie"), SESSION_COOKIE_NAME) !==
      1 ||
    sessionCookies.length !== 1
  ) {
    if (destinationProof) {
      const hasNoSessionCookie =
        countCookieOccurrences(
          request.headers.get("cookie"),
          SESSION_COOKIE_NAME,
        ) === 0 && sessionCookies.length === 0;
      // Only a first, cookie-less top-level GET starts the handoff.  Duplicate,
      // stale, or otherwise malformed credentials terminate with 403.
      if (
        hasNoSessionCookie &&
        !request.headers.has("authorization") &&
        isAutomaticHandoffNavigation(request, pathname)
      ) {
        return destinationStartRedirect(request);
      }
      return handoffForbidden();
    }
    return redirectToLogin(request);
  }
  const session = sessionCookies[0];

  let payload: JWTPayload;
  try {
    ({ payload } = await jwtVerify(session.value, SECRET));
  } catch {
    if (destinationProof) return handoffForbidden();
    return rejectInvalidSession(request);
  }
  if (!payload.sub) {
    if (destinationProof) return handoffForbidden();
    return rejectInvalidSession(request);
  }

  let user:
    | {
        isActive: boolean | null;
        isPasswordResetRequired: boolean | null;
        sessionVersion: number;
      }
    | undefined;
  try {
    [user] = await db
      .select({
        isActive: users.isActive,
        isPasswordResetRequired: users.isPasswordResetRequired,
        sessionVersion: users.sessionVersion,
      })
      .from(users)
      .where(eq(users.id, String(payload.sub)))
      .limit(1);
  } catch (error) {
    if (destinationProof) return handoffForbidden();
    console.error("Failed to validate WebUI session against the database", error);
    return NextResponse.json(
      { detail: "Authentication service is temporarily unavailable" },
      { status: 503 },
    );
  }

  const tokenVersion = Number(payload.session_version ?? 1);
  const userVersion = Number(user?.sessionVersion ?? 1);
  if (
    !user ||
    user.isActive !== true ||
    !Number.isInteger(tokenVersion) ||
    !Number.isInteger(userVersion) ||
    tokenVersion !== userVersion
  ) {
    if (destinationProof) return handoffForbidden();
    return rejectInvalidSession(request);
  }
  if (
    (payload.password_reset_required === true ||
      user.isPasswordResetRequired === true) &&
    !isChangePasswordPath(pathname)
  ) {
    // Keep reset-required sessions on the minimal auth page.  Entering the
    // normal app (including /settings) would mount providers/sidebar that
    // call protected APIs and make the session look signed out.
    if (destinationProof) return handoffForbidden();
    const changePasswordPath = withBasePath(CHANGE_PASSWORD_PATH);
    if (hasValidSourceProof(request)) {
      return NextResponse.redirect(new URL(changePasswordPath, SOURCE_ORIGIN));
    }
    if (requestUrlIsAbsolute(request)) {
      try {
        return NextResponse.redirect(new URL(changePasswordPath, request.url));
      } catch {
        // Fall through to a same-origin relative Location for adapters that
        // expose an invalid/relative request target.
      }
    }
    return new NextResponse(null, {
      status: 307,
      headers: { location: changePasswordPath },
    });
  }

  return NextResponse.next();
}

export const config = {
  // API routes perform their own authentication. Excluding them at matcher
  // level also prevents Next.js Proxy from cloning/buffering upload bodies.
  matcher: ["/((?!api(?:/|$)|_next/static|_next/image|favicon.ico).*)"],
};
