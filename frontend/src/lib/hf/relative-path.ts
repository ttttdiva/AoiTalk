/**
 * HF リポジトリ内の相対パス検証。
 * upload / delete のルートハンドラで同じ規則を使うために切り出したもの。
 *
 * - `\` を `/` に正規化し、前後のスラッシュを落とす
 * - 空セグメント / `.` / `..` / NUL を含むものは不正
 * - 1024 文字を上限とする
 */

const MAX_RELATIVE_PATH_LENGTH = 1024;

export function normalizeRelativePath(value: string): string | null {
  const normalized = value.replace(/\\/g, "/").replace(/^\/+|\/+$/g, "");
  const segments = normalized.split("/");
  if (
    !normalized ||
    normalized.length > MAX_RELATIVE_PATH_LENGTH ||
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
