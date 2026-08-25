import { NextRequest, NextResponse } from "next/server";
import { eq } from "drizzle-orm";
import { users } from "@/db/schema";
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

async function readLoginInput(request: NextRequest): Promise<
  | { username?: string | null; password?: string | null }
  | null
> {
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return null;
  }
  if (!body || typeof body !== "object" || Array.isArray(body)) return null;

  const { username, password } = body as Record<string, unknown>;
  if (
    (username != null && typeof username !== "string") ||
    (password != null && typeof password !== "string") ||
    (typeof username === "string" && username.length > MAX_USERNAME_LENGTH) ||
    (typeof password === "string" && password.length > MAX_PASSWORD_LENGTH)
  ) {
    return null;
  }
  return { username, password } as {
    username?: string | null;
    password?: string | null;
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

export async function POST(request: NextRequest) {
  const input = await readLoginInput(request);
  if (!input) {
    return NextResponse.json({ detail: "Invalid login input" }, { status: 400 });
  }
  const { username, password } = input;

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
    const [user] = await tx
      .select()
      .from(users)
      .where(eq(users.username, username))
      .limit(1);

    if (!user || !user.passwordHash) {
      const auditUnavailable = await recordFailedLoginAudit({
        username,
        request,
        failureReason: "invalid_credentials",
        executor: tx,
      });
      if (auditUnavailable) {
        return NextResponse.json(
          { detail: "Authentication audit logging is unavailable" },
          { status: 503 },
        );
      }
      return NextResponse.json(
        { detail: "Authentication failed" },
        { status: 401 },
      );
    }

    const valid = await verifyPassword(password, user.passwordHash);
    if (!valid) {
      const auditUnavailable = await recordFailedLoginAudit({
        username,
        request,
        failureReason: "invalid_credentials",
        executor: tx,
      });
      if (auditUnavailable) {
        return NextResponse.json(
          { detail: "Authentication audit logging is unavailable" },
          { status: 503 },
        );
      }
      return NextResponse.json(
        { detail: "Authentication failed" },
        { status: 401 },
      );
    }

    if (!user.isActive) {
      const auditUnavailable = await recordFailedLoginAudit({
        username: user.username,
        request,
        failureReason: "account_disabled",
        executor: tx,
      });
      if (auditUnavailable) {
        return NextResponse.json(
          { detail: "Authentication audit logging is unavailable" },
          { status: 503 },
        );
      }
      return NextResponse.json(
        { detail: "Account is inactive" },
        { status: 403 },
      );
    }

    const protocol = request.headers.get("x-forwarded-proto") || "http";
    const token = await createSessionToken(
      user.id,
      !!user.isPasswordResetRequired,
      user.sessionVersion ?? 1,
    );

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

    const response = NextResponse.json({
      authenticated: true,
      user: {
        username: user.username,
        role: user.role,
        display_name: user.displayName,
        password_reset_required: user.isPasswordResetRequired,
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
