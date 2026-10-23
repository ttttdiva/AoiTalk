import { and, desc, eq, gte, isNull, lte } from "drizzle-orm";
import { getDb, schema } from "../db/client";
import { runForegroundSqliteWrite } from "../db/sqlite-write-coordinator";
import {
  assertActiveWriter,
  type SqliteHandle,
  type SqliteWriter,
} from "../db/sqlite-writer";
import { rescheduleLocalTaskNotificationsFromCache } from "../lib/local-notifications";
import { taskApi } from "../lib/task-api";
import {
  compareTaskTimestamps,
  isTaskTombstonedInWriter,
  prepareTaskTombstoneLedgerKey,
} from "./tasks";
import {
  compareTombstoneTimestamps,
  loadTombstoneLedgerInWriter,
  persistTombstoneLedgerInWriter,
  prepareTombstoneLedgerKey,
} from "./tombstone-ledger";
import type { TaskOccurrence } from "../types/api";

type DbOccurrence = typeof schema.taskOccurrences.$inferSelect;

function extractProjectColor(projectMetadata: unknown): string | null {
  if (
    !projectMetadata ||
    typeof projectMetadata !== "object" ||
    Array.isArray(projectMetadata)
  ) {
    return null;
  }
  const color = (projectMetadata as Record<string, unknown>).color;
  return typeof color === "string" && color.trim() ? color : null;
}

function toApiShape(
  row: DbOccurrence,
  task?: {
    projectId: string;
    projectName?: string | null;
    projectColor?: string | null;
    title: string | null;
  },
): TaskOccurrence {
  return {
    id: row.id,
    task_id: row.taskId,
    project_id: task?.projectId ?? null,
    project_name: task?.projectName ?? null,
    project_color: task?.projectColor ?? null,
    title: task?.title ?? null,
    status: row.status,
    start_at: row.startAt,
    end_at: row.endAt ?? null,
    original_start_at: row.originalStartAt ?? null,
    all_day: Boolean(row.allDay),
    reminder_offsets: (row.reminderOffsets as number[] | null) ?? [],
    source_kind: row.sourceKind ?? null,
    is_generated: Boolean(row.isGenerated),
    created_at: row.createdAt ?? null,
    updated_at: row.updatedAt ?? null,
    deleted_at: row.deletedAt ?? null,
  };
}

const OCCURRENCE_LEDGER_PREFIX = "task-occurrences:tombstones";

/** Async preparation (token reads) shared by the occurrence InWriter functions. */
export type OccurrenceApplyPrep = {
  ledgerKey: string;
  taskLedgerKey: string;
};

export async function prepareOccurrenceApply(): Promise<OccurrenceApplyPrep> {
  return {
    ledgerKey: await prepareTombstoneLedgerKey(OCCURRENCE_LEDGER_PREFIX),
    taskLedgerKey: await prepareTaskTombstoneLedgerKey(),
  };
}

/** Returns true when local task notifications should be rescheduled after commit. */
export function applyRemoteOccurrencesInWriter(
  writer: SqliteWriter,
  tx: SqliteHandle,
  list: TaskOccurrence[],
  prep: OccurrenceApplyPrep,
): boolean {
  assertActiveWriter(writer);
  if (!list.length) return false;
  const now = new Date().toISOString();
  const ledger = loadTombstoneLedgerInWriter(writer, tx, prep.ledgerKey);
  let ledgerChanged = false;
  for (const occurrence of list) {
    // A task tree tombstone is authoritative for its materialized rows. Do
    // not let a stale occurrence payload recreate a child while the task is
    // deleted locally (including when only the missing-row ledger exists).
    if (isTaskTombstonedInWriter(writer, tx, occurrence.task_id, prep.taskLedgerKey)) continue;
    const existing = tx
      .select({
        deletedAt: schema.taskOccurrences.deletedAt,
        updatedAt: schema.taskOccurrences.updatedAt,
        originalStartAt: schema.taskOccurrences.originalStartAt,
      })
      .from(schema.taskOccurrences)
      .where(eq(schema.taskOccurrences.id, occurrence.id))
      .all();
    const current = existing[0];
    // Keep the cached canonical identity when talking to an older server that
    // omits the additive field.  An explicit null remains authoritative.
    const hasIncomingOriginalStartAt = Object.prototype.hasOwnProperty.call(
      occurrence,
      "original_start_at",
    );
    const originalStartAt = hasIncomingOriginalStartAt
      ? occurrence.original_start_at ?? null
      : current?.originalStartAt ?? null;
    const incomingUpdatedAt = occurrence.updated_at ?? now;
    const hasIncomingVersion =
      typeof occurrence.updated_at === "string" && occurrence.updated_at.trim().length > 0;
    const explicitActive =
      Object.prototype.hasOwnProperty.call(occurrence, "deleted_at") &&
      occurrence.deleted_at == null;
    const ledgerDeletedAt = ledger.entries.get(occurrence.id) ?? null;
    if (
      ledgerDeletedAt != null &&
      !(
        explicitActive &&
        hasIncomingVersion &&
        compareTombstoneTimestamps(incomingUpdatedAt, ledgerDeletedAt) > 0
      )
    ) {
      continue;
    }
    if (ledgerDeletedAt != null && ledger.entries.delete(occurrence.id)) {
      ledgerChanged = true;
    }
    if (
      current?.deletedAt != null &&
      !(
        explicitActive &&
        hasIncomingVersion &&
        compareTaskTimestamps(incomingUpdatedAt, current.deletedAt) > 0
      )
    ) {
      continue;
    }
    tx
      .insert(schema.taskOccurrences)
      .values({
        id: occurrence.id,
        taskId: occurrence.task_id,
        startAt: occurrence.start_at,
        endAt: occurrence.end_at ?? null,
        originalStartAt,
        status: occurrence.status,
        allDay: Boolean(occurrence.all_day),
        reminderOffsets: occurrence.reminder_offsets ?? [],
        sourceKind: occurrence.source_kind ?? null,
        isGenerated: Boolean(occurrence.is_generated),
        createdAt: occurrence.created_at ?? now,
        updatedAt: occurrence.updated_at ?? now,
        deletedAt: null,
      })
      .onConflictDoUpdate({
        target: schema.taskOccurrences.id,
        set: {
          taskId: occurrence.task_id,
          startAt: occurrence.start_at,
          endAt: occurrence.end_at ?? null,
          originalStartAt,
          status: occurrence.status,
          allDay: Boolean(occurrence.all_day),
          reminderOffsets: occurrence.reminder_offsets ?? [],
          sourceKind: occurrence.source_kind ?? null,
          isGenerated: Boolean(occurrence.is_generated),
          updatedAt: occurrence.updated_at ?? now,
          deletedAt: null,
        },
      })
      .run();
  }
  if (ledgerChanged) persistTombstoneLedgerInWriter(writer, tx, ledger);
  return true;
}

export async function applyRemoteOccurrences(
  list: TaskOccurrence[],
): Promise<void> {
  if (!list.length) return;
  const prep = await prepareOccurrenceApply();
  const reschedule = await runForegroundSqliteWrite((writer) =>
    getDb().transaction((tx) =>
      applyRemoteOccurrencesInWriter(writer, tx, list, prep),
    ),
  );
  if (reschedule) void rescheduleLocalTaskNotificationsFromCache();
}

/** Returns true when local task notifications should be rescheduled after commit. */
export function applyOccurrenceTombstonesInWriter(
  writer: SqliteWriter,
  tx: SqliteHandle,
  tombstones: Array<{ id: string; deleted_at?: string | null }>,
  ledgerKey: string,
): boolean {
  assertActiveWriter(writer);
  if (!tombstones.length) return false;
  const ledger = loadTombstoneLedgerInWriter(writer, tx, ledgerKey);
  let ledgerChanged = false;
  for (const item of tombstones) {
    const deletedAt = item.deleted_at ?? new Date().toISOString();
    const previous = ledger.entries.get(item.id);
    if (previous && compareTombstoneTimestamps(deletedAt, previous) < 0) {
      continue;
    }
    ledger.entries.set(item.id, deletedAt);
    ledgerChanged = true;
    tx
      .update(schema.taskOccurrences)
      .set({ deletedAt, updatedAt: deletedAt })
      .where(eq(schema.taskOccurrences.id, item.id))
      .run();
  }
  if (ledgerChanged) persistTombstoneLedgerInWriter(writer, tx, ledger);
  return true;
}

export async function applyOccurrenceTombstones(
  tombstones: Array<{ id: string; deleted_at?: string | null }>,
): Promise<void> {
  if (!tombstones.length) return;
  const ledgerKey = await prepareTombstoneLedgerKey(OCCURRENCE_LEDGER_PREFIX);
  await runForegroundSqliteWrite((writer) =>
    getDb().transaction((tx) =>
      applyOccurrenceTombstonesInWriter(writer, tx, tombstones, ledgerKey),
    ),
  );
  void rescheduleLocalTaskNotificationsFromCache();
}

export const occurrencesRepo = {
  async listLocal(
    projectId?: string | null,
    spaceId?: string | null,
    startFrom?: string | null,
    endTo?: string | null,
  ): Promise<TaskOccurrence[]> {
    const db = getDb();
    const rows = await db
      .select({
        occurrence: schema.taskOccurrences,
        taskProjectId: schema.tasks.projectId,
        taskProjectName: schema.projects.name,
        taskProjectMetadata: schema.projects.projectMetadata,
        taskTitle: schema.tasks.title,
      })
      .from(schema.taskOccurrences)
      .innerJoin(
        schema.tasks,
        eq(schema.taskOccurrences.taskId, schema.tasks.id),
      )
      .innerJoin(
        schema.projects,
        eq(schema.tasks.projectId, schema.projects.id),
      )
      .where(
        and(
          projectId ? eq(schema.tasks.projectId, projectId) : undefined,
          spaceId ? eq(schema.projects.spaceId, spaceId) : undefined,
          startFrom
            ? gte(schema.taskOccurrences.startAt, startFrom)
            : undefined,
          endTo ? lte(schema.taskOccurrences.startAt, endTo) : undefined,
          isNull(schema.taskOccurrences.deletedAt),
          isNull(schema.tasks.deletedAt),
        ),
      )
      .orderBy(desc(schema.taskOccurrences.startAt));
    return rows.map((row) =>
      toApiShape(row.occurrence, {
        projectId: row.taskProjectId,
        projectName: row.taskProjectName,
        projectColor: extractProjectColor(row.taskProjectMetadata),
        title: row.taskTitle,
      }),
    );
  },

  async refresh(
    projectId?: string | null,
    spaceId?: string | null,
    startFrom?: string | null,
    endTo?: string | null,
  ): Promise<TaskOccurrence[]> {
    const list = await taskApi.listOccurrences(
      projectId ? projectId : spaceId ? { space_id: spaceId } : undefined,
      startFrom ?? undefined,
      endTo ?? undefined,
    );
    await applyRemoteOccurrences(list);
    return list;
  },

  async list(
    projectId?: string | null,
    spaceId?: string | null,
    startFrom?: string | null,
    endTo?: string | null,
  ): Promise<TaskOccurrence[]> {
    const local = await this.listLocal(projectId, spaceId, startFrom, endTo);
    try {
      return await this.refresh(projectId, spaceId, startFrom, endTo);
    } catch {
      return local;
    }
  },
};
