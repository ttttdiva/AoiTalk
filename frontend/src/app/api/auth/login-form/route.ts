import { NextRequest, NextResponse } from "next/server";
import { db } from "@/db";
import { users } from "@/db/schema";
import { eq } from "drizzle-orm";
import {
  attachSessionCookie,
  createSessionToken,
  verifyPassword,
} from "@/lib/auth";

export async function POST(request: NextRequest) {
  const formData = await request.formData();
  const username = formData.get("username") as string;
  const password = formData.get("password") as string;
  const host = request.headers.get("host") || "localhost:3002";
  const protocol = request.headers.get("x-forwarded-proto") || "http";
  const baseUrl = `${protocol}://${host}`;
  const loginUrl = new URL("/login", baseUrl);

  if (!username || !password) {
    loginUrl.searchParams.set("error", "missing");
    return NextResponse.redirect(loginUrl);
  }

  const [user] = await db
    .select()
    .from(users)
    .where(eq(users.username, username))
    .limit(1);

  if (!user || !user.passwordHash) {
    loginUrl.searchParams.set("error", "auth_failed");
    return NextResponse.redirect(loginUrl);
  }

  const valid = await verifyPassword(password, user.passwordHash);
  if (!valid) {
    loginUrl.searchParams.set("error", "auth_failed");
    return NextResponse.redirect(loginUrl);
  }

  if (!user.isActive) {
    loginUrl.searchParams.set("error", "inactive");
    return NextResponse.redirect(loginUrl);
  }

  // Set-Cookie を確実にリダイレクトレスポンスに乗せるため、
  // cookies() API ではなく NextResponse.cookies.set() を使う
  const token = await createSessionToken(user.id);

  // last_login更新
  await db
    .update(users)
    .set({ lastLogin: new Date() })
    .where(eq(users.id, user.id));

  const destination = user.isPasswordResetRequired
    ? "/settings?password=required"
    : "/chat";
  const response = NextResponse.redirect(new URL(destination, baseUrl));
  attachSessionCookie(response, token, protocol === "https");
  return response;
}
