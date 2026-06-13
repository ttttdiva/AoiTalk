"use client";

import { useState } from "react";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { LongTextEditor } from "@/components/editor/long-text-editor";
import { Plus, Pencil, Trash2, Loader2, PenLine } from "lucide-react";
import {
  pyFetch,
  selectClassName,
  SCENE_TYPES,
  STATUSES,
  STATUS_LABELS,
  type ScenarioScene,
  type ScenarioEpisode,
} from "@/lib/scenarios-page-utils";

function SceneEditor({
  scenes,
  scenarioId,
  episodes,
  onUpdate,
}: {
  scenes: ScenarioScene[];
  scenarioId: string;
  episodes: ScenarioEpisode[];
  onUpdate: () => void;
}) {
  const [editing, setEditing] = useState<ScenarioScene | null>(null);
  const [isNew, setIsNew] = useState(false);
  const [saving, setSaving] = useState(false);

  const sortedScenes = [...scenes].sort(
    (a, b) => a.order_index - b.order_index,
  );

  const emptyScene: Omit<ScenarioScene, "id" | "scenario_id"> = {
    title: "",
    description: "",
    scene_type: "intro",
    gm_instructions: "",
    image_prompt: "",
    order_index: scenes.length,
    episode_id: null,
    status: "draft",
    body: "",
  };

  const handleSave = async () => {
    if (!editing || !editing.title.trim()) return;
    setSaving(true);
    try {
      const sceneBody = {
        title: editing.title,
        description: editing.description,
        scene_type: editing.scene_type,
        gm_instructions: editing.gm_instructions,
        image_prompt: editing.image_prompt,
        order_index: editing.order_index,
        episode_id: editing.episode_id || null,
        status: editing.status,
        body: editing.body,
      };
      if (isNew) {
        await pyFetch(`/scenarios/${scenarioId}/scenes`, {
          method: "POST",
          body: JSON.stringify(sceneBody),
        });
      } else {
        await pyFetch(`/scenarios/${scenarioId}/scenes/${editing.id}`, {
          method: "PUT",
          body: JSON.stringify(sceneBody),
        });
      }
      setEditing(null);
      setIsNew(false);
      onUpdate();
    } catch (err) {
      console.error("シーン保存失敗:", err);
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (sceneId: string) => {
    try {
      await pyFetch(`/scenarios/${scenarioId}/scenes/${sceneId}`, {
        method: "DELETE",
      });
      onUpdate();
    } catch (err) {
      console.error("シーン削除失敗:", err);
    }
  };

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <h4 className="text-sm font-medium">シーン一覧</h4>
        <Button
          variant="outline"
          size="sm"
          onClick={() => {
            setEditing({ id: "", scenario_id: scenarioId, ...emptyScene });
            setIsNew(true);
          }}
        >
          <Plus className="mr-1 size-3.5" />
          追加
        </Button>
      </div>

      {sortedScenes.map((scene, idx) => (
        <div
          key={scene.id}
          className="flex items-start justify-between rounded-lg border p-3"
        >
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <span className="text-xs text-muted-foreground">#{idx + 1}</span>
              <span className="font-medium text-sm">{scene.title}</span>
              <Badge variant="secondary">{scene.scene_type}</Badge>
            </div>
            {scene.description && (
              <p className="mt-1 text-xs text-muted-foreground line-clamp-2">
                {scene.description}
              </p>
            )}
          </div>
          <div className="ml-2 flex gap-1">
            <Button
              variant="ghost"
              size="icon-sm"
              onClick={() => {
                setEditing({ ...scene });
                setIsNew(false);
              }}
            >
              <Pencil className="size-3.5" />
            </Button>
            <Button
              variant="ghost"
              size="icon-sm"
              onClick={() => handleDelete(scene.id)}
            >
              <Trash2 className="size-3.5 text-destructive" />
            </Button>
          </div>
        </div>
      ))}

      {scenes.length === 0 && !editing && (
        <p className="py-4 text-center text-xs text-muted-foreground">
          シーンがありません
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
              placeholder="シーンタイトル"
            />
          </div>
          <div className="space-y-1.5">
            <Label>タイプ</Label>
            <select
              value={editing.scene_type}
              onChange={(e) =>
                setEditing({
                  ...editing,
                  scene_type: e.target.value as ScenarioScene["scene_type"],
                })
              }
              className={selectClassName}
            >
              {SCENE_TYPES.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
          </div>
          <div className="space-y-1.5">
            <Label>説明</Label>
            <LongTextEditor
              value={editing.description}
              onChange={(value) => setEditing({ ...editing, description: value })}
              placeholder="シーンの説明"
              minHeight={96}
              maxHeight={240}
            />
          </div>
          <div className="space-y-1.5">
            <Label>GM指示</Label>
            <LongTextEditor
              value={editing.gm_instructions}
              onChange={(value) => setEditing({ ...editing, gm_instructions: value })}
              placeholder="GMへの指示・注意事項"
              minHeight={80}
              maxHeight={220}
            />
          </div>
          <div className="space-y-1.5">
            <Label>画像プロンプト</Label>
            <LongTextEditor
              value={editing.image_prompt}
              onChange={(value) => setEditing({ ...editing, image_prompt: value })}
              placeholder="シーンの画像生成プロンプト"
              minHeight={80}
              maxHeight={220}
              fontFamily="monospace"
              fontSize={12}
            />
          </div>
          <div className="grid grid-cols-2 gap-4">
            <div className="space-y-1.5">
              <Label>エピソード紐付け</Label>
              <select
                value={editing.episode_id ?? ""}
                onChange={(e) =>
                  setEditing({ ...editing, episode_id: e.target.value || null })
                }
                className={selectClassName}
              >
                <option value="">未割り当て</option>
                {episodes.map((ep) => (
                  <option key={ep.id} value={ep.id}>
                    {ep.title}
                  </option>
                ))}
              </select>
            </div>
            <div className="space-y-1.5">
              <Label>ステータス</Label>
              <select
                value={editing.status ?? "draft"}
                onChange={(e) =>
                  setEditing({
                    ...editing,
                    status: e.target.value as ScenarioScene["status"],
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
          </div>
          <div className="space-y-1.5">
            <div className="flex items-center justify-between">
              <Label>本文</Label>
              <span className="text-xs text-muted-foreground">
                {(editing.body ?? "").length}文字
              </span>
            </div>
            <LongTextEditor
              value={editing.body ?? ""}
              onChange={(value) => setEditing({ ...editing, body: value })}
              placeholder="シーンの本文"
              minHeight={280}
              maxHeight={520}
            />
          </div>
          <div className="flex items-center justify-between">
            {!isNew && editing.id && (
              <Button
                size="sm"
                variant="outline"
                onClick={async () => {
                  try {
                    const session = await pyFetch<{
                      conversation_session_id?: string;
                    }>(`/scenarios/${scenarioId}/write`, {
                      method: "POST",
                      body: JSON.stringify({
                        target_scene_id: editing.id,
                        user_id: "default",
                      }),
                    });
                    if (session?.conversation_session_id) {
                      window.location.href = `/chat?s=${session.conversation_session_id}`;
                    }
                  } catch (err) {
                    console.error("執筆セッション開始失敗:", err);
                  }
                }}
              >
                <PenLine className="mr-1 size-3.5" />
                執筆を開始
              </Button>
            )}
            <div className="flex gap-2 ml-auto">
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
        </div>
      )}
    </div>
  );
}

export { SceneEditor };
