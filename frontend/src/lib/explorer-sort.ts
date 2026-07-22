/**
 * ファイラー共通のソート定義。
 * FileGrid / FileList / キーボードショートカット(F8/F9) が同じ並び順を共有する。
 *
 * フォルダとファイルは呼び出し側で別配列のまま扱い、それぞれを個別にソートする
 * （フォルダ先頭表示を維持するため）。
 */

import type { ExplorerDirectory, ExplorerFile } from "@/lib/explorer-api";

export type SortKey = "name" | "size" | "date";
export type SortDir = "asc" | "desc";

export const DEFAULT_SORT_KEY: SortKey = "name";
export const DEFAULT_SORT_DIR: SortDir = "asc";

export function isSortKey(value: string | null): value is SortKey {
  return value === "name" || value === "size" || value === "date";
}

export function isSortDir(value: string | null): value is SortDir {
  return value === "asc" || value === "desc";
}

/** 日時文字列を epoch ms へ。値なし・パース不能は null（＝末尾送り対象） */
function timeValue(raw?: string): number | null {
  if (!raw) return null;
  const parsed = Date.parse(raw);
  return Number.isNaN(parsed) ? null : parsed;
}

function compareName(a: string, b: string): number {
  return a.localeCompare(b, "ja", { numeric: true, sensitivity: "base" });
}

/**
 * 日時比較。日時を持たない項目はソート方向に関わらず常に末尾へ置く。
 * 同着・両方日時なしの場合は名前昇順へフォールバックして安定させる。
 */
function compareDate(
  a: ExplorerDirectory | ExplorerFile,
  b: ExplorerDirectory | ExplorerFile,
  mul: number,
): number {
  const ta = timeValue(a.modified_at);
  const tb = timeValue(b.modified_at);
  if (ta === null && tb === null) return compareName(a.name, b.name);
  if (ta === null) return 1;
  if (tb === null) return -1;
  if (ta === tb) return compareName(a.name, b.name);
  return mul * (ta - tb);
}

export function sortExplorerDirectories(
  dirs: ExplorerDirectory[],
  key: SortKey,
  dir: SortDir,
): ExplorerDirectory[] {
  const mul = dir === "asc" ? 1 : -1;
  return [...dirs].sort((a, b) => {
    if (key === "date") return compareDate(a, b, mul);
    // ディレクトリは size を持たないため、size 指定時は名前順で安定させる
    if (key === "size") return compareName(a.name, b.name);
    return mul * compareName(a.name, b.name);
  });
}

export function sortExplorerFiles(
  files: ExplorerFile[],
  key: SortKey,
  dir: SortDir,
): ExplorerFile[] {
  const mul = dir === "asc" ? 1 : -1;
  return [...files].sort((a, b) => {
    if (key === "date") return compareDate(a, b, mul);
    if (key === "size") {
      const diff = (a.size || 0) - (b.size || 0);
      return diff === 0 ? compareName(a.name, b.name) : mul * diff;
    }
    return mul * compareName(a.name, b.name);
  });
}
