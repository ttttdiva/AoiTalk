import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import {
  deleteUserHydrusSettings,
  getUserHydrusSettings,
  saveUserHydrusSettings,
} from "@/lib/hf/user-store";

const PRIVATE_HEADERS = { "Cache-Control": "private, no-store" };

/**
 * Hydrus接続設定。access keyはDBへ暗号化保存し、レスポンスには一切含めない。
 * 通常ユーザー自身のintegrationだけを操作でき、adminでも他ユーザーを指定できない。
 */
export async function GET() {
  const user = await getSession();
  if (!user) return NextResponse.json({ detail: "認証が必要です" }, { status: 401, headers: PRIVATE_HEADERS });
  const settings = await getUserHydrusSettings(String(user.id));
  return NextResponse.json(
    {
      configured: Boolean(settings),
      apiUrl: settings?.apiUrl ?? null,
      displayName: settings?.displayName ?? null,
    },
    { headers: PRIVATE_HEADERS },
  );
}

export async function PUT(request: NextRequest) {
  const user = await getSession();
  if (!user) return NextResponse.json({ detail: "認証が必要です" }, { status: 401, headers: PRIVATE_HEADERS });
  const body = (await request.json().catch(() => null)) as Record<string, unknown> | null;
  const apiUrl = typeof body?.apiUrl === "string" ? body.apiUrl : "";
  const accessKey = typeof body?.accessKey === "string" ? body.accessKey : "";
  const displayName = typeof body?.displayName === "string" ? body.displayName : undefined;
  try {
    await saveUserHydrusSettings(String(user.id), { apiUrl, accessKey, displayName });
    return NextResponse.json({ success: true }, { headers: PRIVATE_HEADERS });
  } catch (error) {
    const message = error instanceof Error ? error.message : "";
    const detail = message.startsWith("Hydrus API URL") || message === "Hydrus access keyが必要です"
      ? message
      : "Hydrus設定を保存できませんでした";
    return NextResponse.json(
      { detail },
      { status: detail === "Hydrus設定を保存できませんでした" ? 500 : 400, headers: PRIVATE_HEADERS },
    );
  }
}

export async function DELETE() {
  const user = await getSession();
  if (!user) return NextResponse.json({ detail: "認証が必要です" }, { status: 401, headers: PRIVATE_HEADERS });
  await deleteUserHydrusSettings(String(user.id));
  return NextResponse.json({ success: true }, { headers: PRIVATE_HEADERS });
}
