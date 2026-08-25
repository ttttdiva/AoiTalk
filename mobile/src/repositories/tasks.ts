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

import { eq, isNull, isNotNull, and, desc } from "drizzle-orm";
import { getDb, schema } from "../db/client";
import { getToken, getTokenAuthScope } from "../lib/auth";
import {
  isApiConnectionError,
  isApiHttpError,
  isApiTimeoutError,
} from "../lib/api-client";
import {
  taskApi,
  TaskCompletionCancelledError,
} from "../lib/task-api";
import { normalizeTaskStatus } from "../lib/task-status";
import { nowServerNaiveIso } from "../lib/task-datetime";
import { rescheduleLocalTaskNotificationsFromCache } from "../lib/local-notifications";
import { useNetworkStore } from "../stores/network";
import type {
  Task as ApiTask,
  TaskAssignee,
  TaskAttachment,
} from "../types/api";
import { enqueueOutbox, randomId } from "./outbox";

type DbTask = typeof schema.tasks.$inferSelect;
const CACHED_TAGS_KEY = "mobile_cached_tags";
const TASK_TOMBSTONE_LEDGER_PREFIX = "tasks:tombstones:";
// The mobile SQLite schema is migrated by the mobile-db owner. Keep a small
// metadata mirror while older installs are still on the pre-field schema so
// sync never silently turns an enabled setting back off.
const AUTO_CLOSE_ON_DUE_METADATA_KEY = "mobile_auto_close_on_due";

type DbTaskWithAutoClose = DbTask & {
  autoCloseOnDue?: boolean | null;
};

type TaskTombstoneLedgerContext = {
  storageKey: string;
  entries: Map<string, string>;
};

// `tasks.deleted_at` rows are normally retained locally, but a tombstone can
// arrive before the corresponding row (for example after an account switch
// or a partial cache repair).  Keep a small durable ledger in the existing
// sync_state cursor column so a later stale active payload cannot recreate it.
// The in-memory mirror is only a fallback for old test/rolling DB doubles.
const taskTombstoneLedgerMemory = new Map<string, Map<string, string>>();

function parseTimestamp(value: unknown): number | null {
  if (typeof value !== "string" || !value.trim()) return null;
  const raw = value.trim();
  const normalized = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(raw)
    ? raw
    : `${raw}Z`;
  const parsed = Date.parse(normalized);
  return Number.isFinite(parsed) ? parsed : null;
}

/** Compare server timestamps without treating a naive UTC value as local time. */
export function compareTaskTimestamps(
  left: string | null | undefined,
  right: string | null | undefined,
): number {
  const leftMs = parseTimestamp(left);
  const rightMs = parseTimestamp(right);
  if (leftMs !== null && rightMs !== null) {
    return leftMs === rightMs ? 0 : leftMs < rightMs ? -1 : 1;
  }
  if (left == null && right == null) return 0;
  if (left == null) return -1;
  if (right == null) return 1;
  return left === right ? 0 : left < right ? -1 : 1;
}

async function taskTombstoneLedgerKey(): Promise<string> {
  let scope = "anonymous";
  try {
    const token = await getToken();
    if (token) {
      scope =
        typeof getTokenAuthScope === "function"
          ? getTokenAuthScope(token)
          : token;
    }
  } catch {
    // Keep the anonymous fallback for lightweight test doubles and an
    // unavailable secure store.  Production auth transitions clear sync_state.
  }
  return `${TASK_TOMBSTONE_LEDGER_PREFIX}${scope}`;
}

function mergeTaskLedgerTimestamp(
  entries: Map<string, string>,
  id: string,
  timestamp: string,
): boolean {
  const previous = entries.get(id);
  if (previous && compareTaskTimestamps(timestamp, previous) < 0) return false;
  if (previous === timestamp) return false;
  entries.set(id, timestamp);
  return true;
}

async function loadTaskTombstoneLedger(): Promise<TaskTombstoneLedgerContext> {
  const storageKey = await taskTombstoneLedgerKey();
  const entries = new Map(taskTombstoneLedgerMemory.get(storageKey) ?? []);
  const syncState = (schema as unknown as {
    syncState?: { tableName?: unknown; cursor?: unknown };
  }).syncState;
  if (!syncState?.tableName || !syncState.cursor) {
    return { storageKey, entries };
  }
  try {
    const db = getDb();
    const stateTable = syncState as {
      tableName: any;
      cursor: any;
    };
    const rows = (await (db as any)
      .select({ cursor: stateTable.cursor })
      .from(stateTable)
      .where(eq(stateTable.tableName, storageKey))) as Array<{
      cursor?: unknown;
    }>;
    const raw = rows[0]?.cursor;
    if (typeof raw === "string" && raw.trim()) {
      const parsed = JSON.parse(raw) as unknown;
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        for (const [id, timestamp] of Object.entries(
          parsed as Record<string, unknown>,
        )) {
          if (typeof timestamp === "string") {
            mergeTaskLedgerTimestamp(entries, id, timestamp);
          }
        }
      }
    }
  } catch {
    // A legacy/partial test database may not expose sync_state yet.  The
    // in-memory mirror still protects the current process.
  }
  taskTombstoneLedgerMemory.set(storageKey, entries);
  return { storageKey, entries };
}

async function persistTaskTombstoneLedger(
  context: TaskTombstoneLedgerContext,
): Promise<void> {
  taskTombstoneLedgerMemory.set(context.storageKey, context.entries);
  const syncState = (schema as unknown as {
    syncState?: { tableName?: unknown; cursor?: unknown };
  }).syncState;
  if (!syncState?.tableName || !syncState.cursor) return;
  try {
    const db = getDb();
    const stateTable = syncState as {
      tableName: any;
      cursor: any;
    };
    await (db as any)
      .insert(stateTable)
      .values({
        tableName: context.storageKey,
        lastPulledAt: null,
        lastPushedAt: null,
        cursor: JSON.stringify(Object.fromEntries(context.entries)),
      } as never)
      .onConflictDoUpdate({
        target: stateTable.tableName,
        set: { cursor: JSON.stringify(Object.fromEntries(context.entries)) },
      });
  } catch {
    // Keep the in-memory mirror when an older database cannot write the
    // optional ledger row.  The next schema bootstrap can persist it.
  }
}

async function readLocalTaskRow(
  db: ReturnType<typeof getDb>,
  taskId: string,
): Promise<DbTask | null> {
  try {
    const rows = await db
      .select()
      .from(schema.tasks)
      .where(eq(schema.tasks.id, taskId));
    return (rows[0] as DbTask | undefined) ?? null;
  } catch {
    // Keep compatibility with very small repository test doubles that only
    // implement insert/update paths.
    return null;
  }
}

function autoCloseColumn(): unknown {
  return (schema.tasks as unknown as { autoCloseOnDue?: unknown })
    .autoCloseOnDue;
}

function autoCloseOnDueFromRow(
  row: DbTaskWithAutoClose,
  metadata: Record<string, unknown>,
): boolean {
  if (typeof row.autoCloseOnDue === "boolean") return row.autoCloseOnDue;
  return metadata[AUTO_CLOSE_ON_DUE_METADATA_KEY] === true;
}

function metadataWithAutoClose(
  metadata: Record<string, unknown>,
  value: unknown,
): Record<string, unknown> {
  if (typeof value !== "boolean") return metadata;
  return { ...metadata, [AUTO_CLOSE_ON_DUE_METADATA_KEY]: value };
}

// ---------- フォーカス毎フル取得の throttle ----------
// デルタ同期（runSync）が鮮度を担保するため、一覧のフル取得は
// (a) ローカル空の初回 (b) 明示的な pull-to-refresh (c) 前回から60秒経過
// のいずれかに限定する。スコープ単位で最終フル取得時刻を保持する。
const FULL_FETCH_THROTTLE_MS = 60_000;
const lastFullFetchAt = new Map<string, number>();

/**
 * フル取得を実行すべきか判定する純粋関数。
 * - force（明示refresh）: 常に取得
 * - localEmpty（ローカル空の初回）: 常に取得
 * - lastAt 未設定（そのスコープ初回）: 取得
 * - それ以外: 前回から FULL_FETCH_THROTTLE_MS 経過していれば取得
 */
export function shouldRunFullFetch(
  lastAt: number | undefined,
  now: number,
  localEmpty: boolean,
  force: boolean,
): boolean {
  if (force) return true;
  if (localEmpty) return true;
  if (lastAt === undefined) return true;
  return now - lastAt >= FULL_FETCH_THROTTLE_MS;
}

/** テスト用: スコープ別の最終フル取得時刻をクリアする。 */
export function resetTaskFullFetchThrottle(): void {
  lastFullFetchAt.clear();
}

function taskMetadataWithCachedTags(
  task: Record<string, unknown>,
): Record<string, unknown> {
  const metadata =
    task.metadata &&
    typeof task.metadata === "object" &&
    !Array.isArray(task.metadata)
      ? (task.metadata as Record<string, unknown>)
      : {};
  const withAutoClose = metadataWithAutoClose(
    metadata,
    task.auto_close_on_due,
  );
  return {
    ...withAutoClose,
    [CACHED_TAGS_KEY]: Array.isArray(task.tags) ? task.tags : [],
  };
}

function cachedTaskTags(metadata: unknown): ApiTask["tags"] {
  if (!metadata || typeof metadata !== "object" || Array.isArray(metadata)) {
    return [];
  }
  const tags = (metadata as Record<string, unknown>)[CACHED_TAGS_KEY];
  if (!Array.isArray(tags)) return [];
  return tags.filter(
    (tag): tag is NonNullable<ApiTask["tags"]>[number] =>
      Boolean(
        tag &&
          typeof tag === "object" &&
          typeof (tag as Record<string, unknown>).id === "string" &&
          typeof (tag as Record<string, unknown>).name === "string",
      ),
  );
}

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

/**
 * Only a transport failure or a terminal 401 is safe to replay.  HTTP
 * validation/permission/conflict responses and client-side timeouts have an
 * observable (or ambiguous) server outcome and must not be disguised as an
 * offline queue.
 */
function shouldQueueTaskMutation(error: unknown): boolean {
  if (isApiTimeoutError(error)) return false;
  if (isApiHttpError(error)) return error.status === 401;
  return isApiConnectionError(error);
}

function objectPayload(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function authScopeForToken(token: string | null): string | null | undefined {
  if (!token) return null;
  // A few lightweight repository tests mock only getToken.  Production auth
  // always exports getTokenAuthScope; falling back to enqueueOutbox's resolver
  // keeps those doubles compatible without weakening the real scope contract.
  return typeof getTokenAuthScope === "function"
    ? getTokenAuthScope(token)
    : undefined;
}

function authScopeOption(
  authScope: string | null | undefined,
): { authScope?: string | null } {
  return authScope === undefined ? {} : { authScope };
}

/** Keep offline create replay payload independent from local display metadata. */
function buildTaskCreatePayload(
  data: Record<string, unknown>,
): Record<string, unknown> {
  const tagIds = Array.isArray(data.tag_ids)
    ? data.tag_ids.filter((value): value is string => typeof value === "string")
    : [];
  const assigneeIds = Array.isArray(data.assignee_ids)
    ? data.assignee_ids.filter(
        (value): value is string => typeof value === "string",
      )
    : [];
  return {
    project_id: String(data.project_id ?? ""),
    title: String(data.title ?? ""),
    description: (data.description as string | null) ?? null,
    status: normalizeTaskStatus(data.status ?? "open"),
    priority: String(data.priority ?? "normal"),
    start_at: (data.start_at as string | null) ?? null,
    end_at: (data.end_at as string | null) ?? null,
    all_day: Boolean(data.all_day),
    auto_close_on_due: data.auto_close_on_due === true,
    reminder_offsets: Array.isArray(data.reminder_offsets)
      ? data.reminder_offsets
      : [],
    notifications_enabled: data.notifications_enabled !== false,
    estimated_hours:
      typeof data.estimated_hours === "number" ? data.estimated_hours : null,
    parent_task_id: (data.parent_task_id as string | null) ?? null,
    tag_ids: tagIds,
    assignee_ids: assigneeIds,
    recurrence_rrule:
      typeof data.recurrence_rrule === "string"
        ? data.recurrence_rrule
        : null,
    recurrence_timezone:
      typeof data.recurrence_timezone === "string"
        ? data.recurrence_timezone
        : "Asia/Tokyo",
    metadata: objectPayload(data.metadata),
    sort_order:
      typeof data.sort_order === "number" ? data.sort_order : null,
    source: String(data.source ?? "mobile"),
  };
}

function toApiShape(
  row: DbTask,
  projectName?: string | null,
  projectColor?: string | null,
): ApiTask {
  const metadata = (row.taskMetadata as Record<string, unknown> | null) ?? {};
  const autoCloseOnDue = autoCloseOnDueFromRow(
    row as DbTaskWithAutoClose,
    metadata,
  );
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
    auto_close_on_due: autoCloseOnDue,
    notifications_enabled: row.notificationsEnabled ?? true,
    source: row.source ?? "local",
    created_by: row.createdBy ?? null,
    completed_at: row.completedAt ?? null,
    archived_at: row.archivedAt ?? null,
    estimated_hours: row.estimatedHours ?? null,
    parent_task_id: row.parentTaskId ?? null,
    created_at: row.createdAt ?? "",
    updated_at: row.updatedAt ?? "",
    deleted_at: row.deletedAt ?? null,
    metadata,
    assignees: [],
    recurrence_rule: null,
    tags: cachedTaskTags(metadata),
    sort_order: row.sortOrder ?? undefined,
  } as unknown as ApiTask;
}

export async function applyRemoteTasks(list: ApiTask[]): Promise<void> {
  if (!list.length) return;
  const db = getDb();
  const now = nowServerNaiveIso();
  // Some push acknowledgements and rolling API versions include deleted_at in
  // the change payload itself.  Handle those as tombstones first; otherwise a
  // subsequent active upsert would clear the deletion marker.
  const tombstones: Array<{ id: string; deleted_at?: string | null }> = [];
  const activeTasks: ApiTask[] = [];
  for (const task of list) {
    const deletedAt = (task as unknown as Record<string, unknown>).deleted_at;
    if (typeof deletedAt === "string" && deletedAt.trim()) {
      tombstones.push({ id: task.id, deleted_at: deletedAt });
    } else {
      activeTasks.push(task);
    }
  }
  if (tombstones.length) await applyTaskTombstones(tombstones);
  if (!activeTasks.length) return;

  const ledger = await loadTaskTombstoneLedger();
  let ledgerChanged = false;
  for (const t of activeTasks) {
    const anyT = t as unknown as Record<string, unknown>;
    const existing = await readLocalTaskRow(db, t.id);
    const hasIncomingVersion =
      typeof anyT.updated_at === "string" && anyT.updated_at.trim().length > 0;
    const incomingUpdatedAt: string =
      hasIncomingVersion
        ? String(anyT.updated_at)
        : now;
    const hasExplicitActiveMarker =
      Object.prototype.hasOwnProperty.call(anyT, "deleted_at") &&
      anyT.deleted_at == null;
    const knownTombstoneAt =
      ledger.entries.get(t.id) ?? existing?.deletedAt ?? null;
    const isCrossDeviceRestore =
      hasExplicitActiveMarker &&
      hasIncomingVersion &&
      knownTombstoneAt != null &&
      compareTaskTimestamps(incomingUpdatedAt, knownTombstoneAt) > 0;
    // A local tombstone is terminal until an explicit restore operation.  In
    // particular, stale active payloads from a full-list refresh must never
    // resurrect a deleted row. A canonical server row with an explicit null
    // deleted_at and a newer updated_at is the cross-device restore signal.
    if (
      (existing?.deletedAt != null || ledger.entries.has(t.id)) &&
      !isCrossDeviceRestore
    ) {
      continue;
    }
    if (isCrossDeviceRestore && ledger.entries.delete(t.id)) {
      ledgerChanged = true;
    }
    if (
      existing?.updatedAt &&
      compareTaskTimestamps(incomingUpdatedAt, existing.updatedAt) <= 0
    ) {
      // Last-write-wins for active rows.  Equality is intentionally ignored:
      // the local value is already at least as fresh as this payload.
      continue;
    }
    const taskMetadata = taskMetadataWithCachedTags(anyT);
    const insertValues: Record<string, unknown> = {
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
      taskMetadata,
      sortOrder: (anyT.sort_order as number | null) ?? null,
      createdAt: (anyT.created_at as string | null) ?? now,
      updatedAt: (anyT.updated_at as string | null) ?? now,
      deletedAt: null,
    };
    const autoClose = anyT.auto_close_on_due;
    if (autoCloseColumn() && typeof autoClose === "boolean") {
      insertValues.autoCloseOnDue = autoClose;
    }
    const conflictSet: Record<string, unknown> = {
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
      taskMetadata,
      sortOrder: (anyT.sort_order as number | null) ?? null,
      // createdAt もサーバー値で上書きする。端末生成時の暫定値を残すと、
      // 表記の違いで一覧の並び順がローカル読み出しとサーバー応答でずれる。
      createdAt: (anyT.created_at as string | null) ?? now,
      updatedAt: (anyT.updated_at as string | null) ?? now,
      deletedAt: null,
    };
    if (autoCloseColumn() && typeof autoClose === "boolean") {
      conflictSet.autoCloseOnDue = autoClose;
    }
    await db
      .insert(schema.tasks)
      .values(insertValues as never)
      .onConflictDoUpdate({
        target: schema.tasks.id,
        set: conflictSet as never,
      });
  }
  if (ledgerChanged) await persistTaskTombstoneLedger(ledger);
  void rescheduleLocalTaskNotificationsFromCache();
}

/**
 * Compute task rows omitted from a canonical task list.
 *
 * The server list is already ACL-filtered.  Reconciliation is therefore
 * limited to the requested project/space scope and never treats unrelated
 * local-only tasks as revoked.
 */
export function missingRemoteTaskIds(
  existing: Array<{ id: string; projectId: string }>,
  canonical: ApiTask[],
  scope: { projectId?: string | null; projectIds?: Iterable<string> } = {},
): string[] {
  const visibleIds = new Set(canonical.map((task) => task.id));
  const scopedProjectIds = scope.projectIds
    ? new Set(scope.projectIds)
    : null;
  return existing
    .filter((task) => {
      if (scope.projectId) return task.projectId === scope.projectId;
      if (scopedProjectIds) return scopedProjectIds.has(task.projectId);
      return true;
    })
    .filter((task) => !visibleIds.has(task.id))
    .map((task) => task.id);
}

async function reconcileCanonicalTasks(
  list: ApiTask[],
  scope: { projectId?: string | null; spaceId?: string | null } = {},
): Promise<void> {
  const db = getDb();
  const base = db
    .select({
      id: schema.tasks.id,
      projectId: schema.tasks.projectId,
      ownerId: schema.projects.ownerId,
      spaceId: schema.projects.spaceId,
    })
    .from(schema.tasks)
    .leftJoin(schema.projects, eq(schema.tasks.projectId, schema.projects.id));
  const rows = await base.where(
    and(
      isNull(schema.tasks.deletedAt),
      isNull(schema.tasks.archivedAt),
      ...(scope.projectId
        ? [eq(schema.tasks.projectId, scope.projectId)]
        : scope.spaceId
          ? [eq(schema.projects.spaceId, scope.spaceId)]
          : []),
    ),
  );
  // A full-scope refresh must not hide unsynced local-only projects.  The
  // explicit project/space refreshes are already constrained by their server
  // request and can reconcile every row in that scope.
  const existing = rows.filter(
    (row) => scope.projectId || scope.spaceId || row.ownerId != null,
  );
  const scopeProjectIds = scope.spaceId
    ? existing.map((row) => row.projectId)
    : undefined;
  const staleIds = missingRemoteTaskIds(existing, list, {
    projectId: scope.projectId,
    projectIds: scopeProjectIds,
  });
  if (!staleIds.length) return;
  const deletedAt = nowServerNaiveIso();
  await applyTaskTombstones(
    staleIds.map((id) => ({ id, deleted_at: deletedAt })),
  );
}

export async function applyTaskTombstones(
  tombstones: Array<{ id: string; deleted_at?: string | null }>,
): Promise<void> {
  if (!tombstones.length) return;
  const db = getDb();
  const ledger = await loadTaskTombstoneLedger();
  let changed = false;
  for (const item of tombstones) {
    if (!item.id) continue;
    const deletedAt =
      typeof item.deleted_at === "string" && item.deleted_at.trim()
        ? item.deleted_at
        : nowServerNaiveIso();
    const existing = await readLocalTaskRow(db, item.id);
    const previousVersion = existing?.deletedAt ?? existing?.updatedAt ?? null;
    const ledgerVersion = ledger.entries.get(item.id) ?? null;
    const latestKnown =
      previousVersion && ledgerVersion
        ? compareTaskTimestamps(previousVersion, ledgerVersion) >= 0
          ? previousVersion
          : ledgerVersion
        : previousVersion ?? ledgerVersion;
    if (
      latestKnown &&
      compareTaskTimestamps(deletedAt, latestKnown) < 0
    ) {
      continue;
    }
    if (existing?.deletedAt === deletedAt && ledgerVersion === deletedAt) {
      continue;
    }
    if (existing) {
      await db
        .update(schema.tasks)
        .set({ deletedAt, updatedAt: deletedAt })
        .where(eq(schema.tasks.id, item.id));
    }
    changed = mergeTaskLedgerTimestamp(ledger.entries, item.id, deletedAt) || changed;
  }
  if (changed) await persistTaskTombstoneLedger(ledger);
  if (changed || tombstones.some((item) => item.id)) {
    void rescheduleLocalTaskNotificationsFromCache();
  }
}

/**
 * Clear tombstones only as part of an explicit server-authorized restore.
 * Active pull payloads intentionally cannot call this path.
 */
export async function clearTaskTombstoneLedger(
  taskIds: Iterable<string>,
): Promise<void> {
  const ledger = await loadTaskTombstoneLedger();
  let changed = false;
  for (const taskId of taskIds) {
    if (ledger.entries.delete(taskId)) changed = true;
  }
  if (changed) await persistTaskTombstoneLedger(ledger);
}

/** Return whether a task is locally tombstoned, including a missing-row ledger entry. */
export async function isTaskTombstoned(taskId: string): Promise<boolean> {
  const db = getDb();
  const existing = await readLocalTaskRow(db, taskId);
  if (existing?.deletedAt != null) return true;
  const ledger = await loadTaskTombstoneLedger();
  return ledger.entries.has(taskId);
}

/** Apply the canonical result of `/api/tasks/{id}/restore`. */
export async function applyTaskRestore(
  restored: Partial<ApiTask> & {
    id: string;
    task_id?: string;
    task_ids?: string[];
    restored_at?: string | null;
    updated_at?: string | null;
  },
): Promise<void> {
  if (!restored.id) return;
  const ids = new Set<string>([
    restored.id,
    ...(typeof restored.task_id === "string" ? [restored.task_id] : []),
    ...(Array.isArray(restored.task_ids)
      ? restored.task_ids.filter((id): id is string => typeof id === "string")
      : []),
  ]);
  await clearTaskTombstoneLedger(ids);

  const db = getDb();
  const restoredAt =
    (typeof restored.updated_at === "string" && restored.updated_at) ||
    (typeof restored.restored_at === "string" && restored.restored_at) ||
    nowServerNaiveIso();
  for (const taskId of ids) {
    const existing = await readLocalTaskRow(db, taskId);
    if (!existing) continue;
    await db
      .update(schema.tasks)
      .set({ deletedAt: null, updatedAt: restoredAt })
      .where(eq(schema.tasks.id, taskId));
  }

  // The restore endpoint returns a compact batch acknowledgement.  If a
  // full task shape is supplied (for example after a follow-up GET), apply it
  // after clearing the ledger so the active upsert is allowed to insert rows.
  const candidate = restored as Record<string, unknown>;
  if (
    typeof candidate.title === "string" &&
    typeof candidate.project_id === "string"
  ) {
    await applyRemoteTasks([
      {
        ...(candidate as unknown as ApiTask),
        deleted_at: null,
        updated_at:
          (candidate.updated_at as string | null | undefined) ?? restoredAt,
      },
    ]);
  }
  void rescheduleLocalTaskNotificationsFromCache();
}

function buildLocalTask(
  data: Record<string, unknown>,
  id = randomId(),
): ApiTask {
  // サーバーと同じTZ指定なしUTC表記にする（表記が混ざると並び順が揺れる）。
  const now = nowServerNaiveIso();
  const metadata =
    (data.metadata as Record<string, unknown> | undefined) ?? {};
  const taskMetadata = metadataWithAutoClose(
    metadata,
    data.auto_close_on_due,
  );
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
    auto_close_on_due: data.auto_close_on_due === true,
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
    metadata: taskMetadata,
    assignees: [],
    tags: cachedTaskTags(taskMetadata),
    sort_order:
      typeof data.sort_order === "number" ? data.sort_order : undefined,
  };
}

async function nextTopSortOrder(): Promise<number> {
  // Server contract: top-level tasks share the readable ALL scope order.
  const local = await tasksRepo.listLocal(null);
  const orders = local
    .filter((task) => !task.parent_task_id)
    .map((task) => task.sort_order)
    .filter((value): value is number => typeof value === "number");
  return orders.length > 0 ? Math.min(...orders) - 1 : 0;
}

function localUpdatePatch(
  data: Record<string, unknown>,
  currentMetadata?: unknown,
) {
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
  if ("auto_close_on_due" in data) {
    const enabled = Boolean(data.auto_close_on_due);
    const metadata =
      currentMetadata &&
      typeof currentMetadata === "object" &&
      !Array.isArray(currentMetadata)
        ? (currentMetadata as Record<string, unknown>)
        : {};
    patch.taskMetadata = metadataWithAutoClose(metadata, enabled);
    if (autoCloseColumn()) {
      (patch as Record<string, unknown>).autoCloseOnDue = enabled;
    }
  }
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
  if ("sort_order" in data) {
    patch.sortOrder =
      typeof data.sort_order === "number" ? data.sort_order : null;
  }
  if ("metadata" in data) {
    const nextMetadata =
      data.metadata &&
      typeof data.metadata === "object" &&
      !Array.isArray(data.metadata)
        ? (data.metadata as Record<string, unknown>)
        : {};
    patch.taskMetadata =
      "auto_close_on_due" in data
        ? metadataWithAutoClose(nextMetadata, data.auto_close_on_due)
        : (data.metadata as unknown);
  }
  return patch;
}

// ---------- task_detail_cache（詳細スナップショットの永続キャッシュ） ----------
// SQLite 本体行に存在しない集約フィールド（comments / subtasks / assignees /
// 添付一覧）を最終同期時点で保持し、オフライン・取得失敗時に補完する。
// filer_dir_cache と同様、SQLite 実装を注入するがテストではメモリ double へ
// 差し替えられるよう抽象化する。
type SqliteLike = {
  runSync: (sql: string, params?: unknown[]) => unknown;
  getFirstSync: (sql: string, params?: unknown[]) => unknown;
  execSync: (sql: string) => unknown;
};

type TaskDetailCacheRow = {
  payload_json?: string | null;
  cached_at?: string | null;
};

const TASK_DETAIL_CACHE_LIMIT = 500;

export type TaskDetailSnapshotStore = {
  read(cacheKey: string): { payload: unknown; cachedAt: string } | undefined;
  write(cacheKey: string, payload: unknown): void;
  clearAll(): void;
};

export function createSqliteTaskDetailCache(deps?: {
  getDb?: () => SqliteLike;
  ensure?: () => void;
}): TaskDetailSnapshotStore {
  const getDbFn =
    deps?.getDb ??
    (() => {
       
      const { getSqlite } = require("../db/client") as {
        getSqlite: () => SqliteLike;
      };
      return getSqlite();
    });
  const ensure =
    deps?.ensure ??
    (() => {
       
      const { ensureSchema } = require("../db/migrate") as {
        ensureSchema: () => void;
      };
      ensureSchema();
    });

  let ensured = false;
  const prepare = (): SqliteLike | null => {
    try {
      const db = getDbFn();
      if (!ensured) {
        ensure();
        ensured = true;
      }
      return db;
    } catch {
      return null;
    }
  };

  return {
    read(cacheKey) {
      const db = prepare();
      if (!db) return undefined;
      try {
        const row = db.getFirstSync(
          `SELECT payload_json, cached_at FROM task_detail_cache WHERE cache_key = ?;`,
          [cacheKey],
        ) as TaskDetailCacheRow | null | undefined;
        if (!row || row.payload_json == null) return undefined;
        return {
          payload: JSON.parse(row.payload_json),
          cachedAt: row.cached_at ?? "",
        };
      } catch {
        return undefined;
      }
    },

    write(cacheKey, payload) {
      const db = prepare();
      if (!db) return;
      try {
        const cachedAt = new Date().toISOString();
        db.runSync(
          `INSERT INTO task_detail_cache (cache_key, payload_json, cached_at)
           VALUES (?, ?, ?)
           ON CONFLICT(cache_key) DO UPDATE SET
             payload_json = excluded.payload_json,
             cached_at = excluded.cached_at;`,
          [cacheKey, JSON.stringify(payload), cachedAt],
        );
        // 上限超過分を cached_at 昇順（古い順）に削除する。
        const countRow = db.getFirstSync(
          `SELECT COUNT(*) AS n FROM task_detail_cache;`,
        ) as { n?: number } | undefined;
        const total = countRow?.n ?? 0;
        if (total > TASK_DETAIL_CACHE_LIMIT) {
          db.runSync(
            `DELETE FROM task_detail_cache WHERE cache_key IN (
               SELECT cache_key FROM task_detail_cache
               ORDER BY cached_at ASC LIMIT ?
             );`,
            [total - TASK_DETAIL_CACHE_LIMIT],
          );
        }
      } catch {
        // 永続化はベストエフォート。失敗しても本体動作は継続する。
      }
    },

    clearAll() {
      const db = prepare();
      if (!db) return;
      try {
        db.execSync(`DELETE FROM task_detail_cache;`);
      } catch {
        // no-op
      }
    },
  };
}

const taskDetailSnapshotStore = createSqliteTaskDetailCache();

export function taskSnapshotKey(taskId: string): string {
  return `task:${taskId}`;
}

export function attachmentsSnapshotKey(taskId: string): string {
  return `attachments:${taskId}`;
}

export function writeTaskSnapshot(task: ApiTask): void {
  if (!task?.id) return;
  taskDetailSnapshotStore.write(taskSnapshotKey(task.id), task);
}

export function readTaskSnapshot(
  taskId: string,
): { task: ApiTask; cachedAt: string } | undefined {
  const hit = taskDetailSnapshotStore.read(taskSnapshotKey(taskId));
  if (!hit) return undefined;
  return { task: hit.payload as ApiTask, cachedAt: hit.cachedAt };
}

export function writeAttachmentsSnapshot(
  taskId: string,
  attachments: TaskAttachment[],
): void {
  taskDetailSnapshotStore.write(attachmentsSnapshotKey(taskId), attachments);
}

export function readAttachmentsSnapshot(
  taskId: string,
): { attachments: TaskAttachment[]; cachedAt: string } | undefined {
  const hit = taskDetailSnapshotStore.read(attachmentsSnapshotKey(taskId));
  if (!hit) return undefined;
  return {
    attachments: (hit.payload as TaskAttachment[]) ?? [],
    cachedAt: hit.cachedAt,
  };
}

/**
 * オフライン・取得失敗時、SQLite 本体行（local）へスナップショットの集約
 * フィールドだけを補完する。本体フィールドは local を正とし、鮮度逆転を避ける。
 * comments / subtasks / assignees は SQLite に持たないため snapshot 側で補う。
 */
export function mergeTaskSnapshotAggregates(
  local: ApiTask,
  snapshot: ApiTask | undefined,
): ApiTask {
  if (!snapshot) return local;
  return {
    ...local,
    comments: local.comments ?? snapshot.comments,
    subtasks: local.subtasks ?? snapshot.subtasks,
    assignees:
      local.assignees && local.assignees.length > 0
        ? local.assignees
        : (snapshot.assignees ?? local.assignees),
  };
}

export function mergeTaskUpdateAggregates(
  local: ApiTask,
  snapshot: ApiTask | undefined,
  data: Record<string, unknown>,
): ApiTask {
  const merged = mergeTaskSnapshotAggregates(local, snapshot);
  if (!Array.isArray(data.assignee_ids)) return merged;
  const previousByUserId = new Map(
    (snapshot?.assignees ?? []).map((assignee) => [
      assignee.user_id,
      assignee,
    ]),
  );
  const assignees = data.assignee_ids
    .filter((value): value is string => typeof value === "string")
    .filter((value, index, values) => values.indexOf(value) === index)
    .map<TaskAssignee>((userId, index) => ({
      ...(previousByUserId.get(userId) ?? {}),
      id: previousByUserId.get(userId)?.id ?? `local-${local.id}-${userId}`,
      task_id: local.id,
      user_id: userId,
      is_primary: index === 0,
    }));
  return { ...merged, assignees };
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
    // archived はサーバー一覧（Task.archived_at.is_(None)）にも出ない。
    // ローカルだけ含めると、供給元がローカルかサーバーかで件数と並びがずれる。
    const rows = projectId
      ? await base
          .where(
            and(
              eq(schema.tasks.projectId, projectId),
              isNull(schema.tasks.deletedAt),
              isNull(schema.tasks.archivedAt),
              isNull(schema.projects.deletedAt),
              isNotNull(schema.projects.id),
            ),
          )
          .orderBy(desc(schema.tasks.updatedAt))
      : await base
          .where(
            and(
              isNull(schema.tasks.deletedAt),
              isNull(schema.tasks.archivedAt),
              isNull(schema.projects.deletedAt),
              isNotNull(schema.projects.id),
            ),
          )
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
        const projectOrder = String(a.project_id ?? "").localeCompare(
          String(b.project_id ?? ""),
        );
        if (projectOrder !== 0) return projectOrder;
        const aParent = a.parent_task_id;
        const bParent = b.parent_task_id;
        if (aParent == null && bParent != null) return -1;
        if (aParent != null && bParent == null) return 1;
        const parentOrder = String(aParent ?? "").localeCompare(
          String(bParent ?? ""),
        );
        if (parentOrder !== 0) return parentOrder;
        const createdOrder = String(a.created_at ?? "").localeCompare(
          String(b.created_at ?? ""),
        );
        return createdOrder !== 0 ? createdOrder : a.id.localeCompare(b.id);
      });
  },

  /**
   * ローカル読み取りを正とし、フル取得は空初回・明示refresh・60秒throttleのみ。
   * 鮮度はタブ側の runSync（デルタ同期）が担保する。
   */
  async list(
    projectId?: string | null,
    options?: { force?: boolean },
  ): Promise<ApiTask[]> {
    const local = await this.listLocal(projectId);
    if (await canUseServer()) {
      const key = `project:${projectId ?? "all"}`;
      if (
        shouldRunFullFetch(
          lastFullFetchAt.get(key),
          Date.now(),
          local.length === 0,
          options?.force ?? false,
        )
      ) {
        try {
          await this.refresh(projectId);
          lastFullFetchAt.set(key, Date.now());
          // サーバー応答の配列順ではなく、必ずローカルの正規化済み順を返す。
          // 供給元によって並びが変わると、リロードのたびに表示順が入れ替わる。
          return this.listLocal(projectId);
        } catch (error) {
          if (local.length === 0) {
            throw error;
          }
          return local;
        }
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
    options?: { force?: boolean },
  ): Promise<ApiTask[]> {
    if (scope.project_id) return this.list(scope.project_id, options);
    const local = scope.space_id
      ? await this.listLocalBySpace(scope.space_id)
      : await this.listLocal(null);
    if (await canUseServer()) {
      const key = `scope:${scope.space_id ?? "all"}`;
      if (
        shouldRunFullFetch(
          lastFullFetchAt.get(key),
          Date.now(),
          local.length === 0,
          options?.force ?? false,
        )
      ) {
        try {
          const list = await taskApi.listTasksByScope({
            ...(scope.space_id ? { space_id: scope.space_id } : {}),
          });
          await applyRemoteTasks(list);
          await reconcileCanonicalTasks(list, { spaceId: scope.space_id });
          lastFullFetchAt.set(key, Date.now());
          // list（サーバー応答順）ではなくローカルの正規化済み順を返す。
          return scope.space_id
            ? this.listLocalBySpace(scope.space_id)
            : this.listLocal(null);
        } catch (error) {
          if (local.length === 0) {
            throw error;
          }
          return local;
        }
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
    await reconcileCanonicalTasks(list, { projectId });
    return list;
  },

  async getLocal(taskId: string): Promise<ApiTask | null> {
    const db = getDb();
    const rows = await db
      .select({
        task: schema.tasks,
        projectName: schema.projects.name,
        projectMetadata: schema.projects.projectMetadata,
      })
      .from(schema.tasks)
      .leftJoin(schema.projects, eq(schema.tasks.projectId, schema.projects.id))
      .where(
        and(
          eq(schema.tasks.id, taskId),
          isNull(schema.tasks.deletedAt),
          isNull(schema.projects.deletedAt),
          isNotNull(schema.projects.id),
        ),
      );
    return rows[0]
      ? toApiShape(
          rows[0].task,
          rows[0].projectName,
          extractProjectColor(rows[0].projectMetadata),
        )
      : null;
  },

  async get(taskId: string): Promise<ApiTask | null> {
    const local = await this.getLocal(taskId);
    if (await canUseServer()) {
      try {
        const remote = await taskApi.getTask(taskId);
        await applyRemoteTasks([remote]);
        // 詳細レスポンス全体をスナップショットへ保存し、オフライン補完に使う。
        writeTaskSnapshot(remote);
        return remote;
      } catch (error) {
        if (!local) throw error;
      }
    }
    // オフライン・取得失敗時は SQLite 行へスナップショットの集約フィールドを補完。
    if (!local) return null;
    const snapshot = readTaskSnapshot(taskId);
    return mergeTaskSnapshotAggregates(local, snapshot?.task);
  },

  async create(data: Record<string, unknown>): Promise<ApiTask> {
    const token = await getToken();
    const hasToken = Boolean(token);
    const authScope = authScopeForToken(token);
    const sortOrder =
      typeof data.sort_order === "number"
        ? data.sort_order
        : await nextTopSortOrder();
    const createData: Record<string, unknown> = {
      ...data,
      sort_order: sortOrder,
    };
    if (await canUseServer()) {
      try {
        const created = await taskApi.createTask(createData);
        await applyRemoteTasks([created]);
        return created;
      } catch (error) {
        if (!shouldQueueTaskMutation(error)) throw error;
        // Transport/401 failures fall back to local-first below.  The API
        // client emits the reauth notification for a terminal 401.
      }
    }

    const existingMetadata = objectPayload(createData.metadata);
    const local = buildLocalTask({
      ...createData,
      metadata: {
        ...existingMetadata,
        mobile_sync_status: hasToken ? "pending" : "local_only",
      },
    });
    await applyRemoteTasks([local]);
    if (hasToken) {
      await enqueueOutbox({
        table: "tasks",
        action: "create",
        entityId: local.id,
        ...authScopeOption(authScope),
        payload: buildTaskCreatePayload(createData),
      });
    }
    return local;
  },

  async update(
    taskId: string,
    data: Record<string, unknown>,
  ): Promise<ApiTask> {
    const token = await getToken();
    const hasToken = Boolean(token);
    const authScope = authScopeForToken(token);
    let shouldQueue = false;
    if (await canUseServer()) {
      try {
        const updated = await taskApi.updateTask(taskId, data);
        await applyRemoteTasks([updated]);
        writeTaskSnapshot(updated);
        return updated;
      } catch (error) {
        if (
          error instanceof TaskCompletionCancelledError ||
          (isApiHttpError(error) && error.status === 409)
        ) {
          throw error;
        }
        if (!shouldQueueTaskMutation(error)) throw error;
        shouldQueue = hasToken;
      }
    } else {
      shouldQueue = hasToken;
    }

    const db = getDb();
    const before = (
      await db.select().from(schema.tasks).where(eq(schema.tasks.id, taskId))
    )[0];
    const tombstoneLedger = await loadTaskTombstoneLedger();
    if (before?.deletedAt != null || tombstoneLedger.entries.has(taskId)) {
      throw new Error("削除済みタスクは更新できません。復元操作が必要です");
    }
    const requestedProjectId =
      typeof data.project_id === "string" ? data.project_id : null;
    const projectWillChange =
      requestedProjectId !== null &&
      before?.projectId !== undefined &&
      requestedProjectId !== String(before.projectId);
    if (projectWillChange && before) {
      const children = await db
        .select({ id: schema.tasks.id })
        .from(schema.tasks)
        .where(
          and(
            eq(schema.tasks.parentTaskId, taskId),
            isNull(schema.tasks.deletedAt),
          ),
        );
      if (children.length > 0) {
        throw new Error(
          "子タスクがある親タスクは別のプロジェクトへ移動できません",
        );
      }
    }
    const localPatch = localUpdatePatch(data, before?.taskMetadata);
    if (projectWillChange && !("parent_task_id" in data)) {
      // Keep offline optimistic state aligned with the server move invariant:
      // a task moved without an explicit destination parent becomes top-level.
      localPatch.parentTaskId = null;
    }
    await db
      .update(schema.tasks)
      .set(localPatch)
      .where(eq(schema.tasks.id, taskId));
    if (shouldQueue) {
      await enqueueOutbox({
        table: "tasks",
        action: "update",
        entityId: taskId,
        ...authScopeOption(authScope),
        payload: data,
        baseUpdatedAt: before?.updatedAt ?? null,
      });
    }
    void rescheduleLocalTaskNotificationsFromCache();
    const after = (
      await db.select().from(schema.tasks).where(eq(schema.tasks.id, taskId))
    )[0];
    const local = after
      ? toApiShape(after)
      : buildLocalTask({ ...data, title: data.title ?? "" }, taskId);
    const previousSnapshot = readTaskSnapshot(taskId)?.task;
    const merged = mergeTaskUpdateAggregates(local, previousSnapshot, data);
    writeTaskSnapshot(merged);
    return merged;
  },

  /** Restore a server deletion batch, or queue the explicit restore offline. */
  async restore(
    taskId: string,
    deletionBatchId?: string | null,
  ): Promise<ApiTask> {
    const token = await getToken();
    const hasToken = Boolean(token);
    const authScope = authScopeForToken(token);
    if (await canUseServer()) {
      try {
        const response = await taskApi.restoreTask(taskId, deletionBatchId);
        await applyTaskRestore(response);
        // The restore response is intentionally compact (it represents the
        // whole deletion batch).  Rehydrate the root task before returning it.
        try {
          const restored = await taskApi.getTask(taskId);
          await applyTaskRestore(restored);
          return restored;
        } catch {
          const local = await this.getLocal(taskId);
          if (local) return local;
        }
        const local = await this.getLocal(taskId);
        if (local) return local;
        throw new Error("復元されたタスクを取得できませんでした");
      } catch (error) {
        if (!shouldQueueTaskMutation(error)) throw error;
      }
    }

    if (!hasToken) {
      throw new OfflineWriteError("タスクの復元にはサーバーログインが必要です");
    }
    const db = getDb();
    const before = (
      await db.select().from(schema.tasks).where(eq(schema.tasks.id, taskId))
    )[0];
    if (!before) {
      throw new Error("復元対象のローカルタスクが見つかりません");
    }
    await enqueueOutbox({
      table: "tasks",
      action: "restore",
      entityId: taskId,
      ...authScopeOption(authScope),
      payload: deletionBatchId
        ? { deletion_batch_id: deletionBatchId }
        : {},
      baseUpdatedAt: before.updatedAt ?? null,
    });
    const rows = await db
      .select()
      .from(schema.tasks)
      .where(eq(schema.tasks.id, taskId));
    const local = rows[0] ? toApiShape(rows[0] as DbTask) : null;
    if (!local) throw new Error("復元されたタスクを取得できませんでした");
    // Keep the local tombstone until the explicit restore is acknowledged by
    // the server; a rejected/expired outbox operation must not look live.
    return local;
  },

  async reorder(projectId: string | null, taskIds: string[]): Promise<void> {
    const uniqueTaskIds = taskIds.filter(
      (taskId, index) => taskIds.indexOf(taskId) === index,
    );
    if (uniqueTaskIds.length === 0) return;

    const db = getDb();
    const token = await getToken();
    const hasToken = Boolean(token);
    const authScope = authScopeForToken(token);
    let shouldQueue = false;
    if (await canUseServer()) {
      try {
        if (projectId) {
          await taskApi.reorderTasks(projectId, uniqueTaskIds);
        } else {
          await taskApi.reorderAllTasks(uniqueTaskIds);
        }
        // The endpoint has committed the order.  A refresh failure is
        // ambiguous but must not enqueue a blind duplicate reorder.
        try {
          const refreshed = projectId
            ? await taskApi.listTasks(projectId)
            : await taskApi.listAllTasks();
          await applyRemoteTasks(refreshed);
        } catch {
          // Keep the optimistic local order; the next pull reconciles it.
        }
        return;
      } catch (error) {
        if (!shouldQueueTaskMutation(error)) throw error;
        shouldQueue = hasToken;
      }
    } else {
      shouldQueue = hasToken;
    }

    const optimisticNow = new Date().toISOString();
    for (const [index, taskId] of uniqueTaskIds.entries()) {
      await db
        .update(schema.tasks)
        .set({ sortOrder: index, updatedAt: optimisticNow })
        .where(and(eq(schema.tasks.id, taskId), isNull(schema.tasks.deletedAt)));
    }
    if (shouldQueue) {
      await enqueueOutbox({
        table: "tasks",
        action: "reorder",
        entityId: `reorder:${projectId ?? "all"}`,
        ...authScopeOption(authScope),
        payload: {
          project_id: projectId,
          task_ids: uniqueTaskIds,
        },
      });
    }
  },

  async delete(taskId: string): Promise<void> {
    const db = getDb();
    const token = await getToken();
    const hasToken = Boolean(token);
    const authScope = authScopeForToken(token);
    let shouldQueue = false;
    if (await canUseServer()) {
      try {
        await taskApi.deleteTask(taskId);
        // Keep the local tombstone in sync with the successful remote delete.
        shouldQueue = false;
      } catch (error) {
        if (!shouldQueueTaskMutation(error)) throw error;
        shouldQueue = hasToken;
      }
    } else {
      shouldQueue = hasToken;
    }

    const before = (
      await db.select().from(schema.tasks).where(eq(schema.tasks.id, taskId))
    )[0];
    const now = nowServerNaiveIso();
    await applyTaskTombstones([{ id: taskId, deleted_at: now }]);
    if (shouldQueue) {
      await enqueueOutbox({
        table: "tasks",
        action: "delete",
        entityId: taskId,
        ...authScopeOption(authScope),
        payload: {},
        baseUpdatedAt: before?.updatedAt ?? null,
      });
    }
    void rescheduleLocalTaskNotificationsFromCache();
  },
};
