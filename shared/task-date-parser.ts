/**
 * Pure task date input parser shared by the web and native clients.
 *
 * The returned value is a local wall-clock value with minute precision. It is
 * deliberately not converted to UTC and does not depend on the host locale's
 * free-form Date string parser.
 */

const MIN_YEAR = 1000;
const MAX_YEAR = 9999;

const WEEKDAY_ALIASES: Record<string, number> = {
  sunday: 0,
  sun: 0,
  monday: 1,
  mon: 1,
  tuesday: 2,
  tue: 2,
  tues: 2,
  wednesday: 3,
  wed: 3,
  thursday: 4,
  thu: 4,
  thur: 4,
  thurs: 4,
  friday: 5,
  fri: 5,
  saturday: 6,
  sat: 6,
};

const DATE_KEYWORD_ALIASES: Record<string, string[]> = {
  today: ["today", "tod"],
  tomorrow: ["tomorrow", "tomo", "tom"],
  sunday: ["sunday", "sun"],
  monday: ["monday", "mon"],
  tuesday: ["tuesday", "tues", "tue"],
  wednesday: ["wednesday", "wed"],
  thursday: ["thursday", "thurs", "thur", "thu"],
  friday: ["friday", "fri"],
  saturday: ["saturday", "sat"],
};

function normalizeInput(value: string): string {
  return value.trim().toLowerCase().replace(/,\s*/g, " ").replace(/\s+/g, " ");
}

function pad(value: number): string {
  return value.toString().padStart(2, "0");
}

function isValidYear(year: number): boolean {
  return Number.isInteger(year) && year >= MIN_YEAR && year <= MAX_YEAR;
}

function isValidTime(hour: number, minute: number): boolean {
  return (
    Number.isInteger(hour) &&
    Number.isInteger(minute) &&
    hour >= 0 &&
    hour <= 23 &&
    minute >= 0 &&
    minute <= 59
  );
}

function daysInMonth(year: number, month: number): number {
  return new Date(year, month, 0).getDate();
}

function createLocalDate(
  year: number,
  month: number,
  day: number,
  hour = 0,
  minute = 0,
): Date | null {
  if (
    !isValidYear(year) ||
    !Number.isInteger(month) ||
    month < 1 ||
    month > 12
  ) {
    return null;
  }
  if (
    !Number.isInteger(day) ||
    day < 1 ||
    day > daysInMonth(year, month) ||
    !isValidTime(hour, minute)
  ) {
    return null;
  }

  const date = new Date(year, month - 1, day, hour, minute, 0, 0);
  if (
    !Number.isFinite(date.getTime()) ||
    date.getFullYear() !== year ||
    date.getMonth() !== month - 1 ||
    date.getDate() !== day ||
    date.getHours() !== hour ||
    date.getMinutes() !== minute
  ) {
    return null;
  }
  return date;
}

function formatLocalDateTime(date: Date): string | null {
  if (!Number.isFinite(date.getTime())) return null;
  const year = date.getFullYear();
  if (!isValidYear(year)) return null;
  return `${year.toString().padStart(4, "0")}-${pad(
    date.getMonth() + 1,
  )}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function resolveDateKeyword(input: string): string | null {
  const query = input.trim().toLowerCase().replace(/\s+/g, " ");
  if (!query) return null;
  const entries = Object.entries(DATE_KEYWORD_ALIASES);
  const exact = entries.filter(([, aliases]) => aliases.includes(query));
  if (exact.length === 1) return exact[0][0];
  const prefixes = entries.filter(([, aliases]) =>
    aliases.some((alias) => alias.startsWith(query)),
  );
  return prefixes.length === 1 ? prefixes[0][0] : null;
}

function parseTime(
  hourText: string,
  minuteText: string,
): [number, number] | null {
  const hour = Number(hourText);
  const minute = Number(minuteText);
  return isValidTime(hour, minute) ? [hour, minute] : null;
}

function setLocalTime(date: Date, hour: number, minute: number): boolean {
  if (!isValidTime(hour, minute) || !Number.isFinite(date.getTime())) {
    return false;
  }
  const year = date.getFullYear();
  const month = date.getMonth();
  const day = date.getDate();
  date.setHours(hour, minute, 0, 0);
  return (
    Number.isFinite(date.getTime()) &&
    date.getFullYear() === year &&
    date.getMonth() === month &&
    date.getDate() === day &&
    date.getHours() === hour &&
    date.getMinutes() === minute
  );
}

function parseCanonicalLocalDate(raw: string): string | null {
  const match = raw
    .trim()
    .match(
      /^(\d{4})-(\d{2})-(\d{2})(?:[T\s](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,3}))?)?)?$/,
    );
  if (!match) return null;

  const [
    ,
    yearText,
    monthText,
    dayText,
    hourText,
    minuteText,
    secondText,
    millisecondText,
  ] = match;
  const year = Number(yearText);
  const month = Number(monthText);
  const day = Number(dayText);
  const hour = hourText === undefined ? 0 : Number(hourText);
  const minute = minuteText === undefined ? 0 : Number(minuteText);
  const second = secondText === undefined ? 0 : Number(secondText);
  const millisecond = millisecondText
    ? Number(millisecondText.padEnd(3, "0"))
    : 0;
  if (
    !isValidTime(hour, minute) ||
    !Number.isInteger(second) ||
    second < 0 ||
    second > 59 ||
    !Number.isInteger(millisecond) ||
    millisecond < 0 ||
    millisecond > 999
  ) {
    return null;
  }
  const date = createLocalDate(year, month, day, hour, minute);
  return date ? formatLocalDateTime(date) : null;
}

/**
 * Resolve the same date expressions used by the PC task form.
 *
 * `now` is optional for deterministic consumers and tests; callers normally
 * omit it so the current local wall clock is used.
 */
export function parseFlexibleDate(
  raw: string,
  now: Date = new Date(),
): string | null {
  if (!(now instanceof Date) || !Number.isFinite(now.getTime())) return null;

  let lower = normalizeInput(raw);
  if (!lower) return null;
  const current = new Date(now);

  const relativeDaysMatch = lower.match(
    /^([+-]?\d+)\s*days?(?:\s+(\d{1,2}):(\d{2}))?$/,
  );
  if (relativeDaysMatch) {
    const days = Number(relativeDaysMatch[1]);
    if (!Number.isSafeInteger(days)) return null;
    const time = relativeDaysMatch[2]
      ? parseTime(relativeDaysMatch[2], relativeDaysMatch[3])
      : [0, 0];
    if (!time) return null;
    const date = new Date(current);
    date.setDate(date.getDate() + days);
    return setLocalTime(date, time[0], time[1])
      ? formatLocalDateTime(date)
      : null;
  }

  const keywordMatch = lower.match(
    /^(next\s+)?([a-z]+)(?:\s+(\d{1,2}:\d{2}))?$/,
  );
  if (keywordMatch) {
    const keyword = resolveDateKeyword(keywordMatch[2]);
    const next = !!keywordMatch[1];
    if (keyword && (!next || Object.hasOwn(WEEKDAY_ALIASES, keyword))) {
      lower = `${next ? "next " : ""}${keyword}${
        keywordMatch[3] ? ` ${keywordMatch[3]}` : ""
      }`;
    }
  }

  if (lower === "today") {
    const date = new Date(current);
    return setLocalTime(date, 0, 0) ? formatLocalDateTime(date) : null;
  }
  if (lower === "tomorrow") {
    const date = new Date(current);
    date.setDate(date.getDate() + 1);
    return setLocalTime(date, 0, 0) ? formatLocalDateTime(date) : null;
  }
  if (lower === "next week") {
    const date = new Date(current);
    date.setDate(date.getDate() + ((8 - date.getDay()) % 7 || 7));
    return setLocalTime(date, 0, 0) ? formatLocalDateTime(date) : null;
  }
  if (lower === "next month") {
    const date = new Date(current);
    date.setDate(1);
    date.setMonth(date.getMonth() + 1);
    return setLocalTime(date, 0, 0) ? formatLocalDateTime(date) : null;
  }

  const weekdayMatch = lower.match(
    /^(next\s+)?(sunday|sun|monday|mon|tuesday|tue|tues|wednesday|wed|thursday|thu|thur|thurs|friday|fri|saturday|sat)(?:\s+(\d{1,2}):(\d{2}))?$/,
  );
  if (weekdayMatch) {
    const targetDay = WEEKDAY_ALIASES[weekdayMatch[2]];
    let daysUntil = (targetDay - current.getDay() + 7) % 7;
    if (weekdayMatch[1] && daysUntil === 0) daysUntil = 7;
    const time = weekdayMatch[3]
      ? parseTime(weekdayMatch[3], weekdayMatch[4])
      : [0, 0];
    if (!time) return null;
    const date = new Date(current);
    date.setDate(date.getDate() + daysUntil);
    return setLocalTime(date, time[0], time[1])
      ? formatLocalDateTime(date)
      : null;
  }

  const todayTimeMatch = lower.match(/^(?:today|tod)\s+(\d{1,2}):(\d{2})$/);
  if (todayTimeMatch) {
    const time = parseTime(todayTimeMatch[1], todayTimeMatch[2]);
    if (!time) return null;
    const date = new Date(current);
    return setLocalTime(date, time[0], time[1])
      ? formatLocalDateTime(date)
      : null;
  }

  const tomoTimeMatch = lower.match(
    /^(?:tomorrow|tomo|tom)\s+(\d{1,2}):(\d{2})$/,
  );
  if (tomoTimeMatch) {
    const time = parseTime(tomoTimeMatch[1], tomoTimeMatch[2]);
    if (!time) return null;
    const date = new Date(current);
    date.setDate(date.getDate() + 1);
    return setLocalTime(date, time[0], time[1])
      ? formatLocalDateTime(date)
      : null;
  }

  const dateMatch = lower.match(
    /^(\d{1,2})\/(\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?$/,
  );
  if (dateMatch) {
    const time = dateMatch[3] ? parseTime(dateMatch[3], dateMatch[4]) : [0, 0];
    if (!time) return null;
    const date = createLocalDate(
      current.getFullYear(),
      Number(dateMatch[1]),
      Number(dateMatch[2]),
      time[0],
      time[1],
    );
    return date ? formatLocalDateTime(date) : null;
  }

  const isoDateMatch = lower.match(
    /^(\d{4})-(\d{1,2})-(\d{1,2})(?:[ t](\d{1,2}):(\d{2}))?$/,
  );
  if (isoDateMatch) {
    const time = isoDateMatch[4]
      ? parseTime(isoDateMatch[4], isoDateMatch[5])
      : [0, 0];
    if (!time) return null;
    const date = createLocalDate(
      Number(isoDateMatch[1]),
      Number(isoDateMatch[2]),
      Number(isoDateMatch[3]),
      time[0],
      time[1],
    );
    return date ? formatLocalDateTime(date) : null;
  }

  const timeOnly = lower.match(/^(\d{1,2}):(\d{2})$/);
  if (timeOnly) {
    const time = parseTime(timeOnly[1], timeOnly[2]);
    if (!time) return null;
    const date = new Date(current);
    return setLocalTime(date, time[0], time[1])
      ? formatLocalDateTime(date)
      : null;
  }

  return parseCanonicalLocalDate(raw);
}

/** Whether a raw date expression explicitly includes a wall-clock time. */
export function hasFlexibleDateTime(raw: string): boolean {
  const trimmed = raw.trim();
  return (
    /^\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}/.test(trimmed) ||
    /(^|\s)\d{1,2}:\d{2}$/.test(trimmed)
  );
}

export type ParsedFlexibleTaskDate = {
  value: string;
  dateOnly: boolean;
};

/** Resolve a task field value and preserve whether the user supplied a time. */
export function parseFlexibleTaskDate(
  raw: string,
  now: Date = new Date(),
): ParsedFlexibleTaskDate | null {
  const value = parseFlexibleDate(raw, now);
  if (!value) return null;
  return { value, dateOnly: !hasFlexibleDateTime(raw) };
}
