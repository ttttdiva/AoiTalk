import { NextRequest, NextResponse } from "next/server";
import bcrypt from "bcryptjs";
import { eq } from "drizzle-orm";
import { db } from "@/db";
import { users } from "@/db/schema";
import { verifyPasswordResetToken } from "@/lib/auth";

export async function POST(request: NextRequest) {
  const { token, password } = await request.json();
  if (!token) {
    return NextResponse.json({ detail: "再設定トークンが必要です" }, { status: 400 });
  }
  if (!password || password.length < 6) {
    return NextResponse.json(
      { detail: "新しいパスワードは6文字以上必要です" },
      { status: 400 },
    );
  }

  const userId = await verifyPasswordResetToken(token);
  if (!userId) {
    return NextResponse.json(
      { detail: "再設定リンクが無効または期限切れです" },
      { status: 400 },
    );
  }

  const [target] = await db
    .select()
    .from(users)
    .where(eq(users.id, userId))
    .limit(1);
  if (!target || !target.isActive) {
    return NextResponse.json(
      { detail: "対象ユーザーが存在しないか無効です" },
      { status: 400 },
    );
  }

  const hash = await bcrypt.hash(password, 12);
  await db
    .update(users)
    .set({
      passwordHash: hash,
      isPasswordResetRequired: false,
      updatedAt: new Date(),
    })
    .where(eq(users.id, userId));

  return NextResponse.json({ success: true });
}
