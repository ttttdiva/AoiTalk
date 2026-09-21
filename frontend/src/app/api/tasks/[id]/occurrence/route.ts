import { NextRequest, NextResponse } from "next/server";
import { and, eq, gte, inArray, isNull } from "drizzle-orm";
import { db } from "@/db";
import {
  notificationDeliveries,
  taskOccurrences,
  taskRecurrenceScheduleSegments,
  taskRecurrenceRules,
  tasks,
  timeEntries,
} from "@/db/schema";
import { getSession } from "@/lib/auth";
import { normalizeTaskStatus } from "@/lib/task-status";
import {
  buildRecurrenceOverrideSourceKind,
  buildRecurrenceSkipSourceKind,
  canReuseOccurrenceRowForOverride,
  isRecurrenceOverrideSourceKind,
  isRecurrenceSkipSourceKind,
  resolveOccurrenceOriginalStartAt,
  resolveOccurrenceCutoffSource,
  matchesOccurrenceIdentity,
  shouldFindOccurrenceByStartAt,
} from "@/lib/recurrence-exceptions";
import {
  dbTimestampToLocalDate,
  parseDisplayDateAsDbTimestamp,
  serializeDbTimestamp,
  toDbLocalTimestamp,
} from "@/lib/server/db-time";
import { fetchPythonApi } from "@/lib/server/python-api-proxy";
import { canWriteProjectId } from "@/lib/server/task-route-utils";
import {
  applyRecurrenceScheduleSegment,
  getRecurrenceSegmentEnvelopeMs,
  type RecurrenceScheduleSegment,
} from "@/lib/recurrence-schedule-segments";
import { computeOccurrenceCandidatesInRange } from "@/lib/recurrence-preview";
import { parseRrule } from "@/lib/recurrence-rrule";

function parseDate(value: unknown, fieldName: string): Date {
  const parsed =
    typeof value === "string" || value instanceof Date
      ? parseDisplayDateAsDbTimestamp(value)
      : null;
  if (!parsed) {
    throw new Error(`Invalid ${fieldName}`);
  }
  return parsed;
}

function serializeOccurrenceTimestamp(
  value: Date | string | null | undefined,
  allDay: boolean,
): string | null {
  const serialized = serializeDbTimestamp(value);
  return allDay && serialized ? serialized.slice(0, 10) : serialized;
}

function previousDay(value: Date): Date {
  return new Date(
    value.getFullYear(),
    value.getMonth(),
    value.getDate() - 1,
    0,
    0,
    0,
    0,
  );
}

function getTaskDurationMs(task: {
  startAt: Date | string | null;
  endAt: Date | string | null;
}): number {
  const startAt = dbTimestampToLocalDate(task.startAt);
  const endAt = dbTimestampToLocalDate(task.endAt);
  if (!startAt || !endAt) return 0;
  return Math.max(0, endAt.getTime() - startAt.getTime());
}

function resolveOccurrenceStatus(
  requestedStatus: unknown,
  fallbackStatus: unknown,
): string {
  if (typeof requestedStatus === "string") {
    return normalizeTaskStatus(requestedStatus) || "open";
  }
  return normalizeTaskStatus(fallbackStatus || "open") || "open";
}

async function deleteDueSoonNotifications(taskId: string) {
  await db
    .delete(notificationDeliveries)
    .where(
      and(
        eq(notificationDeliveries.taskId, taskId),
        eq(notificationDeliveries.notificationType, "due_soon"),
      ),
    );
}

async function getTaskWithRecurrence(taskId: string) {
  const [task] = await db
    .select()
    .from(tasks)
    .where(and(eq(tasks.id, taskId), isNull(tasks.deletedAt)))
    .limit(1);
  if (!task) return { task: null, rule: null };

  const [rule] = await db
    .select()
    .from(taskRecurrenceRules)
    .where(eq(taskRecurrenceRules.taskId, taskId))
    .limit(1);

  return { task, rule: rule ?? null };
}

async function ensureSingleSkipRow(params: {
  taskId: string;
  occurrenceId?: string | null;
  actualOccurrenceStartAt?: Date | null;
  occurrenceStartAt: Date;
  occurrenceEndAt: Date;
  status: string | null;
  allDay: boolean | null;
  reminderOffsets: unknown;
}) {
  const {
    taskId,
    occurrenceStartAt,
    occurrenceEndAt,
    status,
    allDay,
    reminderOffsets,
  } = params;

  let existing = params.occurrenceId
    ? (
        await db
          .select()
          .from(taskOccurrences)
          .where(
            and(
              eq(taskOccurrences.id, params.occurrenceId),
              eq(taskOccurrences.taskId, taskId),
            ),
          )
          .limit(1)
      )[0] ?? null
    : null;
  if (
    existing &&
    !matchesOccurrenceIdentity({
      sourceKind: existing.sourceKind,
      originalStartAt: existing.originalStartAt,
      startAt: existing.startAt,
      canonicalStartAt: occurrenceStartAt,
      actualStartAt: params.actualOccurrenceStartAt ?? occurrenceStartAt,
    })
  ) {
    existing = null;
  }
  if (!existing) {
    [existing] = await db
    .select()
    .from(taskOccurrences)
    .where(
      and(
        eq(taskOccurrences.taskId, taskId),
        eq(taskOccurrences.sourceKind, buildRecurrenceSkipSourceKind()),
        eq(
          taskOccurrences.originalStartAt,
          toDbLocalTimestamp(occurrenceStartAt),
        ),
      ),
    )
    .limit(1);
  }

  // Legacy skip rows predate original_start_at.  Their source kind is still
  // canonical, so use the old timestamp lookup only after restricting the
  // row to recurrence_skip (never a normal materialized row).
  if (!existing) {
    [existing] = await db
      .select()
      .from(taskOccurrences)
      .where(
        and(
          eq(taskOccurrences.taskId, taskId),
          eq(taskOccurrences.sourceKind, buildRecurrenceSkipSourceKind()),
          eq(taskOccurrences.startAt, toDbLocalTimestamp(occurrenceStartAt)),
        ),
      )
      .limit(1);
  }
  if (!existing) {
    // Reuse the canonical normal row when available.  Leaving it alongside
    // the new skip+override pair would expose a duplicate old occurrence to
    // FastAPI/Mobile list readers and notification workers.
    [existing] = await db
      .select()
      .from(taskOccurrences)
      .where(
        and(
          eq(taskOccurrences.taskId, taskId),
          eq(
            taskOccurrences.originalStartAt,
            toDbLocalTimestamp(occurrenceStartAt),
          ),
          inArray(taskOccurrences.sourceKind, ["recurrence", "task_schedule"]),
        ),
      )
      .limit(1);
  }
  if (!existing) {
    // Legacy materialized rows may still have original_start_at=NULL.  The
    // pre-migration fallback is safe only when the row is a normal recurrence
    // row at the canonical timestamp; never select an arbitrary actual-time
    // row after actual timestamp collisions became valid.
    [existing] = await db
      .select()
      .from(taskOccurrences)
      .where(
        and(
          eq(taskOccurrences.taskId, taskId),
          eq(taskOccurrences.startAt, toDbLocalTimestamp(occurrenceStartAt)),
          inArray(taskOccurrences.sourceKind, ["recurrence", "task_schedule"]),
        ),
      )
      .limit(1);
  }

  if (existing && isRecurrenceSkipSourceKind(existing.sourceKind)) {
    if (!existing.originalStartAt) {
      const [updated] = await db
        .update(taskOccurrences)
        .set({
          originalStartAt: toDbLocalTimestamp(occurrenceStartAt),
          updatedAt: new Date(),
        })
        .where(eq(taskOccurrences.id, existing.id))
        .returning();
      return updated;
    }
    return existing;
  }

  const payload = {
    taskId,
    startAt: toDbLocalTimestamp(occurrenceStartAt),
    endAt: toDbLocalTimestamp(occurrenceEndAt),
    originalStartAt: toDbLocalTimestamp(occurrenceStartAt),
    status: status ?? "open",
    allDay: !!allDay,
    reminderOffsets: reminderOffsets ?? [],
    sourceKind: buildRecurrenceSkipSourceKind(),
    isGenerated: false,
    updatedAt: new Date(),
  };

  if (existing) {
    const [updated] = await db
      .update(taskOccurrences)
      .set(payload)
      .where(eq(taskOccurrences.id, existing.id))
      .returning();
    return updated;
  }

  const [created] = await db
    .insert(taskOccurrences)
    .values(payload)
    .returning();
  return created;
}

async function upsertOverrideRow(params: {
  taskId: string;
  originalStartAtText: string;
  occurrenceId?: string | null;
  reuseOccurrenceId?: boolean;
  nextStartAt: Date;
  nextEndAt: Date;
  status: string | null;
  allDay: boolean | null;
  reminderOffsets: unknown;
}) {
  const {
    taskId,
    originalStartAtText,
    occurrenceId,
    reuseOccurrenceId = false,
    nextStartAt,
    nextEndAt,
    status,
    allDay,
    reminderOffsets,
  } = params;

  const sourceKind = buildRecurrenceOverrideSourceKind(originalStartAtText);

  let existing = occurrenceId
    ? ((
        await db
          .select()
          .from(taskOccurrences)
          .where(
            and(
              eq(taskOccurrences.id, occurrenceId),
              eq(taskOccurrences.taskId, taskId),
            ),
          )
          .limit(1)
      )[0] ?? null)
    : null;

  if (shouldFindOccurrenceByStartAt(occurrenceId, reuseOccurrenceId)) {
    // Resolve an existing override by canonical identity, not actual start.
    // Segment offsets can make two canonical occurrences share one displayed
    // timestamp, so task_id + start_at is no longer a safe lookup key.
    existing =
      (
        await db
          .select()
          .from(taskOccurrences)
          .where(
            and(
              eq(taskOccurrences.taskId, taskId),
              eq(
                taskOccurrences.originalStartAt,
                toDbLocalTimestamp(
                  parseDate(originalStartAtText, "original_start_at"),
                ),
              ),
            ),
          )
          .limit(1)
      )[0] ?? null;
  }

  if (existing && reuseOccurrenceId) {
    const existingStartAt = dbTimestampToLocalDate(existing.startAt);
    if (
      !existingStartAt ||
      existingStartAt.getTime() !== nextStartAt.getTime() ||
      isRecurrenceSkipSourceKind(existing.sourceKind)
    ) {
      throw new Error("Occurrence does not match requested start");
    }
  }

  if (
    !existing ||
    !canReuseOccurrenceRowForOverride(existing.sourceKind, reuseOccurrenceId)
  ) {
    existing =
      (
        await db
          .select()
          .from(taskOccurrences)
          .where(
            and(
              eq(taskOccurrences.taskId, taskId),
              eq(taskOccurrences.sourceKind, sourceKind),
            ),
          )
          .limit(1)
      )[0] ?? null;
  }

  const payload = {
    taskId,
    startAt: toDbLocalTimestamp(nextStartAt),
    endAt: toDbLocalTimestamp(nextEndAt),
    originalStartAt: toDbLocalTimestamp(
      parseDate(originalStartAtText, "original_start_at"),
    ),
    status: status ?? "open",
    allDay: !!allDay,
    reminderOffsets: reminderOffsets ?? [],
    sourceKind,
    isGenerated: false,
    updatedAt: new Date(),
  };

  if (existing) {
    const [updated] = await db
      .update(taskOccurrences)
      .set(payload)
      .where(eq(taskOccurrences.id, existing.id))
      .returning();
    return updated;
  }

  const [created] = await db
    .insert(taskOccurrences)
    .values(payload)
    .returning();
  return created;
}

function resolveStoredOccurrenceOriginalStartAt(row: {
  sourceKind: string | null;
  originalStartAt?: Date | string | null;
  startAt: Date | string;
}): Date | null {
  const value =
    row.originalStartAt ??
    resolveOccurrenceOriginalStartAt(row.sourceKind, row.startAt);
  return value ? dbTimestampToLocalDate(value) : null;
}

function resolveFutureSegmentResult(params: {
  canonicalStart: Date;
  canonicalEnd: Date;
  baseStartAt: Date;
  baseEndAt: Date;
  nextStartAt: Date;
  nextEndAt: Date;
  allDay: boolean;
}) {
  const startOffsetSeconds = Math.round(
    (params.nextStartAt.getTime() - params.baseStartAt.getTime()) / 1000,
  );
  const endOffsetSeconds = Math.round(
    (params.nextEndAt.getTime() - params.baseEndAt.getTime()) / 1000,
  );
  return {
    startOffsetSeconds,
    endOffsetSeconds,
    ...applyRecurrenceScheduleSegment({
      canonicalStart: params.canonicalStart,
      canonicalEnd: params.canonicalEnd,
      baseStartAt: params.baseStartAt,
      baseEndAt: params.baseEndAt,
      baseAllDay: params.allDay,
      segments: [
        {
          effectiveFrom: params.canonicalStart,
          startOffsetSeconds,
          endOffsetSeconds,
          allDay: params.allDay,
        },
      ],
    }),
  };
}

export async function PATCH(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "Not authenticated" }, { status: 401 });
  }

  const { id } = await params;
  const body = await request.json();

  try {
    const { task, rule } = await getTaskWithRecurrence(id);
    if (!task || !rule) {
      return NextResponse.json(
        { detail: "Recurring task not found" },
        { status: 404 },
      );
    }
    if (!(await canWriteProjectId(user, task.projectId))) {
      return NextResponse.json(
        { detail: "Permission denied" },
        { status: 403 },
      );
    }

    const occurrenceStartAt = parseDate(
      body.occurrence_start_at,
      "occurrence_start_at",
    );
    const originalStartAt = body.original_start_at
      ? parseDate(body.original_start_at, "original_start_at")
      : occurrenceStartAt;
    const originalStartAtText =
      serializeDbTimestamp(originalStartAt) ?? String(body.original_start_at);
    const occurrenceEndAt =
      body.occurrence_end_at != null
        ? parseDate(body.occurrence_end_at, "occurrence_end_at")
        : task.endAt && task.startAt
          ? new Date(occurrenceStartAt.getTime() + getTaskDurationMs(task))
          : new Date(occurrenceStartAt);

    if (typeof body.status === "string" && !body.next_start_at) {
      const override = await upsertOverrideRow({
        taskId: id,
        originalStartAtText,
        occurrenceId:
          typeof body.occurrence_id === "string" ? body.occurrence_id : null,
        reuseOccurrenceId: true,
        nextStartAt: occurrenceStartAt,
        nextEndAt: occurrenceEndAt,
        status: normalizeTaskStatus(body.status || "open") || "open",
        allDay:
          typeof body.all_day === "boolean"
            ? body.all_day
            : (task.allDay ?? false),
        reminderOffsets: task.reminderOffsets,
      });

      await deleteDueSoonNotifications(id);

      return NextResponse.json({
        success: true,
        occurrence: {
          id: override.id,
          task_id: override.taskId,
          status: override.status,
          start_at: serializeOccurrenceTimestamp(
            override.startAt,
            override.allDay ?? false,
          ),
          end_at: serializeOccurrenceTimestamp(
            override.endAt,
            override.allDay ?? false,
          ),
          all_day: override.allDay ?? false,
          source_kind: override.sourceKind,
          original_start_at: originalStartAtText,
        },
      });
    }

    const nextStartAt = parseDate(body.next_start_at, "next_start_at");
    const nextEndAt =
      body.next_end_at != null
        ? parseDate(body.next_end_at, "next_end_at")
        : new Date(
            nextStartAt.getTime() +
              Math.max(
                0,
                occurrenceEndAt.getTime() - occurrenceStartAt.getTime(),
              ),
          );

    const mode = body.mode === "future" ? "future" : "single";

    if (mode === "future") {
      const canonicalStart = originalStartAt;
      const baseDurationMs = getTaskDurationMs(task);
      const canonicalEnd = new Date(canonicalStart.getTime() + baseDurationMs);
      const effectiveAllDay =
        typeof body.all_day === "boolean"
          ? body.all_day
          : (task.allDay ?? false);
      let baseStartAt = occurrenceStartAt;
      let baseEndAt = occurrenceEndAt;
      const taskBaseStart = dbTimestampToLocalDate(task.startAt ?? task.endAt);
      if (taskBaseStart?.getTime() === canonicalStart.getTime()) {
        // The range generator intentionally returns occurrences after the
        // base row.  For a second future edit of the first occurrence, use
        // the unshifted task base rather than the currently displayed time so
        // absolute offsets do not accumulate.
        baseStartAt = taskBaseStart;
        baseEndAt = new Date(taskBaseStart.getTime() + baseDurationMs);
      }
      if (taskBaseStart) {
        const parsedRule = parseRrule(rule.rrule);
        const candidates = computeOccurrenceCandidatesInRange(
          taskBaseStart,
          {
            freq: parsedRule.freq,
            interval: parsedRule.interval,
            byDay: parsedRule.byDay,
            skipWeekend: rule.skipWeekend ?? false,
            skipHoliday: rule.skipHoliday ?? false,
            skipMode: rule.skipMode ?? "shift_forward",
            endCount: rule.endCount ?? null,
            endDate: rule.endDate ? serializeDbTimestamp(rule.endDate) : null,
          },
          new Date(canonicalStart.getTime() - 16 * 24 * 60 * 60 * 1000),
          new Date(canonicalStart.getTime() + 16 * 24 * 60 * 60 * 1000),
          20000,
        );
        const candidate = candidates.find(
          (value) =>
            value.canonicalStart.getTime() === canonicalStart.getTime(),
        );
        if (candidate) {
          baseStartAt = candidate.occurrenceStart;
          baseEndAt = new Date(baseStartAt.getTime() + baseDurationMs);
        }
      }
      const segmentValues = resolveFutureSegmentResult({
        canonicalStart,
        canonicalEnd,
        baseStartAt,
        baseEndAt,
        nextStartAt,
        nextEndAt,
        allDay: effectiveAllDay,
      });

      await db.transaction(async (tx) => {
        // Replacing a future boundary also replaces any later schedule
        // changes.  This prevents an older future edit from taking effect
        // again after the newly selected boundary.
        await tx
          .delete(taskRecurrenceScheduleSegments)
          .where(
            and(
              eq(taskRecurrenceScheduleSegments.taskId, id),
              gte(
                taskRecurrenceScheduleSegments.effectiveFrom,
                toDbLocalTimestamp(canonicalStart),
              ),
            ),
          );

        await tx
          .insert(taskRecurrenceScheduleSegments)
          .values({
            taskId: id,
            effectiveFrom: toDbLocalTimestamp(canonicalStart),
            startOffsetSeconds: segmentValues.startOffsetSeconds,
            endOffsetSeconds: segmentValues.endOffsetSeconds,
            allDay: segmentValues.allDay,
            updatedAt: new Date(),
          })
          .onConflictDoUpdate({
            target: [
              taskRecurrenceScheduleSegments.taskId,
              taskRecurrenceScheduleSegments.effectiveFrom,
            ],
            set: {
              startOffsetSeconds: segmentValues.startOffsetSeconds,
              endOffsetSeconds: segmentValues.endOffsetSeconds,
              allDay: segmentValues.allDay,
              updatedAt: new Date(),
            },
          });

        // A single exception at the selected boundary is promoted to the
        // series segment.  Exceptions at earlier/later canonical boundaries
        // remain intact so the future mutation cannot silently lose data.
        const rows = await tx
          .select({
            id: taskOccurrences.id,
            sourceKind: taskOccurrences.sourceKind,
            startAt: taskOccurrences.startAt,
            endAt: taskOccurrences.endAt,
            originalStartAt: taskOccurrences.originalStartAt,
            status: taskOccurrences.status,
            reminderOffsets: taskOccurrences.reminderOffsets,
          })
          .from(taskOccurrences)
          .where(eq(taskOccurrences.taskId, id));
        const exceptionIds = rows
          .filter(
            (row) =>
              isRecurrenceSkipSourceKind(row.sourceKind) ||
              isRecurrenceOverrideSourceKind(row.sourceKind),
          )
          .filter((row) => {
            const rowCanonical = resolveStoredOccurrenceOriginalStartAt(row);
            return (
              rowCanonical !== null &&
              rowCanonical.getTime() === canonicalStart.getTime()
            );
          })
          .map((row) => row.id);
        const boundaryException = rows
          .filter((row) => {
            const rowCanonical = resolveStoredOccurrenceOriginalStartAt(row);
            return (
              rowCanonical !== null &&
              rowCanonical.getTime() === canonicalStart.getTime() &&
              (isRecurrenceOverrideSourceKind(row.sourceKind) ||
                isRecurrenceSkipSourceKind(row.sourceKind))
            );
          })
          .sort(
            (a, b) =>
              Number(isRecurrenceOverrideSourceKind(b.sourceKind)) -
              Number(isRecurrenceOverrideSourceKind(a.sourceKind)),
          )[0];
        if (exceptionIds.length > 0) {
          // Keep the occurrence row's historical activity/time-entry records
          // while detaching their optional occurrence reference.  The FK
          // columns are not ON DELETE CASCADE, so deleting a boundary
          // skip/override without this step can make a valid future edit
          // fail with a constraint violation.
          await tx
            .update(notificationDeliveries)
            .set({ occurrenceId: null })
            .where(
              inArray(notificationDeliveries.occurrenceId, exceptionIds),
            );
          await tx
            .update(timeEntries)
            .set({ occurrenceId: null })
            .where(inArray(timeEntries.occurrenceId, exceptionIds));
          await tx
            .delete(taskOccurrences)
            .where(inArray(taskOccurrences.id, exceptionIds));
        }

        // Reconcile already-materialized normal rows in place.  The Web GET
        // route can re-apply a segment at read time, but FastAPI/Mobile and
        // notification workers read these stored timestamps directly.  Keep
        // the same row IDs and canonical identity while updating their actual
        // values inside this transaction.
        const activeSegmentRows = await tx
          .select({
            effectiveFrom: taskRecurrenceScheduleSegments.effectiveFrom,
            startOffsetSeconds:
              taskRecurrenceScheduleSegments.startOffsetSeconds,
            endOffsetSeconds:
              taskRecurrenceScheduleSegments.endOffsetSeconds,
            allDay: taskRecurrenceScheduleSegments.allDay,
          })
          .from(taskRecurrenceScheduleSegments)
          .where(eq(taskRecurrenceScheduleSegments.taskId, id));
        const activeSegments: RecurrenceScheduleSegment[] =
          activeSegmentRows.map((row) => ({
            effectiveFrom: row.effectiveFrom,
            startOffsetSeconds: row.startOffsetSeconds ?? 0,
            endOffsetSeconds: row.endOffsetSeconds ?? 0,
            allDay: row.allDay ?? false,
          }));
        const taskBaseStart = dbTimestampToLocalDate(task.startAt ?? task.endAt);
        if (taskBaseStart) {
          let canonicalRows = rows
            .map((row) => ({
              row,
              canonical: resolveStoredOccurrenceOriginalStartAt(row),
            }))
            .filter(
              (entry): entry is { row: (typeof rows)[number]; canonical: Date } =>
                entry.canonical !== null &&
                !isRecurrenceSkipSourceKind(entry.row.sourceKind) &&
                !isRecurrenceOverrideSourceKind(entry.row.sourceKind),
            );
          const canonicalTimes = rows
            .map((row) => dbTimestampToLocalDate(row.startAt)?.getTime())
            .filter((value): value is number => value !== undefined);
          const minCanonical = Math.min(
            canonicalStart.getTime(),
            ...canonicalTimes,
          );
          const maxCanonical = Math.max(
            canonicalStart.getTime(),
            ...canonicalTimes,
          );
          const paddingMs = getRecurrenceSegmentEnvelopeMs(activeSegments);
          const candidateRangeStart = new Date(
            minCanonical - baseDurationMs - paddingMs,
          );
          const candidateRangeEnd = new Date(
            maxCanonical + baseDurationMs + paddingMs,
          );
          const parsedRule = parseRrule(rule.rrule);
          const candidates = computeOccurrenceCandidatesInRange(
            taskBaseStart,
            {
              freq: parsedRule.freq,
              interval: parsedRule.interval,
              byDay: parsedRule.byDay,
              skipWeekend: rule.skipWeekend ?? false,
              skipHoliday: rule.skipHoliday ?? false,
              skipMode: rule.skipMode ?? "shift_forward",
              endCount: rule.endCount ?? null,
              endDate: rule.endDate
                ? serializeDbTimestamp(rule.endDate)
                : null,
            },
            candidateRangeStart,
            candidateRangeEnd,
            20000,
          );
          const candidateByActual = new Map<number, (typeof candidates)[number]>();
          for (const candidate of candidates) {
            if (!candidateByActual.has(candidate.occurrenceStart.getTime())) {
              candidateByActual.set(candidate.occurrenceStart.getTime(), candidate);
            }
          }
          // Recover raw canonical identity for legacy normal rows that still
          // have original_start_at=NULL.  Their stored actual timestamp is
          // matched against the same canonical/actual pair generator used by
          // the dynamic route; do this before applying the new segment.
          canonicalRows = canonicalRows.map((entry) => {
            if (entry.row.originalStartAt === null) {
              const rowStart = dbTimestampToLocalDate(entry.row.startAt);
              const candidate = rowStart
                ? candidateByActual.get(rowStart.getTime())
                : undefined;
              if (candidate) return { ...entry, canonical: candidate.canonicalStart };
            }
            return entry;
          });
          const candidateByCanonical = new Map(
            candidates.map((candidate) => [
              candidate.canonicalStart.getTime(),
              candidate,
            ]),
          );
          for (const { row, canonical } of canonicalRows) {
            const candidate = candidateByCanonical.get(canonical.getTime());
            const storedStart = dbTimestampToLocalDate(row.startAt);
            const storedEnd = dbTimestampToLocalDate(row.endAt);
            const baseStart =
              candidate?.occurrenceStart ??
              (canonical.getTime() === taskBaseStart.getTime()
                ? canonical
                : storedStart);
            if (!baseStart) continue;
            const baseEnd = candidate
              ? new Date(baseStart.getTime() + baseDurationMs)
              : storedEnd;
            if (!baseEnd) continue;
            const applied = applyRecurrenceScheduleSegment({
              canonicalStart: canonical,
              canonicalEnd: new Date(canonical.getTime() + baseDurationMs),
              baseStartAt: baseStart,
              baseEndAt: baseEnd,
              baseAllDay: task.allDay ?? false,
              segments: activeSegments,
            });
            if (!applied.endAt) continue;
            await tx
              .update(taskOccurrences)
              .set({
                startAt: toDbLocalTimestamp(applied.startAt),
                endAt: toDbLocalTimestamp(applied.endAt),
                originalStartAt: toDbLocalTimestamp(canonical),
                allDay: applied.allDay,
                updatedAt: new Date(),
              })
              .where(eq(taskOccurrences.id, row.id));
          }

          const reconciledCanonicalKeys = new Set(
            canonicalRows.map((entry) => entry.canonical.getTime()),
          );
          if (!reconciledCanonicalKeys.has(canonicalStart.getTime())) {
            const candidate = candidateByCanonical.get(canonicalStart.getTime());
            const baseStart = candidate?.occurrenceStart ?? canonicalStart;
            const baseEnd = new Date(baseStart.getTime() + baseDurationMs);
            const applied = applyRecurrenceScheduleSegment({
              canonicalStart,
              canonicalEnd,
              baseStartAt: baseStart,
              baseEndAt: baseEnd,
              baseAllDay: task.allDay ?? false,
              segments: activeSegments,
            });
            if (applied.endAt) {
              await tx.insert(taskOccurrences).values({
                taskId: id,
                startAt: toDbLocalTimestamp(applied.startAt),
                endAt: toDbLocalTimestamp(applied.endAt),
                originalStartAt: toDbLocalTimestamp(canonicalStart),
                status: boundaryException?.status ?? task.status ?? "open",
                allDay: applied.allDay,
                reminderOffsets:
                  boundaryException?.reminderOffsets ??
                  task.reminderOffsets ??
                  [],
                sourceKind: "recurrence",
                isGenerated: true,
                updatedAt: new Date(),
              });
            }
          }
        }
      });

      await deleteDueSoonNotifications(id);

      return NextResponse.json({
        success: true,
        occurrence: {
          id: `generated-${id}-${serializeDbTimestamp(canonicalStart) ?? canonicalStart.toISOString()}`,
          task_id: id,
          status: resolveOccurrenceStatus(body.status, task.status),
          start_at: serializeOccurrenceTimestamp(nextStartAt, effectiveAllDay),
          end_at: serializeOccurrenceTimestamp(nextEndAt, effectiveAllDay),
          source_kind: "rrule",
          original_start_at: serializeDbTimestamp(canonicalStart),
          all_day: effectiveAllDay,
        },
      });
    }

    await ensureSingleSkipRow({
      taskId: id,
      occurrenceId:
        typeof body.occurrence_id === "string" ? body.occurrence_id : null,
      actualOccurrenceStartAt: occurrenceStartAt,
      occurrenceStartAt: originalStartAt,
      occurrenceEndAt,
      status: task.status,
      allDay: task.allDay,
      reminderOffsets: task.reminderOffsets,
    });

    const override = await upsertOverrideRow({
      taskId: id,
      originalStartAtText,
      occurrenceId:
        typeof body.occurrence_id === "string" ? body.occurrence_id : null,
      nextStartAt,
      nextEndAt,
      status: resolveOccurrenceStatus(body.status, task.status),
      allDay:
        typeof body.all_day === "boolean"
          ? body.all_day
          : (task.allDay ?? false),
      reminderOffsets: task.reminderOffsets,
    });

    await deleteDueSoonNotifications(id);

    return NextResponse.json({
      success: true,
      occurrence: {
        id: override.id,
        task_id: override.taskId,
        status: override.status,
        start_at: serializeOccurrenceTimestamp(
          override.startAt,
          override.allDay ?? false,
        ),
        end_at: serializeOccurrenceTimestamp(
          override.endAt,
          override.allDay ?? false,
        ),
        all_day: override.allDay ?? false,
        source_kind: override.sourceKind,
        original_start_at: originalStartAtText,
      },
    });
  } catch (error) {
    const detail =
      error instanceof Error ? error.message : "Failed to move occurrence";
    return NextResponse.json({ detail }, { status: 400 });
  }
}

export async function DELETE(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "Not authenticated" }, { status: 401 });
  }

  const { id } = await params;
  const body = await request.json().catch(() => ({}));

  try {
    const { task, rule } = await getTaskWithRecurrence(id);
    if (!task || !rule) {
      return NextResponse.json(
        { detail: "Recurring task not found" },
        { status: 404 },
      );
    }
    if (!(await canWriteProjectId(user, task.projectId))) {
      return NextResponse.json(
        { detail: "Permission denied" },
        { status: 403 },
      );
    }

    const mode = body.mode === "future" ? "future" : "single";
    const occurrenceStartAt = parseDate(
      body.occurrence_start_at,
      "occurrence_start_at",
    );
    const occurrenceEndAt =
      body.occurrence_end_at != null
        ? parseDate(body.occurrence_end_at, "occurrence_end_at")
        : task.endAt && task.startAt
          ? new Date(occurrenceStartAt.getTime() + getTaskDurationMs(task))
          : new Date(occurrenceStartAt);
    const originalStartAt = parseDate(
      body.original_start_at || body.occurrence_start_at,
      "original_start_at",
    );

    if (mode === "single") {
      if (typeof body.occurrence_id === "string") {
        const [existing] = await db
          .select()
          .from(taskOccurrences)
          .where(
            and(
              eq(taskOccurrences.id, body.occurrence_id),
              eq(taskOccurrences.taskId, id),
            ),
          )
          .limit(1);
        if (existing && isRecurrenceOverrideSourceKind(existing.sourceKind)) {
          await db
            .delete(taskOccurrences)
            .where(eq(taskOccurrences.id, existing.id));
        }
      }

      await ensureSingleSkipRow({
        taskId: id,
        occurrenceId:
          typeof body.occurrence_id === "string" ? body.occurrence_id : null,
        actualOccurrenceStartAt: occurrenceStartAt,
        occurrenceStartAt: originalStartAt,
        occurrenceEndAt,
        status: task.status,
        allDay: task.allDay,
        reminderOffsets: task.reminderOffsets,
      });

      await deleteDueSoonNotifications(id);
      return NextResponse.json({ success: true });
    }

    const cutoffStartAt = serializeDbTimestamp(originalStartAt);
    // 開始日時が未設定の繰り返しタスクは、GET 側 (task-occurrences/route.ts) が
    // end_at を base 回の開始として扱う。ここも同じ基準に揃えないと、
    // start_at が NULL の繰り返しタスクはこの分岐に入れず、
    // どの回で「今回以降を削除」を押してもタスク本体が消えないままになる。
    const taskStartAt = dbTimestampToLocalDate(task.startAt ?? task.endAt);
    if (taskStartAt && taskStartAt.getTime() >= originalStartAt.getTime()) {
      await deleteDueSoonNotifications(id);
      const upstream = await fetchPythonApi(
        `/api/tasks/${encodeURIComponent(id)}`,
        {
          method: "DELETE",
          user,
        },
      );
      if (!upstream.ok) {
        const body = await upstream.text().catch(() => "");
        return new NextResponse(
          body || JSON.stringify({ detail: "タスクの削除に失敗しました" }),
          {
            status: upstream.status,
            headers: {
              "content-type":
                upstream.headers.get("content-type") ?? "application/json",
            },
          },
        );
      }
      return NextResponse.json({ success: true, deleted_task: true });
    }

    const taskOccurrenceRows = await db
      .select()
      .from(taskOccurrences)
      .where(eq(taskOccurrences.taskId, id));

    // cutoff 以降の回に対応する保存済みオカレンスは、source_kind を問わず削除する。
    // 判定に使う時刻は「元々どの回だったか」であり、
    //   - 別日へ移動した回（ro: / recurrence_override:）は source_kind に埋まった元の開始時刻
    //   - それ以外（recurrence_skip、materialize 済みの recurrence、task_schedule）は行自身の開始時刻
    // を見る。
    // 以前は override 以外を一律 false にしていたため、Python 側が materialize した
    // source_kind="recurrence" の実体行が1件も消えず、繰り返しルールの endDate だけが
    // 更新されていた。ルール行は残るので GET 側の innerJoin を通過し続け、
    // 「今回以降を削除」を押してもカレンダーの表示が一切変わらなかった。
    const staleOccurrenceIds = taskOccurrenceRows
      .filter((row) => {
        const cutoffSource = resolveOccurrenceCutoffSource(row.sourceKind);
        const anchor =
          cutoffSource.from === "original"
            ? parseDate(cutoffSource.originalStartAt, "original_start_at")
            : dbTimestampToLocalDate(row.startAt);
        return !!anchor && anchor.getTime() >= originalStartAt.getTime();
      })
      .map((row) => row.id);

    if (staleOccurrenceIds.length > 0) {
      // notification_deliveries.occurrence_id と time_entries.occurrence_id は
      // ON DELETE 指定の無い外部キーなので、参照を残したまま消すと
      // ForeignKeyViolation になり削除リクエストごと 400 で失敗する。
      // タスク自体は残るため、配信済み通知や実績時間の記録は消さず参照だけ外す。
      await db
        .update(notificationDeliveries)
        .set({ occurrenceId: null })
        .where(
          inArray(notificationDeliveries.occurrenceId, staleOccurrenceIds),
        );
      await db
        .update(timeEntries)
        .set({ occurrenceId: null })
        .where(inArray(timeEntries.occurrenceId, staleOccurrenceIds));
      await db
        .delete(taskOccurrences)
        .where(inArray(taskOccurrences.id, staleOccurrenceIds));
    }

    await db
      .update(taskRecurrenceRules)
      .set({
        recurForever: false,
        endCount: null,
        endDate: toDbLocalTimestamp(previousDay(originalStartAt)),
        updatedAt: new Date(),
      })
      .where(eq(taskRecurrenceRules.taskId, id));

    await deleteDueSoonNotifications(id);

    return NextResponse.json({
      success: true,
      cutoff_start_at: cutoffStartAt,
    });
  } catch (error) {
    const detail =
      error instanceof Error ? error.message : "Failed to delete occurrence";
    return NextResponse.json({ detail }, { status: 400 });
  }
}
