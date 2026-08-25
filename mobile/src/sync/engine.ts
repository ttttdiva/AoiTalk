import { eq, like } from "drizzle-orm";
import { getDb, schema } from "../db/client";
import {
  docsSqliteAsyncAvailable,
  encodeDocsBoolean,
  encodeDocsJson,
  executeDocsPreparedBatch,
  type DocsSqliteAsyncTransaction,
  withDocsExclusiveTransaction,
} from "../db/docs-sync-async";
import {
  hasPendingOutbox,
  listPendingOutbox,
  markOutboxConflict,
  markOutboxError,
  rebaseOutboxOp,
  recordOutboxServerSnapshot,
  removeOutboxOpIfSnapshot,
  removeOutboxOp,
  randomId,
} from "../repositories/outbox";
import {
  applyRemoteTasks,
  applyTaskRestore,
  applyTaskTombstones,
} from "../repositories/tasks";
import {
  applyProjectTombstones,
  applyRemoteProjects,
} from "../repositories/projects";
import {
  applyOccurrenceTombstones,
  applyRemoteOccurrences,
} from "../repositories/occurrences";
import {
  applyConversationMessageTombstones,
  applyConversationSessionTombstones,
  applyRemoteConversationMessages,
  applyRemoteConversationSessions,
  reconcileConversationSessionsWithServer,
} from "../repositories/conversations";
import {
  applyRemoteTimeEntries,
  applyTimeEntryTombstones,
} from "../repositories/timeEntries";
import {
  applyRecordFieldTombstones,
  applyRecordRowTombstones,
  applyRecordTableTombstones,
  applyRemoteRecordFields,
  applyRemoteRecordRows,
  applyRemoteRecordTables,
} from "../repositories/records";
import {
  applyDocsNodeTombstones,
  applyRemoteDocsEdges,
  applyRemoteDocsFieldValues,
  applyRemoteDocsFields,
  applyRemoteDocsNodeSupertags,
  applyRemoteDocsNodes,
  applyRemoteDocsPlacements,
  applyRemoteDocsSupertagFields,
  applyRemoteDocsSupertags,
  deleteLocalDocsFieldValue,
  deleteLocalDocsNodeSupertag,
  reconcileDocsFieldsWithServer,
  reconcileDocsNodesWithServer,
  reconcileDocsSupertagsWithServer,
  promoteDocsSyncRun,
  quarantineRevokedDocsScope,
} from "../repositories/docs";
import { flushPendingConversations } from "../repositories/conversations";
import { flushPendingClipIngests } from "../repositories/pending-clip-ingest";
import { refreshClipIngestTargetsIfStale } from "../lib/clip-ingest-targets";
import { characterApi } from "../lib/character-api";
import { taskApi } from "../lib/task-api";
import type {
  ConversationMessage,
  ConversationSession,
  DocsEdge,
  DocsField,
  DocsFieldValue,
  DocsNode,
  DocsNodePlacement,
  DocsNodeSupertag,
  DocsSupertag,
  DocsSupertagField,
  Project,
  RecordField,
  RecordRow,
  RecordTable,
  Task,
  TaskOccurrence,
  TimeEntry,
} from "../types/api";
import { useNetworkStore } from "../stores/network";
import { getCachedToken, getToken, getTokenAuthScope } from "../lib/auth";
import { isApiConnectionError, isApiHttpError } from "../lib/api-client";
import {
  enqueueAuthScopeExclusive,
  runAuthScopeTransition,
} from "../lib/auth-scope-queue";
import {
  pullSync,
  pushSync,
  type DocsSyncScope,
  type SyncPullResponse,
  type SyncPushOperation,
  type SyncTable,
} from "./api";

const TABLES: SyncTable[] = [
  "projects",
  "tasks",
  "task_occurrences",
  "time_entries",
  "conversation_sessions",
  "conversation_messages",
  "record_tables",
  "record_fields",
  "record_rows",
];

const DOCS_TABLES: SyncTable[] = [
  "knowledge_nodes",
  "knowledge_supertags",
  "knowledge_node_supertags",
  "knowledge_supertag_fields",
  "knowledge_fields",
  "knowledge_field_values",
  "knowledge_node_placements",
  "knowledge_edges",
];

// Docsはページ単位で同一snapshotを検証するため、Web/PC側の編集がページ間に
// 入るとサーバーが400でcursorを無効化する。短いバックオフを挟んで再取得し、
// 一時的な競合で設定画面の強制同期が失敗しないようにする。
const DOCS_PULL_MAX_ATTEMPTS = 3;
const DOCS_PULL_RETRY_DELAY_MS = 80;
/** Keep each staging write transaction small enough for production pages. */
export const DOCS_STAGING_WRITE_BATCH_SIZE = 256;

export type DocsResyncProgress = {
  phase: "preparing" | "downloading" | "finalizing";
  completed: number;
  total: number | null;
  page: number;
  /** Bounded scale telemetry; UI callers may ignore these optional fields. */
  stagedRowsWritten?: number;
  stagedRowsRead?: number;
  stagedPromotionBatches?: number;
  stagedPromotionMaxBatchSize?: number;
};

type DocsResyncProgressHandler = (progress: DocsResyncProgress) => void;

type PendingOutbox = typeof schema.outbox.$inferSelect;
type SyncExecutionContext = { epoch: number };
type DocsSyncRunRow = typeof schema.docsSyncRuns.$inferSelect;
type DocsSyncAuthoritative = Record<
  string,
  { ids?: string[]; scopeId?: string; digest?: string }
>;
type RunningSyncFlight = {
  epoch: number;
  promise: Promise<void>;
};

class SyncExecutionInterruptedError extends Error {
  constructor() {
    super("同期はアプリがactiveへ復帰するまで中断されました");
    this.name = "SyncExecutionInterruptedError";
  }
}

const completedSync = Promise.resolve();
const runningByAuthScope = new Map<string, RunningSyncFlight>();
let unresolvedAuthFlight: Promise<void> | null = null;
let syncRequestCount = 0;
let syncExecutionCount = 0;
let syncExecutionActive = true;
let syncExecutionEpoch = 0;

export function setSyncExecutionActive(active: boolean): void {
  if (syncExecutionActive === active) return;
  syncExecutionActive = active;
  syncExecutionEpoch += 1;
}

function createSyncExecutionContext(): SyncExecutionContext {
  return { epoch: syncExecutionEpoch };
}

function assertSyncExecutionActive(context: SyncExecutionContext): void {
  if (!syncExecutionActive || context.epoch !== syncExecutionEpoch) {
    throw new SyncExecutionInterruptedError();
  }
}

function enqueueExclusive(operation: () => Promise<void>): Promise<void> {
  return enqueueAuthScopeExclusive(operation);
}

function getSyncStateKey(authScope: string): string {
  return `__global__:${authScope.slice("auth:".length)}`;
}

// v2 は、旧実装が5,000件で打ち切ったまま進めたDocs同期時刻を一度だけ無効化する。
// 一般テーブルの同期時刻とは分離し、Docs全ページ完了後にだけ更新する。
function getDocsScopeSuffix(scopeId?: string): string {
  return scopeId ? `:scope:${scopeId}` : "";
}

/** Canonical Docs scope identity.  A project can share its owner's library,
 * so workspace/library id alone is never a sufficient local key. */
function getDocsScopeKey(scopeId?: string, projectId?: string | null): string {
  const library = scopeId || "personal";
  return `${library}|project:${projectId || ""}`;
}

function getDocsScopeProjectSuffix(projectId?: string | null): string {
  return projectId ? `:project:${projectId}` : "";
}

function getDocsSyncStateKey(
  authScope: string,
  scopeId?: string,
  projectId?: string | null,
): string {
  return `__docs_v2__:${authScope.slice("auth:".length)}${getDocsScopeSuffix(scopeId)}${getDocsScopeProjectSuffix(projectId)}`;
}

// authScope 単位で保持する Docs 権威 digest の syncState.tableName プレフィクス。
function getDocsDigestPrefix(
  authScope: string,
  scopeId?: string,
  projectId?: string | null,
): string {
  return `docs_digest:v2:${authScope.slice("auth:".length)}${getDocsScopeSuffix(scopeId)}${getDocsScopeProjectSuffix(projectId)}:`;
}

function getDocsDigestKey(
  authScope: string,
  table: string,
  scopeId?: string,
  projectId?: string | null,
): string {
  return `${getDocsDigestPrefix(authScope, scopeId, projectId)}${table}`;
}

function getDocsScopeDigestKey(
  authScope: string,
  scopeId?: string,
  projectId?: string | null,
): string {
  return `docs_scope:v2:${authScope.slice("auth:".length)}${getDocsScopeSuffix(scopeId)}${getDocsScopeProjectSuffix(projectId)}`;
}

/** Scope ACL revisions are persisted separately from row/entity digests. */
function getDocsScopeRevisionKey(
  authScope: string,
  scopeId?: string,
  projectId?: string | null,
): string {
  return `docs_scope_revision:v2:${authScope.slice("auth:".length)}${getDocsScopeSuffix(scopeId)}${getDocsScopeProjectSuffix(projectId)}`;
}

// Docs の authoritative_scope_id（現在のワークスペース）を端末側へ保持する。
// listPages はローカル先読みを行うため、旧ワークスペースの残骸を表示しないよう
// 現在の認証スコープと組み合わせて利用する。
function getDocsWorkspaceKey(
  authScope: string,
  scopeId?: string,
  projectId?: string | null,
): string {
  return `docs_workspace:v2:${authScope.slice("auth:".length)}${getDocsScopeSuffix(scopeId)}${getDocsScopeProjectSuffix(projectId)}`;
}

function getDocsScopesKey(authScope: string): string {
  return `docs_scopes:v2:${authScope.slice("auth:".length)}`;
}

function docsStagingAvailable(): boolean {
  return Boolean(schema.docsSyncRuns && schema.docsSyncStaging && docsSqliteAsyncAvailable());
}

function parseJsonRecord<T>(value: unknown, fallback: T): T {
  if (typeof value !== "string") return (value as T) ?? fallback;
  try {
    return (JSON.parse(value) as T) ?? fallback;
  } catch {
    return fallback;
  }
}

type RawDocsSyncRunRow = {
  run_id: string;
  auth_scope: string;
  scope_key: string;
  scope_id: string | null;
  project_id: string | null;
  snapshot_token: string | null;
  scope_revision: string | null;
  scope_digest: string | null;
  server_time: string | null;
  cursor_json: unknown;
  pending_json: unknown;
  digest_json: unknown;
  authoritative_json: unknown;
  scopes_json: unknown;
  force: unknown;
  state: string;
  created_at: string;
  updated_at: string;
};

function deserializeDocsSyncRunRow(row: RawDocsSyncRunRow): DocsSyncRunRow {
  return {
    runId: row.run_id,
    authScope: row.auth_scope,
    scopeKey: row.scope_key,
    scopeId: row.scope_id,
    projectId: row.project_id,
    snapshotToken: row.snapshot_token,
    scopeRevision: row.scope_revision,
    scopeDigest: row.scope_digest,
    serverTime: row.server_time,
    cursorJson: parseJsonRecord<Record<string, string>>(row.cursor_json, {}),
    pendingJson: parseJsonRecord<SyncTable[]>(row.pending_json, []),
    digestJson: parseJsonRecord<Record<string, string>>(row.digest_json, {}),
    authoritativeJson: parseJsonRecord<DocsSyncAuthoritative>(row.authoritative_json, {}),
    scopesJson: row.scopes_json == null
      ? null
      : parseJsonRecord<DocsSyncScope[]>(row.scopes_json, []),
    force: row.force === true || row.force === 1 || row.force === "1",
    state: row.state as DocsSyncRunRow["state"],
    createdAt: row.created_at,
    updatedAt: row.updated_at,
  };
}

async function getDocsSyncRun(
  runId: string,
  authScope: string,
): Promise<DocsSyncRunRow | null> {
  if (!docsStagingAvailable()) return null;
  const row = await withDocsExclusiveTransaction((tx) =>
    tx.getFirstAsync<RawDocsSyncRunRow>(
      "SELECT * FROM docs_sync_runs WHERE run_id = ? AND auth_scope = ?",
      runId,
      authScope,
    ),
  );
  return row ? deserializeDocsSyncRunRow(row) : null;
}

async function getActiveDocsSyncRun(
  authScope: string,
  scopeId?: string,
  projectId?: string | null,
  force?: boolean,
): Promise<DocsSyncRunRow | null> {
  if (!docsStagingAvailable()) return null;
  const rows = await withDocsExclusiveTransaction(async (tx) => {
    const params: unknown[] = [authScope, "downloading", "ready"];
    let sql = `
      SELECT * FROM docs_sync_runs
      WHERE auth_scope = ? AND state IN (?, ?)
    `;
    if (scopeId != null) {
      sql += " AND scope_key = ?";
      params.push(getDocsScopeKey(scopeId, projectId));
    } else if (projectId == null) {
      sql += " AND project_id IS NULL";
    } else {
      sql += " AND project_id = ?";
      params.push(projectId);
    }
    if (force !== undefined) {
      sql += " AND force = ?";
      params.push(encodeDocsBoolean(force));
    }
    sql += " ORDER BY updated_at ASC";
    return tx.getAllAsync<RawDocsSyncRunRow>(sql, ...params);
  });
  const mappedRows = rows.map(deserializeDocsSyncRunRow);
  if (scopeId == null && projectId == null) {
    // A non-project shared library can coexist with the personal root.  The
    // normalized personal run carries its concrete scopeId after page one,
    // so distinguish it by the authoritative owner scope metadata instead of
    // blindly taking the first project_id=NULL run.
    return mappedRows.find((candidate) => {
      const scopes = parseJsonRecord<DocsSyncScope[]>(candidate.scopesJson, []);
      const ownerScopes = scopes.filter(
        (scope) =>
          scope.workspace_id === candidate.scopeId
          && scope.project_id == null,
      );
      // A first-page personal run starts with the historical placeholder key.
      // A run with that key is safe before the server reveals a library id;
      // once it has a concrete id, require matching personal-owner metadata so
      // a normalized shared run cannot be resumed as root.
      if (candidate.scopeKey === getDocsScopeKey(undefined, null)) {
        if (candidate.scopeId == null) return true;
        return ownerScopes.length === 1
          && ownerScopes[0].source === "personal"
          && ownerScopes[0].access === "owner";
      }
      if (!candidate.scopeId || candidate.scopeKey !== getDocsScopeKey(candidate.scopeId, null)) {
        return false;
      }
      // A complete scope set may contain a shared projection for the same
      // library id.  Resume only when that concrete candidate is uniquely
      // identified as the personal owner scope; a broad `some()` would mix a
      // shared run into root discovery after an auth/scope switch.
      return ownerScopes.length === 1
        && ownerScopes[0].source === "personal"
        && ownerScopes[0].access === "owner";
    }) ?? null;
  }
  return mappedRows[0] ?? null;
}

async function createDocsSyncRun(
  authScope: string,
  scopeId: string | undefined,
  projectId: string | null | undefined,
  force: boolean,
): Promise<DocsSyncRunRow | null> {
  if (!docsStagingAvailable()) return null;
  const now = new Date().toISOString();
  const runId = randomId();
  const values = {
    runId,
    authScope,
    scopeKey: getDocsScopeKey(scopeId, projectId),
    scopeId: scopeId ?? null,
    projectId: projectId ?? null,
    snapshotToken: null,
    scopeRevision: null,
    scopeDigest: null,
    serverTime: null,
    cursorJson: {} as Record<string, string>,
    pendingJson: [...DOCS_TABLES],
    digestJson: {} as Record<string, string>,
    authoritativeJson: {} as DocsSyncAuthoritative,
    scopesJson: null,
    force,
    state: "downloading",
    createdAt: now,
    updatedAt: now,
  };
  await withDocsExclusiveTransaction(async (tx) => {
    await tx.runAsync(
      `INSERT INTO docs_sync_runs(
        run_id, auth_scope, scope_key, scope_id, project_id,
        snapshot_token, scope_revision, scope_digest, server_time,
        cursor_json, pending_json, digest_json, authoritative_json, scopes_json,
        force, state, created_at, updated_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      values.runId,
      values.authScope,
      values.scopeKey,
      values.scopeId,
      values.projectId,
      values.snapshotToken,
      values.scopeRevision,
      values.scopeDigest,
      values.serverTime,
      encodeDocsJson(values.cursorJson),
      encodeDocsJson(values.pendingJson),
      encodeDocsJson(values.digestJson),
      encodeDocsJson(values.authoritativeJson),
      encodeDocsJson(values.scopesJson),
      encodeDocsBoolean(values.force),
      values.state,
      values.createdAt,
      values.updatedAt,
    );
  });
  return values as DocsSyncRunRow;
}

async function deleteDocsSyncRun(runId: string, authScope: string): Promise<void> {
  if (!docsStagingAvailable()) return;
  await withDocsExclusiveTransaction(async (tx) => {
    await tx.runAsync(
      "DELETE FROM docs_sync_staging WHERE run_id = ? AND auth_scope = ?",
      runId,
      authScope,
    );
    await tx.runAsync(
      "DELETE FROM docs_sync_runs WHERE run_id = ? AND auth_scope = ?",
      runId,
      authScope,
    );
  });
}

function docsEntityKeyForStage(
  table: SyncTable,
  value: Record<string, unknown>,
): string | null {
  if (table === "knowledge_node_supertags") {
    return value.node_id != null && value.supertag_id != null
      ? `${String(value.node_id)}:${String(value.supertag_id)}`
      : null;
  }
  if (table === "knowledge_supertag_fields") {
    return value.supertag_id != null && value.field_id != null
      ? `${String(value.supertag_id)}:${String(value.field_id)}`
      : null;
  }
  if (table === "knowledge_field_values") {
    return value.node_id != null && value.field_id != null
      ? `${String(value.node_id)}:${String(value.field_id)}`
      : null;
  }
  return value.id == null ? null : String(value.id);
}

async function persistDocsPage(
  run: DocsSyncRunRow,
  authScope: string,
  scopeId: string | undefined,
  projectId: string | null | undefined,
  response: SyncPullResponse,
  pendingTables: SyncTable[],
  nextCursors: Record<string, string>,
): Promise<number> {
  if (!docsStagingAvailable()) return 0;
  const matchingScope = (response.docs_scopes ?? []).find(
    (scope) =>
      (scope.project_id ?? null) === (projectId ?? null)
      && (scopeId == null || scope.workspace_id === scopeId),
  )
    ?? (scopeId == null && projectId == null
      ? (response.docs_scopes ?? []).find(
          (scope) => scope.source === "personal" && scope.project_id == null,
        )
      : undefined);
  const effectiveScopeId = scopeId ?? matchingScope?.workspace_id ?? run.scopeId ?? undefined;
  const effectiveProjectId = projectId ?? matchingScope?.project_id ?? run.projectId ?? null;
  const effectiveScopeKey = getDocsScopeKey(effectiveScopeId, effectiveProjectId);
  const previousDigests = parseJsonRecord<Record<string, string>>(
    run.digestJson,
    {},
  );
  const previousAuthoritative = parseJsonRecord<DocsSyncAuthoritative>(
    run.authoritativeJson,
    {},
  );
  const digestJson = { ...previousDigests };
  const authoritativeJson = { ...previousAuthoritative };
  let snapshotToken = run.snapshotToken;
  let scopeRevision = run.scopeRevision;
  let scopeDigest = run.scopeDigest;
  let serverTime = run.serverTime;
  let scopesJson = run.scopesJson;
  if (response.docs_snapshot_token) snapshotToken = response.docs_snapshot_token;
  if (response.docs_scope_revision) scopeRevision = response.docs_scope_revision;
  if (response.docs_scope_digest) scopeDigest = response.docs_scope_digest;
  if (response.server_time) serverTime = response.server_time;
  if (response.docs_scopes?.length) scopesJson = response.docs_scopes;
  let stagedRowsWritten = 0;
  for (const table of pendingTables) {
    const payload = response.tables[table];
    if (!payload) continue;
    const tableSnapshotToken = payload.docs_snapshot_token;
    const tableScopeRevision = payload.docs_scope_revision;
    if (tableSnapshotToken) snapshotToken = snapshotToken ?? tableSnapshotToken;
    if (tableScopeRevision) scopeRevision = scopeRevision ?? tableScopeRevision;
    if (payload.docs_snapshot_revision) {
      digestJson[table] = payload.docs_snapshot_revision;
    }
    if (payload.authoritative_digest) {
      digestJson[table] = payload.authoritative_digest;
    }
    if (payload.authoritative_ids != null || payload.authoritative_scope_id) {
      authoritativeJson[table] = {
        ids: payload.authoritative_ids,
        scopeId: payload.authoritative_scope_id,
        digest: payload.authoritative_digest,
      };
    }
  }

  const stagePayloads = async (
    table: SyncTable,
    values: unknown[] | undefined,
    isTombstone: boolean,
  ): Promise<void> => {
    if (!values?.length) return;
    for (let offset = 0; offset < values.length; offset += DOCS_STAGING_WRITE_BATCH_SIZE) {
      const batch = values.slice(offset, offset + DOCS_STAGING_WRITE_BATCH_SIZE);
      const rows = batch.flatMap((value) => {
        const object = value as Record<string, unknown>;
        const entityKey = docsEntityKeyForStage(table, object);
        if (!entityKey) return [];
        return [[
          run.runId,
          authScope,
          effectiveScopeKey,
          effectiveScopeId ?? null,
          effectiveProjectId,
          table,
          entityKey,
          JSON.stringify(object),
          encodeDocsBoolean(isTombstone),
        ] as const];
      });
      if (!rows.length) continue;
      await withDocsExclusiveTransaction(async (tx: DocsSqliteAsyncTransaction) => {
        await executeDocsPreparedBatch(
          tx,
          `INSERT INTO docs_sync_staging(
             run_id, auth_scope, scope_key, scope_id, project_id,
             table_name, entity_key, payload_json, is_tombstone
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(run_id, table_name, entity_key) DO UPDATE SET
             payload_json = excluded.payload_json,
             is_tombstone = excluded.is_tombstone`,
          rows,
        );
      });
      stagedRowsWritten += rows.length;
    }
  };

  // Changes are staged before tombstones, preserving the previous conflict
  // rule when a server page contains both entries for the same key.  Each
  // bounded batch commits independently; a failed batch leaves the run cursor
  // unchanged and replaying the page is idempotent through the upsert key.
  for (const table of pendingTables) {
    const payload = response.tables[table];
    if (!payload) continue;
    await stagePayloads(table, payload.changes, false);
  }
  for (const table of pendingTables) {
    const payload = response.tables[table];
    if (!payload) continue;
    await stagePayloads(table, payload.tombstones, true);
  }

  const updatedAt = new Date().toISOString();
  const nextPending = Object.keys(nextCursors);
  await withDocsExclusiveTransaction(async (tx) => {
    await tx.runAsync(
      `UPDATE docs_sync_runs SET
         scope_key = ?, scope_id = ?, project_id = ?,
         snapshot_token = ?, scope_revision = ?, scope_digest = ?, server_time = ?,
         cursor_json = ?, pending_json = ?, state = ?,
         digest_json = ?, authoritative_json = ?, scopes_json = ?, updated_at = ?
       WHERE run_id = ? AND auth_scope = ?`,
      effectiveScopeKey,
      effectiveScopeId ?? null,
      effectiveProjectId,
      snapshotToken,
      scopeRevision,
      scopeDigest,
      serverTime,
      encodeDocsJson(nextCursors),
      encodeDocsJson(nextPending),
      nextPending.length ? "downloading" : "ready",
      encodeDocsJson(digestJson),
      encodeDocsJson(authoritativeJson),
      encodeDocsJson(scopesJson),
      updatedAt,
      run.runId,
      authScope,
    );
  });
  // Keep the in-memory run used by the page loop in lockstep with the row we
  // just persisted.  A pull may span many pages; retaining the original
  // run object would make the next page overwrite accumulated digests and
  // authoritative IDs with only the current page's metadata.
  Object.assign(run, {
    scopeKey: effectiveScopeKey,
    scopeId: effectiveScopeId ?? null,
    projectId: effectiveProjectId,
    snapshotToken,
    scopeRevision,
    scopeDigest,
    serverTime,
    cursorJson: nextCursors,
    pendingJson: nextPending,
    state: nextPending.length ? "downloading" : "ready",
    digestJson,
    authoritativeJson,
    scopesJson,
    updatedAt,
  });
  return stagedRowsWritten;
}

async function getSavedDocsScopeDigest(
  authScope: string,
  scopeId?: string,
  projectId?: string | null,
): Promise<string | null> {
  const db = getDb();
  const rows = await db
    .select()
    .from(schema.syncState)
    .where(eq(schema.syncState.tableName, getDocsScopeDigestKey(authScope, scopeId, projectId)));
  return rows[0]?.cursor ?? null;
}

async function getSavedDocsScopeRevision(
  authScope: string,
  scopeId?: string,
  projectId?: string | null,
): Promise<string | null> {
  const db = getDb();
  const rows = await db
    .select()
    .from(schema.syncState)
    .where(eq(schema.syncState.tableName, getDocsScopeRevisionKey(authScope, scopeId, projectId)));
  return rows[0]?.cursor ?? null;
}

async function saveDocsScopeDigest(
  authScope: string,
  digest: string,
  scopeId?: string,
  projectId?: string | null,
): Promise<void> {
  const db = getDb();
  await db
    .insert(schema.syncState)
    .values({
      tableName: getDocsScopeDigestKey(authScope, scopeId, projectId),
      lastPulledAt: null,
      lastPushedAt: null,
      cursor: digest,
    })
    .onConflictDoUpdate({
      target: schema.syncState.tableName,
      set: { cursor: digest },
    });
}

/** 保存済みの Docs digest を一括読みし、次回 pull でエコーする形へ整える。 */
async function getSavedDocsDigests(
  authScope: string,
  scopeId?: string,
  projectId?: string | null,
): Promise<Record<string, string>> {
  const db = getDb();
  const prefix = getDocsDigestPrefix(authScope, scopeId, projectId);
  const rows = await db
    .select()
    .from(schema.syncState)
    .where(like(schema.syncState.tableName, `${prefix}%`));
  const digests: Record<string, string> = {};
  for (const row of rows) {
    // drizzle の like ヘルパは ESCAPE 句を持たず、prefix 内の `_`/`%`（将来 scope に
    // 含まれ得る）が LIKE ワイルドカードとして過剰マッチしうる。JS 側で厳密な
    // 前方一致に絞り、他 scope や別種 tableName の混入を防ぐ。
    if (!row.tableName.startsWith(prefix)) continue;
    if (row.cursor) {
      digests[row.tableName.slice(prefix.length)] = row.cursor;
    }
  }
  return digests;
}

/** 応答の authoritative_digest を cursor 列へ保存する（適用完了後のみ呼ぶ）。 */
async function saveDocsDigest(
  authScope: string,
  table: string,
  digest: string | null | undefined,
  scopeId?: string,
  projectId?: string | null,
): Promise<void> {
  if (!digest) return;
  const tableName = getDocsDigestKey(authScope, table, scopeId, projectId);
  const db = getDb();
  await db
    .insert(schema.syncState)
    .values({
      tableName,
      lastPulledAt: null,
      lastPushedAt: null,
      cursor: digest,
    })
    .onConflictDoUpdate({
      target: schema.syncState.tableName,
      set: { cursor: digest },
    });
}

async function getLastPulledAt(
  authScope: string,
  docs = false,
  docsScopeId?: string,
  docsProjectId?: string | null,
): Promise<string | null> {
  const tableName = docs
    ? getDocsSyncStateKey(authScope, docsScopeId, docsProjectId)
    : getSyncStateKey(authScope);
  const db = getDb();
  const rows = await db
    .select()
    .from(schema.syncState)
    .where(eq(schema.syncState.tableName, tableName));
  return rows[0]?.lastPulledAt ?? null;
}

async function setLastPulledAt(
  authScope: string,
  value: string,
  docs = false,
  docsScopeId?: string,
  docsProjectId?: string | null,
): Promise<void> {
  const tableName = docs
    ? getDocsSyncStateKey(authScope, docsScopeId, docsProjectId)
    : getSyncStateKey(authScope);
  const db = getDb();
  await db
    .insert(schema.syncState)
    .values({
      tableName,
      lastPulledAt: value,
      lastPushedAt: null,
      cursor: null,
    })
    .onConflictDoUpdate({
      target: schema.syncState.tableName,
      set: { lastPulledAt: value },
    });
}

async function applyPullResponse(
  authScope: string,
  response: SyncPullResponse,
  context: SyncExecutionContext,
  scopeId?: string,
  projectId?: string | null,
  options: { forceDocs?: boolean; stageDocs?: boolean } = {},
): Promise<void> {
  assertSyncExecutionActive(context);
  const projects = response.tables.projects;
  if (projects) {
    await applyRemoteProjects(projects.changes as unknown as Project[]);
    await applyProjectTombstones(projects.tombstones);
  }

  const tasks = response.tables.tasks;
  if (tasks) {
    assertSyncExecutionActive(context);
    await applyRemoteTasks(tasks.changes as unknown as Task[]);
    await applyTaskTombstones(tasks.tombstones);
  }

  const occurrences = response.tables.task_occurrences;
  if (occurrences) {
    assertSyncExecutionActive(context);
    await applyRemoteOccurrences(
      occurrences.changes as unknown as TaskOccurrence[],
    );
    await applyOccurrenceTombstones(occurrences.tombstones);
  }

  const timeEntries = response.tables.time_entries;
  if (timeEntries) {
    assertSyncExecutionActive(context);
    await applyRemoteTimeEntries(timeEntries.changes as unknown as TimeEntry[]);
    await applyTimeEntryTombstones(timeEntries.tombstones);
  }

  const sessions = response.tables.conversation_sessions;
  if (sessions) {
    assertSyncExecutionActive(context);
    await applyRemoteConversationSessions(
      sessions.changes as unknown as ConversationSession[],
    );
    await applyConversationSessionTombstones(sessions.tombstones);
    await reconcileConversationSessionsWithServer(sessions.authoritative_ids);
  }

  const messages = response.tables.conversation_messages;
  if (messages) {
    assertSyncExecutionActive(context);
    await applyRemoteConversationMessages(
      messages.changes as unknown as ConversationMessage[],
    );
    await applyConversationMessageTombstones(messages.tombstones);
  }

  const recordTables = response.tables.record_tables;
  if (recordTables) {
    assertSyncExecutionActive(context);
    await applyRemoteRecordTables(
      recordTables.changes as unknown as RecordTable[],
    );
    await applyRecordTableTombstones(recordTables.tombstones);
  }

  const recordFields = response.tables.record_fields;
  if (recordFields) {
    assertSyncExecutionActive(context);
    await applyRemoteRecordFields(
      recordFields.changes as unknown as RecordField[],
    );
    await applyRecordFieldTombstones(recordFields.tombstones);
  }

  const recordRows = response.tables.record_rows;
  if (recordRows) {
    assertSyncExecutionActive(context);
    await applyRemoteRecordRows(recordRows.changes as unknown as RecordRow[]);
    await applyRecordRowTombstones(recordRows.tombstones);
  }

  // ---------- Docs ----------
  // A staged Docs pull is committed by promoteDocsSyncRun only after all
  // pages, cursors and ACL metadata validate.  General tables above retain
  // their existing incremental apply semantics.
  if (options.stageDocs) return;
  // digest 一致時、サーバは changes を空にし authoritative_ids / authoritative_scope_id を
  // 省略する。changes が空なら apply を、authoritative_ids が無いなら reconcile を呼ばない。
  // 各テーブルの適用と reconcile が例外なく完了した後にだけ digest を保存する。
  const docsNodes = response.tables.knowledge_nodes;
  if (docsNodes) {
    assertSyncExecutionActive(context);
    if (docsNodes.changes?.length) {
      const nodeChanges = docsNodes.changes as unknown as DocsNode[];
      if (options.forceDocs) {
        await applyRemoteDocsNodes(nodeChanges, { force: true });
      } else {
        await applyRemoteDocsNodes(nodeChanges);
      }
    }
    await applyDocsNodeTombstones(docsNodes.tombstones);
    if (docsNodes.authoritative_ids != null) {
      await reconcileDocsNodesWithServer(
        docsNodes.authoritative_ids,
        docsNodes.authoritative_scope_id,
      );
    }
    if (docsNodes.authoritative_scope_id) {
      await saveDocsWorkspaceId(
        authScope,
        docsNodes.authoritative_scope_id,
        scopeId,
        projectId,
      );
    }
    await saveDocsDigest(
      authScope,
      "knowledge_nodes",
      docsNodes.authoritative_digest,
      scopeId,
      projectId,
    );
  }

  const docsSupertags = response.tables.knowledge_supertags;
  if (docsSupertags) {
    assertSyncExecutionActive(context);
    if (docsSupertags.changes?.length) {
      await applyRemoteDocsSupertags(
        docsSupertags.changes as unknown as DocsSupertag[],
      );
    }
    await reconcileDocsSupertagsWithServer(
      docsSupertags.authoritative_ids,
      docsSupertags.authoritative_scope_id,
    );
    await saveDocsDigest(
      authScope,
      "knowledge_supertags",
      docsSupertags.authoritative_digest,
      scopeId,
      projectId,
    );
  }

  const docsFields = response.tables.knowledge_fields;
  if (docsFields) {
    assertSyncExecutionActive(context);
    if (docsFields.changes?.length) {
      await applyRemoteDocsFields(docsFields.changes as unknown as DocsField[]);
    }
    await reconcileDocsFieldsWithServer(
      docsFields.authoritative_ids,
      docsFields.authoritative_scope_id,
    );
    await saveDocsDigest(
      authScope,
      "knowledge_fields",
      docsFields.authoritative_digest,
      scopeId,
      projectId,
    );
  }

  const docsFieldValues = response.tables.knowledge_field_values;
  if (docsFieldValues) {
    assertSyncExecutionActive(context);
    if (
      docsFieldValues.changes?.length ||
      docsFieldValues.authoritative_ids != null
    ) {
      await applyRemoteDocsFieldValues(
        docsFieldValues.changes as unknown as DocsFieldValue[],
        docsFieldValues.authoritative_ids,
      );
    }
    await saveDocsDigest(
      authScope,
      "knowledge_field_values",
      docsFieldValues.authoritative_digest,
      scopeId,
      projectId,
    );
  }

  // 関連表は削除を取りこぼさないよう authoritative_ids で reconcile（省略時は据え置き）。
  const docsNodeSupertags = response.tables.knowledge_node_supertags;
  if (docsNodeSupertags) {
    assertSyncExecutionActive(context);
    if (
      docsNodeSupertags.changes?.length ||
      docsNodeSupertags.authoritative_ids != null
    ) {
      await applyRemoteDocsNodeSupertags(
        docsNodeSupertags.changes as unknown as DocsNodeSupertag[],
        docsNodeSupertags.authoritative_ids,
      );
    }
    await saveDocsDigest(
      authScope,
      "knowledge_node_supertags",
      docsNodeSupertags.authoritative_digest,
      scopeId,
      projectId,
    );
  }

  const docsSupertagFields = response.tables.knowledge_supertag_fields;
  if (docsSupertagFields) {
    assertSyncExecutionActive(context);
    if (
      docsSupertagFields.changes?.length ||
      docsSupertagFields.authoritative_ids != null
    ) {
      await applyRemoteDocsSupertagFields(
        docsSupertagFields.changes as unknown as DocsSupertagField[],
        docsSupertagFields.authoritative_ids,
      );
    }
    await saveDocsDigest(
      authScope,
      "knowledge_supertag_fields",
      docsSupertagFields.authoritative_digest,
      scopeId,
      projectId,
    );
  }

  const docsPlacements = response.tables.knowledge_node_placements;
  if (docsPlacements) {
    assertSyncExecutionActive(context);
    if (
      docsPlacements.changes?.length ||
      docsPlacements.authoritative_ids != null
    ) {
      await applyRemoteDocsPlacements(
        docsPlacements.changes as unknown as DocsNodePlacement[],
        docsPlacements.authoritative_ids,
      );
    }
    await saveDocsDigest(
      authScope,
      "knowledge_node_placements",
      docsPlacements.authoritative_digest,
      scopeId,
      projectId,
    );
  }

  const docsEdges = response.tables.knowledge_edges;
  if (docsEdges) {
    assertSyncExecutionActive(context);
    if (docsEdges.changes?.length || docsEdges.authoritative_ids != null) {
      await applyRemoteDocsEdges(
        docsEdges.changes as unknown as DocsEdge[],
        docsEdges.authoritative_ids,
      );
    }
    await saveDocsDigest(
      authScope,
      "knowledge_edges",
      docsEdges.authoritative_digest,
      scopeId,
      projectId,
    );
  }

}

function isDocsSnapshotChanged(error: unknown): boolean {
  const body = isApiHttpError(error) ? error.responseBody : "";
  return (
    isApiHttpError(error) &&
    (error.status === 400 || error.status === 409) &&
    /Docs (?:sync )?(?:snapshot|scope) (?:changed|revoked)/i.test(body)
  );
}

async function applyDocsPullOnce(
  authScope: string,
  docsSince: string | null,
  docsDigests: Record<string, string>,
  savedDocsScopeDigest: string | null,
  savedDocsScopeRevision: string | null,
  context: SyncExecutionContext,
  scopeId?: string,
  projectId?: string | null,
  options: {
    forceDocs?: boolean;
    onProgress?: DocsResyncProgressHandler;
    run?: DocsSyncRunRow | null;
    previousScopes?: DocsSyncScope[];
  } = {},
): Promise<DocsSyncScope[]> {
  const persistedRun = options.run ?? null;
  let pendingTables = persistedRun
    ? parseJsonRecord<SyncTable[]>(persistedRun.pendingJson, [...DOCS_TABLES])
    : [...DOCS_TABLES];
  if (!pendingTables.length && persistedRun) pendingTables = [];
  let docsCursors = persistedRun
    ? parseJsonRecord<Record<string, string>>(persistedRun.cursorJson, {})
    : {};
  let docsServerTime: string | null = persistedRun?.serverTime ?? null;
  let responseDocsScopeDigest: string | null = persistedRun?.scopeDigest ?? null;
  let requestDocsScopeDigest = savedDocsScopeDigest;
  let snapshotToken = persistedRun?.snapshotToken ?? null;
  let scopeRevision = persistedRun?.scopeRevision ?? savedDocsScopeRevision ?? null;
  const seenCursorStates = new Set<string>();
  const authoritativeCounts = new Map<SyncTable, number>();
  const appliedCounts = new Map<SyncTable, number>();
  let stagedRowsWritten = 0;
  let discoveredScopes: DocsSyncScope[] = [];
  let page = 0;

  while (pendingTables.length) {
    assertSyncExecutionActive(context);
    const docsResponse = await pullSync({
      since: docsSince,
      tables: pendingTables,
      ...(scopeId ? { docs_scope_id: scopeId } : {}),
      ...(projectId ? { project_id: projectId } : {}),
      docs_pagination: true,
      // Staging needs the authoritative ID set on the terminal page so clean
      // rows can be reconciled during atomic promotion.  This is intentionally
      // true even for force resync; force no longer means destructive delete.
      docs_reconcile: true,
      ...(snapshotToken ? { docs_snapshot_token: snapshotToken } : {}),
      ...(scopeRevision ? { docs_scope_revision: scopeRevision } : {}),
      ...(requestDocsScopeDigest
        ? { docs_scope_digest: requestDocsScopeDigest }
        : {}),
      ...(Object.keys(docsDigests).length
        ? { docs_digests: docsDigests }
        : {}),
      ...(Object.keys(docsCursors).length
        ? { docs_cursors: docsCursors }
        : {}),
    });
    assertSyncExecutionActive(context);
    if (docsResponse.docs_pagination_version !== 2) {
      throw new Error("サーバーがDocs分割同期に対応していません");
    }
    if (!docsResponse.docs_scope_digest) {
      throw new Error("サーバーがDocs可視範囲を返しませんでした");
    }
    if (
      snapshotToken &&
      docsResponse.docs_snapshot_token &&
      snapshotToken !== docsResponse.docs_snapshot_token
    ) {
      throw new Error("Docs同期snapshot tokenがページ間で変わりました");
    }
    if (
      scopeRevision &&
      docsResponse.docs_scope_revision &&
      scopeRevision !== docsResponse.docs_scope_revision
    ) {
      throw new Error("Docs同期scope revisionがページ間で変わりました");
    }
    snapshotToken = docsResponse.docs_snapshot_token ?? snapshotToken;
    scopeRevision = docsResponse.docs_scope_revision ?? scopeRevision;
    if (docsResponse.docs_scopes?.length) {
      discoveredScopes = docsResponse.docs_scopes;
    }
    if (
      responseDocsScopeDigest &&
      responseDocsScopeDigest !== docsResponse.docs_scope_digest
    ) {
      throw new Error("Docs可視範囲が同期中に変わりました");
    }
    responseDocsScopeDigest = docsResponse.docs_scope_digest;
    // Echo the server's immutable scope digest on continuation pages.  The
    // saved digest is only a first-page optimization; repeatedly sending a
    // stale value would force a full scan on every page.
    requestDocsScopeDigest = docsResponse.docs_scope_digest;
    docsServerTime ??= docsResponse.server_time;
    for (const table of pendingTables) {
      const count = docsResponse.tables[table]?.authoritative_count;
      if (typeof count === "number" && Number.isFinite(count)) {
        authoritativeCounts.set(table, Math.max(0, count));
      }
    }
    const useStaging = Boolean(persistedRun && docsStagingAvailable());
    await applyPullResponse(authScope, docsResponse, context, scopeId, projectId, {
      forceDocs: options.forceDocs,
      stageDocs: useStaging,
    });
    assertSyncExecutionActive(context);
    page += 1;
    for (const table of pendingTables) {
      const applied = docsResponse.tables[table]?.changes?.length ?? 0;
      appliedCounts.set(table, (appliedCounts.get(table) ?? 0) + applied);
    }
    const nextCursors: Record<string, string> = {};
    for (const table of pendingTables) {
      const cursor = docsResponse.tables[table]?.cursor;
      if (cursor) nextCursors[table] = cursor;
    }
    if (useStaging && persistedRun) {
      stagedRowsWritten += await persistDocsPage(
        persistedRun,
        authScope,
        scopeId,
        projectId,
        docsResponse,
        pendingTables,
        nextCursors,
      );
    }
    if (options.onProgress) {
      const hasAllCounts = DOCS_TABLES.every((table) =>
        authoritativeCounts.has(table),
      );
      const total = hasAllCounts
        ? DOCS_TABLES.reduce(
            (sum, table) => sum + (authoritativeCounts.get(table) ?? 0),
            0,
          )
        : null;
      const completed = DOCS_TABLES.reduce(
        (sum, table) => sum + (appliedCounts.get(table) ?? 0),
        0,
      );
      options.onProgress({
        phase: "downloading",
        completed: total == null ? completed : Math.min(completed, total),
        total,
        page,
        stagedRowsWritten,
      });
    }
    if (!Object.keys(nextCursors).length) break;

    const cursorState = JSON.stringify(nextCursors);
    if (seenCursorStates.has(cursorState)) {
      throw new Error("Docs同期cursorが進みませんでした");
    }
    seenCursorStates.add(cursorState);
    docsCursors = nextCursors;
    pendingTables = Object.keys(nextCursors) as SyncTable[];
  }

  if (docsServerTime && responseDocsScopeDigest) {
    assertSyncExecutionActive(context);
    const total = DOCS_TABLES.every((table) => authoritativeCounts.has(table))
      ? DOCS_TABLES.reduce(
          (sum, table) => sum + (authoritativeCounts.get(table) ?? 0),
          0,
        )
      : null;
    options.onProgress?.({
      phase: "finalizing",
      completed:
        total
        ?? DOCS_TABLES.reduce(
          (sum, table) => sum + (appliedCounts.get(table) ?? 0),
          0,
        ),
      total,
      page,
    });
    if (persistedRun && docsStagingAvailable()) {
      const currentRun = await getDocsSyncRun(persistedRun.runId, authScope);
      if (!currentRun) throw new Error("Docs同期runが見つかりません");
      const authoritative = parseJsonRecord<DocsSyncAuthoritative>(
        currentRun.authoritativeJson,
        {},
      );
      // Only a non-empty, persisted `docs_scopes` array is authoritative.  A
      // legacy v2 response may omit the field entirely; falling back to the
      // previously saved scope set in that case would incorrectly interpret
      // every prior scope as revoked during atomic promotion.  Empty arrays
      // are likewise treated as an unknown projection (the server contract
      // promises a complete non-empty set for authenticated roots).
      const persistedScopes = parseJsonRecord<DocsSyncScope[] | null>(
        currentRun.scopesJson,
        null,
      );
      const hasAuthoritativeScopeSet = Boolean(
        Array.isArray(persistedScopes)
        && persistedScopes.length > 0
        && persistedScopes.every(
          (scope) => Boolean(
            scope
            && typeof scope.workspace_id === "string"
            && scope.workspace_id.length > 0
            && (scope.project_id == null || typeof scope.project_id === "string"),
          ),
        ),
      );
      const runScopes = hasAuthoritativeScopeSet
        ? persistedScopes!
        : discoveredScopes;
      // Root discovery pulls are keyed by the server's authoritative library,
      // not the local "personal" placeholder.  Persist that concrete
      // workspace key in the same transaction as promotion so listPages/read
      // filters never expose rows from an unrelated scope after restart.
      const personalRootScope = runScopes.find(
        (scope) =>
          (scope.project_id ?? null) === null
          && scope.source === "personal"
          && scope.access === "owner",
      );
      // A root run starts without a concrete library id.  Only the explicit
      // personal-owner scope may supply that identity; choosing the first
      // project_id=NULL shared scope would attach personal rows to a sibling
      // library.  The final fallbacks are retained only for pre-metadata
      // servers that return no docs_scopes at all.
      const finalizedWorkspaceId =
        scopeId
        ?? personalRootScope?.workspace_id
        ?? (runScopes.length
          ? undefined
          : currentRun.scopeId
            ?? authoritative.knowledge_nodes?.scopeId
            ?? authoritative.knowledge_supertags?.scopeId
            ?? authoritative.knowledge_fields?.scopeId);
      if (!finalizedWorkspaceId) {
        throw new Error("Docs personal scope is missing from authoritative projection");
      }
      const promotionScopeId = scopeId ?? currentRun.scopeId ?? finalizedWorkspaceId;
      const promotionProjectId = projectId ?? currentRun.projectId ?? null;
      // Membership/outbox isolation always uses the concrete library key.
      // Keep the legacy root digest/sync-state keys below (scopeId was
      // intentionally omitted before the composite rollout) so existing
      // devices do not re-download the same personal snapshot forever.
      const promotionScopeKey = getDocsScopeKey(
        finalizedWorkspaceId ?? promotionScopeId,
        promotionProjectId,
      );
      const stateScopeId = scopeId;
      const stateProjectId = projectId;
      const promotionTelemetry = await promoteDocsSyncRun({
        runId: persistedRun.runId,
        authScope,
        scopeId: promotionScopeId,
        projectId: promotionProjectId,
        scopeKey: promotionScopeKey,
        force: options.forceDocs,
        authoritative,
        scopes: runScopes,
        finalize: {
          scopeDigest: responseDocsScopeDigest,
          serverTime: docsServerTime,
          digestByTable: parseJsonRecord<Record<string, string>>(
            currentRun.digestJson,
            {},
          ),
          scopeDigestKey: getDocsScopeDigestKey(authScope, stateScopeId, stateProjectId),
          scopeRevision,
          scopeRevisionKey: getDocsScopeRevisionKey(authScope, stateScopeId, stateProjectId),
          lastPulledKey: getDocsSyncStateKey(authScope, stateScopeId, stateProjectId),
          digestKeys: Object.fromEntries(
            Object.keys(
              parseJsonRecord<Record<string, string>>(currentRun.digestJson, {}),
            ).map((table) => [
              table,
              getDocsDigestKey(authScope, table, stateScopeId, stateProjectId),
            ]),
          ),
          scopesKey: hasAuthoritativeScopeSet
            ? getDocsScopesKey(authScope)
            : undefined,
          workspaceKey: finalizedWorkspaceId
            ? getDocsWorkspaceKey(authScope, finalizedWorkspaceId, promotionProjectId)
            : undefined,
          workspaceId: finalizedWorkspaceId,
        },
        scopeSet: options.previousScopes && hasAuthoritativeScopeSet
          ? {
              previousScopes: options.previousScopes,
              newScopes: runScopes,
            }
          : undefined,
      });
      if (promotionTelemetry) {
        options.onProgress?.({
          phase: "finalizing",
          completed:
            total
            ?? DOCS_TABLES.reduce(
              (sum, table) => sum + (appliedCounts.get(table) ?? 0),
              0,
            ),
          total,
          page,
          stagedRowsWritten,
          stagedRowsRead: promotionTelemetry.rowsRead,
          stagedPromotionBatches: promotionTelemetry.batches,
          stagedPromotionMaxBatchSize: promotionTelemetry.maxBatchSize,
        });
      }
    } else {
      await saveDocsScopeDigest(authScope, responseDocsScopeDigest, scopeId, projectId);
      if (scopeRevision) {
        await saveDocsScopeRevision(authScope, scopeRevision, scopeId, projectId);
      }
      assertSyncExecutionActive(context);
      await setLastPulledAt(authScope, docsServerTime, true, scopeId, projectId);
    }
  }
  return discoveredScopes;
}

async function applyGeneralPull(
  authScope: string,
  context: SyncExecutionContext,
): Promise<void> {
  assertSyncExecutionActive(context);
  const since = await getLastPulledAt(authScope);
  assertSyncExecutionActive(context);
  const response = await pullSync({ since, tables: TABLES });
  assertSyncExecutionActive(context);
  await applyPullResponse(authScope, response, context);
  assertSyncExecutionActive(context);
  await setLastPulledAt(authScope, response.server_time);
}

async function saveDocsWorkspaceId(
  authScope: string,
  workspaceId: string,
  scopeId?: string,
  projectId?: string | null,
): Promise<void> {
  const db = getDb();
  await db
    .insert(schema.syncState)
    .values({
      tableName: getDocsWorkspaceKey(authScope, scopeId, projectId),
      lastPulledAt: null,
      lastPushedAt: null,
      cursor: workspaceId,
    })
    .onConflictDoUpdate({
      target: schema.syncState.tableName,
      set: { cursor: workspaceId },
    });
}

async function getSavedDocsScopes(authScope: string): Promise<DocsSyncScope[]> {
  const db = getDb();
  const rows = await db
    .select()
    .from(schema.syncState)
    .where(eq(schema.syncState.tableName, getDocsScopesKey(authScope)));
  const value = rows[0]?.cursor;
  if (!value) return [];
  try {
    const parsed = JSON.parse(value) as unknown;
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(
      (scope): scope is DocsSyncScope =>
        Boolean(
          scope &&
            typeof scope === "object" &&
            typeof (scope as DocsSyncScope).workspace_id === "string",
        ),
    );
  } catch {
    return [];
  }
}

async function saveDocsScopes(
  authScope: string,
  scopes: DocsSyncScope[],
): Promise<void> {
  const db = getDb();
  await db
    .insert(schema.syncState)
    .values({
      tableName: getDocsScopesKey(authScope),
      lastPulledAt: null,
      lastPushedAt: null,
      cursor: JSON.stringify(scopes),
    })
    .onConflictDoUpdate({
      target: schema.syncState.tableName,
      set: { cursor: JSON.stringify(scopes) },
    });
}

async function saveDocsScopeRevision(
  authScope: string,
  revision: string,
  scopeId?: string,
  projectId?: string | null,
): Promise<void> {
  const db = getDb();
  await db
    .insert(schema.syncState)
    .values({
      tableName: getDocsScopeRevisionKey(authScope, scopeId, projectId),
      lastPulledAt: null,
      lastPushedAt: null,
      cursor: revision,
    })
    .onConflictDoUpdate({
      target: schema.syncState.tableName,
      set: { cursor: revision },
    });
}

async function applyDocsPull(
  authScope: string,
  context: SyncExecutionContext,
  options: {
    forceDocs?: boolean;
    onProgress?: DocsResyncProgressHandler;
  } = {},
): Promise<void> {
  assertSyncExecutionActive(context);
  const previousScopes = await getSavedDocsScopes(authScope);

  const pullScope = async (
    scopeId?: string,
    projectId?: string | null,
  ): Promise<DocsSyncScope[]> => {
    // Force rebuilds intentionally start without saved cursors/digests.  They
    // still stage into a new run and never clear live rows/outbox before a
    // validated promotion, but a digest-equal response must not short-circuit
    // the requested authoritative snapshot.
    const docsSince = options.forceDocs
      ? null
      : await getLastPulledAt(authScope, true, scopeId, projectId);
    let docsDigests = options.forceDocs
      ? {}
      : await getSavedDocsDigests(authScope, scopeId, projectId);
    let savedDocsScopeDigest = options.forceDocs
      ? null
      : await getSavedDocsScopeDigest(authScope, scopeId, projectId);
    let savedDocsScopeRevision = options.forceDocs
      ? null
      : await getSavedDocsScopeRevision(authScope, scopeId, projectId);
    let run = await getActiveDocsSyncRun(authScope, scopeId, projectId, options.forceDocs);
    if (!run) {
      // A force request starts a new staged snapshot, but never clears live
      // rows/outbox.  Any abandoned normal run is staging-only and safe to
      // discard before creating the explicit force run.
      const otherRun = options.forceDocs
        ? await getActiveDocsSyncRun(authScope, scopeId, projectId, false)
        : null;
      if (otherRun) await deleteDocsSyncRun(otherRun.runId, authScope);
      run = await createDocsSyncRun(authScope, scopeId, projectId, Boolean(options.forceDocs));
    }
    if (run && !options.forceDocs) {
      docsDigests = {
        ...docsDigests,
        ...parseJsonRecord<Record<string, string>>(run.digestJson, {}),
      };
    }
    for (let attempt = 0; attempt < DOCS_PULL_MAX_ATTEMPTS; attempt += 1) {
      try {
        return await applyDocsPullOnce(
          authScope,
          docsSince,
          docsDigests,
          savedDocsScopeDigest,
          savedDocsScopeRevision,
          context,
          scopeId,
          projectId,
          {
            ...options,
            run,
            previousScopes: scopeId == null ? previousScopes : undefined,
          },
        );
      } catch (error) {
        if (
          isDocsSnapshotChanged(error) &&
          attempt < DOCS_PULL_MAX_ATTEMPTS - 1
        ) {
          assertSyncExecutionActive(context);
          if (run) {
            // Cursor/token rejection means the staged pages belong to an
            // invalid server snapshot.  Delete only staging/run metadata;
            // live cache, outbox and conflicts remain intact.
            await deleteDocsSyncRun(run.runId, authScope);
            run = await createDocsSyncRun(
              authScope,
              scopeId,
              projectId,
              Boolean(options.forceDocs),
            );
          }
          // A 400/409 snapshot change can be an ACL revision update.  Do not
          // resend the stale scope digest/revision into the fresh run; the
          // server must establish a new immutable snapshot.  Per-table
          // digests are still reloaded because already-completed tables may
          // have been applied before the failed cursor was rejected.
          savedDocsScopeDigest = null;
          savedDocsScopeRevision = null;
          docsDigests = await getSavedDocsDigests(authScope, scopeId, projectId);
          await new Promise<void>((resolve) =>
            setTimeout(resolve, DOCS_PULL_RETRY_DELAY_MS * (attempt + 1)),
          );
          assertSyncExecutionActive(context);
          continue;
        }
        throw error;
      }
    }
    return [];
  };

  const discoveredScopes = await pullScope();
  // Older servers omit docs_scopes and retain the v2 personal-only contract.
  if (!discoveredScopes.length) return;
  const activeIds = new Set(
    discoveredScopes.map((scope) => getDocsScopeKey(scope.workspace_id, scope.project_id)),
  );
  // With staged promotion the complete scope-set diff was applied inside the
  // root promotion transaction (including docs_scopes sync_state).  Keep the
  // old post-pull path only for rolling databases/test doubles without the
  // staging schema, where no atomic promotion is available.
  if (!docsStagingAvailable()) {
    for (const previous of previousScopes) {
      if (activeIds.has(getDocsScopeKey(previous.workspace_id, previous.project_id))) continue;
      if (typeof quarantineRevokedDocsScope === "function") {
        await quarantineRevokedDocsScope(
          authScope,
          previous.workspace_id,
          previous.project_id,
          getDocsScopeKey(previous.workspace_id, previous.project_id),
        );
      }
      if (!schema.docsScopeMembership) {
        await reconcileDocsNodesWithServer([], previous.workspace_id);
        await reconcileDocsSupertagsWithServer([], previous.workspace_id);
        await reconcileDocsFieldsWithServer([], previous.workspace_id);
      }
    }
    for (const scope of discoveredScopes) {
      if (scope.source === "personal" && scope.access === "owner") continue;
      const previous = previousScopes.find(
        (item) =>
          getDocsScopeKey(item.workspace_id, item.project_id)
          === getDocsScopeKey(scope.workspace_id, scope.project_id),
      );
      if (previous && !previous.read_only && scope.read_only) {
        await quarantineRevokedDocsScope(
          authScope,
          scope.workspace_id,
          scope.project_id,
          getDocsScopeKey(scope.workspace_id, scope.project_id),
          "downgrade",
        );
      }
    }
    await saveDocsScopes(authScope, discoveredScopes);
  }
  for (const scope of discoveredScopes) {
    if (scope.source === "personal" && scope.access === "owner") continue;
    await pullScope(scope.workspace_id, scope.project_id);
  }
}

/**
 * Settingsから呼ぶサーバー正本のDocs再構築。
 * 認証切替・通常同期と同じexclusive queue上で実行し、live cache/outboxを
 * 先に破棄せず staging へ全量取得してから原子的に昇格する。
 */
export async function forceDocsResync(
  onProgress?: DocsResyncProgressHandler,
): Promise<void> {
  if (!useNetworkStore.getState().online) {
    throw new Error("サーバーに接続してからDocsを再構築してください");
  }
  const token = await getToken();
  if (!token) {
    throw new Error("サーバーログインが必要です");
  }
  const authScope = getTokenAuthScope(token);
  await runAuthScopeTransition(async () => {
    const context = createSyncExecutionContext();
    assertSyncExecutionActive(context);
    onProgress?.({
      phase: "preparing",
      completed: 0,
      total: null,
      page: 0,
    });
    const currentToken = await getToken();
    if (!currentToken || getTokenAuthScope(currentToken) !== authScope) {
      throw new Error("認証状態が変わったため、Docs再構築を中止しました");
    }
    if (!useNetworkStore.getState().online) {
      throw new Error("サーバー接続が失われたため、Docs再構築を中止しました");
    }
    assertSyncExecutionActive(context);
    await applyDocsPull(authScope, context, { forceDocs: true, onProgress });
  });
}

/**
 * Docs push 応答の entity（サーバ権威）を対応する applyRemote* で反映する。
 * 409 conflict の応答も、競合解決が適用済みと判断した場合に反映する。
 */
async function applyDocsPushEntity(
  table: string | undefined,
  entity: Record<string, unknown> | undefined,
  authScope?: string,
): Promise<void> {
  if (!table || !entity) return;
  // クリア/タグ削除時、サーバは { ..., "deleted": true } を返す。無条件 upsert すると
  // 削除済み行を再 INSERT してしまうため、deleted の場合はローカル行を物理削除する。
  const deleted = entity.deleted === true;
  switch (table) {
    case "knowledge_nodes":
      if (await hasPendingOutbox(table, String(entity.id), authScope)) {
        await recordOutboxServerSnapshot(table, String(entity.id), entity, authScope);
        return;
      }
      await applyRemoteDocsNodes([entity as unknown as DocsNode], { force: true });
      break;
    case "knowledge_supertags":
      if (await hasPendingOutbox(table, String(entity.id), authScope)) {
        await recordOutboxServerSnapshot(table, String(entity.id), entity, authScope);
        return;
      }
      await applyRemoteDocsSupertags([entity as unknown as DocsSupertag], { force: true });
      break;
    case "knowledge_node_supertags":
      if (await hasPendingOutbox(
        table,
        `${String(entity.node_id)}:${String(entity.supertag_id)}`,
        authScope,
      )) {
        await recordOutboxServerSnapshot(
          table,
          `${String(entity.node_id)}:${String(entity.supertag_id)}`,
          entity,
          authScope,
        );
        return;
      }
      if (deleted) {
        await deleteLocalDocsNodeSupertag(
          String(entity.node_id),
          String(entity.supertag_id),
        );
      } else {
        await applyRemoteDocsNodeSupertags([
          entity as unknown as DocsNodeSupertag,
        ], undefined, { force: true });
      }
      break;
    case "knowledge_field_values":
      if (await hasPendingOutbox(
        table,
        `${String(entity.node_id)}:${String(entity.field_id)}`,
        authScope,
      )) {
        await recordOutboxServerSnapshot(
          table,
          `${String(entity.node_id)}:${String(entity.field_id)}`,
          entity,
          authScope,
        );
        return;
      }
      if (deleted) {
        await deleteLocalDocsFieldValue(
          String(entity.node_id),
          String(entity.field_id),
        );
      } else {
        await applyRemoteDocsFieldValues(
          [entity as unknown as DocsFieldValue],
          undefined,
          { force: true },
        );
      }
      break;
    default:
      break;
  }
}

function jsonEqual(a: unknown, b: unknown): boolean {
  return JSON.stringify(a ?? null) === JSON.stringify(b ?? null);
}

async function getCurrentOutbox(
  operationId: string,
): Promise<PendingOutbox | null> {
  const db = getDb();
  const rows = await db
    .select()
    .from(schema.outbox)
    .where(eq(schema.outbox.opId, operationId));
  return rows[0] ?? null;
}

function isOutboxSnapshotCurrent(
  current: PendingOutbox | null,
  snapshot: PendingOutbox,
): boolean {
  return Boolean(
    current &&
      current.tableName === snapshot.tableName &&
      current.action === snapshot.action &&
      current.entityId === snapshot.entityId &&
      current.payload === snapshot.payload &&
      current.baseUpdatedAt === snapshot.baseUpdatedAt,
  );
}

async function rebaseChangedDocsOutbox(
  operation: PendingOutbox,
  serverEntity: Record<string, unknown> | undefined,
  serverUpdatedAt: string | null | undefined,
): Promise<void> {
  if (!serverEntity || !operation.tableName.startsWith("knowledge_")) return;
  await rebaseOutboxOp(
    operation.opId,
    serverUpdatedAt ?? (serverEntity.updated_at as string | null | undefined) ?? null,
    serverEntity,
  );
}

type DocsConflictResolution = "rebased" | "applied" | "unresolved";

/**
 * サーバー409を端末時計で解決しない。ノードの異なるフィールドだけは
 * サーバー版を新しい基準として再ベースし、同一フィールド・削除状態は
 * outboxに両方を残して明示的競合にする。
 */
async function resolveDocsConflict(
  operation: PendingOutbox,
  serverEntity: Record<string, unknown> | undefined,
  serverUpdatedAt: string | null | undefined,
): Promise<DocsConflictResolution> {
  if (!serverEntity) return "unresolved";
  const base = (operation.basePayload ?? {}) as Record<string, unknown>;
  const payload = JSON.parse(operation.payload || "{}") as Record<string, unknown>;
  const serverVersion = serverUpdatedAt ?? (serverEntity.updated_at as string | null | undefined) ?? null;

  if (operation.tableName === "knowledge_nodes" && operation.action === "create") {
    const createFields = [
      "title",
      "description",
      "node_type",
      "project_id",
      "day_date",
      "sort_order",
    ];
    if ("body_json" in payload) createFields.push("body_json");
    if (
      serverEntity.deleted === true ||
      !createFields.every((key) => jsonEqual(serverEntity[key], payload[key]))
    ) {
      return "unresolved";
    }
    return "applied";
  }

  if (
    operation.tableName === "knowledge_nodes" &&
    operation.action === "update"
  ) {
    const changed = Object.keys(payload).filter((key) => !jsonEqual(payload[key], base[key]));
    if (changed.length && changed.every((key) => jsonEqual(serverEntity[key], payload[key]))) {
      return "applied";
    }
    const overlap = changed.some((key) => !jsonEqual(serverEntity[key], base[key]));
    if (overlap) return "unresolved";
    await rebaseOutboxOp(operation.opId, serverVersion, serverEntity);
    return "rebased";
  }

  if (operation.tableName === "knowledge_nodes" && operation.action === "delete") {
    if (serverEntity.archived_at) return "applied";
    const nodeFields = [
      "title",
      "description",
      "body_json",
      "parent_id",
      "project_id",
      "day_date",
      "sort_order",
      "archived_at",
    ];
    if (nodeFields.some((key) => !jsonEqual(serverEntity[key], base[key]))) {
      return "unresolved";
    }
    await rebaseOutboxOp(operation.opId, serverVersion, serverEntity);
    return "rebased";
  }

  if (operation.tableName === "knowledge_node_supertags") {
    const deleted = serverEntity.deleted === true;
    if ((operation.action === "create" && !deleted) || (operation.action === "delete" && deleted)) {
      return "applied";
    }
    return "unresolved";
  }

  if (operation.tableName === "knowledge_supertags" && operation.action === "create") {
    if (
      serverEntity.deleted === true ||
      !["name", "base_type", "color", "icon"]
        .every((key) => jsonEqual(serverEntity[key], payload[key]))
    ) {
      return "unresolved";
    }
    return "applied";
  }

  if (operation.tableName === "knowledge_field_values") {
    const value = payload.value;
    if (value === null || value === undefined || value === "") {
      return serverEntity.deleted === true ? "applied" : "unresolved";
    }
    const serverMatches =
      (typeof value === "number" && jsonEqual(serverEntity.value_number, value)) ||
      (typeof value === "boolean" && jsonEqual(serverEntity.value_json, { value })) ||
      (typeof value === "string" &&
        (jsonEqual(serverEntity.value_text, value) ||
          jsonEqual(serverEntity.value_datetime, value) ||
          jsonEqual(serverEntity.target_node_id, value))) ||
      jsonEqual(serverEntity.value_json, value);
    // 既に同じ値がサーバーへ適用済みなら、通信再送による409を解消できる。
    // 異なる値の場合は同一フィールドの競合なので、両方を残す。
    return serverMatches ? "applied" : "unresolved";
  }
  return "unresolved";
}

/** Apply a task push response without treating a delete acknowledgement as a
 * partial active task row.  DELETE responses intentionally contain only
 * `{id, deleted_at}`; routing them through applyRemoteTasks would otherwise
 * overwrite required fields and clear the tombstone on a later refresh.
 */
async function applyTaskPushEntity(
  operation: SyncPushOperation | undefined,
  entity: Record<string, unknown> | undefined,
  serverUpdatedAt?: string | null,
): Promise<void> {
  if (operation?.table !== "tasks") return;
  const entityId =
    typeof entity?.id === "string" && entity.id
      ? entity.id
      : operation.entity_id;
  if (!entityId) return;
  if (operation.action === "restore") {
    await applyTaskRestore({
      ...(entity ?? {}),
      id: entityId,
    });
    // Restore acknowledgements are compact batch payloads.  Rehydrate the
    // root when possible so a cache that only held a tombstone has full fields.
    if (!entity || typeof entity.title !== "string") {
      try {
        const restored = await taskApi.getTask(entityId);
        await applyTaskRestore(restored);
      } catch {
        // The next pull will deliver the active row; the explicit restore has
        // already cleared the local tombstone/ledger.
      }
    }
    return;
  }
  const deletedAt =
    typeof entity?.deleted_at === "string" && entity.deleted_at
      ? entity.deleted_at
      : operation.action === "delete"
        ? serverUpdatedAt ?? null
        : null;
  if (deletedAt != null || operation.action === "delete") {
    await applyTaskTombstones([
      { id: entityId, deleted_at: deletedAt },
    ]);
    return;
  }
  if (entity) {
    await applyRemoteTasks([entity as unknown as Task]);
  }
}

type ReorderPayload = {
  projectId: string | null;
  taskIds: string[];
};

function decodeReorderPayload(operation: PendingOutbox): ReorderPayload | null {
  let payload: unknown;
  try {
    payload = JSON.parse(operation.payload || "{}");
  } catch {
    return null;
  }
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return null;
  }
  const value = payload as Record<string, unknown>;
  const projectId = value.project_id;
  const rawTaskIds = value.task_ids;
  if (
    projectId !== null &&
    projectId !== undefined &&
    typeof projectId !== "string"
  ) {
    return null;
  }
  if (
    !Array.isArray(rawTaskIds) ||
    rawTaskIds.length === 0 ||
    rawTaskIds.some(
      (taskId) => typeof taskId !== "string" || taskId.trim().length === 0,
    )
  ) {
    return null;
  }
  return {
    projectId: typeof projectId === "string" ? projectId : null,
    taskIds: rawTaskIds as string[],
  };
}

function isTaskPrerequisite(operation: PendingOutbox): boolean {
  return operation.tableName === "tasks" && operation.action !== "reorder";
}

/**
 * A reorder is causally dependent on any task create/update still in the
 * outbox.  Query the DB rather than only the initial list because a failed
 * response may increment retryCount (and eventually disappear from
 * listPendingOutbox), while it remains an unresolved prerequisite.
 */
async function hasPendingReorderPrerequisite(
  taskIds: string[],
  authScope: string,
  regularPending: PendingOutbox[],
  regularResultStatuses: Map<string, "ok" | "conflict" | "error">,
): Promise<boolean> {
  for (const taskId of taskIds) {
    const prerequisites = regularPending.filter(
      (operation) =>
        isTaskPrerequisite(operation) && operation.entityId === taskId,
    );
    if (
      prerequisites.some(
        (operation) => regularResultStatuses.get(operation.opId) !== "ok",
      )
    ) {
      // An explicit per-operation error/conflict (or a missing result) is not
      // an acknowledgement, regardless of what a test double or stale DB
      // query happens to report.
      return true;
    }
    if (typeof hasPendingOutbox === "function") {
      if (await hasPendingOutbox("tasks", taskId, authScope)) return true;
      continue;
    }
    // Minimal test doubles from older suites do not expose hasPendingOutbox.
    // In that case, require an explicit successful result for each matching
    // operation in this push batch before allowing the reorder through.
    // No row and no failed/missing result means the dependency was acked.
  }
  return false;
}

/** Replay canonical task ordering through the dedicated endpoint. */
async function flushReorderOutbox(
  pending: PendingOutbox[],
  context: SyncExecutionContext,
  authScope: string,
  regularPending: PendingOutbox[] = [],
  regularResultStatuses: Map<string, "ok" | "conflict" | "error"> = new Map(),
): Promise<void> {
  for (const operation of pending) {
    assertSyncExecutionActive(context);
    const decoded = decodeReorderPayload(operation);
    if (!decoded) {
      await markOutboxError(operation.opId, "reorder:invalid_payload");
      continue;
    }

    if (
      await hasPendingReorderPrerequisite(
        decoded.taskIds,
        authScope,
        regularPending,
        regularResultStatuses,
      )
    ) {
      // The regular task create/update has not been acknowledged yet.  Keep
      // this reorder row intact; the next sync retries the prerequisite first.
      continue;
    }

    try {
      if (decoded.projectId) {
        await taskApi.reorderTasks(decoded.projectId, decoded.taskIds);
      } else {
        await taskApi.reorderAllTasks(decoded.taskIds);
      }
    } catch (error) {
      // Never discard a reorder merely because the endpoint returned HTTP
      // non-401.  Preserve it with an explicit classification so a 409 can
      // be reviewed/rebased and validation/permission failures are visible as
      // terminal instead of becoming silent data loss.
      if (isApiHttpError(error)) {
        const detail = error instanceof Error ? error.message : "http_error";
        if (error.status === 409) {
          await markOutboxConflict(
            operation.opId,
            `reorder:conflict:${detail}`,
          );
        } else if (error.status === 401) {
          await markOutboxError(operation.opId, `reorder:auth:${detail}`);
        } else if (error.status >= 400 && error.status < 500) {
          await markOutboxError(
            operation.opId,
            `reorder:terminal_http_${error.status}:${detail}`,
          );
        } else {
          await markOutboxError(operation.opId, `reorder:http_${error.status}:${detail}`);
        }
        continue;
      }
      await markOutboxError(
        operation.opId,
        error instanceof Error ? `reorder:${error.message}` : "reorder:error",
      );
      continue;
    }

    const removed = await removeOutboxOpIfSnapshot(operation.opId, {
      table: operation.tableName,
      action: operation.action,
      entityId: operation.entityId,
      payload: operation.payload,
      baseUpdatedAt: operation.baseUpdatedAt,
    });
    if (!removed) continue;
    // The local order is already optimistic.  A best-effort refresh reconciles
    // server canonical values without turning a refresh timeout into a blind
    // duplicate reorder.
    try {
      const refreshed = decoded.projectId
        ? await taskApi.listTasks(decoded.projectId)
        : await taskApi.listAllTasks();
      await applyRemoteTasks(refreshed);
    } catch {
      // Next regular pull will reconcile the canonical order.
    }
  }
}

async function pushOutbox(
  authScope: string,
  context: SyncExecutionContext,
): Promise<void> {
  assertSyncExecutionActive(context);
  const allPending = await listPendingOutbox(undefined, authScope);
  const pending = allPending.filter((op) => op.action !== "reorder");
  const reorderPending = allPending.filter((op) => op.action === "reorder");
  assertSyncExecutionActive(context);
  if (!pending.length && !reorderPending.length) return;
  if (!pending.length) {
    await flushReorderOutbox(reorderPending, context, authScope);
    return;
  }

  const operations: SyncPushOperation[] = pending.map((op) => ({
    op_id: op.opId,
    table: op.tableName,
    action: op.action as SyncPushOperation["action"],
    entity_id: op.entityId,
    payload: JSON.parse(op.payload || "{}") as Record<string, unknown>,
    base_updated_at: op.baseUpdatedAt ?? null,
  }));

  const response = await pushSync(operations);
  assertSyncExecutionActive(context);
  const operationsById = new Map(
    operations.map((operation) => [operation.op_id, operation]),
  );
  const pendingById = new Map(pending.map((operation) => [operation.opId, operation]));
  const regularResultStatuses = new Map<
    string,
    "ok" | "conflict" | "error"
  >();
  for (const result of response.results) {
    assertSyncExecutionActive(context);
    const operation = operationsById.get(result.op_id);
    const pendingOperation = pendingById.get(result.op_id);
    if (pendingOperation && isTaskPrerequisite(pendingOperation)) {
      regularResultStatuses.set(result.op_id, result.status);
    }
    // A DELETE acknowledgement is a tombstone even when the outbox row was
    // edited while the request was in flight.  Apply it before the snapshot
    // freshness branch; applyTaskTombstones performs the LWW guard against a
    // newer local update and never resurrects the row.
    if (
      result.status === "ok" &&
      operation?.table === "tasks" &&
      (operation.action === "delete" || operation.action === "restore")
    ) {
      await applyTaskPushEntity(
        operation,
        result.entity,
        result.server_updated_at,
      );
    }
    // push 中に同一エンティティが再編集されると、enqueueOutbox は同じ
    // operation_id の行を更新する。古い応答でその行を削除したり、サーバー値を
    // ローカルへ適用したりせず、応答時点のサーバー版を新しいoutboxの基準にする。
    if (pendingOperation) {
      const currentOperation = await getCurrentOutbox(result.op_id);
      if (!isOutboxSnapshotCurrent(currentOperation, pendingOperation)) {
        if (result.status === "ok") {
          // 送信した古い版はサーバーに適用済みなので、最新outboxだけを
          // 応答版へ再baseする。ローカル値そのものは決して上書きしない。
          await rebaseChangedDocsOutbox(
            currentOperation ?? pendingOperation,
            result.entity,
            result.server_updated_at,
          );
          if (isTaskPrerequisite(pendingOperation)) {
            // The sent snapshot was acknowledged, but a newer task mutation
            // is still pending under the same op id.
            regularResultStatuses.set(result.op_id, "error");
          }
          continue;
        }
        if (
          currentOperation &&
          result.status === "conflict" &&
          operation?.table?.startsWith("knowledge_")
        ) {
          const resolution = await resolveDocsConflict(
            currentOperation,
            result.entity,
            result.server_updated_at,
          );
          if (resolution === "applied") {
            const removed = await removeOutboxOpIfSnapshot(result.op_id, {
              table: currentOperation.tableName,
              action: currentOperation.action,
              entityId: currentOperation.entityId,
              payload: currentOperation.payload,
              baseUpdatedAt: currentOperation.baseUpdatedAt,
            });
            if (!removed) continue;
            if (isTaskPrerequisite(pendingOperation)) {
              regularResultStatuses.set(result.op_id, "ok");
            }
            await applyDocsPushEntity(operation.table, result.entity, authScope);
            continue;
          }
          if (resolution === "rebased") continue;
          await recordOutboxServerSnapshot(
            currentOperation.tableName,
            currentOperation.entityId,
            result.entity ?? { deleted: true },
            authScope,
          );
          await markOutboxConflict(
            result.op_id,
            result.reason ?? result.status,
            result.entity,
          );
        }
        // ネットワーク/サーバーエラーは、後から積まれた最新操作へ
        // 古い応答のエラー状態を引き継がせない。
        continue;
      }
    }
    if (result.status === "ok") {
      if (result.entity && operation?.table === "projects") {
        await applyRemoteProjects([result.entity as unknown as Project]);
      }
      if (
        operation?.table !== "tasks" ||
        (operation.action !== "delete" && operation.action !== "restore")
      ) {
        await applyTaskPushEntity(
          operation,
          result.entity,
          result.server_updated_at,
        );
      }
      if (result.entity && operation?.table === "time_entries") {
        await applyRemoteTimeEntries([result.entity as unknown as TimeEntry]);
      }
      if (pendingOperation && operation) {
        const removed = await removeOutboxOpIfSnapshot(result.op_id, {
          table: pendingOperation.tableName,
          action: pendingOperation.action,
          entityId: pendingOperation.entityId,
          payload: pendingOperation.payload,
          baseUpdatedAt: pendingOperation.baseUpdatedAt,
        });
        if (!removed) {
          if (isTaskPrerequisite(pendingOperation)) {
            regularResultStatuses.set(result.op_id, "error");
          }
          const currentOperation = await getCurrentOutbox(result.op_id);
          await rebaseChangedDocsOutbox(
            currentOperation ?? pendingOperation,
            result.entity,
            result.server_updated_at,
          );
          continue;
        }
      } else {
        await removeOutboxOp(result.op_id);
      }
      await applyDocsPushEntity(operation?.table, result.entity, authScope);
      continue;
    }

    if (result.status === "conflict") {
      if (pendingOperation && operation?.table?.startsWith("knowledge_")) {
        const resolution = await resolveDocsConflict(
          pendingOperation,
          result.entity,
          result.server_updated_at,
        );
        if (resolution === "applied") {
          const removed = await removeOutboxOpIfSnapshot(result.op_id, {
            table: pendingOperation.tableName,
            action: pendingOperation.action,
            entityId: pendingOperation.entityId,
            payload: pendingOperation.payload,
            baseUpdatedAt: pendingOperation.baseUpdatedAt,
          });
          if (!removed) continue;
          if (isTaskPrerequisite(pendingOperation)) {
            regularResultStatuses.set(result.op_id, "ok");
          }
          await applyDocsPushEntity(operation.table, result.entity, authScope);
          continue;
        }
        if (resolution === "rebased") continue;
        await recordOutboxServerSnapshot(
          operation.table,
          operation.entity_id,
          result.entity ?? { deleted: true },
          authScope,
        );
        await markOutboxConflict(
          result.op_id,
          result.reason ?? result.status,
          result.entity,
        );
        continue;
      }
      if (result.entity && operation?.table === "projects") {
        await applyRemoteProjects([result.entity as unknown as Project]);
      }
      if (
        operation?.table !== "tasks" ||
        (operation.action !== "delete" && operation.action !== "restore")
      ) {
        await applyTaskPushEntity(
          operation,
          result.entity,
          result.server_updated_at,
        );
      }
      if (result.entity && operation?.table === "time_entries") {
        await applyRemoteTimeEntries([result.entity as unknown as TimeEntry]);
      }
      await applyDocsPushEntity(operation?.table, result.entity, authScope);
      await markOutboxConflict(result.op_id, result.reason ?? result.status);
      continue;
    }

    if (result.status === "error") {
      await markOutboxError(result.op_id, result.reason ?? result.status);
    }
  }
  await flushReorderOutbox(
    reorderPending,
    context,
    authScope,
    pending,
    regularResultStatuses,
  );
}

async function performSync(
  authScope: string,
  context: SyncExecutionContext,
): Promise<void> {
  syncExecutionCount += 1;
  const steps: Array<[string, () => Promise<unknown>]> = [
    ["outbox push", () => pushOutbox(authScope, context)],
    // Docs表示の鮮度を、通常tableや保留conversation/clipの失敗へ連鎖させない。
    // ローカルDocs編集のpush後、Docs pullを独立して先に収束させる。
    ["Docs pull", () => applyDocsPull(authScope, context)],
    ["general pull", () => applyGeneralPull(authScope, context)],
    ["pending conversations", flushPendingConversations],
    ["pending clip ingests", flushPendingClipIngests],
    // サーバー未到達時にモバイルLLMだけで取り込めるよう、到達できたときに
    // 取り込み先設定のキャッシュを更新する。TTL 内は内部でスキップされる。
    ["clip ingest targets", refreshClipIngestTargetsIfStale],
    // オフライン中も最後に取得したsystem prompt等を使えるよう、
    // 接続できた同期のたびにサーバーの全プロフィールを更新する。
    // 既存の保留同期を、キャラクターAPIの一時的な断線で止めないため最後に行う。
    ["character profiles", () => characterApi.refreshCache()],
  ];

  for (const [label, operation] of steps) {
    try {
      assertSyncExecutionActive(context);
      await operation();
      assertSyncExecutionActive(context);
    } catch (error) {
      if (error instanceof SyncExecutionInterruptedError) break;
      console.warn(
        `[sync] ${label} failed`,
        error instanceof Error ? error.name : "UnknownError",
      );
      // transport断では後続のAPIも同じ理由で失敗するため、このrunだけ打ち切る。
      // HTTP応答・認証・ローカルDB・個別機能の失敗では他領域の同期を継続する。
      if (isApiConnectionError(error)) break;
    }
  }
}

function runSyncForToken(token: string | null): Promise<void> {
  if (
    !token ||
    !syncExecutionActive ||
    !useNetworkStore.getState().online
  ) {
    return completedSync;
  }

  const authScope = getTokenAuthScope(token);
  const running = runningByAuthScope.get(authScope);
  if (running) {
    if (running.epoch === syncExecutionEpoch) return running.promise;
    return running.promise.then(() => runSyncForToken(token));
  }

  const context = createSyncExecutionContext();
  const flight = enqueueExclusive(async () => {
    try {
      assertSyncExecutionActive(context);
    } catch (error) {
      if (error instanceof SyncExecutionInterruptedError) return;
      throw error;
    }
    // auth transition中に予約された旧scopeの同期は、DBへ触れる前に破棄する。
    if (getTokenAuthScope(getCachedToken()) !== authScope) return;
    await performSync(authScope, context);
  }).finally(() => {
    if (runningByAuthScope.get(authScope)?.promise === flight) {
      runningByAuthScope.delete(authScope);
    }
  });
  runningByAuthScope.set(authScope, { epoch: context.epoch, promise: flight });
  return flight;
}

/**
 * 同じ認証スコープの同期要求を完全なsingle-flightとしてまとめる。
 * 異なるscopeも共有SQLite/outboxを守るため共通queue上で直列実行する。
 */
export function runSync(): Promise<void> {
  syncRequestCount += 1;
  if (!syncExecutionActive || !useNetworkStore.getState().online) {
    return completedSync;
  }

  const token = getCachedToken();
  if (token !== undefined) return runSyncForToken(token);
  if (unresolvedAuthFlight) return unresolvedAuthFlight;

  const tokenLookup = getToken();
  const flight = tokenLookup
    .then((resolvedToken) => runSyncForToken(resolvedToken))
    .finally(() => {
      if (unresolvedAuthFlight === flight) unresolvedAuthFlight = null;
    });
  unresolvedAuthFlight = flight;
  return flight;
}

/**
 * 進行中の同期と排他的に認証scopeを切り替える。
 * callback中に届いたrunSyncは、token/cache更新完了後まで開始されない。
 */
export { runAuthScopeTransition };

export function getSyncDiagnostics(): {
  requestCount: number;
  executionCount: number;
  runningScopes: number;
} {
  return {
    requestCount: syncRequestCount,
    executionCount: syncExecutionCount,
    runningScopes: runningByAuthScope.size + (unresolvedAuthFlight ? 1 : 0),
  };
}

export function resetSyncDiagnostics(): void {
  syncRequestCount = 0;
  syncExecutionCount = 0;
}
