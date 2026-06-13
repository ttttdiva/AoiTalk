import { NextResponse } from "next/server";
import { eq } from "drizzle-orm";
import { db } from "@/db";
import { users } from "@/db/schema";
import { createPasswordResetToken, getSession } from "@/lib/auth";

type RouteContext = {
  params: Promise<{ id: string }>;
};

function getRequestOrigin(request: Request) {
  const requestUrl = new URL(request.url);
  const forwardedProto = request.headers.get("x-forwarded-proto");
  const forwardedHost = request.headers.get("x-forwarded-host");
  const host = forwardedHost ?? request.headers.get("host") ?? requestUrl.host;
  const protocol = forwardedProto ?? requestUrl.protocol.replace(":", "");

  return `${protocol}://${host}`;
}

export async function POST(request: Request, context: RouteContext) {
  const admin = await getSession();
  if (!admin) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }
  if (admin.role !== "admin") {
    return NextResponse.json({ detail: "管理者権限が必要です" }, { status: 403 });
  }

  const { id } = await context.params;
  const [target] = await db.select().from(users).where(eq(users.id, id)).limit(1);
  if (!target) {
    return NextResponse.json({ detail: "ユーザーが見つかりません" }, { status: 404 });
  }
  if (!target.isActive) {
    return NextResponse.json(
      { detail: "無効または削除済みのユーザーには再設定リンクを発行できません" },
      { status: 400 },
    );
  }

  await db
    .update(users)
    .set({ isPasswordResetRequired: true, updatedAt: new Date() })
    .where(eq(users.id, id));

  const token = await createPasswordResetToken(id);
  const baseUrl = getRequestOrigin(request);
  return NextResponse.json({
    reset_url: `${baseUrl}/reset-password?token=${encodeURIComponent(token)}`,
    expires_in_hours: 24,
  });
}
