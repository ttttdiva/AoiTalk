/** Presentation helpers keep persisted task values unchanged. */
export function taskRecurrenceLabel(rrule?: string | null): string {
  if (!rrule) return "なし";
  const parts = Object.fromEntries(
    rrule
      .replace(/^RRULE:/i, "")
      .split(";")
      .map((part) => part.split("=")),
  );
  const interval = Math.max(1, Number(parts.INTERVAL) || 1);
  const unit = (
    { DAILY: "日", WEEKLY: "週", MONTHLY: "か月", YEARLY: "年" } as Record<
      string,
      string
    >
  )[parts.FREQ];
  if (!unit) return "カスタム";
  const base =
    interval === 1
      ? (
          {
            DAILY: "毎日",
            WEEKLY: "毎週",
            MONTHLY: "毎月",
            YEARLY: "毎年",
          } as Record<string, string>
        )[parts.FREQ]
      : `${interval}${unit}ごと`;
  return Object.keys(parts).some((key) => !["FREQ", "INTERVAL"].includes(key))
    ? `${base}・詳細設定あり`
    : base;
}

export function taskReminderLabel(
  enabled: boolean,
  offsets: readonly number[],
  hasDate: boolean,
): string {
  if (!enabled) return "オフ";
  const timing = offsets.length
    ? [...offsets]
        .sort((a, b) => a - b)
        .map((value) => {
          if (value === 0) return "予定時刻";
          if (value % 1440 === 0) return `${value / 1440}日前`;
          if (value % 60 === 0) return `${value / 60}時間前`;
          return `${value}分前`;
        })
        .join("・")
    : "標準設定";
  return hasDate
    ? timing
    : `開始日未設定${offsets.length ? `・${timing}` : ""}`;
}

export function taskDateWithAllDay(value: string, allDay: boolean): string {
  if (!value) return "";
  return allDay
    ? value.split("T")[0]
    : value.includes("T")
      ? value
      : `${value}T09:00`;
}
