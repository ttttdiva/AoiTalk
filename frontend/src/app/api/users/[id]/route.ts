import { NextRequest, NextResponse } from "next/server";
import { and, eq, ne } from "drizzle-orm";
import { db } from "@/db";
import { users } from "@/db/schema";
import { getSession } from "@/lib/auth";

type RouteContext = {
  params: Promise<{ id: string }>;
};

type UserSettings = Record<string, unknown>;

function getSettings(value: unknown): UserSettings {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as UserSettings)
    : {};
}

function serializeUser(u: typeof users.$inferSelect) {
  const settings = getSettings(u.userSettings);
  const lifecycle = settings.account_lifecycle;
  const lifecycleObject =
    lifecycle && typeof lifecycle === "object" && !Array.isArray(lifecycle)
      ? (lifecycle as UserSettings)
      : null;
  const state = lifecycleObject ? lifecycleObject.state : null;
  const deletedAt = lifecycleObject ? lifecycleObject.deleted_at : null;
  const status = state === "deleted" ? "deleted" : u.isActive ? "active" : "inactive";

  return {
    id: u.id,
    username: u.username,
    email: u.email,
    display_name: u.displayName,
    role: u.role,
    is_active: u.isActive,
    status,
    is_deleted: status === "deleted",
    password_reset_required: u.isPasswordResetRequired,
    created_at: u.createdAt,
    last_login: u.lastLogin,
    deleted_at: status === "deleted" && typeof deletedAt === "string" ? deletedAt : null,
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

async function activeAdminCount(excludingUserId?: string) {
  const rows = await db
    .select({ id: users.id })
    .from(users)
    .where(
      excludingUserId
        ? and(eq(users.role, "admin"), eq(users.isActive, true), ne(users.id, excludingUserId))
        : and(eq(users.role, "admin"), eq(users.isActive, true)),
    );
  return rows.length;
}

export async function PATCH(request: NextRequest, context: RouteContext) {
  const guard = await requireAdmin();
  if (guard.error) return guard.error;

  const { id } = await context.params;
  const body = await request.json();

  const [target] = await db.select().from(users).where(eq(users.id, id)).limit(1);
  if (!target) {
    return NextResponse.json({ detail: "ユーザーが見つかりません" }, { status: 404 });
  }

  const settings = getSettings(target.userSettings);
  const updates: Partial<typeof users.$inferInsert> = {
    updatedAt: new Date(),
  };

  if ("display_name" in body) updates.displayName = body.display_name || null;
  if ("email" in body) updates.email = body.email || null;
  if ("role" in body) {
    if (!["admin", "member", "viewer"].includes(body.role)) {
      return NextResponse.json({ detail: "不正なロールです" }, { status: 400 });
    }
    if (target.role === "admin" && body.role !== "admin") {
      const remainingAdmins = await activeAdminCount(target.id);
      if (remainingAdmins <= 0) {
        return NextResponse.json(
          { detail: "最後の管理者のロールは変更できません" },
          { status: 400 },
        );
      }
    }
    updates.role = body.role;
  }
  if ("is_active" in body) {
    const nextActive = Boolean(body.is_active);
    if (!nextActive && target.id === guard.user?.id) {
      return NextResponse.json(
        { detail: "自分自身は無効化できません" },
        { status: 400 },
      );
    }
    if (!nextActive && target.role === "admin") {
      const remainingAdmins = await activeAdminCount(target.id);
      if (remainingAdmins <= 0) {
        return NextResponse.json(
          { detail: "最後の管理者は無効化できません" },
          { status: 400 },
        );
      }
    }
    updates.isActive = nextActive;
    settings.account_lifecycle = {
      state: nextActive ? "active" : "inactive",
      updated_at: new Date().toISOString(),
      updated_by: guard.user?.id,
    };
    updates.userSettings = settings;
  }
  if ("is_password_reset_required" in body) {
    updates.isPasswordResetRequired = Boolean(body.is_password_reset_required);
  }

  const [updated] = await db
    .update(users)
    .set(updates)
    .where(eq(users.id, id))
    .returning();

  return NextResponse.json(serializeUser(updated));
}

export async function DELETE(_request: NextRequest, context: RouteContext) {
  const guard = await requireAdmin();
  if (guard.error) return guard.error;

  const { id } = await context.params;
  const [target] = await db.select().from(users).where(eq(users.id, id)).limit(1);
  if (!target) {
    return NextResponse.json({ detail: "ユーザーが見つかりません" }, { status: 404 });
  }
  if (target.id === guard.user?.id) {
    return NextResponse.json(
      { detail: "自分自身は削除できません" },
      { status: 400 },
    );
  }
  if (target.role === "admin") {
    const remainingAdmins = await activeAdminCount(target.id);
    if (remainingAdmins <= 0) {
      return NextResponse.json(
        { detail: "最後の管理者は削除できません" },
        { status: 400 },
      );
    }
  }

  const settings = getSettings(target.userSettings);
  settings.account_lifecycle = {
    state: "deleted",
    deleted_at: new Date().toISOString(),
    deleted_by: guard.user?.id,
  };

  const [updated] = await db
    .update(users)
    .set({
      isActive: false,
      isPasswordResetRequired: true,
      userSettings: settings,
      updatedAt: new Date(),
    })
    .where(eq(users.id, id))
    .returning();

  return NextResponse.json(serializeUser(updated));
}
