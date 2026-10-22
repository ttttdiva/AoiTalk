import { getSqlite } from '../db/client';
import { runForegroundSqliteWrite } from '../db/sqlite-write-coordinator';
import { randomId } from './outbox';
import type {
  ResolutionAdoptInput,
  ResolutionContext,
  ResolutionDraft,
  ResolutionDraftInput,
  ResolutionDraftSceneSnapshot,
  ResolutionDraftSceneRequest,
  ResolutionExport,
  ResolutionField,
  ResolutionHistoryItem,
  ResolutionInternalFullSnapshot,
  ResolutionJsonObject,
  ResolutionJsonValue,
  ResolutionLifecycleState,
  ResolutionLocalSnapshot,
  ResolutionOperationSnapshot,
  ResolutionPortableSnapshot,
  ResolutionPreview,
  ResolutionPreviewView,
  ResolutionRecordView,
  ResolutionResult,
  ResolutionResultReason,
  ResolutionServerSnapshot,
  ResolutionGuardSnapshot,
  ResolutionSqlite,
  ResolutionStoreDependencies,
  ResolutionTargetCapability,
  ResolutionTargetMetadata,
  OutboxResolutionStore,
} from './outbox-resolution-types';
import { sameResolutionContext } from './outbox-resolution-types';

/**
 * Private SQLite journal implementation for explicit outbox resolution.
 *
 * The facade owns live GETs and auth-generation checks.  This module owns the
 * durable boundary and the foreground CAS transaction.  It intentionally does
 * not call normal task/docs repositories: those paths enqueue or send edits.
 */

const JOURNAL_TABLE = 'outbox_resolution_records';
const SAFE_OUTBOX_TABLES = new Set(['tasks', 'knowledge_nodes']);
const SAFE_REASON_VALUES = new Set<ResolutionResultReason>([
  'origin_confirmation_required',
  'invalid_input',
  'journal_unavailable',
  'record_not_found',
  'revision_mismatch',
  'context_changed',
  'auth_scope_changed',
  'server_changed',
  'server_unavailable',
  'server_not_found',
  'server_forbidden',
  'server_version_missing',
  'local_snapshot_changed',
  'related_snapshot_changed',
  'membership_changed',
  'cache_snapshot_changed',
  'editor_changed',
  'sync_inflight',
  'duplicate_entity',
  'acl_changed',
  'scope_changed',
  'unsupported',
  'not_prepareable',
  'not_adoptable',
  'outbox_replaced',
  'draft_missing',
  'transaction_failed',
  'export_unavailable',
]);

type DbJournalRow = {
  id?: unknown;
  revision?: unknown;
  auth_scope?: unknown;
  server_fingerprint?: unknown;
  credential_identity_revision?: unknown;
  kind?: unknown;
  table_name?: unknown;
  entity_id?: unknown;
  op_id?: unknown;
  created_at?: unknown;
  state?: unknown;
  operation_origin?: unknown;
  target_namespace_json?: unknown;
  operation_snapshot_json?: unknown;
  local_snapshot_json?: unknown;
  related_snapshot_json?: unknown;
  membership_snapshot_json?: unknown;
  cache_snapshot_json?: unknown;
  guard_snapshot_json?: unknown;
  server_snapshot_json?: unknown;
  view_json?: unknown;
  draft_json?: unknown;
  export_snapshot_json?: unknown;
  has_draft?: unknown;
  updated_at?: unknown;
};

type DbOutboxRow = {
  op_id?: unknown;
  created_at?: unknown;
  table_name?: unknown;
  action?: unknown;
  entity_id?: unknown;
  payload?: unknown;
  auth_scope?: unknown;
  docs_scope_key?: unknown;
  blocked_reason?: unknown;
  base_updated_at?: unknown;
  base_payload?: unknown;
  conflict_payload?: unknown;
  retry_count?: unknown;
  last_error?: unknown;
};

type DbRow = Record<string, unknown>;

class ResolutionAbort extends Error {
  constructor(readonly reason: ResolutionResultReason) {
    super(reason);
    this.name = 'ResolutionAbort';
  }
}
function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value));
}

const PRIVATE_KEYS = /^(?:access[_-]?token|refresh[_-]?token|token|cookie|password|passwd|authorization|credential|secret|api[_-]?key)$/i;

/** Clone domain values for the private journal without dropping opaque user
 * metadata. Authentication material is supplied through ResolutionContext and
 * is never copied into these domain snapshots. */
function cloneJson(value: unknown, seen = new Set<unknown>()): ResolutionJsonValue {
  if (value == null) return null;
  if (typeof value === 'string' || typeof value === 'boolean') return value;
  if (typeof value === 'number') return Number.isFinite(value) ? value : null;
  if (typeof value !== 'object') return null;
  if (seen.has(value)) return null;
  seen.add(value);
  if (Array.isArray(value)) return value.map((item) => cloneJson(item, seen));
  const output: ResolutionJsonObject = {};
  for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
    output[key] = cloneJson(item, seen);
  }
  return output;
}

/** Convert arbitrary input to JSON while omitting credential-like keys for a
 * safe view/export projection. */
function redactJson(value: unknown, seen = new Set<unknown>()): ResolutionJsonValue {
  if (value == null) return null;
  if (typeof value === 'string' || typeof value === 'boolean') return value;
  if (typeof value === 'number') return Number.isFinite(value) ? value : null;
  if (typeof value !== 'object') return null;
  if (seen.has(value)) return null;
  seen.add(value);
  if (Array.isArray(value)) return value.map((item) => redactJson(item, seen));
  const output: ResolutionJsonObject = {};
  for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
    if (PRIVATE_KEYS.test(key)) continue;
    output[key] = redactJson(item, seen);
  }
  return output;
}

function containsPrivateKey(value: unknown, seen = new Set<unknown>()): boolean {
  if (!value || typeof value !== 'object' || seen.has(value)) return false;
  seen.add(value);
  if (Array.isArray(value)) return value.some((item) => containsPrivateKey(item, seen));
  return Object.entries(value as Record<string, unknown>).some(([key, item]) =>
    PRIVATE_KEYS.test(key) || containsPrivateKey(item, seen));
}

function jsonText(
  value: unknown,
  fallback: ResolutionJsonValue = null,
  safe = false,
): string {
  try {
    const encoded = JSON.stringify(safe ? redactJson(value) : cloneJson(value));
    return encoded === undefined ? JSON.stringify(fallback) : encoded;
  } catch {
    return JSON.stringify(fallback);
  }
}

function parseJson(value: unknown): ResolutionJsonValue | null {
  if (typeof value !== 'string') return value == null ? null : cloneJson(value);
  try {
    return cloneJson(JSON.parse(value));
  } catch {
    return null;
  }
}

function parseSafeJson(value: unknown): ResolutionJsonValue | null {
  if (typeof value !== 'string') return value == null ? null : redactJson(value);
  try {
    return redactJson(JSON.parse(value));
  } catch {
    return null;
  }
}

function asString(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback;
}

function asNullableString(value: unknown): string | null {
  return typeof value === 'string' ? value : null;
}

function asNumber(value: unknown, fallback = 0): number {
  return typeof value === 'number' && Number.isFinite(value)
    ? value
    : typeof value === 'string' && Number.isFinite(Number(value))
      ? Number(value)
      : fallback;
}

function stableValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(stableValue);
  if (isRecord(value)) {
    return Object.fromEntries(
      Object.keys(value).sort().map((key) => [key, stableValue(value[key])]),
    );
  }
  return value;
}

function equalValue(left: unknown, right: unknown): boolean {
  try {
    return JSON.stringify(stableValue(cloneJson(left)))
      === JSON.stringify(stableValue(cloneJson(right)));
  } catch {
    return false;
  }
}

function finiteReason(value: unknown): string | null {
  if (typeof value !== 'string') return null;
  return SAFE_REASON_VALUES.has(value as ResolutionResultReason) ? value : null;
}

function fieldValue(value: unknown): ResolutionJsonValue | null {
  return value == null ? null : redactJson(value);
}

function normalizeField(value: unknown): ResolutionField | null {
  if (!isRecord(value) || typeof value.key !== 'string' || !value.key) return null;
  const comparison = value.comparison === 'same'
    || value.comparison === 'different'
    || value.comparison === 'unknown'
    ? value.comparison
    : 'unknown';
  return {
    key: value.key,
    ...(typeof value.label === 'string' ? { label: value.label } : {}),
    device: fieldValue(value.device),
    server: fieldValue(value.server),
    serverKnown: value.serverKnown === true,
    comparison,
  };
}

function normalizeFields(value: unknown): ResolutionField[] {
  if (!Array.isArray(value)) return [];
  return value.map(normalizeField).filter((item): item is ResolutionField => item !== null);
}

function normalizeView(value: unknown, fallback?: ResolutionPreviewView): ResolutionPreviewView {
  const input = isRecord(value) ? value : {};
  const kind = input.kind === 'task' || input.kind === 'docs' || input.kind === 'unsupported'
    ? input.kind
    : fallback?.kind ?? 'unsupported';
  const serverState = input.serverState === 'present'
    || input.serverState === 'not_found'
    || input.serverState === 'forbidden'
    || input.serverState === 'unavailable'
    || input.serverState === 'invalid'
    || input.serverState === 'unsupported'
    ? input.serverState
    : fallback?.serverState ?? 'unsupported';
  const fields = normalizeFields(input.fields ?? fallback?.fields);
  return {
    opId: asString(input.opId, fallback?.opId ?? ''),
    entityId: asString(input.entityId, fallback?.entityId ?? ''),
    tableName: asString(input.tableName, fallback?.tableName ?? ''),
    action: asString(input.action, fallback?.action ?? ''),
    docsScopeKey: typeof input.docsScopeKey === 'string'
      ? input.docsScopeKey
      : fallback?.docsScopeKey ?? null,
    kind,
    ...(input.operationOrigin === 'unrecorded' || fallback?.operationOrigin === 'unrecorded'
      ? { operationOrigin: 'unrecorded' as const } : {}),
    ...(typeof input.resolutionTargetLabel === 'string'
      ? { resolutionTargetLabel: input.resolutionTargetLabel }
      : fallback?.resolutionTargetLabel ? { resolutionTargetLabel: fallback.resolutionTargetLabel } : {}),
    checkedAt: asString(input.checkedAt, fallback?.checkedAt ?? new Date(0).toISOString()),
    serverState,
    ...(typeof input.serverSource === 'string'
      ? { serverSource: input.serverSource }
      : fallback?.serverSource === undefined ? {} : { serverSource: fallback.serverSource }),
    liveReadAt: typeof input.liveReadAt === 'string'
      ? input.liveReadAt
      : fallback?.liveReadAt ?? null,
    serverCapturedAt: typeof input.serverCapturedAt === 'string'
      ? input.serverCapturedAt
      : fallback?.serverCapturedAt ?? null,
    serverComplete: input.serverComplete === true || fallback?.serverComplete === true,
    serverUpdatedAt: typeof input.serverUpdatedAt === 'string'
      ? input.serverUpdatedAt
      : fallback?.serverUpdatedAt ?? null,
    baseUpdatedAt: typeof input.baseUpdatedAt === 'string'
      ? input.baseUpdatedAt
      : fallback?.baseUpdatedAt ?? null,
    hasBaseSnapshot: input.hasBaseSnapshot === true
      || fallback?.hasBaseSnapshot === true,
    hasConflictSnapshot: input.hasConflictSnapshot === true
      || fallback?.hasConflictSnapshot === true,
    fields,
    canPrepare: input.canPrepare === true,
    canAdopt: input.canAdopt === true,
    reason: finiteReason(input.reason) ?? fallback?.reason ?? null,
    localComplete: input.localComplete === true,
  };
}

function normalizeContext(value: unknown): ResolutionContext | null {
  if (!isRecord(value)) return null;
  if (typeof value.authScope !== 'string' || !value.authScope
    || typeof value.serverFingerprint !== 'string' || !value.serverFingerprint
    || typeof value.credentialIdentityRevision !== 'string'
    || !value.credentialIdentityRevision) return null;
  return {
    authScope: value.authScope,
    serverFingerprint: value.serverFingerprint,
    credentialIdentityRevision: value.credentialIdentityRevision,
    ownerId: typeof value.ownerId === 'string' ? value.ownerId : null,
  };
}

function normalizeOperation(value: unknown): ResolutionOperationSnapshot | null {
  if (!isRecord(value)) return null;
  if (typeof value.opId !== 'string' || !value.opId
    || typeof value.tableName !== 'string' || !value.tableName
    || typeof value.action !== 'string' || !value.action
    || typeof value.entityId !== 'string' || !value.entityId
    || typeof value.payload !== 'string'
    || typeof value.createdAt !== 'number') return null;
  return {
    opId: value.opId,
    tableName: value.tableName,
    action: value.action,
    entityId: value.entityId,
    payload: value.payload,
    createdAt: value.createdAt,
    baseUpdatedAt: typeof value.baseUpdatedAt === 'string' ? value.baseUpdatedAt : null,
    basePayload: value.basePayload == null ? null : cloneJson(value.basePayload),
    conflictPayload: value.conflictPayload == null ? null : cloneJson(value.conflictPayload),
    retryCount: asNumber(value.retryCount),
    lastError: typeof value.lastError === 'string' ? value.lastError : null,
    blockedReason: typeof value.blockedReason === 'string' ? value.blockedReason : null,
    authScope: typeof value.authScope === 'string' ? value.authScope : null,
    docsScopeKey: typeof value.docsScopeKey === 'string' ? value.docsScopeKey : null,
  };
}

function normalizeLocal(value: unknown): ResolutionLocalSnapshot {
  if (!isRecord(value)) return { row: null };
  return {
    row: value.row == null ? null : cloneJson(value.row),
    ...(typeof value.known === 'boolean' ? { known: value.known } : {}),
    revision: typeof value.revision === 'string' ? value.revision : null,
    related: value.related == null ? undefined : cloneJson(value.related),
    relatedRevision: typeof value.relatedRevision === 'string' ? value.relatedRevision : null,
    ...(typeof value.relatedKnown === 'boolean' ? { relatedKnown: value.relatedKnown } : {}),
    ...(Array.isArray(value.editorTargets) ? {
      editorTargets: value.editorTargets.filter((item): item is { tableName: string; entityId: string } =>
        isRecord(item) && typeof item.tableName === 'string' && typeof item.entityId === 'string')
        .map((item) => ({ tableName: item.tableName, entityId: item.entityId })),
    } : {}),
    ...(Array.isArray(value.editorAncestors) ? {
      editorAncestors: value.editorAncestors.filter((item): item is {
        id: string; workspaceId: string | null; projectId: string | null;
        parentId: string | null; rootPageId: string | null;
      } => isRecord(item) && typeof item.id === 'string')
        .map((item) => ({
          id: item.id,
          workspaceId: typeof item.workspaceId === 'string' ? item.workspaceId : null,
          projectId: typeof item.projectId === 'string' ? item.projectId : null,
          parentId: typeof item.parentId === 'string' ? item.parentId : null,
          rootPageId: typeof item.rootPageId === 'string' ? item.rootPageId : null,
        })),
    } : {}),
    ...(typeof value.editorAncestryComplete === 'boolean'
      ? { editorAncestryComplete: value.editorAncestryComplete } : {}),
  };
}

function normalizeServer(value: unknown): ResolutionServerSnapshot {
  const input = isRecord(value) ? value : {};
  const state = input.state === 'present'
    || input.state === 'not_found'
    || input.state === 'forbidden'
    || input.state === 'unavailable'
    || input.state === 'invalid'
    || input.state === 'unsupported'
    ? input.state
    : 'invalid';
  return {
    state,
    updatedAt: typeof input.updatedAt === 'string' ? input.updatedAt : null,
    value: input.value == null ? null : cloneJson(input.value),
    fields: normalizeFields(input.fields),
    ...(typeof input.serverSource === 'string' ? { serverSource: input.serverSource } : {}),
    liveReadAt: typeof input.liveReadAt === 'string' ? input.liveReadAt : null,
    serverCapturedAt: typeof input.serverCapturedAt === 'string' ? input.serverCapturedAt : null,
    serverComplete: input.serverComplete === true,
    ...(typeof input.taskAggregatesComplete === 'boolean'
      ? { taskAggregatesComplete: input.taskAggregatesComplete } : {}),
  };
}

function normalizeGuard(value: unknown): ResolutionGuardSnapshot {
  const input = isRecord(value) ? value : {};
  return {
    editorRevision: asNumber(input.editorRevision),
    sceneRevision: typeof input.sceneRevision === 'string' ? input.sceneRevision : null,
    inflightSync: input.inflightSync === true,
    uniqueEntity: input.uniqueEntity !== false,
    acl: input.acl === 'writable' || input.acl === 'read_only' || input.acl === 'unknown'
      ? input.acl
      : 'unknown' as const,
    scopeProven: input.scopeProven === true,
    unsupportedRelatedValues: input.unsupportedRelatedValues === true,
  };
}

function normalizePortable(value: unknown): ResolutionPortableSnapshot {
  const input = isRecord(value) ? value : {};
  return {
    target: input.target == null ? null : cloneJson(input.target),
    ...(input.children == null ? {} : { children: cloneJson(input.children) }),
    fields: normalizeFields(input.fields),
    incomplete: Array.isArray(input.incomplete)
      ? input.incomplete.filter((item): item is string => typeof item === 'string').slice(0, 32)
      : [],
  };
}

function normalizeDraft(value: unknown): ResolutionDraft | null {
  if (!isRecord(value) || typeof value.restoredAt !== 'string') return null;
  return {
    restoredAt: value.restoredAt,
    fields: normalizeFields(value.fields),
    changes: isRecord(value.changes) ? redactJson(value.changes) as ResolutionJsonObject : {},
  };
}

function normalizePrivateDraft(value: unknown): ResolutionDraft | null {
  if (!isRecord(value) || typeof value.restoredAt !== 'string') return null;
  return {
    restoredAt: value.restoredAt,
    fields: normalizeFields(value.fields),
    changes: isRecord(value.changes) ? cloneJson(value.changes) as ResolutionJsonObject : {},
  };
}

function nowIso(now: number): string {
  try {
    return new Date(now).toISOString();
  } catch {
    return new Date(0).toISOString();
  }
}

function assertContext(context: ResolutionContext): void {
  if (!normalizeContext(context)) throw new ResolutionAbort('invalid_input');
}

function sameOperation(left: ResolutionOperationSnapshot, right: ResolutionOperationSnapshot): boolean {
  return left.opId === right.opId
    && left.createdAt === right.createdAt
    && left.tableName === right.tableName
    && left.action === right.action
    && left.entityId === right.entityId
    && left.payload === right.payload
    && left.baseUpdatedAt === right.baseUpdatedAt
    && equalValue(left.basePayload, right.basePayload)
    && equalValue(left.conflictPayload, right.conflictPayload)
    && left.retryCount === right.retryCount
    && left.lastError === right.lastError
    && left.blockedReason === right.blockedReason
    && left.authScope === right.authScope
    && left.docsScopeKey === right.docsScopeKey;
}

function rowOperation(row: DbOutboxRow): ResolutionOperationSnapshot | null {
  const opId = asString(row.op_id);
  const tableName = asString(row.table_name);
  const action = asString(row.action);
  const entityId = asString(row.entity_id);
  if (!opId || !tableName || !action || !entityId || typeof row.payload !== 'string') return null;
  return {
    opId,
    createdAt: asNumber(row.created_at),
    tableName,
    action,
    entityId,
    payload: row.payload,
    baseUpdatedAt: asNullableString(row.base_updated_at),
    basePayload: row.base_payload == null ? null : parseJson(row.base_payload),
    conflictPayload: row.conflict_payload == null ? null : parseJson(row.conflict_payload),
    retryCount: asNumber(row.retry_count),
    lastError: asNullableString(row.last_error),
    blockedReason: asNullableString(row.blocked_reason),
    authScope: asNullableString(row.auth_scope),
    docsScopeKey: asNullableString(row.docs_scope_key),
  };
}

function rowContext(row: DbJournalRow): ResolutionContext | null {
  return normalizeContext({
    authScope: row.auth_scope,
    serverFingerprint: row.server_fingerprint,
    credentialIdentityRevision: row.credential_identity_revision,
  });
}

function rowItem(
  row: DbJournalRow,
  draft: ResolutionDraft | null,
  hasDraftOverride?: boolean,
): ResolutionHistoryItem {
  const state = row.state === 'adopted' || row.state === 'draft' || row.state === 'held'
    ? row.state
    : 'prepared';
  return {
    id: asString(row.id),
    revision: asNumber(row.revision, 1),
    kind: row.kind === 'task' || row.kind === 'docs' || row.kind === 'unsupported'
      ? row.kind
      : 'unsupported',
    entityId: asString(row.entity_id),
    opId: asString(row.op_id),
    createdAt: asNumber(row.created_at),
    state,
    hasDraft: hasDraftOverride ?? draft !== null,
  };
}

function safeTargetLabel(tableName: unknown): string {
  if (tableName === 'tasks') return 'Task resolution target';
  if (tableName === 'knowledge_nodes') return 'Docs node resolution target';
  return 'Unsupported resolution target';
}

function rowRecordView(row: DbJournalRow): ResolutionRecordView {
  const draft = normalizeDraft(parseSafeJson(row.draft_json));
  const persistedView = parseSafeJson(row.view_json);
  const view = normalizeView({
    ...((persistedView as Record<string, unknown> | null) ?? {}),
    ...(row.operation_origin === 'unrecorded' ? { operationOrigin: 'unrecorded' } : {}),
    resolutionTargetLabel: isRecord(persistedView) && typeof persistedView.resolutionTargetLabel === 'string'
      ? persistedView.resolutionTargetLabel
      : safeTargetLabel(row.table_name),
  });
  return { item: rowItem(row, draft), view, draft };
}

function getValue(db: ResolutionSqlite, sql: string, params: readonly unknown[] = []): unknown {
  if (typeof db.getFirstSync === 'function') return db.getFirstSync(sql, params);
  const statement = (db as unknown as { prepare?: (source: string) => { get: (...args: unknown[]) => unknown } }).prepare;
  if (typeof statement !== 'function') throw new Error('sqlite get unavailable');
  return statement.call(db, sql).get(...params);
}

function getRows<T = DbRow>(db: ResolutionSqlite, sql: string, params: readonly unknown[] = []): T[] {
  if (typeof db.getAllSync === 'function') return db.getAllSync<T>(sql, params) ?? [];
  const statement = (db as unknown as { prepare?: (source: string) => { all: (...args: unknown[]) => T[] } }).prepare;
  if (typeof statement !== 'function') throw new Error('sqlite all unavailable');
  return statement.call(db, sql).all(...params) ?? [];
}

function runSql(db: ResolutionSqlite, sql: string, params: readonly unknown[] = []): { changes?: number } | unknown {
  if (typeof db.runSync === 'function') return db.runSync(sql, params);
  const statement = (db as unknown as { prepare?: (source: string) => { run: (...args: unknown[]) => unknown } }).prepare;
  if (typeof statement !== 'function') throw new Error('sqlite run unavailable');
  return statement.call(db, sql).run(...params);
}

function execSql(db: ResolutionSqlite, sql: string): void {
  if (typeof db.execSync === 'function') {
    db.execSync(sql);
    return;
  }
  const exec = (db as unknown as { exec?: (source: string) => unknown }).exec;
  if (typeof exec !== 'function') throw new Error('sqlite exec unavailable');
  exec.call(db, sql);
}

function withTransaction<T>(db: ResolutionSqlite, callback: () => T): T {
  if (typeof db.withTransactionSync === 'function') return db.withTransactionSync(callback);
  execSql(db, 'BEGIN IMMEDIATE;');
  try {
    const result = callback();
    execSql(db, 'COMMIT;');
    return result;
  } catch (error) {
    try { execSql(db, 'ROLLBACK;'); } catch { /* preserve the original failure */ }
    throw error;
  }
}

function ensureJournal(deps: ResolutionStoreDependencies): ResolutionSqlite {
  if (deps.ensureSchema) deps.ensureSchema();
  const db = deps.sqlite ?? getSqlite() as unknown as ResolutionSqlite;
  if (!db) throw new Error('sqlite unavailable');
  return db;
}

async function currentContextMatches(
  deps: ResolutionStoreDependencies,
  expected: ResolutionContext,
  requireGeneration: boolean,
): Promise<boolean> {
  if (!deps.currentContext) return true;
  try {
    const actual = await deps.currentContext();
    return requireGeneration
      ? sameResolutionContext(actual, expected)
      : sameResolutionNamespace(actual, expected);
  } catch {
    return false;
  }
}

function rowById(
  db: ResolutionSqlite,
  id: string,
  authScope: string,
  context?: ResolutionContext,
): DbJournalRow | null {
  if (context && context.authScope !== authScope) return null;
  const serverClause = context ? ' AND server_fingerprint = ?' : '';
  const params = context ? [id, authScope, context.serverFingerprint] : [id, authScope];
  return getValue(db,
    `SELECT * FROM ${JOURNAL_TABLE} WHERE id = ? AND auth_scope = ?${serverClause} LIMIT 1;`,
    params,
  ) as DbJournalRow | null;
}

function sameResolutionNamespace(left: ResolutionContext, right: ResolutionContext): boolean {
  return left.authScope === right.authScope
    && left.serverFingerprint === right.serverFingerprint;
}

function safeRecordId(idFactory: (() => string) | undefined): string {
  const candidate = idFactory?.() ?? randomId();
  return typeof candidate === 'string' && candidate.length > 0 ? candidate : randomId();
}

function defaultTargetMetadata(
  context: ResolutionContext,
  operation: ResolutionOperationSnapshot,
): ResolutionTargetMetadata {
  return {
    operationOrigin: 'unrecorded',
    authScope: context.authScope,
    serverFingerprint: context.serverFingerprint,
    tableName: operation.tableName,
    entityId: operation.entityId,
    opId: operation.opId,
    intentKey: `${operation.tableName}:${operation.entityId}:${operation.opId}`,
  };
}

function targetMetadataForCapture(
  capture: ResolutionInternalFullSnapshot,
): ResolutionTargetMetadata {
  const candidate = capture.target;
  if (candidate
    && candidate.operationOrigin === 'unrecorded'
    && candidate.authScope === capture.context.authScope
    && candidate.serverFingerprint === capture.context.serverFingerprint
    && candidate.tableName === capture.operation.tableName
    && candidate.entityId === capture.operation.entityId
    && candidate.opId === capture.operation.opId
    && candidate.intentKey) {
    return candidate;
  }
  return defaultTargetMetadata(capture.context, capture.operation);
}

function targetCapabilityForCapture(
  capture: ResolutionInternalFullSnapshot,
): ResolutionTargetCapability | null {
  const capability = capture.capability;
  if (!capability || typeof capability !== 'object' || !capability.opaque) return null;
  const target = targetMetadataForCapture(capture);
  return capability.metadata && equalValue(capability.metadata, target) ? capability : null;
}

async function revalidateTarget(
  deps: ResolutionStoreDependencies,
  preview: ResolutionPreview,
  stage: 'prepare' | 'adopt',
): Promise<boolean> {
  const capability = targetCapabilityForCapture(preview.capture);
  if (deps.targetCapabilityRequired && (!capability || !deps.revalidateTarget)) return false;
  if (!capability || !deps.revalidateTarget) return true;
  return Boolean(await deps.revalidateTarget(capability, stage));
}

function assertTargetInTransaction(
  deps: ResolutionStoreDependencies,
  preview: ResolutionPreview,
  stage: 'prepare' | 'adopt',
): boolean {
  const capability = targetCapabilityForCapture(preview.capture);
  if (deps.targetCapabilityRequired && (!capability || !deps.assertTargetInTransaction)) return false;
  return !capability || !deps.assertTargetInTransaction
    ? true
    : deps.assertTargetInTransaction(capability, stage);
}

function validatePreview(preview: ResolutionPreview): ResolutionResultReason | null {
  if (!preview || !preview.capture || !preview.view) return 'invalid_input';
  const capture = preview.capture;
  const context = normalizeContext(capture.context);
  const operation = normalizeOperation(capture.operation);
  if (!context || !operation) return 'invalid_input';
  if (!capture.kind || !capture.local || !capture.server || !capture.guard) return 'invalid_input';
  if (preview.view.opId !== operation.opId
    || preview.view.entityId !== operation.entityId
    || preview.view.tableName !== operation.tableName) return 'invalid_input';
  if (capture.docsScopeKey !== operation.docsScopeKey) return 'invalid_input';
  const target = targetMetadataForCapture(capture);
  if (target.authScope !== context.authScope
    || target.serverFingerprint !== context.serverFingerprint
    || target.tableName !== operation.tableName
    || target.entityId !== operation.entityId
    || target.opId !== operation.opId) return 'context_changed';
  if (capture.capability && !targetCapabilityForCapture(capture)) return 'not_adoptable';
  return null;
}

function safeDeleteOutbox(db: ResolutionSqlite, operation: ResolutionOperationSnapshot): void {
  const jsonParam = (value: unknown): string | null => value == null ? null : jsonText(value);
  const result = runSql(db,
    `DELETE FROM outbox
       WHERE op_id = ? AND created_at = ?
         AND table_name = ? AND action = ? AND entity_id = ? AND payload = ?
         AND auth_scope IS ? AND docs_scope_key IS ?
         AND blocked_reason IS ? AND base_updated_at IS ?
         AND base_payload IS ? AND conflict_payload IS ?
         AND retry_count = ? AND last_error IS ?;`,
    [operation.opId, operation.createdAt, operation.tableName, operation.action,
      operation.entityId, operation.payload, operation.authScope, operation.docsScopeKey,
      operation.blockedReason, operation.baseUpdatedAt, jsonParam(operation.basePayload),
      jsonParam(operation.conflictPayload), operation.retryCount, operation.lastError],
  ) as { changes?: number } | undefined;
  if (!result || typeof result.changes === 'undefined' || Number(result.changes) !== 1) {
    throw new ResolutionAbort('outbox_replaced');
  }
}

function currentOutbox(db: ResolutionSqlite, opId: string): ResolutionOperationSnapshot | null {
  const row = getValue(db,
    `SELECT op_id, created_at, table_name, action, entity_id, payload,
       auth_scope, docs_scope_key, blocked_reason, base_updated_at,
       base_payload, conflict_payload, retry_count, last_error
       FROM outbox WHERE op_id = ? LIMIT 1;`,
    [opId],
  ) as DbOutboxRow | null;
  return row ? rowOperation(row) : null;
}

function hasOtherEntityOperations(
  db: ResolutionSqlite,
  operation: ResolutionOperationSnapshot,
): boolean {
  const row = getValue(db,
    `SELECT COUNT(*) AS n FROM outbox WHERE table_name = ? AND entity_id = ?;`,
    [operation.tableName, operation.entityId],
  ) as DbRow | null;
  return asNumber(row?.n) > 1;
}

function localTableFor(operation: ResolutionOperationSnapshot): string | null {
  return SAFE_OUTBOX_TABLES.has(operation.tableName) ? operation.tableName : null;
}

function currentLocalRow(db: ResolutionSqlite, operation: ResolutionOperationSnapshot): DbRow | null {
  const table = localTableFor(operation);
  if (!table) return null;
  return getValue(db, `SELECT * FROM ${table} WHERE id = ? LIMIT 1;`, [operation.entityId]) as DbRow | null;
}

function aliasesForColumn(table: string, key: string): string[] {
  const aliases: Record<string, Record<string, string>> = {
    tasks: {
      project_id: 'projectId', title: 'title', description: 'description', status: 'status',
      priority: 'priority', start_at: 'startAt', end_at: 'endAt', all_day: 'allDay',
      reminder_offsets: 'reminderOffsets', notifications_enabled: 'notificationsEnabled',
      auto_close_on_due: 'autoCloseOnDue', source: 'source', created_by: 'createdBy',
      completed_at: 'completedAt', archived_at: 'archivedAt', estimated_hours: 'estimatedHours',
      parent_task_id: 'parentTaskId', task_metadata: 'taskMetadata', sort_order: 'sortOrder',
      created_at: 'createdAt', updated_at: 'updatedAt', deleted_at: 'deletedAt',
    },
    knowledge_nodes: {
      workspace_id: 'workspaceId', parent_id: 'parentId', root_page_id: 'rootPageId',
      project_id: 'projectId', source: 'source', access: 'access', read_only: 'readOnly',
      system_key: 'systemKey', title: 'title', aliases: 'aliases', description: 'description',
      body_json: 'bodyJson', body_text: 'bodyText', node_type: 'nodeType',
      display_props: 'displayProps', query_json: 'queryJson', view_json: 'viewJson',
      day_date: 'dayDate', sort_order: 'sortOrder', created_by: 'createdBy',
      updated_by: 'updatedBy', created_at: 'createdAt', updated_at: 'updatedAt',
      server_updated_at: 'serverUpdatedAt', dirty: 'dirty', conflict_payload: 'conflictPayload',
      archived_at: 'archivedAt',
    },
    knowledge_node_supertags: {
      node_id: 'nodeId', supertag_id: 'supertagId', created_at: 'createdAt',
      updated_at: 'updatedAt', server_updated_at: 'serverUpdatedAt',
      conflict_payload: 'conflictPayload', created_by: 'createdBy',
    },
    knowledge_field_values: {
      node_id: 'nodeId', field_id: 'fieldId', value_json: 'valueJson',
      value_text: 'valueText', value_number: 'valueNumber',
      value_datetime: 'valueDatetime', target_node_id: 'targetNodeId',
      updated_at: 'updatedAt', server_updated_at: 'serverUpdatedAt',
      conflict_payload: 'conflictPayload', updated_by: 'updatedBy',
    },
    knowledge_node_placements: {
      id: 'id', node_id: 'nodeId', parent_node_id: 'parentNodeId',
      sort_order: 'sortOrder', collapsed: 'collapsed', created_by: 'createdBy',
      created_at: 'createdAt',
    },
    knowledge_edges: {
      id: 'id', source_node_id: 'sourceNodeId', target_node_id: 'targetNodeId',
      relation_type: 'relationType', confidence: 'confidence', created_by: 'createdBy',
      created_at: 'createdAt',
    },
  };
  const tableAliases = aliases[table] ?? {};
  const camel = tableAliases[key];
  if (camel) return [key, camel];
  const snake = Object.entries(tableAliases).find(([, value]) => value === key)?.[0];
  return snake ? [key, snake] : [key];
}

function decodeLocalColumn(table: string, key: string, value: unknown): unknown {
  const jsonColumns = table === 'tasks'
    ? new Set(['reminder_offsets', 'task_metadata', 'reminderOffsets', 'taskMetadata'])
    : new Set([
      'aliases', 'body_json', 'display_props', 'query_json', 'view_json',
      'conflict_payload', 'aliases', 'bodyJson', 'displayProps', 'queryJson',
      'viewJson', 'conflictPayload', 'value_json', 'valueJson',
    ]);
  if (typeof value !== 'string' || !jsonColumns.has(key)) return value;
  try { return JSON.parse(value); } catch { return value; }
}

function projectDbRow(table: string, row: DbRow): DbRow {
  const projected: DbRow = {};
  const tableAliases: Record<string, Record<string, string>> = {
    tasks: {
      project_id: 'projectId', title: 'title', description: 'description', status: 'status',
      priority: 'priority', start_at: 'startAt', end_at: 'endAt', all_day: 'allDay',
      reminder_offsets: 'reminderOffsets', notifications_enabled: 'notificationsEnabled',
      auto_close_on_due: 'autoCloseOnDue', source: 'source', created_by: 'createdBy',
      completed_at: 'completedAt', archived_at: 'archivedAt', estimated_hours: 'estimatedHours',
      parent_task_id: 'parentTaskId', task_metadata: 'taskMetadata', sort_order: 'sortOrder',
      created_at: 'createdAt', updated_at: 'updatedAt', deleted_at: 'deletedAt',
    },
    knowledge_nodes: {
      workspace_id: 'workspaceId', parent_id: 'parentId', root_page_id: 'rootPageId',
      project_id: 'projectId', source: 'source', access: 'access', read_only: 'readOnly',
      system_key: 'systemKey', title: 'title', aliases: 'aliases', description: 'description',
      body_json: 'bodyJson', body_text: 'bodyText', node_type: 'nodeType',
      display_props: 'displayProps', query_json: 'queryJson', view_json: 'viewJson',
      day_date: 'dayDate', sort_order: 'sortOrder', created_by: 'createdBy',
      updated_by: 'updatedBy', created_at: 'createdAt', updated_at: 'updatedAt',
      server_updated_at: 'serverUpdatedAt', dirty: 'dirty', conflict_payload: 'conflictPayload',
      archived_at: 'archivedAt',
    },
    knowledge_node_supertags: {
      node_id: 'nodeId', supertag_id: 'supertagId', created_at: 'createdAt',
      updated_at: 'updatedAt', server_updated_at: 'serverUpdatedAt',
      conflict_payload: 'conflictPayload', created_by: 'createdBy',
    },
    knowledge_field_values: {
      node_id: 'nodeId', field_id: 'fieldId', value_json: 'valueJson',
      value_text: 'valueText', value_number: 'valueNumber',
      value_datetime: 'valueDatetime', target_node_id: 'targetNodeId',
      updated_at: 'updatedAt', server_updated_at: 'serverUpdatedAt',
      conflict_payload: 'conflictPayload', updated_by: 'updatedBy',
    },
    knowledge_node_placements: {
      id: 'id', node_id: 'nodeId', parent_node_id: 'parentNodeId',
      sort_order: 'sortOrder', collapsed: 'collapsed', created_by: 'createdBy',
      created_at: 'createdAt',
    },
    knowledge_edges: {
      id: 'id', source_node_id: 'sourceNodeId', target_node_id: 'targetNodeId',
      relation_type: 'relationType', confidence: 'confidence', created_by: 'createdBy',
      created_at: 'createdAt',
    },
  };
  for (const [key, value] of Object.entries(row)) {
    const projectedKey = tableAliases[table]?.[key] ?? key;
    projected[projectedKey] = decodeLocalColumn(table, key, value);
  }
  return projected;
}

function projectMembershipRow(row: DbRow): DbRow {
  return {
    authScope: row.auth_scope,
    scopeKey: row.scope_key,
    scopeId: row.scope_id,
    projectId: row.project_id,
    tableName: row.table_name,
    entityKey: row.entity_key,
    state: row.state,
    access: row.access,
    readOnly: row.read_only,
    updatedAt: row.updated_at,
  };
}

function valuesEqualForColumn(left: unknown, right: unknown): boolean {
  if (left === right) return true;
  if ((left == null) && (right == null)) return true;
  if (typeof left === 'boolean' && (right === 0 || right === 1)) return (left ? 1 : 0) === right;
  if (typeof right === 'boolean' && (left === 0 || left === 1)) return (right ? 1 : 0) === left;
  if (typeof left === 'number' || typeof right === 'number') {
    return Number(left) === Number(right);
  }
  if (typeof left === 'string' && (right === 0 || right === 1)) return (left === '1') === Boolean(right);
  if (typeof right === 'string' && (left === 0 || left === 1)) return (right === '1') === Boolean(left);
  return equalValue(left, right);
}

function localMatches(table: string, current: DbRow | null, expected: ResolutionLocalSnapshot): boolean {
  const expectedRow = expected.row;
  if (expectedRow == null) return current == null;
  if (!current || !isRecord(expectedRow)) return false;
  const expectedObject = expectedRow as Record<string, unknown>;
  for (const [key, value] of Object.entries(expectedObject)) {
    if (key === 'revision' || key === 'related' || key === 'relatedRevision') continue;
    const values = aliasesForColumn(table, key);
    const currentValue = values.find((candidate) => Object.prototype.hasOwnProperty.call(current, candidate))
      ? current[values.find((candidate) => Object.prototype.hasOwnProperty.call(current, candidate)) as string]
      : undefined;
    if (currentValue === undefined && value === undefined) continue;
    if (!valuesEqualForColumn(decodeLocalColumn(table, values.find((candidate) => Object.prototype.hasOwnProperty.call(current, candidate)) ?? key, currentValue), value)) return false;
  }
  if (expected.revision) {
    const currentRevision = current.updated_at ?? current.updatedAt ?? current.server_updated_at ?? current.serverUpdatedAt;
    if (String(currentRevision ?? '') !== expected.revision) return false;
  }
  return true;
}

function objectField(value: Record<string, unknown>, ...keys: string[]): unknown {
  for (const key of keys) {
    if (Object.prototype.hasOwnProperty.call(value, key)) return value[key];
  }
  return undefined;
}

function toSqlValue(value: unknown): unknown {
  if (value === undefined) return null;
  if (value && typeof value === 'object') return JSON.stringify(cloneJson(value));
  if (typeof value === 'boolean') return value ? 1 : 0;
  return value;
}

const TASK_CACHED_TAGS_KEY = 'mobile_cached_tags';

function canonicalTaskTags(value: unknown, existing: unknown): {
  known: boolean;
  present: boolean;
  tags: ResolutionJsonValue[];
} {
  if (value !== undefined) {
    if (!Array.isArray(value)) return { known: false, present: true, tags: [] };
    const valid = value.every((tag) => isRecord(tag)
      && typeof tag.id === 'string' && typeof tag.name === 'string');
    return {
      known: valid,
      present: true,
      tags: valid ? value.map((tag) => cloneJson(tag)) : [],
    };
  }
  if (!Array.isArray(existing)) return { known: false, present: false, tags: [] };
  const valid = existing.every((tag) => isRecord(tag)
    && typeof tag.id === 'string' && typeof tag.name === 'string');
  return {
    known: valid,
    present: false,
    tags: valid ? existing.map((tag) => cloneJson(tag)) : [],
  };
}

function taskMetadataWithCanonicalTags(
  db: ResolutionSqlite,
  entityId: string,
  value: Record<string, unknown>,
): unknown {
  const row = getValue(db,
    'SELECT task_metadata FROM tasks WHERE id = ? LIMIT 1;', [entityId],
  ) as DbRow | null;
  const existing = row?.task_metadata == null ? {} : parseJson(row.task_metadata);
  const existingMetadata = isRecord(existing) ? existing : {};
  const serverMetadata = objectField(value, 'task_metadata', 'taskMetadata', 'metadata');
  const hasServerMetadata = isRecord(serverMetadata);
  const explicitTags = Object.prototype.hasOwnProperty.call(value, 'tags')
    && value.tags_complete !== false
    ? canonicalTaskTags(value.tags, undefined)
    : {
      known: false,
      present: Object.prototype.hasOwnProperty.call(value, 'tags'),
      tags: [],
    };
  if (!hasServerMetadata && !explicitTags.known) return undefined;
  const metadata = hasServerMetadata
    ? cloneJson(serverMetadata) as ResolutionJsonObject
    : cloneJson(existingMetadata) as ResolutionJsonObject;
  const previousTags = existingMetadata[TASK_CACHED_TAGS_KEY];
  const tags = explicitTags.known
    ? { known: true, tags: explicitTags.tags }
    : canonicalTaskTags(undefined, previousTags);
  if (explicitTags.known || tags.known) metadata[TASK_CACHED_TAGS_KEY] = tags.tags;
  else if (Object.prototype.hasOwnProperty.call(existingMetadata, TASK_CACHED_TAGS_KEY)) {
    metadata[TASK_CACHED_TAGS_KEY] = cloneJson(previousTags);
  }
  return metadata;
}

function presentSqlValues(
  values: Array<[string, unknown]>,
): Array<[string, unknown]> {
  return values.filter((entry): entry is [string, unknown] => entry[1] !== undefined);
}

function applyTaskCanonical(db: ResolutionSqlite, entityId: string, server: ResolutionServerSnapshot): void {
  if (!isRecord(server.value)) throw new ResolutionAbort('server_changed');
  const value = server.value as Record<string, unknown>;
  const canonicalMetadata = taskMetadataWithCanonicalTags(db, entityId, value);
  const values = presentSqlValues([
    ['project_id', objectField(value, 'project_id', 'projectId')],
    ['title', objectField(value, 'title')],
    ['description', objectField(value, 'description')],
    ['status', objectField(value, 'status')],
    ['priority', objectField(value, 'priority')],
    ['start_at', objectField(value, 'start_at', 'startAt')],
    ['end_at', objectField(value, 'end_at', 'endAt')],
    ['all_day', objectField(value, 'all_day', 'allDay')],
    ['reminder_offsets', objectField(value, 'reminder_offsets', 'reminderOffsets')],
    ['notifications_enabled', objectField(value, 'notifications_enabled', 'notificationsEnabled')],
    ['auto_close_on_due', objectField(value, 'auto_close_on_due', 'autoCloseOnDue')],
    ['source', objectField(value, 'source')],
    ['created_by', objectField(value, 'created_by', 'createdBy')],
    ['completed_at', objectField(value, 'completed_at', 'completedAt')],
    ['archived_at', objectField(value, 'archived_at', 'archivedAt')],
    ['estimated_hours', objectField(value, 'estimated_hours', 'estimatedHours')],
    ['parent_task_id', objectField(value, 'parent_task_id', 'parentTaskId')],
    ['task_metadata', canonicalMetadata],
    ['sort_order', objectField(value, 'sort_order', 'sortOrder')],
    ['created_at', objectField(value, 'created_at', 'createdAt')],
    ['updated_at', objectField(value, 'updated_at', 'updatedAt')],
    ['deleted_at', objectField(value, 'deleted_at', 'deletedAt')],
  ]);
  if (!values.some(([key]) => key === 'updated_at')) throw new ResolutionAbort('server_version_missing');
  const set = values.map(([key]) => `${key} = ?`).join(', ');
  const params = values.map(([, item]) => toSqlValue(item));
  const result = runSql(db, `UPDATE tasks SET ${set} WHERE id = ?;`, [...params, entityId]) as { changes?: number } | undefined;
  if (!result || typeof result.changes === 'undefined' || Number(result.changes) !== 1) {
    throw new ResolutionAbort('local_snapshot_changed');
  }
}

/** Merge only canonical aggregate fields returned by the selected Task GET.
 * Unknown comments/subtasks/attachments and unrelated cache keys stay intact;
 * a missing assignee/tag collection is never interpreted as an empty clear. */
function mergeTaskDetailCacheCanonical(
  db: ResolutionSqlite,
  entityId: string,
  server: ResolutionServerSnapshot,
): void {
  if (!isRecord(server.value)) return;
  const value = server.value as Record<string, unknown>;
  const aggregate = (
    key: string,
    completeKey: string,
    validator: (item: unknown) => boolean,
  ): { known: boolean; present: boolean; values: ResolutionJsonValue[] } => {
    const present = Object.prototype.hasOwnProperty.call(value, key);
    if (!present || value[completeKey] === false) return { known: false, present, values: [] };
    if (!Array.isArray(value[key])) return { known: false, present: true, values: [] };
    const known = value[key].every(validator);
    return { known, present: true, values: known ? value[key].map((item) => cloneJson(item)) : [] };
  };
  const assignees = aggregate(
    'assignees',
    'assignees_complete',
    (item) => isRecord(item)
      && typeof item.user_id === 'string',
  );
  const tags = aggregate(
    'tags',
    'tags_complete',
    (item) => isRecord(item)
      && typeof item.id === 'string' && typeof item.name === 'string',
  );
  if (!assignees.known && !tags.known) return;
  const key = `task:${entityId}`;
  const row = getValue(db,
    `SELECT payload_json FROM task_detail_cache WHERE cache_key = ? LIMIT 1;`, [key],
  ) as DbRow | null;
  let payload: Record<string, unknown> = {};
  if (row?.payload_json != null) {
    const parsed = parseJson(row.payload_json);
    if (!isRecord(parsed)) return;
    payload = parsed;
  }
  if (assignees.known) {
    payload.assignees = assignees.values;
    payload.assignees_complete = value.assignees_complete === false ? false : true;
  }
  if (tags.known) {
    payload.tags = tags.values;
    payload.tags_complete = value.tags_complete === false ? false : true;
  }
  const cachedAt = new Date().toISOString();
  const result = runSql(db,
    `INSERT INTO task_detail_cache(cache_key, payload_json, cached_at)
       VALUES (?, ?, ?)
       ON CONFLICT(cache_key) DO UPDATE SET
         payload_json = excluded.payload_json,
         cached_at = excluded.cached_at;`,
    [key, jsonText(payload), cachedAt],
  ) as { changes?: number } | undefined;
  if (!result || typeof result.changes === 'undefined' || Number(result.changes) !== 1) {
    throw new ResolutionAbort('cache_snapshot_changed');
  }
}

function applyDocsCanonical(db: ResolutionSqlite, entityId: string, server: ResolutionServerSnapshot): void {
  if (!isRecord(server.value)) throw new ResolutionAbort('server_changed');
  const value = server.value as Record<string, unknown>;
  const values = presentSqlValues([
    ['title', objectField(value, 'title')],
    ['aliases', objectField(value, 'aliases')],
    ['description', objectField(value, 'description')],
    ['body_json', objectField(value, 'body_json', 'bodyJson')],
    ['body_text', objectField(value, 'body_text', 'bodyText')],
    ['display_props', objectField(value, 'display_props', 'displayProps')],
    ['query_json', objectField(value, 'query_json', 'queryJson')],
    ['view_json', objectField(value, 'view_json', 'viewJson')],
    ['day_date', objectField(value, 'day_date', 'dayDate')],
    ['sort_order', objectField(value, 'sort_order', 'sortOrder')],
    ['updated_by', objectField(value, 'updated_by', 'updatedBy')],
    ['updated_at', objectField(value, 'updated_at', 'updatedAt')],
    ['server_updated_at', objectField(value, 'updated_at', 'updatedAt')],
    ['dirty', false],
    ['conflict_payload', null],
  ]);
  if (!values.some(([key]) => key === 'updated_at')) throw new ResolutionAbort('server_version_missing');
  const set = values.map(([key]) => `${key} = ?`).join(', ');
  const params = values.map(([, item]) => toSqlValue(item));
  const result = runSql(db, `UPDATE knowledge_nodes SET ${set} WHERE id = ?;`, [...params, entityId]) as { changes?: number } | undefined;
  if (!result || typeof result.changes === 'undefined' || Number(result.changes) !== 1) {
    throw new ResolutionAbort('local_snapshot_changed');
  }
}

/** Docs adoption is content-only.  Parent/root/project/type/system identity
 * must already be identical in the inspected local row and live node; a
 * structural change is held for a normal validated Docs sync. */
function docsStructureMatches(current: DbRow | null, server: ResolutionServerSnapshot): boolean {
  if (!current || !isRecord(server.value)) return false;
  const value = server.value as Record<string, unknown>;
  const structural: Array<[string, string[]]> = [
    ['workspace_id', ['workspace_id', 'workspaceId']],
    ['parent_id', ['parent_id', 'parentId']],
    ['root_page_id', ['root_page_id', 'rootPageId']],
    ['project_id', ['project_id', 'projectId']],
    ['system_key', ['system_key', 'systemKey']],
    ['node_type', ['node_type', 'nodeType']],
  ];
  for (const [column, keys] of structural) {
    const serverValue = objectField(value, ...keys);
    if (serverValue === undefined) return false;
    const localValue = current[column];
    if (!valuesEqualForColumn(localValue, serverValue)) return false;
  }
  // ACL/source fields are also guarded when the live response includes them;
  // they are never overwritten by this content-only operation.
  for (const [column, keys] of [
    ['source', ['source']],
    ['access', ['access']],
    ['read_only', ['read_only', 'readOnly']],
  ] as Array<[string, string[]]>) {
    const serverValue = objectField(value, ...keys);
    if (serverValue !== undefined && !valuesEqualForColumn(current[column], serverValue)) return false;
  }
  return true;
}

function checkGuard(preview: ResolutionPreview): ResolutionResultReason | null {
  const guard = preview.capture.guard;
  if (preview.capture.kind === 'docs'
    && preview.capture.local.editorAncestryComplete !== true) return 'scope_changed';
  if (guard.inflightSync) return 'sync_inflight';
  if (!guard.uniqueEntity) return 'duplicate_entity';
  if (guard.acl !== 'writable') return guard.acl === 'read_only' ? 'acl_changed' : 'scope_changed';
  if (!guard.scopeProven) return 'scope_changed';
  if (guard.unsupportedRelatedValues) return 'unsupported';
  if (!preview.view.localComplete) return 'local_snapshot_changed';
  if (!preview.view.canAdopt) return 'not_adoptable';
  if (preview.view.serverSource
    && !['live_get', 'live_current'].includes(preview.view.serverSource)) return 'not_adoptable';
  if (preview.view.serverComplete === false || preview.capture.server.serverComplete === false) {
    return 'not_adoptable';
  }
  if (preview.capture.kind === 'task' && preview.capture.server.taskAggregatesComplete === false) {
    return 'not_adoptable';
  }
  if (preview.capture.server.state === 'not_found') return 'server_not_found';
  if (preview.capture.server.state === 'forbidden') return 'server_forbidden';
  if (preview.capture.server.state === 'unavailable') return 'server_unavailable';
  if (preview.capture.server.state !== 'present') return 'unsupported';
  return null;
}

function canPreserveMissingLocal(preview: ResolutionPreview): boolean {
  const capture = preview.capture;
  if (capture.local.row != null || capture.local.known === false) return false;
  if (capture.server.state !== 'not_found') return false;
  if (typeof capture.operation.payload !== 'string' || !capture.operation.payload.trim()) return false;
  try {
    const payload = JSON.parse(capture.operation.payload) as unknown;
    return isRecord(payload);
  } catch {
    return false;
  }
}

/** Preparation is allowed to retain a held record when the live server value
 * is unavailable, but all local ownership/editor/sync observations must still
 * be complete before that record is durable. */
function checkPreparationGuard(preview: ResolutionPreview): ResolutionResultReason | null {
  const guard = preview.capture.guard;
  if (preview.capture.kind === 'docs'
    && preview.capture.local.editorAncestryComplete !== true) return 'scope_changed';
  if (guard.inflightSync) return 'sync_inflight';
  if (!guard.uniqueEntity) return 'duplicate_entity';
  const missingLocal = canPreserveMissingLocal(preview);
  if (!guard.scopeProven && !missingLocal && preview.capture.kind !== 'task') return 'scope_changed';
  if (guard.unsupportedRelatedValues && !missingLocal) return 'unsupported';
  if (!preview.view.localComplete && !missingLocal) return 'local_snapshot_changed';
  return null;
}

function verifyMembership(db: ResolutionSqlite, preview: ResolutionPreview): boolean {
  if (preview.capture.kind !== 'docs') return true;
  return verifyMembershipSnapshot(
    db,
    preview.capture.context,
    preview.capture.operation,
    preview.capture.docsScopeKey,
    preview.capture.membership,
  );
}

function verifyMembershipSnapshot(
  db: ResolutionSqlite,
  context: ResolutionContext,
  operation: ResolutionOperationSnapshot,
  docsScopeKey: string | null,
  expected: ResolutionJsonValue,
): boolean {
  if (!docsScopeKey) return false;
  const rows = getRows<DbRow>(db,
    `SELECT auth_scope, scope_key, scope_id, project_id, table_name, entity_key,
       state, access, read_only, updated_at
       FROM docs_scope_membership
       WHERE auth_scope = ? AND scope_key = ? AND table_name = ? AND entity_key = ?;`,
    [context.authScope, docsScopeKey, operation.tableName, operation.entityId],
  );
  if (Array.isArray(expected)) {
    return equalValue(rows, expected)
      || equalValue(rows.map(projectMembershipRow), expected);
  }
  return rows.length > 0;
}

function verifyEditorAncestry(
  db: ResolutionSqlite,
  context: ResolutionContext,
  operation: ResolutionOperationSnapshot,
  local: ResolutionLocalSnapshot,
): ResolutionResultReason | null {
  if (operation.tableName !== 'knowledge_nodes') return null;
  if (local.editorAncestryComplete !== true) return 'scope_changed';
  const ancestors = local.editorAncestors ?? [];
  if (!ancestors.length) return null;
  const scope = operation.docsScopeKey;
  if (!scope) return 'scope_changed';
  const separator = scope.indexOf('|project:');
  const workspaceId = separator >= 0 ? scope.slice(0, separator) : '';
  const projectId = separator >= 0 ? scope.slice(separator + '|project:'.length) : '';
  for (const ancestor of ancestors) {
    const row = getValue(db,
      `SELECT workspace_id, project_id, parent_id, root_page_id FROM knowledge_nodes
         WHERE id = ? LIMIT 1;`, [ancestor.id],
    ) as DbRow | null;
    if (!row) return 'local_snapshot_changed';
    const same = String(row.workspace_id ?? '') === workspaceId
      && String(row.project_id ?? '') === projectId
      && (row.parent_id ?? null) === ancestor.parentId
      && (row.root_page_id ?? null) === ancestor.rootPageId;
    if (!same) return 'related_snapshot_changed';
    const membership = getRows<DbRow>(db,
      `SELECT state, access, read_only FROM docs_scope_membership
         WHERE auth_scope = ? AND scope_key = ? AND table_name = 'knowledge_nodes' AND entity_key = ?;`,
      [context.authScope, scope, ancestor.id],
    );
    if (!membership.some((item) => item.state === 'active' || item.state === 'readonly')) {
      return 'scope_changed';
    }
  }
  return null;
}

function verifyDocsAdoptability(
  db: ResolutionSqlite,
  preview: ResolutionPreview,
  local: DbRow | null,
  server: ResolutionServerSnapshot,
): ResolutionResultReason | null {
  if (preview.capture.kind !== 'docs') return null;
  if (!local || !isRecord(server.value) || !preview.capture.docsScopeKey) return 'scope_changed';
  const localSystem = String(local.system_key ?? local.systemKey ?? '');
  const serverValue = server.value as Record<string, unknown>;
  const serverSystem = String(serverValue.system_key ?? serverValue.systemKey ?? '');
  if (localSystem === 'project_information_root'
    || localSystem.startsWith('project_information:')
    || serverSystem === 'project_information_root'
    || serverSystem.startsWith('project_information:')) return 'unsupported';
  if (local.archived_at != null || local.archivedAt != null
    || serverValue.archived_at != null || serverValue.archivedAt != null) return 'unsupported';
  if (local.access === 'read' || local.read_only === 1 || local.read_only === true
    || serverValue.access === 'read' || serverValue.read_only === true) return 'acl_changed';
  if (typeof serverValue.access !== 'string' || typeof serverValue.read_only !== 'boolean') {
    return 'acl_changed';
  }
  const rows = getRows<DbRow>(db,
    `SELECT state, access, read_only FROM docs_scope_membership
       WHERE auth_scope = ? AND scope_key = ? AND table_name = 'knowledge_nodes' AND entity_key = ?;`,
    [preview.capture.context.authScope, preview.capture.docsScopeKey, preview.capture.operation.entityId],
  );
  const writable = rows.some((row) =>
    row.state === 'active'
    && typeof row.access === 'string'
    && row.access !== 'read'
    && (row.read_only === false || row.read_only === 0));
  return writable ? null : 'acl_changed';
}

/** Re-read the related snapshots inside the same transaction as the local
 * update.  The facade performs a before/after read around the live GET; this
 * last synchronous check closes the gap after that read and before commit. */
function verifyRelatedSnapshots(
  db: ResolutionSqlite,
  snapshot: ResolutionInternalFullSnapshot,
): void {
  if (snapshot.kind === 'task') {
    const cacheDescriptor = isRecord(snapshot.cache)
      && typeof snapshot.cache.cacheKey === 'string'
      && typeof snapshot.cache.cachedAt === 'string'
      ? snapshot.cache
      : null;
    const cachedAt = snapshot.local.relatedRevision
      ?? (cacheDescriptor ? String(cacheDescriptor.cachedAt) : null);
    const row = getValue(db,
      `SELECT payload_json, cached_at FROM task_detail_cache WHERE cache_key = ? LIMIT 1;`,
      [`task:${snapshot.operation.entityId}`],
    ) as DbRow | null;
    if (!cachedAt) {
      if (snapshot.local.relatedKnown === false) throw new ResolutionAbort('cache_snapshot_changed');
      // A captured known absence remains part of the CAS. A cache row that
      // appears after inspect is a scene change even when its payload is not
      // exposed to the UI/export.
      if (row) throw new ResolutionAbort('cache_snapshot_changed');
      return;
    }
    if (!row || String(row.cached_at ?? '') !== cachedAt) throw new ResolutionAbort('cache_snapshot_changed');
    if (cacheDescriptor) return;
    const payload = parseJson(row.payload_json) ?? {};
    if (!equalValue(payload, snapshot.cache)) throw new ResolutionAbort('cache_snapshot_changed');
    return;
  }
  if (snapshot.kind !== 'docs') return;
  if (isRecord(snapshot.related)) {
    const related = snapshot.related;
    const expectedNodes = Array.isArray(related.nodes)
      ? related.nodes.filter((item): item is ResolutionJsonObject => isRecord(item))
      : [];
    const targetId = snapshot.operation.entityId;
    const expectedById = new Map(
      expectedNodes
        .filter((item) => typeof item.id === 'string')
        .map((item) => [item.id as string, item]),
    );
    if (!expectedById.has(targetId)) throw new ResolutionAbort('related_snapshot_changed');
    const scopeRows = getRows<DbRow>(db,
      `SELECT entity_key FROM docs_scope_membership
         WHERE auth_scope = ? AND scope_key = ? AND table_name = 'knowledge_nodes'
           AND state IN ('active', 'readonly');`,
      [snapshot.context.authScope, snapshot.docsScopeKey],
    );
    const scopedIds = scopeRows
      .map((row) => row.entity_key)
      .filter((item): item is string => typeof item === 'string');
    if (!scopedIds.includes(targetId)) throw new ResolutionAbort('related_snapshot_changed');
    const scopedMarks = scopedIds.map(() => '?').join(', ');
    const scopedRows = getRows<DbRow>(db,
      `SELECT * FROM knowledge_nodes WHERE id IN (${scopedMarks});`, scopedIds,
    ).map((row) => projectDbRow('knowledge_nodes', row));
    const scopeParts = snapshot.docsScopeKey?.split('|project:') ?? [];
    const workspaceId = scopeParts.length === 2 ? scopeParts[0] : null;
    const projectId = scopeParts.length === 2 && scopeParts[1] ? scopeParts[1] : null;
    if (!workspaceId || scopedRows.some((row) =>
      row.workspaceId !== workspaceId || (row.projectId ?? null) !== projectId)) {
      throw new ResolutionAbort('membership_changed');
    }
    const children = new Map<string, string[]>();
    for (const row of scopedRows) {
      if (typeof row.id !== 'string' || typeof row.parentId !== 'string') continue;
      const list = children.get(row.parentId) ?? [];
      list.push(row.id);
      children.set(row.parentId, list);
    }
    const currentIds = new Set<string>();
    const pending = [targetId];
    while (pending.length) {
      const id = pending.shift()!;
      if (currentIds.has(id)) continue;
      currentIds.add(id);
      for (const child of children.get(id) ?? []) pending.push(child);
    }
    const expectedIds = new Set(expectedById.keys());
    if (currentIds.size !== expectedIds.size || [...currentIds].some((id) => !expectedIds.has(id))) {
      throw new ResolutionAbort('related_snapshot_changed');
    }
    const ids = [...currentIds];
    const placeholders = ids.map(() => '?').join(', ');
    const rows = getRows<DbRow>(db,
      `SELECT * FROM knowledge_nodes WHERE id IN (${placeholders});`, ids,
    );
    const currentNodes = rows.map((row) => projectDbRow('knowledge_nodes', row))
      .sort((left, right) => String(left.id ?? '').localeCompare(String(right.id ?? '')));
    const expectedSorted = [...expectedById.values()]
      .sort((left, right) => String(left.id ?? '').localeCompare(String(right.id ?? '')));
    if (!equalValue(currentNodes, expectedSorted)) {
      throw new ResolutionAbort('related_snapshot_changed');
    }
    if (currentNodes.some((row) =>
      row.workspaceId !== workspaceId
      || (row.projectId ?? null) !== projectId)) {
      throw new ResolutionAbort('membership_changed');
    }
    const readRelatedRows = (
      table: string,
      column: string,
      values: string[],
    ): DbRow[] => {
      if (!values.length) return [];
      const marks = values.map(() => '?').join(', ');
      return getRows<DbRow>(db, `SELECT * FROM ${table} WHERE ${column} IN (${marks});`, values);
    };
    const expectedSupertags = Array.isArray(related.nodeSupertags)
      ? related.nodeSupertags.filter((item): item is ResolutionJsonObject => isRecord(item))
      : [];
    const currentSupertags = readRelatedRows('knowledge_node_supertags', 'node_id', ids)
      .map((row) => projectDbRow('knowledge_node_supertags', row))
      .sort((left, right) => `${left.nodeId}:${left.supertagId}`.localeCompare(`${right.nodeId}:${right.supertagId}`));
    if (!equalValue(currentSupertags, expectedSupertags.sort((left, right) =>
      `${left.nodeId ?? left.node_id}:${left.supertagId ?? left.supertag_id}`
        .localeCompare(`${right.nodeId ?? right.node_id}:${right.supertagId ?? right.supertag_id}`)))) {
      throw new ResolutionAbort('related_snapshot_changed');
    }
    const expectedFieldValues = Array.isArray(related.fieldValues)
      ? related.fieldValues.filter((item): item is ResolutionJsonObject => isRecord(item))
      : [];
    const currentFieldValues = readRelatedRows('knowledge_field_values', 'node_id', ids)
      .map((row) => projectDbRow('knowledge_field_values', row))
      .sort((left, right) => `${left.nodeId}:${left.fieldId}`.localeCompare(`${right.nodeId}:${right.fieldId}`));
    if (!equalValue(currentFieldValues, expectedFieldValues.sort((left, right) =>
      `${left.nodeId ?? left.node_id}:${left.fieldId ?? left.field_id}`
        .localeCompare(`${right.nodeId ?? right.node_id}:${right.fieldId ?? right.field_id}`)))) {
      throw new ResolutionAbort('related_snapshot_changed');
    }
    const expectedPlacements = Array.isArray(related.placements)
      ? related.placements.filter((item): item is ResolutionJsonObject => isRecord(item))
      : [];
    const currentPlacements = getRows<DbRow>(db,
      `SELECT * FROM knowledge_node_placements WHERE node_id IN (${ids.map(() => '?').join(',')})
       OR parent_node_id IN (${ids.map(() => '?').join(',')});`, [...ids, ...ids],
    ).map((row) => projectDbRow('knowledge_node_placements', row))
      .filter((row) => ids.includes(String(row.nodeId)) && ids.includes(String(row.parentNodeId)))
      .sort((left, right) => String(left.id ?? '').localeCompare(String(right.id ?? '')));
    if (!equalValue(currentPlacements, expectedPlacements.sort((left, right) =>
      String(left.id).localeCompare(String(right.id))))) {
      throw new ResolutionAbort('related_snapshot_changed');
    }
    const expectedEdges = Array.isArray(related.edges)
      ? related.edges.filter((item): item is ResolutionJsonObject => isRecord(item))
      : [];
    const currentEdges = getRows<DbRow>(db,
      `SELECT * FROM knowledge_edges WHERE source_node_id IN (${ids.map(() => '?').join(',')})
       OR target_node_id IN (${ids.map(() => '?').join(',')});`, [...ids, ...ids],
    ).map((row) => projectDbRow('knowledge_edges', row))
      .filter((row) => ids.includes(String(row.sourceNodeId)) && ids.includes(String(row.targetNodeId)))
      .sort((left, right) => String(left.id ?? '').localeCompare(String(right.id ?? '')));
    if (!equalValue(currentEdges, expectedEdges.sort((left, right) =>
      String(left.id).localeCompare(String(right.id))))) {
      throw new ResolutionAbort('related_snapshot_changed');
    }
    return;
  }
  if (!Array.isArray(snapshot.related)) return;
  const memberships = Array.isArray(snapshot.membership)
    ? snapshot.membership.filter((item): item is ResolutionJsonObject => isRecord(item))
    : [];
  const expected = snapshot.related
    .filter((item): item is ResolutionJsonObject => isRecord(item));
  const targetId = snapshot.operation.entityId;
  const expectedById = new Map(
    expected
      .filter((item) => typeof item.id === 'string')
      .map((item) => [item.id as string, item]),
  );
  if (!expectedById.has(targetId)) throw new ResolutionAbort('related_snapshot_changed');

  // Recompute the current target subtree from the durable scope projection so
  // a child/relation added after capture cannot hide behind a stale ID list.
  const scopeRows = getRows<DbRow>(db,
    `SELECT entity_key FROM docs_scope_membership
       WHERE auth_scope = ? AND scope_key = ? AND table_name = 'knowledge_nodes'
         AND state IN ('active', 'readonly');`,
    [snapshot.context.authScope, snapshot.docsScopeKey],
  );
  const scopedIds = scopeRows
    .map((row) => row.entity_key)
    .filter((item): item is string => typeof item === 'string');
  if (!scopedIds.includes(targetId)) throw new ResolutionAbort('related_snapshot_changed');
  const placeholders = scopedIds.map(() => '?').join(', ');
  const rows = getRows<DbRow>(db,
    `SELECT * FROM knowledge_nodes WHERE id IN (${placeholders});`, scopedIds,
  );
  const projectedRows = rows.map((row) => projectDbRow('knowledge_nodes', row));
  const byId = new Map(projectedRows
    .filter((row) => typeof row.id === 'string')
    .map((row) => [row.id as string, row]));
  const scopeKey = snapshot.docsScopeKey ?? '';
  const separator = scopeKey.indexOf('|project:');
  const workspaceId = separator >= 0 ? scopeKey.slice(0, separator) : '';
  const projectId = separator >= 0 ? scopeKey.slice(separator + '|project:'.length) : null;
  for (const row of projectedRows) {
    if (!currentIdsForRowScope(row, scopedIds, targetId)) continue;
    if (workspaceId && String(row.workspaceId ?? '') !== workspaceId) {
      throw new ResolutionAbort('scope_changed');
    }
    const rowProject = row.projectId == null ? '' : String(row.projectId);
    if (projectId !== null && rowProject !== projectId) throw new ResolutionAbort('scope_changed');
  }
  const children = new Map<string, string[]>();
  for (const row of projectedRows) {
    if (typeof row.id !== 'string' || typeof row.parentId !== 'string') continue;
    const list = children.get(row.parentId) ?? [];
    list.push(row.id);
    children.set(row.parentId, list);
  }
  const currentIds = new Set<string>();
  const pending = [targetId];
  while (pending.length) {
    const id = pending.shift()!;
    if (currentIds.has(id)) continue;
    currentIds.add(id);
    for (const child of children.get(id) ?? []) pending.push(child);
  }
  const expectedIds = new Set(expectedById.keys());
  if (currentIds.size !== expectedIds.size || [...currentIds].some((id) => !expectedIds.has(id))) {
    throw new ResolutionAbort('related_snapshot_changed');
  }
  const current = [...currentIds]
    .map((id) => byId.get(id))
    .filter((row): row is DbRow => row !== undefined)
    .sort((left, right) => String(left.id ?? '').localeCompare(String(right.id ?? '')));
  if (current.length !== expectedIds.size) throw new ResolutionAbort('related_snapshot_changed');
  const expectedSorted = [...expectedById.values()]
    .sort((left, right) => String(left.id ?? '').localeCompare(String(right.id ?? '')));
  if (!equalValue(current, expectedSorted)) throw new ResolutionAbort('related_snapshot_changed');

  const expectedRelations = new Set(memberships
    .filter((item) => item.tableName !== 'knowledge_nodes' && item.table_name !== 'knowledge_nodes')
    .map((item) => `${String(item.tableName ?? item.table_name ?? '')}:${String(item.entityKey ?? item.entity_key ?? '')}`));
  if (expectedRelations.size) {
    const currentRelations = getRows<DbRow>(db,
      `SELECT table_name, entity_key FROM docs_scope_membership
         WHERE auth_scope = ? AND scope_key = ? AND state IN ('active', 'readonly')
           AND table_name <> 'knowledge_nodes';`,
      [snapshot.context.authScope, snapshot.docsScopeKey],
    )
      .filter((row) => typeof row.table_name === 'string' && typeof row.entity_key === 'string')
      .filter((row) => [...currentIds].some((id) =>
        new RegExp(`(^|:)${id.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}(:|$)`).test(String(row.entity_key))))
      .map((row) => `${String(row.table_name)}:${String(row.entity_key)}`);
    const currentRelationSet = new Set(currentRelations);
    if (currentRelationSet.size !== expectedRelations.size
      || [...currentRelationSet].some((key) => !expectedRelations.has(key))) {
      throw new ResolutionAbort('related_snapshot_changed');
    }
  }
}

function currentIdsForRowScope(
  row: DbRow,
  scopedIds: readonly string[],
  targetId: string,
): boolean {
  // Scope validation is applied to rows that can participate in the selected
  // target subtree. The helper keeps the guard explicit without exposing an
  // ancestor/sibling ID list outside this private transaction.
  return typeof row.id === 'string' && (scopedIds.includes(row.id) || row.id === targetId);
}

function draftFields(
  inputFields: ResolutionField[] | undefined,
  record: ResolutionRecordView,
  portable: ResolutionPortableSnapshot,
): ResolutionField[] {
  const existing = new Map(
    (inputFields?.length ? normalizeFields(inputFields) : record.view.fields)
      .map((field) => [field.key, field] as const),
  );
  const target = isRecord(portable.target) ? portable.target : {};
  for (const [key, value] of Object.entries(target)) {
    if (existing.has(key)) continue;
    existing.set(key, {
      key,
      device: fieldValue(value),
      server: null,
      serverKnown: false,
      comparison: 'unknown',
    });
  }
  return [...existing.values()];
}

function buildDraft(
  input: ResolutionDraftInput,
  record: ResolutionRecordView,
  portable: ResolutionPortableSnapshot,
  operation: ResolutionOperationSnapshot,
  previousDraft: ResolutionDraft | null,
  now: number,
): ResolutionDraft {
  const fields = draftFields(input.fields, record, portable);
  const previous = previousDraft?.changes ?? {};
  const target = isRecord(portable.target) ? portable.target as ResolutionJsonObject : {};
  let queued: ResolutionJsonObject = {};
  try {
    const payload = JSON.parse(operation.payload) as unknown;
    if (isRecord(payload)) queued = payload as ResolutionJsonObject;
  } catch {
    queued = {};
  }
  const original = mergeJsonObjects(target, queued);
  const changes = Object.keys(input.changes).length === 0
    ? (Object.keys(previous).length ? previous : original)
    : mergeJsonObjects(Object.keys(previous).length ? previous : original, input.changes);
  return {
    restoredAt: input.restoredAt ?? nowIso(now),
    fields,
    changes: cloneJson(changes) as ResolutionJsonObject,
  };
}

function mergeJsonObjects(
  base: ResolutionJsonObject,
  patch: ResolutionJsonObject,
): ResolutionJsonObject {
  const output = cloneJson(base) as ResolutionJsonObject;
  for (const [key, value] of Object.entries(patch)) {
    const current = output[key];
    if (isRecord(current) && isRecord(value)) {
      output[key] = mergeJsonObjects(current as ResolutionJsonObject, value as ResolutionJsonObject);
    } else {
      output[key] = cloneJson(value);
    }
  }
  return output;
}

function journalValues(preview: ResolutionPreview, now: number, id: string) {
  const capture = preview.capture;
  const operation = normalizeOperation(capture.operation);
  const context = normalizeContext(capture.context);
  if (!operation || !context) throw new ResolutionAbort('invalid_input');
  const view = normalizeView(preview.view);
  const portable = normalizePortable(capture.portable);
  const target = targetMetadataForCapture(capture);
  if (portable.target == null) {
    try {
      const payload = JSON.parse(operation.payload) as unknown;
      if (isRecord(payload)) {
        portable.target = redactJson(payload);
        portable.incomplete = [...(portable.incomplete ?? []), 'local_row_missing'];
      }
    } catch {
      portable.incomplete = [...(portable.incomplete ?? []), 'operation_payload_invalid'];
    }
  }
  const state: ResolutionLifecycleState = view.canPrepare
    && view.canAdopt
    && capture.server.state === 'present'
    && capture.server.serverComplete !== false
    && capture.guard.acl === 'writable'
    ? 'prepared'
    : 'held';
  return {
    id,
    revision: 1,
    authScope: context.authScope,
    serverFingerprint: context.serverFingerprint,
    credentialIdentityRevision: context.credentialIdentityRevision,
    kind: capture.kind,
    tableName: operation.tableName,
    entityId: operation.entityId,
    opId: operation.opId,
    createdAt: operation.createdAt,
    state,
    operationOrigin: target.operationOrigin,
    targetNamespaceJson: jsonText(target, {}),
    operationSnapshotJson: jsonText(operation),
    localSnapshotJson: jsonText(capture.local),
    relatedSnapshotJson: jsonText(capture.related, {}),
    membershipSnapshotJson: jsonText(capture.membership, []),
    cacheSnapshotJson: jsonText(capture.cache, {}),
    guardSnapshotJson: jsonText(capture.guard, {}),
    serverSnapshotJson: jsonText(capture.server),
    viewJson: jsonText(view, null, true),
    draftJson: null,
    exportSnapshotJson: jsonText(portable, {}),
    updatedAt: now,
  };
}

function insertJournal(db: ResolutionSqlite, preview: ResolutionPreview, now: number, id: string): void {
  const values = journalValues(preview, now, id);
  runSql(db,
    `INSERT INTO ${JOURNAL_TABLE}(
       id, revision, auth_scope, server_fingerprint, credential_identity_revision,
       kind, table_name, entity_id, op_id, created_at, state,
       operation_origin, target_namespace_json,
       operation_snapshot_json, local_snapshot_json, related_snapshot_json,
       membership_snapshot_json, cache_snapshot_json, guard_snapshot_json,
       server_snapshot_json, view_json, draft_json, export_snapshot_json, updated_at
     ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);`,
    [values.id, values.revision, values.authScope, values.serverFingerprint,
      values.credentialIdentityRevision, values.kind, values.tableName, values.entityId,
      values.opId, values.createdAt, values.state, values.operationOrigin,
      values.targetNamespaceJson, values.operationSnapshotJson,
      values.localSnapshotJson, values.relatedSnapshotJson, values.membershipSnapshotJson,
      values.cacheSnapshotJson, values.guardSnapshotJson, values.serverSnapshotJson,
      values.viewJson, values.draftJson, values.exportSnapshotJson, values.updatedAt],
  );
}

function captureFromRow(row: DbJournalRow): ResolutionInternalFullSnapshot | null {
  const operation = normalizeOperation(parseJson(row.operation_snapshot_json));
  const context = rowContext(row);
  const local = normalizeLocal(parseJson(row.local_snapshot_json));
  if (!operation || !context) return null;
  return {
    context,
    operation,
    kind: row.kind === 'task' || row.kind === 'docs' || row.kind === 'unsupported'
      ? row.kind
      : 'unsupported',
    docsScopeKey: operation.docsScopeKey,
    local,
    related: parseJson(row.related_snapshot_json) ?? {},
    membership: parseJson(row.membership_snapshot_json) ?? [],
    cache: parseJson(row.cache_snapshot_json) ?? {},
    guard: normalizeGuard(parseJson(row.guard_snapshot_json)),
    server: normalizeServer(parseJson(row.server_snapshot_json)),
    portable: normalizePortable(parseJson(row.export_snapshot_json)),
    target: (() => {
      const value = parseJson(row.target_namespace_json);
      return isRecord(value) ? value as unknown as ResolutionTargetMetadata : undefined;
    })(),
  };
}

function compareCapture(stored: ResolutionInternalFullSnapshot, current: ResolutionPreview): ResolutionResultReason | null {
  const next = current.capture;
  // Durable history is bound to the account/server namespace. Credential
  // generation is a per-call freshness fence, not a permanent history key;
  // token refresh or app restart must not make an otherwise safe record
  // unreadable or prevent a new full CAS capture from proceeding.
  if (!sameResolutionNamespace(stored.context, next.context)) return 'context_changed';
  if (!equalValue(
    stored.target ?? defaultTargetMetadata(stored.context, stored.operation),
    targetMetadataForCapture(next),
  )) return 'outbox_replaced';
  if (!sameOperation(stored.operation, next.operation)) return 'outbox_replaced';
  if (!equalValue(stored.local, normalizeLocal(next.local))) return 'local_snapshot_changed';
  if (!equalValue(stored.related, next.related)) return 'related_snapshot_changed';
  if (!equalValue(stored.membership, next.membership)) return 'membership_changed';
  if (!equalValue(stored.cache, next.cache)) return 'cache_snapshot_changed';
  const storedGuard = { ...stored.guard, editorRevision: 0 };
  const currentGuard = { ...normalizeGuard(next.guard), editorRevision: 0 };
  if (!equalValue(storedGuard, currentGuard)) return 'editor_changed';
  const storedServer = { ...stored.server, liveReadAt: null, serverCapturedAt: null };
  const currentServer = { ...normalizeServer(next.server), liveReadAt: null, serverCapturedAt: null };
  if (!equalValue(storedServer, currentServer)) return 'server_changed';
  return null;
}

function semanticCaptureEqual(
  stored: ResolutionInternalFullSnapshot,
  current: ResolutionPreview,
): boolean {
  if (!sameResolutionNamespace(stored.context, current.capture.context)) return false;
  if (!sameOperation(stored.operation, current.capture.operation)) return false;
  if (!equalValue(stored.local, normalizeLocal(current.capture.local))) return false;
  if (!equalValue(stored.related, current.capture.related)
    || !equalValue(stored.membership, current.capture.membership)
    || !equalValue(stored.cache, current.capture.cache)) return false;
  const storedGuard = { ...stored.guard, editorRevision: 0 };
  const currentGuard = { ...normalizeGuard(current.capture.guard), editorRevision: 0 };
  if (!equalValue(storedGuard, currentGuard)) return false;
  const storedServer = { ...stored.server, liveReadAt: null, serverCapturedAt: null };
  const currentServer = { ...normalizeServer(current.capture.server), liveReadAt: null, serverCapturedAt: null };
  if (!equalValue(storedServer, currentServer)) return false;
  if (!equalValue(stored.portable, normalizePortable(current.capture.portable))) return false;
  return equalValue(
    stored.target ?? defaultTargetMetadata(stored.context, stored.operation),
    targetMetadataForCapture(current.capture),
  );
}

function findSemanticJournalRecord(
  db: ResolutionSqlite,
  preview: ResolutionPreview,
): DbJournalRow | null {
  const rows = getRows<DbJournalRow>(db,
    `SELECT * FROM ${JOURNAL_TABLE}
       WHERE auth_scope = ? AND server_fingerprint = ? AND op_id = ?
       ORDER BY created_at DESC, id DESC;`,
    [preview.capture.context.authScope, preview.capture.context.serverFingerprint,
      preview.capture.operation.opId],
  );
  for (const row of rows) {
    const stored = captureFromRow(row);
    if (stored && semanticCaptureEqual(stored, preview)) return row;
  }
  return null;
}

function draftSceneReason(
  expected: ResolutionDraftSceneSnapshot,
  actual: ResolutionDraftSceneSnapshot,
): ResolutionResultReason | null {
  if (!sameResolutionContext(expected.context, actual.context)) return 'context_changed';
  if (expected.operation == null || actual.operation == null) {
    if (expected.operation !== actual.operation) return 'outbox_replaced';
  } else if (!sameOperation(expected.operation, actual.operation)) {
    return 'outbox_replaced';
  }
  if (!equalValue(expected.local, actual.local)) return 'local_snapshot_changed';
  if (!equalValue(expected.related, actual.related)) return 'related_snapshot_changed';
  if (!equalValue(expected.membership, actual.membership)) return 'membership_changed';
  if (!equalValue(expected.cache, actual.cache)) return 'cache_snapshot_changed';
  if (!equalValue(expected.guard, actual.guard)) return 'editor_changed';
  return null;
}

function sceneAsInternal(
  stored: ResolutionInternalFullSnapshot,
  scene: ResolutionDraftSceneSnapshot,
): ResolutionInternalFullSnapshot {
  return {
    ...stored,
    context: scene.context,
    operation: scene.operation ?? stored.operation,
    local: scene.local,
    related: scene.related,
    membership: scene.membership,
    cache: scene.cache,
    guard: scene.guard,
  };
}

function updateJournalState(
  db: ResolutionSqlite,
  id: string,
  authScope: string,
  revision: number,
  state: ResolutionLifecycleState,
  view: ResolutionPreviewView,
  now: number,
  draft?: ResolutionDraft | null,
): void {
  const result = runSql(db,
    `UPDATE ${JOURNAL_TABLE}
       SET revision = ?, state = ?, view_json = ?, draft_json = COALESCE(?, draft_json), updated_at = ?
       WHERE id = ? AND auth_scope = ? AND revision = ?;`,
    [revision + 1, state, jsonText(view, null, true), draft == null ? null : jsonText(draft), now, id, authScope, revision],
  ) as { changes?: number } | undefined;
  if (!result || typeof result.changes === 'undefined' || Number(result.changes) !== 1) {
    throw new ResolutionAbort('revision_mismatch');
  }
}

function safeServerFingerprint(value: unknown): string {
  if (typeof value !== 'string' || !value.trim()) return 'unknown';
  try {
    const parsed = new URL(value);
    if (parsed.username || parsed.password || parsed.search || parsed.hash) return 'unknown';
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return 'unknown';
    const path = parsed.pathname.replace(/\/+$/, '');
    return `${parsed.protocol}//${parsed.host}${path}`;
  } catch {
    return 'unknown';
  }
}

function safeExport(
  row: DbJournalRow,
  record: ResolutionRecordView,
  freshContext?: ResolutionContext,
): ResolutionExport {
  const portable = normalizePortable(parseJson(row.export_snapshot_json));
  const privateDraft = normalizePrivateDraft(parseJson(row.draft_json));
  const portableChildren = record.item.kind === 'docs' ? portable.children : undefined;
  const incomplete = [
    ...(portable.incomplete ?? []),
    ...(record.item.kind === 'task' && portable.children != null
      ? ['task_detail_cache_private']
      : []),
    ...(containsPrivateKey(portable.target)
      || containsPrivateKey(portable.children)
      || containsPrivateKey(privateDraft?.changes)
      || (portable.fields ?? []).some((field) => PRIVATE_KEYS.test(field.key))
      ? ['redacted_private_fields']
      : []),
  ];
  const payload = {
    schema: 'aoitalk-outbox-resolution-export/v1',
    provenance: {
      operationOrigin: 'unrecorded' as const,
      comparisonTarget: safeServerFingerprint(row.server_fingerprint),
    },
    record: record.item,
    view: record.view,
    target: portable.target,
    ...(portableChildren == null ? {} : { children: portableChildren }),
    fields: portable.fields ?? record.view.fields,
    incomplete,
    ...(record.draft ? { draft: record.draft } : {}),
  };
  const id = asString(row.id, 'record').replace(/[^a-zA-Z0-9._-]+/g, '_');
  const context: ResolutionContext = freshContext ?? {
    authScope: asString(row.auth_scope),
    serverFingerprint: asString(row.server_fingerprint),
    credentialIdentityRevision: asString(row.credential_identity_revision),
  };
  return {
    json: JSON.stringify(redactJson(payload), null, 2),
    context,
    fileName: `aoitalk-outbox-resolution-${id}.json`,
  };
}

/** Production default context provider. Focused storage tests inject a
 * DatabaseSync and intentionally omit this provider so they remain auth-free;
 * the app-private default store still fences a queued write immediately before
 * entering its synchronous SQLite transaction. */
async function defaultCurrentContext(): Promise<ResolutionContext> {
  const auth = require('../lib/auth') as {
    getTokenSnapshot: () => Promise<{ token: string | null; revision: number }>;
    getTokenAuthScope: (token: string | null | undefined) => string;
  };
  const api = require('../lib/api-client') as {
    getConfiguredApiServerFingerprint: () => Promise<string>;
    getCredentialIdentityRevision: () => Promise<string>;
  };
  const [snapshot, serverFingerprint, credentialIdentityRevision] = await Promise.all([
    auth.getTokenSnapshot(),
    api.getConfiguredApiServerFingerprint(),
    api.getCredentialIdentityRevision(),
  ]);
  return {
    authScope: auth.getTokenAuthScope(snapshot.token),
    serverFingerprint,
    credentialIdentityRevision,
  };
}

function createStore(deps: ResolutionStoreDependencies = {}): OutboxResolutionStore {
  if (!deps.currentContext && !deps.sqlite) {
    deps = { ...deps, currentContext: defaultCurrentContext };
  }
  const now = deps.now ?? (() => Date.now());
  const idFactory = deps.id;

  const prepare = async (preview: ResolutionPreview): Promise<ResolutionResult<ResolutionRecordView>> => {
    const invalid = validatePreview(preview);
    if (invalid) return { ok: false, reason: invalid };
    try {
      assertContext(preview.capture.context);
      return await runForegroundSqliteWrite(async () => {
        if (!await revalidateTarget(deps, preview, 'prepare')) {
          return { ok: false, reason: 'not_prepareable' };
        }
        if (!await currentContextMatches(deps, preview.capture.context, true)) {
          return { ok: false, reason: 'context_changed' };
        }
        const db = ensureJournal(deps);
        const preparationReason = checkPreparationGuard(preview);
        if (preparationReason) return { ok: false, reason: preparationReason };
        const current = currentOutbox(db, preview.capture.operation.opId);
        if (!current || !sameOperation(current, preview.capture.operation)) {
          return { ok: false, reason: 'outbox_replaced' };
        }
        const local = currentLocalRow(db, preview.capture.operation);
        if (!localMatches(preview.capture.operation.tableName, local, preview.capture.local)) {
          return { ok: false, reason: 'local_snapshot_changed' };
        }
        const ancestryReason = verifyEditorAncestry(
          db, preview.capture.context, preview.capture.operation, preview.capture.local,
        );
        if (ancestryReason) return { ok: false, reason: ancestryReason };
        if (hasOtherEntityOperations(db, preview.capture.operation)) {
          return { ok: false, reason: 'duplicate_entity' };
        }
        verifyRelatedSnapshots(db, preview.capture);
        if (!verifyMembership(db, preview)) return { ok: false, reason: 'membership_changed' };
        if (!assertTargetInTransaction(deps, preview, 'prepare')) {
          return { ok: false, reason: 'not_prepareable' };
        }
        const existing = findSemanticJournalRecord(db, preview);
        if (existing) return { ok: true, value: rowRecordView(existing) };
        const id = safeRecordId(idFactory);
        const timestamp = now();
        insertJournal(db, preview, timestamp, id);
        const row = rowById(db, id, preview.capture.context.authScope, preview.capture.context);
        if (!row) return { ok: false, reason: 'journal_unavailable' };
        return { ok: true, value: rowRecordView(row) };
      });
    } catch (error) {
      if (error instanceof ResolutionAbort) return { ok: false, reason: error.reason };
      return { ok: false, reason: 'journal_unavailable' };
    }
  };

  const adopt = async (input: ResolutionAdoptInput): Promise<ResolutionResult<ResolutionRecordView>> => {
    if (!input || typeof input.recordId !== 'string' || !input.recordId
      || typeof input.authScope !== 'string' || !input.authScope
      || !Number.isInteger(input.expectedRevision)) {
      return { ok: false, reason: 'invalid_input' };
    }
    const invalid = validatePreview(input.preview);
    if (invalid) return { ok: false, reason: invalid };
    const guardReason = checkGuard(input.preview);
    if (guardReason) return { ok: false, reason: guardReason };
    if (input.preview.capture.context.authScope !== input.authScope) {
      return { ok: false, reason: 'auth_scope_changed' };
    }
    try {
      const db = ensureJournal(deps);
      const initial = rowById(db, input.recordId, input.authScope, input.preview.capture.context);
      if (!initial) return { ok: false, reason: 'record_not_found' };
      if (asNumber(initial.revision) !== input.expectedRevision) {
        return { ok: false, reason: 'revision_mismatch' };
      }
      const stored = captureFromRow(initial);
      if (!stored) return { ok: false, reason: 'journal_unavailable' };
      const captureReason = compareCapture(stored, input.preview);
      if (captureReason) return { ok: false, reason: captureReason };
      let result: ResolutionRecordView | null = null;
      await runForegroundSqliteWrite(async () => {
        if (!await revalidateTarget(deps, input.preview, 'adopt')) {
          throw new ResolutionAbort('not_adoptable');
        }
        if (!await currentContextMatches(deps, input.preview.capture.context, true)) {
          throw new ResolutionAbort('context_changed');
        }
        return withTransaction(db, () => {
          const row = rowById(db, input.recordId, input.authScope, input.preview.capture.context);
          if (!row) throw new ResolutionAbort('record_not_found');
          const revision = asNumber(row.revision);
          if (revision !== input.expectedRevision) throw new ResolutionAbort('revision_mismatch');
          const current = currentOutbox(db, stored.operation.opId);
          if (!current || !sameOperation(current, stored.operation)) {
            throw new ResolutionAbort('outbox_replaced');
          }
          // Same entity IDs can be visible through multiple accounts/scopes.
          // Count only metadata for other operations; never read their payload
          // or project their values into the resolution UI.
          if (hasOtherEntityOperations(db, stored.operation)) {
            throw new ResolutionAbort('duplicate_entity');
          }
          if (!assertTargetInTransaction(deps, input.preview, 'adopt')) {
            throw new ResolutionAbort('not_adoptable');
          }
          const currentLocal = currentLocalRow(db, stored.operation);
          if (!localMatches(stored.operation.tableName, currentLocal, stored.local)) {
            throw new ResolutionAbort('local_snapshot_changed');
          }
          const ancestryReason = verifyEditorAncestry(
            db, input.preview.capture.context, stored.operation, stored.local,
          );
          if (ancestryReason) throw new ResolutionAbort(ancestryReason);
          verifyRelatedSnapshots(db, stored);
          if (!verifyMembership(db, input.preview)) throw new ResolutionAbort('membership_changed');
          const docsGuardReason = verifyDocsAdoptability(db, input.preview, currentLocal, stored.server);
          if (docsGuardReason) throw new ResolutionAbort(docsGuardReason);
          if (stored.kind === 'task') {
            applyTaskCanonical(db, stored.operation.entityId, stored.server);
            mergeTaskDetailCacheCanonical(db, stored.operation.entityId, stored.server);
          }
          else if (stored.kind === 'docs' && stored.operation.tableName === 'knowledge_nodes') {
            if (!docsStructureMatches(currentLocal, stored.server)) throw new ResolutionAbort('unsupported');
            applyDocsCanonical(db, stored.operation.entityId, stored.server);
          } else {
            throw new ResolutionAbort('unsupported');
          }
          safeDeleteOutbox(db, stored.operation);
          const nextView: ResolutionPreviewView = {
            ...normalizeView(parseSafeJson(row.view_json), input.preview.view),
            checkedAt: nowIso(now()),
            canAdopt: false,
            reason: null,
          };
          updateJournalState(db, input.recordId, input.authScope, revision, 'adopted', nextView, now());
          const updated = rowById(db, input.recordId, input.authScope, input.preview.capture.context);
          if (!updated) throw new ResolutionAbort('journal_unavailable');
          result = rowRecordView(updated);
        });
      });
      return result ? { ok: true, value: result } : { ok: false, reason: 'transaction_failed' };
    } catch (error) {
      if (error instanceof ResolutionAbort) return { ok: false, reason: error.reason };
      return { ok: false, reason: 'transaction_failed' };
    }
  };

  const saveDraft = async (input: ResolutionDraftInput): Promise<ResolutionResult<ResolutionRecordView>> => {
    if (!input || typeof input.recordId !== 'string' || !input.recordId
      || typeof input.authScope !== 'string' || !input.authScope
      || !Number.isInteger(input.expectedRevision)
      || !isRecord(input.changes)) return { ok: false, reason: 'invalid_input' };
    try {
      const db = ensureJournal(deps);
      const initial = rowById(db, input.recordId, input.authScope, input.context);
      if (!initial) return { ok: false, reason: 'record_not_found' };
      if (asNumber(initial.revision) !== input.expectedRevision) return { ok: false, reason: 'revision_mismatch' };
      const stored = captureFromRow(initial);
      if (!stored) return { ok: false, reason: 'journal_unavailable' };
      const draftContext = input.context ?? stored.context;
      let sceneBefore: ResolutionDraftSceneSnapshot | null = null;
      const captureScene = deps.captureDraftScene;
      const sceneRequest: ResolutionDraftSceneRequest = {
        recordId: input.recordId,
        authScope: input.authScope,
        context: draftContext,
        operation: stored.operation,
      };
      if (captureScene) {
        sceneBefore = await captureScene(sceneRequest);
        if (!sameResolutionContext(sceneBefore.context, draftContext)) {
          return { ok: false, reason: 'context_changed' };
        }
      }
      if (input.preview) {
        const invalid = validatePreview(input.preview);
        if (invalid) return { ok: false, reason: invalid };
        const captureReason = compareCapture(stored, input.preview);
        if (captureReason) return { ok: false, reason: captureReason };
      }
      if (input.expectedOutboxAbsent && currentOutbox(db, stored.operation.opId)) {
        return { ok: false, reason: 'outbox_replaced' };
      }
      const record = rowRecordView(initial);
      const previousDraft = normalizePrivateDraft(parseJson(initial.draft_json));
      // Restore is idempotent. Once a private draft exists, the restore
      // action must return that immutable user edit rather than replacing it
      // with the empty `changes:{}` restore payload.
      if (record.draft && Object.keys(input.changes).length === 0) {
        if (sceneBefore && captureScene) {
          const sceneAfter = await captureScene(sceneRequest);
          const sceneReason = draftSceneReason(sceneBefore, sceneAfter);
          if (sceneReason) return { ok: false, reason: sceneReason };
          if (deps.assertDraftSceneInTransaction && !deps.assertDraftSceneInTransaction(sceneAfter)) {
            return { ok: false, reason: 'editor_changed' };
          }
        }
        if (!await currentContextMatches(deps, draftContext, Boolean(input.context || sceneBefore))) {
          return { ok: false, reason: 'context_changed' };
        }
        return { ok: true, value: record };
      }
      const draft = buildDraft(input, record, stored.portable, stored.operation, previousDraft, now());
      let output: ResolutionRecordView | null = null;
      await runForegroundSqliteWrite(async () => {
        const expectedContext = input.preview?.capture.context ?? input.context ?? draftContext;
        let sceneAfter: ResolutionDraftSceneSnapshot | null = null;
        if (sceneBefore && captureScene) {
          sceneAfter = await captureScene(sceneRequest);
          const sceneReason = draftSceneReason(sceneBefore, sceneAfter);
          if (sceneReason) throw new ResolutionAbort(sceneReason);
          if (deps.assertDraftSceneInTransaction && !deps.assertDraftSceneInTransaction(sceneAfter)) {
            throw new ResolutionAbort('editor_changed');
          }
        }
        if (!await currentContextMatches(
          deps,
          expectedContext,
          Boolean(input.preview || input.context || sceneBefore),
        )) throw new ResolutionAbort('context_changed');
        return withTransaction(db, () => {
          const row = rowById(db, input.recordId, input.authScope, input.context);
          if (!row) throw new ResolutionAbort('record_not_found');
          const revision = asNumber(row.revision);
          if (revision !== input.expectedRevision) throw new ResolutionAbort('revision_mismatch');
          if (input.expectedOutboxAbsent && currentOutbox(db, stored.operation.opId)) {
            throw new ResolutionAbort('outbox_replaced');
          }
          if (sceneAfter) {
            const current = currentOutbox(db, stored.operation.opId);
            if (sceneAfter.operation == null) {
              if (current) throw new ResolutionAbort('outbox_replaced');
            } else if (!current || !sameOperation(current, sceneAfter.operation)) {
              throw new ResolutionAbort('outbox_replaced');
            }
            const currentLocal = currentLocalRow(db, stored.operation);
            if (!localMatches(stored.operation.tableName, currentLocal, sceneAfter.local)) {
              throw new ResolutionAbort('local_snapshot_changed');
            }
            const ancestryReason = verifyEditorAncestry(
              db, sceneAfter.context, stored.operation, sceneAfter.local,
            );
            if (ancestryReason) throw new ResolutionAbort(ancestryReason);
            verifyRelatedSnapshots(db, sceneAsInternal(stored, sceneAfter));
            if (stored.kind === 'docs' && !verifyMembershipSnapshot(
              db,
              sceneAfter.context,
              stored.operation,
              stored.docsScopeKey,
              sceneAfter.membership,
            )) throw new ResolutionAbort('membership_changed');
          }
          const view = normalizeView(parseSafeJson(row.view_json));
          updateJournalState(db, input.recordId, input.authScope, revision, 'draft', view, now(), draft);
          const updated = rowById(db, input.recordId, input.authScope, input.context);
          if (!updated) throw new ResolutionAbort('journal_unavailable');
          output = rowRecordView(updated);
        });
      });
      return output ? { ok: true, value: output } : { ok: false, reason: 'transaction_failed' };
    } catch (error) {
      if (error instanceof ResolutionAbort) return { ok: false, reason: error.reason };
      return { ok: false, reason: 'transaction_failed' };
    }
  };

  const restoreDraft = async (input: ResolutionDraftInput): Promise<ResolutionResult<ResolutionRecordView>> =>
    saveDraft(input);

  const read = async (
    recordId: string,
    authScope: string,
    context?: ResolutionContext,
  ): Promise<ResolutionResult<ResolutionRecordView>> => {
    if (!recordId || !authScope) return { ok: false, reason: 'invalid_input' };
    try {
      const db = ensureJournal(deps);
      const row = rowById(db, recordId, authScope, context);
      return row ? { ok: true, value: rowRecordView(row) } : { ok: false, reason: 'record_not_found' };
    } catch {
      return { ok: false, reason: 'journal_unavailable' };
    }
  };

  const list = async (
    authScope: string,
    context?: ResolutionContext,
  ): Promise<ResolutionResult<ResolutionHistoryItem[]>> => {
    if (!authScope) return { ok: false, reason: 'invalid_input' };
    if (context && context.authScope !== authScope) return { ok: false, reason: 'context_changed' };
    try {
      const db = ensureJournal(deps);
      const serverClause = context ? ' AND server_fingerprint = ?' : '';
      const rows = getRows<DbJournalRow>(db,
        `SELECT id, revision, auth_scope, server_fingerprint, kind, entity_id,
           op_id, created_at, state,
           CASE WHEN draft_json IS NULL THEN 0 ELSE 1 END AS has_draft
           FROM ${JOURNAL_TABLE}
           WHERE auth_scope = ?${serverClause}
           ORDER BY created_at DESC, id DESC;`,
        context ? [authScope, context.serverFingerprint] : [authScope],
      );
      return {
        ok: true,
        value: rows.map((row) => rowItem(row, null, asNumber(row.has_draft) > 0)),
      };
    } catch {
      return { ok: false, reason: 'journal_unavailable' };
    }
  };

  const exportRecord = async (
    recordId: string,
    authScope: string,
    expectedRevision: number,
    context?: ResolutionContext,
  ): Promise<ResolutionResult<ResolutionExport>> => {
    if (!recordId || !authScope || !Number.isInteger(expectedRevision)) return { ok: false, reason: 'invalid_input' };
    try {
      const db = ensureJournal(deps);
      const row = rowById(db, recordId, authScope, context);
      if (!row) return { ok: false, reason: 'record_not_found' };
      if (asNumber(row.revision) !== expectedRevision) return { ok: false, reason: 'revision_mismatch' };
      const record = rowRecordView(row);
      if (!row.export_snapshot_json) return { ok: false, reason: 'export_unavailable' };
      return { ok: true, value: safeExport(row, record, context) };
    } catch {
      return { ok: false, reason: 'export_unavailable' };
    }
  };

  return { prepare, adopt, restoreDraft, saveDraft, read, list, export: exportRecord };
}

export function createOutboxResolutionStore(
  deps: ResolutionStoreDependencies = {},
): OutboxResolutionStore {
  return createStore(deps);
}

