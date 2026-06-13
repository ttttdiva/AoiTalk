import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import { resolveToken } from "@/lib/hf/account";
import { listUserRepos, withRetry } from "@/lib/hf/client";
import { errorToString } from "@/lib/hf/api-utils";

export async function GET(request: NextRequest) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const accountId = request.nextUrl.searchParams.get("accountId");
  const resolved = resolveToken(accountId);
  if (!resolved) {
    return NextResponse.json(
      { detail: "HFアカウントが設定されていません(.env に HF_TOKEN_<name> を追加してください)" },
      { status: 400 },
    );
  }

  try {
    const repos = await withRetry(() =>
      listUserRepos(resolved.token, resolved.username),
    );
    return NextResponse.json({
      accountId: resolved.accountId,
      username: resolved.username,
      repos,
    });
  } catch (err) {
    return NextResponse.json(
      { detail: `リポジトリ一覧取得失敗: ${errorToString(err)}` },
      { status: 502 },
    );
  }
}
