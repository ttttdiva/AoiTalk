"use client";

import { useState } from "react";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { LongTextEditor } from "@/components/editor/long-text-editor";
import { cn } from "@/lib/utils";
import {
  Plus,
  Trash2,
  Loader2,
  ChevronDown,
  ChevronUp,
  MessageCircle,
} from "lucide-react";
import {
  pyFetch,
  selectClassName,
  ROLES,
  IMPORTANCES,
  COC_RULESETS,
  COC_CHARACTERISTICS,
  COC_SKILL_CATEGORIES,
  normalizeCocPcState,
  intValue,
  clampPercent,
  type ScenarioCharacter,
  type CocPcState,
} from "@/lib/scenarios-page-utils";

function CharacterEditor({
  characters,
  scenarioId,
  scenarioKind,
  ruleset,
  onUpdate,
}: {
  characters: ScenarioCharacter[];
  scenarioId: string;
  scenarioKind: "writing" | "trpg";
  ruleset: string;
  onUpdate: () => void;
}) {
  const [editing, setEditing] = useState<ScenarioCharacter | null>(null);
  const [isNew, setIsNew] = useState(false);
  const [saving, setSaving] = useState(false);

  const emptyChar: Omit<ScenarioCharacter, "id" | "scenario_id"> = {
    name: "",
    role: "npc",
    description: "",
    importance: 2,
    speech_pattern: "",
    psychology: "",
    backstory: "",
    relationships: "",
    arc: "",
    dialogue_samples: "",
    trpg_ruleset: COC_RULESETS.has(ruleset) ? ruleset : "",
    trpg_pc_state: COC_RULESETS.has(ruleset)
      ? normalizeCocPcState(undefined, "", ruleset)
      : undefined,
  };

  const isCoc = scenarioKind === "trpg" && COC_RULESETS.has(ruleset);

  const updateCocState = (updater: (current: CocPcState) => CocPcState) => {
    if (!editing) return;
    const current = normalizeCocPcState(
      editing.trpg_pc_state,
      editing.name,
      ruleset,
    );
    const next = normalizeCocPcState(updater(current), editing.name, ruleset);
    setEditing({
      ...editing,
      trpg_ruleset: next.ruleset,
      trpg_pc_state: next,
    });
  };

  const handleSave = async () => {
    if (!editing || !editing.name.trim()) return;
    setSaving(true);
    try {
      const charBody = {
        name: editing.name,
        role: editing.role,
        description: editing.description,
        importance: editing.importance,
        speech_pattern: editing.speech_pattern,
        psychology: editing.psychology,
        backstory: editing.backstory,
        relationships: editing.relationships,
        arc: editing.arc,
        dialogue_samples: editing.dialogue_samples,
        trpg_ruleset: isCoc ? ruleset : "",
        trpg_pc_state: isCoc
          ? normalizeCocPcState(editing.trpg_pc_state, editing.name, ruleset)
          : {},
      };
      if (isNew) {
        await pyFetch(`/scenarios/${scenarioId}/characters`, {
          method: "POST",
          body: JSON.stringify(charBody),
        });
      } else {
        await pyFetch(`/scenarios/${scenarioId}/characters/${editing.id}`, {
          method: "PUT",
          body: JSON.stringify(charBody),
        });
      }
      setEditing(null);
      setIsNew(false);
      onUpdate();
    } catch (err) {
      console.error("キャラクター保存失敗:", err);
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (charId: string) => {
    try {
      await pyFetch(`/scenarios/${scenarioId}/characters/${charId}`, {
        method: "DELETE",
      });
      onUpdate();
    } catch (err) {
      console.error("キャラクター削除失敗:", err);
    }
  };

  const handleStartRoleplay = async (char: ScenarioCharacter) => {
    try {
      const res = await fetch("/api/conversations", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          character_name: `scenario_roleplay:${scenarioId}:${char.id}`,
        }),
      });
      if (!res.ok) throw new Error(`API Error: ${res.status}`);
      const data = (await res.json()) as { session?: { id?: string } };
      if (data.session?.id) {
        window.location.href = `/chat?s=${data.session.id}`;
      }
    } catch (err) {
      console.error("ロールプレイ開始失敗:", err);
    }
  };

  const prepareCharacterForEditing = (char: ScenarioCharacter) => ({
    ...char,
    trpg_ruleset: isCoc ? ruleset : char.trpg_ruleset,
    trpg_pc_state: isCoc
      ? normalizeCocPcState(char.trpg_pc_state, char.name, ruleset)
      : char.trpg_pc_state,
  });

  const toggleCharacter = (char: ScenarioCharacter) => {
    if (!isNew && editing?.id === char.id) {
      setEditing(null);
      return;
    }
    setEditing(prepareCharacterForEditing(char));
    setIsNew(false);
  };

  const renderEditingForm = (className?: string) => {
    if (!editing) return null;
    return (
      <div
        className={cn(
          "space-y-3 rounded-lg border bg-muted/30 p-3",
          className,
        )}
      >
        <div className="space-y-1.5">
          <Label>名前</Label>
          <Input
            value={editing.name}
            onChange={(e) => setEditing({ ...editing, name: e.target.value })}
            placeholder="キャラクター名"
          />
        </div>
        <div className="space-y-1.5">
          <Label>役割</Label>
          <select
            value={editing.role}
            onChange={(e) =>
              setEditing({
                ...editing,
                role: e.target.value as ScenarioCharacter["role"],
              })
            }
            className={selectClassName}
          >
            {ROLES.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
        </div>
        <div className="space-y-1.5">
          <Label>説明</Label>
          <LongTextEditor
            value={editing.description}
            onChange={(value) => setEditing({ ...editing, description: value })}
            placeholder="キャラクターの説明"
            minHeight={96}
            maxHeight={240}
          />
        </div>
        <div className="space-y-1.5">
          <Label>重要度</Label>
          <select
            value={editing.importance ?? 2}
            onChange={(e) =>
              setEditing({ ...editing, importance: Number(e.target.value) })
            }
            className={selectClassName}
          >
            {IMPORTANCES.map((i) => (
              <option key={i.value} value={i.value}>
                {i.label}
              </option>
            ))}
          </select>
        </div>
        <div className="space-y-1.5">
          <Label>口調パターン</Label>
          <LongTextEditor
            value={editing.speech_pattern ?? ""}
            onChange={(value) => setEditing({ ...editing, speech_pattern: value })}
            placeholder="キャラクター固有の話し方・口調"
            minHeight={96}
            maxHeight={240}
          />
        </div>
        <div className="space-y-1.5">
          <Label>心理・動機</Label>
          <LongTextEditor
            value={editing.psychology ?? ""}
            onChange={(value) => setEditing({ ...editing, psychology: value })}
            placeholder="キャラクターの心理状態・動機"
            minHeight={96}
            maxHeight={240}
          />
        </div>
        <div className="space-y-1.5">
          <Label>経歴</Label>
          <LongTextEditor
            value={editing.backstory ?? ""}
            onChange={(value) => setEditing({ ...editing, backstory: value })}
            placeholder="キャラクターの経歴・バックストーリー"
            minHeight={96}
            maxHeight={240}
          />
        </div>
        <div className="space-y-1.5">
          <Label>関係性</Label>
          <LongTextEditor
            value={editing.relationships ?? ""}
            onChange={(value) => setEditing({ ...editing, relationships: value })}
            placeholder='[{"target": "ゆかり", "type": "友人", "description": "幼なじみ"}]'
            minHeight={96}
            maxHeight={240}
            language="json"
            fontFamily="monospace"
            fontSize={12}
          />
        </div>
        <div className="space-y-1.5">
          <Label>成長軌道</Label>
          <LongTextEditor
            value={editing.arc ?? ""}
            onChange={(value) => setEditing({ ...editing, arc: value })}
            placeholder="キャラクターの成長・変化の方向性"
            minHeight={72}
            maxHeight={180}
          />
        </div>
        <div className="space-y-1.5">
          <Label>会話サンプル</Label>
          <LongTextEditor
            value={editing.dialogue_samples ?? ""}
            onChange={(value) => setEditing({ ...editing, dialogue_samples: value })}
            placeholder="キャラクターの会話例"
            minHeight={140}
            maxHeight={320}
          />
        </div>
        {isCoc && editing.trpg_pc_state && (
          <div className="space-y-4 rounded-lg border bg-background p-3">
            <div className="flex items-center justify-between gap-2">
              <div>
                <h4 className="text-sm font-medium">CoCキャラクターシート</h4>
                <p className="text-xs text-muted-foreground">
                  能力値・技能値・ココフォリア用ステータスをこのキャラクターに直接保存します。
                </p>
              </div>
              <Badge variant="outline">{ruleset}</Badge>
            </div>
            <div className="grid gap-3 md:grid-cols-4">
              {[
                ["探索者名", "name"],
                ["PL名", "player_name"],
                ["職業", "occupation"],
                ["年齢", "age"],
                ["性別", "sex"],
              ].map(([label, key]) => (
                <div key={key} className="space-y-1.5">
                  <Label>{label}</Label>
                  <Input
                    value={String(editing.trpg_pc_state?.[key as keyof CocPcState] ?? "")}
                    onChange={(e) =>
                      updateCocState((current) => ({
                        ...current,
                        [key]: e.target.value,
                      }))
                    }
                  />
                </div>
              ))}
            </div>
            <div className="grid grid-cols-2 gap-2 md:grid-cols-8">
              {COC_CHARACTERISTICS.map((key) => (
                <div key={key} className="space-y-1.5">
                  <Label>{key}</Label>
                  <Input
                    type="number"
                    min={1}
                    max={ruleset === "coc7" ? 100 : 30}
                    value={editing.trpg_pc_state?.characteristics?.[key] ?? 0}
                    onChange={(e) =>
                      updateCocState((current) => ({
                        ...current,
                        characteristics: {
                          ...(current.characteristics ?? {}),
                          [key]: intValue(e.target.value, 0),
                        },
                      }))
                    }
                  />
                </div>
              ))}
            </div>
            <div className="grid grid-cols-2 gap-2 md:grid-cols-6">
              {[
                ["HP", "hp"],
                ["最大HP", "max_hp"],
                ["MP", "mp"],
                ["最大MP", "max_mp"],
                ["SAN", "sanity"],
                ["最大SAN", "max_sanity"],
                ["幸運", "luck"],
              ].map(([label, key]) => (
                <div key={key} className="space-y-1.5">
                  <Label>{label}</Label>
                  <Input
                    type="number"
                    value={Number(editing.trpg_pc_state?.[key as keyof CocPcState] ?? 0)}
                    onChange={(e) =>
                      updateCocState((current) => ({
                        ...current,
                        [key]: intValue(e.target.value, 0),
                      }))
                    }
                  />
                </div>
              ))}
            </div>
            <div className="space-y-3">
              {Object.entries(COC_SKILL_CATEGORIES).map(([category, names]) => (
                <div key={category} className="space-y-2">
                  <h5 className="text-xs font-medium text-muted-foreground">
                    {category}
                  </h5>
                  <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
                    {names.map((name) => (
                      <div key={name} className="space-y-1.5">
                        <Label className="text-xs">{name}</Label>
                        <Input
                          type="number"
                          min={0}
                          max={100}
                          value={editing.trpg_pc_state?.skills?.[name] ?? 0}
                          onChange={(e) =>
                            updateCocState((current) => ({
                              ...current,
                              skills: {
                                ...(current.skills ?? {}),
                                [name]: clampPercent(intValue(e.target.value, 0)),
                              },
                            }))
                          }
                        />
                      </div>
                    ))}
                  </div>
                </div>
              ))}
            </div>
            <div className="grid gap-3 md:grid-cols-2">
              <div className="space-y-1.5">
                <Label>所持品</Label>
                <LongTextEditor
                  value={(editing.trpg_pc_state.items ?? []).join("\n")}
                  minHeight={100}
                  maxHeight={220}
                  onChange={(value) =>
                    updateCocState((current) => ({
                      ...current,
                      items: value.split("\n").map((item) => item.trim()).filter(Boolean),
                    }))
                  }
                />
              </div>
              <div className="space-y-1.5">
                <Label>メモ・特殊能力</Label>
                <LongTextEditor
                  value={editing.trpg_pc_state.notes ?? ""}
                  minHeight={100}
                  maxHeight={220}
                  onChange={(value) =>
                    updateCocState((current) => ({
                      ...current,
                      notes: value,
                    }))
                  }
                />
              </div>
            </div>
          </div>
        )}
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
            disabled={saving || !editing.name.trim()}
          >
            {saving && <Loader2 className="mr-1 size-3.5 animate-spin" />}
            保存
          </Button>
        </div>
      </div>
    );
  };

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <h4 className="text-sm font-medium">キャラクター一覧</h4>
        <Button
          variant="outline"
          size="sm"
          onClick={() => {
            setEditing({ id: "", scenario_id: scenarioId, ...emptyChar });
            setIsNew(true);
          }}
        >
          <Plus className="mr-1 size-3.5" />
          追加
        </Button>
      </div>

      {isNew && renderEditingForm()}

      {characters.map((char) => {
        const expanded = !isNew && editing?.id === char.id;
        return (
          <div
            key={char.id}
            className={cn(
              "overflow-hidden rounded-lg border",
              expanded && "border-primary/30 bg-muted/20",
            )}
          >
            <div className="flex items-start gap-2 p-3">
              <button
                type="button"
                className="min-w-0 flex-1 text-left outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
                aria-expanded={expanded}
                onClick={() => toggleCharacter(char)}
              >
                <div className="flex items-center gap-2">
                  <span className="font-medium text-sm">{char.name}</span>
                  <Badge variant="secondary">{char.role}</Badge>
                </div>
                {char.description && (
                  <p
                    className={cn(
                      "mt-1 text-xs text-muted-foreground",
                      !expanded && "line-clamp-2",
                    )}
                  >
                    {char.description}
                  </p>
                )}
              </button>
              <div className="flex shrink-0 items-center gap-1">
                <Button
                  variant="ghost"
                  size="icon-sm"
                  onClick={() => handleStartRoleplay(char)}
                  title="ロールプレイ開始"
                >
                  <MessageCircle className="size-3.5" />
                </Button>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  onClick={() => handleDelete(char.id)}
                  title="削除"
                >
                  <Trash2 className="size-3.5 text-destructive" />
                </Button>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  onClick={() => toggleCharacter(char)}
                  title={expanded ? "閉じる" : "詳細を開く"}
                >
                  {expanded ? (
                    <ChevronUp className="size-3.5" />
                  ) : (
                    <ChevronDown className="size-3.5" />
                  )}
                </Button>
              </div>
            </div>
            {expanded &&
              renderEditingForm(
                "rounded-none border-x-0 border-b-0 border-t bg-background/70 p-3",
              )}
          </div>
        );
      })}

      {characters.length === 0 && !editing && (
        <p className="py-4 text-center text-xs text-muted-foreground">
          キャラクターがありません
        </p>
      )}
    </div>
  );
}

export { CharacterEditor };
