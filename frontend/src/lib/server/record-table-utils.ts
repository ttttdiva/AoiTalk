import { and, eq, isNull, sql } from "drizzle-orm";
import { db } from "@/db";
import {
  recordFields,
  recordRows,
  recordTables,
} from "@/db/schema";
import {
  decryptJsonValueIfNeeded,
  decryptTextIfNeeded,
  encryptJsonValue,
  encryptText,
} from "@/lib/server/field-crypto";

export type JsonRecord = Record<string, unknown>;

const RECORD_ROW_VALUES_AAD = "record_rows.values";
const RECORD_ROW_TITLE_AAD = "record_rows.title";
const RECORD_ROW_SEARCH_TEXT_AAD = "record_rows.search_text";

export const FIELD_TYPES = new Set([
  "text",
  "long_text",
  "number",
  "date",
  "select",
  "multi_select",
  "checkbox",
  "url",
  "file",
]);

export function asRecord(value: unknown): JsonRecord {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  return { ...(value as JsonRecord) };
}

export function decryptRecordRowValues(value: unknown): JsonRecord {
  return asRecord(decryptJsonValueIfNeeded(value, RECORD_ROW_VALUES_AAD));
}

export function encryptRecordRowStorage(
  values: JsonRecord,
  materialized: { title: string | null; searchText: string },
) {
  return {
    values: encryptJsonValue(values, RECORD_ROW_VALUES_AAD),
    title: encryptText(materialized.title, RECORD_ROW_TITLE_AAD),
    searchText: encryptText(materialized.searchText, RECORD_ROW_SEARCH_TEXT_AAD),
  };
}

type RecordRowLike = { values: unknown; title: string | null; searchText: string | null };

export function decryptRecordRow<T extends RecordRowLike>(
  row: T,
): Omit<T, "values" | "title" | "searchText"> & {
  values: JsonRecord;
  title: string | null;
  searchText: string | null;
} {
  return {
    ...row,
    values: decryptRecordRowValues(row.values),
    title: decryptTextIfNeeded(row.title, RECORD_ROW_TITLE_AAD),
    searchText: decryptTextIfNeeded(row.searchText, RECORD_ROW_SEARCH_TEXT_AAD),
  };
}

export function cleanString(value: unknown, fallback = ""): string {
  if (typeof value !== "string") return fallback;
  const trimmed = value.trim();
  return trimmed || fallback;
}

export function normalizeFieldType(value: unknown): string {
  const type = cleanString(value, "text");
  return FIELD_TYPES.has(type) ? type : "text";
}

export function slugKey(label: string): string {
  const base = label
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9_\-\u3040-\u30ff\u3400-\u9fff]+/g, "_")
    .replace(/^_+|_+$/g, "");
  return base || `field_${Date.now().toString(36)}`;
}

export async function uniqueFieldKey(tableId: string, label: string): Promise<string> {
  const base = slugKey(label);
  const fields = await db
    .select({ key: recordFields.key })
    .from(recordFields)
    .where(and(eq(recordFields.tableId, tableId), isNull(recordFields.deletedAt)));
  const taken = new Set(fields.map((field) => field.key));
  if (!taken.has(base)) return base;

  let index = 2;
  while (taken.has(`${base}_${index}`)) index += 1;
  return `${base}_${index}`;
}

export async function requireRecordTable(projectId: string, tableId: string) {
  const [table] = await db
    .select()
    .from(recordTables)
    .where(
      and(
        eq(recordTables.id, tableId),
        eq(recordTables.projectId, projectId),
        isNull(recordTables.deletedAt),
      ),
    )
    .limit(1);
  return table ?? null;
}

export function materializeRow(values: JsonRecord, fields: Array<typeof recordFields.$inferSelect>) {
  const titleField = fields.find((field) => field.isTitle) ?? fields[0];
  const dueField = fields.find((field) => field.isDue);
  const titleValue = titleField ? values[titleField.key] : undefined;
  const title =
    titleValue == null || titleValue === "" ? null : String(titleValue).slice(0, 500);
  const searchText = fields
    .map((field) => values[field.key])
    .filter((value) => value != null && value !== "")
    .map((value) => String(value))
    .join(" ")
    .slice(0, 8000);

  let dueAt: Date | null = null;
  if (dueField) {
    const dueValue = values[dueField.key];
    if (typeof dueValue === "string" && dueValue.trim()) {
      const parsed = new Date(`${dueValue.trim()}T00:00:00`);
      if (!Number.isNaN(parsed.getTime())) dueAt = parsed;
    }
  }

  return { title, searchText, dueAt };
}

export async function getTableFields(tableId: string) {
  return await db
    .select()
    .from(recordFields)
    .where(and(eq(recordFields.tableId, tableId), isNull(recordFields.deletedAt)))
    .orderBy(recordFields.sortOrder, recordFields.createdAt);
}

export async function countRowsByTable(projectId: string) {
  const counts = await db
    .select({
      tableId: recordRows.tableId,
      count: sql<number>`count(*)::int`,
    })
    .from(recordRows)
    .where(and(eq(recordRows.projectId, projectId), isNull(recordRows.deletedAt)))
    .groupBy(recordRows.tableId);
  return new Map(counts.map((row) => [row.tableId, Number(row.count)]));
}
