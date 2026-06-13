import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import { resolveToken, getFallbackToken } from "@/lib/hf/account";
import { listRepoTree, withRetry, type RepoType } from "@/lib/hf/client";
import { errorToString } from "@/lib/hf/api-utils";

export async function GET(request: NextRequest) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const sp = request.nextUrl.searchParams;
  const accountId = sp.get("accountId");
  const repoId = sp.get("repoId");
  const repoType = (sp.get("repoType") || "model") as RepoType;
  const path = sp.get("path") || undefined;

  if (!repoId) {
    return NextResponse.json({ detail: "repoId は必須" }, { status: 400 });
  }
  if (repoType !== "model" && repoType !== "dataset") {
    return NextResponse.json({ detail: "repoType 不正" }, { status: 400 });
  }

  const resolved = resolveToken(accountId);
  const token = resolved?.token ?? getFallbackToken();

  try {
    const entries = await withRetry(() =>
      listRepoTree(token, repoId, repoType, path),
    );
    return NextResponse.json({
      repoId,
      repoType,
      path: path ?? "",
      entries,
    });
  } catch (err) {
    return NextResponse.json(
      { detail: `ツリー取得失敗: ${errorToString(err)}` },
      { status: 502 },
    );
  }
}
