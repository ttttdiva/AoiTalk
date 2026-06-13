import { NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import { listAccounts } from "@/lib/hf/account";

export async function GET() {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const accounts = listAccounts().map((a) => ({
    id: a.id,
    username: a.username,
    label: a.label,
    source: a.source,
  }));
  return NextResponse.json({ accounts });
}
