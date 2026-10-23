import { and, eq, isNull } from "drizzle-orm";
import { getDb, schema } from "../../db/client";
import { SYNC_V3_RECORD_TABLES, type SyncV3Table } from "./types";

export type SyncV3Safety = { safe: boolean; unclassifiedRecords: number; reason: string | null };

async function recordIds(table: SyncV3Table): Promise<Array<{ id: string }>> {
  const db = getDb();
  if (table === "record_tables") return db.select({ id: schema.recordTables.id }).from(schema.recordTables)
    .where(isNull(schema.recordTables.deletedAt));
  if (table === "record_fields") return db.select({ id: schema.recordFields.id }).from(schema.recordFields)
    .where(isNull(schema.recordFields.deletedAt));
  return db.select({ id: schema.recordRows.id }).from(schema.recordRows)
    .where(isNull(schema.recordRows.deletedAt));
}

/**
 * `clearLocalSyncCache` deliberately retains record_tables/fields/rows but
 * clears their account membership. V3 is stopped until each remaining row is
 * attributable to this account or is protected by its durable queue/conflict.
 * The lane never guesses ownership and never performs an unconditional delete.
 */
export async function checkSyncV3CacheSafety(authScope: string): Promise<SyncV3Safety> {
  const db = getDb();
  const [memberships, queue, conflicts] = await Promise.all([
    db.select({ table: schema.syncV3Membership.tableName, entityId: schema.syncV3Membership.entityId })
      .from(schema.syncV3Membership).where(eq(schema.syncV3Membership.authScope, authScope)),
    db.select({ table: schema.outbox.tableName, entityId: schema.outbox.entityId })
      .from(schema.outbox).where(eq(schema.outbox.authScope, authScope)),
    db.select({ table: schema.syncV3Conflicts.tableName, entityId: schema.syncV3Conflicts.entityId })
      .from(schema.syncV3Conflicts).where(and(
        eq(schema.syncV3Conflicts.authScope, authScope),
        eq(schema.syncV3Conflicts.state, "open"),
      )),
  ]);
  const protectedKeys = new Set([
    ...memberships.map((row) => `${row.table}:${row.entityId}`),
    ...queue.map((row) => `${row.table}:${row.entityId}`),
    ...conflicts.map((row) => `${row.table}:${row.entityId}`),
  ]);
  let unclassifiedRecords = 0;
  for (const table of SYNC_V3_RECORD_TABLES) {
    const rows = await recordIds(table);
    for (const row of rows) {
      if (!protectedKeys.has(`${table}:${row.id}`)) unclassifiedRecords += 1;
    }
  }
  return {
    safe: unclassifiedRecords === 0,
    unclassifiedRecords,
    reason: unclassifiedRecords ? "full_eviction_unclassified_records" : null,
  };
}

let manuallyStopped = false;
export function isSyncV3ManuallyStopped(): boolean { return manuallyStopped; }
export function setSyncV3ManuallyStopped(value: boolean): void { manuallyStopped = value; }
