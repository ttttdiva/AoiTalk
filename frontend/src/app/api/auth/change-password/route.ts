import { NextRequest, NextResponse } from "next/server";
import {
  attachSessionCookie,
  createSessionToken,
  getSession,
} from "@/lib/auth";
import { proxyRequestToPythonApi } from "@/lib/server/python-api-proxy";

const MAX_PASSWORD_LENGTH = 1024;

async function readChangePasswordInput(request: NextRequest): Promise<
  | { current_password?: string | null; new_password?: string | null }
  | null
> {
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return null;
  }
  if (!body || typeof body !== "object" || Array.isArray(body)) return null;

  const { current_password, new_password } = body as Record<string, unknown>;
  if (
    (current_password != null && typeof current_password !== "string") ||
    (new_password != null && typeof new_password !== "string") ||
    (typeof current_password === "string" &&
      current_password.length > MAX_PASSWORD_LENGTH) ||
    (typeof new_password === "string" &&
      new_password.length > MAX_PASSWORD_LENGTH)
  ) {
    return null;
  }
  return { current_password, new_password } as {
    current_password?: string | null;
    new_password?: string | null;
  };
}

function isSameOriginMutation(request: NextRequest): boolean {
  const host = request.headers.get("x-forwarded-host") || request.headers.get("host");
  if (!host) return false;
  const forwardedProto = request.headers.get("x-forwarded-proto")?.split(",")[0]?.trim();
  const protocol = forwardedProto || request.nextUrl.protocol.replace(":", "");
  const expectedOrigin = `${protocol}://${host}`;
  const origin = request.headers.get("origin")?.trim();
  if (origin) return origin === expectedOrigin;
  const referer = request.headers.get("referer")?.trim();
  if (!referer) return false;
  try {
    return new URL(referer).origin === expectedOrigin;
  } catch {
    return false;
  }
}

export async function POST(request: NextRequest) {
  if (!isSameOriginMutation(request)) {
    return NextResponse.json({ detail: "同一オリジンからの操作が必要です" }, { status: 403 });
  }
  let user: Awaited<ReturnType<typeof getSession>>;
  try {
    user = await getSession({
      allowPasswordReset: true,
      throwOnDatabaseError: true,
    });
  } catch (error) {
    console.error("Failed to resolve WebUI session during password change", error);
    return NextResponse.json(
      { detail: "認証サービスを一時的に利用できません" },
      { status: 503 },
    );
  }
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const input = await readChangePasswordInput(request);
  if (!input) {
    return NextResponse.json({ detail: "入力が不正です" }, { status: 400 });
  }
  const { current_password, new_password } = input;

  if (!new_password || new_password.length < 6) {
    return NextResponse.json(
      { detail: "新しいパスワードは6文字以上必要です" },
      { status: 400 },
    );
  }

  if (!user.isPasswordResetRequired && !current_password) {
    return NextResponse.json(
      { detail: "現在のパスワードが必要です" },
      { status: 400 },
    );
  }

  // Password verification, hashing, row locking, and session invalidation are
  // owned by the FastAPI repository.  Keeping this BFF as a thin adapter
  // avoids a second Drizzle implementation with subtly different races.
  const upstream = await proxyRequestToPythonApi(request, {
    path: ["auth", "change-password"],
    user: { id: String(user.id), username: user.username },
    body: JSON.stringify({
      current_password: current_password ?? null,
      new_password,
    }),
  });
  const result = await upstream.json().catch(() => null);
  if (!upstream.ok) {
    return NextResponse.json(result ?? { detail: "パスワード変更に失敗しました" }, {
      status: upstream.status,
    });
  }

  const protocol = request.headers.get("x-forwarded-proto") || "http";
  const response = NextResponse.json(result ?? { success: true });
  const sessionVersion =
    result && typeof result === "object" && !Array.isArray(result)
      ? Number((result as Record<string, unknown>).session_version)
      : NaN;
  if (Number.isInteger(sessionVersion) && sessionVersion > 0) {
    attachSessionCookie(
      response,
      await createSessionToken(user.id, false, sessionVersion),
      protocol === "https",
    );
  }
  return response;
}
