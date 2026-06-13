"use client";

import { useState, useEffect, useCallback } from "react";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { LongTextEditor } from "@/components/editor/long-text-editor";
import { Plus, Pencil, Trash2, Loader2 } from "lucide-react";
import {
  pyFetch,
  selectClassName,
  CANON_CATEGORIES,
  CANON_CATEGORY_LABELS,
  type CanonEntry,
} from "@/lib/scenarios-page-utils";

function CanonEditor({ scenarioId }: { scenarioId: string }) {
  const [entries, setEntries] = useState<CanonEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [filterCategory, setFilterCategory] = useState<string>("all");
  const [editing, setEditing] = useState<CanonEntry | null>(null);
  const [isNew, setIsNew] = useState(false);
  const [saving, setSaving] = useState(false);

  const loadEntries = useCallback(async () => {
    setLoading(true);
    try {
      const query =
        filterCategory !== "all" ? `?category=${filterCategory}` : "";
      const data = await pyFetch<{ entries: CanonEntry[] }>(
        `/scenarios/${scenarioId}/canon${query}`,
      );
      setEntries(data.entries ?? []);
    } catch (err) {
      console.error("Canon取得失敗:", err);
    } finally {
      setLoading(false);
    }
  }, [scenarioId, filterCategory]);

  useEffect(() => {
    loadEntries();
  }, [loadEntries]);

  const handleSave = async () => {
    if (!editing || !editing.fact.trim()) return;
    setSaving(true);
    try {
      const body = { category: editing.category, fact: editing.fact };
      if (isNew) {
        await pyFetch(`/scenarios/${scenarioId}/canon`, {
          method: "POST",
          body: JSON.stringify(body),
        });
      } else {
        await pyFetch(`/scenarios/canon/${editing.id}`, {
          method: "PUT",
          body: JSON.stringify(body),
        });
      }
      setEditing(null);
      setIsNew(false);
      loadEntries();
    } catch (err) {
      console.error("Canon保存失敗:", err);
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (entryId: string) => {
    try {
      await pyFetch(`/scenarios/canon/${entryId}`, { method: "DELETE" });
      loadEntries();
    } catch (err) {
      console.error("Canon削除失敗:", err);
    }
  };

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <h4 className="text-sm font-medium">Canon一覧</h4>
          <select
            value={filterCategory}
            onChange={(e) => setFilterCategory(e.target.value)}
            className={selectClassName + " w-auto"}
          >
            <option value="all">すべて</option>
            {CANON_CATEGORIES.map((c) => (
              <option key={c} value={c}>
                {CANON_CATEGORY_LABELS[c]}
              </option>
            ))}
          </select>
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={() => {
            setEditing({
              id: "",
              scenario_id: scenarioId,
              category: "established",
              fact: "",
            });
            setIsNew(true);
          }}
        >
          <Plus className="mr-1 size-3.5" />
          追加
        </Button>
      </div>

      {loading ? (
        <div className="flex justify-center py-8">
          <Loader2 className="size-5 animate-spin text-muted-foreground" />
        </div>
      ) : (
        <>
          {entries.map((entry) => (
            <div
              key={entry.id}
              className="flex items-start justify-between rounded-lg border p-3"
            >
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2 mb-1">
                  <Badge variant="secondary">
                    {CANON_CATEGORY_LABELS[entry.category] ?? entry.category}
                  </Badge>
                </div>
                <p className="text-xs">{entry.fact}</p>
              </div>
              <div className="ml-2 flex gap-1">
                <Button
                  variant="ghost"
                  size="icon-sm"
                  onClick={() => {
                    setEditing({ ...entry });
                    setIsNew(false);
                  }}
                >
                  <Pencil className="size-3.5" />
                </Button>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  onClick={() => handleDelete(entry.id)}
                >
                  <Trash2 className="size-3.5 text-destructive" />
                </Button>
              </div>
            </div>
          ))}

          {entries.length === 0 && !editing && (
            <p className="py-4 text-center text-xs text-muted-foreground">
              Canonエントリがありません
            </p>
          )}
        </>
      )}

      {editing && (
        <div className="space-y-3 rounded-lg border bg-muted/30 p-3">
          <div className="space-y-1.5">
            <Label>カテゴリ</Label>
            <select
              value={editing.category}
              onChange={(e) =>
                setEditing({ ...editing, category: e.target.value })
              }
              className={selectClassName}
            >
              {CANON_CATEGORIES.map((c) => (
                <option key={c} value={c}>
                  {CANON_CATEGORY_LABELS[c]}
                </option>
              ))}
            </select>
          </div>
          <div className="space-y-1.5">
            <Label>事実</Label>
            <LongTextEditor
              value={editing.fact}
              onChange={(value) => setEditing({ ...editing, fact: value })}
              placeholder="確立された事実を記述"
              minHeight={72}
              maxHeight={180}
            />
          </div>
          <div className="flex justify-end gap-2">
            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                setEditing(null);
                setIsNew(false);
              }}
            >
              キャンセル
            </Button>
            <Button
              size="sm"
              onClick={handleSave}
              disabled={saving || !editing.fact.trim()}
            >
              {saving && <Loader2 className="mr-1 size-3.5 animate-spin" />}
              保存
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}

export { CanonEditor };
