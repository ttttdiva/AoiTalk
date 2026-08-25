import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import {
  listResolvedTokensForUser,
  saveUserHfToken,
} from "@/lib/hf/account";
import { detectRepoTypes, verifyToken, type RepoType } from "@/lib/hf/client";
import {
  addUserHfReferences as addScopedReferences,
  type UserHfReferenceRepo,
} from "@/lib/hf/user-store";
import { parseHfReferenceInput } from "@/lib/hf/reference-input";
import { buildHfPath } from "@/lib/hf/virtual-path";

const PRIVATE_HEADERS = { "Cache-Control": "private, no-store" };

export async function POST(request: NextRequest) {
  const user = await getSession();
  if (!user) return NextResponse.json({ detail: "認証が必要です" }, { status: 401, headers: PRIVATE_HEADERS });
  const userId = String(user.id);

  const body = (await request.json().catch(() => null)) as { value?: unknown } | null;
  const parsed = parseHfReferenceInput(typeof body?.value === "string" ? body.value : "");
  if (!parsed) {
    return NextResponse.json(
      { detail: "HFトークン、owner/repository、またはHugging Face URLを入力してください" },
      { status: 400, headers: PRIVATE_HEADERS },
    );
  }

  if (parsed.kind === "token") {
    try {
      const info = await verifyToken(parsed.token);
      if (!info.name) throw new Error("ユーザー名を取得できませんでした");
      const account = await saveUserHfToken(
        userId,
        info.name,
        parsed.token,
        info.fullname || info.name,
      );
      return NextResponse.json({ kind: "account", account }, { headers: PRIVATE_HEADERS });
    } catch {
      return NextResponse.json({ detail: "HFトークンを確認できませんでした" }, { status: 400, headers: PRIVATE_HEADERS });
    }
  }

  const found = new Map<RepoType, string | undefined>();
  // Anonymous public repositories are always checked first.  Private access
  // may use only credentials owned by the current AoiTalk principal.
  const scopedTokens = await listResolvedTokensForUser(userId);
  const candidates: Array<{ accountId?: string; token?: string }> = [
    { token: undefined },
    ...scopedTokens.map((item) => ({
      accountId: item.accountId,
      token: item.token,
    })),
  ];
  for (const candidate of candidates) {
    const types = await detectRepoTypes(parsed.repoId, candidate.token);
    for (const type of types) {
      if (!found.has(type)) found.set(type, candidate.accountId);
    }
    if (found.size === 2) break;
  }

  if (found.size === 0) {
    return NextResponse.json(
      { detail: "Model/Datasetが見つかりません。repo ID、公開状態、token権限を確認してください" },
      { status: 404, headers: PRIVATE_HEADERS },
    );
  }

  const references: UserHfReferenceRepo[] = [...found].map(([repoType, accountId]) => ({
    repoId: parsed.repoId,
    repoType,
    accountId,
  }));
  try {
    await addScopedReferences(userId, references);
  } catch {
    return NextResponse.json(
      { detail: "HF参照を保存できませんでした" },
      { status: 500, headers: PRIVATE_HEADERS },
    );
  }
  return NextResponse.json({
    kind: "repository",
    repositories: references.map((entry) => ({
      ...entry,
      path: buildHfPath({ kind: "repo", ...entry, subPath: "" }),
    })),
  }, { headers: PRIVATE_HEADERS });
}
