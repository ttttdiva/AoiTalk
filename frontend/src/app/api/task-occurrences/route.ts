import { NextRequest, NextResponse } from "next/server";
import { db } from "@/db";
import {
  taskOccurrences,
  tasks,
  projects,
  taskRecurrenceRules,
  taskTags,
  tags,
} from "@/db/schema";
import { eq, and, inArray, gte, lte, isNotNull } from "drizzle-orm";
import { getSession } from "@/lib/auth";
import { computeOccurrencesInRange } from "@/lib/recurrence-preview";
import type { RecurrencePreviewConfig } from "@/lib/recurrence-preview";
import {
  isRecurrenceOverrideSourceKind,
  isRecurrenceSkipSourceKind,
  resolveOccurrenceOriginalStartAt,
} from "@/lib/recurrence-exceptions";
import { normalizeTaskStatus } from "@/lib/task-status";
import { estimateOccurrenceCount, parseRrule } from "@/lib/recurrence-rrule";
import { getReadableProjectIds } from "@/lib/server/task-route-utils";
import {
  dbTimestampToLocalDate,
  localDateToDbTimestampDate,
  parseDisplayDateAsDbTimestamp,
  serializeDbTimestamp,
  toDbLocalTimestamp,
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

function serializeOriginalStartAt(value: string | null): string | null {
  return serializeDbTimestamp(value) ?? value;
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
  const projectId = searchParams.get("project_id");
  const spaceId = searchParams.get("space_id");
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

  const scopedProjectIds = await getReadableProjectIds(user.id, {
    projectId,
    spaceId: projectId ? null : spaceId,
  });
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
      .leftJoin(taskRecurrenceRules, eq(taskRecurrenceRules.taskId, tasks.id))
      .innerJoin(projects, eq(tasks.projectId, projects.id))
      .where(
        and(
          inArray(tasks.projectId, scopedProjectIds),
          lte(taskOccurrences.startAt, toDbLocalTimestamp(rangeEndDb)),
          gte(taskOccurrences.endAt, toDbLocalTimestamp(rangeStartDb)),
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
      })
      .from(tasks)
      .innerJoin(taskRecurrenceRules, eq(taskRecurrenceRules.taskId, tasks.id))
      .innerJoin(projects, eq(tasks.projectId, projects.id))
      .where(
        and(inArray(tasks.projectId, scopedProjectIds), isNotNull(tasks.endAt)),
      );

    const taskIds = [
      ...new Set([
        ...storedRows.map((row) => row.taskId),
        ...recurringTasks.map((row) => row.taskId),
      ]),
    ];

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

    for (const row of storedRows) {
      const rowStartAt = dbTimestampToLocalDate(row.startAt);
      if (!rowStartAt) continue;

      const originalStartAt = resolveOccurrenceOriginalStartAt(
        row.sourceKind,
        rowStartAt,
      );
      const originalKey = originalStartAt
        ? occurrenceKey(row.taskId, originalStartAt)
        : null;

      if (originalKey && isRecurrenceSkipSourceKind(row.sourceKind)) {
        hiddenOccurrences.add(originalKey);
        continue;
      }

      const key = occurrenceKey(row.taskId, row.startAt);
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
          startAt: rowStartAt,
          now,
        }),
        start_at: serializeOccurrenceTimestamp(
          row.startAt,
          row.allDay ?? false,
        ),
        end_at: serializeOccurrenceTimestamp(row.endAt, row.allDay ?? false),
        all_day: row.allDay ?? false,
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
      const durationMs =
        baseStartLocal && baseEndLocal
          ? baseEndLocal.getTime() - baseStartLocal.getTime()
          : 0;

      if (
        baseStart &&
        overlapsRange(baseStartLocal, baseEndLocal, rangeStart, rangeEnd)
      ) {
        const key = occurrenceKey(task.taskId, baseStart);
        if (!occurrences.has(key) && !hiddenOccurrences.has(key)) {
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
              startAt: baseStartLocal,
              now,
            }),
            start_at: serializeOccurrenceTimestamp(
              baseStart,
              task.allDay ?? false,
            ),
            end_at: serializeOccurrenceTimestamp(
              baseEnd,
              task.allDay ?? false,
            ),
            all_day: task.allDay ?? false,
            source_kind: "task_schedule",
            is_generated: false,
            original_start_at: serializeDbTimestamp(baseStart),
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
        endCount: task.endCount ?? null,
        endDate: task.endDate ? serializeDbTimestamp(task.endDate) : null,
      };

      const count = estimateOccurrenceCount(
        baseStartLocal,
        rangeEnd,
        previewConfig,
      );
      const occurrenceRangeStart =
        durationMs > 0
          ? new Date(rangeStart.getTime() - durationMs)
          : rangeStart;
      const upcomingStarts = computeOccurrencesInRange(
        baseStartLocal,
        previewConfig,
        occurrenceRangeStart,
        rangeEnd,
        count,
      );

      for (const nextStart of upcomingStarts) {
        const nextEnd =
          durationMs > 0 ? new Date(nextStart.getTime() + durationMs) : null;
        if (!overlapsRange(nextStart, nextEnd, rangeStart, rangeEnd)) {
          continue;
        }

        const nextStartDb = localDateToDbTimestampDate(nextStart) ?? nextStart;
        const nextEndDb = nextEnd
          ? (localDateToDbTimestampDate(nextEnd) ?? nextEnd)
          : null;
        const key = occurrenceKey(task.taskId, nextStartDb);
        if (occurrences.has(key) || hiddenOccurrences.has(key)) continue;

        occurrences.set(key, {
          id: `generated-${task.taskId}-${toLocalTimestampKey(nextStartDb)}`,
          task_id: task.taskId,
          project_id: task.projectId,
          title: task.title,
          project_name: task.projectName,
          status: resolveFutureRecurringStatus({
            status: task.resetStatusTo || "open",
            resetStatusTo: task.resetStatusTo,
            triggerStatus: task.triggerStatus,
            startAt: nextStart,
            now,
          }),
          start_at: serializeOccurrenceTimestamp(
            nextStartDb,
            task.allDay ?? false,
          ),
          end_at: serializeOccurrenceTimestamp(
            nextEndDb,
            task.allDay ?? false,
          ),
          all_day: task.allDay ?? false,
          source_kind: "rrule",
          is_generated: true,
          original_start_at: serializeDbTimestamp(nextStartDb),
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
