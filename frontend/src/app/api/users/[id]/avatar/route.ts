import fs from "node:fs";
import { NextRequest, NextResponse } from "next/server";
import { eq } from "drizzle-orm";
import { db } from "@/db";
import { users } from "@/db/schema";
import { getSession } from "@/lib/auth";
import {
  MAX_AVATAR_BYTES,
  detectAvatarMime,
  resolveStoredAvatarPath,
} from "@/lib/server/user-avatar";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(
  _request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const sessionUser = await getSession();
  if (!sessionUser) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const { id } = await params;
  const [target] = await db
    .select({ id: users.id, role: users.role, avatarPath: users.avatarPath })
    .from(users)
    .where(eq(users.id, id))
    .limit(1);
  if (!target) {
    return NextResponse.json({ detail: "ユーザーが見つかりません" }, { status: 404 });
  }
  if (sessionUser.id !== target.id && sessionUser.role !== "admin") {
    return NextResponse.json({ detail: "権限がありません" }, { status: 403 });
  }

  const targetPath = resolveStoredAvatarPath(target.id, target.avatarPath);
  if (!targetPath) {
    return NextResponse.json({ detail: "アイコンが設定されていません" }, { status: 404 });
  }

  let stat: fs.Stats;
  try {
    stat = fs.lstatSync(targetPath);
  } catch {
    return NextResponse.json({ detail: "アイコンファイルが見つかりません" }, { status: 404 });
  }
  if (stat.isSymbolicLink() || !stat.isFile()) {
    return NextResponse.json({ detail: "アイコンファイルが不正です" }, { status: 404 });
  }
  if (stat.size <= 0 || stat.size > MAX_AVATAR_BYTES) {
    return NextResponse.json({ detail: "アイコンファイルが不正です" }, { status: 404 });
  }

  let data: Buffer;
  try {
    data = fs.readFileSync(targetPath);
  } catch {
    return NextResponse.json({ detail: "アイコンファイルを読み込めません" }, { status: 404 });
  }
  const mime = detectAvatarMime(data);
  if (!mime) {
    return NextResponse.json({ detail: "許可されていない画像形式です" }, { status: 415 });
  }

  return new NextResponse(new Uint8Array(data), {
    headers: {
      "Content-Type": mime,
      "Content-Length": String(data.byteLength),
      "Cache-Control": "private, max-age=31536000, immutable",
      "X-Content-Type-Options": "nosniff",
    },
  });
}
