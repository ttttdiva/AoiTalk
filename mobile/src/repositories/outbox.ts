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
import { asc, eq } from 'drizzle-orm';

export function randomId(): string {
  // Lightweight uuid v4 generator (no dependency).
  // Sync engine only needs uniqueness, not crypto strength.
  const rand = () => Math.floor(Math.random() * 0x100000000).toString(16).padStart(8, '0');
  return `${rand()}-${rand()}-${rand()}-${rand()}`;
}

export async function enqueueOutbox(op: OutboxEnqueue): Promise<string> {
  const db = getDb();
  const opId = randomId();
  await db.insert(schema.outbox).values({
    opId,
    createdAt: Date.now(),
    tableName: op.table,
    action: op.action,
    entityId: op.entityId,
    payload: JSON.stringify(op.payload ?? {}),
    baseUpdatedAt: op.baseUpdatedAt ?? null,
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
    (row) => row.retryCount < 5 && !(row.lastError ?? '').startsWith('conflict:'),
  );
}

export async function removeOutboxOp(opId: string): Promise<void> {
  const db = getDb();
  await db.delete(schema.outbox).where(eq(schema.outbox.opId, opId));
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

export async function markOutboxConflict(opId: string, error: string): Promise<void> {
  await markOutboxError(opId, `conflict:${error}`);
}
