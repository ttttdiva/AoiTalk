import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import { proxyRequestToPythonApi } from "@/lib/server/python-api-proxy";

export const dynamic = "force-dynamic";
export const fetchCache = "force-no-store";

type UserRecord = Record<string, unknown>;

function isRecord(value: unknown): value is UserRecord {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function getSettings(value: unknown): UserRecord {
  return isRecord(value) ? value : {};
}

function normalizeUser(value: unknown): UserRecord {
  const user = isRecord(value) ? value : {};
  const settings = getSettings(user.settings ?? user.user_settings);
  const lifecycle = getSettings(settings.account_lifecycle);
  const active = user.is_active ?? user.isActive;
  const status =
    lifecycle.state === "deleted"
      ? "deleted"
      : active === true
        ? "active"
        : "inactive";
  return {
    ...user,
    id: user.id == null ? "" : String(user.id),
    display_name: user.display_name ?? user.displayName ?? null,
    avatar_url: user.avatar_url ?? user.avatarUrl ?? null,
    role: user.role ?? "user",
    is_active: active === true,
    status,
    is_deleted: status === "deleted",
    password_reset_required:
      user.password_reset_required ?? user.is_password_reset_required ?? null,
    deleted_at: status === "deleted" && typeof lifecycle.deleted_at === "string"
      ? lifecycle.deleted_at
      : null,
  };
}

function parseCreateBody(value: unknown): UserRecord | null {
  if (!isRecord(value)) return null;
  const allowed = new Set([
    "username",
    "password",
    "role",
    "email",
    "display_name",
    "require_password_change",
  ]);
  if (Object.keys(value).some((key) => !allowed.has(key))) return null;
  if (
    typeof value.username !== "string" ||
    !value.username.trim() ||
    value.username.trim().length > 100
  ) {
    return null;
  }
  if (typeof value.password !== "string" || value.password.length < 6 || value.password.length > 1024) {
    return null;
  }
  if (value.role !== undefined && value.role !== "admin" && value.role !== "user") {
    return null;
  }
  if (
    value.email !== undefined &&
    value.email !== null &&
    (typeof value.email !== "string" || value.email.length > 255)
  ) {
    return null;
  }
  if (
    value.display_name !== undefined &&
    value.display_name !== null &&
    (typeof value.display_name !== "string" || value.display_name.length > 100)
  ) {
    return null;
  }
  if (
    value.require_password_change !== undefined &&
    typeof value.require_password_change !== "boolean"
  ) {
    return null;
  }
  return {
    ...value,
    username: value.username.trim(),
    email: typeof value.email === "string" && value.email.trim() ? value.email.trim() : null,
    display_name:
      typeof value.display_name === "string" && value.display_name.trim()
        ? value.display_name.trim()
        : null,
    role: value.role ?? "user",
    require_password_change: value.require_password_change ?? true,
  };
}

async function requireAdmin() {
  const user = await getSession();
  if (!user) {
    return {
      error: NextResponse.json({ detail: "認証が必要です" }, { status: 401 }),
      user: null,
    };
  }
  if (user.role !== "admin") {
    return {
      error: NextResponse.json({ detail: "管理者権限が必要です" }, { status: 403 }),
      user: null,
    };
  }
  return { error: null, user };
}

async function readProxyJson(response: NextResponse): Promise<unknown> {
  return response.json().catch(() => null);
}

function proxyResponse(body: unknown, status: number): NextResponse {
  return NextResponse.json(body, {
    status,
    headers: { "cache-control": "no-store, no-cache, must-revalidate" },
  });
}

export async function GET(request: NextRequest) {
  const guard = await requireAdmin();
  if (guard.error) return guard.error;

  // The management console includes inactive/deleted accounts.  Keep this
  // adapter concern here while the FastAPI list endpoint remains reusable for
  // callers that intentionally request active users only.
  const url = new URL(request.url);
  if (!url.searchParams.has("include_inactive")) {
    url.searchParams.set("include_inactive", "true");
  }
  if (!url.searchParams.has("limit")) {
    url.searchParams.set("limit", "500");
  }
  const proxyRequest = new NextRequest(url, {
    method: "GET",
    headers: request.headers,
  });
  const response = await proxyRequestToPythonApi(proxyRequest, {
    path: ["users"],
    user: guard.user,
  });
  const body = await readProxyJson(response);
  if (!response.ok) return proxyResponse(body, response.status);
  const users = isRecord(body) && Array.isArray(body.users) ? body.users : body;
  const normalizedUsers = Array.isArray(users) ? users.map(normalizeUser) : [];
  const stateOrder: Record<string, number> = {
    active: 0,
    inactive: 1,
    deleted: 2,
  };
  normalizedUsers.sort((left, right) => {
    const byState =
      (stateOrder[String(left.status)] ?? 99) -
      (stateOrder[String(right.status)] ?? 99);
    if (byState !== 0) return byState;
    return String(left.username ?? "").localeCompare(String(right.username ?? ""));
  });
  return proxyResponse(
    normalizedUsers,
    response.status,
  );
}

export async function POST(request: NextRequest) {
  const guard = await requireAdmin();
  if (guard.error) return guard.error;

  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ detail: "不正なJSONです" }, { status: 400 });
  }
  const payload = parseCreateBody(body);
  if (!payload) {
    return NextResponse.json({ detail: "リクエスト形式が不正です" }, { status: 400 });
  }

  const response = await proxyRequestToPythonApi(request, {
    path: ["users"],
    user: guard.user,
    body: JSON.stringify(payload),
  });
  const result = await readProxyJson(response);
  if (!response.ok) return proxyResponse(result, response.status);
  const user = isRecord(result) ? result.user : result;
  return proxyResponse(normalizeUser(user), response.status);
}
