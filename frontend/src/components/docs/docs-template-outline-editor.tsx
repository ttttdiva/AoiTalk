"use client";

import {
  useEffect,
  useState,
} from "react";
import {
  ChevronLeft,
  ChevronRight,
  Plus,
  X,
} from "lucide-react";
import {
  Button,
} from "@/components/ui/button";
import {
  Input,
} from "@/components/ui/input";
import {
  createTemplateOutlineRow,
  templateJsonFromRows,
  templateRowsFromJson,
  type TemplateOutlineRow,
} from "@/lib/docs-template-outline";

// Supertag のコンテンツテンプレートをアウトライン形式で編集するエディタ。
export function TemplateOutlineEditor({
  value,
  onSave,
}: {
  value: Record<string, unknown>;
  onSave: (templateJson: Record<string, unknown>) => void;
}) {
  const [rows, setRows] = useState<TemplateOutlineRow[]>(() => {
    const parsed = templateRowsFromJson(value);
    return parsed.length > 0 ? parsed : [createTemplateOutlineRow("")];
  });

  useEffect(() => {
    const parsed = templateRowsFromJson(value);
    let cancelled = false;
    queueMicrotask(() => {
      if (!cancelled) setRows(parsed.length > 0 ? parsed : [createTemplateOutlineRow("")]);
    });
    return () => {
      cancelled = true;
    };
  }, [value]);

  const updateRow = (id: string, patch: Partial<TemplateOutlineRow>) => {
    setRows((current) => current.map((row) => row.id === id ? { ...row, ...patch } : row));
  };
  const addRowAfter = (id: string) => {
    setRows((current) => {
      const index = current.findIndex((row) => row.id === id);
      const depth = index >= 0 ? current[index]?.depth ?? 0 : 0;
      const next = [...current];
      next.splice(index + 1, 0, createTemplateOutlineRow("", depth));
      return next;
    });
  };
  const removeRow = (id: string) => {
    setRows((current) => current.length <= 1 ? [createTemplateOutlineRow("")] : current.filter((row) => row.id !== id));
  };
  const save = () => onSave(templateJsonFromRows(rows));

  return (
    <div className="space-y-2 rounded border bg-background p-2">
      <div className="space-y-1">
        {rows.map((row) => (
          <div key={row.id} className="grid grid-cols-[auto_minmax(0,1fr)_auto_auto_auto] items-center gap-1" style={{ paddingLeft: row.depth * 16 }}>
            <Button type="button" size="icon-sm" variant="ghost" onClick={() => updateRow(row.id, { depth: Math.max(0, row.depth - 1) })} aria-label="Outdent">
              <ChevronLeft className="size-3.5" />
            </Button>
            <Input
              value={row.text}
              onChange={(event) => updateRow(row.id, { text: event.target.value })}
              onBlur={save}
              className="h-8 text-xs"
              placeholder="Template row"
            />
            <Button type="button" size="icon-sm" variant="ghost" onClick={() => updateRow(row.id, { depth: Math.min(8, row.depth + 1) })} aria-label="Indent">
              <ChevronRight className="size-3.5" />
            </Button>
            <Button type="button" size="icon-sm" variant="ghost" onClick={() => addRowAfter(row.id)} aria-label="Add row">
              <Plus className="size-3.5" />
            </Button>
            <Button type="button" size="icon-sm" variant="ghost" onClick={() => removeRow(row.id)} aria-label="Remove row">
              <X className="size-3.5" />
            </Button>
          </div>
        ))}
      </div>
      <div className="flex justify-end">
        <Button type="button" size="sm" variant="secondary" onClick={save}>Save template</Button>
      </div>
    </div>
  );
}
