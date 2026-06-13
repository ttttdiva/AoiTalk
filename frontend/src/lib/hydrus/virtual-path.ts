/**
 * Hydrus 仮想パス: ファイラー内で Hydrus ファイルを一意識別するためのパス表現。
 *   "Hydrus|<fileId>"
 * ディレクトリ構造は持たず、検索結果を平坦に並べる。
 */

export const HYDRUS_PREFIX = "Hydrus";
export const HYDRUS_SEP = "|";

export function isHydrusPath(p: string): boolean {
  if (!p) return false;
  return p === HYDRUS_PREFIX || p.startsWith(HYDRUS_PREFIX + HYDRUS_SEP);
}

export function buildHydrusPath(fileId: number): string {
  return `${HYDRUS_PREFIX}${HYDRUS_SEP}${fileId}`;
}

export function parseHydrusFileId(p: string): number | null {
  if (!p.startsWith(HYDRUS_PREFIX + HYDRUS_SEP)) return null;
  const idStr = p.slice(HYDRUS_PREFIX.length + HYDRUS_SEP.length);
  const id = parseInt(idStr, 10);
  return Number.isFinite(id) ? id : null;
}
