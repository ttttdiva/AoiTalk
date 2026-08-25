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

/** Sidebar-facing representation. Older installations stored only string[];
 * records are deliberately derived from that shape so no existing bookmark is
 * lost during the UI migration. */
export interface HydrusTagBookmark {
  tag: string;
  name: string;
  sort_order: number;
}

/** Hydrus タブを初めて開いた時に適用する検索条件（空白を含む単一タグ） */
export const HYDRUS_DEFAULT_SEARCH_TAGS = [
  "system:import time: since 3 days ago",
];

function scopedKey(key: string, userId?: string | null): string {
  // Never read the old unscoped keys: doing so would briefly expose a previous
  // principal's Hydrus state after logout/login switching.
  const scope = userId && userId.trim() ? userId.trim() : "anonymous";
  return `${key}:${scope}`;
}

function readTags(key: string, userId?: string | null): string[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = localStorage.getItem(scopedKey(key, userId));
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((t): t is string => typeof t === "string" && t !== "");
  } catch {
    return [];
  }
}

function writeTags(key: string, tags: unknown[], userId?: string | null): void {
  if (typeof window === "undefined") return;
  try {
    localStorage.setItem(scopedKey(key, userId), JSON.stringify(tags));
  } catch {
    /* ignore */
  }
}

/**
 * 現在の検索条件。未保存（＝Hydrus タブを一度も使っていない）なら null を返し、
 * 呼び出し側が初期条件 HYDRUS_DEFAULT_SEARCH_TAGS を適用する。
 * ユーザーが条件を変更した後はその内容を保存し、タブ再表示時に初期条件へ戻さない。
 */
export function readSearchTags(userId?: string | null): string[] | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = localStorage.getItem(scopedKey(SEARCH_TAGS_KEY, userId));
    if (raw === null) return null;
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return null;
    return parsed.filter((t): t is string => typeof t === "string" && t !== "");
  } catch {
    return null;
  }
}

export function writeSearchTags(tags: string[], userId?: string | null): void {
  writeTags(SEARCH_TAGS_KEY, tags, userId);
}

export function readTagHistory(userId?: string | null): string[] {
  return readTags(HISTORY_KEY, userId);
}

/** 使用したタグを履歴先頭へ。既出タグは重複させず先頭へ繰り上げる。 */
export function pushTagHistory(tags: string[], userId?: string | null): string[] {
  const current = readTagHistory(userId);
  let next = current;
  for (const tag of tags) {
    if (!tag) continue;
    next = [tag, ...next.filter((t) => t !== tag)];
  }
  next = next.slice(0, HISTORY_LIMIT);
  writeTags(HISTORY_KEY, next, userId);
  return next;
}

export function removeTagHistory(tag: string, userId?: string | null): string[] {
  const next = readTagHistory(userId).filter((t) => t !== tag);
  writeTags(HISTORY_KEY, next, userId);
  return next;
}

export function clearTagHistory(userId?: string | null): string[] {
  writeTags(HISTORY_KEY, [], userId);
  return [];
}

export function readTagBookmarks(userId?: string | null): string[] {
  return readTagBookmarkRecords(userId).map((record) => record.tag);
}

export function readTagBookmarkRecords(
  userId?: string | null,
): HydrusTagBookmark[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = localStorage.getItem(scopedKey(BOOKMARK_KEY, userId));
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed
      .map((entry, index): HydrusTagBookmark | null => {
        if (typeof entry === "string" && entry) {
          return { tag: entry, name: entry, sort_order: index };
        }
        if (!entry || typeof entry !== "object") return null;
        const value = entry as Record<string, unknown>;
        const tag = typeof value.tag === "string" ? value.tag : "";
        if (!tag) return null;
        const name = typeof value.name === "string" && value.name
          ? value.name
          : tag;
        const sortOrder = typeof value.sort_order === "number"
          ? value.sort_order
          : index;
        return { tag, name, sort_order: sortOrder };
      })
      .filter((entry): entry is HydrusTagBookmark => entry !== null)
      .sort((a, b) => a.sort_order - b.sort_order)
      .map((entry, index) => ({ ...entry, sort_order: index }));
  } catch {
    return [];
  }
}

function writeTagBookmarkRecords(
  records: HydrusTagBookmark[],
  userId?: string | null,
): HydrusTagBookmark[] {
  const normalized = records
    .filter((record) => record.tag.trim())
    .map((record, index) => ({
      tag: record.tag,
      name: record.name.trim() || record.tag,
      sort_order: index,
    }));
  writeTags(BOOKMARK_KEY, normalized, userId);
  if (typeof window !== "undefined") {
    window.dispatchEvent(
      new CustomEvent("hydrus-tag-bookmarks-changed", {
        detail: { userId: userId ?? null, bookmarks: normalized },
      }),
    );
  }
  return normalized;
}

export function renameTagBookmark(
  tag: string,
  name: string,
  userId?: string | null,
): HydrusTagBookmark[] {
  const next = readTagBookmarkRecords(userId).map((record) =>
    record.tag === tag ? { ...record, name: name.trim() || record.tag } : record,
  );
  return writeTagBookmarkRecords(next, userId);
}

export function removeTagBookmark(
  tag: string,
  userId?: string | null,
): HydrusTagBookmark[] {
  return writeTagBookmarkRecords(
    readTagBookmarkRecords(userId).filter((record) => record.tag !== tag),
    userId,
  );
}

export function reorderTagBookmarks(
  tags: string[],
  userId?: string | null,
): HydrusTagBookmark[] {
  const byTag = new Map(readTagBookmarkRecords(userId).map((record) => [record.tag, record]));
  const ordered = tags.map((tag) => byTag.get(tag)).filter(
    (record): record is HydrusTagBookmark => record != null,
  );
  for (const record of byTag.values()) {
    if (!ordered.some((item) => item.tag === record.tag)) ordered.push(record);
  }
  return writeTagBookmarkRecords(ordered, userId);
}

/** ブックマーク登録/解除のトグル。登録は末尾追加で登録順を保つ。 */
export function toggleTagBookmark(tag: string, userId?: string | null): string[] {
  if (!tag) return readTagBookmarks(userId);
  const current = readTagBookmarkRecords(userId);
  const next = current.some((record) => record.tag === tag)
    ? current.filter((record) => record.tag !== tag)
    : [...current, { tag, name: tag, sort_order: current.length }];
  const records = writeTagBookmarkRecords(next, userId);
  return records.map((record) => record.tag);
}
