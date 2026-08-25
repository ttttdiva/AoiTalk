import path from "node:path";
import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import { resolveTokenForUser } from "@/lib/hf/account";
import { uploadRepoFile } from "@/lib/hf/client";
import { parseHfPath } from "@/lib/hf/virtual-path";

const PRIVATE_HEADERS = { "Cache-Control": "private, no-store" };

function normalizeRelativePath(value: string): string | null {
  const normalized = value.replace(/\\/g, "/").replace(/^\/+|\/+$/g, "");
  const segments = normalized.split("/");
  if (
    !normalized ||
    normalized.length > 1024 ||
    segments.some(
      (segment) =>
        !segment ||
        segment === "." ||
        segment === ".." ||
        segment.includes("\0"),
    )
  ) {
    return null;
  }
  return segments.join("/");
}

export async function POST(request: NextRequest) {
  const user = await getSession();
  if (!user)
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401, headers: PRIVATE_HEADERS });

  const form = await request.formData();
  const virtualPath = form.get("path");
  const parsed =
    typeof virtualPath === "string" ? parseHfPath(virtualPath) : null;
  if (!parsed || parsed.kind !== "repo" || !parsed.repoId || !parsed.repoType) {
    return NextResponse.json(
      { detail: "HFリポジトリ内で実行してください" },
      { status: 400, headers: PRIVATE_HEADERS },
    );
  }
  if (!parsed.accountId) {
    return NextResponse.json(
      { detail: "書き込み用HFアカウントが関連付けられていません" },
      { status: 403, headers: PRIVATE_HEADERS },
    );
  }
  let resolved: Awaited<ReturnType<typeof resolveTokenForUser>> = null;
  try {
    resolved = await resolveTokenForUser(String(user.id), parsed.accountId);
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

  const files = form
    .getAll("files")
    .filter((item): item is File => item instanceof File);
  const filePaths = form.getAll("filePaths");
  if (files.length === 0) {
    return NextResponse.json(
      { detail: "アップロードするファイルがありません" },
      { status: 400, headers: PRIVATE_HEADERS },
    );
  }

  const basePath = parsed.subPath?.replace(/^\/+|\/+$/g, "") || "";
  if (basePath && !normalizeRelativePath(basePath)) {
    return NextResponse.json(
      { detail: "アップロード先のパスが不正です" },
      { status: 400, headers: PRIVATE_HEADERS },
    );
  }
  const uploadEntries = files.map((file, index) => {
    const supplied = filePaths[index];
    const fallback = path.posix.basename(file.name.replace(/\\/g, "/"));
    const rawRelativePath =
      typeof supplied === "string" && supplied.trim() ? supplied : fallback;
    const relativePath = normalizeRelativePath(rawRelativePath);
    return {
      relativePath: relativePath || fallback,
      destination: relativePath
        ? [basePath, relativePath].filter(Boolean).join("/")
        : null,
    };
  });
  const destinations = uploadEntries.map((entry) => entry.destination);
  const duplicatePaths = new Set<string>();
  const seenPaths = new Set<string>();
  for (const destination of destinations) {
    if (!destination) continue;
    if (seenPaths.has(destination)) duplicatePaths.add(destination);
    seenPaths.add(destination);
  }

  const results: PromiseSettledResult<void>[] = destinations.map(
    (destination) =>
      !destination || duplicatePaths.has(destination)
        ? {
            status: "rejected",
            reason: new Error("invalid or duplicate destination"),
          }
        : (undefined as unknown as PromiseSettledResult<void>),
  );
  let cursor = 0;
  await Promise.all(
    Array.from({ length: Math.min(3, files.length) }, async () => {
      for (;;) {
        const index = cursor++;
        if (index >= files.length) return;
        if (results[index]) continue;
        const file = files[index];
        try {
          await uploadRepoFile({
            accessToken: resolved.token,
            repoId: parsed.repoId!,
            repoType: parsed.repoType!,
            path: destinations[index]!,
            file,
          });
          results[index] = { status: "fulfilled", value: undefined };
        } catch (reason) {
          results[index] = { status: "rejected", reason };
        }
      }
    }),
  );
  const failures = results
    .map((result, index) => ({
      result,
      name: files[index].name,
      relativePath: uploadEntries[index].relativePath,
    }))
    .filter((item) => item.result.status === "rejected")
    .map((item) => ({
      name: item.name,
      relativePath: item.relativePath,
      error: "HFへのアップロードに失敗しました",
    }));
  const successCount = results.length - failures.length;
  return NextResponse.json(
    {
      success: failures.length === 0,
      successCount,
      failureCount: failures.length,
      failures,
      ...(successCount === 0
        ? { detail: "HFへのアップロードに失敗しました" }
        : {}),
    },
    { status: successCount > 0 ? 200 : 502, headers: PRIVATE_HEADERS },
  );
}
