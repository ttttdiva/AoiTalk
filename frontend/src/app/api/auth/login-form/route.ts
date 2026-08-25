import { NextRequest, NextResponse } from "next/server";
import { users } from "@/db/schema";
import { eq } from "drizzle-orm";
import {
  attachSessionCookie,
  createSessionToken,
  verifyPassword,
} from "@/lib/auth";
import {
  isEnterpriseProfile,
  recordWebUILoginLog,
} from "@/lib/server/login-log";
import { withLoginThrottle } from "@/lib/server/login-throttle";
import type { LoginThrottleTransaction } from "@/lib/server/login-throttle";

const MAX_USERNAME_LENGTH = 255;
const MAX_PASSWORD_LENGTH = 1024;
const MAX_NEXT_LENGTH = 2048;

async function failedLoginAuditUnavailable({
  username,
  request,
  failureReason,
  executor,
}: {
  username: string | null | undefined;
  request: NextRequest;
  failureReason: string;
  executor?: LoginThrottleTransaction;
}): Promise<boolean> {
  const recorded = await recordWebUILoginLog({
    username,
    action: "login",
    request,
    success: false,
    failureReason,
    executor,
  });
  return !recorded && isEnterpriseProfile();
}

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
  let formData: FormData;
  try {
    formData = await request.formData();
  } catch {
    return NextResponse.json({ detail: "Invalid login input" }, { status: 400 });
  }
  const usernameEntry = formData.get("username");
  const passwordEntry = formData.get("password");
  const next = formData.get("next");
  if (
    (usernameEntry != null && typeof usernameEntry !== "string") ||
    (passwordEntry != null && typeof passwordEntry !== "string") ||
    (next != null && typeof next !== "string") ||
    (typeof usernameEntry === "string" &&
      usernameEntry.length > MAX_USERNAME_LENGTH) ||
    (typeof passwordEntry === "string" &&
      passwordEntry.length > MAX_PASSWORD_LENGTH) ||
    (typeof next === "string" && next.length > MAX_NEXT_LENGTH)
  ) {
    return NextResponse.json({ detail: "Invalid login input" }, { status: 400 });
  }
  const username = usernameEntry ?? "";
  const password = passwordEntry ?? "";
  const host = request.headers.get("host") || "localhost:3002";
  const protocol = request.headers.get("x-forwarded-proto") || "http";
  const baseUrl = `${protocol}://${host}`;
  const loginUrl = new URL("/login", baseUrl);
  if (typeof next === "string" && next.trim()) loginUrl.searchParams.set("next", next);

  if (!username || !password) {
    if (await failedLoginAuditUnavailable({
      username,
      request,
      failureReason: "missing_credentials",
    })) loginUrl.searchParams.set("error", "audit_unavailable");
    if (loginUrl.searchParams.get("error") === "audit_unavailable") {
      return NextResponse.redirect(loginUrl, { status: 303 });
    }
    loginUrl.searchParams.set("error", "missing");
    return NextResponse.redirect(loginUrl, { status: 303 });
  }

  const guarded = await withLoginThrottle(request, username, async (tx) => {
    const [user] = await tx
      .select()
      .from(users)
      .where(eq(users.username, username))
      .limit(1);

    if (!user || !user.passwordHash) {
      if (await failedLoginAuditUnavailable({
        username,
        request,
        failureReason: "invalid_credentials",
        executor: tx,
      })) loginUrl.searchParams.set("error", "audit_unavailable");
      if (loginUrl.searchParams.get("error") === "audit_unavailable") {
        return NextResponse.redirect(loginUrl, { status: 303 });
      }
      loginUrl.searchParams.set("error", "auth_failed");
      return NextResponse.redirect(loginUrl, { status: 303 });
    }

    const valid = await verifyPassword(password, user.passwordHash);
    if (!valid) {
      if (await failedLoginAuditUnavailable({
        username,
        request,
        failureReason: "invalid_credentials",
        executor: tx,
      })) loginUrl.searchParams.set("error", "audit_unavailable");
      if (loginUrl.searchParams.get("error") === "audit_unavailable") {
        return NextResponse.redirect(loginUrl, { status: 303 });
      }
      loginUrl.searchParams.set("error", "auth_failed");
      return NextResponse.redirect(loginUrl, { status: 303 });
    }

    if (!user.isActive) {
      if (await failedLoginAuditUnavailable({
        username: user.username,
        request,
        failureReason: "account_disabled",
        executor: tx,
      })) loginUrl.searchParams.set("error", "audit_unavailable");
      if (loginUrl.searchParams.get("error") === "audit_unavailable") {
        return NextResponse.redirect(loginUrl, { status: 303 });
      }
      loginUrl.searchParams.set("error", "inactive");
      return NextResponse.redirect(loginUrl, { status: 303 });
    }

    // Set-Cookie を確実にリダイレクトレスポンスに乗せるため、
    // cookies() API ではなく NextResponse.cookies.set() を使う
    const token = await createSessionToken(
      user.id,
      !!user.isPasswordResetRequired,
      user.sessionVersion ?? 1,
    );

    // last_login更新
    const auditRecorded = await recordWebUILoginLog({
      username: user.username,
      action: "login",
      request,
      success: true,
      executor: tx,
    });
    if (!auditRecorded && isEnterpriseProfile()) {
      loginUrl.searchParams.set("error", "audit_unavailable");
      return NextResponse.redirect(loginUrl, { status: 303 });
    }

    await tx
      .update(users)
      .set({ lastLogin: new Date() })
      .where(eq(users.id, user.id));

    // A user with an initial password must not enter the regular app layout yet:
    // the sidebar/providers mounted there immediately call protected APIs and
    // turn the reset-required session into an apparent logout.  Keep this
    // redirect on the minimal auth route until the password change endpoint has
    // issued a fresh, non-reset session token.
    const destination = user.isPasswordResetRequired
      ? "/change-password"
      : safeLoginDestination(next, baseUrl);
    const response = NextResponse.redirect(new URL(destination, baseUrl), {
      status: 303,
    });
    attachSessionCookie(response, token, protocol === "https");
    return response;
  });

  if (!guarded.throttled) return guarded.value;
  const auditUnavailable = await failedLoginAuditUnavailable({
    username,
    request,
    failureReason: "rate_limited",
  });
  if (auditUnavailable) {
    return NextResponse.json(
      { detail: "Authentication audit logging is unavailable" },
      { status: 503 },
    );
  }
  return NextResponse.json(
    { detail: "Too many login attempts. Try again shortly." },
    {
      status: 429,
      headers: { "Retry-After": String(guarded.retryAfter) },
    },
  );
}
