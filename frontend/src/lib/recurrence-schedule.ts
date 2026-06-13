import {
  computeUpcomingOccurrences,
  type RecurrencePreviewConfig,
} from "@/lib/recurrence-preview";
import {
  dbTimestampToLocalDate,
  localDateToDbTimestampDate,
  type DbTimestampValue,
} from "@/lib/server/db-time";

const DAY_MS = 24 * 60 * 60 * 1000;

function copyDateWithTime(date: Date, timeSource: Date): Date {
  const next = new Date(date);
  next.setHours(
    timeSource.getHours(),
    timeSource.getMinutes(),
    timeSource.getSeconds(),
    timeSource.getMilliseconds(),
  );
  return next;
}

function estimateLookaheadCount(
  currentStartAt: Date,
  threshold: Date,
  config: RecurrencePreviewConfig,
): number {
  if (config.endCount && config.endCount > 0) {
    return Math.max(0, config.endCount - 1);
  }

  const elapsedDays = Math.max(
    0,
    Math.ceil((threshold.getTime() - currentStartAt.getTime()) / DAY_MS),
  );
  return Math.min(20000, Math.max(16, elapsedDays + 366));
}

export function computeNextRecurringScheduleAfter({
  currentStartAt,
  currentEndAt,
  config,
  after = new Date(),
}: {
  currentStartAt: DbTimestampValue;
  currentEndAt: DbTimestampValue;
  config: RecurrencePreviewConfig;
  after?: Date;
}): { startAt: Date; endAt: Date | null; advancedBy: number } | null {
  const currentStartLocal = dbTimestampToLocalDate(currentStartAt);
  if (!currentStartLocal) return null;
  const currentEndLocal = currentEndAt
    ? dbTimestampToLocalDate(currentEndAt)
    : null;
  const threshold =
    after.getTime() > currentStartLocal.getTime() ? after : currentStartLocal;
  const lookaheadCount = estimateLookaheadCount(
    currentStartLocal,
    threshold,
    config,
  );
  if (lookaheadCount <= 0) return null;

  const durationMs =
    currentEndLocal && !isNaN(currentEndLocal.getTime())
      ? currentEndLocal.getTime() - currentStartLocal.getTime()
      : null;

  const candidates = computeUpcomingOccurrences(
    currentStartLocal,
    config,
    lookaheadCount,
  );

  for (let i = 0; i < candidates.length; i++) {
    const startAt = copyDateWithTime(candidates[i], currentStartLocal);
    if (startAt.getTime() <= threshold.getTime()) continue;

    const endAt =
      durationMs !== null ? new Date(startAt.getTime() + durationMs) : null;
    return {
      startAt: localDateToDbTimestampDate(startAt) ?? startAt,
      endAt: endAt ? (localDateToDbTimestampDate(endAt) ?? endAt) : null,
      advancedBy: i + 1,
    };
  }

  return null;
}

export function computeNextRecurringSchedule({
  currentStartAt,
  currentEndAt,
  config,
  now = new Date(),
}: {
  currentStartAt: DbTimestampValue;
  currentEndAt: DbTimestampValue;
  config: RecurrencePreviewConfig;
  now?: Date;
}): { startAt: Date; endAt: Date | null; advancedBy: number } | null {
  return computeNextRecurringScheduleAfter({
    currentStartAt,
    currentEndAt,
    config,
    after: now,
  });
}
