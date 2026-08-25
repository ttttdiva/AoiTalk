// UI プレビュー専用の次回発生日計算。サーバーの RRULE 展開とは独立した
// ベストエフォート実装で、skip_weekend / skip_holiday の可視化を目的とする。

import { parseLocalDateTime } from "@/lib/date-time";
import { isJapaneseHoliday } from "@/lib/japanese-holidays";

export { isJapaneseHoliday } from "@/lib/japanese-holidays";

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

function isWeekend(d: Date): boolean {
  const day = d.getDay();
  return day === 0 || day === 6;
}

const DAY_KEYS = ["SU", "MO", "TU", "WE", "TH", "FR", "SA"];

/** 土日・祝日に当たった回の扱い。 */
export type RecurrenceSkipMode = "shift_forward" | "omit";

export const RECURRENCE_SKIP_MODES: readonly RecurrenceSkipMode[] = [
  "shift_forward",
  "omit",
];

export const DEFAULT_RECURRENCE_SKIP_MODE: RecurrenceSkipMode = "shift_forward";

/** 未知・未設定・旧 shift_backward の skip_mode は翌営業日へずらす。 */
export function normalizeSkipMode(
  value: string | null | undefined,
): RecurrenceSkipMode {
  if (value === "shift_backward") return "shift_forward";
  return RECURRENCE_SKIP_MODES.includes(value as RecurrenceSkipMode)
    ? (value as RecurrenceSkipMode)
    : DEFAULT_RECURRENCE_SKIP_MODE;
}

export interface RecurrencePreviewConfig {
  freq: string;
  interval: number;
  byDay: string[];
  skipWeekend: boolean;
  skipHoliday: boolean;
  /** 未指定は shift_forward（従来挙動）。 */
  skipMode?: RecurrenceSkipMode | string | null;
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

/**
 * 土日・祝日に当たった発生日を skipMode に従って処理する。
 * - shift_forward: 条件を満たす最初の翌日へずらす（既定）
 * - 旧 shift_backward: shift_forward として条件を満たす最初の翌日へずらす
 * - omit: その回を発生させない（null を返す）
 * src/services/task_management/_shared.py の apply_occurrence_skip と同じ挙動。
 */
function applySkip(d: Date, cfg: RecurrencePreviewConfig): Date | null {
  if (!shouldSkip(d, cfg)) return new Date(d);

  const mode = normalizeSkipMode(cfg.skipMode);
  if (mode === "omit") return null;

  let cur = new Date(d);
  for (let guard = 0; guard < 14; guard++) {
    cur = addDays(cur, 1);
    if (!shouldSkip(cur, cfg)) return cur;
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
  let lastShiftedTime: number | null = null;
  // omit で消えた回も回数としては消費する（サーバーの RRULE 展開と同じ数え方）。
  let budget = remaining;

  const guardLimit = Math.min(20000, Math.max(400, limit * 4));

  while (result.length < limit && budget > 0 && guard < guardLimit) {
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
    if (shifted === null) {
      budget--;
      continue;
    }
    if (until && shifted.getTime() > until.getTime()) break;

    // スキップは「発生させない」ではなく「後ろへずらす」ため、複数回が同じ日に
    // 着地することがある（毎日+土日スキップだと 土・日・月 がすべて月曜へ寄る）。
    // 同じ日時を重複して返さない。
    if (lastShiftedTime === shifted.getTime()) continue;
    lastShiftedTime = shifted.getTime();

    budget--;
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
  let lastShiftedTime: number | null = null;
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

    const skipped = applySkip(cursor, normCfg);
    if (skipped === null) continue;
    const shifted = copyTimeFrom(skipped, startDate);
    if (shifted.getTime() > effectiveEnd.getTime()) break;

    // computeUpcomingOccurrences と同じくスキップの寄せによる重複を落とす。
    // 範囲外で捨てる分も含めて直前の着地点と比較する必要があるため、
    // result の末尾ではなく lastShiftedTime を使う。
    if (lastShiftedTime === shifted.getTime()) continue;
    lastShiftedTime = shifted.getTime();

    if (shifted.getTime() >= rangeStart.getTime()) {
      result.push(shifted);
    }
  }

  return result;
}
