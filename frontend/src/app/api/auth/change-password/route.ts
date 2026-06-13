import { NextRequest, NextResponse } from "next/server";
import { db } from "@/db";
import { users } from "@/db/schema";
import { eq } from "drizzle-orm";
import { getSession, verifyPassword } from "@/lib/auth";
import bcrypt from "bcryptjs";

export async function POST(request: NextRequest) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const { current_password, new_password } = await request.json();

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

  if (current_password) {
    const valid = await verifyPassword(current_password, user.passwordHash);
    if (!valid) {
      return NextResponse.json(
        { detail: "現在のパスワードが正しくありません" },
        { status: 401 },
      );
    }
  }

  const hash = await bcrypt.hash(new_password, 12);
  await db
    .update(users)
    .set({ passwordHash: hash, isPasswordResetRequired: false })
    .where(eq(users.id, user.id));

  return NextResponse.json({ success: true });
}
