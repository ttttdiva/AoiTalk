import type { Task } from "../types/api";

type CalendarTask = Pick<
  Task,
  | "title"
  | "description"
  | "start_at"
  | "end_at"
  | "all_day"
  | "reminder_offsets"
  | "project_name"
>;

function parseDate(value: string | null | undefined): Date | null {
  if (!value) return null;
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

function pad(value: number): string {
  return String(value).padStart(2, "0");
}

function formatUtcDateTime(value: Date): string {
  return (
    [
      value.getUTCFullYear(),
      pad(value.getUTCMonth() + 1),
      pad(value.getUTCDate()),
    ].join("") +
    "T" +
    [
      pad(value.getUTCHours()),
      pad(value.getUTCMinutes()),
      pad(value.getUTCSeconds()),
    ].join("") +
    "Z"
  );
}

function formatAllDayDate(value: Date): string {
  return [
    value.getFullYear(),
    pad(value.getMonth() + 1),
    pad(value.getDate()),
  ].join("");
}

function addDays(value: Date, days: number): Date {
  const next = new Date(value);
  next.setDate(next.getDate() + days);
  return next;
}

function buildDetails(task: CalendarTask): string {
  const lines: string[] = [];
  const description = task.description?.trim();
  if (description) {
    lines.push(description);
  }
  if (task.project_name) {
    lines.push(`Project: ${task.project_name}`);
  }
  if (task.reminder_offsets.length > 0) {
    lines.push(
      `AoiTalk reminder presets: ${task.reminder_offsets.map((offset) => `${offset}m`).join(", ")}`,
    );
  }
  return lines.join("\n\n");
}

function resolveDates(task: CalendarTask): string | null {
  const start = parseDate(task.start_at) ?? parseDate(task.end_at);
  if (!start) return null;

  const fallbackEnd = task.all_day
    ? addDays(start, 1)
    : new Date(start.getTime() + 60 * 60 * 1000);
  let end = parseDate(task.end_at) ?? fallbackEnd;
  if (end.getTime() <= start.getTime()) {
    end = fallbackEnd;
  }

  if (task.all_day) {
    const startDate = formatAllDayDate(start);
    const endDate = formatAllDayDate(addDays(end, 1));
    return `${startDate}/${endDate}`;
  }

  return `${formatUtcDateTime(start)}/${formatUtcDateTime(end)}`;
}

function resolveTimezone(): string | null {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || null;
  } catch {
    return null;
  }
}

export function canCreateGoogleCalendarEvent(task: CalendarTask): boolean {
  return Boolean(resolveDates(task));
}

export function buildGoogleCalendarEventUrl(task: CalendarTask): string | null {
  const dates = resolveDates(task);
  if (!dates) return null;

  const params = new URLSearchParams({
    action: "TEMPLATE",
    text: task.title,
    dates,
  });

  const details = buildDetails(task);
  if (details) {
    params.set("details", details);
  }

  const timezone = resolveTimezone();
  if (timezone) {
    params.set("ctz", timezone);
  }

  return `https://calendar.google.com/calendar/render?${params.toString()}`;
}
