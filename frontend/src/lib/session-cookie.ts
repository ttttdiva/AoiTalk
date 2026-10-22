import type { NextResponse } from "next/server";
import { publicCookiePath } from "@/lib/base-path";

export const DEFAULT_SESSION_COOKIE_NAME = "aoitalk_session";
export const SESSION_COOKIE_MAX_AGE = 60 * 60 * 24 * 7;
export const LEGACY_SESSION_COOKIE_PATH = "/";

const COOKIE_NAME_RE = /^[A-Za-z0-9_-]+$/u;

export function resolveSessionCookieName(
  value: string | null | undefined,
): string {
  const normalized = value?.trim();
  if (!normalized) return DEFAULT_SESSION_COOKIE_NAME;
  if (!COOKIE_NAME_RE.test(normalized)) {
    throw new Error(
      "AOITALK_SESSION_COOKIE_NAME contains invalid cookie-name characters",
    );
  }
  return normalized;
}

export const SESSION_COOKIE_NAME = resolveSessionCookieName(
  process.env.AOITALK_SESSION_COOKIE_NAME,
);

/**
 * Keep a caller supplied lifetime within the same bound as the signed
 * session.  A cookie with a longer lifetime than the JWT can leave stale
 * credentials around and a negative lifetime has no useful meaning for an
 * issued session cookie.
 */
export function normalizeSessionCookieMaxAge(maxAge: number): number {
  if (!Number.isFinite(maxAge)) {
    throw new Error("Session cookie maxAge must be finite");
  }
  const normalized = Math.trunc(maxAge);
  if (normalized < 0) {
    throw new Error("Session cookie maxAge must not be negative");
  }
  return Math.min(normalized, SESSION_COOKIE_MAX_AGE);
}

export function sessionCookiePaths(currentPath = publicCookiePath()): string[] {
  return currentPath === LEGACY_SESSION_COOKIE_PATH
    ? [LEGACY_SESSION_COOKIE_PATH]
    : [currentPath, LEGACY_SESSION_COOKIE_PATH];
}

export function buildSessionCookieOptions(
  secure: boolean,
  maxAge = SESSION_COOKIE_MAX_AGE,
  path = publicCookiePath(),
) {
  return {
    httpOnly: true,
    secure,
    sameSite: "lax" as const,
    maxAge: normalizeSessionCookieMaxAge(maxAge),
    path,
  };
}

function appendExpiredCookie(
  response: NextResponse,
  cookieName: string,
  path: string,
  secure: boolean,
): void {
  if (!COOKIE_NAME_RE.test(cookieName)) {
    throw new Error("Cookie name contains invalid characters");
  }
  response.headers.append(
    "Set-Cookie",
    `${cookieName}=; Path=${path}; Max-Age=0; HttpOnly; SameSite=Lax${
      secure ? "; Secure" : ""
    }`,
  );
}

export function setSessionCookie(
  response: NextResponse,
  token: string,
  secure: boolean,
  options?: { maxAge?: number },
): void {
  response.cookies.set(
    SESSION_COOKIE_NAME,
    token,
    buildSessionCookieOptions(secure, options?.maxAge ?? SESSION_COOKIE_MAX_AGE),
  );
  if (publicCookiePath() !== LEGACY_SESSION_COOKIE_PATH) {
    appendExpiredCookie(
      response,
      SESSION_COOKIE_NAME,
      LEGACY_SESSION_COOKIE_PATH,
      secure,
    );
  }
}

export function clearCookieAtAllPaths(
  response: NextResponse,
  cookieName: string,
  secure: boolean,
): void {
  const currentPath = publicCookiePath();
  // Use raw fields for both paths.  Calling response.cookies.set() after a
  // previous append makes Next reserialize its internal cookie map and can
  // discard an already appended legacy-path deletion when logout clears more
  // than one cookie name.
  for (const path of sessionCookiePaths(currentPath)) {
    appendExpiredCookie(
      response,
      cookieName,
      path,
      secure,
    );
  }
}
