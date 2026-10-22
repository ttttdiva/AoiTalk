import { randomUUID } from "node:crypto";
import { cookies, headers } from "next/headers";
import { NextResponse } from "next/server";
import { SignJWT, jwtVerify, type JWTPayload } from "jose";
import { eq } from "drizzle-orm";
import { db } from "@/db";
import { users } from "@/db/schema";
import { countCookieOccurrences } from "@/lib/cookie-guard";
import {
  buildSessionCookieOptions,
  clearCookieAtAllPaths,
  normalizeSessionCookieMaxAge,
  SESSION_COOKIE_NAME,
  setSessionCookie,
} from "@/lib/session-cookie";

export const SESSION_MAX_AGE_SECONDS = 60 * 60 * 24 * 7;

export type SessionSnapshot = {
  user: typeof users.$inferSelect;
  expiresAt: number;
  sessionVersion: number;
};

type VerifiedSession = {
  user: typeof users.$inferSelect;
  expiresAt: number | null;
  sessionVersion: number;
};

function getRequiredJwtSecret(): Uint8Array {
  const secret = process.env.NEXTAUTH_SECRET?.trim();
  if (!secret) {
    throw new Error("NEXTAUTH_SECRET is required for AoiTalk session signing");
  }
  return new TextEncoder().encode(secret);
}

const SECRET = getRequiredJwtSecret();
const FASTAPI_COOKIE_NAME =
  process.env.AOITALK_FASTAPI_SESSION_COOKIE?.trim() ||
  "aoitalk_fastapi_session";

export async function createSessionToken(
  userId: string,
  passwordResetRequired = false,
  sessionVersion = 1,
  options?: { expiresAt?: number },
): Promise<string> {
  const now = Math.floor(Date.now() / 1000);
  let expiresAt = now + SESSION_MAX_AGE_SECONDS;
  if (options?.expiresAt !== undefined) {
    if (!Number.isFinite(options.expiresAt)) {
      throw new Error("Session expiration must be finite");
    }
    expiresAt = Math.trunc(options.expiresAt);
    if (expiresAt <= now) {
      throw new Error("Session expiration must be in the future");
    }
    expiresAt = Math.min(expiresAt, now + SESSION_MAX_AGE_SECONDS);
  }
  return await new SignJWT({
    sub: userId,
    password_reset_required: passwordResetRequired,
    session_version: Math.max(1, Number(sessionVersion) || 1),
  })
    .setProtectedHeader({ alg: "HS256" })
    .setIssuedAt()
    .setJti(randomUUID())
    .setExpirationTime(expiresAt)
    .sign(SECRET);
}

export async function createPasswordResetToken(
  userId: string,
  sessionVersion: number,
): Promise<string> {
  return await new SignJWT({
    sub: userId,
    purpose: "password_reset",
    session_version: Math.max(1, Math.trunc(sessionVersion)),
  })
    .setProtectedHeader({ alg: "HS256" })
    .setIssuedAt()
    .setJti(randomUUID())
    .setExpirationTime("24h")
    .sign(SECRET);
}

export async function verifyPasswordResetToken(
  token: string,
): Promise<{ userId: string; sessionVersion: number } | null> {
  try {
    const { payload } = await jwtVerify(token, SECRET);
    const sessionVersion = payload.session_version;
    if (
      payload.purpose !== "password_reset" ||
      !payload.sub ||
      typeof sessionVersion !== "number" ||
      !Number.isInteger(sessionVersion) ||
      sessionVersion < 1
    ) {
      return null;
    }
    return { userId: String(payload.sub), sessionVersion };
  } catch {
    return null;
  }
}

export function attachSessionCookie(
  response: NextResponse,
  token: string,
  secure: boolean,
  options?: { maxAge?: number },
) {
  setSessionCookie(response, token, secure, {
    ...(options?.maxAge === undefined
      ? {}
      : { maxAge: normalizeSessionCookieMaxAge(options.maxAge) }),
  });
}

export function clearSessionCookie(response: NextResponse, secure: boolean) {
  clearCookieAtAllPaths(response, SESSION_COOKIE_NAME, secure);
}

export function clearFastAPISessionCookie(
  response: NextResponse,
  secure: boolean,
) {
  if (FASTAPI_COOKIE_NAME === SESSION_COOKIE_NAME) return;
  clearCookieAtAllPaths(response, FASTAPI_COOKIE_NAME, secure);
}

// Legacy helper for call sites still using cookies() directly.
export async function createSession(userId: string, secure: boolean = false) {
  const token = await createSessionToken(userId);
  const cookieStore = await cookies();
  cookieStore.set(
    SESSION_COOKIE_NAME,
    token,
    buildSessionCookieOptions(secure),
  );
  return token;
}

export async function getSession(options?: {
  allowPasswordReset?: boolean;
  throwOnDatabaseError?: boolean;
}) {
  const session = await readVerifiedSession(options, false);
  return session?.user ?? null;
}

/**
 * Return the verified user together with the signed session expiry and
 * version.  The JWT, cookie, database activity, password-reset, and session
 * version checks intentionally remain identical to getSession().
 */
export async function getSessionSnapshot(options?: {
  allowPasswordReset?: boolean;
  throwOnDatabaseError?: boolean;
}): Promise<SessionSnapshot | null> {
  const session = await readVerifiedSession(options, true);
  return session as SessionSnapshot | null;
}

async function readVerifiedSession(
  options: {
    allowPasswordReset?: boolean;
    throwOnDatabaseError?: boolean;
  } | undefined,
  requireExpiresAt: boolean,
): Promise<VerifiedSession | null> {
  const requestHeaders = await headers();
  if (requestHeaders.has("authorization")) return null;
  if (
    countCookieOccurrences(requestHeaders.get("cookie"), SESSION_COOKIE_NAME) !==
    1
  ) {
    return null;
  }
  const cookieStore = await cookies();
  const sessionCookies = cookieStore.getAll(SESSION_COOKIE_NAME);
  if (sessionCookies.length !== 1) return null;
  const token = sessionCookies[0]?.value;
  if (!token) return null;

  let payload: JWTPayload;
  try {
    ({ payload } = await jwtVerify(token, SECRET));
  } catch {
    return null;
  }
  const userId = payload.sub;
  if (!userId) return null;

  const expiresAt = payload.exp;
  const verifiedExpiresAt =
    typeof expiresAt === "number" &&
    Number.isInteger(expiresAt) &&
    expiresAt > 0
      ? expiresAt
      : null;
  if (
    requireExpiresAt &&
    verifiedExpiresAt === null
  ) {
    return null;
  }

  if (requireExpiresAt) {
    // Password-reset JWTs are a separate purpose and must never be accepted
    // as browser sessions during an origin handoff.  Session snapshots also
    // require the explicit boolean claim written by createSessionToken().
    if (payload.purpose !== undefined || typeof payload.password_reset_required !== "boolean") {
      return null;
    }
    if (payload.password_reset_required && !options?.allowPasswordReset) {
      return null;
    }
  }

  let user: (typeof users.$inferSelect) | undefined;
  try {
    [user] = await db
      .select()
      .from(users)
      .where(eq(users.id, userId))
      .limit(1);
  } catch (error) {
    if (options?.throwOnDatabaseError) throw error;
    return null;
  }
  if (!user || !user.isActive) return null;
  const tokenVersion = Number(payload.session_version ?? 1);
  const userVersion = Number(user.sessionVersion ?? 1);
  if (
    !Number.isInteger(tokenVersion) ||
    !Number.isInteger(userVersion) ||
    tokenVersion !== userVersion
  ) {
    return null;
  }
  if (user.isPasswordResetRequired && !options?.allowPasswordReset) return null;
  return { user, expiresAt: verifiedExpiresAt, sessionVersion: userVersion };
}

export async function clearSession() {
  const cookieStore = await cookies();
  cookieStore.set(SESSION_COOKIE_NAME, "", {
    ...buildSessionCookieOptions(false),
    maxAge: 0,
  });
}
