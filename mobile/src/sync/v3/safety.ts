import { and, eq, isNull, notExists, sql } from "drizzle-orm";
import { getDb, schema } from "../../db/client";

export type SyncV3Safety = { safe: boolean; unclassifiedRecords: number; reason: string | null };

/** Count every live record using indexed existence checks without guessing ownership. */
export async function checkSyncV3CacheSafety(authScope: string): Promise<SyncV3Safety> {
  const db = getDb();
  let unclassifiedRecords = 0;
  const tables = [
    ["record_tables", schema.recordTables],
    ["record_fields", schema.recordFields],
    ["record_rows", schema.recordRows],
  ] as const;
  for (const [name, table] of tables) {
    const row = await db.select({ value: sql<number>`count(*)` }).from(table).where(and(
      isNull(table.deletedAt),
      notExists(db.select({ one: sql`1` }).from(schema.syncV3Membership).where(and(
        eq(schema.syncV3Membership.authScope, authScope),
        eq(schema.syncV3Membership.tableName, name),
        eq(schema.syncV3Membership.entityId, table.id),
      ))),
      notExists(db.select({ one: sql`1` }).from(schema.outbox).where(and(
        eq(schema.outbox.authScope, authScope), eq(schema.outbox.tableName, name),
        eq(schema.outbox.entityId, table.id),
      ))),
      notExists(db.select({ one: sql`1` }).from(schema.syncV3Conflicts).where(and(
        eq(schema.syncV3Conflicts.authScope, authScope), eq(schema.syncV3Conflicts.state, "open"),
        eq(schema.syncV3Conflicts.tableName, name), eq(schema.syncV3Conflicts.entityId, table.id),
      ))),
    ));
    unclassifiedRecords += Number(row[0]?.value ?? 0);
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
