import { isClosedTaskStatus } from "@/lib/task-status";

export function occurrenceOverlapsRange(
  start: Date | null,
  end: Date | null,
  rangeStart: Date,
  rangeEnd: Date,
): boolean {
  if (!start) return false;
  const effectiveEnd = end ?? start;
  return start <= rangeEnd && effectiveEnd >= rangeStart;
}

export function shouldIncludeTaskScheduleOccurrence(params: {
  start: Date | null;
  end: Date | null;
  status: unknown;
  rangeStart: Date;
  rangeEnd: Date;
  blocked?: boolean;
}): boolean {
  const { start, end, status, rangeStart, rangeEnd, blocked } = params;
  if (blocked) return false;
  if (occurrenceOverlapsRange(start, end, rangeStart, rangeEnd)) return true;
  if (!start || isClosedTaskStatus(status)) return false;

  const effectiveEnd = end ?? start;
  return effectiveEnd < rangeStart;
}

export function chooseEarliestOpenOccurrence<T>(
  occurrences: T[],
  options: {
    getStart: (occurrence: T) => Date | null;
    getStatus: (occurrence: T) => unknown;
  },
): T | undefined {
  return [...occurrences]
    .sort((a, b) => {
      const aTime = options.getStart(a)?.getTime() ?? 0;
      const bTime = options.getStart(b)?.getTime() ?? 0;
      return aTime - bTime;
    })
    .find((occurrence) => !isClosedTaskStatus(options.getStatus(occurrence)));
}
