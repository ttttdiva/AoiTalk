import { dbTimestampToLocalDate } from "@/lib/server/db-time";

/** A series-level change, effective from a canonical recurrence boundary. */
export type RecurrenceScheduleSegment = {
  effectiveFrom: Date | string;
  startOffsetSeconds: number;
  endOffsetSeconds: number;
  allDay: boolean;
};

export type AppliedRecurrenceSchedule = {
  /** The canonical RRULE occurrence start, before the segment offset. */
  originalStartAt: Date;
  /** The displayed/materialized start after applying the effective segment. */
  startAt: Date;
  /** The displayed/materialized end after applying the effective segment. */
  endAt: Date | null;
  allDay: boolean;
  /** Semantic aliases useful at call sites that use actual/canonical names. */
  actualStart: Date;
  actualEnd: Date | null;
  actualAllDay: boolean;
  originalStart: Date;
  segment: RecurrenceScheduleSegment | null;
};

function asDate(value: Date | string): Date | null {
  const parsed = dbTimestampToLocalDate(value);
  return parsed && !Number.isNaN(parsed.getTime()) ? parsed : null;
}

function offsetSeconds(value: unknown): number {
  const parsed = Number(value || 0);
  return Number.isFinite(parsed) ? parsed : 0;
}

/**
 * Find the last schedule segment whose canonical boundary is not after the
 * occurrence.  Callers normally provide segments ordered by effective_from,
 * but sorting is deliberately not required here so this helper remains safe
 * for rows returned by ad-hoc queries and easy to unit test.
 */
export function resolveRecurrenceScheduleSegment(
  segments: readonly RecurrenceScheduleSegment[],
  canonicalStart: Date | string,
): RecurrenceScheduleSegment | null {
  const canonical = asDate(canonicalStart);
  if (!canonical) return null;

  let resolved: RecurrenceScheduleSegment | null = null;
  let resolvedAt = Number.NEGATIVE_INFINITY;
  for (const segment of segments) {
    const effectiveFrom = asDate(segment.effectiveFrom);
    if (!effectiveFrom || effectiveFrom.getTime() > canonical.getTime()) {
      continue;
    }
    if (effectiveFrom.getTime() >= resolvedAt) {
      resolved = segment;
      resolvedAt = effectiveFrom.getTime();
    }
  }
  return resolved;
}

/**
 * Apply the effective series segment to one canonical occurrence.
 * Offsets are absolute relative to the original canonical schedule (not
 * deltas from the previous segment), so repeated future edits do not drift.
 */
export function applyRecurrenceScheduleSegment({
  canonicalStart,
  canonicalEnd,
  baseStartAt,
  baseEndAt,
  baseAllDay,
  segments,
}: {
  canonicalStart: Date | string;
  canonicalEnd?: Date | string | null;
  /** Optional pre-segment actual start (e.g. skip-forward adjusted date). */
  baseStartAt?: Date | string | null;
  /** Optional pre-segment actual end (e.g. skip-forward adjusted date). */
  baseEndAt?: Date | string | null;
  baseAllDay: boolean;
  segments: readonly RecurrenceScheduleSegment[];
}): AppliedRecurrenceSchedule {
  const originalStartAt = asDate(canonicalStart) ?? new Date(canonicalStart);
  const canonicalEndAt = canonicalEnd ? asDate(canonicalEnd) : null;
  const baseStart = baseStartAt ? asDate(baseStartAt) ?? originalStartAt : originalStartAt;
  const baseEnd = baseEndAt ? asDate(baseEndAt) ?? canonicalEndAt : canonicalEndAt;
  const segment = resolveRecurrenceScheduleSegment(segments, originalStartAt);
  const startOffsetMs = segment
    ? offsetSeconds(segment.startOffsetSeconds) * 1000
    : 0;
  const endOffsetMs = segment
    ? offsetSeconds(segment.endOffsetSeconds) * 1000
    : 0;
  const startAt = new Date(baseStart.getTime() + startOffsetMs);
  const endAt = baseEnd
    ? new Date(baseEnd.getTime() + endOffsetMs)
    : null;
  const allDay = segment ? !!segment.allDay : baseAllDay;

  return {
    originalStartAt,
    startAt,
    endAt,
    allDay,
    actualStart: startAt,
    actualEnd: endAt,
    actualAllDay: allDay,
    originalStart: originalStartAt,
    segment,
  };
}

/** Return the largest absolute offset in milliseconds for query-window padding. */
export function getRecurrenceSegmentEnvelopeMs(
  segments: readonly RecurrenceScheduleSegment[],
): number {
  return segments.reduce((max, segment) => {
    const start = Math.abs(offsetSeconds(segment.startOffsetSeconds));
    const end = Math.abs(offsetSeconds(segment.endOffsetSeconds));
    return Math.max(max, start, end) * 1000;
  }, 0);
}
