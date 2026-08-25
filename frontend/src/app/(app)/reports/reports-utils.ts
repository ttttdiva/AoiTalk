import { type CSSProperties } from "react";
import { type Task, type TimeEntry } from "@/lib/task-api";
import { type ProjectColorTokens } from "@/lib/project-colors";

export function formatSeconds(seconds: number): string {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (h > 0) return `${h}時間${m}分`;
  return `${m}分`;
}

export function formatHours(seconds: number): string {
  const h = seconds / 3600;
  return h.toFixed(1) + "h";
}

export type PeriodPreset = "this_week" | "this_month" | "custom";
export type ScopeMode = "project" | "space" | "all";
export type ReportsViewMode = "summary" | "timeline";

export type ReportsViewSettings = {
  active_view?: ReportsViewMode;
  scope?: ScopeMode;
  period?: PeriodPreset;
  custom_from?: string;
  custom_to?: string;
  week_offset?: number;
  show_schedule_frames?: boolean;
};

export function getWeekRangeFromDate(base: Date): {
  monday: Date;
  sunday: Date;
} {
  const day = base.getDay();
  const diff = base.getDate() - day + (day === 0 ? -6 : 1);
  const monday = new Date(base);
  monday.setDate(diff);
  monday.setHours(0, 0, 0, 0);
  const sunday = new Date(monday);
  sunday.setDate(monday.getDate() + 6);
  sunday.setHours(23, 59, 59, 999);
  return { monday, sunday };
}

export function getMonthRange(): { start: Date; end: Date } {
  const now = new Date();
  const start = new Date(now.getFullYear(), now.getMonth(), 1, 0, 0, 0, 0);
  const end = new Date(
    now.getFullYear(),
    now.getMonth() + 1,
    0,
    23,
    59,
    59,
    999,
  );
  return { start, end };
}

export const DAY_LABELS = ["月", "火", "水", "木", "金", "土", "日"];
export const HOUR_START = 7;
export const HOUR_END = 22;
export const TOTAL_HOURS = HOUR_END - HOUR_START;

export const DEFAULT_ENTRY_COLOR = "#94a3b8";

export function timelineBlockStyle(tokens: ProjectColorTokens): CSSProperties {
  return {
    background: tokens.surfaceGradient,
    borderColor: tokens.border,
    color: tokens.text,
    boxShadow: `inset 3px 0 ${tokens.stripe}, inset 0 1px rgba(255,255,255,0.42), 0 12px 28px -24px rgba(0,0,0,0.42)`,
    backdropFilter: "blur(12px) saturate(1.18)",
  };
}

export function formatTimeWindow(entry: TimeEntry): string {
  if (!entry.started_at) return "-";
  const start = new Date(entry.started_at);
  const startText = start.toLocaleTimeString("ja-JP", {
    hour: "2-digit",
    minute: "2-digit",
  });
  if (!entry.ended_at) return `${startText} - 計測中`;
  const end = new Date(entry.ended_at);
  return `${startText} - ${end.toLocaleTimeString("ja-JP", {
    hour: "2-digit",
    minute: "2-digit",
  })}`;
}

export function toLocalHM(date: Date): string {
  return `${String(date.getHours()).padStart(2, "0")}:${String(
    date.getMinutes(),
  ).padStart(2, "0")}`;
}

export function toLocalYMD(date: Date): string {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(
    2,
    "0",
  )}-${String(date.getDate()).padStart(2, "0")}`;
}

export function parseDateValue(value?: string | null): Date | null {
  if (!value) return null;
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

export function parseTimeInput(input: string): string | null {
  const trimmed = input.trim();
  if (!trimmed) return null;
  let m = trimmed.match(/^(\d{1,2}):(\d{2})$/);
  if (!m) m = trimmed.match(/^(\d{1,2})(\d{2})$/);
  if (!m) return null;
  const h = parseInt(m[1], 10);
  const mm = parseInt(m[2], 10);
  if (isNaN(h) || isNaN(mm) || h > 23 || mm > 59) return null;
  return `${String(h).padStart(2, "0")}:${String(mm).padStart(2, "0")}`;
}

export function parseDurationInput(input: string): number | null {
  const trimmed = input.trim();
  if (!trimmed) return null;
  const parts = trimmed.split(":");
  if (parts.length === 3) {
    const h = parseInt(parts[0], 10);
    const m = parseInt(parts[1], 10);
    const s = parseInt(parts[2], 10);
    if ([h, m, s].some(isNaN) || m > 59 || s > 59) return null;
    return h * 3600 + m * 60 + s;
  }
  if (parts.length === 2) {
    const a = parseInt(parts[0], 10);
    const b = parseInt(parts[1], 10);
    if ([a, b].some(isNaN) || b > 59) return null;
    return a * 3600 + b * 60;
  }
  if (parts.length === 1) {
    const n = parseInt(parts[0], 10);
    if (isNaN(n)) return null;
    return n * 60;
  }
  return null;
}

export function formatDurationInput(seconds: number): string {
  const sec = Math.max(0, Math.floor(seconds));
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

export function combineDateTime(ymd: string, hm: string): Date {
  return new Date(`${ymd}T${hm}:00`);
}

export function getWeekDays(monday: Date): Date[] {
  return Array.from({ length: 7 }, (_, i) => {
    const d = new Date(monday);
    d.setDate(monday.getDate() + i);
    return d;
  });
}

export function groupEntriesByDay(
  entries: TimeEntry[],
  weekDays: Date[],
): Map<string, TimeEntry[]> {
  const map = new Map<string, TimeEntry[]>();
  for (const d of weekDays) {
    map.set(toLocalYMD(d), []);
  }
  for (const entry of entries) {
    if (!entry.started_at) continue;
    const start = new Date(entry.started_at);
    const dateKey = toLocalYMD(start);
    if (map.has(dateKey)) {
      map.get(dateKey)!.push(entry);
    }
  }
  return map;
}

export function getEntryDurationSeconds(entry: TimeEntry, now: Date): number {
  if (!entry.started_at) return 0;
  const start = new Date(entry.started_at);
  const end = entry.ended_at ? new Date(entry.ended_at) : now;
  if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) return 0;
  return Math.max(0, Math.floor((end.getTime() - start.getTime()) / 1000));
}

export function getEntryHourRange(
  entry: TimeEntry,
  now: Date,
): { startHour: number; endHour: number } | null {
  if (!entry.started_at) return null;
  const start = new Date(entry.started_at);
  const end = entry.ended_at ? new Date(entry.ended_at) : now;
  if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) {
    return null;
  }
  return {
    startHour: start.getHours() + start.getMinutes() / 60,
    endHour: end.getHours() + end.getMinutes() / 60,
  };
}

export type EntryColumnLayout = {
  columnIndex: number;
  columnCount: number;
};

export function buildEntryColumnLayouts(
  entries: TimeEntry[],
  now: Date,
): Map<string, EntryColumnLayout> {
  const ranges = entries
    .map((entry) => {
      if (!entry.started_at) return null;
      const start = new Date(entry.started_at).getTime();
      const rawEnd = entry.ended_at
        ? new Date(entry.ended_at).getTime()
        : now.getTime();
      if (Number.isNaN(start) || Number.isNaN(rawEnd)) return null;
      return {
        entry,
        start,
        end: Math.max(rawEnd, start + 60 * 1000),
      };
    })
    .filter((range): range is NonNullable<typeof range> => range !== null)
    .sort((a, b) => a.start - b.start || a.end - b.end);

  const layouts = new Map<string, EntryColumnLayout>();
  let group: typeof ranges = [];
  let groupEnd = -Infinity;

  const flushGroup = () => {
    if (group.length === 0) return;
    const columns: number[] = [];
    const assigned = new Map<string, number>();
    for (const range of group) {
      let columnIndex = columns.findIndex((end) => end <= range.start);
      if (columnIndex === -1) {
        columnIndex = columns.length;
        columns.push(range.end);
      } else {
        columns[columnIndex] = range.end;
      }
      assigned.set(range.entry.id, columnIndex);
    }
    const columnCount = Math.max(columns.length, 1);
    for (const range of group) {
      layouts.set(range.entry.id, {
        columnIndex: assigned.get(range.entry.id) ?? 0,
        columnCount,
      });
    }
  };

  for (const range of ranges) {
    if (group.length > 0 && range.start >= groupEnd) {
      flushGroup();
      group = [];
      groupEnd = -Infinity;
    }
    group.push(range);
    groupEnd = Math.max(groupEnd, range.end);
  }
  flushGroup();

  return layouts;
}

export function isTaskScheduledInRange(
  task: Task,
  rangeStart: Date,
  rangeEnd: Date,
) {
  const start = parseDateValue(task.start_at);
  const end = parseDateValue(task.end_at);
  if (!start || !end || end <= start || task.all_day) return false;
  return end >= rangeStart && start <= rangeEnd;
}

export function getTaskScheduleSegmentForDay(task: Task, day: Date) {
  const start = parseDateValue(task.start_at);
  const end = parseDateValue(task.end_at);
  if (!start || !end || end <= start || task.all_day) return null;

  const dayStart = new Date(day);
  dayStart.setHours(0, 0, 0, 0);
  const dayEnd = new Date(dayStart);
  dayEnd.setDate(dayEnd.getDate() + 1);

  if (end <= dayStart || start >= dayEnd) return null;

  const segmentStart = start > dayStart ? start : dayStart;
  const segmentEnd = end < dayEnd ? end : dayEnd;

  return {
    startHour:
      segmentStart.getHours() +
      segmentStart.getMinutes() / 60 +
      segmentStart.getSeconds() / 3600,
    endHour:
      segmentEnd.getHours() +
      segmentEnd.getMinutes() / 60 +
      segmentEnd.getSeconds() / 3600,
  };
}

export function formatTaskScheduleLabel(task: Task): string {
  const start = parseDateValue(task.start_at);
  const end = parseDateValue(task.end_at);
  if (!start || !end) return task.title;
  const startText = start.toLocaleString("ja-JP", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
  const endText = end.toLocaleString("ja-JP", {
    hour: "2-digit",
    minute: "2-digit",
  });
  return `${task.title} ${startText} - ${endText}`;
}

export function getDayTotalSeconds(entries: TimeEntry[], now: Date): number {
  return entries.reduce((sum, e) => sum + getEntryDurationSeconds(e, now), 0);
}

export type ResizeState = {
  entryId: string;
  edge: "top" | "bottom";
  dayIndex: number;
  originalStartHour: number;
  originalEndHour: number;
  currentHour: number;
};

export type MoveState = {
  entryId: string;
  originalStartedAt: string;
  originalEndedAt: string;
  originalDayIndex: number;
  originalStartHour: number;
  durationHours: number;
  pointerOffsetHours: number;
  mouseStartX: number;
  mouseStartY: number;
  currentDayIndex: number;
  currentStartHour: number;
  moving: boolean;
};

export type CtxMenuState = {
  entry: TimeEntry;
  x: number;
  y: number;
};
