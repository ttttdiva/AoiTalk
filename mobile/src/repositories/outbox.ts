import { acknowledgeLegacyOpInWriter } from "../sync-v3/legacy-migration";
/**
 * Outbox helpers (M1: enqueue-only skeleton).
 *
 * At M1 mutations go straight to the server (existing behaviour). The
 * enqueue helper is in place so M2 can flip writes to Local-First without
 * touching repository call sites. The M2 sync engine will own dequeue,
 * retry, conflict resolution.
 */

import { getDb, schema } from '../db/client';
import type { OutboxEnqueue } from './types';
import { getToken, getTokenAuthScope } from '../lib/auth';
import { asc, eq, and, isNull, like, sql } from 'drizzle-orm';
import type { OutboxSyncState } from '../stores/sync-status';
import { assertActiveWriter, type SqliteHandle, type SqliteWriter } from '../db/sqlite-writer';
import {
  readSyncV3DeviceStateInWriter,
  writeSyncV3DeviceStateInWriter,
} from '../sync/v3/state';

/**
 * Immutable pending-operation snapshot used by every resolution write.
 *
 * A conflict screen can remain open while enqueue merges a newer edit into the
 * same operation id.  The operation id and payload alone therefore do not
 * identify the row that was read.  Keep every nullable lifecycle field in the
 * expected snapshot and turn null into an explicit `IS NULL` predicate below.
 */
export type PendingOutboxSnapshot = {
  opId: string;
  tableName: string;
  action: string;
  entityId: string;
  payload: string;
  baseUpdatedAt: string | null;
  basePayload: unknown | null;
  conflictPayload: unknown | null;
  retryCount: number;
  lastError: string | null;
  blockedReason: string | null;
  authScope: string | null;
  docsScopeKey: string | null;
};

/** A conflict reference is always scoped to a known authenticated account. */
export type OutboxConflictReference = Omit<PendingOutboxSnapshot, 'authScope'> & {
  authScope: string;
};

/**
 * Identity and receipt-state fields captured when an outbox operation is sent.
 *
 * A caller passes this snapshot to a receipt marker so a response for an
 * older payload cannot classify a newer merge under the same operation id, or
 * overwrite a receipt that already advanced the row's mutable state.  The
 * lifecycle and scope fields are required; core-only receipt updates are not
 * safe for conflict resolution.
 */
/** Full lifecycle snapshot excluding the separately supplied operation id. */
export type OutboxMutationSnapshot = Omit<PendingOutboxSnapshot, 'opId'>;

export type OutboxConflictResolutionResult = {
  ok: boolean;
  reason?:
    | 'auth_scope_changed'
    | 'missing_conflict'
    | 'missing_server_snapshot'
    | 'server_scope_missing'
    | 'server_scope_mismatch'
    | 'server_id_mismatch'
    | 'docs_scope_missing'
    | 'docs_scope_mismatch'
    | 'docs_scope_not_writable'
    | 'node_not_writable'
    | 'server_version_missing'
    | 'outbox_replaced';
  serverPayload?: Record<string, unknown>;
};

type OutboxConflictLookup = {
  /** Expected current account scope. A changed token never adopts this row. */
  authScope?: string | null;
  /** Composite Docs library/project identity. Required for Docs conflicts. */
  docsScopeKey?: string | null;
};

/**
 * Resolve the account scope used for an outbox operation.  A missing token is
 * represented as `null` and is intentionally never replayed; all existing
 * mutation call sites enqueue only after a token check.  Keeping legacy NULL
 * rows untouched prevents an unknown old mutation from being attributed to a
 * newly signed-in account.
 */
export async function resolveAuthScope(
  requested?: string | null,
): Promise<string | null> {
  if (requested !== undefined) return requested;
  const token = await getToken();
  return token
    ? typeof getTokenAuthScope === 'function'
      ? getTokenAuthScope(token)
      : null
    : null;
}

/** Resolve a scope while proving that the caller still owns the row. */
async function resolveCurrentAuthScope(
  expected?: string | null,
): Promise<string | null> {
  const token = await getToken();
  const actual = token ? getTokenAuthScope(token) : null;
  if (expected !== undefined && actual !== expected) return null;
  return actual;
}

function parseJsonObject(value: unknown): Record<string, unknown> | null {
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    return value as Record<string, unknown>;
  }
  if (typeof value !== 'string' || !value.trim()) return null;
  try {
    const parsed = JSON.parse(value);
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
      ? parsed as Record<string, unknown>
      : null;
  } catch {
    return null;
  }
}

/**
 * Validate the composite identity carried by a durable server conflict
 * snapshot.  Docs UUIDs are not globally unique: the workspace/library and
 * project metadata are part of the identity.  Never allow a snapshot with a
 * missing or mismatched composite key to reach the local cache or UI.
 */
export function validateOutboxConflictServerScope(
  reference: Pick<OutboxConflictReference, 'entityId' | 'docsScopeKey'>,
  value: unknown,
):
  | { ok: true; payload: Record<string, unknown> }
  | {
      ok: false;
      reason:
        | 'missing_server_snapshot'
        | 'server_scope_missing'
        | 'server_scope_mismatch'
        | 'server_id_mismatch'
        | 'docs_scope_missing';
    } {
  const payload = parseJsonObject(value);
  if (!payload) return { ok: false, reason: 'missing_server_snapshot' };
  if (!reference.docsScopeKey) {
    return { ok: false, reason: 'docs_scope_missing' };
  }

  const hasWorkspaceSnake = Object.prototype.hasOwnProperty.call(payload, 'workspace_id');
  const hasWorkspaceCamel = Object.prototype.hasOwnProperty.call(payload, 'workspaceId');
  const workspaceValue = hasWorkspaceSnake ? payload.workspace_id : payload.workspaceId;
  const hasProjectSnake = Object.prototype.hasOwnProperty.call(payload, 'project_id');
  const hasProjectCamel = Object.prototype.hasOwnProperty.call(payload, 'projectId');
  const projectValue = hasProjectSnake ? payload.project_id : payload.projectId;
  if (
    (!hasWorkspaceSnake && !hasWorkspaceCamel)
    || typeof workspaceValue !== 'string'
    || !workspaceValue.trim()
    || (!hasProjectSnake && !hasProjectCamel)
    || projectValue === undefined
    || (projectValue !== null && projectValue !== undefined && typeof projectValue !== 'string')
  ) {
    return { ok: false, reason: 'server_scope_missing' };
  }

  // If both aliases are supplied, they must agree.  Accepting one alias is
  // useful for older rolling clients, but silently preferring conflicting
  // metadata would permit a cross-scope adoption.
  if (
    hasWorkspaceSnake
    && hasWorkspaceCamel
    && String(payload.workspace_id) !== String(payload.workspaceId)
  ) {
    return { ok: false, reason: 'server_scope_mismatch' };
  }
  if (
    hasProjectSnake
    && hasProjectCamel
    && (projectValue ?? null) !== (payload.projectId ?? null)
  ) {
    return { ok: false, reason: 'server_scope_mismatch' };
  }

  const project = projectValue == null ? '' : String(projectValue);
  const actualScopeKey = `${String(workspaceValue)}|project:${project}`;
  if (actualScopeKey !== reference.docsScopeKey) {
    return { ok: false, reason: 'server_scope_mismatch' };
  }

  if (
    !Object.prototype.hasOwnProperty.call(payload, 'id')
    || typeof payload.id !== 'string'
    || payload.id !== reference.entityId
  ) {
    return { ok: false, reason: 'server_id_mismatch' };
  }
  return { ok: true, payload };
}

function serverUpdatedAt(payload: Record<string, unknown>): string | null {
  const value = payload.updated_at ?? payload.updatedAt;
  return typeof value === 'string' && value.trim() ? value : null;
}

function snapshotReference(row: OutboxSnapshotRow): OutboxConflictReference | null {
  const snapshot = pendingSnapshot(row);
  if (!snapshot.authScope) return null;
  return snapshot as OutboxConflictReference;
}

function scopePredicate(
  authScope: string | null,
): ReturnType<typeof eq> | ReturnType<typeof isNull> {
  return authScope === null
    ? isNull(schema.outbox.authScope)
    : eq(schema.outbox.authScope, authScope);
}

type OutboxSnapshotRow = {
  opId: string;
  tableName: string;
  action: string;
  entityId: string;
  payload: string;
  baseUpdatedAt: string | null;
  authScope: string | null;
  docsScopeKey: string | null;
  basePayload?: unknown;
  conflictPayload?: unknown;
  retryCount?: number;
  lastError?: string | null;
  blockedReason?: string | null;
};

function pendingSnapshot(row: OutboxSnapshotRow): PendingOutboxSnapshot {
  return {
    opId: row.opId,
    tableName: row.tableName,
    action: row.action,
    entityId: row.entityId,
    payload: row.payload,
    baseUpdatedAt: row.baseUpdatedAt ?? null,
    basePayload: row.basePayload ?? null,
    conflictPayload: row.conflictPayload ?? null,
    retryCount: Number(row.retryCount ?? 0),
    lastError: row.lastError ?? null,
    blockedReason: row.blockedReason ?? null,
    authScope: row.authScope ?? null,
    docsScopeKey: row.docsScopeKey ?? null,
  };
}

function inferDocsScopeKey(op: OutboxEnqueue): string | null {
  if (!op.table.startsWith('knowledge_')) return null;
  const payload = op.payload && typeof op.payload === 'object'
    ? op.payload as Record<string, unknown>
    : null;
  const workspaceId = payload?.workspace_id ?? payload?.workspaceId;
  const projectId = payload?.project_id ?? payload?.projectId;
  if (workspaceId) {
    return `${String(workspaceId)}|project:${projectId ? String(projectId) : ''}`;
  }
  return null;
}

export function randomId(): string {
  // Lightweight uuid v4 generator (no dependency).
  // Sync engine only needs uniqueness, not crypto strength.
  // NOTE: サーバ側は正規の RFC4122 形式 (8-4-4-4-12) を前提に ID を解決する
  // ため、必ず標準形式（version 4 / variant 10xx）で生成する。
  const hex: string[] = [];
  for (let i = 0; i < 32; i += 1) {
    hex.push(Math.floor(Math.random() * 16).toString(16));
  }
  hex[12] = '4'; // version 4
  hex[16] = ((parseInt(hex[16], 16) & 0x3) | 0x8).toString(16); // variant 10xx
  const s = hex.join('');
  return `${s.slice(0, 8)}-${s.slice(8, 12)}-${s.slice(12, 16)}-${s.slice(16, 20)}-${s.slice(20)}`;
}

export async function enqueueOutbox(op: OutboxEnqueue): Promise<string> {
  const db = getDb();
  const authScope = await resolveAuthScope(op.authScope);
  let inferredDocsScopeKey = op.docsScopeKey ?? inferDocsScopeKey(op);
  // Updates/deletes often carry only a composite entity id.  Resolve the
  // scope from persisted membership when exactly one active scope owns it;
  // ambiguity remains NULL and is quarantined conservatively on revocation.
  if (!inferredDocsScopeKey && authScope && schema.docsScopeMembership) {
    try {
      const memberships = await db
        .select({ scopeKey: schema.docsScopeMembership.scopeKey })
        .from(schema.docsScopeMembership)
        .where(
          and(
            eq(schema.docsScopeMembership.authScope, authScope),
            eq(schema.docsScopeMembership.tableName, op.table),
            eq(schema.docsScopeMembership.entityKey, op.entityId),
            eq(schema.docsScopeMembership.state, 'active'),
          ),
        );
      const keys = [...new Set(memberships.map((row) => row.scopeKey))];
      if (keys.length === 1) inferredDocsScopeKey = keys[0];
    } catch {
      // Rolling databases/test doubles may not have the membership table yet.
    }
  }
  const ambiguousDocsScope = op.table.startsWith('knowledge_') && !inferredDocsScopeKey;
  // Without a verified composite key this operation cannot be attributed to
  // one of two sibling scopes that share an entity UUID.  Do not merge it
  // into an ambiguous legacy NULL row; retaining separate operations is the
  // fail-closed choice and lets a later membership migration resolve them.
  const existing = op.table.startsWith('knowledge_') && !inferredDocsScopeKey
    ? []
    : await db
        .select()
        .from(schema.outbox)
        .where(
          and(
            scopePredicate(authScope),
            eq(schema.outbox.tableName, op.table),
            eq(schema.outbox.entityId, op.entityId),
            op.table.startsWith('knowledge_')
              ? inferredDocsScopeKey
                ? eq(schema.outbox.docsScopeKey, inferredDocsScopeKey)
                : undefined
              : undefined,
          ),
        )
        .orderBy(asc(schema.outbox.createdAt));
  const mergeable = existing[existing.length - 1];
  const docsScopePatch = inferredDocsScopeKey
    ? { docsScopeKey: inferredDocsScopeKey }
    : {};
  if (
    mergeable &&
    (mergeable.action === 'update' || mergeable.action === 'create') &&
    (op.action === 'update' || op.action === 'create')
  ) {
    const previous = JSON.parse(mergeable.payload || '{}') as Record<string, unknown>;
    const next = op.payload && typeof op.payload === 'object'
      ? { ...previous, ...(op.payload as Record<string, unknown>) }
      : previous;
    await db
      .update(schema.outbox)
      .set({
        payload: JSON.stringify(next),
        lastError: null,
        retryCount: 0,
        conflictPayload: null,
        ...docsScopePatch,
      })
      .where(eq(schema.outbox.opId, mergeable.opId));
    return mergeable.opId;
  }
  if (mergeable && mergeable.action === 'reorder' && op.action === 'reorder') {
    // Reorder is a first-class operation: retain only the latest canonical
    // order for this account + table/entity scope.
    await db
      .update(schema.outbox)
      .set({
        payload: JSON.stringify(op.payload ?? {}),
        lastError: null,
        retryCount: 0,
        conflictPayload: null,
        ...docsScopePatch,
      })
      .where(eq(schema.outbox.opId, mergeable.opId));
    return mergeable.opId;
  }
  if (
    mergeable &&
    op.action === 'delete' &&
    mergeable.action === 'update' &&
    op.table === 'knowledge_nodes'
  ) {
    const previousPayload = JSON.parse(mergeable.payload || '{}') as Record<string, unknown>;
    const deletePayload = op.payload && typeof op.payload === 'object'
      ? { ...previousPayload, ...(op.payload as Record<string, unknown>) }
      : previousPayload;
    await db
      .update(schema.outbox)
      .set({
        action: 'delete',
        payload: JSON.stringify(deletePayload),
        ...docsScopePatch,
      })
      .where(eq(schema.outbox.opId, mergeable.opId));
    return mergeable.opId;
  }
  if (
    mergeable &&
    op.action === 'delete' &&
    mergeable.action === 'create' &&
    op.table === 'knowledge_node_supertags'
  ) {
    // 同期前に追加してから削除した関連は、create を消さず delete へ変換する。
    // create の送信中に物理削除すると、create の成功応答後にサーバーだけ関連が
    // 残るため、同じ operation_id の行を残して応答後に必ず delete を送る。
    await db
      .update(schema.outbox)
      .set({
        action: 'delete',
        payload: JSON.stringify(op.payload ?? {}),
        lastError: null,
        retryCount: 0,
        conflictPayload: null,
        ...docsScopePatch,
      })
      .where(eq(schema.outbox.opId, mergeable.opId));
    return mergeable.opId;
  }
  const opId = randomId();
  await db.insert(schema.outbox).values({
    opId,
    createdAt: Date.now(),
    tableName: op.table,
    action: op.action,
    entityId: op.entityId,
    payload: JSON.stringify(op.payload ?? {}),
    authScope,
    baseUpdatedAt: op.baseUpdatedAt ?? null,
    basePayload: op.basePayload ?? null,
    conflictPayload: null,
    retryCount: 0,
    lastError: null,
    docsScopeKey: inferredDocsScopeKey,
    // A new Docs mutation without a verified composite identity must never be
    // replayed into whichever sibling project happens to share its UUID.
    // Migration can later resolve this row from membership/payload and clear
    // the block through an explicit unblock operation.
    blockedReason: ambiguousDocsScope ? 'docs_scope_ambiguous' : null,
  });
  return opId;
}

/**
 * Capture wake routing from a successfully saved operation, never a fresh
 * token read. Call inside the caller's foreground SQLite unit, then publish
 * the returned scope only after that whole unit and required post-reads pass.
 * This hint must not turn a completed local edit into a new failure.
 */
export async function getOutboxMutationWakeScope(opId: string): Promise<string | null> {
  try {
    const [row] = await getDb()
      .select({ authScope: schema.outbox.authScope, blockedReason: schema.outbox.blockedReason })
      .from(schema.outbox)
      .where(eq(schema.outbox.opId, opId))
      .limit(1);
    return row?.authScope && row.blockedReason == null ? row.authScope : null;
  } catch {
    // Do not log the token, actor, operation id, payload, or database error.
    console.warn('[outbox] Mutation wake metadata unavailable; normal sync will retry');
    return null;
  }
}

export async function listPendingOutbox(
  limit?: number,
  requestedAuthScope?: string | null,
) {
  const db = getDb();
  const authScope = await resolveAuthScope(requestedAuthScope);
  // No token means no authenticated mutation may be sent.  In particular,
  // legacy NULL rows must not be adopted by an anonymous/new account.
  if (authScope === null) return [];
  const query = db
    .select()
    .from(schema.outbox)
    .where(scopePredicate(authScope))
    .orderBy(asc(schema.outbox.createdAt));
  const rows = limit ? await query.limit(limit) : await query;
  return rows.filter(
    (row) =>
      row.authScope === authScope &&
      row.blockedReason == null &&
      // Older task conflicts have no captured entity: allow one reconciliation
      // read, then retain unresolved updates without resending indefinitely.
      !(row.tableName === 'tasks' && (row.lastError ?? '').startsWith('conflict:')
        && (row.action !== 'update' || row.conflictPayload != null)) &&
      (row.retryCount < 5 ||
        (row.lastError ?? '').startsWith('conflict:')),
  );
}

/**
 * Read the durable state for the currently authenticated scope, including
 * blocked/terminal rows that are intentionally excluded from the send list.
 * A completed flight is only a successful sync when this state is empty.
 */
export async function getOutboxState(
  requestedAuthScope?: string | null,
): Promise<OutboxSyncState> {
  const db = getDb();
  const authScope = await resolveCurrentAuthScope(requestedAuthScope);
  if (authScope === null) return 'empty';
  const rows = await db
    .select()
    .from(schema.outbox)
    .where(scopePredicate(authScope));
  const scopedRows = rows.filter((row) => row.authScope === authScope);
  if (scopedRows.length === 0) return 'empty';
  if (scopedRows.some((row) =>
    row.blockedReason != null
    || row.retryCount >= 5
    || (row.lastError ?? '').startsWith('conflict:'),
  )) {
    return 'failed';
  }
  if (scopedRows.some((row) => row.retryCount > 0 || row.lastError != null)) {
    return 'retrying';
  }
  return 'pending';
}

export type OutboxDiagnostic = {
  opId: string;
  tableName: string;
  action: string;
  entityId: string;
  /** Composite Docs scope, when the operation carries one. */
  docsScopeKey: string | null;
  /** Base/server snapshot metadata only; payloads remain private. */
  baseUpdatedAt: string | null;
  hasBaseSnapshot: boolean;
  hasConflictSnapshot: boolean;
  retryCount: number;
  blockedReason: string | null;
  errorKind: 'conflict' | 'scope' | 'missing_base' | 'missing_entity' | 'server_error' | 'pending';
};

/**
 * One exact outbox operation for the generic resolution facade.  This is an
 * internal read boundary: payload and lifecycle error fields are retained for
 * private CAS/journal capture, while the public diagnostics DTO below never
 * exposes them.
 */
export type OutboxResolutionReadSnapshot = PendingOutboxSnapshot & {
  createdAt: number;
};

/** Read one operation by id and authenticated scope, with no queue mutation. */
export async function getOutboxResolutionReadSnapshot(
  opId: string,
  expectedAuthScope: string,
): Promise<OutboxResolutionReadSnapshot | null> {
  const normalizedOpId = typeof opId === 'string' ? opId.trim() : '';
  const authScope = await resolveCurrentAuthScope(expectedAuthScope);
  if (!normalizedOpId || !authScope) return null;
  const rows = await getDb()
    .select()
    .from(schema.outbox)
    .where(and(eq(schema.outbox.opId, normalizedOpId), scopePredicate(authScope)))
    .limit(1);
  if (await resolveCurrentAuthScope(authScope) !== authScope) return null;
  const row = rows[0];
  if (!row || row.opId !== normalizedOpId || row.authScope !== authScope) return null;
  const createdAt = Number(row.createdAt);
  if (!Number.isFinite(createdAt)) return null;
  return {
    opId: row.opId,
    tableName: row.tableName,
    action: row.action,
    entityId: row.entityId,
    payload: row.payload,
    baseUpdatedAt: row.baseUpdatedAt ?? null,
    basePayload: row.basePayload ?? null,
    conflictPayload: row.conflictPayload ?? null,
    retryCount: Number(row.retryCount ?? 0),
    lastError: row.lastError ?? null,
    blockedReason: row.blockedReason ?? null,
    authScope,
    docsScopeKey: row.docsScopeKey ?? null,
    createdAt,
  };
}

/** Read operation metadata only, without payloads, receipts or credentials. */
export async function getOutboxDiagnostics(
  expectedAuthScope: string,
): Promise<OutboxDiagnostic[]> {
  const authScope = await resolveCurrentAuthScope(expectedAuthScope);
  if (authScope === null) return [];
  const rows = await getDb().select({
    opId: schema.outbox.opId,
    authScope: schema.outbox.authScope,
    tableName: schema.outbox.tableName,
    action: schema.outbox.action,
    entityId: schema.outbox.entityId,
    docsScopeKey: schema.outbox.docsScopeKey,
    baseUpdatedAt: schema.outbox.baseUpdatedAt,
    hasBaseSnapshot: sql<number>`CASE WHEN ${schema.outbox.basePayload} IS NULL THEN 0 ELSE 1 END`,
    hasConflictSnapshot: sql<number>`CASE WHEN ${schema.outbox.conflictPayload} IS NULL THEN 0 ELSE 1 END`,
    retryCount: schema.outbox.retryCount,
    blockedReason: schema.outbox.blockedReason,
    lastError: schema.outbox.lastError,
  }).from(schema.outbox).where(scopePredicate(authScope)).orderBy(asc(schema.outbox.createdAt));
  // A token can change while SQLite is waiting behind a foreground write.
  if (await resolveCurrentAuthScope(authScope) !== authScope) return [];
  return rows.filter((row) => row.authScope === authScope).map((row) => {
    const error = row.lastError ?? '';
    const errorKind: OutboxDiagnostic['errorKind'] = row.blockedReason != null || error.includes('invalid_ack_scope')
      ? 'scope'
      : /base_updated_at|base version|(?:docs|task|sync)_base_missing|base_missing|基準.*版|基準.*更新/i.test(error)
        ? 'missing_base'
        : error.startsWith('conflict:') ? 'conflict'
        : row.tableName === 'tasks' && error === 'Task not found' ? 'missing_entity'
        : error ? 'server_error' : 'pending';
    return { opId: row.opId, tableName: row.tableName, action: row.action,
      entityId: row.entityId, retryCount: row.retryCount,
      docsScopeKey: row.docsScopeKey ?? null,
      baseUpdatedAt: row.baseUpdatedAt ?? null,
      hasBaseSnapshot: Number(row.hasBaseSnapshot ?? 0) !== 0,
      hasConflictSnapshot: Number(row.hasConflictSnapshot ?? 0) !== 0,
      blockedReason: row.blockedReason, errorKind };
  });
}

export async function hasPendingOutbox(
  table: string,
  entityId: string,
  requestedAuthScope?: string | null,
): Promise<boolean> {
  const db = getDb();
  const authScope = await resolveAuthScope(requestedAuthScope);
  if (authScope === null) return false;
  const rows = await db
    .select({ opId: schema.outbox.opId })
    .from(schema.outbox)
    .where(
      and(
        scopePredicate(authScope),
        eq(schema.outbox.tableName, table),
        eq(schema.outbox.entityId, entityId),
      ),
    )
    .limit(1);
  return rows.length > 0;
}

/**
 * Synchronous form of `hasPendingOutbox` for a caller already inside a
 * coordinator write unit. The auth scope is resolved by the caller (see
 * `resolveAuthScope`); `null` means "no signed-in actor" and is never pending.
 */
export function hasPendingOutboxInWriter(
  w: SqliteWriter,
  tx: SqliteHandle,
  table: string,
  entityId: string,
  authScope: string | null,
): boolean {
  assertActiveWriter(w);
  if (authScope === null) return false;
  const rows = tx
    .select({ opId: schema.outbox.opId })
    .from(schema.outbox)
    .where(
      and(
        scopePredicate(authScope),
        eq(schema.outbox.tableName, table),
        eq(schema.outbox.entityId, entityId),
      ),
    )
    .limit(1)
    .all();
  return rows.length > 0;
}

export async function recordOutboxServerSnapshot(
  table: string,
  entityId: string,
  payload: unknown,
  requestedAuthScope?: string | null,
  requestedDocsScopeKey?: string | null,
): Promise<void> {
  const db = getDb();
  const authScope = await resolveCurrentAuthScope(requestedAuthScope);
  if (authScope === null) return;
  attachOutboxServerSnapshot(
    db,
    authScope,
    table,
    entityId,
    payload,
    requestedDocsScopeKey,
  );
}

/**
 * Synchronous form of `recordOutboxServerSnapshot` for a caller already inside
 * a coordinator write unit. The auth scope is resolved by the caller (token
 * reads are asynchronous) and `null` means "no signed-in actor": no-op.
 */
export function recordOutboxServerSnapshotInWriter(
  w: SqliteWriter,
  tx: SqliteHandle,
  authScope: string | null,
  table: string,
  entityId: string,
  payload: unknown,
  requestedDocsScopeKey?: string | null,
): void {
  assertActiveWriter(w);
  if (authScope === null) return;
  attachOutboxServerSnapshot(
    tx,
    authScope,
    table,
    entityId,
    payload,
    requestedDocsScopeKey,
  );
}

function attachOutboxServerSnapshot(
  db: SqliteHandle,
  authScope: string,
  table: string,
  entityId: string,
  payload: unknown,
  requestedDocsScopeKey?: string | null,
): void {
  const inferredScopeKey = requestedDocsScopeKey
    ?? (table.startsWith('knowledge_') ? inferDocsScopeKeyFromPayload(payload) : null);
  const rows = db
    .select()
    .from(schema.outbox)
    .where(and(
      scopePredicate(authScope),
      eq(schema.outbox.tableName, table),
      eq(schema.outbox.entityId, entityId),
      ...(inferredScopeKey
        ? [eq(schema.outbox.docsScopeKey, inferredScopeKey)]
        : []),
    ))
    .orderBy(asc(schema.outbox.createdAt))
    .all();
  // The helper has no opId.  Even a verified composite scope cannot identify
  // one row when multiple operations for the same entity are pending; keep
  // every candidate untouched rather than attaching the snapshot to the
  // oldest operation by accident.  A caller with an exact operation snapshot
  // must perform the scoped CAS itself.
  const row = rows.length === 1 ? rows[0] : null;
  if (!row) return;
  if (table === 'knowledge_nodes') {
    const reference = snapshotReference({
      ...row,
      tableName: table,
      entityId,
      // The snapshot is attached before conflict marker promotion.  These
      // fields are not used for scope validation but are required by the
      // reference shape and are intentionally sourced from the durable row.
      action: row.action,
      payload: row.payload,
      baseUpdatedAt: row.baseUpdatedAt,
      conflictPayload: payload,
    });
    if (!reference || !validateOutboxConflictServerScope(reference, payload).ok) {
      return;
    }
  }
  const expected = pendingSnapshot(row);
  db
    .update(schema.outbox)
    .set({ conflictPayload: payload as never })
    .where(outboxMutationSnapshotCondition(row.opId, expected))
    .run();
}

export async function rebaseOutboxOp(
  opId: string,
  baseUpdatedAt: string | null,
  basePayload: unknown,
  expected: PendingOutboxSnapshot,
): Promise<boolean> {
  if (expected.opId !== opId) return false;
  const db = getDb();
  const result = await db
    .update(schema.outbox)
    .set({
      baseUpdatedAt,
      basePayload: basePayload as never,
      conflictPayload: null,
      lastError: null,
      retryCount: 0,
    })
    .where(and(
      outboxMutationSnapshotCondition(opId, expected),
      // A blocked row requires its scope recovery workflow; rebase must not
      // clear a terminal failure merely because the snapshot still matches.
      isNull(schema.outbox.blockedReason),
    ))
    .run();
  return Number(result?.changes ?? 0) > 0;
}

function sameConflictReference(
  current: OutboxConflictReference | null,
  expected: OutboxConflictReference,
): boolean {
  return Boolean(
    current
      && current.opId === expected.opId
      && current.tableName === expected.tableName
      && current.action === expected.action
      && current.entityId === expected.entityId
      && current.payload === expected.payload
      && current.baseUpdatedAt === expected.baseUpdatedAt
      && JSON.stringify(current.basePayload ?? null)
        === JSON.stringify(expected.basePayload ?? null)
      && JSON.stringify(current.conflictPayload ?? null)
        === JSON.stringify(expected.conflictPayload ?? null)
      && current.retryCount === expected.retryCount
      && current.lastError === expected.lastError
      && current.blockedReason === expected.blockedReason
      && current.authScope === expected.authScope
      && current.docsScopeKey === expected.docsScopeKey
  );
}

/**
 * Read one conflict for the current account and (for Docs) one composite
 * library/project scope.  A conflict without a durable server snapshot is
 * intentionally not returned: the UI cannot offer a safe resolution for it.
 */
export async function getOutboxConflict(
  table: string,
  entityId: string,
  options: OutboxConflictLookup = {},
): Promise<OutboxConflictReference | null> {
  const authScope = await resolveCurrentAuthScope(options.authScope);
  if (!authScope) return null;
  // A UUID can be shared by sibling project scopes.  Do not guess which one
  // this screen belongs to when the caller has not supplied its composite key.
  if (table.startsWith('knowledge_') && !options.docsScopeKey) return null;
  const db = getDb();
  const conditions = [
    eq(schema.outbox.authScope, authScope),
    eq(schema.outbox.tableName, table),
    eq(schema.outbox.entityId, entityId),
    like(schema.outbox.lastError, 'conflict:%'),
  ];
  if (options.docsScopeKey !== undefined) {
    conditions.push(
      options.docsScopeKey === null
        ? isNull(schema.outbox.docsScopeKey)
        : eq(schema.outbox.docsScopeKey, options.docsScopeKey),
    );
  }
  const rows = await db
    .select()
    .from(schema.outbox)
    .where(and(...conditions));
  // This lookup has no opId.  If multiple conflicts share the same entity and
  // scope, returning the first row would let a UI resolve the wrong pending
  // operation.  Require a unique candidate and let the caller refresh with an
  // exact operation reference instead.
  if (rows.length !== 1) return null;
  for (const row of rows) {
    if (row.authScope !== authScope) continue;
    if (
      options.docsScopeKey !== undefined
      && row.docsScopeKey !== options.docsScopeKey
    ) continue;
    const reference = snapshotReference(row);
    const server = reference ? parseJsonObject(reference.conflictPayload) : null;
    if (!reference || !server) continue;
    if (table === 'knowledge_nodes') {
      const validation = validateOutboxConflictServerScope(reference, server);
      if (!validation.ok) continue;
    } else if (
      table.startsWith('knowledge_')
      && typeof server.id === 'string'
      && server.id !== entityId
    ) continue;
    return {
      ...reference,
      conflictPayload: server,
    };
  }
  return null;
}

function inferDocsScopeKeyFromPayload(payload: unknown): string | null {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return null;
  const value = payload as Record<string, unknown>;
  const workspaceId = value.workspace_id
    ?? value.workspaceId
    ?? value.docs_library_id
    ?? value.docsLibraryId;
  const projectId = value.project_id ?? value.projectId;
  if (!workspaceId) return null;
  return `${String(workspaceId)}|project:${projectId ? String(projectId) : ''}`;
}

/** Verify current ACL and local writable state before resolving a conflict. */
async function conflictWritableReason(
  reference: OutboxConflictReference,
): Promise<OutboxConflictResolutionResult['reason'] | undefined> {
  if (!reference.docsScopeKey) return 'docs_scope_missing';
  if (!schema.docsScopeMembership) return 'docs_scope_missing';
  const db = getDb();
  try {
    const membershipRows = await db
      .select({
        state: schema.docsScopeMembership.state,
        access: schema.docsScopeMembership.access,
        readOnly: schema.docsScopeMembership.readOnly,
      })
      .from(schema.docsScopeMembership)
      .where(and(
        eq(schema.docsScopeMembership.authScope, reference.authScope),
        eq(schema.docsScopeMembership.scopeKey, reference.docsScopeKey),
        eq(schema.docsScopeMembership.tableName, reference.tableName),
        eq(schema.docsScopeMembership.entityKey, reference.entityId),
      ));
    const writableMembership = membershipRows.some((row) =>
      row.state === 'active'
      && row.access !== 'read'
      && row.readOnly !== true,
    );
    if (!writableMembership) return 'docs_scope_not_writable';

    if (reference.tableName === 'knowledge_nodes') {
      const nodeRows = await db
        .select({
          access: schema.knowledgeNodes.access,
          readOnly: schema.knowledgeNodes.readOnly,
          workspaceId: schema.knowledgeNodes.workspaceId,
          projectId: schema.knowledgeNodes.projectId,
          archivedAt: schema.knowledgeNodes.archivedAt,
          systemKey: schema.knowledgeNodes.systemKey,
        })
        .from(schema.knowledgeNodes)
        .where(eq(schema.knowledgeNodes.id, reference.entityId));
      const node = nodeRows[0];
      if (!node) return 'node_not_writable';
      const nodeScopeKey = node.workspaceId
        ? `${node.workspaceId}|project:${node.projectId ?? ''}`
        : null;
      if (!nodeScopeKey || nodeScopeKey !== reference.docsScopeKey) {
        return 'docs_scope_mismatch';
      }
      const systemKey = typeof node.systemKey === 'string' ? node.systemKey.trim() : '';
      if (
        node.readOnly === true
        || node.access === 'read'
        || node.archivedAt != null
        || systemKey === 'project_information_root'
        || systemKey.startsWith('project_information:')
      ) {
        return 'node_not_writable';
      }
    }
  } catch {
    // Missing/partially migrated ACL state is not a license to resolve a row.
    return 'docs_scope_missing';
  }
  return undefined;
}

async function loadConflictForResolution(
  reference: OutboxConflictReference,
): Promise<{
  current: OutboxConflictReference;
  server: Record<string, unknown>;
} | { reason: OutboxConflictResolutionResult['reason'] }> {
  const currentAuthScope = await resolveCurrentAuthScope(reference.authScope);
  if (!currentAuthScope) return { reason: 'auth_scope_changed' };
  // Read by operation id first so malformed scope metadata can return an
  // explicit fail-closed reason rather than being mistaken for a replacement.
  const db = getDb();
  const rows = await db
    .select()
    .from(schema.outbox)
    .where(eq(schema.outbox.opId, reference.opId));
  const row = rows[0];
  const current = row ? snapshotReference(row) : null;
  if (
    !sameConflictReference(current, reference)
    || typeof row?.lastError !== 'string'
    || !row.lastError.startsWith('conflict:')
  ) {
    return { reason: 'outbox_replaced' };
  }
  const server = parseJsonObject(current?.conflictPayload);
  if (!server) return { reason: 'missing_server_snapshot' };
  if (reference.tableName === 'knowledge_nodes') {
    const validation = validateOutboxConflictServerScope(reference, server);
    if (!validation.ok) return { reason: validation.reason };
  }
  const writableReason = await conflictWritableReason(reference);
  if (writableReason) return { reason: writableReason };
  return { current: current!, server };
}

/**
 * Keep the device payload, but make the server snapshot/version its new base.
 * This is the explicit "re-apply device edit" action; no local field is
 * overwritten and no wall-clock comparison is involved.
 */
export async function rebaseOutboxConflict(
  reference: OutboxConflictReference,
): Promise<OutboxConflictResolutionResult> {
  const loaded = await loadConflictForResolution(reference);
  if ('reason' in loaded) return { ok: false, reason: loaded.reason };
  const version = serverUpdatedAt(loaded.server);
  if (!version) return { ok: false, reason: 'server_version_missing' };
  const db = getDb();
  const result = await db
    .update(schema.outbox)
    .set({
      baseUpdatedAt: version,
      basePayload: loaded.server as never,
      conflictPayload: null,
      lastError: null,
      retryCount: 0,
    })
    .where(and(
      outboxMutationSnapshotCondition(reference.opId, reference),
      like(schema.outbox.lastError, 'conflict:%'),
      isNull(schema.outbox.blockedReason),
    ))
    .run();
  return Number(result.changes ?? 0) > 0
    ? { ok: true, serverPayload: loaded.server }
    : { ok: false, reason: 'outbox_replaced' };
}

export async function listOutboxConflicts(
  requestedAuthScope?: string | null,
) {
  const db = getDb();
  // A caller-supplied scope is only a claim; prove it still matches the
  // current token before exposing any conflict payload to the UI.
  const authScope = await resolveCurrentAuthScope(requestedAuthScope);
  if (authScope === null) return [];
  const rows = await db
    .select()
    .from(schema.outbox)
    .where(
      and(
        scopePredicate(authScope),
        like(schema.outbox.lastError, 'conflict:%'),
      ),
    )
    .orderBy(asc(schema.outbox.createdAt));
  return rows.filter((row) => {
    if (row.tableName !== 'knowledge_nodes') return true;
    const reference = snapshotReference(row);
    if (!reference) return false;
    return validateOutboxConflictServerScope(reference, row.conflictPayload).ok;
  });
}

export async function removeOutboxOp(opId: string): Promise<void> {
  const db = getDb();
  await db.delete(schema.outbox).where(eq(schema.outbox.opId, opId));
}

export async function removeOutboxOpIfSnapshot(
  opId: string,
  snapshot: PendingOutboxSnapshot,
  guard: {
    authScope?: string | null;
    docsScopeKey?: string | null;
    requireConflict?: boolean;
  } = {},
): Promise<boolean> {
  if (snapshot.opId !== opId) return false;
  const db = getDb();
  const conditions = [outboxMutationSnapshotCondition(opId, snapshot)];
  if (guard.authScope !== undefined) {
    conditions.push(scopePredicate(guard.authScope));
  }
  if (guard.docsScopeKey !== undefined) {
    conditions.push(
      guard.docsScopeKey === null
        ? isNull(schema.outbox.docsScopeKey)
        : eq(schema.outbox.docsScopeKey, guard.docsScopeKey),
    );
  }
  if (guard.requireConflict) {
    conditions.push(like(schema.outbox.lastError, 'conflict:%'));
  }
  const result = await db
    .delete(schema.outbox)
    .where(and(...conditions))
    .run();
  return Number(result.changes ?? 0) > 0;
}

/** Synchronous form of `removeOutboxOpIfSnapshot` for a caller inside a write unit. */
export function removeOutboxOpIfSnapshotInWriter(
  w: SqliteWriter,
  tx: SqliteHandle,
  opId: string,
  snapshot: PendingOutboxSnapshot,
  guard: {
    authScope?: string | null;
    docsScopeKey?: string | null;
    requireConflict?: boolean;
  } = {},
): boolean {
  assertActiveWriter(w);
  if (snapshot.opId !== opId) return false;
  const conditions = [outboxMutationSnapshotCondition(opId, snapshot)];
  if (guard.authScope !== undefined) {
    conditions.push(scopePredicate(guard.authScope));
  }
  if (guard.docsScopeKey !== undefined) {
    conditions.push(
      guard.docsScopeKey === null
        ? isNull(schema.outbox.docsScopeKey)
        : eq(schema.outbox.docsScopeKey, guard.docsScopeKey),
    );
  }
  if (guard.requireConflict) {
    conditions.push(like(schema.outbox.lastError, 'conflict:%'));
  }
  const result = tx
    .delete(schema.outbox)
    .where(and(...conditions))
    .run();
  const removed = Number(result.changes ?? 0) > 0;
  if (removed) acknowledgeLegacyOpInWriter(w, tx, snapshot as unknown as Record<string, unknown>);
  return removed;
}

/**
 * Build the one full-lifecycle predicate used by every resolution write.
 *
 * Do not make any field optional here.  A caller that only has the historic
 * core identity cannot prove that it is still changing the row it read.
 */
export function outboxMutationSnapshotCondition(
  opId: string,
  expected: OutboxMutationSnapshot,
) {
  const conditions = [
    eq(schema.outbox.opId, opId),
  ];
  conditions.push(
    eq(schema.outbox.tableName, expected.tableName),
    eq(schema.outbox.action, expected.action),
    eq(schema.outbox.entityId, expected.entityId),
    eq(schema.outbox.payload, expected.payload),
    expected.baseUpdatedAt == null
      ? isNull(schema.outbox.baseUpdatedAt)
      : eq(schema.outbox.baseUpdatedAt, expected.baseUpdatedAt),
    expected.basePayload == null
      ? isNull(schema.outbox.basePayload)
      : eq(schema.outbox.basePayload, expected.basePayload as never),
    expected.conflictPayload == null
      ? isNull(schema.outbox.conflictPayload)
      : eq(schema.outbox.conflictPayload, expected.conflictPayload as never),
    eq(schema.outbox.retryCount, expected.retryCount),
    expected.lastError == null
      ? isNull(schema.outbox.lastError)
      : eq(schema.outbox.lastError, expected.lastError),
    expected.blockedReason == null
      ? isNull(schema.outbox.blockedReason)
      : eq(schema.outbox.blockedReason, expected.blockedReason),
    expected.authScope == null
      ? isNull(schema.outbox.authScope)
      : eq(schema.outbox.authScope, expected.authScope),
    expected.docsScopeKey == null
      ? isNull(schema.outbox.docsScopeKey)
      : eq(schema.outbox.docsScopeKey, expected.docsScopeKey),
  );
  return and(...conditions);
}

// ---------- Sync v3 send identity ----------

type OutboxRow = typeof schema.outbox.$inferSelect;

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`;
  if (value && typeof value === 'object') {
    const record = value as Record<string, unknown>;
    return `{${Object.keys(record).sort()
      .filter((key) => record[key] !== undefined)
      .map((key) => `${JSON.stringify(key)}:${canonicalJson(record[key])}`).join(',')}}`;
  }
  return JSON.stringify(value ?? null);
}

/** 2 x 53-bit string hash (cyrb53, two seeds). Only change detection, not security. */
function digestString(text: string): string {
  const hash = (seed: number) => {
    let h1 = 0xdeadbeef ^ seed;
    let h2 = 0x41c6ce57 ^ seed;
    for (let i = 0; i < text.length; i += 1) {
      const ch = text.charCodeAt(i);
      h1 = Math.imul(h1 ^ ch, 2654435761);
      h2 = Math.imul(h2 ^ ch, 1597334677);
    }
    h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^ Math.imul(h2 ^ (h2 >>> 13), 3266489909);
    h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^ Math.imul(h1 ^ (h1 >>> 13), 3266489909);
    return (4294967296 * (2097151 & h2) + (h1 >>> 0)).toString(16).padStart(14, '0');
  };
  return `${hash(1)}${hash(2)}:${text.length}`;
}

/** Digest of what the server MACs per op (minus op_seq/scope_key, fixed by identity). */
export function outboxV3Digest(row: Pick<OutboxRow, 'tableName' | 'action' | 'entityId' | 'payload' | 'baseUpdatedAt'>): string {
  let patch: unknown;
  try {
    patch = JSON.parse(row.payload || '{}');
  } catch {
    patch = row.payload;
  }
  return digestString(canonicalJson({
    table: row.tableName,
    action: row.action,
    entity_id: row.entityId,
    patch,
    base: { updated_at: row.baseUpdatedAt ?? null },
  }));
}

export type OutboxV3Allocation = {
  deviceId: string;
  retirementFloor: number;
  rows: OutboxRow[];
};

/**
 * Give each send row a stable v3 op identity inside the caller's write unit.
 *
 * An unchanged row keeps its op_id/op_seq and nothing is written (an
 * idempotent resend under the same device). A changed row (coalesced edit)
 * gets a new op_id and the next op_seq. The device identity in sync_state
 * `v3-device:<authScope>` survives auth cache clears; if it is nevertheless
 * missing, ops already sent under the lost device cannot be resent with a
 * matching receipt MAC nor safely re-identified (the lost response may have
 * been applied), so they are quarantined and a new device starts at seq 0.
 */
export function allocateV3OpIdentitiesInWriter(
  w: SqliteWriter,
  tx: SqliteHandle,
  authScope: string,
  rows: OutboxRow[],
): OutboxV3Allocation {
  assertActiveWriter(w);
  const lostReason = 'sync_v3_device_identity_lost';
  let device = readSyncV3DeviceStateInWriter(w, tx, authScope);
  const created = !device;
  let sendRows = rows;
  if (!device) {
    const orphaned = tx.select().from(schema.outbox)
      .where(and(
        eq(schema.outbox.authScope, authScope),
        sql`${schema.outbox.v3OpId} IS NOT NULL`,
        isNull(schema.outbox.blockedReason),
      ))
      .all();
    const excluded = new Set<string>();
    for (const row of orphaned) {
      // Whether or not the CAS wins, the row is not sent now; the next run
      // re-reads it and decides again.
      excluded.add(row.opId);
      quarantineOutboxOpIfSnapshotInWriter(w, tx, row.opId, pendingSnapshot(row), lostReason);
    }
    sendRows = rows.filter((row) => !excluded.has(row.opId));
    device = { device_id: randomId(), next_op_seq: 0 };
  }
  let nextSeq = device.next_op_seq;
  const out: OutboxRow[] = [];
  for (const row of sendRows) {
    const digest = outboxV3Digest(row);
    if (row.v3OpId && row.v3SentDigest === digest && row.v3OpSeq != null) {
      out.push(row);
      continue;
    }
    const identity = { v3OpId: randomId(), v3OpSeq: nextSeq, v3SentDigest: digest };
    nextSeq += 1;
    tx.update(schema.outbox).set(identity).where(eq(schema.outbox.opId, row.opId)).run();
    out.push({ ...row, ...identity });
  }
  if (created || nextSeq !== device.next_op_seq) {
    writeSyncV3DeviceStateInWriter(w, tx, authScope, { device_id: device.device_id, next_op_seq: nextSeq });
  }
  const remaining = tx.select({ seq: schema.outbox.v3OpSeq, blocked: schema.outbox.blockedReason })
    .from(schema.outbox)
    .where(and(eq(schema.outbox.authScope, authScope), sql`${schema.outbox.v3OpId} IS NOT NULL`))
    .all()
    .filter((row) => row.blocked !== lostReason)
    .map((row) => Number(row.seq))
    .filter((seq) => Number.isFinite(seq));
  return {
    deviceId: device.device_id,
    retirementFloor: remaining.length ? Math.min(...remaining) : nextSeq,
    rows: out,
  };
}

/** Terminally block one exact outbox snapshot (a permanently rejected v3 op). */
export function quarantineOutboxOpIfSnapshotInWriter(
  w: SqliteWriter,
  tx: SqliteHandle,
  opId: string,
  snapshot: OutboxMutationSnapshot,
  reason: string,
): boolean {
  assertActiveWriter(w);
  const result = tx.update(schema.outbox)
    .set({ retryCount: 5, blockedReason: reason })
    .where(outboxMutationSnapshotCondition(opId, snapshot))
    .run();
  return Number(result?.changes ?? 0) > 0;
}

export async function markOutboxError(
  opId: string,
  error: string,
  expected: OutboxMutationSnapshot,
): Promise<void> {
  const db = getDb();
  const row = (
    await db.select().from(schema.outbox).where(eq(schema.outbox.opId, opId))
  )[0];
  await db
    .update(schema.outbox)
    .set({
      retryCount: (row?.retryCount ?? 0) + 1,
      lastError: error,
    })
    .where(outboxMutationSnapshotCondition(opId, expected));
}

export async function markOutboxConflict(
  opId: string,
  error: string,
  serverPayload: unknown | undefined,
  expected: OutboxMutationSnapshot,
): Promise<void> {
  const db = getDb();
  const row = (
    await db.select().from(schema.outbox).where(eq(schema.outbox.opId, opId))
  )[0];
  // A conflict response can arrive through an engine path that marks the
  // row directly (without first calling recordOutboxServerSnapshot).  Apply
  // the same composite identity guard here so malformed/cross-scope server
  // entities never become durable snapshots that a later UI can consume.
  let safeServerPayload = serverPayload;
  if (serverPayload !== undefined && row?.tableName === 'knowledge_nodes') {
    const reference = snapshotReference(row);
    if (!reference || !validateOutboxConflictServerScope(reference, serverPayload).ok) {
      safeServerPayload = undefined;
    }
  }
  await db
    .update(schema.outbox)
    .set({
      retryCount: (row?.retryCount ?? 0) + 1,
      lastError: `conflict:${error}`,
      ...(safeServerPayload === undefined ? {} : { conflictPayload: safeServerPayload as never }),
    })
    .where(outboxMutationSnapshotCondition(opId, expected));
}
