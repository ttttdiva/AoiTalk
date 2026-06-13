"use client";

import type { ExplorerFile } from "@/lib/explorer-api";

export type JsonRecord = Record<string, unknown>;

export type RecordTableSummary = {
  id: string;
  name: string;
  description: string | null;
  row_count?: number;
  updatedAt?: string | null;
  updated_at?: string | null;
};

export type RecordField = {
  id: string;
  tableId: string;
  key: string;
  label: string;
  fieldType: string;
  sortOrder: number | null;
  isTitle: boolean | null;
  isDue: boolean | null;
};

export type RecordRow = {
  id: string;
  values: JsonRecord | null;
  title: string | null;
  createdAt?: string | null;
  updatedAt?: string | null;
};

export type RecordTableDetail = {
  table: RecordTableSummary;
  fields: RecordField[];
  rows: RecordRow[];
};

export const RECORD_TABLE_EXTENSION = ".dbtable";
export const RECORD_TABLE_TYPE = "application/x-aoitalk-record-table";

export const FIELD_TYPES = [
  { value: "text", label: "Text" },
  { value: "long_text", label: "Long text" },
  { value: "number", label: "Number" },
  { value: "date", label: "Date" },
  { value: "select", label: "Select" },
  { value: "checkbox", label: "Checkbox" },
  { value: "url", label: "URL" },
  { value: "file", label: "File" },
];

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...init?.headers },
    ...init,
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(detail.detail || res.statusText);
  }
  return res.json();
}

export function asRecord(value: unknown): JsonRecord {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  return value as JsonRecord;
}

export function isRecordTableFile(
  file: Pick<ExplorerFile, "type" | "path">,
): boolean {
  return (
    file.type === RECORD_TABLE_TYPE ||
    file.path.startsWith("aoitalk-record-table:")
  );
}

export function recordTableToExplorerFile(
  projectId: string,
  table: RecordTableSummary,
): ExplorerFile {
  const updatedAt = table.updatedAt ?? table.updated_at ?? undefined;
  return {
    name: `${table.name}${RECORD_TABLE_EXTENSION}`,
    path: `aoitalk-record-table:${projectId}:${table.id}`,
    type: RECORD_TABLE_TYPE,
    extension: RECORD_TABLE_EXTENSION,
    modified_at: updatedAt,
    virtual_kind: "record_table",
    project_id: projectId,
    record_table_id: table.id,
    row_count: table.row_count ?? 0,
    description: table.description,
  };
}

export async function listProjectRecordTables(projectId: string) {
  return apiFetch<{ tables: RecordTableSummary[] }>(
    `/api/projects/${projectId}/records`,
  );
}

export async function createProjectRecordTable(
  projectId: string,
  name: string,
) {
  return apiFetch<{ table: RecordTableSummary }>(
    `/api/projects/${projectId}/records`,
    {
      method: "POST",
      body: JSON.stringify({ name }),
    },
  );
}

export async function getProjectRecordTable(
  projectId: string,
  tableId: string,
) {
  return apiFetch<RecordTableDetail>(
    `/api/projects/${projectId}/records/${tableId}`,
  );
}

export async function updateProjectRecordTable(
  projectId: string,
  tableId: string,
  values: { name?: string; description?: string | null },
) {
  return apiFetch<{ table: RecordTableSummary }>(
    `/api/projects/${projectId}/records/${tableId}`,
    {
      method: "PATCH",
      body: JSON.stringify(values),
    },
  );
}

export async function deleteProjectRecordTable(
  projectId: string,
  tableId: string,
) {
  return apiFetch<{ success: boolean }>(
    `/api/projects/${projectId}/records/${tableId}`,
    { method: "DELETE" },
  );
}

export async function createRecordField(
  projectId: string,
  tableId: string,
  values: { label: string; field_type?: string },
) {
  return apiFetch<{ field: RecordField }>(
    `/api/projects/${projectId}/records/${tableId}/fields`,
    {
      method: "POST",
      body: JSON.stringify(values),
    },
  );
}

export async function updateRecordField(
  projectId: string,
  tableId: string,
  fieldId: string,
  values: { label?: string; field_type?: string },
) {
  return apiFetch<{ field: RecordField }>(
    `/api/projects/${projectId}/records/${tableId}/fields/${fieldId}`,
    {
      method: "PATCH",
      body: JSON.stringify(values),
    },
  );
}

export async function deleteRecordField(
  projectId: string,
  tableId: string,
  fieldId: string,
) {
  return apiFetch<{ success: boolean }>(
    `/api/projects/${projectId}/records/${tableId}/fields/${fieldId}`,
    { method: "DELETE" },
  );
}

export async function createRecordRow(
  projectId: string,
  tableId: string,
  values: JsonRecord,
) {
  return apiFetch<{ row: RecordRow }>(
    `/api/projects/${projectId}/records/${tableId}/rows`,
    {
      method: "POST",
      body: JSON.stringify({ values }),
    },
  );
}

export async function updateRecordRow(
  projectId: string,
  tableId: string,
  rowId: string,
  values: JsonRecord,
) {
  return apiFetch<{ row: RecordRow }>(
    `/api/projects/${projectId}/records/${tableId}/rows/${rowId}`,
    {
      method: "PATCH",
      body: JSON.stringify({ values }),
    },
  );
}

export async function deleteRecordRow(
  projectId: string,
  tableId: string,
  rowId: string,
) {
  return apiFetch<{ success: boolean }>(
    `/api/projects/${projectId}/records/${tableId}/rows/${rowId}`,
    { method: "DELETE" },
  );
}
