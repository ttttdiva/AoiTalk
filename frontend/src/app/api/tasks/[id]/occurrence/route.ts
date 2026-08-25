import { NextRequest, NextResponse } from "next/server";
import { and, eq, inArray, isNull } from "drizzle-orm";
import { db } from "@/db";
import {
  notificationDeliveries,
  taskOccurrences,
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
  resolveOccurrenceCutoffSource,
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
    existing =
      (
        await db
          .select()
          .from(taskOccurrences)
          .where(
            and(
              eq(taskOccurrences.taskId, taskId),
              eq(
                taskOccurrences.startAt,
                toDbLocalTimestamp(nextStartAt),
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
    !canReuseOccurrenceRowForOverride(
      existing.sourceKind,
      reuseOccurrenceId,
    )
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
    if (!(await canWriteProjectId(user, task.projectId))) {
      return NextResponse.json({ detail: "Permission denied" }, { status: 403 });
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
    if (!(await canWriteProjectId(user, task.projectId))) {
      return NextResponse.json({ detail: "Permission denied" }, { status: 403 });
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
    // 開始日時が未設定の繰り返しタスクは、GET 側 (task-occurrences/route.ts) が
    // end_at を base 回の開始として扱う。ここも同じ基準に揃えないと、
    // start_at が NULL の繰り返しタスクはこの分岐に入れず、
    // どの回で「今回以降を削除」を押してもタスク本体が消えないままになる。
    const taskStartAt = dbTimestampToLocalDate(task.startAt ?? task.endAt);
    if (taskStartAt && taskStartAt.getTime() >= originalStartAt.getTime()) {
      await deleteDueSoonNotifications(id);
      const upstream = await fetchPythonApi(`/api/tasks/${encodeURIComponent(id)}`, {
        method: "DELETE",
        user,
      });
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
        .where(inArray(notificationDeliveries.occurrenceId, staleOccurrenceIds));
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
