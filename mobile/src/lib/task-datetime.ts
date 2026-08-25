function pad(value: number): string {
  return String(value).padStart(2, "0");
}

const DATE_ONLY_PATTERN = /^\d{4}-\d{2}-\d{2}$/;

export function isTaskDateOnlyInput(value: string | null | undefined): boolean {
  return typeof value === "string" && DATE_ONLY_PATTERN.test(value.trim());
}

/**
 * サーバーの `created_at` / `updated_at` と同じ表記（TZ指定なしのUTC）を返す。
 *
 * サーバーは `datetime.utcnow()` を TZ 指定なしで返す。端末側だけ `Z` 付きで
 * 保存すると、`Date.parse` が TZ 指定なしを端末ローカル時刻として解釈するため、
 * 同じタスクの作成日時がローカル読み出しとサーバー応答で最大±TZ分ずれ、
 * 一覧の並び順がリロードのたびに入れ替わる。
 */
export function nowServerNaiveIso(): string {
  return new Date().toISOString().replace(/Z$/, "");
}

export function toTaskWallClockIso(
  value: string | Date | null | undefined,
): string | null {
  if (!value) return null;
  if (typeof value === "string") {
    const trimmed = value.trim();
    if (!trimmed) return null;
    const match = trimmed.match(
      /^(\d{4})-(\d{2})-(\d{2})(?:[T\s](\d{2}):(\d{2})(?::(\d{2})(?:\.\d{1,3})?)?)?(?:Z|[+-]\d{2}:?\d{2})?$/,
    );
    if (match) {
      const [, year, month, day, hour = "00", minute = "00", second = "00"] =
        match;
      return `${year}-${month}-${day}T${hour}:${minute}:${second}`;
    }
  }
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(
    date.getDate(),
  )}T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(
    date.getSeconds(),
  )}`;
}
