/**
 * HF リポジトリをファイラーの ExplorerListResponse 形式に変換するローダー。
 * ExplorerContext.fetchDirectory から呼び出される。
 */

import type { ExplorerListResponse } from "@/lib/explorer-api";
import {
  HF_PREFIX,
  buildHfPath,
  inferMediaType,
  parseHfPath,
  type HfVirtualPath,
} from "./virtual-path";

async function jsonFetch<T>(url: string): Promise<T> {
  const res = await fetch(url, { credentials: "include" });
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
