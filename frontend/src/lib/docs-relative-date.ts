export type DocsRelativeDateRange = { start: string; end: string };

function ymd(date: Date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function startOfLocalDay(date: Date) {
  const next = new Date(date);
  next.setHours(0, 0, 0, 0);
  return next;
}

function addDays(date: Date, days: number) {
  const next = new Date(date);
  next.setDate(next.getDate() + days);
  return next;
}

export function docsRelativeDateRange(value: unknown, now = new Date()): DocsRelativeDateRange | null | false {
  if (typeof value !== "string") return null;
  const key = value.trim();
  const today = startOfLocalDay(now);
  const day = today.getDay();
  const mondayOffset = day === 0 ? -6 : 1 - day;
  const thisWeekStart = addDays(today, mondayOffset);
  if (key === "this_week") return { start: ymd(thisWeekStart), end: ymd(addDays(thisWeekStart, 7)) };
  if (key === "next_week") return { start: ymd(addDays(thisWeekStart, 7)), end: ymd(addDays(thisWeekStart, 14)) };
  if (key === "past_7d") return { start: ymd(addDays(today, -7)), end: ymd(addDays(today, 1)) };
  if (key === "past_30d") return { start: ymd(addDays(today, -30)), end: ymd(addDays(today, 1)) };
  if (/^(this|next|past)_[a-z0-9_]+$/i.test(key)) return false;
  return null;
}
