"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Check,
  Loader2,
  Plus,
  RefreshCw,
  Table2,
  Trash2,
  X,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  FIELD_TYPES,
  asRecord,
  createRecordField,
  createRecordRow,
  deleteRecordField,
  deleteRecordRow,
  getProjectRecordTable,
  updateProjectRecordTable,
  updateRecordField,
  updateRecordRow,
  type JsonRecord,
  type RecordField,
  type RecordRow,
  type RecordTableSummary,
} from "@/lib/record-tables-api";

type Props = {
  projectId: string;
  tableId: string;
  initialName?: string;
  onClose: () => void;
  onChanged?: () => void;
};

function fieldTypeLabel(value: string) {
  return FIELD_TYPES.find((type) => type.value === value)?.label ?? value;
}

function inputTypeFor(field: RecordField): string {
  if (field.fieldType === "number") return "number";
  if (field.fieldType === "date") return "date";
  if (field.fieldType === "url") return "url";
  return "text";
}

function toInputValue(value: unknown): string {
  if (value == null) return "";
  if (typeof value === "boolean") return value ? "true" : "false";
  return String(value);
}

function coerceValue(field: RecordField, raw: string): unknown {
  if (field.fieldType === "number") return raw === "" ? null : Number(raw);
  return raw;
}

function hasDraftValue(values: JsonRecord) {
  return Object.values(values).some((value) => value !== "" && value != null);
}

function rowValues(row: RecordRow) {
  return asRecord(row.values);
}

function AddFieldDialog({
  open,
  onOpenChange,
  onCreate,
  saving,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCreate: (label: string, fieldType: string) => Promise<void>;
  saving: boolean;
}) {
  const [label, setLabel] = useState("");
  const [fieldType, setFieldType] = useState("text");

  const handleOpenChange = (nextOpen: boolean) => {
    if (!nextOpen) {
      setLabel("");
      setFieldType("text");
    }
    onOpenChange(nextOpen);
  };

  const submit = async () => {
    if (!label.trim()) return;
    await onCreate(label.trim(), fieldType);
    setLabel("");
    setFieldType("text");
    handleOpenChange(false);
  };

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className="sm:max-w-sm">
        <DialogHeader>
          <DialogTitle>列を追加</DialogTitle>
        </DialogHeader>
        <div className="space-y-3">
          <Input
            value={label}
            onChange={(event) => setLabel(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") void submit();
            }}
            placeholder="列名"
            autoFocus
          />
          <select
            value={fieldType}
            onChange={(event) => setFieldType(event.target.value)}
            className="h-9 w-full rounded-md border bg-background px-2 text-sm"
          >
            {FIELD_TYPES.map((type) => (
              <option key={type.value} value={type.value}>
                {type.label}
              </option>
            ))}
          </select>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => handleOpenChange(false)}>
            キャンセル
          </Button>
          <Button onClick={submit} disabled={!label.trim() || saving}>
            {saving ? "追加中..." : "追加"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function RecordTableEditor({
  projectId,
  tableId,
  initialName,
  onClose,
  onChanged,
}: Props) {
  const [table, setTable] = useState<RecordTableSummary | null>(null);
  const [fields, setFields] = useState<RecordField[]>([]);
  const [rows, setRows] = useState<RecordRow[]>([]);
  const [tableName, setTableName] = useState(initialName ?? "");
  const [draftRow, setDraftRow] = useState<JsonRecord>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [addFieldOpen, setAddFieldOpen] = useState(false);

  const sortedFields = useMemo(
    () =>
      [...fields].sort(
        (a, b) =>
          (a.sortOrder ?? 0) - (b.sortOrder ?? 0) ||
          a.label.localeCompare(b.label),
      ),
    [fields],
  );

  const loadTable = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const detail = await getProjectRecordTable(projectId, tableId);
      setTable(detail.table);
      setTableName(detail.table.name);
      setFields(detail.fields);
      setRows(detail.rows);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "台帳を読み込めませんでした",
      );
    } finally {
      setLoading(false);
    }
  }, [projectId, tableId]);

  useEffect(() => {
    void loadTable();
  }, [loadTable]);

  const saveTableName = useCallback(async () => {
    const nextName = tableName.trim();
    if (!table || !nextName || nextName === table.name) {
      setTableName(table?.name ?? nextName);
      return;
    }
    setSaving(true);
    setError("");
    try {
      const result = await updateProjectRecordTable(projectId, tableId, {
        name: nextName,
      });
      setTable(result.table);
      setTableName(result.table.name);
      onChanged?.();
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "表名を保存できませんでした",
      );
      setTableName(table.name);
    } finally {
      setSaving(false);
    }
  }, [onChanged, projectId, table, tableId, tableName]);

  const addField = useCallback(
    async (label: string, fieldType: string) => {
      setSaving(true);
      setError("");
      try {
        const result = await createRecordField(projectId, tableId, {
          label,
          field_type: fieldType,
        });
        setFields((current) => [...current, result.field]);
        onChanged?.();
      } catch (err) {
        setError(
          err instanceof Error ? err.message : "列を追加できませんでした",
        );
      } finally {
        setSaving(false);
      }
    },
    [onChanged, projectId, tableId],
  );

  const renameField = useCallback(
    async (field: RecordField, label: string) => {
      const nextLabel = label.trim();
      if (!nextLabel || nextLabel === field.label) return;
      setFields((current) =>
        current.map((item) =>
          item.id === field.id ? { ...item, label: nextLabel } : item,
        ),
      );
      try {
        await updateRecordField(projectId, tableId, field.id, {
          label: nextLabel,
        });
        onChanged?.();
      } catch (err) {
        setError(
          err instanceof Error ? err.message : "列名を保存できませんでした",
        );
        await loadTable();
      }
    },
    [loadTable, onChanged, projectId, tableId],
  );

  const removeField = useCallback(
    async (field: RecordField) => {
      setFields((current) => current.filter((item) => item.id !== field.id));
      try {
        await deleteRecordField(projectId, tableId, field.id);
        onChanged?.();
      } catch (err) {
        setError(
          err instanceof Error ? err.message : "列を削除できませんでした",
        );
        await loadTable();
      }
    },
    [loadTable, onChanged, projectId, tableId],
  );

  const addEmptyRow = useCallback(async () => {
    setSaving(true);
    setError("");
    try {
      const result = await createRecordRow(projectId, tableId, {});
      setRows((current) => [...current, result.row]);
      onChanged?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : "行を追加できませんでした");
    } finally {
      setSaving(false);
    }
  }, [onChanged, projectId, tableId]);

  const saveDraftRow = useCallback(async () => {
    if (!hasDraftValue(draftRow)) return;
    setSaving(true);
    setError("");
    try {
      const result = await createRecordRow(projectId, tableId, draftRow);
      setRows((current) => [...current, result.row]);
      setDraftRow({});
      onChanged?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : "行を保存できませんでした");
    } finally {
      setSaving(false);
    }
  }, [draftRow, onChanged, projectId, tableId]);

  const saveCell = useCallback(
    async (row: RecordRow, field: RecordField, value: unknown) => {
      setRows((current) =>
        current.map((item) =>
          item.id === row.id
            ? {
                ...item,
                values: { ...rowValues(item), [field.key]: value },
              }
            : item,
        ),
      );
      try {
        await updateRecordRow(projectId, tableId, row.id, {
          [field.key]: value,
        });
        onChanged?.();
      } catch (err) {
        setError(
          err instanceof Error ? err.message : "セルを保存できませんでした",
        );
        await loadTable();
      }
    },
    [loadTable, onChanged, projectId, tableId],
  );

  const removeRow = useCallback(
    async (row: RecordRow) => {
      setRows((current) => current.filter((item) => item.id !== row.id));
      try {
        await deleteRecordRow(projectId, tableId, row.id);
        onChanged?.();
      } catch (err) {
        setError(
          err instanceof Error ? err.message : "行を削除できませんでした",
        );
        await loadTable();
      }
    },
    [loadTable, onChanged, projectId, tableId],
  );

  return (
    <div className="flex h-full min-h-0 flex-col bg-background">
      <div className="flex shrink-0 items-center gap-2 border-b px-3 py-2">
        <Table2 className="size-4 text-primary" />
        <Input
          value={tableName}
          onChange={(event) => setTableName(event.target.value)}
          onBlur={saveTableName}
          onKeyDown={(event) => {
            if (event.key === "Enter") event.currentTarget.blur();
          }}
          className="h-8 max-w-sm border-0 bg-transparent px-1 text-sm font-semibold shadow-none focus-visible:ring-1"
        />
        {saving && (
          <Loader2 className="size-3.5 animate-spin text-muted-foreground" />
        )}
        <div className="ml-auto flex items-center gap-1">
          <Button
            variant="ghost"
            size="sm"
            onClick={loadTable}
            disabled={loading}
          >
            <RefreshCw
              className={`mr-1 size-3.5 ${loading ? "animate-spin" : ""}`}
            />
            更新
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() => setAddFieldOpen(true)}
          >
            <Plus className="mr-1 size-3.5" />列
          </Button>
          <Button
            size="sm"
            onClick={addEmptyRow}
            disabled={saving || fields.length === 0}
          >
            <Plus className="mr-1 size-3.5" />行
          </Button>
          <Button
            variant="ghost"
            size="icon-sm"
            onClick={onClose}
            title="閉じる"
          >
            <X className="size-4" />
          </Button>
        </div>
      </div>

      {error && (
        <div className="shrink-0 border-b bg-destructive/10 px-3 py-2 text-xs text-destructive">
          {error}
        </div>
      )}

      {loading ? (
        <div className="flex flex-1 items-center justify-center text-sm text-muted-foreground">
          <Loader2 className="mr-2 size-4 animate-spin" />
          読み込み中...
        </div>
      ) : fields.length === 0 ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-3 text-sm text-muted-foreground">
          <Table2 className="size-10 opacity-60" />
          <div>最初の列を追加してください</div>
          <Button onClick={() => setAddFieldOpen(true)}>
            <Plus className="mr-1 size-4" />
            列を追加
          </Button>
        </div>
      ) : (
        <div className="min-h-0 flex-1 overflow-auto">
          <table className="w-full min-w-[760px] border-separate border-spacing-0 text-sm">
            <thead className="sticky top-0 z-10 bg-background">
              <tr>
                <th className="w-12 border-b border-r bg-muted/40 px-2 py-2 text-center text-xs font-medium text-muted-foreground">
                  #
                </th>
                {sortedFields.map((field) => (
                  <th
                    key={field.id}
                    className="w-56 border-b border-r bg-muted/40 p-0 align-bottom"
                  >
                    <div className="group flex items-center gap-1 px-2 py-1.5">
                      <Input
                        defaultValue={field.label}
                        onBlur={(event) =>
                          renameField(field, event.target.value)
                        }
                        onKeyDown={(event) => {
                          if (event.key === "Enter") event.currentTarget.blur();
                        }}
                        className="h-7 border-0 bg-transparent px-1 text-xs font-semibold shadow-none focus-visible:ring-1"
                      />
                      <span className="shrink-0 rounded border px-1.5 py-0.5 text-[10px] font-normal text-muted-foreground">
                        {fieldTypeLabel(field.fieldType)}
                      </span>
                      <Button
                        variant="ghost"
                        size="icon-xs"
                        className="opacity-0 group-hover:opacity-100"
                        onClick={() => removeField(field)}
                        title="列を削除"
                      >
                        <Trash2 className="size-3" />
                      </Button>
                    </div>
                  </th>
                ))}
                <th className="w-10 border-b bg-muted/40 px-2 py-2" />
              </tr>
            </thead>
            <tbody>
              {rows.map((row, rowIndex) => {
                const values = rowValues(row);
                return (
                  <tr key={row.id} className="group">
                    <td className="border-b border-r bg-muted/20 px-2 py-1 text-center text-xs text-muted-foreground">
                      {rowIndex + 1}
                    </td>
                    {sortedFields.map((field) => (
                      <td key={field.id} className="border-b border-r p-0">
                        {field.fieldType === "checkbox" ? (
                          <label className="flex h-9 items-center px-3">
                            <input
                              type="checkbox"
                              checked={values[field.key] === true}
                              onChange={(event) =>
                                void saveCell(row, field, event.target.checked)
                              }
                              className="size-4"
                            />
                          </label>
                        ) : (
                          <Input
                            type={inputTypeFor(field)}
                            value={toInputValue(values[field.key])}
                            onChange={(event) => {
                              const value = coerceValue(
                                field,
                                event.target.value,
                              );
                              setRows((current) =>
                                current.map((item) =>
                                  item.id === row.id
                                    ? {
                                        ...item,
                                        values: {
                                          ...rowValues(item),
                                          [field.key]: value,
                                        },
                                      }
                                    : item,
                                ),
                              );
                            }}
                            onBlur={(event) =>
                              void saveCell(
                                row,
                                field,
                                coerceValue(field, event.target.value),
                              )
                            }
                            onKeyDown={(event) => {
                              if (event.key === "Enter")
                                event.currentTarget.blur();
                            }}
                            className="h-9 rounded-none border-0 bg-transparent px-2 shadow-none focus-visible:ring-1"
                          />
                        )}
                      </td>
                    ))}
                    <td className="border-b p-0 text-center">
                      <Button
                        variant="ghost"
                        size="icon-xs"
                        className="opacity-0 group-hover:opacity-100"
                        onClick={() => removeRow(row)}
                        title="行を削除"
                      >
                        <Trash2 className="size-3" />
                      </Button>
                    </td>
                  </tr>
                );
              })}
              <tr className="bg-muted/10">
                <td className="border-b border-r px-2 py-1 text-center text-xs text-muted-foreground">
                  +
                </td>
                {sortedFields.map((field) => (
                  <td key={field.id} className="border-b border-r p-0">
                    {field.fieldType === "checkbox" ? (
                      <label className="flex h-9 items-center px-3">
                        <input
                          type="checkbox"
                          checked={draftRow[field.key] === true}
                          onChange={(event) =>
                            setDraftRow((current) => ({
                              ...current,
                              [field.key]: event.target.checked,
                            }))
                          }
                          className="size-4"
                        />
                      </label>
                    ) : (
                      <Input
                        type={inputTypeFor(field)}
                        value={toInputValue(draftRow[field.key])}
                        onChange={(event) =>
                          setDraftRow((current) => ({
                            ...current,
                            [field.key]: coerceValue(field, event.target.value),
                          }))
                        }
                        onKeyDown={(event) => {
                          if (event.key === "Enter") void saveDraftRow();
                        }}
                        className="h-9 rounded-none border-0 bg-transparent px-2 shadow-none focus-visible:ring-1"
                        placeholder="新しい行"
                      />
                    )}
                  </td>
                ))}
                <td className="border-b p-0 text-center">
                  <Button
                    variant="ghost"
                    size="icon-xs"
                    onClick={saveDraftRow}
                    disabled={!hasDraftValue(draftRow) || saving}
                    title="行を保存"
                  >
                    <Check className="size-3" />
                  </Button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      )}

      <AddFieldDialog
        open={addFieldOpen}
        onOpenChange={setAddFieldOpen}
        onCreate={addField}
        saving={saving}
      />
    </div>
  );
}
