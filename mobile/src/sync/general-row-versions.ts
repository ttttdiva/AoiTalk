import { and, eq, inArray, sql } from "drizzle-orm";
import { getDb, getSqlite, schema } from "../db/client";
import { runBackgroundSqliteWrite } from "../db/sqlite-write-coordinator";

export const GENERAL_ROW_MANIFEST_TABLES = [
  "tasks",
  "task_occurrences",
  "time_entries",
  "conversation_sessions",
] as const;

export type GeneralRowManifestTable = (typeof GENERAL_ROW_MANIFEST_TABLES)[number];
export type GeneralRowVersion = { id: string; version: string };

export function isGeneralRowManifestTable(table: string): table is GeneralRowManifestTable {
  return (GENERAL_ROW_MANIFEST_TABLES as readonly string[]).includes(table);
}

export function readGeneralRowVersions(
  authScope: string,
  table: GeneralRowManifestTable,
): Map<string, string> {
  const rows = getSqlite().getAllSync<{ row_id: unknown; version: unknown }>(`
    SELECT row_id, version
      FROM general_row_versions
     WHERE auth_scope = ? AND table_name = ?
  `, [authScope, table]);
  return new Map(rows.flatMap((row) =>
    typeof row.row_id === "string"
      && typeof row.version === "string"
      && /^[a-f0-9]{16}$/.test(row.version)
      ? [[row.row_id, row.version] as const]
      : [],
  ));
}

export function readPendingGeneralRowIds(
  authScope: string,
  table: GeneralRowManifestTable,
  ids: readonly string[],
): Set<string> {
  if (!ids.length) return new Set();
  const result = new Set<string>();
  for (let offset = 0; offset < ids.length; offset += 300) {
    const batch = ids.slice(offset, offset + 300);
    const placeholders = batch.map(() => "?").join(",");
    const rows = getSqlite().getAllSync<{ entity_id: unknown }>(`
      SELECT DISTINCT entity_id
        FROM outbox
       WHERE table_name = ?
         AND (auth_scope = ? OR auth_scope IS NULL)
         AND entity_id IN (${placeholders})
    `, [table, authScope, ...batch]);
    for (const row of rows) if (typeof row.entity_id === "string") result.add(row.entity_id);
  }
  return result;
}

export async function upsertGeneralRowVersions(
  authScope: string,
  table: GeneralRowManifestTable,
  versions: readonly GeneralRowVersion[],
): Promise<void> {
  if (!versions.length) return;
  await runBackgroundSqliteWrite(() => getDb().transaction((tx) => {
    for (let offset = 0; offset < versions.length; offset += 100) {
      const rows = versions.slice(offset, offset + 100).map((item) => ({
        authScope,
        tableName: table,
        rowId: item.id,
        version: item.version,
      }));
      tx.insert(schema.generalRowVersions)
        .values(rows)
        .onConflictDoUpdate({
          target: [
            schema.generalRowVersions.authScope,
            schema.generalRowVersions.tableName,
            schema.generalRowVersions.rowId,
          ],
          set: { version: sql`excluded.version` },
        })
        .run();
    }
  }));
}

export async function deleteGeneralRowVersions(
  authScope: string,
  table: GeneralRowManifestTable,
  ids: readonly string[],
): Promise<void> {
  if (!ids.length) return;
  await runBackgroundSqliteWrite(() => getDb().transaction((tx) => {
    for (let offset = 0; offset < ids.length; offset += 300) {
      tx.delete(schema.generalRowVersions)
        .where(and(
          eq(schema.generalRowVersions.authScope, authScope),
          eq(schema.generalRowVersions.tableName, table),
          inArray(schema.generalRowVersions.rowId, ids.slice(offset, offset + 300)),
        ))
        .run();
    }
  }));
}
