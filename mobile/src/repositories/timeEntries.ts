import { and, desc, eq, inArray, isNull } from "drizzle-orm";
import { getDb, schema } from "../db/client";
import { decodeTokenPayload, getToken } from "../lib/auth";
import { taskApi } from "../lib/task-api";
import { useNetworkStore } from "../stores/network";
import type { TimeEntry, TimeReport, TimeReportBucket } from "../types/api";
import { enqueueOutbox, randomId } from "./outbox";

type JoinedTimeEntryRow = {
  entry: typeof schema.timeEntries.$inferSelect;
  taskTitle: string;
  projectId: string;
  projectName: string | null;
  projectMetadata: unknown;
};

type TimeEntryScope = string | { project_id?: string; space_id?: string };

export function calculateTimeEntryDuration(
  entry: Pick<TimeEntry, "started_at" | "ended_at">,
  now: Date = new Date(),
): number | null {
  if (!entry.started_at) return null;
  const startedAt = new Date(entry.started_at).getTime();
  const endedAt = entry.ended_at
    ? new Date(entry.ended_at).getTime()
    : now.getTime();
  if (
    Number.isNaN(startedAt) ||
    Number.isNaN(endedAt) ||
    endedAt <= startedAt
  ) {
    return null;
  }
  return Math.floor((endedAt - startedAt) / 1000);
}

function toApiShape(row: JoinedTimeEntryRow): TimeEntry {
  const metadata =
    (row.entry.entryMetadata as Record<string, unknown> | null) ?? {};
  return {
    id: row.entry.id,
    task_id: row.entry.taskId ?? "",
    occurrence_id: row.entry.occurrenceId ?? null,
    user_id: row.entry.userId ?? "",
    started_at: row.entry.startedAt,
    ended_at: row.entry.endedAt ?? null,
    duration_seconds: calculateTimeEntryDuration({
      started_at: row.entry.startedAt,
      ended_at: row.entry.endedAt ?? null,
    }),
    source: row.entry.source ?? "manual",
    note: row.entry.note ?? null,
    task_title: row.taskTitle,
    project_id: row.projectId,
    project_name: row.projectName,
    project_color:
      row.projectMetadata &&
      typeof row.projectMetadata === "object" &&
      !Array.isArray(row.projectMetadata) &&
      typeof (row.projectMetadata as Record<string, unknown>).color === "string"
        ? ((row.projectMetadata as Record<string, unknown>).color as string)
        : null,
    metadata,
    updated_at: row.entry.updatedAt ?? null,
    deleted_at: row.entry.deletedAt ?? null,
  };
}

function toBucketMapEntry(
  target: Map<string, TimeReportBucket>,
  key: string,
  label: string,
): TimeReportBucket {
  const bucket = target.get(key);
  if (bucket) return bucket;
  const created = { key, label, seconds: 0, entries: 0 };
  target.set(key, created);
  return created;
}

function withinDateRange(
  entry: TimeEntry,
  dateFrom?: string,
  dateTo?: string,
): boolean {
  if (!entry.started_at) return false;
  const startedAt = new Date(entry.started_at);
  if (Number.isNaN(startedAt.getTime())) return false;
  if (dateFrom) {
    const lower = new Date(`${dateFrom}T00:00:00`);
    if (startedAt < lower) return false;
  }
  if (dateTo) {
    const upper = new Date(`${dateTo}T23:59:59.999`);
    if (startedAt > upper) return false;
  }
  return true;
}

async function currentUserId(): Promise<string | null> {
  const token = await getToken();
  if (!token) return null;
  return decodeTokenPayload(token, { ignoreExpiration: true })?.user_id ?? null;
}

async function canUseServer(): Promise<boolean> {
  const network = useNetworkStore.getState();
  return network.online && network.serverReachable && Boolean(await getToken());
}

async function joinTimeEntries(
  scope?: TimeEntryScope | null,
  taskId?: string | null,
): Promise<JoinedTimeEntryRow[]> {
  const db = getDb();
  let projectIds: string[] | null = null;
  const projectId = typeof scope === "string" ? scope : scope?.project_id;
  const spaceId = typeof scope === "object" ? scope?.space_id : null;
  if (spaceId && !projectId) {
    const rows = await db
      .select({ id: schema.projects.id })
      .from(schema.projects)
      .where(
        and(
          eq(schema.projects.spaceId, spaceId),
          isNull(schema.projects.deletedAt),
        ),
      );
    projectIds = rows.map((row) => row.id);
    if (projectIds.length === 0) return [];
  }
  return await db
    .select({
      entry: schema.timeEntries,
      taskTitle: schema.tasks.title,
      projectId: schema.tasks.projectId,
      projectName: schema.projects.name,
      projectMetadata: schema.projects.projectMetadata,
    })
    .from(schema.timeEntries)
    .innerJoin(schema.tasks, eq(schema.timeEntries.taskId, schema.tasks.id))
    .innerJoin(schema.projects, eq(schema.tasks.projectId, schema.projects.id))
    .where(
      and(
        projectId ? eq(schema.tasks.projectId, projectId) : undefined,
        projectIds ? inArray(schema.tasks.projectId, projectIds) : undefined,
        taskId ? eq(schema.timeEntries.taskId, taskId) : undefined,
        isNull(schema.timeEntries.deletedAt),
        isNull(schema.tasks.deletedAt),
      ),
    )
    .orderBy(desc(schema.timeEntries.startedAt));
}

export async function applyRemoteTimeEntries(list: TimeEntry[]): Promise<void> {
  if (!list.length) return;
  const db = getDb();
  const now = new Date().toISOString();
  for (const entry of list) {
    await db
      .insert(schema.timeEntries)
      .values({
        id: entry.id,
        taskId: entry.task_id,
        occurrenceId: entry.occurrence_id ?? null,
        userId: entry.user_id,
        startedAt: entry.started_at ?? now,
        endedAt: entry.ended_at ?? null,
        source: entry.source,
        note: entry.note ?? null,
        entryMetadata: entry.metadata ?? {},
        createdAt: (entry as { created_at?: string | null }).created_at ?? now,
        updatedAt: entry.updated_at ?? now,
        deletedAt: null,
      })
      .onConflictDoUpdate({
        target: schema.timeEntries.id,
        set: {
          taskId: entry.task_id,
          occurrenceId: entry.occurrence_id ?? null,
          userId: entry.user_id,
          startedAt: entry.started_at ?? now,
          endedAt: entry.ended_at ?? null,
          source: entry.source,
          note: entry.note ?? null,
          entryMetadata: entry.metadata ?? {},
          updatedAt: entry.updated_at ?? now,
          deletedAt: null,
        },
      });
  }
}

export async function applyTimeEntryTombstones(
  tombstones: Array<{ id: string; deleted_at?: string | null }>,
): Promise<void> {
  if (!tombstones.length) return;
  const db = getDb();
  for (const item of tombstones) {
    const deletedAt = item.deleted_at ?? new Date().toISOString();
    await db
      .update(schema.timeEntries)
      .set({ deletedAt, updatedAt: deletedAt })
      .where(eq(schema.timeEntries.id, item.id));
  }
}

export function buildTimeReportFromEntries(
  entries: TimeEntry[],
  now: Date = new Date(),
): TimeReport {
  const byProject = new Map<string, TimeReportBucket>();
  const byDay = new Map<string, TimeReportBucket>();
  const byUser = new Map<string, TimeReportBucket>();
  const byTask = new Map<string, TimeReportBucket>();

  const report: TimeReport = {
    summary: {
      total_seconds: 0,
      entry_count: 0,
      active_entries: 0,
    },
    by_project: [],
    by_day: [],
    by_user: [],
    by_task: [],
  };

  for (const entry of entries) {
    if (!entry.started_at) continue;
    const startedAt = new Date(entry.started_at);
    if (Number.isNaN(startedAt.getTime())) continue;
    const duration = calculateTimeEntryDuration(entry, now) ?? 0;
    report.summary.total_seconds += duration;
    report.summary.entry_count += 1;
    if (!entry.ended_at) {
      report.summary.active_entries += 1;
    }

    const projectBucket = toBucketMapEntry(
      byProject,
      entry.project_id ?? "unknown",
      entry.project_name ?? entry.project_id ?? "Unknown project",
    );
    projectBucket.seconds += duration;
    projectBucket.entries += 1;

    const taskBucket = toBucketMapEntry(
      byTask,
      entry.task_id,
      entry.task_title ?? "Unknown task",
    );
    taskBucket.seconds += duration;
    taskBucket.entries += 1;
    taskBucket.project_id = entry.project_id ?? null;
    taskBucket.project_name = entry.project_name ?? null;

    const userBucket = toBucketMapEntry(
      byUser,
      entry.user_id || "unknown",
      entry.user_id || "Unknown user",
    );
    userBucket.seconds += duration;
    userBucket.entries += 1;

    const dayKey = startedAt.toISOString().slice(0, 10);
    const dayBucket = toBucketMapEntry(byDay, dayKey, dayKey);
    dayBucket.seconds += duration;
    dayBucket.entries += 1;
  }

  report.by_project = [...byProject.values()].sort(
    (a, b) => b.seconds - a.seconds,
  );
  report.by_day = [...byDay.values()].sort((a, b) =>
    a.key.localeCompare(b.key),
  );
  report.by_user = [...byUser.values()].sort((a, b) => b.seconds - a.seconds);
  report.by_task = [...byTask.values()].sort((a, b) => b.seconds - a.seconds);
  return report;
}

export const timeEntriesRepo = {
  async listLocal(
    scope?: TimeEntryScope | null,
    dateFrom?: string,
    dateTo?: string,
  ): Promise<TimeEntry[]> {
    const rows = await joinTimeEntries(scope);
    return rows
      .map(toApiShape)
      .filter((entry) => withinDateRange(entry, dateFrom, dateTo));
  },

  async list(
    scope?: TimeEntryScope | null,
    dateFrom?: string,
    dateTo?: string,
  ): Promise<TimeEntry[]> {
    const local = await this.listLocal(scope, dateFrom, dateTo);
    if (scope && (await canUseServer())) {
      try {
        return await this.refresh(scope, dateFrom, dateTo);
      } catch {
        return local;
      }
    }
    return local;
  },

  async refresh(
    scope: TimeEntryScope,
    dateFrom?: string,
    dateTo?: string,
  ): Promise<TimeEntry[]> {
    const list = await taskApi.listTimeEntries(scope, dateFrom, dateTo);
    await applyRemoteTimeEntries(list);
    return list;
  },

  async getReport(
    scope: TimeEntryScope,
    dateFrom?: string,
    dateTo?: string,
  ): Promise<TimeReport> {
    const entries = await this.list(scope, dateFrom, dateTo);
    return buildTimeReportFromEntries(entries);
  },

  async getActiveLocal(taskId?: string | null): Promise<TimeEntry | null> {
    const rows = await joinTimeEntries(undefined, taskId ?? undefined);
    const active = rows.find(
      (row) => row.entry.endedAt == null && row.entry.deletedAt == null,
    );
    return active ? toApiShape(active) : null;
  },

  async getActive(taskId?: string | null): Promise<TimeEntry | null> {
    const local = await this.getActiveLocal(taskId);
    if (local) return local;
    if (!(await canUseServer())) return null;
    const remote = await taskApi.getActiveTimer();
    if (remote) {
      await applyRemoteTimeEntries([remote]);
      if (!taskId || remote.task_id === taskId) return remote;
    }
    return null;
  },

  async startTimer(
    taskId: string,
    occurrenceId?: string | null,
    note?: string | null,
  ): Promise<TimeEntry> {
    const hasToken = Boolean(await getToken());
    if (await canUseServer()) {
      try {
        const entry = await taskApi.startTimer(taskId, occurrenceId, note);
        await applyRemoteTimeEntries([entry]);
        return entry;
      } catch {
        // Fall back to local-first below.
      }
    }

    const db = getDb();
    const now = new Date().toISOString();
    const currentUser = (await currentUserId()) ?? "";
    const active = await this.getActiveLocal();
    if (active?.id) {
      await db
        .update(schema.timeEntries)
        .set({ endedAt: now, updatedAt: now })
        .where(eq(schema.timeEntries.id, active.id));
      if (hasToken) {
        await enqueueOutbox({
          table: "time_entries",
          action: "update",
          entityId: active.id,
          payload: { ended_at: now },
          baseUpdatedAt: active.updated_at ?? null,
        });
      }
    }

    const local: TimeEntry = {
      id: randomId(),
      task_id: taskId,
      occurrence_id: occurrenceId ?? null,
      user_id: currentUser,
      started_at: now,
      ended_at: null,
      duration_seconds: null,
      source: "mobile",
      note: note ?? null,
      metadata: {},
      updated_at: now,
      deleted_at: null,
    };

    await applyRemoteTimeEntries([local]);
    if (hasToken) {
      await enqueueOutbox({
        table: "time_entries",
        action: "create",
        entityId: local.id,
        payload: {
          task_id: taskId,
          occurrence_id: occurrenceId ?? null,
          started_at: now,
          ended_at: null,
          source: "mobile",
          note: note ?? null,
        },
      });
    }
    return (await this.getActiveLocal(taskId)) ?? local;
  },

  async stopTimer(entryId?: string): Promise<TimeEntry> {
    const hasToken = Boolean(await getToken());
    if (await canUseServer()) {
      try {
        const entry = await taskApi.stopTimer(entryId);
        await applyRemoteTimeEntries([entry]);
        return entry;
      } catch {
        // Fall back to local-first below.
      }
    }

    const active = entryId
      ? (await this.listLocal()).find(
          (entry) => entry.id === entryId && !entry.ended_at,
        )
      : await this.getActiveLocal();
    if (!active) {
      throw new Error("No active timer found");
    }

    const db = getDb();
    const now = new Date().toISOString();
    await db
      .update(schema.timeEntries)
      .set({ endedAt: now, updatedAt: now })
      .where(eq(schema.timeEntries.id, active.id));
    if (hasToken) {
      await enqueueOutbox({
        table: "time_entries",
        action: "update",
        entityId: active.id,
        payload: { ended_at: now },
        baseUpdatedAt: active.updated_at ?? null,
      });
    }
    return (
      (await this.listLocal()).find((entry) => entry.id === active.id) ?? {
        ...active,
        ended_at: now,
        duration_seconds: calculateTimeEntryDuration({
          started_at: active.started_at ?? null,
          ended_at: now,
        }),
        updated_at: now,
      }
    );
  },

  async update(
    entryId: string,
    data: Record<string, unknown>,
  ): Promise<TimeEntry> {
    const hasToken = Boolean(await getToken());
    if (await canUseServer()) {
      try {
        const entry = await taskApi.updateTimeEntry(entryId, data);
        await applyRemoteTimeEntries([entry]);
        return entry;
      } catch {
        // Fall back to local-first below.
      }
    }

    const db = getDb();
    const before = (
      await db
        .select()
        .from(schema.timeEntries)
        .where(eq(schema.timeEntries.id, entryId))
    )[0];
    const patch: Partial<typeof schema.timeEntries.$inferInsert> = {
      updatedAt: new Date().toISOString(),
    };
    if ("started_at" in data && data.started_at != null)
      patch.startedAt = String(data.started_at);
    if ("ended_at" in data)
      patch.endedAt = (data.ended_at as string | null) ?? null;
    if ("note" in data) patch.note = (data.note as string | null) ?? null;
    if ("source" in data && data.source != null)
      patch.source = String(data.source);
    if ("occurrence_id" in data)
      patch.occurrenceId = (data.occurrence_id as string | null) ?? null;
    if ("metadata" in data)
      patch.entryMetadata = data.metadata as Record<string, unknown>;
    await db
      .update(schema.timeEntries)
      .set(patch)
      .where(eq(schema.timeEntries.id, entryId));
    if (hasToken) {
      await enqueueOutbox({
        table: "time_entries",
        action: "update",
        entityId: entryId,
        payload: data,
        baseUpdatedAt: before?.updatedAt ?? null,
      });
    }
    return (await this.listLocal()).find(
      (entry) => entry.id === entryId,
    ) as TimeEntry;
  },

  async delete(entryId: string): Promise<void> {
    const db = getDb();
    const hasToken = Boolean(await getToken());
    let shouldQueue = hasToken;
    if (await canUseServer()) {
      try {
        await taskApi.deleteTimeEntry(entryId);
        shouldQueue = false;
      } catch {
        shouldQueue = true;
      }
    }
    if (shouldQueue) {
      const before = (
        await db
          .select()
          .from(schema.timeEntries)
          .where(eq(schema.timeEntries.id, entryId))
      )[0];
      await enqueueOutbox({
        table: "time_entries",
        action: "delete",
        entityId: entryId,
        payload: {},
        baseUpdatedAt: before?.updatedAt ?? null,
      });
    }
    const now = new Date().toISOString();
    await db
      .update(schema.timeEntries)
      .set({ deletedAt: now, updatedAt: now })
      .where(eq(schema.timeEntries.id, entryId));
  },
};
