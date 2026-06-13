import { and, desc, eq, gte, isNull, lte } from "drizzle-orm";
import { getDb, schema } from "../db/client";
import { rescheduleLocalTaskNotificationsFromCache } from "../lib/local-notifications";
import { taskApi } from "../lib/task-api";
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
  for (const occurrence of list) {
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
  void rescheduleLocalTaskNotificationsFromCache();
}

export async function applyOccurrenceTombstones(
  tombstones: Array<{ id: string; deleted_at?: string | null }>,
): Promise<void> {
  if (!tombstones.length) return;
  const db = getDb();
  for (const item of tombstones) {
    const deletedAt = item.deleted_at ?? new Date().toISOString();
    await db
      .update(schema.taskOccurrences)
      .set({ deletedAt, updatedAt: deletedAt })
      .where(eq(schema.taskOccurrences.id, item.id));
  }
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
