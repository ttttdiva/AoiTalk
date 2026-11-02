function pad(value: number): string {
  return String(value).padStart(2, "0");
}

const DATE_ONLY_PATTERN = /^\d{4}-\d{2}-\d{2}$/;

export function isTaskDateOnlyInput(value: string | null | undefined): boolean {
  return typeof value === "string" && DATE_ONLY_PATTERN.test(value.trim());
}

export function inferTaskAllDay(
  explicitAllDay: boolean,
  startAt: string | null | undefined,
  endAt: string | null | undefined,
): boolean {
  if (explicitAllDay) return true;
  const providedValues = [startAt, endAt].filter(
    (value): value is string => typeof value === "string" && value.trim() !== "",
  );
  return (
    providedValues.length > 0 && providedValues.every(isTaskDateOnlyInput)
  );
}

export function taskAllDayFromStoredRecord(
  allDay: boolean | null | undefined,
): boolean {
  return Boolean(allDay);
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

export function buildTaskSchedulePayload(
  explicitAllDay: boolean,
  startAt: string | null | undefined,
  endAt: string | null | undefined,
): { start_at: string | null; end_at: string | null; all_day: boolean } {
  return {
    start_at: toTaskWallClockIso(startAt),
    end_at: toTaskWallClockIso(endAt),
    all_day: inferTaskAllDay(explicitAllDay, startAt, endAt),
  };
}

export function buildStoredTaskSchedulePayload(
  allDay: boolean | null | undefined,
  startAt: string | null | undefined,
  endAt: string | null | undefined,
): { start_at: string | null; end_at: string | null; all_day: boolean } {
  return buildTaskSchedulePayload(
    taskAllDayFromStoredRecord(allDay),
    startAt,
    endAt,
  );
}
