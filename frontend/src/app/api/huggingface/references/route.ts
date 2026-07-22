import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import {
  getFallbackToken,
  listResolvedTokens,
} from "@/lib/hf/account";
import { detectRepoTypes, verifyToken, type RepoType } from "@/lib/hf/client";
import {
  addReferenceRepos,
  saveHfToken,
  type HfReferenceRepo,
} from "@/lib/hf/env-store";
import { parseHfReferenceInput } from "@/lib/hf/reference-input";
import { buildHfPath } from "@/lib/hf/virtual-path";

export async function POST(request: NextRequest) {
  const user = await getSession();
  if (!user) return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  if (user.role !== "admin") {
    return NextResponse.json({ detail: "管理者権限が必要です" }, { status: 403 });
  }

  const body = (await request.json().catch(() => null)) as { value?: unknown } | null;
  const parsed = parseHfReferenceInput(typeof body?.value === "string" ? body.value : "");
  if (!parsed) {
    return NextResponse.json(
      { detail: "HFトークン、owner/repository、またはHugging Face URLを入力してください" },
      { status: 400 },
    );
  }

  if (parsed.kind === "token") {
    try {
      const info = await verifyToken(parsed.token);
      if (!info.name) throw new Error("ユーザー名を取得できませんでした");
      await saveHfToken(info.name, parsed.token);
      return NextResponse.json({
        kind: "account",
        account: { id: `env:${info.name}`, username: info.name, label: info.fullname || info.name },
      });
    } catch {
      return NextResponse.json({ detail: "HFトークンを確認できませんでした" }, { status: 400 });
    }
  }

  const found = new Map<RepoType, string | undefined>();
  const candidates: Array<{ accountId?: string; token?: string }> = [
    { token: getFallbackToken() },
    ...listResolvedTokens().map((item) => ({ accountId: item.accountId, token: item.token })),
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
      { status: 404 },
    );
  }

  const references: HfReferenceRepo[] = [...found].map(([repoType, accountId]) => ({
    repoId: parsed.repoId,
    repoType,
    accountId,
  }));
  await addReferenceRepos(references);
  return NextResponse.json({
    kind: "repository",
    repositories: references.map((entry) => ({
      ...entry,
      path: buildHfPath({ kind: "repo", ...entry, subPath: "" }),
    })),
  });
}
