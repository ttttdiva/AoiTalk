import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import { resolveTokenForUser } from "@/lib/hf/account";
import {
  listRepoTree,
  withRetry,
  type FileEntry,
  type RepoType,
} from "@/lib/hf/client";
import { createHfNameMatcher, normalizeHfSearchLimit } from "@/lib/hf/search";
import { buildHfPath, inferMediaType } from "@/lib/hf/virtual-path";
import type { ExplorerSearchResponse, SearchResult } from "@/lib/explorer-api";

const PRIVATE_HEADERS = { "Cache-Control": "private, no-store" };

function errorResponse(detail: string, status: number): NextResponse {
  return NextResponse.json(
    { detail },
    {
      status,
      headers: PRIVATE_HEADERS,
    },
  );
}

function normalizeRepositoryPath(
  value: string | null,
): string | null | undefined {
  const raw = (value ?? "").trim().replace(/\\/g, "/");
  if (!raw) return undefined;
  const parts = raw.split("/").filter(Boolean);
  if (
    parts.length === 0 ||
    parts.some(
      (part) =>
        part === "." ||
        part === ".." ||
        part.includes("|") ||
        /[\u0000-\u001f\u007f]/.test(part),
    )
  ) {
    return null;
  }
  return parts.join("/");
}

function canonicalEntryPath(
  rootPath: string,
  entryPath: string,
): string | null {
  const raw = entryPath.trim().replace(/\\/g, "/");
  if (!raw) return null;
  const parts = raw.split("/").filter(Boolean);
  if (
    parts.length === 0 ||
    parts.some(
      (part) =>
        part === "." ||
        part === ".." ||
        part.includes("|") ||
        /[\u0000-\u001f\u007f]/.test(part),
    )
  ) {
    return null;
  }
  const normalized = parts.join("/");
  if (!rootPath) return normalized;
  return normalized === rootPath || normalized.startsWith(`${rootPath}/`)
    ? normalized
    : `${rootPath}/${normalized}`;
}

function fileExtension(name: string): string {
  const separator = name.lastIndexOf(".");
  return separator >= 0 ? name.slice(separator).toLowerCase() : "";
}

function entrySearchResult(
  entry: FileEntry,
  virtualPath: string,
): SearchResult {
  const result: SearchResult = {
    name: entry.name || virtualPath.split("/").pop() || virtualPath,
    path: virtualPath,
    kind: entry.type,
    modified_at: entry.lastModified,
  };
  if (entry.type === "file") {
    result.type = inferMediaType(result.name);
    result.extension = fileExtension(result.name);
    result.size_bytes = entry.size;
  }
  return result;
}

function relativeEntryPath(rootPath: string, fullPath: string): string {
  if (!rootPath) return fullPath;
  if (fullPath === rootPath) return "";
  if (fullPath.startsWith(`${rootPath}/`)) {
    return fullPath.slice(rootPath.length + 1);
  }
  return fullPath;
}

/** Add parent directories omitted by recursive Hub listings. */
function addDerivedDirectories(
  results: Map<string, SearchResult>,
  rootPath: string,
  fullPath: string,
  toVirtualPath: (path: string) => string,
): void {
  const relative = relativeEntryPath(rootPath, fullPath);
  const parts = relative.split("/").filter(Boolean);
  if (parts.length < 2) return;
  for (let index = 1; index < parts.length; index += 1) {
    const path = [rootPath, ...parts.slice(0, index)].filter(Boolean).join("/");
    if (!results.has(path)) {
      results.set(path, {
        name: parts[index - 1],
        path: toVirtualPath(path),
        kind: "directory",
      });
    }
  }
}

function compareResults(a: SearchResult, b: SearchResult): number {
  const aDirectory = a.kind === "directory";
  const bDirectory = b.kind === "directory";
  if (aDirectory !== bDirectory) return aDirectory ? -1 : 1;
  return a.name.localeCompare(b.name) || a.path.localeCompare(b.path);
}

export async function GET(request: NextRequest) {
  const user = await getSession();
  if (!user) return errorResponse("認証が必要です", 401);

  const searchParams = request.nextUrl.searchParams;
  const repoId = (searchParams.get("repoId") ?? "").trim();
  const query = (searchParams.get("q") ?? "").trim();
  const repoTypeRaw =
    searchParams.get("repoType") ?? searchParams.get("type") ?? "model";
  const regexRaw = searchParams.get("regex");
  const regex = regexRaw === "true";
  const accountIdRaw = searchParams.get("accountId");

  if (
    !repoId ||
    repoId.length > 512 ||
    /[\u0000-\u001f\u007f|\\]/.test(repoId)
  ) {
    return errorResponse("repoId は必須です", 400);
  }
  if (!query || query.length > 512) {
    return errorResponse("q は必須です", 400);
  }
  if (repoTypeRaw !== "model" && repoTypeRaw !== "dataset") {
    return errorResponse("repoType 不正", 400);
  }
  if (regexRaw !== null && regexRaw !== "true" && regexRaw !== "false") {
    return errorResponse("regex 不正", 400);
  }

  let matcher: (name: string) => boolean;
  try {
    matcher = createHfNameMatcher(query, regex);
  } catch {
    return errorResponse("正規表現が不正です", 400);
  }

  const normalizedPath = normalizeRepositoryPath(searchParams.get("path"));
  if (normalizedPath === null) {
    return errorResponse("path 不正", 400);
  }
  const path = normalizedPath;
  const accountId = accountIdRaw?.trim() || undefined;

  let resolved: Awaited<ReturnType<typeof resolveTokenForUser>> = null;
  if (accountId) {
    try {
      resolved = await resolveTokenForUser(String(user.id), accountId);
    } catch {
      return errorResponse("HFアカウントを解決できませんでした", 503);
    }
    if (!resolved) {
      return errorResponse("HFアカウントへのアクセス権がありません", 403);
    }
  }

  const repoType = repoTypeRaw as RepoType;
  const limit = normalizeHfSearchLimit(
    Number(searchParams.get("limit") ?? undefined),
  );
  try {
    const entries = await withRetry(() =>
      listRepoTree(resolved?.token, repoId, repoType, path, {
        recursive: true,
      }),
    );
    const rootPath = buildHfPath({
      kind: "repo",
      accountId,
      repoType,
      repoId,
      subPath: path ?? "",
    });
    const resultsByPath = new Map<string, SearchResult>();
    const toVirtualPath = (entryPath: string) =>
      buildHfPath({
        kind: "repo",
        accountId,
        repoType,
        repoId,
        subPath: entryPath,
      });

    for (const entry of entries) {
      const fullPath = canonicalEntryPath(path ?? "", entry.path);
      if (!fullPath) continue;
      addDerivedDirectories(resultsByPath, path ?? "", fullPath, toVirtualPath);
      if (!matcher(entry.name)) continue;
      const virtualPath = toVirtualPath(fullPath);
      resultsByPath.set(fullPath, entrySearchResult(entry, virtualPath));
    }

    // Derived directories are only useful when their own name matches the
    // query.  Keep explicit entries' metadata while filtering synthetic ones.
    const matching = [...resultsByPath.values()].filter((result) =>
      result.kind === "directory" ? matcher(result.name) : true,
    );
    matching.sort(compareResults);
    const results = matching.slice(0, limit);
    const response: ExplorerSearchResponse = {
      success: true,
      results,
      total: matching.length,
      total_returned: results.length,
      root_path: rootPath,
      truncated: matching.length > results.length,
      query,
    };
    return NextResponse.json(response, { headers: PRIVATE_HEADERS });
  } catch {
    // Never reflect provider errors: they can contain a private token or a
    // signed CDN URL.  Keep this route's response safe for the browser UI.
    return errorResponse("HF検索に失敗しました", 502);
  }
}
