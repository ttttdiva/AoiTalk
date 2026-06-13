import { eq } from "drizzle-orm";
import { getDb, schema } from "../db/client";
import type { RecordField, RecordRow, RecordTable } from "../types/api";

export async function applyRemoteRecordTables(
  list: RecordTable[],
): Promise<void> {
  if (!list.length) return;
  const db = getDb();
  const now = new Date().toISOString();
  for (const table of list) {
    await db
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
      });
  }
}

export async function applyRecordTableTombstones(
  tombstones: Array<{ id: string; deleted_at?: string | null }>,
): Promise<void> {
  const db = getDb();
  for (const item of tombstones) {
    const deletedAt = item.deleted_at ?? new Date().toISOString();
    await db
      .update(schema.recordTables)
      .set({ deletedAt, updatedAt: deletedAt })
      .where(eq(schema.recordTables.id, item.id));
  }
}

export async function applyRemoteRecordFields(
  list: RecordField[],
): Promise<void> {
  if (!list.length) return;
  const db = getDb();
  const now = new Date().toISOString();
  for (const field of list) {
    await db
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
      });
  }
}

export async function applyRecordFieldTombstones(
  tombstones: Array<{ id: string; deleted_at?: string | null }>,
): Promise<void> {
  const db = getDb();
  for (const item of tombstones) {
    const deletedAt = item.deleted_at ?? new Date().toISOString();
    await db
      .update(schema.recordFields)
      .set({ deletedAt, updatedAt: deletedAt })
      .where(eq(schema.recordFields.id, item.id));
  }
}

export async function applyRemoteRecordRows(list: RecordRow[]): Promise<void> {
  if (!list.length) return;
  const db = getDb();
  const now = new Date().toISOString();
  for (const row of list) {
    await db
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
      });
  }
}

export async function applyRecordRowTombstones(
  tombstones: Array<{ id: string; deleted_at?: string | null }>,
): Promise<void> {
  const db = getDb();
  for (const item of tombstones) {
    const deletedAt = item.deleted_at ?? new Date().toISOString();
    await db
      .update(schema.recordRows)
      .set({ deletedAt, updatedAt: deletedAt })
      .where(eq(schema.recordRows.id, item.id));
  }
}
