import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import { proxyRequestToPythonApi } from "@/lib/server/python-api-proxy";

type RouteContext = {
  params: Promise<{ id: string }>;
};

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

function parsePatchBody(value: unknown): UserRecord | null {
  if (!isRecord(value)) return null;
  const allowed = new Set([
    "display_name",
    "email",
    "role",
    "is_active",
    "is_password_reset_required",
    "preferred_character",
    "user_settings",
  ]);
  if (Object.keys(value).some((key) => !allowed.has(key))) return null;
  if (
    "display_name" in value &&
    value.display_name !== null &&
    (typeof value.display_name !== "string" || value.display_name.length > 100)
  ) {
    return null;
  }
  if (
    "email" in value &&
    value.email !== null &&
    (typeof value.email !== "string" || value.email.length > 255)
  ) {
    return null;
  }
  if ("role" in value && value.role !== "admin" && value.role !== "user") {
    return null;
  }
  for (const key of ["is_active", "is_password_reset_required"]) {
    if (key in value && typeof value[key] !== "boolean") return null;
  }
  if (
    "preferred_character" in value &&
    value.preferred_character !== null &&
    (typeof value.preferred_character !== "string" ||
      value.preferred_character.length > 100)
  ) {
    return null;
  }
  if (
    "user_settings" in value &&
    (value.user_settings === null ||
      typeof value.user_settings !== "object" ||
      Array.isArray(value.user_settings))
  ) {
    return null;
  }
  if (
    isRecord(value.user_settings) &&
    Object.prototype.hasOwnProperty.call(value.user_settings, "account_lifecycle")
  ) {
    return null;
  }
  const normalized = { ...value };
  for (const key of ["display_name", "email", "preferred_character"] as const) {
    const current = normalized[key];
    if (typeof current === "string") {
      normalized[key] = current.trim() || null;
    }
  }
  return normalized;
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

export async function PATCH(request: NextRequest, context: RouteContext) {
  const guard = await requireAdmin();
  if (guard.error) return guard.error;
  const { id } = await context.params;
  const encodedId = encodeURIComponent(id);

  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ detail: "不正なJSONです" }, { status: 400 });
  }
  const patch = parsePatchBody(body);
  if (!patch || Object.keys(patch).length === 0) {
    return NextResponse.json({ detail: "更新内容が不正です" }, { status: 400 });
  }

  const response = await proxyRequestToPythonApi(request, {
    path: ["users", encodedId],
    user: guard.user,
    body: JSON.stringify(patch),
  });
  const result = await readProxyJson(response);
  if (!response.ok) return proxyResponse(result, response.status);
  const user = isRecord(result) ? result.user : result;
  return proxyResponse(normalizeUser(user), response.status);
}

export async function DELETE(request: NextRequest, context: RouteContext) {
  const guard = await requireAdmin();
  if (guard.error) return guard.error;
  const { id } = await context.params;
  const encodedId = encodeURIComponent(id);

  const response = await proxyRequestToPythonApi(request, {
    path: ["users", encodedId],
    user: guard.user,
  });
  const result = await readProxyJson(response);
  if (!response.ok) return proxyResponse(result, response.status);
  const user = isRecord(result) ? result.user : result;
  return proxyResponse(normalizeUser(user), response.status);
}
