import { NextRequest, NextResponse } from "next/server";
import { and, eq, sql } from "drizzle-orm";
import { db } from "@/db";
import {
  notificationDeliveries,
  taskAssignees,
  taskComments,
  taskOccurrences,
  taskRecurrenceRules,
  taskTags,
  tasks,
  timeEntries,
} from "@/db/schema";
import { getSession } from "@/lib/auth";
import { normalizeTaskStatus } from "@/lib/task-status";
import {
  buildRecurrenceOverrideSourceKind,
  buildRecurrenceSkipSourceKind,
  isRecurrenceOverrideSourceKind,
  isRecurrenceSkipSourceKind,
  parseRecurrenceOriginalStartAt,
} from "@/lib/recurrence-exceptions";
import {
  dbTimestampToLocalDate,
  parseDisplayDateAsDbTimestamp,
  serializeDbTimestamp,
  toDbLocalTimestamp,
} from "@/lib/server/db-time";

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
    .where(eq(tasks.id, taskId))
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

  const [existing] = await db
    .select()
    .from(taskOccurrences)
    .where(
      and(
        eq(taskOccurrences.taskId, taskId),
        eq(taskOccurrences.startAt, toDbLocalTimestamp(occurrenceStartAt)),
      ),
    )
    .limit(1);

  if (existing && isRecurrenceSkipSourceKind(existing.sourceKind)) {
    return existing;
  }

  const payload = {
    taskId,
    startAt: toDbLocalTimestamp(occurrenceStartAt),
    endAt: toDbLocalTimestamp(occurrenceEndAt),
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

  if (!existing || !isRecurrenceOverrideSourceKind(existing.sourceKind)) {
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

    await ensureSingleSkipRow({
      taskId: id,
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
    const taskStartAt = dbTimestampToLocalDate(task.startAt);
    if (taskStartAt && taskStartAt.getTime() >= originalStartAt.getTime()) {
      await deleteDueSoonNotifications(id);
      await db
        .delete(notificationDeliveries)
        .where(eq(notificationDeliveries.taskId, id));
      await db.delete(timeEntries).where(eq(timeEntries.taskId, id));
      await db.delete(taskOccurrences).where(eq(taskOccurrences.taskId, id));
      await db.execute(sql`DELETE FROM task_activities WHERE task_id = ${id}`);
      await db.execute(
        sql`DELETE FROM task_dependencies WHERE task_id = ${id} OR depends_on_task_id = ${id}`,
      );
      await db
        .delete(taskRecurrenceRules)
        .where(eq(taskRecurrenceRules.taskId, id));
      await db.delete(taskComments).where(eq(taskComments.taskId, id));
      await db.delete(taskTags).where(eq(taskTags.taskId, id));
      await db.delete(taskAssignees).where(eq(taskAssignees.taskId, id));
      await db.delete(tasks).where(eq(tasks.id, id));
      return NextResponse.json({ success: true, deleted_task: true });
    }

    const taskOccurrenceRows = await db
      .select()
      .from(taskOccurrences)
      .where(eq(taskOccurrences.taskId, id));

    const staleOccurrenceIds = taskOccurrenceRows
      .filter((row) => {
        if (isRecurrenceSkipSourceKind(row.sourceKind)) {
          const rowStartAt = dbTimestampToLocalDate(row.startAt);
          return (
            !!rowStartAt && rowStartAt.getTime() >= originalStartAt.getTime()
          );
        }
        const original = parseRecurrenceOriginalStartAt(row.sourceKind);
        if (!original) return false;
        return (
          parseDate(original, "original_start_at").getTime() >=
          originalStartAt.getTime()
        );
      })
      .map((row) => row.id);

    for (const occurrenceId of staleOccurrenceIds) {
      await db
        .delete(taskOccurrences)
        .where(eq(taskOccurrences.id, occurrenceId));
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
