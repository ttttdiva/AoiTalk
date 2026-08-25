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
    await db
      .update(schema.outbox)
      .set({ action: 'delete', ...docsScopePatch })
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
): Promise<void> {
  const db = getDb();
  const authScope = await resolveAuthScope(requestedAuthScope);
  if (authScope === null) return;
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
    .orderBy(asc(schema.outbox.createdAt));
  const row = rows[0];
  if (!row) return;
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

export async function listOutboxConflicts(
  requestedAuthScope?: string | null,
) {
  const db = getDb();
  const authScope = await resolveAuthScope(requestedAuthScope);
  if (authScope === null) return [];
  return db
    .select()
    .from(schema.outbox)
    .where(
      and(
        scopePredicate(authScope),
        like(schema.outbox.lastError, 'conflict:%'),
      ),
    )
    .orderBy(asc(schema.outbox.createdAt));
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
): Promise<boolean> {
  const db = getDb();
  const result = await db
    .delete(schema.outbox)
    .where(
      and(
        eq(schema.outbox.opId, opId),
        eq(schema.outbox.tableName, snapshot.table),
        eq(schema.outbox.action, snapshot.action),
        eq(schema.outbox.entityId, snapshot.entityId),
        eq(schema.outbox.payload, snapshot.payload),
        snapshot.baseUpdatedAt === null
          ? isNull(schema.outbox.baseUpdatedAt)
          : eq(schema.outbox.baseUpdatedAt, snapshot.baseUpdatedAt),
      ),
    )
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
  await db
    .update(schema.outbox)
    .set({
      retryCount: (row?.retryCount ?? 0) + 1,
      lastError: `conflict:${error}`,
      ...(serverPayload === undefined ? {} : { conflictPayload: serverPayload as never }),
    })
    .where(eq(schema.outbox.opId, opId));
}
