import { NextRequest, NextResponse } from "next/server";
import { eq } from "drizzle-orm";
import { users } from "@/db/schema";
import { attachSessionCookie, createSessionToken } from "@/lib/auth";
import {
  authFailureReason,
  authenticatePasswordViaPython,
  CanonicalAuthError,
  isUnavailableAuthError,
  normalizeCredentialSource,
} from "@/lib/server/password-auth";
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
    if (url.origin !== base.origin || url.pathname.startsWith("//")) return "/chat";
    if (url.pathname === "/login" || url.pathname.startsWith("/api/auth/")) return "/chat";
    return `${url.pathname}${url.search}${url.hash}`;
  } catch {
    return "/chat";
  }
}

// Preserve the browser's origin when a trusted proxy rewrites the Host header.
function redirectWithinSite(url: URL): NextResponse {
  return new NextResponse(null, {
    status: 303,
    headers: { Location: `${url.pathname}${url.search}${url.hash}` },
  });
}

function redirectForAuthFailure(
  loginUrl: URL,
  error: unknown,
): NextResponse {
  if (error instanceof CanonicalAuthError && error.code === "account_disabled") {
    loginUrl.searchParams.set("error", "inactive");
  } else if (error instanceof CanonicalAuthError && error.code === "ad_provisioning_conflict") {
    loginUrl.searchParams.set("error", "auth_conflict");
  } else if (isUnavailableAuthError(error)) {
    loginUrl.searchParams.set("error", "auth_unavailable");
  } else {
    loginUrl.searchParams.set("error", "auth_failed");
  }
  return redirectWithinSite(loginUrl);
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
  const credentialSourceEntry = formData.get("credential_source");
  const next = formData.get("next");
  if (
    (usernameEntry != null && typeof usernameEntry !== "string") ||
    (passwordEntry != null && typeof passwordEntry !== "string") ||
    (credentialSourceEntry != null && typeof credentialSourceEntry !== "string") ||
    (next != null && typeof next !== "string") ||
    (typeof usernameEntry === "string" && usernameEntry.length > MAX_USERNAME_LENGTH) ||
    (typeof passwordEntry === "string" && passwordEntry.length > MAX_PASSWORD_LENGTH) ||
    (typeof credentialSourceEntry === "string" &&
      normalizeCredentialSource(credentialSourceEntry) === null) ||
    (typeof next === "string" && next.length > MAX_NEXT_LENGTH)
  ) {
    return NextResponse.json({ detail: "Invalid login input" }, { status: 400 });
  }
  const username = usernameEntry ?? "";
  const password = passwordEntry ?? "";
  const credentialSource =
    typeof credentialSourceEntry === "string"
      ? normalizeCredentialSource(credentialSourceEntry) ?? undefined
      : undefined;
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
      return redirectWithinSite(loginUrl);
    }
    loginUrl.searchParams.set("error", "missing");
    return redirectWithinSite(loginUrl);
  }

  const guarded = await withLoginThrottle(request, username, async (tx) => {
    let authenticated: Awaited<ReturnType<typeof authenticatePasswordViaPython>>;
    try {
      authenticated = await authenticatePasswordViaPython(request, {
        username,
        password,
        credentialSource,
      });
    } catch (error) {
      if (await failedLoginAuditUnavailable({
        username,
        request,
        failureReason: authFailureReason(error),
        executor: tx,
      })) {
        loginUrl.searchParams.set("error", "audit_unavailable");
        return redirectWithinSite(loginUrl);
      }
      return redirectForAuthFailure(loginUrl, error);
    }

    const user = authenticated.user;
    const auditRecorded = await recordWebUILoginLog({
      username: user.username,
      action: "login",
      request,
      success: true,
      executor: tx,
    });
    if (!auditRecorded && isEnterpriseProfile()) {
      loginUrl.searchParams.set("error", "audit_unavailable");
      return redirectWithinSite(loginUrl);
    }

    await tx
      .update(users)
      .set({ lastLogin: new Date() })
      .where(eq(users.id, user.id));

    // Set-Cookie を確実にリダイレクトレスポンスに乗せるため、
    // cookies() API ではなく NextResponse.cookies.set() を使う
    const token = await createSessionToken(
      user.id,
      user.password_reset_required,
      user.session_version,
    );

    // A user with an initial password must not enter the regular app layout yet.
    const destination = user.password_reset_required
      ? "/change-password"
      : safeLoginDestination(next, baseUrl);
    const response = redirectWithinSite(new URL(destination, baseUrl));
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
