import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import { resolveTokenForUser } from "@/lib/hf/account";
import { deleteRepoFiles, listRepoFilePathsRecursive } from "@/lib/hf/client";
import { normalizeRelativePath } from "@/lib/hf/relative-path";
import { parseHfPath, type RepoType } from "@/lib/hf/virtual-path";

const PRIVATE_HEADERS = { "Cache-Control": "private, no-store" };

interface DeleteItem {
  path: string;
  isDirectory: boolean;
}

interface RepoGroup {
  accountId: string;
  repoId: string;
  repoType: RepoType;
  /** ファイルの subPath */
  filePaths: string[];
  /** ディレクトリの subPath（サーバー側で再帰展開する） */
  directoryPaths: string[];
}

function parseItems(raw: unknown): DeleteItem[] | null {
  if (!Array.isArray(raw) || raw.length === 0) return null;
  const items: DeleteItem[] = [];
  for (const entry of raw) {
    if (!entry || typeof entry !== "object") return null;
    const record = entry as { path?: unknown; isDirectory?: unknown };
    if (typeof record.path !== "string" || !record.path) return null;
    items.push({
      path: record.path,
      isDirectory: record.isDirectory === true,
    });
  }
  return items;
}

export async function POST(request: NextRequest) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401, headers: PRIVATE_HEADERS });
  }

  const body = (await request.json().catch(() => null)) as {
    items?: unknown;
  } | null;
  const items = parseItems(body?.items);
  if (!items) {
    return NextResponse.json(
      { detail: "削除対象が指定されていません" },
      { status: 400, headers: PRIVATE_HEADERS },
    );
  }

  // 同一リポジトリごとにまとめて1コミットで削除する
  const groups = new Map<string, RepoGroup>();
  for (const item of items) {
    const parsed = parseHfPath(item.path);
    if (
      !parsed ||
      parsed.kind !== "repo" ||
      !parsed.repoId ||
      !parsed.repoType ||
      !parsed.subPath
    ) {
      return NextResponse.json(
        { detail: "HFリポジトリ内のファイルを指定してください" },
        { status: 400, headers: PRIVATE_HEADERS },
      );
    }
    if (!parsed.accountId) {
      return NextResponse.json(
        { detail: "書き込み用HFアカウントが関連付けられていません" },
        { status: 403, headers: PRIVATE_HEADERS },
      );
    }
    // upload/route.ts と同じ規則で検証する
    const subPath = normalizeRelativePath(parsed.subPath);
    if (!subPath) {
      return NextResponse.json(
        { detail: "削除対象のパスが不正です" },
        { status: 400, headers: PRIVATE_HEADERS },
      );
    }
    const key = `${parsed.accountId}|${parsed.repoType}|${parsed.repoId}`;
    const group = groups.get(key) ?? {
      accountId: parsed.accountId,
      repoId: parsed.repoId,
      repoType: parsed.repoType,
      filePaths: [],
      directoryPaths: [],
    };
    if (item.isDirectory) group.directoryPaths.push(subPath);
    else group.filePaths.push(subPath);
    groups.set(key, group);
  }

  let deletedCount = 0;
  for (const group of groups.values()) {
    let resolved: Awaited<ReturnType<typeof resolveTokenForUser>> = null;
    try {
      resolved = await resolveTokenForUser(String(user.id), group.accountId);
    } catch {
      return NextResponse.json(
        { detail: "HFアカウントを解決できませんでした" },
        { status: 503, headers: PRIVATE_HEADERS },
      );
    }
    if (!resolved) {
      return NextResponse.json(
        { detail: "HFアカウントが見つかりません" },
        { status: 403, headers: PRIVATE_HEADERS },
      );
    }
    const targets = new Set(group.filePaths);
    try {
      for (const directory of group.directoryPaths) {
        const children = await listRepoFilePathsRecursive(
          resolved.token,
          group.repoId,
          group.repoType,
          directory,
        );
        for (const child of children) targets.add(child);
      }
      const paths = Array.from(targets);
      if (paths.length === 0) continue;
      await deleteRepoFiles({
        accessToken: resolved.token,
        repoId: group.repoId,
        repoType: group.repoType,
        paths,
      });
      deletedCount += paths.length;
    } catch {
      return NextResponse.json(
        { detail: "HF上のファイル削除に失敗しました" },
        { status: 502, headers: PRIVATE_HEADERS },
      );
    }
  }

  return NextResponse.json({ success: true, deletedCount }, { headers: PRIVATE_HEADERS });
}
