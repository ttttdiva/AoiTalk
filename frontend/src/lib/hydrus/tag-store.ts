/**
 * Hydrus 検索タグの使用履歴・ブックマークを localStorage に永続化する。
 *
 * ファイル/フォルダ用の既存ブックマーク（サーバー側 /explorer/bookmarks）とは
 * 別機能として扱い、ストレージも共有しない。
 *
 * タグ文字列は "system:import time: since 3 days ago" のように空白を含むため、
 * 分割・正規化はせず元の文字列をそのまま保持する。
 */

const HISTORY_KEY = "hydrus-tag-history";
const BOOKMARK_KEY = "hydrus-tag-bookmarks";
const SEARCH_TAGS_KEY = "hydrus-search-tags";
const HISTORY_LIMIT = 50;

/** Hydrus タブを初めて開いた時に適用する検索条件（空白を含む単一タグ） */
export const HYDRUS_DEFAULT_SEARCH_TAGS = [
  "system:import time: since 3 days ago",
];

function readTags(key: string): string[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((t): t is string => typeof t === "string" && t !== "");
  } catch {
    return [];
  }
}

function writeTags(key: string, tags: string[]): void {
  if (typeof window === "undefined") return;
  try {
    localStorage.setItem(key, JSON.stringify(tags));
  } catch {
    /* ignore */
  }
}

/**
 * 現在の検索条件。未保存（＝Hydrus タブを一度も使っていない）なら null を返し、
 * 呼び出し側が初期条件 HYDRUS_DEFAULT_SEARCH_TAGS を適用する。
 * ユーザーが条件を変更した後はその内容を保存し、タブ再表示時に初期条件へ戻さない。
 */
export function readSearchTags(): string[] | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = localStorage.getItem(SEARCH_TAGS_KEY);
    if (raw === null) return null;
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return null;
    return parsed.filter((t): t is string => typeof t === "string" && t !== "");
  } catch {
    return null;
  }
}

export function writeSearchTags(tags: string[]): void {
  writeTags(SEARCH_TAGS_KEY, tags);
}

export function readTagHistory(): string[] {
  return readTags(HISTORY_KEY);
}

/** 使用したタグを履歴先頭へ。既出タグは重複させず先頭へ繰り上げる。 */
export function pushTagHistory(tags: string[]): string[] {
  const current = readTagHistory();
  let next = current;
  for (const tag of tags) {
    if (!tag) continue;
    next = [tag, ...next.filter((t) => t !== tag)];
  }
  next = next.slice(0, HISTORY_LIMIT);
  writeTags(HISTORY_KEY, next);
  return next;
}

export function removeTagHistory(tag: string): string[] {
  const next = readTagHistory().filter((t) => t !== tag);
  writeTags(HISTORY_KEY, next);
  return next;
}

export function clearTagHistory(): string[] {
  writeTags(HISTORY_KEY, []);
  return [];
}

export function readTagBookmarks(): string[] {
  return readTags(BOOKMARK_KEY);
}

/** ブックマーク登録/解除のトグル。登録は末尾追加で登録順を保つ。 */
export function toggleTagBookmark(tag: string): string[] {
  if (!tag) return readTagBookmarks();
  const current = readTagBookmarks();
  const next = current.includes(tag)
    ? current.filter((t) => t !== tag)
    : [...current, tag];
  writeTags(BOOKMARK_KEY, next);
  return next;
}
