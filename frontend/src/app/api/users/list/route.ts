import { NextResponse } from "next/server";
import { db } from "@/db";
import { users } from "@/db/schema";
import { eq } from "drizzle-orm";
import { getSession } from "@/lib/auth";

// 全ユーザーの基本情報を返す（メンバー追加UI用、ログイン済みなら誰でも取得可）
export async function GET() {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const rows = await db
    .select({
      id: users.id,
      username: users.username,
      displayName: users.displayName,
      isActive: users.isActive,
    })
    .from(users)
    .where(eq(users.isActive, true));

  const result = rows.map((u) => ({
    id: u.id,
    username: u.username,
    display_name: u.displayName,
  }));

  return NextResponse.json(result);
}
