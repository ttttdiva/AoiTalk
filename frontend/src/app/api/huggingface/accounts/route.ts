import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import { listAccountsForUser } from "@/lib/hf/account";
import { listUserHfReferences } from "@/lib/hf/user-store";
import { deleteUserHfAccount } from "@/lib/hf/user-store";

const PRIVATE_HEADERS = { "Cache-Control": "private, no-store" };

export async function GET() {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401, headers: PRIVATE_HEADERS });
  }

  const accounts = (await listAccountsForUser(String(user.id))).map((a) => ({
    id: a.id,
    username: a.username,
    label: a.label,
    source: a.source,
  }));
  const references = await listUserHfReferences(String(user.id));
  return NextResponse.json({ accounts, references }, {
    headers: { "Cache-Control": "private, no-store" },
  });
}

/** Delete only an HF account owned by the current principal. */
export async function DELETE(request: NextRequest) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401, headers: PRIVATE_HEADERS });
  }
  const accountId = request.nextUrl.searchParams.get("accountId");
  if (!accountId) {
    return NextResponse.json({ detail: "accountId は必須です" }, { status: 400, headers: PRIVATE_HEADERS });
  }
  let deleted = false;
  try {
    deleted = await deleteUserHfAccount(String(user.id), accountId);
  } catch {
    return NextResponse.json(
      { detail: "HFアカウントを削除できませんでした" },
      { status: 500, headers: PRIVATE_HEADERS },
    );
  }
  if (!deleted) {
    return NextResponse.json({ detail: "HFアカウントが見つかりません" }, { status: 404, headers: PRIVATE_HEADERS });
  }
  return NextResponse.json(
    { success: true },
    { headers: PRIVATE_HEADERS },
  );
}
