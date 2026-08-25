import type {
  RecurrencePreviewConfig,
  RecurrenceSkipMode,
} from "@/lib/recurrence-preview";

export const RECURRENCE_FREQ_OPTIONS = [
  { value: "DAILY", label: "毎日" },
  { value: "WEEKLY", label: "毎週" },
  { value: "MONTHLY", label: "毎月" },
  { value: "YEARLY", label: "毎年" },
] as const;

export const RECURRENCE_WEEKDAYS = [
  { key: "MO", label: "月" },
  { key: "TU", label: "火" },
  { key: "WE", label: "水" },
  { key: "TH", label: "木" },
  { key: "FR", label: "金" },
  { key: "SA", label: "土" },
  { key: "SU", label: "日" },
] as const;

/**
 * 土日・祝日スキップに当たった回の扱い。
 * 「次の平日にずらす」と「その回は実施しない」をユーザーが選べるようにする。
 */
export const RECURRENCE_SKIP_MODE_OPTIONS: readonly {
  value: RecurrenceSkipMode;
  label: string;
}[] = [
  { value: "shift_forward", label: "後ろにずらす（翌営業日）" },
  { value: "omit", label: "実施しない（その回を飛ばす）" },
];

export type ParsedRrule = {
  freq: string;
  interval: number;
  byDay: string[];
  count: number | null;
  until: string | null;
};

export function recurrenceLabel(
  freq: string,
  interval: number,
  byDay: string[],
): string {
  const freqLabel =
    RECURRENCE_FREQ_OPTIONS.find((option) => option.value === freq)?.label ??
    freq;
  if (interval === 1) {
    if (freq === "WEEKLY" && byDay.length > 0) {
      const dayLabels = byDay
        .map(
          (day) =>
            RECURRENCE_WEEKDAYS.find((weekday) => weekday.key === day)?.label ??
            day,
        )
        .join("・");
      return `毎週 ${dayLabels}`;
    }
    return freqLabel;
  }
  const unit =
    { DAILY: "日", WEEKLY: "週", MONTHLY: "月", YEARLY: "年" }[freq] ?? "";
  return `${interval}${unit}ごと`;
}

/**
 * 「週末をスキップ」を設定できる繰り返し頻度かどうか。
 * WEEKLY で曜日を明示指定している場合はその曜日指定が優先されるため対象外にする。
 * それ以外（毎日・毎月・毎年・曜日未指定の毎週）はすべて設定できる。
 */
export function supportsSkipWeekend(freq: string, byDay: string[]): boolean {
  return !(freq === "WEEKLY" && byDay.length > 0);
}

export function buildRrule(
  freq: string,
  interval: number,
  byDay: string[],
  count: number | null,
  until: string | null,
): string {
  let rrule = `FREQ=${freq};INTERVAL=${interval}`;
  if (freq === "WEEKLY" && byDay.length > 0) {
    rrule += `;BYDAY=${byDay.join(",")}`;
  }
  if (count && count > 0) {
    rrule += `;COUNT=${count}`;
  }
  if (until) {
    const date = new Date(until);
    if (!isNaN(date.getTime())) {
      const year = date.getFullYear();
      const month = String(date.getMonth() + 1).padStart(2, "0");
      const day = String(date.getDate()).padStart(2, "0");
      rrule += `;UNTIL=${year}${month}${day}T235959Z`;
    }
  }
  return rrule;
}

export function parseRrule(rrule: string): ParsedRrule {
  const result: ParsedRrule = {
    freq: "DAILY",
    interval: 1,
    byDay: [],
    count: null,
    until: null,
  };
  for (const part of rrule.split(";")) {
    const [rawKey, rawValue] = part.split("=");
    const key = rawKey?.trim().toUpperCase();
    const value = rawValue?.trim();
    if (!key || !value) continue;
    switch (key) {
      case "FREQ":
        result.freq = value.toUpperCase();
        break;
      case "INTERVAL":
        result.interval = Number(value) || 1;
        break;
      case "BYDAY":
        result.byDay = value
          .split(",")
          .map((day) => day.trim().toUpperCase())
          .filter(Boolean);
        break;
      case "COUNT":
        result.count = Number(value) || null;
        break;
      case "UNTIL":
        if (value.length >= 8) {
          result.until = `${value.slice(0, 4)}-${value.slice(4, 6)}-${value.slice(6, 8)}`;
        }
        break;
    }
  }
  return result;
}

export function recurrenceEndDateInputValue(
  value: string | null | undefined,
): string | null {
  if (!value) return null;
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return value;
  const date = new Date(value);
  if (isNaN(date.getTime())) return null;
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

export function estimateOccurrenceCount(
  anchor: Date,
  rangeEnd: Date,
  config: Pick<RecurrencePreviewConfig, "freq" | "interval" | "byDay">,
): number {
  const interval = Math.max(1, config.interval || 1);
  const diffDays = Math.max(
    0,
    Math.ceil((rangeEnd.getTime() - anchor.getTime()) / (1000 * 60 * 60 * 24)),
  );
  switch (config.freq) {
    case "DAILY":
      return Math.min(2000, Math.max(16, Math.ceil(diffDays / interval) + 8));
    case "WEEKLY": {
      const byDayCount = Math.max(1, config.byDay.length);
      return Math.min(
        1000,
        Math.max(16, Math.ceil(diffDays / (7 * interval)) * byDayCount + 8),
      );
    }
    case "MONTHLY":
      return Math.min(
        240,
        Math.max(12, Math.ceil(diffDays / (28 * interval)) + 8),
      );
    case "YEARLY":
      return Math.min(
        64,
        Math.max(8, Math.ceil(diffDays / (365 * interval)) + 4),
      );
    default:
      return 180;
  }
}
