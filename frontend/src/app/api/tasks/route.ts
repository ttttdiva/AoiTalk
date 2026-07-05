import { NextRequest, NextResponse } from "next/server";
import { db } from "@/db";
import {
  tasks,
  taskAssignees,
  taskTags,
  tags,
  users,
  timeEntries,
  taskRecurrenceRules,
  taskOccurrences,
  projects,
} from "@/db/schema";
import {
  eq,
  and,
  isNull,
  isNotNull,
  inArray,
  min,
  sql,
  lte,
} from "drizzle-orm";
import { getSession } from "@/lib/auth";
import { isClosedTaskStatus, normalizeTaskStatus } from "@/lib/task-status";
import { enqueueAutoSyncGoogleCalendarForTask } from "@/lib/server/google-calendar-auto-sync";
import { computeOccurrencesInRange } from "@/lib/recurrence-preview";
import {
  applyOccurrenceDuration,
  getOccurrenceDurationMs,
} from "@/lib/recurrence-schedule";
import {
  correctLikelyTimerStartedAt,
  dbTimestampToLocalDate,
  localDateToDbTimestampDate,
  serializeDbTimestamp,
  toDbLocalTimestamp,
  type DbTimestampValue,
} from "@/lib/server/db-time";
import {
  canWriteProjectId,
  extractProjectColor,
  getReadableProjectIds,
  normalizeOptionalUuid,
  isDateOnlyTaskInput,
  parseTaskWallClockDate,
  normalizeTaskTitle,
  resolveProjectTagIds,
  stripGoogleCalendarMetadata,
  taskToSnake,
} from "@/lib/server/task-route-utils";
import { getTaskNotificationsDefaultEnabled } from "@/lib/user-settings";
import { estimateOccurrenceCount, parseRrule } from "@/lib/recurrence-rrule";
import {
  isRecurrenceSkipSourceKind,
  resolveOccurrenceOriginalStartAt,
} from "@/lib/recurrence-exceptions";
import {
  chooseEarliestOpenOccurrence,
  occurrenceOverlapsRange,
  shouldIncludeTaskScheduleOccurrence,
} from "@/lib/task-list-effective-occurrence";

const TASK_LIST_OCCURRENCE_LOOKAHEAD_DAYS = 366;

type TaskListRow = typeof tasks.$inferSelect;
type TaskRecurrenceRow = typeof taskRecurrenceRules.$inferSelect;

type EffectiveOccurrence = {
  id: string | null;
  startAt: DbTimestampValue;
  endAt: DbTimestampValue;
  allDay: boolean;
  status: string | null;
  sourceKind: string | null;
  originalStartAt: string | null;
};

function addDays(date: Date, days: number): Date {
  const next = new Date(date);
  next.setDate(next.getDate() + days);
  return next;
}

function toLocalTimestampKey(value: DbTimestampValue): string {
  return serializeDbTimestamp(value)?.replace(/[-:]/g, "") ?? "";
}

function serializeTaskListTimestamp(
  value: DbTimestampValue,
  allDay: boolean,
): string | null {
  const serialized = serializeDbTimestamp(value);
  return allDay && serialized ? serialized.slice(0, 10) : serialized;
}

function occurrenceKey(taskId: string, value: DbTimestampValue): string {
  return `${taskId}:${toLocalTimestampKey(value)}`;
}

async function resolveTaskListEffectiveOccurrences(
  taskRows: TaskListRow[],
  recurrenceRows: TaskRecurrenceRow[],
): Promise<Map<string, EffectiveOccurrence>> {
  if (taskRows.length === 0 || recurrenceRows.length === 0) {
    return new Map();
  }

  const recurrenceByTask = new Map(
    recurrenceRows.map((row) => [row.taskId, row]),
  );
  const recurrenceTaskIds = [...recurrenceByTask.keys()];
  const taskById = new Map(taskRows.map((task) => [task.id, task]));
  const rangeStart = new Date();
  rangeStart.setHours(0, 0, 0, 0);
  const rangeEnd = addDays(rangeStart, TASK_LIST_OCCURRENCE_LOOKAHEAD_DAYS);

  const storedRows =
    recurrenceTaskIds.length === 0
      ? []
      : await db
          .select()
          .from(taskOccurrences)
          .where(
            and(
              inArray(taskOccurrences.taskId, recurrenceTaskIds),
              lte(taskOccurrences.startAt, toDbLocalTimestamp(rangeEnd)),
            ),
          );

  const occurrenceMaps = new Map<string, Map<string, EffectiveOccurrence>>();
  const hiddenOccurrences = new Set<string>();

  const setOccurrence = (
    taskId: string,
    key: string,
    occurrence: EffectiveOccurrence,
  ) => {
    const map = occurrenceMaps.get(taskId) ?? new Map();
    if (!map.has(key)) {
      map.set(key, occurrence);
      occurrenceMaps.set(taskId, map);
    }
  };

  for (const row of storedRows) {
    const task = taskById.get(row.taskId);
    if (!task) continue;
    const rowStartAt = dbTimestampToLocalDate(row.startAt);
    const rowEndAt = dbTimestampToLocalDate(row.endAt);
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
    if (originalKey && isClosedTaskStatus(row.status)) {
      hiddenOccurrences.add(originalKey);
    }

    if (!occurrenceOverlapsRange(rowStartAt, rowEndAt, rangeStart, rangeEnd))
      continue;

    setOccurrence(row.taskId, occurrenceKey(row.taskId, row.startAt), {
      id: row.id,
      startAt: row.startAt,
      endAt: row.endAt,
      allDay: row.allDay ?? task.allDay ?? false,
      status: row.status ?? task.status ?? null,
      sourceKind: row.sourceKind ?? null,
      originalStartAt: originalStartAt
        ? (serializeDbTimestamp(originalStartAt) ?? originalStartAt)
        : null,
    });
  }

  for (const task of taskRows) {
    const rule = recurrenceByTask.get(task.id);
    if (!rule) continue;

    const baseStart = task.startAt ?? task.endAt;
    if (!baseStart) continue;
    const baseEnd =
      task.startAt && task.endAt ? task.endAt : (task.endAt ?? task.startAt);
    const baseStartLocal = dbTimestampToLocalDate(baseStart);
    const baseEndLocal = dbTimestampToLocalDate(baseEnd);
    if (!baseStartLocal) continue;

    if (
      shouldIncludeTaskScheduleOccurrence({
        start: baseStartLocal,
        end: baseEndLocal,
        status: task.status,
        rangeStart,
        rangeEnd,
        blocked: hiddenOccurrences.has(occurrenceKey(task.id, baseStart)),
      })
    ) {
      const key = occurrenceKey(task.id, baseStart);
      if (!hiddenOccurrences.has(key)) {
        setOccurrence(task.id, key, {
          id: null,
          startAt: baseStart,
          endAt: baseEnd,
          allDay: task.allDay ?? false,
          status: task.status ?? null,
          sourceKind: "task_schedule",
          originalStartAt: serializeDbTimestamp(baseStart),
        });
      }
    }

    const parsed = parseRrule(rule.rrule);
    const config = {
      freq: parsed.freq,
      interval: parsed.interval,
      byDay: parsed.byDay,
      skipWeekend: rule.skipWeekend,
      skipHoliday: rule.skipHoliday,
      endCount: rule.recurForever ? null : rule.endCount,
      endDate: rule.recurForever
        ? null
        : rule.endDate
          ? serializeDbTimestamp(rule.endDate)
          : null,
    };
    const durationMs = task.endAt
      ? getOccurrenceDurationMs(baseStart, baseEnd)
      : null;
    const occurrenceRangeStart =
      durationMs !== null && durationMs > 0
        ? new Date(rangeStart.getTime() - durationMs)
        : rangeStart;
    const count = estimateOccurrenceCount(baseStartLocal, rangeEnd, config);
    const upcomingStarts = computeOccurrencesInRange(
      baseStartLocal,
      config,
      occurrenceRangeStart,
      rangeEnd,
      count,
    );

    for (const nextStart of upcomingStarts) {
      const nextEnd = applyOccurrenceDuration(nextStart, durationMs);
      if (!occurrenceOverlapsRange(nextStart, nextEnd, rangeStart, rangeEnd))
        continue;

      const nextStartDb = localDateToDbTimestampDate(nextStart) ?? nextStart;
      const nextEndDb = nextEnd
        ? (localDateToDbTimestampDate(nextEnd) ?? nextEnd)
        : null;
      const key = occurrenceKey(task.id, nextStartDb);
      if (hiddenOccurrences.has(key)) continue;

      setOccurrence(task.id, key, {
        id: null,
        startAt: nextStartDb,
        endAt: nextEndDb,
        allDay: task.allDay ?? false,
        status: normalizeTaskStatus(rule.resetStatusTo || "open") || "open",
        sourceKind: "rrule",
        originalStartAt: serializeDbTimestamp(nextStartDb),
      });
    }
  }

  const effectiveByTask = new Map<string, EffectiveOccurrence>();
  for (const [taskId, occurrences] of occurrenceMaps) {
    const effective = chooseEarliestOpenOccurrence([...occurrences.values()], {
      getStart: (occurrence) => dbTimestampToLocalDate(occurrence.startAt),
      getStatus: (occurrence) => occurrence.status,
    });
    if (effective) effectiveByTask.set(taskId, effective);
  }

  return effectiveByTask;
}

export async function GET(request: NextRequest) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const { searchParams } = new URL(request.url);
  const projectId = searchParams.get("project_id");
  const spaceId = searchParams.get("space_id");

  const readableProjectIds = await getReadableProjectIds(user.id, {
    projectId,
    spaceId: projectId ? null : spaceId,
  });
  if (readableProjectIds.length === 0) {
    return NextResponse.json([]);
  }

  const taskRows = await db
    .select()
    .from(tasks)
    .where(and(inArray(tasks.projectId, readableProjectIds), isNull(tasks.deletedAt)))
    .orderBy(
      sql`${tasks.sortOrder} asc nulls last`,
      tasks.projectId,
      sql`${tasks.parentTaskId} asc nulls first`,
      tasks.createdAt,
      tasks.id,
    );

  if (taskRows.length === 0) {
    return NextResponse.json([]);
  }

  const taskIds = taskRows.map((t) => t.id);
  const projectIds = [...new Set(taskRows.map((t) => t.projectId))];

  const projectRows = await db
    .select({
      id: projects.id,
      name: projects.name,
      projectMetadata: projects.projectMetadata,
    })
    .from(projects)
    .where(inArray(projects.id, projectIds));

  // assignees
  const assigneeRows = await db
    .select({
      id: taskAssignees.id,
      taskId: taskAssignees.taskId,
      userId: taskAssignees.userId,
      isPrimary: taskAssignees.isPrimary,
      assignedAt: taskAssignees.assignedAt,
      displayName: users.displayName,
      username: users.username,
    })
    .from(taskAssignees)
    .leftJoin(users, eq(taskAssignees.userId, users.id))
    .where(inArray(taskAssignees.taskId, taskIds));

  // tags
  const tagRows = await db
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

  // active time entries
  const activeEntries = await db
    .select()
    .from(timeEntries)
    .where(
      and(
        inArray(timeEntries.taskId, taskIds),
        eq(timeEntries.userId, user.id),
        isNull(timeEntries.endedAt),
      ),
    );

  // 完了済み time entries の合計（タスクごと）
  const totalTimeRows = await db
    .select({
      taskId: timeEntries.taskId,
      totalSeconds: sql<number>`coalesce(sum(greatest(extract(epoch from (${timeEntries.endedAt} - ${timeEntries.startedAt})), 0))::int, 0)`,
    })
    .from(timeEntries)
    .where(
      and(inArray(timeEntries.taskId, taskIds), isNotNull(timeEntries.endedAt)),
    )
    .groupBy(timeEntries.taskId);

  // 繰り返し設定を持つタスクID
  const recurrenceRows = await db
    .select()
    .from(taskRecurrenceRules)
    .where(inArray(taskRecurrenceRules.taskId, taskIds));
  const recurrenceTaskIds = new Set(recurrenceRows.map((r) => r.taskId));
  const effectiveOccurrences = await resolveTaskListEffectiveOccurrences(
    taskRows,
    recurrenceRows,
  );

  const assigneesByTask = new Map<string, unknown[]>();
  for (const a of assigneeRows) {
    const list = assigneesByTask.get(a.taskId) || [];
    list.push({
      id: a.id,
      task_id: a.taskId,
      user_id: a.userId,
      is_primary: a.isPrimary,
      assigned_at: a.assignedAt,
      display_name: a.displayName,
      username: a.username,
    });
    assigneesByTask.set(a.taskId, list);
  }

  const tagsByTask = new Map<string, unknown[]>();
  for (const t of tagRows) {
    const list = tagsByTask.get(t.taskId) || [];
    list.push({
      id: t.id,
      space_id: t.spaceId,
      name: t.name,
      color: t.color,
      created_by: t.createdBy,
      created_at: t.createdAt,
    });
    tagsByTask.set(t.taskId, list);
  }

  const activeByTask = new Map<string, unknown>();
  for (const e of activeEntries) {
    const startedAt = correctLikelyTimerStartedAt(
      e.startedAt,
      e.createdAt,
      e.source,
    );
    activeByTask.set(e.taskId, {
      id: e.id,
      task_id: e.taskId,
      user_id: e.userId,
      started_at: serializeDbTimestamp(startedAt),
      ended_at: serializeDbTimestamp(e.endedAt),
      duration_seconds:
        e.startedAt && e.endedAt
          ? Math.max(
              0,
              Math.floor(
                ((dbTimestampToLocalDate(e.endedAt)?.getTime() ?? 0) -
                  (dbTimestampToLocalDate(startedAt)?.getTime() ?? 0)) /
                  1000,
              ),
            )
          : null,
      source: e.source,
      note: e.note,
    });
  }

  const totalTimeByTask = new Map<string, number>();
  for (const r of totalTimeRows) {
    totalTimeByTask.set(r.taskId, r.totalSeconds);
  }

  const projectColorById = new Map<string, string | null>();
  const projectNameById = new Map<string, string | null>();
  for (const project of projectRows) {
    projectNameById.set(project.id, project.name);
    projectColorById.set(
      project.id,
      extractProjectColor(project.projectMetadata),
    );
  }

  const result = taskRows.map((t) => {
    const base = taskToSnake(t as unknown as Record<string, unknown>);
    base.assignees = assigneesByTask.get(t.id) || [];
    base.tags = tagsByTask.get(t.id) || [];
    base.active_time_entry = activeByTask.get(t.id) || null;
    base.total_time_seconds = totalTimeByTask.get(t.id) || 0;
    base.has_recurrence = recurrenceTaskIds.has(t.id);
    const effectiveOccurrence = effectiveOccurrences.get(t.id);
    if (effectiveOccurrence) {
      base.effective_start_at = serializeTaskListTimestamp(
        effectiveOccurrence.startAt,
        effectiveOccurrence.allDay,
      );
      base.effective_end_at = serializeTaskListTimestamp(
        effectiveOccurrence.endAt,
        effectiveOccurrence.allDay,
      );
      base.effective_all_day = effectiveOccurrence.allDay;
      base.effective_occurrence_id = effectiveOccurrence.id;
      base.effective_occurrence_start_at = serializeTaskListTimestamp(
        effectiveOccurrence.startAt,
        effectiveOccurrence.allDay,
      );
      base.effective_occurrence_end_at = serializeTaskListTimestamp(
        effectiveOccurrence.endAt,
        effectiveOccurrence.allDay,
      );
      base.effective_occurrence_original_start_at =
        effectiveOccurrence.originalStartAt;
      base.effective_occurrence_source_kind = effectiveOccurrence.sourceKind;
      base.effective_occurrence_status = effectiveOccurrence.status;
    }
    base.project_name = projectNameById.get(t.projectId) || null;
    base.project_color = projectColorById.get(t.projectId) || null;
    return base;
  });

  return NextResponse.json(result);
}

export async function POST(request: NextRequest) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const body = await request.json();
  const {
    project_id,
    knowledge_node_id,
    title,
    description,
    status,
    priority,
    start_at,
    end_at,
    all_day,
    reminder_offsets,
    notifications_enabled,
    tag_ids,
    assignee_ids,
    parent_task_id,
    estimated_hours,
    metadata,
  } = body;
  const normalizedStatus = normalizeTaskStatus(status || "todo");

  const normalizedTitle = normalizeTaskTitle(title);
  if (!project_id || !normalizedTitle) {
    return NextResponse.json(
      { detail: "project_idとtitleは必須です" },
      { status: 400 },
    );
  }

  if (!(await canWriteProjectId(user, project_id))) {
    return NextResponse.json({ detail: "権限がありません" }, { status: 403 });
  }

  const { tagIds: normalizedTagIds, invalidTagIds } =
    await resolveProjectTagIds(project_id, tag_ids);
  if (invalidTagIds.length > 0) {
    return NextResponse.json(
      { detail: "タスクのスペース外タグは指定できません" },
      { status: 400 },
    );
  }

  const normalizedAssigneeIds =
    Array.isArray(assignee_ids) && assignee_ids.length > 0
      ? [...new Set(assignee_ids)]
      : [user.id];
  const normalizedParentTaskId = normalizeOptionalUuid(parent_task_id);

  try {
    // トップレベルの新規タスクは ALL 表示の先頭に置く。
    // サブタスクは親配下だけの順序なので、従来通り同じ親の先頭に置く。
    const readableSortProjectIds = normalizedParentTaskId
      ? []
      : await getReadableProjectIds(user.id);
    const sortProjectIds =
      !normalizedParentTaskId && readableSortProjectIds.includes(project_id)
        ? readableSortProjectIds
        : [project_id];
    const sortScope = normalizedParentTaskId
      ? and(
          eq(tasks.projectId, project_id),
          eq(tasks.parentTaskId, normalizedParentTaskId),
          isNull(tasks.deletedAt),
        )
      : and(
          inArray(tasks.projectId, sortProjectIds),
          isNull(tasks.parentTaskId),
          isNull(tasks.deletedAt),
        );
    const [minRow] = await db
      .select({ minSort: min(tasks.sortOrder) })
      .from(tasks)
      .where(sortScope);
    const newSortOrder = (minRow?.minSort ?? 0) - 1;

    const inferredAllDay =
      all_day ?? (isDateOnlyTaskInput(start_at) || isDateOnlyTaskInput(end_at));
    const parsedStartAt = parseTaskWallClockDate(start_at);
    const parsedEndAt = parseTaskWallClockDate(end_at);
    const defaultNotificationsEnabled = getTaskNotificationsDefaultEnabled(
      (user.userSettings as Record<string, unknown> | null) ?? {},
    );
    const taskMetadata = stripGoogleCalendarMetadata(metadata);
    if (!("agent_triage_status" in taskMetadata)) {
      taskMetadata.agent_triage_status = "pending";
    }

    const [task] = await db
      .insert(tasks)
      .values({
        projectId: project_id,
        knowledgeNodeId: normalizeOptionalUuid(knowledge_node_id),
        title: normalizedTitle,
        description: description || null,
        status: normalizedStatus,
        priority: priority || "medium",
        startAt: parsedStartAt ? toDbLocalTimestamp(parsedStartAt) : null,
        endAt: parsedEndAt ? toDbLocalTimestamp(parsedEndAt) : null,
        allDay: inferredAllDay,
        reminderOffsets: Array.isArray(reminder_offsets)
          ? reminder_offsets
          : null,
        notificationsEnabled:
          notifications_enabled === undefined
            ? defaultNotificationsEnabled
            : !!notifications_enabled,
        createdBy: user.id,
        taskMetadata,
        estimatedHours:
          estimated_hours != null ? Number(estimated_hours) : null,
        sortOrder: newSortOrder,
        parentTaskId: normalizedParentTaskId,
        completedAt:
          normalizedStatus === "closed" ? toDbLocalTimestamp(new Date()) : null,
      })
      .returning();

    // タグ関連付け
    if (normalizedTagIds.length > 0) {
      await db.insert(taskTags).values(
        normalizedTagIds.map((tagId: string) => ({
          taskId: task.id,
          tagId,
        })),
      );
    }

    // アサイン
    if (normalizedAssigneeIds.length > 0) {
      await db.insert(taskAssignees).values(
        normalizedAssigneeIds.map((userId: string, i: number) => ({
          taskId: task.id,
          userId,
          isPrimary: i === 0,
        })),
      );
    }

    const result = taskToSnake(task as unknown as Record<string, unknown>);
    result.assignees = normalizedAssigneeIds.map(
      (userId: string, i: number) => ({
        id: crypto.randomUUID(),
        task_id: task.id,
        user_id: userId,
        is_primary: i === 0,
        display_name: userId === user.id ? user.displayName : null,
        username: userId === user.id ? user.username : null,
      }),
    );
    if (normalizedTagIds.length > 0) {
      const insertedTags = await db
        .select({
          id: tags.id,
          spaceId: tags.spaceId,
          name: tags.name,
          color: tags.color,
          createdBy: tags.createdBy,
          createdAt: tags.createdAt,
        })
        .from(taskTags)
        .innerJoin(tags, eq(taskTags.tagId, tags.id))
        .where(eq(taskTags.taskId, task.id));
      result.tags = insertedTags.map((tag) => ({
        id: tag.id,
        space_id: tag.spaceId,
        name: tag.name,
        color: tag.color,
        created_by: tag.createdBy,
        created_at: tag.createdAt,
      }));
    } else {
      result.tags = [];
    }
    result.active_time_entry = null;
    enqueueAutoSyncGoogleCalendarForTask(task.id, user);
    return NextResponse.json(result);
  } catch (err) {
    console.error("Task creation error:", err);
    return NextResponse.json({ detail: String(err) }, { status: 500 });
  }
}
