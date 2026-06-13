/**
 * Task Repository.
 *
 * M1 policy:
 *   - Reads (`list`, `get`): local-first. Return SQLite cache immediately,
 *     kick off a remote refresh when online. On cold cache + online, wait
 *     for the remote call so the UI isn't empty.
 *   - Writes (`create`, `update`, `delete`): at M1 writes still go straight
 *     to the server when online; local cache is updated on success. If
 *     offline, the write is rejected with an `OfflineWriteError` (caller
 *     shows a toast). M2 replaces this with outbox-backed optimistic writes.
 *
 * Return shape matches `types/api.Task` so existing UI doesn't change.
 */

import { eq, isNull, and, desc } from "drizzle-orm";
import { getDb, schema } from "../db/client";
import { getToken } from "../lib/auth";
import { taskApi } from "../lib/task-api";
import { normalizeTaskStatus } from "../lib/task-status";
import { rescheduleLocalTaskNotificationsFromCache } from "../lib/local-notifications";
import { useNetworkStore } from "../stores/network";
import type { Task as ApiTask } from "../types/api";
import { enqueueOutbox, randomId } from "./outbox";

type DbTask = typeof schema.tasks.$inferSelect;

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

export class OfflineWriteError extends Error {
  constructor(msg = "サーバーに接続していないため実行できません") {
    super(msg);
    this.name = "OfflineWriteError";
  }
}

async function canUseServer(): Promise<boolean> {
  const network = useNetworkStore.getState();
  return network.online && network.serverReachable && Boolean(await getToken());
}

function toApiShape(
  row: DbTask,
  projectName?: string | null,
  projectColor?: string | null,
): ApiTask {
  return {
    id: row.id,
    project_id: row.projectId,
    project_name: projectName ?? null,
    project_color: projectColor ?? null,
    title: row.title,
    description: row.description ?? null,
    status: normalizeTaskStatus(row.status),
    priority: row.priority ?? "normal",
    start_at: row.startAt ?? null,
    end_at: row.endAt ?? null,
    all_day: Boolean(row.allDay),
    notifications_enabled: row.notificationsEnabled ?? true,
    source: row.source ?? "local",
    created_by: row.createdBy ?? null,
    completed_at: row.completedAt ?? null,
    archived_at: row.archivedAt ?? null,
    estimated_hours: row.estimatedHours ?? null,
    parent_task_id: row.parentTaskId ?? null,
    created_at: row.createdAt ?? "",
    updated_at: row.updatedAt ?? "",
    metadata: (row.taskMetadata as Record<string, unknown> | null) ?? {},
    assignees: [],
    recurrence_rule: null,
    tags: [],
    sort_order: row.sortOrder ?? undefined,
  } as unknown as ApiTask;
}

export async function applyRemoteTasks(list: ApiTask[]): Promise<void> {
  if (!list.length) return;
  const db = getDb();
  const now = new Date().toISOString();
  for (const t of list) {
    const anyT = t as unknown as Record<string, unknown>;
    await db
      .insert(schema.tasks)
      .values({
        id: t.id,
        projectId: (anyT.project_id as string) ?? "",
        title: t.title,
        description: (anyT.description as string | null) ?? null,
        status: normalizeTaskStatus(t.status),
        priority: (anyT.priority as string | null) ?? null,
        startAt: (anyT.start_at as string | null) ?? null,
        endAt: (anyT.end_at as string | null) ?? null,
        allDay: Boolean(anyT.all_day),
        reminderOffsets: (anyT.reminder_offsets as unknown) ?? null,
        notificationsEnabled:
          (anyT.notifications_enabled as boolean | null) ?? true,
        source: (anyT.source as string | null) ?? null,
        createdBy: (anyT.created_by as string | null) ?? null,
        completedAt: (anyT.completed_at as string | null) ?? null,
        archivedAt: (anyT.archived_at as string | null) ?? null,
        estimatedHours: (anyT.estimated_hours as number | null) ?? null,
        parentTaskId: (anyT.parent_task_id as string | null) ?? null,
        taskMetadata: (anyT.metadata as unknown) ?? null,
        sortOrder: (anyT.sort_order as number | null) ?? null,
        createdAt: (anyT.created_at as string | null) ?? now,
        updatedAt: (anyT.updated_at as string | null) ?? now,
        deletedAt: null,
      })
      .onConflictDoUpdate({
        target: schema.tasks.id,
        set: {
          projectId: (anyT.project_id as string) ?? "",
          title: t.title,
          description: (anyT.description as string | null) ?? null,
          status: normalizeTaskStatus(t.status),
          priority: (anyT.priority as string | null) ?? null,
          startAt: (anyT.start_at as string | null) ?? null,
          endAt: (anyT.end_at as string | null) ?? null,
          allDay: Boolean(anyT.all_day),
          reminderOffsets: (anyT.reminder_offsets as unknown) ?? null,
          notificationsEnabled:
            (anyT.notifications_enabled as boolean | null) ?? true,
          completedAt: (anyT.completed_at as string | null) ?? null,
          archivedAt: (anyT.archived_at as string | null) ?? null,
          estimatedHours: (anyT.estimated_hours as number | null) ?? null,
          parentTaskId: (anyT.parent_task_id as string | null) ?? null,
          taskMetadata: (anyT.metadata as unknown) ?? null,
          sortOrder: (anyT.sort_order as number | null) ?? null,
          updatedAt: (anyT.updated_at as string | null) ?? now,
          deletedAt: null,
        },
      });
  }
  void rescheduleLocalTaskNotificationsFromCache();
}

export async function applyTaskTombstones(
  tombstones: Array<{ id: string; deleted_at?: string | null }>,
): Promise<void> {
  if (!tombstones.length) return;
  const db = getDb();
  for (const item of tombstones) {
    const deletedAt = item.deleted_at ?? new Date().toISOString();
    await db
      .update(schema.tasks)
      .set({ deletedAt, updatedAt: deletedAt })
      .where(eq(schema.tasks.id, item.id));
  }
  void rescheduleLocalTaskNotificationsFromCache();
}

function buildLocalTask(
  data: Record<string, unknown>,
  id = randomId(),
): ApiTask {
  const now = new Date().toISOString();
  const nextStatus = normalizeTaskStatus(data.status ?? "open");
  const completedAt =
    data.completed_at !== undefined
      ? ((data.completed_at as string | null) ?? null)
      : nextStatus === "closed"
        ? now
        : null;
  return {
    id,
    project_id: String(data.project_id ?? ""),
    title: String(data.title ?? ""),
    description: (data.description as string | null) ?? null,
    status: nextStatus,
    priority: String(data.priority ?? "normal"),
    start_at: (data.start_at as string | null) ?? null,
    end_at: (data.end_at as string | null) ?? null,
    all_day: Boolean(data.all_day),
    reminder_offsets: (data.reminder_offsets as number[] | undefined) ?? [],
    notifications_enabled:
      (data.notifications_enabled as boolean | undefined) ?? true,
    source: String(data.source ?? "mobile"),
    created_by: null,
    completed_at: completedAt,
    estimated_hours:
      typeof data.estimated_hours === "number" ? data.estimated_hours : null,
    parent_task_id: (data.parent_task_id as string | null) ?? null,
    created_at: now,
    updated_at: now,
    metadata: (data.metadata as Record<string, unknown> | undefined) ?? {},
    assignees: [],
    tags: [],
  };
}

function localUpdatePatch(data: Record<string, unknown>) {
  const patch: Partial<typeof schema.tasks.$inferInsert> = {
    updatedAt: new Date().toISOString(),
  };
  if ("project_id" in data) patch.projectId = String(data.project_id ?? "");
  if ("title" in data) patch.title = String(data.title ?? "");
  if ("description" in data)
    patch.description = (data.description as string | null) ?? null;
  if ("status" in data) {
    patch.status = normalizeTaskStatus(data.status ?? "open");
    patch.completedAt =
      patch.status === "closed" ? new Date().toISOString() : null;
  }
  if ("priority" in data)
    patch.priority = (data.priority as string | null) ?? null;
  if ("start_at" in data)
    patch.startAt = (data.start_at as string | null) ?? null;
  if ("end_at" in data) patch.endAt = (data.end_at as string | null) ?? null;
  if ("all_day" in data) patch.allDay = Boolean(data.all_day);
  if ("reminder_offsets" in data)
    patch.reminderOffsets = data.reminder_offsets as unknown;
  if ("notifications_enabled" in data) {
    patch.notificationsEnabled = Boolean(data.notifications_enabled);
  }
  if ("completed_at" in data) {
    patch.completedAt = (data.completed_at as string | null) ?? null;
  }
  if ("estimated_hours" in data) {
    const value = data.estimated_hours;
    patch.estimatedHours =
      typeof value === "number" && Number.isFinite(value) ? value : null;
  }
  if ("parent_task_id" in data) {
    patch.parentTaskId = (data.parent_task_id as string | null) ?? null;
  }
  if ("metadata" in data) patch.taskMetadata = data.metadata as unknown;
  return patch;
}

export const tasksRepo = {
  /** List from local cache only. */
  async listLocal(projectId?: string | null): Promise<ApiTask[]> {
    const db = getDb();
    const base = db
      .select({
        task: schema.tasks,
        projectName: schema.projects.name,
        projectMetadata: schema.projects.projectMetadata,
      })
      .from(schema.tasks)
      .leftJoin(
        schema.projects,
        eq(schema.tasks.projectId, schema.projects.id),
      );
    const rows = projectId
      ? await base
          .where(
            and(
              eq(schema.tasks.projectId, projectId),
              isNull(schema.tasks.deletedAt),
            ),
          )
          .orderBy(desc(schema.tasks.updatedAt))
      : await base
          .where(isNull(schema.tasks.deletedAt))
          .orderBy(desc(schema.tasks.updatedAt));
    return rows
      .map((row) =>
        toApiShape(
          row.task,
          row.projectName,
          extractProjectColor(row.projectMetadata),
        ),
      )
      .sort((a, b) => {
        const aSort =
          typeof a.sort_order === "number"
            ? a.sort_order
            : Number.POSITIVE_INFINITY;
        const bSort =
          typeof b.sort_order === "number"
            ? b.sort_order
            : Number.POSITIVE_INFINITY;
        if (aSort !== bSort) return aSort - bSort;
        return String(b.updated_at ?? "").localeCompare(
          String(a.updated_at ?? ""),
        );
      });
  },

  /**
   * Online returns fresh data; offline or failed refresh falls back to local cache.
   */
  async list(projectId?: string | null): Promise<ApiTask[]> {
    const local = await this.listLocal(projectId);
    if (await canUseServer()) {
      try {
        return await this.refresh(projectId);
      } catch (error) {
        if (local.length === 0) {
          throw error;
        }
        return local;
      }
    }
    return local;
  },

  async listLocalBySpace(spaceId: string): Promise<ApiTask[]> {
    const all = await this.listLocal(null);
    const db = getDb();
    const projects = await db
      .select()
      .from(schema.projects)
      .where(
        and(
          eq(schema.projects.spaceId, spaceId),
          isNull(schema.projects.deletedAt),
        ),
      );
    const projectIds = new Set(projects.map((project) => project.id));
    return all.filter((task) => projectIds.has(task.project_id));
  },

  async listByScope(
    scope: {
      project_id?: string | null;
      space_id?: string | null;
    } = {},
  ): Promise<ApiTask[]> {
    if (scope.project_id) return this.list(scope.project_id);
    const local = scope.space_id
      ? await this.listLocalBySpace(scope.space_id)
      : await this.listLocal(null);
    if (await canUseServer()) {
      try {
        const list = await taskApi.listTasksByScope({
          ...(scope.space_id ? { space_id: scope.space_id } : {}),
        });
        await applyRemoteTasks(list);
        return list;
      } catch (error) {
        if (local.length === 0) {
          throw error;
        }
        return local;
      }
    }
    return local;
  },

  /** Fetch from server and upsert into SQLite. */
  async refresh(projectId?: string | null): Promise<ApiTask[]> {
    const list = projectId
      ? await taskApi.listTasks(projectId)
      : await taskApi.listAllTasks();
    await applyRemoteTasks(list);
    return list;
  },

  async get(taskId: string): Promise<ApiTask | null> {
    const db = getDb();
    const rows = await db
      .select({
        task: schema.tasks,
        projectName: schema.projects.name,
        projectMetadata: schema.projects.projectMetadata,
      })
      .from(schema.tasks)
      .leftJoin(schema.projects, eq(schema.tasks.projectId, schema.projects.id))
      .where(and(eq(schema.tasks.id, taskId), isNull(schema.tasks.deletedAt)));
    const local = rows[0]
      ? toApiShape(
          rows[0].task,
          rows[0].projectName,
          extractProjectColor(rows[0].projectMetadata),
        )
      : null;
    if (await canUseServer()) {
      try {
        const remote = await taskApi.getTask(taskId);
        await applyRemoteTasks([remote]);
        return remote;
      } catch (error) {
        if (!local) throw error;
      }
    }
    return local;
  },

  async create(data: Record<string, unknown>): Promise<ApiTask> {
    const hasToken = Boolean(await getToken());
    if (await canUseServer()) {
      try {
        const created = await taskApi.createTask(data);
        await applyRemoteTasks([created]);
        return created;
      } catch {
        // Fall back to local-first below.
      }
    }

    const local = buildLocalTask(data);
    await applyRemoteTasks([local]);
    if (hasToken) {
      await enqueueOutbox({
        table: "tasks",
        action: "create",
        entityId: local.id,
        payload: local,
      });
    }
    return local;
  },

  async update(
    taskId: string,
    data: Record<string, unknown>,
  ): Promise<ApiTask> {
    const hasToken = Boolean(await getToken());
    if (await canUseServer()) {
      try {
        const updated = await taskApi.updateTask(taskId, data);
        await applyRemoteTasks([updated]);
        return updated;
      } catch {
        // Fall back to local-first below.
      }
    }

    const db = getDb();
    const before = (
      await db.select().from(schema.tasks).where(eq(schema.tasks.id, taskId))
    )[0];
    await db
      .update(schema.tasks)
      .set(localUpdatePatch(data))
      .where(eq(schema.tasks.id, taskId));
    if (hasToken) {
      await enqueueOutbox({
        table: "tasks",
        action: "update",
        entityId: taskId,
        payload: data,
        baseUpdatedAt: before?.updatedAt ?? null,
      });
    }
    void rescheduleLocalTaskNotificationsFromCache();
    const after = (
      await db.select().from(schema.tasks).where(eq(schema.tasks.id, taskId))
    )[0];
    return after
      ? toApiShape(after)
      : buildLocalTask({ ...data, title: data.title ?? "" }, taskId);
  },

  async reorder(projectId: string | null, taskIds: string[]): Promise<void> {
    const uniqueTaskIds = taskIds.filter(
      (taskId, index) => taskIds.indexOf(taskId) === index,
    );
    if (uniqueTaskIds.length === 0) return;

    if (await canUseServer()) {
      if (projectId) {
        await taskApi.reorderTasks(projectId, uniqueTaskIds);
      } else {
        await taskApi.reorderAllTasks(uniqueTaskIds);
      }
      const refreshed = projectId
        ? await taskApi.listTasks(projectId)
        : await taskApi.listAllTasks();
      await applyRemoteTasks(refreshed);
      return;
    }

    const db = getDb();
    const hasToken = Boolean(await getToken());
    for (const [index, taskId] of uniqueTaskIds.entries()) {
      await db
        .update(schema.tasks)
        .set({ sortOrder: index, updatedAt: new Date().toISOString() })
        .where(eq(schema.tasks.id, taskId));
      if (hasToken) {
        await enqueueOutbox({
          table: "tasks",
          action: "update",
          entityId: taskId,
          payload: { sort_order: index },
        });
      }
    }
  },

  async delete(taskId: string): Promise<void> {
    const db = getDb();
    const hasToken = Boolean(await getToken());
    let shouldQueue = hasToken;
    if (await canUseServer()) {
      try {
        await taskApi.deleteTask(taskId);
        shouldQueue = false;
      } catch {
        // Fall back to local-first below.
        shouldQueue = true;
      }
    }

    const before = (
      await db.select().from(schema.tasks).where(eq(schema.tasks.id, taskId))
    )[0];
    const now = new Date().toISOString();
    await db
      .update(schema.tasks)
      .set({ deletedAt: now, updatedAt: now })
      .where(eq(schema.tasks.id, taskId));
    if (shouldQueue) {
      await enqueueOutbox({
        table: "tasks",
        action: "delete",
        entityId: taskId,
        payload: {},
        baseUpdatedAt: before?.updatedAt ?? null,
      });
    }
    void rescheduleLocalTaskNotificationsFromCache();
  },
};
