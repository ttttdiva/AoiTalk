function pad(value: number): string {
  return value.toString().padStart(2, "0");
}

function formatAbsoluteDate(date: Date, style: "short" | "long"): string {
  if (style === "short") {
    return `${pad(date.getMonth() + 1)}/${pad(date.getDate())}`;
  }
  return `${date.getFullYear()}/${pad(date.getMonth() + 1)}/${pad(date.getDate())}`;
}

function formatTime(date: Date): string {
  return `${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function formatWeekday(date: Date): string {
  return new Intl.DateTimeFormat("en-US", { weekday: "long" }).format(date);
}

export function formatTaskDateLabel(
  value: string | null | undefined,
  options?: {
    allDay?: boolean;
    absoluteStyle?: "short" | "long";
  },
): string {
  if (!value) return "";

  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";

  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const target = new Date(date.getFullYear(), date.getMonth(), date.getDate());
  const diffDays = Math.round(
    (target.getTime() - today.getTime()) / (1000 * 60 * 60 * 24),
  );

  const hasTime = !(
    options?.allDay ||
    (date.getHours() === 0 && date.getMinutes() === 0)
  );

  let dateLabel: string;
  if (diffDays === 0) dateLabel = "Today";
  else if (diffDays === 1) dateLabel = "Tomorrow";
  else if (diffDays === -1) dateLabel = "Yesterday";
  else if (diffDays >= -3 && diffDays <= -2)
    dateLabel = `${Math.abs(diffDays)} days ago`;
  else if (diffDays >= 2 && diffDays <= 7) dateLabel = formatWeekday(date);
  else dateLabel = formatAbsoluteDate(date, options?.absoluteStyle ?? "long");

  return hasTime ? `${dateLabel}, ${formatTime(date)}` : dateLabel;
}
