"use client";

import { useState } from "react";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { LongTextEditor } from "@/components/editor/long-text-editor";
import {
  Plus,
  Pencil,
  Trash2,
  Loader2,
  ArrowUp,
  ArrowDown,
} from "lucide-react";
import {
  pyFetch,
  selectClassName,
  STATUSES,
  STATUS_LABELS,
  STATUS_COLORS,
  type ScenarioEpisode,
} from "@/lib/scenarios-page-utils";

function EpisodeEditor({
  episodes,
  scenarioId,
  onUpdate,
}: {
  episodes: ScenarioEpisode[];
  scenarioId: string;
  onUpdate: () => void;
}) {
  const [editing, setEditing] = useState<ScenarioEpisode | null>(null);
  const [isNew, setIsNew] = useState(false);
  const [saving, setSaving] = useState(false);

  const sortedEpisodes = [...episodes].sort(
    (a, b) => a.sort_order - b.sort_order,
  );

  const emptyEpisode: Omit<ScenarioEpisode, "id" | "scenario_id"> = {
    title: "",
    one_line_summary: "",
    paragraph_summary: "",
    full_summary: "",
    status: "draft",
    beat_sheet: "",
    sort_order: episodes.length,
  };

  const handleSave = async () => {
    if (!editing || !editing.title.trim()) return;
    setSaving(true);
    try {
      const body = {
        title: editing.title,
        one_line_summary: editing.one_line_summary,
        paragraph_summary: editing.paragraph_summary,
        full_summary: editing.full_summary,
        status: editing.status,
        beat_sheet: editing.beat_sheet,
        sort_order: editing.sort_order,
      };
      if (isNew) {
        await pyFetch(`/scenarios/${scenarioId}/episodes`, {
          method: "POST",
          body: JSON.stringify(body),
        });
      } else {
        await pyFetch(`/scenarios/episodes/${editing.id}`, {
          method: "PUT",
          body: JSON.stringify(body),
        });
      }
      setEditing(null);
      setIsNew(false);
      onUpdate();
    } catch (err) {
      console.error("エピソード保存失敗:", err);
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (epId: string) => {
    try {
      await pyFetch(`/scenarios/episodes/${epId}`, { method: "DELETE" });
      onUpdate();
    } catch (err) {
      console.error("エピソード削除失敗:", err);
    }
  };

  const handleReorder = async (idx: number, direction: "up" | "down") => {
    const swapIdx = direction === "up" ? idx - 1 : idx + 1;
    if (swapIdx < 0 || swapIdx >= sortedEpisodes.length) return;
    const newOrder = sortedEpisodes.map((ep) => ep.id);
    [newOrder[idx], newOrder[swapIdx]] = [newOrder[swapIdx], newOrder[idx]];
    try {
      await pyFetch(`/scenarios/${scenarioId}/episodes/reorder`, {
        method: "PUT",
        body: JSON.stringify({ episode_ids: newOrder }),
      });
      onUpdate();
    } catch (err) {
      console.error("並べ替え失敗:", err);
    }
  };

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <h4 className="text-sm font-medium">エピソード一覧</h4>
        <Button
          variant="outline"
          size="sm"
          onClick={() => {
            setEditing({ id: "", scenario_id: scenarioId, ...emptyEpisode });
            setIsNew(true);
          }}
        >
          <Plus className="mr-1 size-3.5" />
          追加
        </Button>
      </div>

      {sortedEpisodes.map((ep, idx) => (
        <div
          key={ep.id}
          className="flex items-start justify-between rounded-lg border p-3"
        >
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <span className="text-xs text-muted-foreground">#{idx + 1}</span>
              <span className="font-medium text-sm">{ep.title}</span>
              <Badge
                variant={
                  STATUS_COLORS[ep.status] as
                    | "secondary"
                    | "default"
                    | "outline"
                }
              >
                {STATUS_LABELS[ep.status]}
              </Badge>
            </div>
            {ep.one_line_summary && (
              <p className="mt-1 text-xs text-muted-foreground line-clamp-1">
                {ep.one_line_summary}
              </p>
            )}
          </div>
          <div className="ml-2 flex gap-1">
            <Button
              variant="ghost"
              size="icon-sm"
              onClick={() => handleReorder(idx, "up")}
              disabled={idx === 0}
            >
              <ArrowUp className="size-3.5" />
            </Button>
            <Button
              variant="ghost"
              size="icon-sm"
              onClick={() => handleReorder(idx, "down")}
              disabled={idx === sortedEpisodes.length - 1}
            >
              <ArrowDown className="size-3.5" />
            </Button>
            <Button
              variant="ghost"
              size="icon-sm"
              onClick={() => {
                setEditing({ ...ep });
                setIsNew(false);
              }}
            >
              <Pencil className="size-3.5" />
            </Button>
            <Button
              variant="ghost"
              size="icon-sm"
              onClick={() => handleDelete(ep.id)}
            >
              <Trash2 className="size-3.5 text-destructive" />
            </Button>
          </div>
        </div>
      ))}

      {episodes.length === 0 && !editing && (
        <p className="py-4 text-center text-xs text-muted-foreground">
          エピソードがありません
        </p>
      )}

      {editing && (
        <div className="space-y-3 rounded-lg border bg-muted/30 p-3">
          <div className="space-y-1.5">
            <Label>タイトル</Label>
            <Input
              value={editing.title}
              onChange={(e) =>
                setEditing({ ...editing, title: e.target.value })
              }
              placeholder="エピソードタイトル"
            />
          </div>
          <div className="space-y-1.5">
            <Label>1文要約</Label>
            <Input
              value={editing.one_line_summary}
              onChange={(e) =>
                setEditing({ ...editing, one_line_summary: e.target.value })
              }
              placeholder="このエピソードを一文で"
            />
          </div>
          <div className="space-y-1.5">
            <Label>段落要約</Label>
            <LongTextEditor
              value={editing.paragraph_summary}
              onChange={(value) => setEditing({ ...editing, paragraph_summary: value })}
              placeholder="数行で要約"
              minHeight={96}
              maxHeight={220}
            />
          </div>
          <div className="space-y-1.5">
            <Label>完全要約</Label>
            <LongTextEditor
              value={editing.full_summary}
              onChange={(value) => setEditing({ ...editing, full_summary: value })}
              placeholder="詳細な要約"
              minHeight={140}
              maxHeight={320}
            />
          </div>
          <div className="space-y-1.5">
            <Label>ステータス</Label>
            <select
              value={editing.status}
              onChange={(e) =>
                setEditing({
                  ...editing,
                  status: e.target.value as ScenarioEpisode["status"],
                })
              }
              className={selectClassName}
            >
              {STATUSES.map((s) => (
                <option key={s} value={s}>
                  {STATUS_LABELS[s]}
                </option>
              ))}
            </select>
          </div>
          <div className="space-y-1.5">
            <Label>ビートシート</Label>
            <LongTextEditor
              value={editing.beat_sheet}
              onChange={(value) => setEditing({ ...editing, beat_sheet: value })}
              placeholder='[{"beat": "主人公が洞窟に入る", "notes": "緊張感を高める"}]'
              minHeight={200}
              maxHeight={420}
              language="json"
              fontFamily="monospace"
              fontSize={12}
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
              disabled={saving || !editing.title.trim()}
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

export { EpisodeEditor };
