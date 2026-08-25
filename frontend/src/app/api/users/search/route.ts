import { and, eq, ilike, isNull, or } from "drizzle-orm";
import { NextRequest, NextResponse } from "next/server";
import { db } from "@/db";
import { users } from "@/db/schema";
import { getSession } from "@/lib/auth";
import { avatarUrl } from "@/lib/server/user-avatar";

/** Safe user lookup for mention/share pickers (never exposes credentials/roles). */
export async function GET(request: NextRequest) {
  const session = await getSession();
  if (!session) return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  const q = request.nextUrl.searchParams.get("q")?.trim() ?? "";
  const limit = Math.min(Math.max(Number(request.nextUrl.searchParams.get("limit")) || 20, 1), 50);
  const pattern = `%${q}%`;
  const rows = await db
    .select({
      id: users.id,
      username: users.username,
      email: users.email,
      displayName: users.displayName,
      avatarPath: users.avatarPath,
    })
    .from(users)
    .where(
      and(
        or(eq(users.isActive, true), isNull(users.isActive)),
        q
          ? or(
              ilike(users.username, pattern),
              ilike(users.email, pattern),
              ilike(users.displayName, pattern),
            )
          : undefined,
      ),
    )
    .orderBy(users.username)
    .limit(limit);
  return NextResponse.json({
    users: rows.map((row) => ({
      id: row.id,
      username: row.username,
      email: row.email,
      display_name: row.displayName,
      avatar_url: avatarUrl(row.id, row.avatarPath),
    })),
  });
}
