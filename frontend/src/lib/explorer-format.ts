/**
 * ファイラーの日時表示フォーマット。
 * 更新日は日付だけでは同日の更新順が分からないため、時刻（時:分）まで表示する。
 */
export function formatExplorerDateTime(value?: string | null): string {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "-";
  return date.toLocaleString("ja-JP", {
    year: "numeric",
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}
