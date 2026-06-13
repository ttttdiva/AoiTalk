import { NextRequest, NextResponse } from "next/server";
import { and, desc, eq, inArray, isNull, or } from "drizzle-orm";
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

const DEFAULT_REMINDER_OFFSETS = [5];
const WEB_PRESENCE_ACTIVE_MS = 75_000;
const CLOSED_STATUSES = new Set(["closed", "done", "cancelled", "canceled"]);

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return (
    typeof value === "object" &&
    value !== null &&
    !Array.isArray(value) &&
    Object.getPrototypeOf(value) === Object.prototype
  );
}

function normalizeOffsets(value: unknown, fallback: number[]): number[] {
  const parsed =
    typeof value === "string"
      ? (() => {
          try {
            return JSON.parse(value) as unknown;
          } catch {
            return null;
          }
        })()
      : value;

  if (!Array.isArray(parsed)) return fallback;
  const offsets = [
    ...new Set(
      parsed
        .map((offset) => Number(offset))
        .filter((offset) => Number.isFinite(offset) && offset >= 0)
        .map((offset) => Math.floor(offset)),
    ),
  ];
  return offsets.length > 0 ? offsets : fallback;
}

function getUserReminderOffsets(settings: unknown): number[] {
  const raw = isPlainObject(settings)
    ? settings.task_notification_minutes_before
    : undefined;
  const value = Number(raw);
  return Number.isFinite(value) && value >= 0
    ? [Math.floor(value)]
    : DEFAULT_REMINDER_OFFSETS;
}

function isClosedStatus(status: unknown): boolean {
  return CLOSED_STATUSES.has(String(status ?? "").toLowerCase());
}

function formatReminderMessage(taskTitle: string, anchor: Date, offset: number) {
  const scheduledAt = anchor.toLocaleString("ja-JP", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
  return offset > 0
    ? `${taskTitle} starts in ${offset} minutes (${scheduledAt})`
    : `${taskTitle} starts now`;
}

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

async function touchWebNotificationPresence(
  userId: string,
  currentSettings: unknown,
  now: Date,
) {
  const settings = isPlainObject(currentSettings) ? currentSettings : {};
  const activeUntil = new Date(now.getTime() + WEB_PRESENCE_ACTIVE_MS);
  await db
    .update(users)
    .set({
      userSettings: {
        ...settings,
        task_notification_web_presence: {
          active_until: activeUntil.toISOString(),
          last_seen_at: now.toISOString(),
          surface: "web",
        },
      },
      updatedAt: now,
    })
    .where(eq(users.id, userId));
}

async function syncTaskReminderNotifications(user: {
  id: string;
  userSettings?: unknown;
}) {
  const now = new Date();
  const userOffsets = getUserReminderOffsets(user.userSettings);
  const scanFrom = new Date(now.getTime() - 24 * 60 * 60_000);
  const scanTo = new Date(now.getTime() + 24 * 60 * 60_000);

  const assignedTaskRows = await db
    .select({ taskId: taskAssignees.taskId })
    .from(taskAssignees)
    .where(eq(taskAssignees.userId, user.id));
  const assignedTaskIds = assignedTaskRows.map((row) => row.taskId);

  const ownershipFilters = [eq(tasks.createdBy, user.id)];
  if (assignedTaskIds.length > 0) {
    ownershipFilters.push(inArray(tasks.id, assignedTaskIds));
  }

  const occurrenceRows = await db
    .select({
      occurrenceId: taskOccurrences.id,
      taskId: tasks.id,
      projectId: tasks.projectId,
      title: tasks.title,
      taskStatus: tasks.status,
      occurrenceStatus: taskOccurrences.status,
      startAt: taskOccurrences.startAt,
      endAt: taskOccurrences.endAt,
      allDay: taskOccurrences.allDay,
      sourceKind: taskOccurrences.sourceKind,
      occurrenceReminderOffsets: taskOccurrences.reminderOffsets,
      taskReminderOffsets: tasks.reminderOffsets,
      notificationsEnabled: tasks.notificationsEnabled,
    })
    .from(taskOccurrences)
    .innerJoin(tasks, eq(taskOccurrences.taskId, tasks.id))
    .where(or(...ownershipFilters));

  const candidates: Array<{
    keyPrefix: string;
    taskId: string;
    occurrenceId: string | null;
    projectId: string;
    title: string;
    startAt: Date | null;
    endAt: Date | null;
    allDay: boolean | null;
    status: string | null;
    offsets: number[];
    notificationsEnabled: boolean | null;
  }> = [];

  const occurrenceKeys = new Set<string>();
  for (const row of occurrenceRows) {
    if (isRecurrenceSkipSourceKind(row.sourceKind)) continue;
    occurrenceKeys.add(`${row.taskId}:${serializeDbTimestamp(row.startAt)}`);
    candidates.push({
      keyPrefix: `occurrence:${row.occurrenceId}`,
      taskId: row.taskId,
      occurrenceId: row.occurrenceId,
      projectId: row.projectId,
      title: row.title,
      startAt: dbTimestampToLocalDate(row.startAt),
      endAt: dbTimestampToLocalDate(row.endAt),
      allDay: row.allDay,
      status: row.occurrenceStatus || row.taskStatus,
      offsets: normalizeOffsets(
        row.occurrenceReminderOffsets ?? row.taskReminderOffsets,
        userOffsets,
      ),
      notificationsEnabled: row.notificationsEnabled,
    });
  }

  const taskRows = await db
    .select({
      taskId: tasks.id,
      projectId: tasks.projectId,
      title: tasks.title,
      status: tasks.status,
      startAt: tasks.startAt,
      endAt: tasks.endAt,
      allDay: tasks.allDay,
      reminderOffsets: tasks.reminderOffsets,
      notificationsEnabled: tasks.notificationsEnabled,
    })
    .from(tasks)
    .where(or(...ownershipFilters));

  for (const task of taskRows) {
    const taskStartKey = `${task.taskId}:${serializeDbTimestamp(task.startAt)}`;
    if (occurrenceKeys.has(taskStartKey)) continue;
    candidates.push({
      keyPrefix: `task:${task.taskId}`,
      taskId: task.taskId,
      occurrenceId: null,
      projectId: task.projectId,
      title: task.title,
      startAt: dbTimestampToLocalDate(task.startAt),
      endAt: dbTimestampToLocalDate(task.endAt),
      allDay: task.allDay,
      status: task.status,
      offsets: normalizeOffsets(task.reminderOffsets, userOffsets),
      notificationsEnabled: task.notificationsEnabled,
    });
  }

  const activeCandidates: Array<{
    dedupeKey: string;
    projectId: string;
    taskId: string;
    occurrenceId: string | null;
    title: string;
    message: string;
    scheduledFor: Date;
  }> = [];

  for (const candidate of candidates) {
    if (candidate.notificationsEnabled === false) continue;
    if (isClosedStatus(candidate.status)) continue;
    if (
      isDateOnlySchedule(candidate.allDay, candidate.startAt, candidate.endAt)
    ) {
      continue;
    }
    const anchor = candidate.startAt ?? candidate.endAt;
    if (!anchor || anchor < scanFrom || anchor > scanTo || anchor < now) {
      continue;
    }

    for (const offset of candidate.offsets) {
      const triggerAt = new Date(anchor.getTime() - offset * 60_000);
      if (triggerAt > now || triggerAt < scanFrom) continue;
      const anchorKey = serializeDbTimestamp(anchor) ?? anchor.toISOString();
      activeCandidates.push({
        dedupeKey: `reminder:${candidate.keyPrefix}:at:${anchorKey}:offset:${offset}:user:${user.id}`,
        projectId: candidate.projectId,
        taskId: candidate.taskId,
        occurrenceId: candidate.occurrenceId,
        title: `Upcoming: ${candidate.title}`,
        message: formatReminderMessage(candidate.title, anchor, offset),
        scheduledFor: triggerAt,
      });
    }
  }

  if (activeCandidates.length === 0) return;

  const existingRows = await db
    .select({ dedupeKey: notificationDeliveries.dedupeKey })
    .from(notificationDeliveries)
    .where(
      and(
        eq(notificationDeliveries.userId, user.id),
        eq(notificationDeliveries.channel, "in_app"),
        eq(notificationDeliveries.notificationType, "reminder"),
      ),
    );
  const existingKeys = new Set(existingRows.map((row) => row.dedupeKey));

  for (const candidate of activeCandidates) {
    if (existingKeys.has(candidate.dedupeKey)) continue;
    await db.insert(notificationDeliveries).values({
      projectId: candidate.projectId,
      taskId: candidate.taskId,
      occurrenceId: candidate.occurrenceId,
      userId: user.id,
      channel: "in_app",
      notificationType: "reminder",
      dedupeKey: candidate.dedupeKey,
      title: candidate.title,
      message: candidate.message,
      scheduledFor: candidate.scheduledFor,
      deliveredAt: now,
      status: "delivered",
      payload: { kind: "task_reminder" },
      createdAt: now,
      updatedAt: now,
    });
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
  await touchWebNotificationPresence(user.id, user.userSettings, now);
  await syncTaskReminderNotifications(user);
  await syncHolidayConflictNotifications(user.id);

  const { searchParams } = new URL(request.url);
  const unreadOnly = searchParams.get("unread_only") === "true";

  const conditions = [eq(notificationDeliveries.userId, user.id)];
  if (unreadOnly) {
    conditions.push(isNull(notificationDeliveries.readAt));
  }

  const rows = await db
    .select()
    .from(notificationDeliveries)
    .where(and(...conditions))
    .orderBy(desc(notificationDeliveries.createdAt));

  const result = rows
    .filter((n) => n.notificationType !== "overdue")
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
