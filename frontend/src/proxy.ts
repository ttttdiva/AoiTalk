import { jwtVerify } from "jose";
import type { JWTPayload } from "jose";
import { NextRequest, NextResponse } from "next/server";
import { eq } from "drizzle-orm";
import { db } from "@/db";
import { users } from "@/db/schema";
import { countCookieOccurrences } from "@/lib/cookie-guard";

const PUBLIC_PATHS = [
  "/login",
  "/reset-password",
  "/api/auth/login",
  "/api/auth/login-form",
  "/api/auth/reset-password",
  "/api/auth/status",
];
const COOKIE_NAME = "aoitalk_session";
const CHANGE_PASSWORD_PATH = "/change-password";
const STATIC_PATHS = [
  "/_next/static",
  "/_next/image",
  "/favicon.ico",
  "/aoitalk-notifications-sw.js",
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

function getRequiredJwtSecret(): Uint8Array {
  const secret = process.env.NEXTAUTH_SECRET?.trim();
  if (!secret) {
    throw new Error("NEXTAUTH_SECRET is required for AoiTalk session signing");
  }
  return new TextEncoder().encode(secret);
}

const SECRET = getRequiredJwtSecret();

function redirectToLogin(request: NextRequest) {
  const loginUrl = new URL("/login", request.url);
  const nextPath = `${request.nextUrl.pathname}${request.nextUrl.search}`;
  if (nextPath !== "/login") loginUrl.searchParams.set("next", nextPath);
  return NextResponse.redirect(loginUrl);
}

function rejectInvalidSession(request: NextRequest) {
  const response = redirectToLogin(request);
  response.cookies.set(COOKIE_NAME, "", {
    httpOnly: true,
    secure: request.nextUrl.protocol === "https:",
    sameSite: "lax",
    maxAge: 0,
    path: "/",
  });
  return response;
}

export async function proxy(request: NextRequest) {
  const { pathname } = request.nextUrl;

  // 公開パス、API、明示した静的ファイルはスキップ
  if (
    isPublicPath(pathname) ||
    pathname.startsWith("/api/") ||
    isStaticAssetPath(pathname)
  ) {
    return NextResponse.next();
  }

  // セッションCookie確認。FastAPI の旧Cookieなど、Next.js JWTではない値は通さない。
  const sessionCookies = request.cookies.getAll(COOKIE_NAME);
  if (
    request.headers.has("authorization") ||
    countCookieOccurrences(request.headers.get("cookie"), COOKIE_NAME) !== 1 ||
    sessionCookies.length !== 1
  ) {
    return redirectToLogin(request);
  }
  const session = sessionCookies[0];

  let payload: JWTPayload;
  try {
    ({ payload } = await jwtVerify(session.value, SECRET));
  } catch {
    return rejectInvalidSession(request);
  }
  if (!payload.sub) return rejectInvalidSession(request);

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
    return NextResponse.redirect(new URL(CHANGE_PASSWORD_PATH, request.url));
  }

  return NextResponse.next();
}

export const config = {
  // API routes perform their own authentication. Excluding them at matcher
  // level also prevents Next.js Proxy from cloning/buffering upload bodies.
  matcher: ["/((?!api(?:/|$)|_next/static|_next/image|favicon.ico).*)"],
};
