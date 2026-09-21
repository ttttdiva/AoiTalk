import { sql, type SQL, type SQLWrapper } from "drizzle-orm";

export type DbTimestampValue = Date | string | null | undefined;

/**
 * Time-entry timestamps are stored in PostgreSQL as timestamp-without-time-zone
 * values.  They represent a deployment-zone wall clock, unlike the other
 * database timestamps handled by this module.  Keep this contract local to the
 * timer helpers below so task due/deadline/recurrence values retain their
 * existing wall-clock behavior.
 */
export const DEFAULT_TIMER_TIMEZONE = "Asia/Tokyo";

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
const EXPLICIT_TIMESTAMP_OFFSET_PATTERN = /(?:Z|[+-]\d{2}:?\d{2})$/i;

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

function timerTimezone(): string {
  const configured = process.env.AOITALK_TIMEZONE?.trim();
  if (!configured) return DEFAULT_TIMER_TIMEZONE;

  // Intl throws for an unknown IANA zone.  A bad optional setting must not make
  // a task list/detail route fail; retain the documented default instead.
  try {
    new Intl.DateTimeFormat("en-US", { timeZone: configured }).format();
    return configured;
  } catch {
    return DEFAULT_TIMER_TIMEZONE;
  }
}

function utcEpochFromWallClockParts(parts: WallClockParts): number {
  // Date.UTC treats years 0..99 as 1900..1999.  Constructing from epoch and
  // assigning the UTC year avoids that legacy quirk while keeping the helper
  // valid for the full four-digit timestamp grammar above.
  const date = new Date(0);
  date.setUTCFullYear(parts.year, parts.month - 1, parts.day);
  date.setUTCHours(
    parts.hour,
    parts.minute,
    parts.second,
    parts.millisecond,
  );
  return date.getTime();
}

function timerZoneParts(instant: Date, timezone: string): WallClockParts {
  const values = Object.fromEntries(
    new Intl.DateTimeFormat("en-US", {
      timeZone: timezone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hourCycle: "h23",
    })
      .formatToParts(instant)
      .filter((part) => part.type !== "literal")
      .map((part) => [part.type, part.value]),
  ) as Record<string, string>;

  return {
    year: Number(values.year),
    month: Number(values.month),
    day: Number(values.day),
    hour: Number(values.hour) % 24,
    minute: Number(values.minute),
    second: Number(values.second),
    millisecond: instant.getUTCMilliseconds(),
  };
}

function timerZoneOffsetMinutes(value: Date, timezone: string): number {
  const localParts = timerZoneParts(value, timezone);
  return Math.round(
    (utcEpochFromWallClockParts(localParts) - value.getTime()) / 60000,
  );
}

function timerInstantFromWallClockParts(
  parts: WallClockParts,
  timezone: string,
): Date {
  const wallClockMs = utcEpochFromWallClockParts(parts);
  // Start with the wall clock interpreted as UTC, then correct it using the
  // configured zone's offset.  Re-evaluating once more handles DST boundary
  // transitions where the initial approximation uses the adjacent offset.
  let instantMs = wallClockMs;
  for (let attempt = 0; attempt < 3; attempt += 1) {
    const correctedMs =
      wallClockMs - timerZoneOffsetMinutes(new Date(instantMs), timezone) * 60000;
    if (correctedMs === instantMs) break;
    instantMs = correctedMs;
  }
  return new Date(instantMs);
}

function formatTimerInstant(value: Date, timezone: string): string {
  assertValidDate(value);
  const parts = timerZoneParts(value, timezone);
  const offsetMinutes = timerZoneOffsetMinutes(value, timezone);
  const sign = offsetMinutes >= 0 ? "+" : "-";
  const absoluteOffset = Math.abs(offsetMinutes);
  const offset = `${sign}${pad(Math.floor(absoluteOffset / 60))}:${pad(
    absoluteOffset % 60,
  )}`;
  const base = `${pad(parts.year, 4)}-${pad(parts.month)}-${pad(
    parts.day,
  )}T${pad(parts.hour)}:${pad(parts.minute)}:${pad(parts.second)}`;
  const milliseconds = parts.millisecond > 0 ? `.${pad(parts.millisecond, 3)}` : "";
  return `${base}${milliseconds}${offset}`;
}

function timerWallClockPartsFromInstant(value: Date, timezone: string): WallClockParts {
  return timerZoneParts(value, timezone);
}

/** Return the configured deployment zone used for direct-DB timer values. */
export function getTimerTimezone(): string {
  return timerTimezone();
}

/**
 * Interpret a direct-DB time-entry value as an instant.
 *
 * Naive values are wall-clock values in AOITALK_TIMEZONE.  A value that already
 * carries Z/an offset is treated as an instant, which keeps this helper safe
 * when a route is fed an already-normalized value during a migration.
 */
export function dbTimerTimestampToDate(value: DbTimestampValue): Date | null {
  if (value === null || value === undefined || value === "") return null;

  if (value instanceof Date) {
    try {
      return timerInstantFromWallClockParts(
        partsFromDate(value),
        timerTimezone(),
      );
    } catch {
      return null;
    }
  }

  const trimmed = value.trim();
  if (!trimmed) return null;
  if (EXPLICIT_TIMESTAMP_OFFSET_PATTERN.test(trimmed)) {
    const parsed = new Date(trimmed);
    return Number.isNaN(parsed.getTime()) ? null : parsed;
  }

  try {
    return timerInstantFromWallClockParts(partsFromString(trimmed), timerTimezone());
  } catch {
    return null;
  }
}

/** Serialize a direct-DB timer value as RFC3339 with the deployment offset. */
export function serializeTimerTimestamp(
  value: DbTimestampValue,
): string | null {
  const parsed = dbTimerTimestampToDate(value);
  if (!parsed) return null;
  try {
    return formatTimerInstant(parsed, timerTimezone());
  } catch {
    return null;
  }
}

/** Serialize a timer input back to the naive deployment-zone DB spelling. */
export function serializeTimerDbTimestampInput(value: Date | string): string {
  const instant = dbTimerTimestampToDate(value);
  if (!instant) throw new Error("Invalid timestamp");
  return formatDbTimestampParts(
    timerWallClockPartsFromInstant(instant, timerTimezone()),
  );
}

/** Build a SQL timestamp literal for timer-specific query filters. */
export function toDbTimerTimestamp(value: Date | string): SQL {
  return sql`${serializeTimerDbTimestampInput(value)}::timestamp`;
}

/**
 * Build a deployment-zone-aware SQL duration expression for naive timer rows.
 * PostgreSQL's direct subtraction treats timestamp-without-time-zone values as
 * fixed wall-clock numbers, which is wrong across DST transitions.  Attaching
 * the configured zone before subtraction keeps task-list totals aligned with
 * the JS detail/report duration helper.
 */
export function timerDurationSecondsSql(
  startedAt: SQLWrapper,
  endedAt: SQLWrapper,
): SQL<number> {
  const timezone = timerTimezone();
  return sql<number>`coalesce(sum(greatest(extract(epoch from ((${endedAt} at time zone ${timezone}) - (${startedAt} at time zone ${timezone}))), 0))::int, 0)`;
}

/** Calculate elapsed timer duration using instants, not browser/server locale. */
export function calculateTimerDurationSeconds(
  startedAt: DbTimestampValue,
  endedAt: DbTimestampValue,
  now = new Date(),
): number {
  const start = dbTimerTimestampToDate(startedAt);
  if (!start) return 0;
  const end = endedAt ? dbTimerTimestampToDate(endedAt) : now;
  if (!end) return 0;
  return Math.max(0, Math.floor((end.getTime() - start.getTime()) / 1000));
}

/** Return the deployment-zone calendar day for a direct-DB timer timestamp. */
export function timerDateKey(value: DbTimestampValue): string | null {
  return serializeTimerTimestamp(value)?.slice(0, 10) ?? null;
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
