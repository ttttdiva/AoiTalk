import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import { resolveTokenForUser } from "@/lib/hf/account";
import { listUserRepos, withRetry } from "@/lib/hf/client";
import { errorToString } from "@/lib/hf/api-utils";

const PRIVATE_HEADERS = { "Cache-Control": "private, no-store" };

export async function GET(request: NextRequest) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401, headers: PRIVATE_HEADERS });
  }

  const accountId = request.nextUrl.searchParams.get("accountId");
  let resolved: Awaited<ReturnType<typeof resolveTokenForUser>> = null;
  try {
    resolved = await resolveTokenForUser(String(user.id), accountId);
  } catch {
    return NextResponse.json(
      { detail: "HFアカウントを解決できませんでした" },
      { status: 503, headers: PRIVATE_HEADERS },
    );
  }
  if (!resolved) {
    return NextResponse.json(
      { detail: "HFアカウントが設定されていません" },
      { status: 400, headers: PRIVATE_HEADERS },
    );
  }

  try {
    const repos = await withRetry(() =>
      listUserRepos(resolved.token, resolved.username),
    );
    return NextResponse.json(
      {
        accountId: resolved.accountId,
        username: resolved.username,
        repos,
      },
      { headers: { "Cache-Control": "private, no-store" } },
    );
  } catch (err) {
    return NextResponse.json(
      { detail: `リポジトリ一覧取得失敗: ${errorToString(err)}` },
      { status: 502, headers: PRIVATE_HEADERS },
    );
  }
}
