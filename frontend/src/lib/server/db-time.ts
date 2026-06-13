import { sql, type SQL } from "drizzle-orm";

export type DbTimestampValue = Date | string | null | undefined;

type WallClockParts = {
  year: number;
  month: number;
  day: number;
  hour: number;
  minute: number;
  second: number;
  millisecond: number;
};

const WALL_CLOCK_TIMESTAMP_PATTERN =
  /^(\d{4})-(\d{2})-(\d{2})(?:[T\s](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,6}))?)?)?(?:Z|[+-]\d{2}:?\d{2})?$/;

function pad(value: number, length = 2): string {
  return String(value).padStart(length, "0");
}

function assertValidDate(value: Date): void {
  if (Number.isNaN(value.getTime())) {
    throw new Error("Invalid timestamp");
  }
}

function partsFromDate(value: Date): WallClockParts {
  assertValidDate(value);
  return {
    year: value.getFullYear(),
    month: value.getMonth() + 1,
    day: value.getDate(),
    hour: value.getHours(),
    minute: value.getMinutes(),
    second: value.getSeconds(),
    millisecond: value.getMilliseconds(),
  };
}

function partsFromString(value: string): WallClockParts {
  const trimmed = value.trim();
  if (!trimmed) {
    throw new Error("Invalid timestamp");
  }

  const match = trimmed.match(WALL_CLOCK_TIMESTAMP_PATTERN);
  if (!match) {
    const parsed = new Date(trimmed);
    if (Number.isNaN(parsed.getTime())) throw new Error("Invalid timestamp");
    return partsFromDate(parsed);
  }

  const [
    ,
    year,
    month,
    day,
    hour = "0",
    minute = "0",
    second = "0",
    fraction = "0",
  ] = match;
  const parts = {
    year: Number(year),
    month: Number(month),
    day: Number(day),
    hour: Number(hour),
    minute: Number(minute),
    second: Number(second),
    millisecond: Number(fraction.slice(0, 3).padEnd(3, "0")),
  };

  const date = new Date(
    parts.year,
    parts.month - 1,
    parts.day,
    parts.hour,
    parts.minute,
    parts.second,
    parts.millisecond,
  );
  if (
    date.getFullYear() !== parts.year ||
    date.getMonth() !== parts.month - 1 ||
    date.getDate() !== parts.day ||
    date.getHours() !== parts.hour ||
    date.getMinutes() !== parts.minute ||
    date.getSeconds() !== parts.second ||
    date.getMilliseconds() !== parts.millisecond
  ) {
    throw new Error("Invalid timestamp");
  }
  return parts;
}

function partsFromTimestamp(value: Date | string): WallClockParts {
  return value instanceof Date ? partsFromDate(value) : partsFromString(value);
}

function dateFromParts(parts: WallClockParts): Date {
  return new Date(
    parts.year,
    parts.month - 1,
    parts.day,
    parts.hour,
    parts.minute,
    parts.second,
    parts.millisecond,
  );
}

function formatIsoTimestampParts(parts: WallClockParts): string {
  const base = `${pad(parts.year, 4)}-${pad(parts.month)}-${pad(
    parts.day,
  )}T${pad(parts.hour)}:${pad(parts.minute)}:${pad(parts.second)}`;
  return parts.millisecond > 0 ? `${base}.${pad(parts.millisecond, 3)}` : base;
}

function formatDbTimestampParts(parts: WallClockParts): string {
  return `${pad(parts.year, 4)}-${pad(parts.month)}-${pad(parts.day)} ${pad(
    parts.hour,
  )}:${pad(parts.minute)}:${pad(parts.second)}.${pad(parts.millisecond, 3)}`;
}

export function parseDbTimestampOutput(value: string): string {
  return formatIsoTimestampParts(partsFromString(value));
}

export function serializeDbTimestampInput(value: Date | string): string {
  return formatDbTimestampParts(partsFromTimestamp(value));
}

export function formatDbLocalTimestamp(value: Date | string): string {
  return serializeDbTimestampInput(value);
}

export function toDbLocalTimestamp(value: Date | string): SQL {
  return sql`${serializeDbTimestampInput(value)}::timestamp`;
}

export function toDbCurrentLocalTimestamp(): SQL {
  return sql`localtimestamp`;
}

export function parseInputDate(value: string): Date {
  return dateFromParts(partsFromString(value));
}

export function dbTimestampToLocalDate(value: DbTimestampValue): Date | null {
  if (value === null || value === undefined || value === "") return null;
  try {
    return dateFromParts(partsFromTimestamp(value));
  } catch {
    return null;
  }
}

export function localDateToDbTimestampDate(value: Date | null): Date | null {
  return dbTimestampToLocalDate(value);
}

export function parseDisplayDateAsDbTimestamp(
  value: Date | string | null | undefined,
): Date | null {
  if (value === null || value === undefined || value === "") return null;
  if (value instanceof Date) {
    return localDateToDbTimestampDate(value);
  }

  const trimmed = value.trim();
  if (!trimmed) return null;
  return localDateToDbTimestampDate(parseInputDate(trimmed));
}

export function serializeDbTimestamp(value: DbTimestampValue): string | null {
  if (value === null || value === undefined || value === "") return null;
  try {
    return formatIsoTimestampParts(partsFromTimestamp(value));
  } catch {
    return null;
  }
}

export function isLikelyUtcStoredAsLocalTimestamp(
  earlier: DbTimestampValue,
  later: DbTimestampValue,
): boolean {
  const earlierDate = dbTimestampToLocalDate(earlier);
  const laterDate = dbTimestampToLocalDate(later);
  if (!earlierDate || !laterDate) return false;
  const diffMs = laterDate.getTime() - earlierDate.getTime();
  const eightHoursMs = 8 * 60 * 60 * 1000;
  const tenHoursMs = 10 * 60 * 60 * 1000;
  return diffMs >= eightHoursMs && diffMs <= tenHoursMs;
}

export function correctLikelyTimerStartedAt(
  startedAt: Date | string,
  createdAt: DbTimestampValue,
  source: string | null,
): Date {
  const startedAtDate = dbTimestampToLocalDate(startedAt);
  if (!startedAtDate) {
    throw new Error("Invalid timestamp");
  }
  const createdAtDate = dbTimestampToLocalDate(createdAt);
  if (
    source === "timer" &&
    createdAtDate &&
    isLikelyUtcStoredAsLocalTimestamp(startedAtDate, createdAtDate)
  ) {
    return createdAtDate;
  }
  return startedAtDate;
}
