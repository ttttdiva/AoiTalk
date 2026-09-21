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
  type CredentialSource,
} from "@/lib/server/password-auth";
import {
  isEnterpriseProfile,
  recordWebUILoginLog,
} from "@/lib/server/login-log";
import { withLoginThrottle } from "@/lib/server/login-throttle";
import type { LoginThrottleTransaction } from "@/lib/server/login-throttle";

const MAX_USERNAME_LENGTH = 255;
const MAX_PASSWORD_LENGTH = 1024;

async function readLoginInput(request: NextRequest): Promise<
  | {
      username?: string | null;
      password?: string | null;
      credential_source?: CredentialSource;
    }
  | null
> {
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return null;
  }
  if (!body || typeof body !== "object" || Array.isArray(body)) return null;

  const { username, password, credential_source } = body as Record<string, unknown>;
  if (
    (username != null && typeof username !== "string") ||
    (password != null && typeof password !== "string") ||
    (credential_source != null && normalizeCredentialSource(credential_source) === null) ||
    (typeof username === "string" && username.length > MAX_USERNAME_LENGTH) ||
    (typeof password === "string" && password.length > MAX_PASSWORD_LENGTH)
  ) {
    return null;
  }
  return {
    username,
    password,
    ...(credential_source != null
      ? { credential_source: normalizeCredentialSource(credential_source)! }
      : {}),
  };
}

async function recordFailedLoginAudit({
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

function failureResponse(error: unknown): NextResponse {
  if (error instanceof CanonicalAuthError) {
    if (error.code === "account_disabled") {
      return NextResponse.json({ detail: "Account is inactive" }, { status: 403 });
    }
    if (
      error.code === "credential_source_required" ||
      error.code === "unsupported_credential_source"
    ) {
      return NextResponse.json({ detail: "Invalid login input" }, { status: 400 });
    }
    if (error.code === "ad_provisioning_conflict") {
      return NextResponse.json(
        { detail: "Authentication account provisioning conflict" },
        { status: 409 },
      );
    }
    if (isUnavailableAuthError(error)) {
      return NextResponse.json(
        { detail: "Authentication service is unavailable" },
        { status: 503 },
      );
    }
  }
  return NextResponse.json({ detail: "Authentication failed" }, { status: 401 });
}

export async function POST(request: NextRequest) {
  const input = await readLoginInput(request);
  if (!input) {
    return NextResponse.json({ detail: "Invalid login input" }, { status: 400 });
  }
  const { username, password, credential_source: credentialSource } = input;

  if (!username || !password) {
    const auditUnavailable = await recordFailedLoginAudit({
      username,
      request,
      failureReason: "missing_credentials",
    });
    if (auditUnavailable) {
      return NextResponse.json(
        { detail: "Authentication audit logging is unavailable" },
        { status: 503 },
      );
    }
    return NextResponse.json(
      { detail: "Username and password are required" },
      { status: 400 },
    );
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
      const auditUnavailable = await recordFailedLoginAudit({
        username,
        request,
        failureReason: authFailureReason(error),
        executor: tx,
      });
      if (auditUnavailable) {
        return NextResponse.json(
          { detail: "Authentication audit logging is unavailable" },
          { status: 503 },
        );
      }
      return failureResponse(error);
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
      return NextResponse.json(
        { detail: "Authentication audit logging is unavailable" },
        { status: 503 },
      );
    }

    await tx
      .update(users)
      .set({ lastLogin: new Date() })
      .where(eq(users.id, user.id));

    const protocol = request.headers.get("x-forwarded-proto") || "http";
    const token = await createSessionToken(
      user.id,
      user.password_reset_required,
      user.session_version,
    );
    const response = NextResponse.json({
      authenticated: true,
      user: {
        id: user.id,
        username: user.username,
        role: user.role,
        display_name: user.display_name,
        password_reset_required: user.password_reset_required,
        auth_source: user.auth_source,
      },
    });
    attachSessionCookie(response, token, protocol === "https");
    return response;
  });

  if (!guarded.throttled) return guarded.value;
  const auditUnavailable = await recordFailedLoginAudit({
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
