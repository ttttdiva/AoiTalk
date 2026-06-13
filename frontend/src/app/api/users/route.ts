import { NextRequest, NextResponse } from "next/server";
import { db } from "@/db";
import { users } from "@/db/schema";
import { getSession } from "@/lib/auth";
import { eq } from "drizzle-orm";
import bcrypt from "bcryptjs";

type UserSettings = Record<string, unknown>;

function getSettings(value: unknown): UserSettings {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as UserSettings)
    : {};
}

function lifecycleState(value: unknown): string {
  const settings = getSettings(value);
  const lifecycle = settings.account_lifecycle;
  if (lifecycle && typeof lifecycle === "object" && !Array.isArray(lifecycle)) {
    const state = (lifecycle as UserSettings).state;
    if (typeof state === "string") return state;
  }
  return "active";
}

function lifecycleDate(value: unknown, key: string): string | null {
  const settings = getSettings(value);
  const lifecycle = settings.account_lifecycle;
  if (lifecycle && typeof lifecycle === "object" && !Array.isArray(lifecycle)) {
    const date = (lifecycle as UserSettings)[key];
    if (typeof date === "string") return date;
  }
  return null;
}

function serializeUser(u: typeof users.$inferSelect) {
  const lifecycle = lifecycleState(u.userSettings);
  const status =
    lifecycle === "deleted" ? "deleted" : u.isActive ? "active" : "inactive";

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
    deleted_at: status === "deleted" ? lifecycleDate(u.userSettings, "deleted_at") : null,
  };
}

export async function GET() {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  if (user.role !== "admin") {
    return NextResponse.json({ detail: "管理者権限が必要です" }, { status: 403 });
  }

  const rows = await db.select().from(users);

  const result = rows
    .map(serializeUser)
    .sort((a, b) => {
      const stateOrder: Record<string, number> = {
        active: 0,
        inactive: 1,
        deleted: 2,
      };
      const byState = stateOrder[a.status] - stateOrder[b.status];
      if (byState !== 0) return byState;
      return a.username.localeCompare(b.username);
    });

  return NextResponse.json(result);
}

export async function POST(request: NextRequest) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  if (user.role !== "admin") {
    return NextResponse.json({ detail: "管理者権限が必要です" }, { status: 403 });
  }

  const body = await request.json();
  const {
    username,
    password,
    role,
    email,
    display_name,
    require_password_change,
  } = body;

  if (!username || !password) {
    return NextResponse.json(
      { detail: "usernameとpasswordは必須です" },
      { status: 400 }
    );
  }

  if (password.length < 6) {
    return NextResponse.json(
      { detail: "パスワードは6文字以上必要です" },
      { status: 400 }
    );
  }

  const [existing] = await db
    .select({ id: users.id })
    .from(users)
    .where(eq(users.username, username))
    .limit(1);
  if (existing) {
    return NextResponse.json(
      { detail: "このユーザー名は既に使われています" },
      { status: 409 },
    );
  }

  const hash = await bcrypt.hash(password, 12);

  const [newUser] = await db
    .insert(users)
    .values({
      username,
      email: email || null,
      displayName: display_name || null,
      passwordHash: hash,
      role: role || "member",
      isActive: true,
      isPasswordResetRequired: require_password_change !== false,
      createdAt: new Date(),
      updatedAt: new Date(),
      userSettings: {
        account_lifecycle: {
          state: "active",
        },
      },
    })
    .returning();

  return NextResponse.json(serializeUser(newUser));
}
