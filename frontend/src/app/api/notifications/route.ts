import { NextRequest, NextResponse } from "next/server";
import { and, desc, eq, inArray, isNull, ne, or, sql } from "drizzle-orm";
import { db } from "@/db";
import {
  notificationDeliveries,
  taskAssignees,
  taskOccurrences,
  taskRecurrenceRules,
  tasks,
  users,
} from "@/db/schema";
import { getSession } from "@/lib/auth";
import {
  computeUpcomingOccurrences,
  isJapaneseHoliday,
} from "@/lib/recurrence-preview";
import type { RecurrencePreviewConfig } from "@/lib/recurrence-preview";
import {
  isRecurrenceOverrideSourceKind,
  isRecurrenceSkipSourceKind,
  resolveOccurrenceOriginalStartAt,
} from "@/lib/recurrence-exceptions";
import { estimateOccurrenceCount, parseRrule } from "@/lib/recurrence-rrule";
import {
  dbTimestampToLocalDate,
  parseInputDate,
  serializeDbTimestamp,
} from "@/lib/server/db-time";
import {
  isLegacyPythonInAppReminderDedupeKey,
  isTaskNotificationSuppressed,
} from "@/lib/task-notification-policy";

const WEB_PRESENCE_ACTIVE_MS = 75_000;

function getNextWeekWindow(now: Date) {
  const nextWeekStart = new Date(now);
  nextWeekStart.setHours(0, 0, 0, 0);
  const daysUntilNextWeek = (7 - nextWeekStart.getDay()) % 7 || 7;
  nextWeekStart.setDate(nextWeekStart.getDate() + daysUntilNextWeek);

  const nextWeekEnd = new Date(nextWeekStart);
  nextWeekEnd.setDate(nextWeekEnd.getDate() + 7);

  return { nextWeekStart, nextWeekEnd };
}

function formatHolidayMessage(taskTitle: string, visibleStartAt: Date): string {
  return `${taskTitle} is scheduled on holiday ${visibleStartAt.toLocaleDateString(
    "ja-JP",
  )}.`;
}

function isMidnight(value: Date | string | null | undefined): boolean {
  const local = dbTimestampToLocalDate(value);
  return (
    !!local &&
    local.getHours() === 0 &&
    local.getMinutes() === 0 &&
    local.getSeconds() === 0 &&
    local.getMilliseconds() === 0
  );
}

function isDateOnlySchedule(
  allDay: boolean | null | undefined,
  startAt: Date | string | null | undefined,
  endAt: Date | string | null | undefined,
): boolean {
  if (allDay) return true;
  if (startAt && endAt) return isMidnight(startAt) && isMidnight(endAt);
  return isMidnight(startAt ?? endAt);
}

export function isStaleNonRecurringTaskSchedule(input: {
  occurrenceId: string | null | undefined;
  occurrenceSourceKind: string | null | undefined;
  recurrenceRuleTaskId: string | null | undefined;
}): boolean {
  // ``task_schedule`` rows are legacy mirrors for non-recurring tasks. The
  // task row is their canonical anchor; retaining the mirror would expose an
  // old date after a task edit and can replay a stale reminder.
  return (
    !!input.occurrenceId &&
    input.occurrenceSourceKind === "task_schedule" &&
    !input.recurrenceRuleTaskId
  );
}

async function touchWebNotificationPresence(
  userId: string,
  now: Date,
) {
  const activeUntil = new Date(now.getTime() + WEB_PRESENCE_ACTIVE_MS);
  const presence = {
    active_until: activeUntil.toISOString(),
    last_seen_at: now.toISOString(),
    surface: "web",
  };
  try {
    await db
      .update(users)
      .set({
        // Patch only the presence key in PostgreSQL.  Building a replacement
        // object from getSession() would overwrite a concurrent settings PATCH
        // with the stale snapshot that authenticated this request.
        userSettings: sql`
          jsonb_set(
            case
              when jsonb_typeof(${users.userSettings}::jsonb) = 'object'
                then ${users.userSettings}::jsonb
              else '{}'::jsonb
            end,
            '{task_notification_web_presence}',
            ${JSON.stringify(presence)}::jsonb,
            true
          )::json
        `,
        updatedAt: now,
      })
      .where(eq(users.id, userId));
  } catch (error) {
    // Presence is advisory.  A transient write failure must not prevent the
    // already-authenticated user from reading notifications.
    console.error("Failed to update Web notification presence", error);
  }
}

async function syncHolidayConflictNotifications(userId: string) {
  const now = new Date();
  const { nextWeekStart, nextWeekEnd } = getNextWeekWindow(now);

  const assignedTaskRows = await db
    .select({ taskId: taskAssignees.taskId })
    .from(taskAssignees)
    .where(eq(taskAssignees.userId, userId));
  const assignedTaskIds = assignedTaskRows.map((row) => row.taskId);

  const ownershipFilters = [eq(tasks.createdBy, userId)];
  if (assignedTaskIds.length > 0) {
    ownershipFilters.push(inArray(tasks.id, assignedTaskIds));
  }

  const recurringTasks = await db
    .select({
      id: tasks.id,
      projectId: tasks.projectId,
      title: tasks.title,
      startAt: tasks.startAt,
      endAt: tasks.endAt,
      allDay: tasks.allDay,
      notificationsEnabled: tasks.notificationsEnabled,
      rrule: taskRecurrenceRules.rrule,
      endDate: taskRecurrenceRules.endDate,
      endCount: taskRecurrenceRules.endCount,
      skipWeekend: taskRecurrenceRules.skipWeekend,
      skipHoliday: taskRecurrenceRules.skipHoliday,
      skipMode: taskRecurrenceRules.skipMode,
    })
    .from(tasks)
    .innerJoin(taskRecurrenceRules, eq(taskRecurrenceRules.taskId, tasks.id))
    .where(or(...ownershipFilters));

  const recurringTaskIds = recurringTasks.map((task) => task.id);
  const exceptionRows =
    recurringTaskIds.length > 0
      ? await db
          .select({
            id: taskOccurrences.id,
            taskId: taskOccurrences.taskId,
            startAt: taskOccurrences.startAt,
            sourceKind: taskOccurrences.sourceKind,
            updatedAt: taskOccurrences.updatedAt,
          })
          .from(taskOccurrences)
          .where(inArray(taskOccurrences.taskId, recurringTaskIds))
      : [];

  const skipByTask = new Map<string, Map<string, string>>();
  const overrideByTask = new Map<
    string,
    Map<string, { id: string; startAt: Date; updatedAt: Date | null }>
  >();

  for (const row of exceptionRows) {
    const rowStartAt = dbTimestampToLocalDate(row.startAt);
    if (!rowStartAt) continue;
    const originalStartAt = resolveOccurrenceOriginalStartAt(
      row.sourceKind,
      row.startAt,
    );
    if (!originalStartAt) continue;
    if (isRecurrenceSkipSourceKind(row.sourceKind)) {
      const map = skipByTask.get(row.taskId) ?? new Map<string, string>();
      map.set(originalStartAt, serializeDbTimestamp(row.startAt) ?? originalStartAt);
      skipByTask.set(row.taskId, map);
      continue;
    }
    if (!isRecurrenceOverrideSourceKind(row.sourceKind)) continue;
    const map =
      overrideByTask.get(row.taskId) ??
      new Map<string, { id: string; startAt: Date; updatedAt: Date | null }>();
    const existing = map.get(originalStartAt);
    if (
      !existing ||
      (dbTimestampToLocalDate(row.updatedAt)?.getTime() ?? 0) >=
        (existing.updatedAt?.getTime() ?? 0)
    ) {
      map.set(originalStartAt, {
        id: row.id,
        startAt: rowStartAt,
        updatedAt: dbTimestampToLocalDate(row.updatedAt),
      });
    }
    overrideByTask.set(row.taskId, map);
  }

  const dueSoonCandidates: Array<{
    dedupeKey: string;
    projectId: string;
    taskId: string;
    title: string;
    message: string;
    scheduledFor: Date;
  }> = [];

  for (const task of recurringTasks) {
    if (!task.notificationsEnabled) continue;
    if (isDateOnlySchedule(task.allDay, task.startAt, task.endAt)) continue;
    if (!task.startAt && !task.endAt) continue;

    const anchor = task.startAt ?? task.endAt;
    const anchorLocal = dbTimestampToLocalDate(anchor);
    if (!anchorLocal) continue;

    const parsed = parseRrule(task.rrule);
    if (parsed.freq !== "WEEKLY") continue;

    const previewConfig: RecurrencePreviewConfig = {
      freq: parsed.freq,
      interval: parsed.interval,
      byDay: parsed.byDay,
      skipWeekend: task.skipWeekend ?? false,
      skipHoliday: task.skipHoliday ?? false,
      skipMode: task.skipMode,
      endCount: task.endCount ?? null,
      endDate: serializeDbTimestamp(task.endDate),
    };

    const candidateOriginalStarts: Date[] = [];
    const taskStartAt = dbTimestampToLocalDate(task.startAt);
    if (
      taskStartAt &&
      taskStartAt >= nextWeekStart &&
      taskStartAt < nextWeekEnd
    ) {
      candidateOriginalStarts.push(taskStartAt);
    }

    const count = estimateOccurrenceCount(
      anchorLocal,
      nextWeekEnd,
      previewConfig,
    );
    const futureStarts = computeUpcomingOccurrences(
      anchorLocal,
      previewConfig,
      count,
    );
    for (const start of futureStarts) {
      if (start >= nextWeekStart && start < nextWeekEnd) {
        candidateOriginalStarts.push(start);
      }
    }

    const uniqueOriginalStarts = [
      ...new Set(
        candidateOriginalStarts
          .map((value) => serializeDbTimestamp(value))
          .filter((value): value is string => !!value),
      ),
    ];
    for (const originalStartAtKey of uniqueOriginalStarts) {
      const skipMap = skipByTask.get(task.id);
      const overrideMap = overrideByTask.get(task.id);
      if (
        skipMap?.has(originalStartAtKey) &&
        !overrideMap?.has(originalStartAtKey)
      ) {
        continue;
      }

      const visibleStartAt =
        overrideMap?.get(originalStartAtKey)?.startAt ??
        parseInputDate(originalStartAtKey);
      if (visibleStartAt < nextWeekStart || visibleStartAt >= nextWeekEnd)
        continue;
      if (!isJapaneseHoliday(visibleStartAt)) continue;

      dueSoonCandidates.push({
        dedupeKey: `due_soon:${task.id}:original:${originalStartAtKey}:user:${userId}`,
        projectId: task.projectId,
        taskId: task.id,
        title: `Next week holiday: ${task.title}`,
        message: formatHolidayMessage(task.title, visibleStartAt),
        scheduledFor: visibleStartAt,
      });
    }
  }

  const existingRows = await db
    .select({
      id: notificationDeliveries.id,
      dedupeKey: notificationDeliveries.dedupeKey,
    })
    .from(notificationDeliveries)
    .where(
      and(
        eq(notificationDeliveries.userId, userId),
        eq(notificationDeliveries.channel, "in_app"),
        eq(notificationDeliveries.notificationType, "due_soon"),
      ),
    );

  const activeKeys = new Set(
    dueSoonCandidates.map((candidate) => candidate.dedupeKey),
  );
  const existingKeys = new Set(existingRows.map((row) => row.dedupeKey));

  for (const row of existingRows) {
    if (!activeKeys.has(row.dedupeKey)) {
      await db
        .delete(notificationDeliveries)
        .where(eq(notificationDeliveries.id, row.id));
    }
  }

  for (const candidate of dueSoonCandidates) {
    if (existingKeys.has(candidate.dedupeKey)) continue;
    await db.insert(notificationDeliveries).values({
      projectId: candidate.projectId,
      taskId: candidate.taskId,
      occurrenceId: null,
      userId,
      channel: "in_app",
      notificationType: "due_soon",
      dedupeKey: candidate.dedupeKey,
      title: candidate.title,
      message: candidate.message,
      scheduledFor: candidate.scheduledFor,
      deliveredAt: now,
      status: "delivered",
      payload: { kind: "holiday_conflict" },
      createdAt: now,
      updatedAt: now,
    });
  }
}

export async function GET(request: NextRequest) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json(
      { detail: "隱崎ｨｼ縺悟ｿ・ｦ√〒縺・" },
      { status: 401 },
    );
  }

  const now = new Date();
  await touchWebNotificationPresence(user.id, now);
  // Task reminder rows are materialized and delivered by the authoritative
  // Python scheduler. GET is list-only for task reminders, so a hidden or
  // background tab can never create one or change its scheduled time. The
  // separate holiday-conflict advisory remains a calendar preview feature.
  await syncHolidayConflictNotifications(user.id);

  const { searchParams } = new URL(request.url);
  const unreadOnly = searchParams.get("unread_only") === "true";

  const conditions = [
    eq(notificationDeliveries.userId, user.id),
    ne(notificationDeliveries.status, "cancelled"),
  ];
  if (unreadOnly) {
    conditions.push(isNull(notificationDeliveries.readAt));
  }

  const rows = await db
    .select({
      delivery: notificationDeliveries,
      joinedTaskId: tasks.id,
      taskStatus: tasks.status,
      taskArchivedAt: tasks.archivedAt,
      taskDeletedAt: tasks.deletedAt,
      taskNotificationsEnabled: tasks.notificationsEnabled,
      taskStartAt: tasks.startAt,
      taskEndAt: tasks.endAt,
      taskAllDay: tasks.allDay,
      joinedOccurrenceId: taskOccurrences.id,
      occurrenceStatus: taskOccurrences.status,
      occurrenceDeletedAt: taskOccurrences.deletedAt,
      occurrenceSourceKind: taskOccurrences.sourceKind,
      recurrenceRuleTaskId: taskRecurrenceRules.taskId,
      occurrenceStartAt: taskOccurrences.startAt,
      occurrenceEndAt: taskOccurrences.endAt,
      occurrenceAllDay: taskOccurrences.allDay,
    })
    .from(notificationDeliveries)
    .leftJoin(tasks, eq(notificationDeliveries.taskId, tasks.id))
    .leftJoin(
      taskOccurrences,
      eq(notificationDeliveries.occurrenceId, taskOccurrences.id),
    )
    .leftJoin(taskRecurrenceRules, eq(taskRecurrenceRules.taskId, tasks.id))
    .where(and(...conditions))
    .orderBy(desc(notificationDeliveries.createdAt));

  const result = rows
    .filter((row) => {
      const n = row.delivery;
      if (n.notificationType === "overdue") return false;
      if (n.notificationType !== "reminder") return true;
      if (isLegacyPythonInAppReminderDedupeKey(n.dedupeKey)) return false;
      if (n.taskId && !row.joinedTaskId) return false;
      if (n.occurrenceId && !row.joinedOccurrenceId) return false;
      if (
        isStaleNonRecurringTaskSchedule({
          occurrenceId: n.occurrenceId,
          occurrenceSourceKind: row.occurrenceSourceKind,
          recurrenceRuleTaskId: row.recurrenceRuleTaskId,
        })
      ) {
        return false;
      }
      if (row.taskNotificationsEnabled === false) return false;
      if (
        n.occurrenceId
          ? isDateOnlySchedule(
              row.occurrenceAllDay || row.taskAllDay,
              row.occurrenceStartAt,
              row.occurrenceEndAt,
            )
          : isDateOnlySchedule(
              row.taskAllDay,
              row.taskStartAt,
              row.taskEndAt,
            )
      ) {
        return false;
      }
      return !isTaskNotificationSuppressed({
        taskStatus: row.taskStatus,
        occurrenceStatus: row.occurrenceStatus,
        taskArchivedAt: row.taskArchivedAt,
        taskDeletedAt: row.taskDeletedAt,
        occurrenceDeletedAt: row.occurrenceDeletedAt,
        sourceKind: row.occurrenceSourceKind,
      });
    })
    .map((row) => row.delivery)
    .map((n) => ({
      id: n.id,
      project_id: n.projectId,
      task_id: n.taskId,
      occurrence_id: n.occurrenceId,
      user_id: n.userId,
      channel: n.channel,
      notification_type: n.notificationType,
      type: n.notificationType,
      dedupe_key: n.dedupeKey,
      title: n.title,
      message: n.message,
      scheduled_for: n.scheduledFor,
      delivered_at: n.deliveredAt,
      read_at: n.readAt,
      is_read: !!n.readAt,
      status: n.status,
      payload: n.payload,
      created_at: n.createdAt,
      updated_at: n.updatedAt,
    }));

  return NextResponse.json(result);
}
