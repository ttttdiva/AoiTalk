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
import { asc, eq, and, isNull, like } from 'drizzle-orm';

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
  const existing = await db
    .select()
    .from(schema.outbox)
    .where(
      and(
        eq(schema.outbox.tableName, op.table),
        eq(schema.outbox.entityId, op.entityId),
      ),
    )
    .orderBy(asc(schema.outbox.createdAt));
  const mergeable = existing[existing.length - 1];
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
      .set({ action: 'delete' })
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
    baseUpdatedAt: op.baseUpdatedAt ?? null,
    basePayload: op.basePayload ?? null,
    conflictPayload: null,
    retryCount: 0,
    lastError: null,
  });
  return opId;
}

export async function listPendingOutbox(limit?: number) {
  const db = getDb();
  const query = db
    .select()
    .from(schema.outbox)
    .orderBy(asc(schema.outbox.createdAt));
  const rows = limit ? await query.limit(limit) : await query;
  return rows.filter(
    (row) =>
      row.retryCount < 5 ||
      (row.lastError ?? '').startsWith('conflict:'),
  );
}

export async function hasPendingOutbox(table: string, entityId: string): Promise<boolean> {
  const db = getDb();
  const rows = await db
    .select({ opId: schema.outbox.opId })
    .from(schema.outbox)
    .where(and(eq(schema.outbox.tableName, table), eq(schema.outbox.entityId, entityId)))
    .limit(1);
  return rows.length > 0;
}

export async function recordOutboxServerSnapshot(
  table: string,
  entityId: string,
  payload: unknown,
): Promise<void> {
  const db = getDb();
  const rows = await db
    .select({ opId: schema.outbox.opId })
    .from(schema.outbox)
    .where(and(eq(schema.outbox.tableName, table), eq(schema.outbox.entityId, entityId)))
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

export async function listOutboxConflicts() {
  const db = getDb();
  return db
    .select()
    .from(schema.outbox)
    .where(like(schema.outbox.lastError, 'conflict:%'))
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
