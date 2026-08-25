import { and, desc, eq, gte, isNull, lte } from "drizzle-orm";
import { getDb, schema } from "../db/client";
import { rescheduleLocalTaskNotificationsFromCache } from "../lib/local-notifications";
import { taskApi } from "../lib/task-api";
import { compareTaskTimestamps, isTaskTombstoned } from "./tasks";
import {
  compareTombstoneTimestamps,
  loadTombstoneLedger,
  persistTombstoneLedger,
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
    all_day: Boolean(row.allDay),
    reminder_offsets: (row.reminderOffsets as number[] | null) ?? [],
    source_kind: row.sourceKind ?? null,
    is_generated: Boolean(row.isGenerated),
    created_at: row.createdAt ?? null,
    updated_at: row.updatedAt ?? null,
    deleted_at: row.deletedAt ?? null,
  };
}

export async function applyRemoteOccurrences(
  list: TaskOccurrence[],
): Promise<void> {
  if (!list.length) return;
  const db = getDb();
  const now = new Date().toISOString();
  const ledger = await loadTombstoneLedger("task-occurrences:tombstones");
  let ledgerChanged = false;
  for (const occurrence of list) {
    // A task tree tombstone is authoritative for its materialized rows. Do
    // not let a stale occurrence payload recreate a child while the task is
    // deleted locally (including when only the missing-row ledger exists).
    if (await isTaskTombstoned(occurrence.task_id)) continue;
    const existing = await db
      .select({ deletedAt: schema.taskOccurrences.deletedAt, updatedAt: schema.taskOccurrences.updatedAt })
      .from(schema.taskOccurrences)
      .where(eq(schema.taskOccurrences.id, occurrence.id));
    const current = existing[0];
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
    await db
      .insert(schema.taskOccurrences)
      .values({
        id: occurrence.id,
        taskId: occurrence.task_id,
        startAt: occurrence.start_at,
        endAt: occurrence.end_at ?? null,
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
          status: occurrence.status,
          allDay: Boolean(occurrence.all_day),
          reminderOffsets: occurrence.reminder_offsets ?? [],
          sourceKind: occurrence.source_kind ?? null,
          isGenerated: Boolean(occurrence.is_generated),
          updatedAt: occurrence.updated_at ?? now,
          deletedAt: null,
        },
      });
  }
  if (ledgerChanged) await persistTombstoneLedger(ledger);
  void rescheduleLocalTaskNotificationsFromCache();
}

export async function applyOccurrenceTombstones(
  tombstones: Array<{ id: string; deleted_at?: string | null }>,
): Promise<void> {
  if (!tombstones.length) return;
  const db = getDb();
  const ledger = await loadTombstoneLedger("task-occurrences:tombstones");
  let ledgerChanged = false;
  for (const item of tombstones) {
    const deletedAt = item.deleted_at ?? new Date().toISOString();
    const previous = ledger.entries.get(item.id);
    if (previous && compareTombstoneTimestamps(deletedAt, previous) < 0) {
      continue;
    }
    ledger.entries.set(item.id, deletedAt);
    ledgerChanged = true;
    await db
      .update(schema.taskOccurrences)
      .set({ deletedAt, updatedAt: deletedAt })
      .where(eq(schema.taskOccurrences.id, item.id));
  }
  if (ledgerChanged) await persistTombstoneLedger(ledger);
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
