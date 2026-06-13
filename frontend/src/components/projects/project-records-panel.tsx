"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Database,
  Loader2,
  Plus,
  RefreshCw,
  Table2,
  Trash2,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";

type JsonRecord = Record<string, unknown>;

type RecordTableSummary = {
  id: string;
  name: string;
  description: string | null;
  row_count: number;
};

type RecordField = {
  id: string;
  tableId: string;
  key: string;
  label: string;
  fieldType: string;
  sortOrder: number | null;
  isTitle: boolean | null;
  isDue: boolean | null;
};

type RecordRow = {
  id: string;
  values: JsonRecord | null;
  title: string | null;
};

const FIELD_TYPES = [
  { value: "text", label: "Text" },
  { value: "long_text", label: "Long" },
  { value: "number", label: "Number" },
  { value: "date", label: "Date" },
  { value: "select", label: "Select" },
  { value: "checkbox", label: "Check" },
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

function asRecord(value: unknown): JsonRecord {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  return value as JsonRecord;
}

function inputTypeFor(field: RecordField): string {
  if (field.fieldType === "number") return "number";
  if (field.fieldType === "date") return "date";
  if (field.fieldType === "url") return "url";
  return "text";
}

export function ProjectRecordsPanel({ projectId }: { projectId: string }) {
  const [tables, setTables] = useState<RecordTableSummary[]>([]);
  const [selectedTableId, setSelectedTableId] = useState<string | null>(null);
  const [fields, setFields] = useState<RecordField[]>([]);
  const [rows, setRows] = useState<RecordRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [newTableName, setNewTableName] = useState("");
  const [newFieldLabel, setNewFieldLabel] = useState("");
  const [newFieldType, setNewFieldType] = useState("text");

  const selectedTable = useMemo(
    () => tables.find((table) => table.id === selectedTableId) ?? null,
    [selectedTableId, tables],
  );

  const loadTables = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const data = await apiFetch<{ tables: RecordTableSummary[] }>(
        `/api/projects/${projectId}/records`,
      );
      setTables(data.tables);
      setSelectedTableId((current) => {
        if (current && data.tables.some((table) => table.id === current)) {
          return current;
        }
        return data.tables[0]?.id ?? null;
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "台帳一覧の取得に失敗しました");
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  const loadTableDetail = useCallback(async () => {
    if (!selectedTableId) {
      setFields([]);
      setRows([]);
      return;
    }
    setLoading(true);
    setError("");
    try {
      const data = await apiFetch<{
        fields: RecordField[];
        rows: RecordRow[];
      }>(`/api/projects/${projectId}/records/${selectedTableId}`);
      setFields(data.fields);
      setRows(data.rows);
    } catch (err) {
      setError(err instanceof Error ? err.message : "台帳の取得に失敗しました");
    } finally {
      setLoading(false);
    }
  }, [projectId, selectedTableId]);

  useEffect(() => {
    void loadTables();
  }, [loadTables]);

  useEffect(() => {
    void loadTableDetail();
  }, [loadTableDetail]);

  const createTable = useCallback(async () => {
    const name = newTableName.trim();
    if (!name) return;
    setSaving(true);
    setError("");
    try {
      const data = await apiFetch<{ table: RecordTableSummary }>(
        `/api/projects/${projectId}/records`,
        {
          method: "POST",
          body: JSON.stringify({ name }),
        },
      );
      setNewTableName("");
      await loadTables();
      setSelectedTableId(data.table.id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "表の作成に失敗しました");
    } finally {
      setSaving(false);
    }
  }, [loadTables, newTableName, projectId]);

  const addField = useCallback(async () => {
    if (!selectedTableId || !newFieldLabel.trim()) return;
    setSaving(true);
    setError("");
    try {
      await apiFetch(`/api/projects/${projectId}/records/${selectedTableId}/fields`, {
        method: "POST",
        body: JSON.stringify({
          label: newFieldLabel.trim(),
          field_type: newFieldType,
        }),
      });
      setNewFieldLabel("");
      setNewFieldType("text");
      await loadTableDetail();
    } catch (err) {
      setError(err instanceof Error ? err.message : "列の追加に失敗しました");
    } finally {
      setSaving(false);
    }
  }, [loadTableDetail, newFieldLabel, newFieldType, projectId, selectedTableId]);

  const addRow = useCallback(async () => {
    if (!selectedTableId) return;
    setSaving(true);
    setError("");
    try {
      await apiFetch(`/api/projects/${projectId}/records/${selectedTableId}/rows`, {
        method: "POST",
        body: JSON.stringify({ values: {} }),
      });
      await loadTableDetail();
      await loadTables();
    } catch (err) {
      setError(err instanceof Error ? err.message : "行の追加に失敗しました");
    } finally {
      setSaving(false);
    }
  }, [loadTableDetail, loadTables, projectId, selectedTableId]);

  const updateCell = useCallback(
    async (row: RecordRow, field: RecordField, value: unknown) => {
      if (!selectedTableId) return;
      const nextValues = { ...asRecord(row.values), [field.key]: value };
      setRows((current) =>
        current.map((item) =>
          item.id === row.id ? { ...item, values: nextValues } : item,
        ),
      );
      try {
        await apiFetch(
          `/api/projects/${projectId}/records/${selectedTableId}/rows/${row.id}`,
          {
            method: "PATCH",
            body: JSON.stringify({ values: { [field.key]: value } }),
          },
        );
      } catch (err) {
        setError(err instanceof Error ? err.message : "セルの保存に失敗しました");
        await loadTableDetail();
      }
    },
    [loadTableDetail, projectId, selectedTableId],
  );

  const renameField = useCallback(
    async (field: RecordField, label: string) => {
      if (!selectedTableId || !label.trim() || label.trim() === field.label) return;
      try {
        await apiFetch(
          `/api/projects/${projectId}/records/${selectedTableId}/fields/${field.id}`,
          {
            method: "PATCH",
            body: JSON.stringify({ label: label.trim() }),
          },
        );
        await loadTableDetail();
      } catch (err) {
        setError(err instanceof Error ? err.message : "列名の保存に失敗しました");
      }
    },
    [loadTableDetail, projectId, selectedTableId],
  );

  const deleteRow = useCallback(
    async (rowId: string) => {
      if (!selectedTableId) return;
      setRows((current) => current.filter((row) => row.id !== rowId));
      try {
        await apiFetch(
          `/api/projects/${projectId}/records/${selectedTableId}/rows/${rowId}`,
          { method: "DELETE" },
        );
        await loadTables();
      } catch (err) {
        setError(err instanceof Error ? err.message : "行の削除に失敗しました");
        await loadTableDetail();
      }
    },
    [loadTableDetail, loadTables, projectId, selectedTableId],
  );

  const deleteField = useCallback(
    async (fieldId: string) => {
      if (!selectedTableId) return;
      try {
        await apiFetch(
          `/api/projects/${projectId}/records/${selectedTableId}/fields/${fieldId}`,
          { method: "DELETE" },
        );
        await loadTableDetail();
      } catch (err) {
        setError(err instanceof Error ? err.message : "列の削除に失敗しました");
      }
    },
    [loadTableDetail, projectId, selectedTableId],
  );

  return (
    <div className="flex h-full min-h-[520px] overflow-hidden">
      <aside className="w-56 shrink-0 border-r bg-muted/20 p-3">
        <div className="mb-3 flex items-center justify-between">
          <div className="flex items-center gap-2 text-sm font-medium">
            <Database className="size-4" />
            台帳
          </div>
          <Button variant="ghost" size="icon" onClick={loadTables} disabled={loading}>
            <RefreshCw className={`size-4 ${loading ? "animate-spin" : ""}`} />
          </Button>
        </div>
        <div className="space-y-1">
          {tables.map((table) => (
            <button
              key={table.id}
              type="button"
              onClick={() => setSelectedTableId(table.id)}
              className={`flex w-full items-center justify-between gap-2 rounded-md px-2 py-2 text-left text-sm ${
                table.id === selectedTableId
                  ? "bg-accent text-accent-foreground"
                  : "hover:bg-accent/60"
              }`}
            >
              <span className="truncate">{table.name}</span>
              <Badge variant="secondary" className="shrink-0 text-[10px]">
                {table.row_count}
              </Badge>
            </button>
          ))}
          {tables.length === 0 && (
            <p className="px-1 py-4 text-xs text-muted-foreground">
              まだ表がありません。
            </p>
          )}
        </div>
        <div className="mt-4 space-y-2">
          <Label className="text-xs">新しい表</Label>
          <div className="flex gap-1">
            <Input
              value={newTableName}
              onChange={(event) => setNewTableName(event.target.value)}
              placeholder="申請台帳"
              onKeyDown={(event) => {
                if (event.key === "Enter") void createTable();
              }}
            />
            <Button size="icon" onClick={createTable} disabled={saving}>
              <Plus className="size-4" />
            </Button>
          </div>
        </div>
      </aside>

      <section className="flex min-w-0 flex-1 flex-col">
        <div className="flex shrink-0 items-center justify-between border-b px-4 py-3">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <Table2 className="size-4 text-muted-foreground" />
              <h3 className="truncate text-sm font-semibold">
                {selectedTable?.name ?? "表を選択"}
              </h3>
            </div>
            <p className="mt-1 text-xs text-muted-foreground">
              表・列・行をプロジェクト単位で管理します。
            </p>
          </div>
          <Button size="sm" onClick={addRow} disabled={!selectedTableId || saving}>
            <Plus className="mr-1 size-3" />
            行
          </Button>
        </div>

        {error && (
          <div className="border-b bg-destructive/10 px-4 py-2 text-xs text-destructive">
            {error}
          </div>
        )}

        {selectedTableId ? (
          <>
            <div className="flex shrink-0 items-end gap-2 border-b px-4 py-3">
              <div className="w-56 space-y-1">
                <Label className="text-xs">列名</Label>
                <Input
                  value={newFieldLabel}
                  onChange={(event) => setNewFieldLabel(event.target.value)}
                  placeholder="期限"
                />
              </div>
              <div className="w-36 space-y-1">
                <Label className="text-xs">型</Label>
                <select
                  value={newFieldType}
                  onChange={(event) => setNewFieldType(event.target.value)}
                  className="h-9 w-full rounded-md border bg-background px-2 text-sm"
                >
                  {FIELD_TYPES.map((type) => (
                    <option key={type.value} value={type.value}>
                      {type.label}
                    </option>
                  ))}
                </select>
              </div>
              <Button size="sm" onClick={addField} disabled={saving}>
                {saving ? <Loader2 className="mr-1 size-3 animate-spin" /> : <Plus className="mr-1 size-3" />}
                列
              </Button>
            </div>

            <div className="min-h-0 flex-1 overflow-auto">
              <table className="w-full min-w-[720px] border-separate border-spacing-0 text-sm">
                <thead className="sticky top-0 z-10 bg-background">
                  <tr>
                    {fields.map((field) => (
                      <th
                        key={field.id}
                        className="w-48 border-b border-r p-0 align-bottom"
                      >
                        <div className="flex items-center gap-1 p-2">
                          <Input
                            defaultValue={field.label}
                            className="h-8 border-0 bg-transparent px-1 font-medium shadow-none"
                            onBlur={(event) => renameField(field, event.target.value)}
                          />
                          <Button
                            variant="ghost"
                            size="icon"
                            className="size-7 shrink-0"
                            onClick={() => deleteField(field.id)}
                            title="列を削除"
                          >
                            <Trash2 className="size-3" />
                          </Button>
                        </div>
                      </th>
                    ))}
                    <th className="w-10 border-b p-2" />
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row) => {
                    const values = asRecord(row.values);
                    return (
                      <tr key={row.id} className="group">
                        {fields.map((field) => (
                          <td key={field.id} className="border-b border-r p-1">
                            {field.fieldType === "checkbox" ? (
                              <input
                                type="checkbox"
                                checked={values[field.key] === true}
                                onChange={(event) =>
                                  void updateCell(row, field, event.target.checked)
                                }
                                className="ml-2 size-4"
                              />
                            ) : (
                              <Input
                                type={inputTypeFor(field)}
                                value={
                                  values[field.key] == null
                                    ? ""
                                    : String(values[field.key])
                                }
                                onChange={(event) => {
                                  const value =
                                    field.fieldType === "number"
                                      ? event.target.value === ""
                                        ? null
                                        : Number(event.target.value)
                                      : event.target.value;
                                  setRows((current) =>
                                    current.map((item) =>
                                      item.id === row.id
                                        ? {
                                            ...item,
                                            values: {
                                              ...asRecord(item.values),
                                              [field.key]: value,
                                            },
                                          }
                                        : item,
                                    ),
                                  );
                                }}
                                onBlur={(event) => {
                                  const value =
                                    field.fieldType === "number"
                                      ? event.target.value === ""
                                        ? null
                                        : Number(event.target.value)
                                      : event.target.value;
                                  void updateCell(row, field, value);
                                }}
                                className="h-8 border-0 bg-transparent px-2 shadow-none focus-visible:ring-1"
                              />
                            )}
                          </td>
                        ))}
                        <td className="border-b p-1">
                          <Button
                            variant="ghost"
                            size="icon"
                            className="size-7 opacity-0 group-hover:opacity-100"
                            onClick={() => deleteRow(row.id)}
                            title="行を削除"
                          >
                            <Trash2 className="size-3" />
                          </Button>
                        </td>
                      </tr>
                    );
                  })}
                  {rows.length === 0 && (
                    <tr>
                      <td
                        colSpan={Math.max(fields.length + 1, 1)}
                        className="p-8 text-center text-sm text-muted-foreground"
                      >
                        行がありません。
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </>
        ) : (
          <div className="flex flex-1 items-center justify-center text-sm text-muted-foreground">
            左側から表を作成してください。
          </div>
        )}
      </section>
    </div>
  );
}
