import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import { resolveTokenForUser } from "@/lib/hf/account";
import { listRepoTree, withRetry, type RepoType } from "@/lib/hf/client";
import { errorToString } from "@/lib/hf/api-utils";

const PRIVATE_HEADERS = { "Cache-Control": "private, no-store" };

export async function GET(request: NextRequest) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401, headers: PRIVATE_HEADERS });
  }

  const sp = request.nextUrl.searchParams;
  const accountId = sp.get("accountId");
  const repoId = sp.get("repoId");
  const repoType = (sp.get("repoType") || "model") as RepoType;
  const path = sp.get("path") || undefined;

  if (!repoId) {
    return NextResponse.json({ detail: "repoId は必須" }, { status: 400, headers: PRIVATE_HEADERS });
  }
  if (repoType !== "model" && repoType !== "dataset") {
    return NextResponse.json({ detail: "repoType 不正" }, { status: 400, headers: PRIVATE_HEADERS });
  }

  // accountId supplied by the client is never trusted by itself.  It must be
  // owned by the current principal.  Without one, only anonymous public data is
  // requested; no global fallback token is used.
  let resolved: Awaited<ReturnType<typeof resolveTokenForUser>> = null;
  try {
    resolved = accountId
      ? await resolveTokenForUser(String(user.id), accountId)
      : null;
  } catch {
    return NextResponse.json(
      { detail: "HFアカウントを解決できませんでした" },
      { status: 503, headers: PRIVATE_HEADERS },
    );
  }
  if (accountId && !resolved) {
    return NextResponse.json({ detail: "HFアカウントへのアクセス権がありません" }, { status: 403, headers: PRIVATE_HEADERS });
  }
  const token = resolved?.token;

  try {
    const entries = await withRetry(() =>
      listRepoTree(token, repoId, repoType, path),
    );
    return NextResponse.json(
      {
        repoId,
        repoType,
        path: path ?? "",
        entries,
      },
      { headers: { "Cache-Control": "private, no-store" } },
    );
  } catch (err) {
    return NextResponse.json(
      { detail: `ツリー取得失敗: ${errorToString(err)}` },
      { status: 502, headers: PRIVATE_HEADERS },
    );
  }
}
