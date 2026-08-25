import { randomUUID } from "node:crypto";
import { cookies, headers } from "next/headers";
import { NextResponse } from "next/server";
import { SignJWT, jwtVerify, type JWTPayload } from "jose";
import { eq } from "drizzle-orm";
import { db } from "@/db";
import { users } from "@/db/schema";
import { countCookieOccurrences } from "@/lib/cookie-guard";

function getRequiredJwtSecret(): Uint8Array {
  const secret = process.env.NEXTAUTH_SECRET?.trim();
  if (!secret) {
    throw new Error("NEXTAUTH_SECRET is required for AoiTalk session signing");
  }
  return new TextEncoder().encode(secret);
}

const SECRET = getRequiredJwtSecret();
const COOKIE_NAME = "aoitalk_session";
const FASTAPI_COOKIE_NAME =
  process.env.AOITALK_FASTAPI_SESSION_COOKIE?.trim() ||
  "aoitalk_fastapi_session";
const COOKIE_MAX_AGE = 60 * 60 * 24 * 7;

function buildSessionCookieOptions(secure: boolean) {
  return {
    httpOnly: true,
    secure,
    sameSite: "lax" as const,
    maxAge: COOKIE_MAX_AGE,
    path: "/",
  };
}

export async function createSessionToken(
  userId: string,
  passwordResetRequired = false,
  sessionVersion = 1,
): Promise<string> {
  return await new SignJWT({
    sub: userId,
    password_reset_required: passwordResetRequired,
    session_version: Math.max(1, Number(sessionVersion) || 1),
  })
    .setProtectedHeader({ alg: "HS256" })
    .setIssuedAt()
    .setJti(randomUUID())
    .setExpirationTime("7d")
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
) {
  response.cookies.set(COOKIE_NAME, token, buildSessionCookieOptions(secure));
}

export function clearSessionCookie(response: NextResponse, secure: boolean) {
  response.cookies.set(COOKIE_NAME, "", {
    ...buildSessionCookieOptions(secure),
    maxAge: 0,
  });
}

export function clearFastAPISessionCookie(
  response: NextResponse,
  secure: boolean,
) {
  if (FASTAPI_COOKIE_NAME === COOKIE_NAME) return;
  response.cookies.set(FASTAPI_COOKIE_NAME, "", {
    ...buildSessionCookieOptions(secure),
    maxAge: 0,
  });
}

// Legacy helper for call sites still using cookies() directly.
export async function createSession(userId: string, secure: boolean = false) {
  const token = await createSessionToken(userId);
  const cookieStore = await cookies();
  cookieStore.set(COOKIE_NAME, token, buildSessionCookieOptions(secure));
  return token;
}

export async function getSession(options?: {
  allowPasswordReset?: boolean;
  throwOnDatabaseError?: boolean;
}) {
  const requestHeaders = await headers();
  if (requestHeaders.has("authorization")) return null;
  if (countCookieOccurrences(requestHeaders.get("cookie"), COOKIE_NAME) !== 1) {
    return null;
  }
  const cookieStore = await cookies();
  const sessionCookies = cookieStore.getAll(COOKIE_NAME);
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
  return user;
}

export async function clearSession() {
  const cookieStore = await cookies();
  cookieStore.set(COOKIE_NAME, "", {
    ...buildSessionCookieOptions(false),
    maxAge: 0,
  });
}

export async function verifyPassword(
  password: string,
  hash: string,
): Promise<boolean> {
  const bcrypt = await import("bcryptjs");
  return bcrypt.compare(password, hash);
}
