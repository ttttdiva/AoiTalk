// UI プレビュー専用の次回発生日計算。サーバーの RRULE 展開とは独立した
// ベストエフォート実装で、skip_weekend / skip_holiday の可視化を目的とする。

import { parseLocalDateTime } from "@/lib/date-time";

const JAPAN_HOLIDAYS: ReadonlySet<string> = new Set<string>([
  // 2025
  "2025-01-01",
  "2025-01-13",
  "2025-02-11",
  "2025-02-23",
  "2025-02-24",
  "2025-03-20",
  "2025-04-29",
  "2025-05-03",
  "2025-05-04",
  "2025-05-05",
  "2025-05-06",
  "2025-07-21",
  "2025-08-11",
  "2025-09-15",
  "2025-09-23",
  "2025-10-13",
  "2025-11-03",
  "2025-11-23",
  "2025-11-24",
  // 2026
  "2026-01-01",
  "2026-01-12",
  "2026-02-11",
  "2026-02-23",
  "2026-03-20",
  "2026-04-29",
  "2026-05-03",
  "2026-05-04",
  "2026-05-05",
  "2026-05-06",
  "2026-07-20",
  "2026-08-11",
  "2026-09-21",
  "2026-09-22",
  "2026-09-23",
  "2026-10-12",
  "2026-11-03",
  "2026-11-23",
  // 2027
  "2027-01-01",
  "2027-01-11",
  "2027-02-11",
  "2027-02-23",
  "2027-03-21",
  "2027-03-22",
  "2027-04-29",
  "2027-05-03",
  "2027-05-04",
  "2027-05-05",
  "2027-07-19",
  "2027-08-11",
  "2027-09-20",
  "2027-09-23",
  "2027-10-11",
  "2027-11-03",
  "2027-11-23",
  // 2028
  "2028-01-01",
  "2028-01-10",
  "2028-02-11",
  "2028-02-23",
  "2028-03-20",
  "2028-04-29",
  "2028-05-03",
  "2028-05-04",
  "2028-05-05",
  "2028-07-17",
  "2028-08-11",
  "2028-09-18",
  "2028-09-22",
  "2028-10-09",
  "2028-11-03",
  "2028-11-23",
  // 2029
  "2029-01-01",
  "2029-01-08",
  "2029-02-11",
  "2029-02-12",
  "2029-02-23",
  "2029-03-20",
  "2029-04-29",
  "2029-05-03",
  "2029-05-04",
  "2029-05-05",
  "2029-07-16",
  "2029-08-11",
  "2029-09-17",
  "2029-09-23",
  "2029-10-08",
  "2029-11-03",
  "2029-11-23",
]);

function toDateKey(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function copyTimeFrom(date: Date, timeSource: Date): Date {
  const next = new Date(date);
  next.setHours(
    timeSource.getHours(),
    timeSource.getMinutes(),
    timeSource.getSeconds(),
    timeSource.getMilliseconds(),
  );
  return next;
}

export function isJapaneseHoliday(d: Date): boolean {
  return JAPAN_HOLIDAYS.has(toDateKey(d));
}

function isWeekend(d: Date): boolean {
  const day = d.getDay();
  return day === 0 || day === 6;
}

const DAY_KEYS = ["SU", "MO", "TU", "WE", "TH", "FR", "SA"];

export interface RecurrencePreviewConfig {
  freq: string;
  interval: number;
  byDay: string[];
  skipWeekend: boolean;
  skipHoliday: boolean;
  endCount: number | null;
  endDate: string | null;
}

function shouldSkip(d: Date, cfg: RecurrencePreviewConfig): boolean {
  if (cfg.skipWeekend && isWeekend(d)) return true;
  if (cfg.skipHoliday && isJapaneseHoliday(d)) return true;
  return false;
}

function addDays(d: Date, days: number): Date {
  const next = new Date(d);
  next.setDate(next.getDate() + days);
  return next;
}

function applySkip(d: Date, cfg: RecurrencePreviewConfig): Date {
  let cur = new Date(d);
  let guard = 0;
  while (shouldSkip(cur, cfg) && guard < 14) {
    cur = addDays(cur, 1);
    guard++;
  }
  return cur;
}

function advanceBase(d: Date, freq: string, interval: number): Date {
  const n = new Date(d);
  switch (freq) {
    case "DAILY":
      n.setDate(n.getDate() + interval);
      break;
    case "WEEKLY":
      n.setDate(n.getDate() + 7 * interval);
      break;
    case "MONTHLY":
      n.setMonth(n.getMonth() + interval);
      break;
    case "YEARLY":
      n.setFullYear(n.getFullYear() + interval);
      break;
    default:
      n.setDate(n.getDate() + interval);
  }
  return n;
}

function nextWeeklyByDay(
  from: Date,
  startAnchor: Date,
  cfg: RecurrencePreviewConfig,
): Date {
  // from の翌日から順に、byDay に含まれ、かつ startAnchor の週から interval 週間隔の週に入る日を探す。
  const interval = Math.max(1, cfg.interval);
  const anchorWeekStart = addDays(startAnchor, -startAnchor.getDay());
  let cur = addDays(from, 1);
  for (let i = 0; i < 7 * interval * 2 + 7; i++) {
    const dayKey = DAY_KEYS[cur.getDay()];
    if (cfg.byDay.includes(dayKey)) {
      const curWeekStart = addDays(cur, -cur.getDay());
      const diffDays = Math.round(
        (curWeekStart.getTime() - anchorWeekStart.getTime()) /
          (1000 * 60 * 60 * 24),
      );
      const weeksDiff = Math.round(diffDays / 7);
      if (weeksDiff >= 0 && weeksDiff % interval === 0) return cur;
    }
    cur = addDays(cur, 1);
  }
  return cur;
}

export function computeUpcomingOccurrences(
  startDate: Date,
  cfg: RecurrencePreviewConfig,
  maxCount: number,
): Date[] {
  if (!startDate || isNaN(startDate.getTime())) return [];
  const interval = Math.max(1, cfg.interval || 1);
  const normCfg: RecurrencePreviewConfig = { ...cfg, interval };
  const anchor = new Date(startDate);
  anchor.setHours(0, 0, 0, 0);

  const until = cfg.endDate
    ? (parseLocalDateTime(cfg.endDate) ?? new Date(cfg.endDate))
    : null;
  if (until) until.setHours(23, 59, 59, 999);

  // endCount は総回数。開始日(=#1)を除いた残り件数でキャップする。
  const remaining =
    cfg.endCount && cfg.endCount > 0 ? cfg.endCount - 1 : Infinity;
  const limit = Math.min(maxCount, remaining);

  const result: Date[] = [];
  let cursor = new Date(anchor);
  let guard = 0;

  const guardLimit = Math.min(20000, Math.max(400, limit * 4));

  while (result.length < limit && guard < guardLimit) {
    guard++;
    let nextBase: Date;
    if (cfg.freq === "WEEKLY" && cfg.byDay.length > 0) {
      nextBase = nextWeeklyByDay(cursor, anchor, normCfg);
    } else {
      nextBase = advanceBase(cursor, cfg.freq, interval);
    }

    cursor = nextBase;

    if (until && cursor.getTime() > until.getTime()) break;

    const shifted = applySkip(cursor, normCfg);
    if (until && shifted.getTime() > until.getTime()) break;

    result.push(shifted);
  }

  return result;
}

function diffCalendarMonths(from: Date, to: Date): number {
  return (
    (to.getFullYear() - from.getFullYear()) * 12 +
    (to.getMonth() - from.getMonth())
  );
}

function fastForwardCursor(
  anchor: Date,
  rangeStart: Date,
  cfg: RecurrencePreviewConfig,
): Date {
  if (rangeStart.getTime() <= anchor.getTime()) return new Date(anchor);

  const interval = Math.max(1, cfg.interval || 1);
  const diffDays = Math.max(
    0,
    Math.floor(
      (rangeStart.getTime() - anchor.getTime()) / (1000 * 60 * 60 * 24),
    ),
  );

  if (cfg.freq === "WEEKLY" && cfg.byDay.length > 0) {
    const anchorWeekStart = addDays(anchor, -anchor.getDay());
    const rangeWeekStart = addDays(rangeStart, -rangeStart.getDay());
    const weeksDiff = Math.max(
      0,
      Math.floor(
        (rangeWeekStart.getTime() - anchorWeekStart.getTime()) /
          (1000 * 60 * 60 * 24 * 7),
      ),
    );
    const cycles = Math.max(0, Math.floor(weeksDiff / interval) - 2);
    return addDays(anchorWeekStart, cycles * interval * 7 - 1);
  }

  switch (cfg.freq) {
    case "DAILY": {
      const steps = Math.max(0, Math.floor(diffDays / interval) - 2);
      return addDays(anchor, steps * interval);
    }
    case "WEEKLY": {
      const steps = Math.max(0, Math.floor(diffDays / (7 * interval)) - 2);
      return addDays(anchor, steps * 7 * interval);
    }
    case "MONTHLY": {
      const monthsDiff = Math.max(0, diffCalendarMonths(anchor, rangeStart));
      const steps = Math.max(0, Math.floor(monthsDiff / interval) - 2);
      const cursor = new Date(anchor);
      cursor.setMonth(cursor.getMonth() + steps * interval);
      return cursor;
    }
    case "YEARLY": {
      const yearsDiff = Math.max(
        0,
        rangeStart.getFullYear() - anchor.getFullYear(),
      );
      const steps = Math.max(0, Math.floor(yearsDiff / interval) - 2);
      const cursor = new Date(anchor);
      cursor.setFullYear(cursor.getFullYear() + steps * interval);
      return cursor;
    }
    default: {
      const steps = Math.max(0, Math.floor(diffDays / interval) - 2);
      return addDays(anchor, steps * interval);
    }
  }
}

export function computeOccurrencesInRange(
  startDate: Date,
  cfg: RecurrencePreviewConfig,
  rangeStart: Date,
  rangeEnd: Date,
  maxCount: number,
): Date[] {
  if (!startDate || isNaN(startDate.getTime())) return [];
  if (isNaN(rangeStart.getTime()) || isNaN(rangeEnd.getTime())) return [];
  if (rangeEnd.getTime() < rangeStart.getTime()) return [];

  const interval = Math.max(1, cfg.interval || 1);
  const normCfg: RecurrencePreviewConfig = { ...cfg, interval };
  const anchor = new Date(startDate);
  anchor.setHours(0, 0, 0, 0);

  const until = cfg.endDate
    ? (parseLocalDateTime(cfg.endDate) ?? new Date(cfg.endDate))
    : null;
  if (until) until.setHours(23, 59, 59, 999);

  // COUNT needs the same ordinal accounting as computeUpcomingOccurrences.
  // Keep that path exact; the unbounded path below is optimized for calendar ranges.
  if (cfg.endCount && cfg.endCount > 0) {
    return computeUpcomingOccurrences(startDate, normCfg, maxCount)
      .map((date) => copyTimeFrom(date, startDate))
      .filter(
        (date) =>
          date.getTime() >= rangeStart.getTime() &&
          date.getTime() <= rangeEnd.getTime(),
      );
  }

  const effectiveEnd =
    until && until.getTime() < rangeEnd.getTime() ? until : rangeEnd;
  if (effectiveEnd.getTime() < rangeStart.getTime()) return [];

  const result: Date[] = [];
  let cursor = fastForwardCursor(anchor, rangeStart, normCfg);
  let guard = 0;
  const guardLimit = Math.min(20000, Math.max(400, maxCount * 4));

  while (result.length < maxCount && guard < guardLimit) {
    guard++;
    let nextBase: Date;
    if (normCfg.freq === "WEEKLY" && normCfg.byDay.length > 0) {
      nextBase = nextWeeklyByDay(cursor, anchor, normCfg);
    } else {
      nextBase = advanceBase(cursor, normCfg.freq, interval);
    }

    cursor = nextBase;
    if (cursor.getTime() > effectiveEnd.getTime()) break;

    const shifted = copyTimeFrom(applySkip(cursor, normCfg), startDate);
    if (shifted.getTime() > effectiveEnd.getTime()) break;
    if (shifted.getTime() >= rangeStart.getTime()) {
      result.push(shifted);
    }
  }

  return result;
}
