/**
 * HF リポジトリをファイラーの ExplorerListResponse 形式に変換するローダー。
 * ExplorerContext.fetchDirectory から呼び出される。
 */

import type {
  ExplorerDirectory,
  ExplorerListResponse,
  ExplorerSearchOptions,
  ExplorerSearchResponse,
  SearchResult,
} from "@/lib/explorer-api";
import {
  HF_PREFIX,
  buildHfPath,
  inferMediaType,
  parseHfPath,
  type HfVirtualPath,
} from "./virtual-path";
import { createHfNameMatcher, normalizeHfSearchLimit } from "./search";

async function jsonFetch<T>(url: string): Promise<T> {
  const res = await fetch(url, {
    credentials: "include",
    cache: "no-store",
  });
  if (!res.ok) {
    const t = await res.text().catch(() => "");
    throw new Error(`${res.status}: ${t || res.statusText}`);
  }
  return (await res.json()) as T;
}

interface AccountsResp {
  accounts: Array<{
    id: string;
    username: string;
    label: string;
    source: string;
  }>;
  references?: Array<{
    repoId: string;
    repoType: "model" | "dataset";
    accountId?: string;
  }>;
}

interface ReposResp {
  accountId: string;
  username: string;
  repos: Array<{
    id: string;
    name: string;
    owner: string;
    private: boolean;
    lastModified: string;
    type: "model" | "dataset";
    description?: string;
  }>;
}

interface TreeResp {
  repoId: string;
  repoType: "model" | "dataset";
  path: string;
  entries: Array<{
    path: string;
    name: string;
    size?: number;
    type: "file" | "directory";
    lastModified?: string;
  }>;
}

export type HfExplorerSearchOptions = ExplorerSearchOptions & {
  /**
   * The visible HF root repositories.  Root search is deliberately local:
   * fetching every repository solely to answer a name query would turn a
   * keyboard shortcut into an unbounded network fan-out.
   */
  rootDirectories?: readonly ExplorerDirectory[];
};

/**
 * HF 仮想パスを ExplorerListResponse に変換。
 * ルートなら全アカウントの全リポジトリを平坦化して返し、
 * リポジトリ内ならツリー API を叩いて変換する。
 */
export async function hfExplorerList(
  path: string,
): Promise<ExplorerListResponse> {
  const parsed: HfVirtualPath = parseHfPath(path) ?? { kind: "root" };

  if (parsed.kind === "root") {
    return loadRoot();
  }
  return loadRepoTree(parsed);
}

async function loadRoot(): Promise<ExplorerListResponse> {
  const accResp = await jsonFetch<AccountsResp>("/api/huggingface/accounts");
  const accounts = accResp.accounts ?? [];
  const references = accResp.references ?? [];

  const results = await Promise.all(
    accounts.map(async (a) => {
      try {
        const r = await jsonFetch<ReposResp>(
          `/api/huggingface/repos?accountId=${encodeURIComponent(a.id)}`,
        );
        return { acc: a, repos: r.repos ?? [] };
      } catch {
        return { acc: a, repos: [] as ReposResp["repos"] };
      }
    }),
  );

  const directories = results.flatMap(({ acc, repos }) =>
    repos.map((r) => {
      const suffix = r.type === "dataset" ? " (dataset)" : "";
      const displayName = `${acc.username}/${r.name}${suffix}`;
      return {
        name: displayName,
        path: buildHfPath({
          kind: "repo",
          accountId: acc.id,
          repoType: r.type,
          repoId: r.id,
          subPath: "",
        }),
        item_count: undefined as number | undefined,
        modified_at: r.lastModified || undefined,
      };
    }),
  );

  const seen = new Set(
    directories.map((directory) => {
      const parsed = parseHfPath(directory.path);
      return `${parsed?.repoType}:${parsed?.repoId?.toLowerCase()}`;
    }),
  );
  for (const reference of references) {
    const key = `${reference.repoType}:${reference.repoId.toLowerCase()}`;
    if (seen.has(key)) continue;
    seen.add(key);
    directories.push({
      name: `${reference.repoId}${reference.repoType === "dataset" ? " (dataset)" : ""}`,
      path: buildHfPath({
        kind: "repo",
        accountId: reference.accountId,
        repoType: reference.repoType,
        repoId: reference.repoId,
        subPath: "",
      }),
      item_count: undefined,
      modified_at: undefined,
    });
  }

  directories.sort((a, b) => {
    const ta = a.modified_at ? new Date(a.modified_at).getTime() : 0;
    const tb = b.modified_at ? new Date(b.modified_at).getTime() : 0;
    return tb - ta;
  });

  return {
    success: true,
    current_path: HF_PREFIX,
    parent_path: null,
    can_go_up: false,
    directories,
    files: [],
    total_items: directories.length,
  };
}

async function loadRepoTree(
  parsed: HfVirtualPath,
): Promise<ExplorerListResponse> {
  const qs = new URLSearchParams();
  if (parsed.accountId) qs.set("accountId", parsed.accountId);
  qs.set("repoId", parsed.repoId!);
  qs.set("repoType", parsed.repoType!);
  if (parsed.subPath) qs.set("path", parsed.subPath);

  const data = await jsonFetch<TreeResp>(
    `/api/huggingface/tree?${qs.toString()}`,
  );
  const entries = data.entries ?? [];

  const directories = entries
    .filter((e) => e.type === "directory")
    .map((e) => ({
      name: e.name,
      path: buildHfPath({ ...parsed, subPath: e.path }),
      item_count: undefined as number | undefined,
      modified_at: e.lastModified,
    }));

  const files = entries
    .filter((e) => e.type === "file")
    .map((e) => {
      const ext = e.name.includes(".")
        ? "." + e.name.split(".").pop()!.toLowerCase()
        : "";
      return {
        name: e.name,
        path: buildHfPath({ ...parsed, subPath: e.path }),
        type: inferMediaType(e.name),
        size: e.size,
        modified_at: e.lastModified,
        extension: ext,
      };
    });

  // 親パス
  let parent_path: string | null;
  if (!parsed.subPath) {
    parent_path = HF_PREFIX;
  } else {
    const idx = parsed.subPath.lastIndexOf("/");
    const parentSub = idx === -1 ? "" : parsed.subPath.slice(0, idx);
    parent_path = buildHfPath({ ...parsed, subPath: parentSub });
  }

  return {
    success: true,
    current_path: buildHfPath(parsed),
    parent_path,
    can_go_up: true,
    directories,
    files,
    total_items: directories.length + files.length,
  };
}

function normalizedHfPath(value: string | undefined): string {
  const segments = (value ?? "")
    .replace(/\\/g, "/")
    .replace(/^\/+|\/+$/g, "")
    .split("/")
    .filter(Boolean);
  if (
    segments.some(
      (segment) =>
        segment === ".." ||
        segment.includes("|") ||
        /[\u0000-\u001f\u007f]/.test(segment),
    )
  ) {
    throw new Error("HFパスが不正です");
  }
  return segments.filter((segment) => segment !== ".").join("/");
}

function asRootSearchResult(directory: ExplorerDirectory): SearchResult {
  return {
    name: directory.name,
    path: directory.path,
    kind: "directory",
    item_count: directory.item_count,
    modified_at: directory.modified_at,
  };
}

function rootSearch(
  query: string,
  limit: number,
  options: HfExplorerSearchOptions,
): ExplorerSearchResponse {
  const matcher = createHfNameMatcher(query, options.regex);
  const matching = (options.rootDirectories ?? [])
    .filter((directory) => matcher(directory.name))
    .map(asRootSearchResult);
  const results = matching.slice(0, limit);
  return {
    success: true,
    results,
    total: matching.length,
    total_returned: results.length,
    root_path: HF_PREFIX,
    truncated: matching.length > results.length,
    query: query.trim(),
  };
}

/**
 * Search the displayed HF root or one repository subtree using the same
 * ExplorerSearchResponse shape as local Files search.
 */
export async function hfExplorerSearch(
  path: string,
  query: string,
  limit?: number,
  options: HfExplorerSearchOptions = {},
): Promise<ExplorerSearchResponse> {
  const normalizedLimit = normalizeHfSearchLimit(limit);
  const parsed =
    path === HF_PREFIX ? { kind: "root" as const } : parseHfPath(path);
  if (!parsed) {
    throw new Error("HFパスが不正です");
  }
  if (parsed.kind === "root") {
    return rootSearch(query, normalizedLimit, options);
  }

  // Compile locally first so invalid regex patterns never result in a network
  // request.  The authenticated route repeats this validation as its trust
  // boundary; this also keeps root and repository search behavior aligned.
  createHfNameMatcher(query, options.regex);

  const qs = new URLSearchParams({
    q: query.trim(),
    repoId: parsed.repoId ?? "",
    repoType: parsed.repoType ?? "model",
    limit: String(normalizedLimit),
  });
  if (parsed.accountId) qs.set("accountId", parsed.accountId);
  const subPath = normalizedHfPath(parsed.subPath);
  if (subPath) qs.set("path", subPath);
  if (options.regex) qs.set("regex", "true");

  return jsonFetch<ExplorerSearchResponse>(
    `/api/huggingface/search?${qs.toString()}`,
  );
}
