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
import { asc, eq, and, isNull, like } from 'drizzle-orm';

/**
 * A stable identity for one conflict row.  Resolution handlers must carry
 * the complete operation snapshot rather than only an entity id: enqueue can
 * merge a newer edit into the same op id while a screen is open, and resolving
 * that newer row would otherwise silently discard the edit.
 */
export type OutboxConflictReference = {
  opId: string;
  tableName: string;
  action: string;
  entityId: string;
  payload: string;
  baseUpdatedAt: string | null;
  authScope: string;
  docsScopeKey: string | null;
  conflictPayload?: unknown;
};

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
async function resolveAuthScope(
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

function snapshotReference(row: {
  opId: string;
  tableName: string;
  action: string;
  entityId: string;
  payload: string;
  baseUpdatedAt: string | null;
  authScope: string | null;
  docsScopeKey: string | null;
  conflictPayload?: unknown;
}): OutboxConflictReference | null {
  if (!row.authScope) return null;
  return {
    opId: row.opId,
    tableName: row.tableName,
    action: row.action,
    entityId: row.entityId,
    payload: row.payload,
    baseUpdatedAt: row.baseUpdatedAt,
    authScope: row.authScope,
    docsScopeKey: row.docsScopeKey,
    conflictPayload: row.conflictPayload,
  };
}

function scopePredicate(
  authScope: string | null,
): ReturnType<typeof eq> | ReturnType<typeof isNull> {
  return authScope === null
    ? isNull(schema.outbox.authScope)
    : eq(schema.outbox.authScope, authScope);
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
      (row.retryCount < 5 ||
        (row.lastError ?? '').startsWith('conflict:')),
  );
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
  const inferredScopeKey = requestedDocsScopeKey
    ?? (table.startsWith('knowledge_') ? inferDocsScopeKeyFromPayload(payload) : null);
  const rows = await db
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
    .orderBy(asc(schema.outbox.createdAt));
  // If multiple sibling scopes share an entity UUID and the server payload
  // did not carry a composite identity, do not attach the snapshot to an
  // arbitrary row.  A later scoped pull can safely retry with its key.
  const row = rows.length === 1 ? rows[0] : inferredScopeKey ? rows[0] : null;
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
  await db
    .update(schema.outbox)
    .set({ conflictPayload: payload as never })
    .where(eq(schema.outbox.opId, row.opId));
}

export async function rebaseOutboxOp(
  opId: string,
  baseUpdatedAt: string | null,
  basePayload?: unknown,
): Promise<void> {
  const db = getDb();
  await db
    .update(schema.outbox)
    .set({
      baseUpdatedAt,
      ...(basePayload === undefined ? {} : { basePayload: basePayload as never }),
      conflictPayload: null,
      lastError: null,
      retryCount: 0,
    })
    .where(eq(schema.outbox.opId, opId));
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
      && current.authScope === expected.authScope
      && current.docsScopeKey === expected.docsScopeKey
      && JSON.stringify(current.conflictPayload ?? null)
        === JSON.stringify(expected.conflictPayload ?? null),
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
      eq(schema.outbox.opId, reference.opId),
      eq(schema.outbox.authScope, reference.authScope),
      eq(schema.outbox.tableName, reference.tableName),
      eq(schema.outbox.action, reference.action),
      eq(schema.outbox.entityId, reference.entityId),
      eq(schema.outbox.payload, reference.payload),
      reference.baseUpdatedAt === null
        ? isNull(schema.outbox.baseUpdatedAt)
        : eq(schema.outbox.baseUpdatedAt, reference.baseUpdatedAt),
      reference.docsScopeKey === null
        ? isNull(schema.outbox.docsScopeKey)
        : eq(schema.outbox.docsScopeKey, reference.docsScopeKey),
      like(schema.outbox.lastError, 'conflict:%'),
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
  snapshot: {
    table: string;
    action: string;
    entityId: string;
    payload: string;
    baseUpdatedAt: string | null;
  },
  guard: {
    authScope?: string | null;
    docsScopeKey?: string | null;
    requireConflict?: boolean;
  } = {},
): Promise<boolean> {
  const db = getDb();
  const conditions = [
    eq(schema.outbox.opId, opId),
    eq(schema.outbox.tableName, snapshot.table),
    eq(schema.outbox.action, snapshot.action),
    eq(schema.outbox.entityId, snapshot.entityId),
    eq(schema.outbox.payload, snapshot.payload),
    snapshot.baseUpdatedAt === null
      ? isNull(schema.outbox.baseUpdatedAt)
      : eq(schema.outbox.baseUpdatedAt, snapshot.baseUpdatedAt),
  ];
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

export async function markOutboxError(opId: string, error: string): Promise<void> {
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
    .where(eq(schema.outbox.opId, opId));
}

export async function markOutboxConflict(
  opId: string,
  error: string,
  serverPayload?: unknown,
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
    .where(eq(schema.outbox.opId, opId));
}
