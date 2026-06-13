/**
 * HF リポジトリ直下の `creator_mapping.json` を読み込むローダー。
 * 60_huggingface-sync 互換のフォーマット:
 *   { "<folder_name>": { "name"?: string, "platform"?: string, "tags"?: string[] } }
 *
 * creator(フォルダ)単位のタグ辞書として扱い、ファイラーで絞り込み検索に使う。
 */

import type { RepoType } from "./virtual-path";

export interface CreatorMappingEntry {
  name?: string;
  platform?: string;
  tags?: string[];
  [key: string]: unknown;
}

export type CreatorMapping = Record<string, CreatorMappingEntry>;

const TTL_MS = 60 * 60 * 1000; // 1時間
const cache = new Map<
  string,
  { data: CreatorMapping | null; fetchedAt: number }
>();

function repoKey(
  accountId: string | undefined,
  repoType: RepoType,
  repoId: string,
): string {
  return `${accountId ?? ""}|${repoType}|${repoId}`;
}

export async function fetchCreatorMapping(params: {
  accountId?: string;
  repoType: RepoType;
  repoId: string;
}): Promise<CreatorMapping | null> {
  const key = repoKey(params.accountId, params.repoType, params.repoId);
  const now = Date.now();
  const cached = cache.get(key);
  if (cached && now - cached.fetchedAt < TTL_MS) return cached.data;

  const qs = new URLSearchParams();
  if (params.accountId) qs.set("accountId", params.accountId);
  qs.set("repoId", params.repoId);
  qs.set("repoType", params.repoType);
  qs.set("path", "creator_mapping.json");
  qs.set("mode", "text");

  try {
    const res = await fetch(`/api/huggingface/file?${qs.toString()}`, {
      credentials: "include",
    });
    if (!res.ok) {
      cache.set(key, { data: null, fetchedAt: now });
      return null;
    }
    const j = (await res.json()) as { text?: string };
    const text = j.text ?? "";
    const parsed = JSON.parse(text);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      const mapping = parsed as CreatorMapping;
      cache.set(key, { data: mapping, fetchedAt: now });
      return mapping;
    }
    cache.set(key, { data: null, fetchedAt: now });
    return null;
  } catch {
    cache.set(key, { data: null, fetchedAt: now });
    return null;
  }
}

/** 指定フォルダ名が検索クエリにマッチするか（creator_mapping 経由でタグ/表示名/プラットフォームも見る） */
export function creatorMatchesQuery(
  mapping: CreatorMapping | null,
  folderName: string,
  query: string,
): boolean {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  if (folderName.toLowerCase().includes(q)) return true;
  const entry = mapping?.[folderName];
  if (!entry) return false;
  if (entry.name && entry.name.toLowerCase().includes(q)) return true;
  if (entry.platform && entry.platform.toLowerCase().includes(q)) return true;
  if (entry.tags && entry.tags.some((t) => t.toLowerCase().includes(q)))
    return true;
  return false;
}
