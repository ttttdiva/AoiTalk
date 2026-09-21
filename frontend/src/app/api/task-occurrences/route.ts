import { NextRequest, NextResponse } from "next/server";
import { db } from "@/db";
import {
  taskOccurrences,
  tasks,
  projects,
  taskRecurrenceScheduleSegments,
  taskRecurrenceRules,
  taskTags,
  tags,
} from "@/db/schema";
import { eq, and, inArray, isNotNull, isNull } from "drizzle-orm";
import { getSession } from "@/lib/auth";
import { computeOccurrenceCandidatesInRange } from "@/lib/recurrence-preview";
import type { RecurrencePreviewConfig } from "@/lib/recurrence-preview";
import {
  applyOccurrenceDuration,
  getOccurrenceDurationMs,
} from "@/lib/recurrence-schedule";
import {
  applyRecurrenceScheduleSegment,
  getRecurrenceSegmentEnvelopeMs,
  type RecurrenceScheduleSegment,
} from "@/lib/recurrence-schedule-segments";
import {
  isRecurrenceOverrideSourceKind,
  isRecurrenceSkipSourceKind,
  resolveOccurrenceOriginalStartAt,
} from "@/lib/recurrence-exceptions";
import { normalizeTaskStatus } from "@/lib/task-status";
import { estimateOccurrenceCount, parseRrule } from "@/lib/recurrence-rrule";
import {
  resolveReadScope,
  TaskBrowseScopeError,
} from "@/lib/server/task-route-utils";
import {
  dbTimestampToLocalDate,
  localDateToDbTimestampDate,
  parseDisplayDateAsDbTimestamp,
  serializeDbTimestamp,
  type DbTimestampValue,
} from "@/lib/server/db-time";

function extractProjectColor(value: unknown): string | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  const color = (value as Record<string, unknown>).color;
  return typeof color === "string" && color.trim() ? color : null;
}

function toLocalTimestampKey(value: DbTimestampValue): string {
  return serializeDbTimestamp(value)?.replace(/[-:]/g, "") ?? "";
}

function occurrenceKey(taskId: string, value: DbTimestampValue): string {
  return `${taskId}:${toLocalTimestampKey(value)}`;
}

function serializeOriginalStartAt(value: DbTimestampValue): string | null {
  const serialized = serializeDbTimestamp(value);
  return serialized ?? (typeof value === "string" ? value : null);
}

function serializeOccurrenceTimestamp(
  value: DbTimestampValue,
  allDay: boolean,
): string | null {
  const serialized = serializeDbTimestamp(value);
  return allDay && serialized ? serialized.slice(0, 10) : serialized;
}

function overlapsRange(
  start: Date | null,
  end: Date | null,
  rangeStart: Date,
  rangeEnd: Date,
): boolean {
  if (!start) return false;
  const effectiveEnd = end ?? start;
  return start <= rangeEnd && effectiveEnd >= rangeStart;
}

type OccurrenceResponse = {
  id: string;
  task_id: string;
  project_id: string;
  title: string | null;
  project_name: string | null;
  status: string;
  start_at: string | null;
  end_at: string | null;
  all_day: boolean;
  source_kind: string;
  is_generated: boolean;
  original_start_at: string | null;
  tags: {
    id: string;
    space_id: string;
    name: string;
    color: string | null;
    created_by: string | null;
    created_at: Date | null;
  }[];
  project_color: string | null;
};

function resolveFutureRecurringStatus(params: {
  status: unknown;
  resetStatusTo: unknown;
  triggerStatus: unknown;
  sourceKind?: string | null;
  startAt: Date;
  now: Date;
}): string {
  const status = normalizeTaskStatus(params.status || "open") || "open";
  if (
    params.startAt.getTime() <= params.now.getTime() ||
    status !== "closed" ||
    isRecurrenceOverrideSourceKind(params.sourceKind)
  ) {
    return status;
  }

  const resetStatus = normalizeTaskStatus(params.resetStatusTo || "open");
  const triggerStatus = normalizeTaskStatus(params.triggerStatus || "closed");
  if (
    resetStatus &&
    resetStatus !== "closed" &&
    resetStatus !== triggerStatus
  ) {
    return resetStatus;
  }
  return "open";
}

export async function GET(request: NextRequest) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const { searchParams } = new URL(request.url);
  const startFrom = searchParams.get("start_from");
  const endTo = searchParams.get("end_to");

  if (!startFrom || !endTo) {
    return NextResponse.json(
      { detail: "start_from, end_to は必須です" },
      { status: 400 },
    );
  }

  let rangeStartDb: Date | null = null;
  let rangeEndDb: Date | null = null;
  try {
    rangeStartDb = parseDisplayDateAsDbTimestamp(startFrom);
    rangeEndDb = parseDisplayDateAsDbTimestamp(endTo);
  } catch {
    rangeStartDb = null;
    rangeEndDb = null;
  }
  if (
    !rangeStartDb ||
    !rangeEndDb ||
    Number.isNaN(rangeStartDb.getTime()) ||
    Number.isNaN(rangeEndDb.getTime())
  ) {
    return NextResponse.json(
      { detail: "start_from, end_to が不正です" },
      { status: 400 },
    );
  }
  const rangeStart = rangeStartDb;
  const rangeEnd = rangeEndDb;
  const now = new Date();

  let scopedProjectIds: string[];
  try {
    scopedProjectIds = (await resolveReadScope(user, searchParams)).projectIds;
  } catch (error) {
    if (error instanceof TaskBrowseScopeError) {
      return NextResponse.json(
        { detail: error.message },
        { status: error.status },
      );
    }
    throw error;
  }
  if (scopedProjectIds.length === 0) return NextResponse.json([]);

  try {
    const storedRows = await db
      .select({
        id: taskOccurrences.id,
        taskId: taskOccurrences.taskId,
        title: tasks.title,
        status: taskOccurrences.status,
        startAt: taskOccurrences.startAt,
        endAt: taskOccurrences.endAt,
        originalStartAt: taskOccurrences.originalStartAt,
        allDay: taskOccurrences.allDay,
        sourceKind: taskOccurrences.sourceKind,
        isGenerated: taskOccurrences.isGenerated,
        resetStatusTo: taskRecurrenceRules.resetStatusTo,
        triggerStatus: taskRecurrenceRules.triggerStatus,
        projectId: tasks.projectId,
        projectName: projects.name,
        projectMetadata: projects.projectMetadata,
      })
      .from(taskOccurrences)
      .innerJoin(tasks, eq(taskOccurrences.taskId, tasks.id))
      // 繰り返しルールを持つタスクだけを対象にする。
      // 非繰り返しタスクは tasks 本体の start_at / end_at が予定の正本で、
      // カレンダーもタスク本体をそのままイベント化している（calendar-view.tsx の
      // `!task.has_recurrence` フィルタ）。一方 Python 側の materialize は
      // 繰り返しなしのタスクにも source_kind="task_schedule" のミラー行を1件作るため、
      // ここで一緒に返すとタスク本体とミラーの二重表示になる。
      // Web UI からの日付変更はミラー行を更新しないので、ミラーが古い日付のまま
      // 別日に居残るケース（同じタスクが離れた日に2回出る）も同じ原因。
      // タスク一覧 API (app/api/tasks/route.ts) も同様に繰り返しタスクへ限定済み。
      .innerJoin(taskRecurrenceRules, eq(taskRecurrenceRules.taskId, tasks.id))
      .innerJoin(projects, eq(tasks.projectId, projects.id))
      .where(
        and(
          inArray(tasks.projectId, scopedProjectIds),
          isNull(tasks.deletedAt),
          isNull(taskOccurrences.deletedAt),
          isNull(projects.deletedAt),
        ),
      );

    const recurringTasks = await db
      .select({
        taskId: tasks.id,
        title: tasks.title,
        status: tasks.status,
        startAt: tasks.startAt,
        endAt: tasks.endAt,
        allDay: tasks.allDay,
        projectId: tasks.projectId,
        projectName: projects.name,
        projectMetadata: projects.projectMetadata,
        rrule: taskRecurrenceRules.rrule,
        timezone: taskRecurrenceRules.timezone,
        horizonDays: taskRecurrenceRules.horizonDays,
        triggerStatus: taskRecurrenceRules.triggerStatus,
        resetStatusTo: taskRecurrenceRules.resetStatusTo,
        endCount: taskRecurrenceRules.endCount,
        endDate: taskRecurrenceRules.endDate,
        skipWeekend: taskRecurrenceRules.skipWeekend,
        skipHoliday: taskRecurrenceRules.skipHoliday,
        skipMode: taskRecurrenceRules.skipMode,
      })
      .from(tasks)
      .innerJoin(taskRecurrenceRules, eq(taskRecurrenceRules.taskId, tasks.id))
      .innerJoin(projects, eq(tasks.projectId, projects.id))
      .where(
        and(
          inArray(tasks.projectId, scopedProjectIds),
          isNull(tasks.deletedAt),
          isNull(projects.deletedAt),
          isNotNull(tasks.endAt),
        ),
      );

    const taskIds = [
      ...new Set([
        ...storedRows.map((row) => row.taskId),
        ...recurringTasks.map((row) => row.taskId),
      ]),
    ];

    const segmentRows =
      taskIds.length === 0
        ? []
        : await db
            .select({
              taskId: taskRecurrenceScheduleSegments.taskId,
              effectiveFrom: taskRecurrenceScheduleSegments.effectiveFrom,
              startOffsetSeconds:
                taskRecurrenceScheduleSegments.startOffsetSeconds,
              endOffsetSeconds: taskRecurrenceScheduleSegments.endOffsetSeconds,
              allDay: taskRecurrenceScheduleSegments.allDay,
            })
            .from(taskRecurrenceScheduleSegments)
            .where(inArray(taskRecurrenceScheduleSegments.taskId, taskIds));
    const segmentsByTask = new Map<string, RecurrenceScheduleSegment[]>();
    for (const row of segmentRows) {
      const segments = segmentsByTask.get(row.taskId) ?? [];
      segments.push({
        effectiveFrom: row.effectiveFrom,
        startOffsetSeconds: row.startOffsetSeconds ?? 0,
        endOffsetSeconds: row.endOffsetSeconds ?? 0,
        allDay: row.allDay ?? false,
      });
      segmentsByTask.set(row.taskId, segments);
    }
    const recurringTaskById = new Map(
      recurringTasks.map((task) => [task.taskId, task]),
    );
    const legacyCanonicalByTask = new Map<string, Map<number, Date>>();
    for (const task of recurringTasks) {
      const taskStart = dbTimestampToLocalDate(task.startAt ?? task.endAt);
      if (!taskStart) continue;
      const durationMs =
        task.startAt && task.endAt
          ? (getOccurrenceDurationMs(task.startAt, task.endAt) ?? 0)
          : 0;
      const segmentPadding = getRecurrenceSegmentEnvelopeMs(
        segmentsByTask.get(task.taskId) ?? [],
      );
      const parsed = parseRrule(task.rrule);
      const candidates = computeOccurrenceCandidatesInRange(
        taskStart,
        {
          freq: parsed.freq,
          interval: parsed.interval,
          byDay: parsed.byDay,
          skipWeekend: task.skipWeekend ?? false,
          skipHoliday: task.skipHoliday ?? false,
          skipMode: task.skipMode ?? "shift_forward",
          endCount: task.endCount ?? null,
          endDate: task.endDate ? serializeDbTimestamp(task.endDate) : null,
        },
        new Date(
          rangeStart.getTime() - durationMs - segmentPadding - 14 * 86400000,
        ),
        new Date(
          rangeEnd.getTime() + durationMs + segmentPadding + 14 * 86400000,
        ),
        20000,
      );
      const byActual = new Map<number, Date>();
      for (const candidate of candidates) {
        if (!byActual.has(candidate.occurrenceStart.getTime())) {
          byActual.set(
            candidate.occurrenceStart.getTime(),
            candidate.canonicalStart,
          );
        }
      }
      legacyCanonicalByTask.set(task.taskId, byActual);
    }

    const tagRows =
      taskIds.length === 0
        ? []
        : await db
            .select({
              taskId: taskTags.taskId,
              id: tags.id,
              spaceId: tags.spaceId,
              name: tags.name,
              color: tags.color,
              createdBy: tags.createdBy,
              createdAt: tags.createdAt,
            })
            .from(taskTags)
            .innerJoin(tags, eq(taskTags.tagId, tags.id))
            .where(inArray(taskTags.taskId, taskIds));

    const tagsByTask = new Map<string, OccurrenceResponse["tags"]>();
    for (const tag of tagRows) {
      const list = tagsByTask.get(tag.taskId) || [];
      list.push({
        id: tag.id,
        space_id: tag.spaceId,
        name: tag.name,
        color: tag.color,
        created_by: tag.createdBy,
        created_at: tag.createdAt,
      });
      tagsByTask.set(tag.taskId, list);
    }

    const occurrences = new Map<string, OccurrenceResponse>();
    const hiddenOccurrences = new Set<string>();
    const explicitCanonicalOccurrences = new Set<string>();
    const removeCanonicalOccurrences = (canonicalKey: string) => {
      for (const [key, occurrence] of occurrences) {
        if (
          occurrence.original_start_at &&
          occurrenceKey(occurrence.task_id, occurrence.original_start_at) ===
            canonicalKey
        ) {
          occurrences.delete(key);
        }
      }
    };

    for (const row of storedRows) {
      const rowStartAt = dbTimestampToLocalDate(row.startAt);
      if (!rowStartAt) continue;

      let originalStartAt = row.originalStartAt
        ? dbTimestampToLocalDate(row.originalStartAt)
        : dbTimestampToLocalDate(
            resolveOccurrenceOriginalStartAt(row.sourceKind, rowStartAt),
          );
      if (
        row.originalStartAt === null &&
        !isRecurrenceOverrideSourceKind(row.sourceKind) &&
        !isRecurrenceSkipSourceKind(row.sourceKind)
      ) {
        originalStartAt =
          legacyCanonicalByTask.get(row.taskId)?.get(rowStartAt.getTime()) ??
          originalStartAt;
      }
      const originalKey = originalStartAt
        ? occurrenceKey(row.taskId, originalStartAt)
        : null;

      if (originalKey && isRecurrenceSkipSourceKind(row.sourceKind)) {
        // SELECT order is not part of the contract.  If the override row was
        // seen first, it wins deterministically over the companion skip row.
        if (explicitCanonicalOccurrences.has(originalKey)) continue;
        hiddenOccurrences.add(originalKey);
        removeCanonicalOccurrences(originalKey);
        continue;
      }

      // A single override owns its canonical occurrence and therefore takes
      // precedence over a schedule segment.  Keep the canonical key even if
      // the override's displayed timestamp is outside the requested window;
      // otherwise dynamic generation would leak the old canonical occurrence.
      if (originalKey && isRecurrenceOverrideSourceKind(row.sourceKind)) {
        explicitCanonicalOccurrences.add(originalKey);
        hiddenOccurrences.delete(originalKey);
        removeCanonicalOccurrences(originalKey);
      }

      const isExplicitException =
        isRecurrenceOverrideSourceKind(row.sourceKind) ||
        isRecurrenceSkipSourceKind(row.sourceKind);
      const hasCanonicalIdentity = row.originalStartAt !== null;
      const segmentResult =
        !isExplicitException && !hasCanonicalIdentity && originalStartAt
          ? applyRecurrenceScheduleSegment({
              canonicalStart: originalStartAt,
              canonicalEnd: (() => {
                const task = recurringTaskById.get(row.taskId);
                const durationMs =
                  task?.startAt && task.endAt
                    ? getOccurrenceDurationMs(task.startAt, task.endAt)
                    : null;
                return durationMs !== null
                  ? new Date(originalStartAt.getTime() + durationMs)
                  : dbTimestampToLocalDate(row.endAt);
              })(),
              baseAllDay: row.allDay ?? false,
              segments: segmentsByTask.get(row.taskId) ?? [],
            })
          : null;
      // Materializers may already have applied the segment to a stored row.
      // Avoid shifting that row a second time while still remapping stale
      // canonical rows after a web-only future mutation.
      const rowAlreadyApplied =
        segmentResult !== null &&
        rowStartAt.getTime() === segmentResult.startAt.getTime() &&
        (row.allDay ?? false) === segmentResult.allDay &&
        (!segmentResult.endAt ||
          dbTimestampToLocalDate(row.endAt)?.getTime() ===
            segmentResult.endAt.getTime());
      const actualStartAt = rowAlreadyApplied
        ? rowStartAt
        : (segmentResult?.startAt ?? rowStartAt);
      const actualEndAt = rowAlreadyApplied
        ? dbTimestampToLocalDate(row.endAt)
        : (segmentResult?.endAt ?? dbTimestampToLocalDate(row.endAt));
      const actualAllDay = rowAlreadyApplied
        ? (row.allDay ?? false)
        : (segmentResult?.allDay ?? row.allDay ?? false);
      if (!overlapsRange(actualStartAt, actualEndAt, rangeStart, rangeEnd)) {
        continue;
      }

      const actualKey = occurrenceKey(row.taskId, actualStartAt);
      const key = originalKey ?? actualKey;
      occurrences.set(key, {
        id: row.id,
        task_id: row.taskId,
        project_id: row.projectId,
        title: row.title,
        project_name: row.projectName,
        status: resolveFutureRecurringStatus({
          status: row.status,
          resetStatusTo: row.resetStatusTo,
          triggerStatus: row.triggerStatus,
          sourceKind: row.sourceKind,
          startAt: actualStartAt,
          now,
        }),
        start_at: serializeOccurrenceTimestamp(actualStartAt, actualAllDay),
        end_at: serializeOccurrenceTimestamp(actualEndAt, actualAllDay),
        all_day: actualAllDay,
        source_kind: row.sourceKind ?? "task_schedule",
        is_generated: row.isGenerated ?? false,
        original_start_at: serializeOriginalStartAt(originalStartAt),
        tags: tagsByTask.get(row.taskId) || [],
        project_color: extractProjectColor(row.projectMetadata),
      });
    }

    for (const task of recurringTasks) {
      const baseStart = task.startAt ?? task.endAt;
      if (!baseStart) continue;
      const baseEnd =
        task.startAt && task.endAt
          ? task.endAt
          : (task.endAt ?? task.startAt ?? null);
      const baseStartLocal = dbTimestampToLocalDate(baseStart);
      const baseEndLocal = baseEnd ? dbTimestampToLocalDate(baseEnd) : null;
      if (!baseStartLocal) continue;
      const durationMs = task.endAt
        ? getOccurrenceDurationMs(baseStart, baseEnd)
        : null;
      const segments = segmentsByTask.get(task.taskId) ?? [];
      const segmentEnvelopeMs = getRecurrenceSegmentEnvelopeMs(segments);
      const generationRangeStart = new Date(
        rangeStart.getTime() - segmentEnvelopeMs,
      );
      const generationRangeEnd = new Date(
        rangeEnd.getTime() + segmentEnvelopeMs,
      );
      const baseApplied = applyRecurrenceScheduleSegment({
        canonicalStart: baseStartLocal,
        canonicalEnd: baseEndLocal,
        baseAllDay: task.allDay ?? false,
        segments,
      });

      if (
        baseStart &&
        overlapsRange(
          baseApplied.startAt,
          baseApplied.endAt,
          rangeStart,
          rangeEnd,
        )
      ) {
        const canonicalBaseKey = occurrenceKey(task.taskId, baseStart);
        const key = canonicalBaseKey;
        if (
          !occurrences.has(key) &&
          !hiddenOccurrences.has(canonicalBaseKey) &&
          !explicitCanonicalOccurrences.has(canonicalBaseKey)
        ) {
          occurrences.set(key, {
            id: `base-${task.taskId}-${toLocalTimestampKey(baseStart)}`,
            task_id: task.taskId,
            project_id: task.projectId,
            title: task.title,
            project_name: task.projectName,
            status: resolveFutureRecurringStatus({
              status: task.status,
              resetStatusTo: task.resetStatusTo,
              triggerStatus: task.triggerStatus,
              startAt: baseApplied.startAt,
              now,
            }),
            start_at: serializeOccurrenceTimestamp(
              baseApplied.startAt,
              baseApplied.allDay,
            ),
            end_at: serializeOccurrenceTimestamp(
              baseApplied.endAt,
              baseApplied.allDay,
            ),
            all_day: baseApplied.allDay,
            source_kind: "task_schedule",
            is_generated: false,
            original_start_at: serializeDbTimestamp(
              baseApplied.originalStartAt,
            ),
            tags: tagsByTask.get(task.taskId) || [],
            project_color: extractProjectColor(task.projectMetadata),
          });
        }
      }

      const parsed = parseRrule(task.rrule);
      const previewConfig: RecurrencePreviewConfig = {
        freq: parsed.freq,
        interval: parsed.interval,
        byDay: parsed.byDay,
        skipWeekend: task.skipWeekend ?? false,
        skipHoliday: task.skipHoliday ?? false,
        skipMode: task.skipMode,
        endCount: task.endCount ?? null,
        endDate: task.endDate ? serializeDbTimestamp(task.endDate) : null,
      };

      const count = estimateOccurrenceCount(
        baseStartLocal,
        generationRangeEnd,
        previewConfig,
      );
      const occurrenceRangeStart =
        durationMs !== null && durationMs > 0
          ? new Date(generationRangeStart.getTime() - durationMs)
          : generationRangeStart;
      const upcomingOccurrences = computeOccurrenceCandidatesInRange(
        baseStartLocal,
        previewConfig,
        occurrenceRangeStart,
        generationRangeEnd,
        count,
      );

      for (const {
        canonicalStart: nextStart,
        occurrenceStart: shiftedStart,
      } of upcomingOccurrences) {
        const canonicalEnd = applyOccurrenceDuration(nextStart, durationMs);
        const shiftedEnd = applyOccurrenceDuration(shiftedStart, durationMs);
        const applied = applyRecurrenceScheduleSegment({
          canonicalStart: nextStart,
          canonicalEnd,
          baseStartAt: shiftedStart,
          baseEndAt: shiftedEnd,
          baseAllDay: task.allDay ?? false,
          segments,
        });
        if (
          !overlapsRange(applied.startAt, applied.endAt, rangeStart, rangeEnd)
        ) {
          continue;
        }

        const canonicalStartDb =
          localDateToDbTimestampDate(nextStart) ?? nextStart;
        const actualStartDb =
          localDateToDbTimestampDate(applied.startAt) ?? applied.startAt;
        const actualEndDb = applied.endAt
          ? (localDateToDbTimestampDate(applied.endAt) ?? applied.endAt)
          : null;
        const canonicalKey = occurrenceKey(task.taskId, canonicalStartDb);
        const key = canonicalKey;
        if (
          occurrences.has(key) ||
          hiddenOccurrences.has(canonicalKey) ||
          explicitCanonicalOccurrences.has(canonicalKey)
        )
          continue;

        occurrences.set(key, {
          id: `generated-${task.taskId}-${toLocalTimestampKey(canonicalStartDb)}`,
          task_id: task.taskId,
          project_id: task.projectId,
          title: task.title,
          project_name: task.projectName,
          status: resolveFutureRecurringStatus({
            status: task.resetStatusTo || "open",
            resetStatusTo: task.resetStatusTo,
            triggerStatus: task.triggerStatus,
            startAt: applied.startAt,
            now,
          }),
          start_at: serializeOccurrenceTimestamp(actualStartDb, applied.allDay),
          end_at: serializeOccurrenceTimestamp(actualEndDb, applied.allDay),
          all_day: applied.allDay,
          source_kind: "rrule",
          is_generated: true,
          original_start_at: serializeDbTimestamp(canonicalStartDb),
          tags: tagsByTask.get(task.taskId) || [],
          project_color: extractProjectColor(task.projectMetadata),
        });
      }
    }

    const result = [...occurrences.values()].sort((a, b) => {
      const aTime = dbTimestampToLocalDate(a.start_at)?.getTime() ?? 0;
      const bTime = dbTimestampToLocalDate(b.start_at)?.getTime() ?? 0;
      return aTime - bTime;
    });

    return NextResponse.json(result);
  } catch (err) {
    console.error("Occurrences fetch error:", err);
    return NextResponse.json([], { status: 200 });
  }
}
