import { randomUUID } from "node:crypto";
import { cookies } from "next/headers";
import { NextResponse } from "next/server";
import { SignJWT, jwtVerify } from "jose";
import { eq } from "drizzle-orm";
import { db } from "@/db";
import { users } from "@/db/schema";

function getRequiredJwtSecret(): Uint8Array {
  const secret = process.env.NEXTAUTH_SECRET?.trim();
  if (!secret) {
    throw new Error("NEXTAUTH_SECRET is required for AoiTalk session signing");
  }
  return new TextEncoder().encode(secret);
}

const SECRET = getRequiredJwtSecret();
const COOKIE_NAME = "aoitalk_session";
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

export async function createSessionToken(userId: string): Promise<string> {
  return await new SignJWT({ sub: userId })
    .setProtectedHeader({ alg: "HS256" })
    .setIssuedAt()
    .setJti(randomUUID())
    .setExpirationTime("7d")
    .sign(SECRET);
}

export async function createPasswordResetToken(
  userId: string,
): Promise<string> {
  return await new SignJWT({ sub: userId, purpose: "password_reset" })
    .setProtectedHeader({ alg: "HS256" })
    .setIssuedAt()
    .setJti(randomUUID())
    .setExpirationTime("24h")
    .sign(SECRET);
}

export async function verifyPasswordResetToken(
  token: string,
): Promise<string | null> {
  try {
    const { payload } = await jwtVerify(token, SECRET);
    if (payload.purpose !== "password_reset" || !payload.sub) return null;
    return String(payload.sub);
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

// Legacy helper for call sites still using cookies() directly.
export async function createSession(userId: string, secure: boolean = false) {
  const token = await createSessionToken(userId);
  const cookieStore = await cookies();
  cookieStore.set(COOKIE_NAME, token, buildSessionCookieOptions(secure));
  return token;
}

export async function getSession() {
  const cookieStore = await cookies();
  const token = cookieStore.get(COOKIE_NAME)?.value;
  if (!token) return null;

  try {
    const { payload } = await jwtVerify(token, SECRET);
    const userId = payload.sub;
    if (!userId) return null;

    const [user] = await db
      .select()
      .from(users)
      .where(eq(users.id, userId))
      .limit(1);
    if (!user || !user.isActive) return null;
    return user;
  } catch {
    return null;
  }
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
