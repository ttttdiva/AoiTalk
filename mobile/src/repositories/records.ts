import { eq } from "drizzle-orm";
import { getDb, schema } from "../db/client";
import { runForegroundSqliteWrite } from "../db/sqlite-write-coordinator";
import {
  assertActiveWriter,
  type SqliteHandle,
  type SqliteWriter,
} from "../db/sqlite-writer";
import type { RecordField, RecordRow, RecordTable } from "../types/api";

export function applyRemoteRecordTablesInWriter(
  writer: SqliteWriter,
  tx: SqliteHandle,
  list: RecordTable[],
): void {
  assertActiveWriter(writer);
  if (!list.length) return;
  const now = new Date().toISOString();
  for (const table of list) {
    tx
      .insert(schema.recordTables)
      .values({
        id: table.id,
        projectId: table.project_id,
        name: table.name,
        description: table.description ?? null,
        icon: table.icon ?? null,
        sortOrder: table.sort_order ?? 0,
        schemaVersion: table.schema_version ?? 1,
        memoryPolicy: table.memory_policy ?? "manual",
        defaultSensitivity: table.default_sensitivity ?? "normal",
        tableMetadata: table.metadata ?? {},
        createdBy: table.created_by ?? null,
        createdAt: table.created_at ?? now,
        updatedAt: table.updated_at ?? now,
        deletedAt: null,
      })
      .onConflictDoUpdate({
        target: schema.recordTables.id,
        set: {
          projectId: table.project_id,
          name: table.name,
          description: table.description ?? null,
          icon: table.icon ?? null,
          sortOrder: table.sort_order ?? 0,
          schemaVersion: table.schema_version ?? 1,
          memoryPolicy: table.memory_policy ?? "manual",
          defaultSensitivity: table.default_sensitivity ?? "normal",
          tableMetadata: table.metadata ?? {},
          createdBy: table.created_by ?? null,
          updatedAt: table.updated_at ?? now,
          deletedAt: null,
        },
      })
      .run();
  }
}

export async function applyRemoteRecordTables(
  list: RecordTable[],
): Promise<void> {
  await runForegroundSqliteWrite((writer) =>
    getDb().transaction((tx) => applyRemoteRecordTablesInWriter(writer, tx, list)),
  );
}

export function applyRecordTableTombstonesInWriter(
  writer: SqliteWriter,
  tx: SqliteHandle,
  tombstones: Array<{ id: string; deleted_at?: string | null }>,
): void {
  assertActiveWriter(writer);
  for (const item of tombstones) {
    const deletedAt = item.deleted_at ?? new Date().toISOString();
    tx
      .update(schema.recordTables)
      .set({ deletedAt, updatedAt: deletedAt })
      .where(eq(schema.recordTables.id, item.id))
      .run();
  }
}

export async function applyRecordTableTombstones(
  tombstones: Array<{ id: string; deleted_at?: string | null }>,
): Promise<void> {
  await runForegroundSqliteWrite((writer) =>
    getDb().transaction((tx) => applyRecordTableTombstonesInWriter(writer, tx, tombstones)),
  );
}

export function applyRemoteRecordFieldsInWriter(
  writer: SqliteWriter,
  tx: SqliteHandle,
  list: RecordField[],
): void {
  assertActiveWriter(writer);
  if (!list.length) return;
  const now = new Date().toISOString();
  for (const field of list) {
    tx
      .insert(schema.recordFields)
      .values({
        id: field.id,
        tableId: field.table_id,
        key: field.key,
        label: field.label,
        fieldType: field.field_type,
        options: field.options ?? {},
        required: Boolean(field.required),
        uniqueValue: Boolean(field.unique_value),
        sortOrder: field.sort_order ?? 0,
        isTitle: Boolean(field.is_title),
        isDue: Boolean(field.is_due),
        sensitivity: field.sensitivity ?? "normal",
        fieldMetadata: field.metadata ?? {},
        createdAt: field.created_at ?? now,
        updatedAt: field.updated_at ?? now,
        deletedAt: null,
      })
      .onConflictDoUpdate({
        target: schema.recordFields.id,
        set: {
          tableId: field.table_id,
          key: field.key,
          label: field.label,
          fieldType: field.field_type,
          options: field.options ?? {},
          required: Boolean(field.required),
          uniqueValue: Boolean(field.unique_value),
          sortOrder: field.sort_order ?? 0,
          isTitle: Boolean(field.is_title),
          isDue: Boolean(field.is_due),
          sensitivity: field.sensitivity ?? "normal",
          fieldMetadata: field.metadata ?? {},
          updatedAt: field.updated_at ?? now,
          deletedAt: null,
        },
      })
      .run();
  }
}

export async function applyRemoteRecordFields(
  list: RecordField[],
): Promise<void> {
  await runForegroundSqliteWrite((writer) =>
    getDb().transaction((tx) => applyRemoteRecordFieldsInWriter(writer, tx, list)),
  );
}

export function applyRecordFieldTombstonesInWriter(
  writer: SqliteWriter,
  tx: SqliteHandle,
  tombstones: Array<{ id: string; deleted_at?: string | null }>,
): void {
  assertActiveWriter(writer);
  for (const item of tombstones) {
    const deletedAt = item.deleted_at ?? new Date().toISOString();
    tx
      .update(schema.recordFields)
      .set({ deletedAt, updatedAt: deletedAt })
      .where(eq(schema.recordFields.id, item.id))
      .run();
  }
}

export async function applyRecordFieldTombstones(
  tombstones: Array<{ id: string; deleted_at?: string | null }>,
): Promise<void> {
  await runForegroundSqliteWrite((writer) =>
    getDb().transaction((tx) => applyRecordFieldTombstonesInWriter(writer, tx, tombstones)),
  );
}

export function applyRemoteRecordRowsInWriter(
  writer: SqliteWriter,
  tx: SqliteHandle,
  list: RecordRow[],
): void {
  assertActiveWriter(writer);
  if (!list.length) return;
  const now = new Date().toISOString();
  for (const row of list) {
    tx
      .insert(schema.recordRows)
      .values({
        id: row.id,
        tableId: row.table_id,
        projectId: row.project_id,
        createdBy: row.created_by ?? null,
        values: row.values ?? {},
        title: row.title ?? null,
        status: row.status ?? null,
        dueAt: row.due_at ?? null,
        searchText: row.search_text ?? null,
        sensitivity: row.sensitivity ?? "normal",
        rowMetadata: row.metadata ?? {},
        createdAt: row.created_at ?? now,
        updatedAt: row.updated_at ?? now,
        deletedAt: null,
      })
      .onConflictDoUpdate({
        target: schema.recordRows.id,
        set: {
          tableId: row.table_id,
          projectId: row.project_id,
          createdBy: row.created_by ?? null,
          values: row.values ?? {},
          title: row.title ?? null,
          status: row.status ?? null,
          dueAt: row.due_at ?? null,
          searchText: row.search_text ?? null,
          sensitivity: row.sensitivity ?? "normal",
          rowMetadata: row.metadata ?? {},
          updatedAt: row.updated_at ?? now,
          deletedAt: null,
        },
      })
      .run();
  }
}

export async function applyRemoteRecordRows(
  list: RecordRow[],
): Promise<void> {
  await runForegroundSqliteWrite((writer) =>
    getDb().transaction((tx) => applyRemoteRecordRowsInWriter(writer, tx, list)),
  );
}

export function applyRecordRowTombstonesInWriter(
  writer: SqliteWriter,
  tx: SqliteHandle,
  tombstones: Array<{ id: string; deleted_at?: string | null }>,
): void {
  assertActiveWriter(writer);
  for (const item of tombstones) {
    const deletedAt = item.deleted_at ?? new Date().toISOString();
    tx
      .update(schema.recordRows)
      .set({ deletedAt, updatedAt: deletedAt })
      .where(eq(schema.recordRows.id, item.id))
      .run();
  }
}

export async function applyRecordRowTombstones(
  tombstones: Array<{ id: string; deleted_at?: string | null }>,
): Promise<void> {
  await runForegroundSqliteWrite((writer) =>
    getDb().transaction((tx) => applyRecordRowTombstonesInWriter(writer, tx, tombstones)),
  );
}
