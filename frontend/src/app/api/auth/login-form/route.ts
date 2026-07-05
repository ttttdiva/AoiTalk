import { NextRequest, NextResponse } from "next/server";
import { db } from "@/db";
import { users } from "@/db/schema";
import { eq } from "drizzle-orm";
import {
  attachSessionCookie,
  createSessionToken,
  verifyPassword,
} from "@/lib/auth";
import { recordWebUILoginLog } from "@/lib/server/login-log";

function safeLoginDestination(rawNext: FormDataEntryValue | null, baseUrl: string) {
  if (typeof rawNext !== "string" || !rawNext.trim()) return "/chat";
  try {
    const url = new URL(rawNext, baseUrl);
    const base = new URL(baseUrl);
    if (url.origin !== base.origin) return "/chat";
    if (url.pathname === "/login" || url.pathname.startsWith("/api/auth/")) return "/chat";
    return `${url.pathname}${url.search}${url.hash}`;
  } catch {
    return "/chat";
  }
}

export async function POST(request: NextRequest) {
  const formData = await request.formData();
  const username = formData.get("username") as string;
  const password = formData.get("password") as string;
  const next = formData.get("next");
  const host = request.headers.get("host") || "localhost:3002";
  const protocol = request.headers.get("x-forwarded-proto") || "http";
  const baseUrl = `${protocol}://${host}`;
  const loginUrl = new URL("/login", baseUrl);
  if (typeof next === "string" && next.trim()) loginUrl.searchParams.set("next", next);

  if (!username || !password) {
    await recordWebUILoginLog({
      username,
      action: "login",
      request,
      success: false,
      failureReason: "missing_credentials",
    });
    loginUrl.searchParams.set("error", "missing");
    return NextResponse.redirect(loginUrl, { status: 303 });
  }

  const [user] = await db
    .select()
    .from(users)
    .where(eq(users.username, username))
    .limit(1);

  if (!user || !user.passwordHash) {
    await recordWebUILoginLog({
      username,
      action: "login",
      request,
      success: false,
      failureReason: "invalid_credentials",
    });
    loginUrl.searchParams.set("error", "auth_failed");
    return NextResponse.redirect(loginUrl, { status: 303 });
  }

  const valid = await verifyPassword(password, user.passwordHash);
  if (!valid) {
    await recordWebUILoginLog({
      username,
      action: "login",
      request,
      success: false,
      failureReason: "invalid_credentials",
    });
    loginUrl.searchParams.set("error", "auth_failed");
    return NextResponse.redirect(loginUrl, { status: 303 });
  }

  if (!user.isActive) {
    await recordWebUILoginLog({
      username,
      action: "login",
      request,
      success: false,
      failureReason: "account_disabled",
    });
    loginUrl.searchParams.set("error", "inactive");
    return NextResponse.redirect(loginUrl, { status: 303 });
  }

  // Set-Cookie を確実にリダイレクトレスポンスに乗せるため、
  // cookies() API ではなく NextResponse.cookies.set() を使う
  const token = await createSessionToken(user.id);

  // last_login更新
  await db
    .update(users)
    .set({ lastLogin: new Date() })
    .where(eq(users.id, user.id));

  await recordWebUILoginLog({
    username: user.username,
    action: "login",
    request,
    success: true,
  });

  const destination = user.isPasswordResetRequired
    ? "/settings?password=required"
    : safeLoginDestination(next, baseUrl);
  const response = NextResponse.redirect(new URL(destination, baseUrl), {
    status: 303,
  });
  attachSessionCookie(response, token, protocol === "https");
  return response;
}
