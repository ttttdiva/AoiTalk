"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  COC_CHARACTERISTICS,
  COC_DEFAULT_CHARACTERISTICS,
  COC_DEFAULT_SKILLS,
  COC_KEY_SKILLS,
  DEFAULT_GENERIC_PC_DRAFT,
  applyCocPcState,
  applyGenericPcState,
  buildCocPcState,
  buildGenericPcState,
  clampPercent,
  deriveCoc6,
  intValue,
  isCocScenario,
  parseCocPaste,
  py,
  type CocPersonal,
  type GenericPcDraft,
  type Participant,
  type Room,
  type ScenarioCharacter,
} from "@/lib/trpg-room-utils";

// 入室（キャラクター作成）ダイアログ
export function JoinRoomDialog({
  open,
  onOpenChange,
  room,
  inviteCode,
  onJoined,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  room: Room;
  inviteCode: string;
  onJoined: (participantId: string) => Promise<void> | void;
}) {
  const [joinName, setJoinName] = useState("");
  const [joinAvatarUrl, setJoinAvatarUrl] = useState("");
  const [joining, setJoining] = useState(false);
  const [cocPersonal, setCocPersonal] = useState<CocPersonal>({
    name: "",
    occupation: "",
    age: "",
    sex: "",
  });
  const [cocCharacteristics, setCocCharacteristics] =
    useState<Record<string, number>>(COC_DEFAULT_CHARACTERISTICS);
  const [cocSkills, setCocSkills] = useState<Record<string, number>>(COC_DEFAULT_SKILLS);
  const [cocPaste, setCocPaste] = useState("");
  const [genericPcDraft, setGenericPcDraft] = useState<GenericPcDraft>(
    DEFAULT_GENERIC_PC_DRAFT,
  );
  const [playerSheets, setPlayerSheets] = useState<ScenarioCharacter[]>([]);
  const [selectedPlayerSheetId, setSelectedPlayerSheetId] = useState("");

  const cocDerived = useMemo(
    () => deriveCoc6(cocCharacteristics),
    [cocCharacteristics],
  );

  const applyPlayerSheet = useCallback((sheet: ScenarioCharacter) => {
    const applied = applyCocPcState(sheet.trpg_pc_state, sheet.name);
    const avatarUrl =
      typeof sheet.sheet_metadata?.avatar_url === "string"
        ? sheet.sheet_metadata.avatar_url
        : "";
    setSelectedPlayerSheetId(sheet.id);
    setJoinName(applied.personal.name);
    setJoinAvatarUrl(avatarUrl);
    setCocPersonal(applied.personal);
    setCocCharacteristics(applied.characteristics);
    setCocSkills(applied.skills);
  }, []);

  const applyGenericPlayerSheet = useCallback((sheet: ScenarioCharacter) => {
    const avatarUrl =
      typeof sheet.sheet_metadata?.avatar_url === "string"
        ? sheet.sheet_metadata.avatar_url
        : "";
    setJoinName(sheet.name);
    setJoinAvatarUrl(avatarUrl);
    setGenericPcDraft(applyGenericPcState(sheet.trpg_pc_state));
  }, []);

  const loadPlayerSheets = useCallback(async () => {
    if (!room) {
      setPlayerSheets([]);
      return;
    }
    try {
      const data = await py<{ sheets: ScenarioCharacter[] }>(
        `/api/trpg/rooms/${room.id}/player-sheets`,
      );
      setPlayerSheets(data.sheets ?? []);
    } catch {
      setPlayerSheets([]);
    }
  }, [room]);

  useEffect(() => {
    if (open) {
      void loadPlayerSheets();
    }
  }, [loadPlayerSheets, open]);

  // ── 入室 ──
  const handleJoin = useCallback(async () => {
    if (!room || !joinName.trim()) return;
    setJoining(true);
    try {
      const cocRoom = isCocScenario(room);
      const pcName = (cocPersonal.name || joinName).trim();
      const pcState = cocRoom
        ? buildCocPcState(
            { ...cocPersonal, name: pcName },
            cocCharacteristics,
            cocSkills,
          )
        : buildGenericPcState(
            pcName,
            genericPcDraft,
            room.scenario?.ruleset || room.scenario?.genre,
          );
      const p = await py<Participant>(
        `/api/trpg/rooms/${room.id}/join`,
        {
          method: "POST",
          body: JSON.stringify({
            display_name: pcName,
            role: "player",
            scenario_character_id: selectedPlayerSheetId || undefined,
            pc_state: pcState,
            avatar_url: joinAvatarUrl.trim(),
            save_character_sheet: true,
            invite_code: inviteCode || undefined,
          }),
        }
      );
      await onJoined(p.id);
    } catch (e) {
      console.error(e);
      alert("入室に失敗しました");
    } finally {
      setJoining(false);
    }
  }, [room, joinName, joinAvatarUrl, cocPersonal, cocCharacteristics, cocSkills, genericPcDraft, selectedPlayerSheetId, inviteCode, onJoined]);

  const handleApplyCocPaste = useCallback(() => {
    const parsed = parseCocPaste(cocPaste);
    if (parsed.personal.name) {
      setJoinName(parsed.personal.name);
    }
    setCocPersonal((prev) => ({ ...prev, ...parsed.personal }));
    setCocCharacteristics((prev) => ({ ...prev, ...parsed.characteristics }));
    setCocSkills((prev) => ({ ...prev, ...parsed.skills }));
  }, [cocPaste]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle>キャラクター作成して入室</DialogTitle>
          </DialogHeader>
          <div className="space-y-3">
            <div>
              <Label>キャラクターシート</Label>
              <select
                value={selectedPlayerSheetId}
                onChange={(e) => {
                  const sheetId = e.target.value;
                  setSelectedPlayerSheetId(sheetId);
                  if (!sheetId) {
                    setJoinAvatarUrl("");
                    return;
                  }
                  const sheet = playerSheets.find((item) => item.id === sheetId);
                  if (!sheet) return;
                  if (isCocScenario(room)) {
                    applyPlayerSheet(sheet);
                  } else {
                    applyGenericPlayerSheet(sheet);
                  }
                }}
                className="mt-1 w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
              >
                <option value="">新規作成</option>
                {playerSheets.map((sheet) => (
                  <option key={sheet.id} value={sheet.id}>
                    {sheet.name}
                  </option>
                ))}
              </select>
              <p className="mt-1 text-xs text-muted-foreground">
                {playerSheets.length > 0
                  ? "既存シートを選ぶか、新規作成して入室時に保存します。"
                  : "まだ保存済みシートがありません。新規作成して入室時に保存します。"}
              </p>
            </div>
            <div>
              <Label>表示名 / PC名</Label>
              <Input
                value={joinName}
                onChange={(e) => {
                  setJoinName(e.target.value);
                  setCocPersonal((prev) => ({ ...prev, name: e.target.value }));
                }}
                placeholder="例: アルカナ"
              />
            </div>
            <div>
              <Label>アイコンURL</Label>
              <Input
                type="url"
                value={joinAvatarUrl}
                onChange={(e) => setJoinAvatarUrl(e.target.value)}
                placeholder="https://example.com/avatar.png"
              />
            </div>
            {isCocScenario(room) && (
              <div className="space-y-3 rounded border p-3">
                <div className="grid grid-cols-3 gap-2">
                  <div>
                    <Label>職業</Label>
                    <Input
                      value={cocPersonal.occupation}
                      onChange={(e) =>
                        setCocPersonal((prev) => ({
                          ...prev,
                          occupation: e.target.value,
                        }))
                      }
                    />
                  </div>
                  <div>
                    <Label>年齢</Label>
                    <Input
                      value={cocPersonal.age}
                      onChange={(e) =>
                        setCocPersonal((prev) => ({ ...prev, age: e.target.value }))
                      }
                    />
                  </div>
                  <div>
                    <Label>性別</Label>
                    <Input
                      value={cocPersonal.sex}
                      onChange={(e) =>
                        setCocPersonal((prev) => ({ ...prev, sex: e.target.value }))
                      }
                    />
                  </div>
                </div>
                <div className="grid grid-cols-4 gap-2">
                  {COC_CHARACTERISTICS.map((key) => (
                    <div key={key}>
                      <Label>{key}</Label>
                      <Input
                        type="number"
                        min={1}
                        max={30}
                        value={cocCharacteristics[key]}
                        onChange={(e) =>
                          setCocCharacteristics((prev) => ({
                            ...prev,
                            [key]: intValue(e.target.value, prev[key]),
                          }))
                        }
                      />
                    </div>
                  ))}
                </div>
                <div className="grid grid-cols-4 gap-2 text-xs">
                  <div>HP {cocDerived.hp}</div>
                  <div>MP {cocDerived.mp}</div>
                  <div>SAN {cocDerived.sanity}</div>
                  <div>幸運 {cocDerived.luck}</div>
                </div>
                <div className="grid grid-cols-4 gap-2">
                  {COC_KEY_SKILLS.map((skill) => (
                    <div key={skill}>
                      <Label>{skill}</Label>
                      <Input
                        type="number"
                        min={0}
                        max={100}
                        value={cocSkills[skill] ?? 0}
                        onChange={(e) =>
                          setCocSkills((prev) => ({
                            ...prev,
                            [skill]: clampPercent(intValue(e.target.value, prev[skill] ?? 0)),
                          }))
                        }
                      />
                    </div>
                  ))}
                </div>
                <div>
                  <Label>キャラクター保管所テキスト貼り付け</Label>
                  <Textarea
                    value={cocPaste}
                    onChange={(e) => setCocPaste(e.target.value)}
                    rows={4}
                    className="mt-1 resize-none text-xs"
                  />
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    className="mt-2"
                    onClick={handleApplyCocPaste}
                    disabled={!cocPaste.trim()}
                  >
                    貼り付け内容を反映
                  </Button>
                </div>
              </div>
            )}
            {!isCocScenario(room) && (
              <div className="space-y-3 rounded border p-3">
                <div>
                  <Label>キャラクター概要</Label>
                  <Textarea
                    value={genericPcDraft.description}
                    onChange={(e) =>
                      setGenericPcDraft((prev) => ({
                        ...prev,
                        description: e.target.value,
                      }))
                    }
                    rows={3}
                    className="mt-1 resize-none text-xs"
                    placeholder="役割、性格、背景など"
                  />
                </div>
                <div className="grid grid-cols-2 gap-2">
                  <div>
                    <Label>HP</Label>
                    <Input
                      type="number"
                      min={0}
                      value={genericPcDraft.hp}
                      onChange={(e) =>
                        setGenericPcDraft((prev) => ({ ...prev, hp: e.target.value }))
                      }
                    />
                  </div>
                  <div>
                    <Label>MP / リソース</Label>
                    <Input
                      type="number"
                      min={0}
                      value={genericPcDraft.mp}
                      onChange={(e) =>
                        setGenericPcDraft((prev) => ({ ...prev, mp: e.target.value }))
                      }
                    />
                  </div>
                </div>
                <div className="space-y-2">
                  <Label>技能 / 能力値</Label>
                  {genericPcDraft.skills.map((skill, index) => (
                    <div key={index} className="grid grid-cols-[1fr_96px] gap-2">
                      <Input
                        value={skill.name}
                        onChange={(e) =>
                          setGenericPcDraft((prev) => ({
                            ...prev,
                            skills: prev.skills.map((item, itemIndex) =>
                              itemIndex === index
                                ? { ...item, name: e.target.value }
                                : item,
                            ),
                          }))
                        }
                        placeholder="技能名"
                      />
                      <Input
                        type="number"
                        value={skill.value}
                        onChange={(e) =>
                          setGenericPcDraft((prev) => ({
                            ...prev,
                            skills: prev.skills.map((item, itemIndex) =>
                              itemIndex === index
                                ? { ...item, value: e.target.value }
                                : item,
                            ),
                          }))
                        }
                        placeholder="値"
                      />
                    </div>
                  ))}
                </div>
                <div>
                  <Label>所持品</Label>
                  <Textarea
                    value={genericPcDraft.items}
                    onChange={(e) =>
                      setGenericPcDraft((prev) => ({
                        ...prev,
                        items: e.target.value,
                      }))
                    }
                    rows={2}
                    className="mt-1 resize-none text-xs"
                    placeholder="改行または読点で区切り"
                  />
                </div>
                <div>
                  <Label>メモ</Label>
                  <Textarea
                    value={genericPcDraft.notes}
                    onChange={(e) =>
                      setGenericPcDraft((prev) => ({
                        ...prev,
                        notes: e.target.value,
                      }))
                    }
                    rows={2}
                    className="mt-1 resize-none text-xs"
                  />
                </div>
              </div>
            )}
            <div className="flex justify-end">
              <Button onClick={handleJoin} disabled={!joinName.trim() || joining}>
                {joining ? "入室中…" : "入室"}
              </Button>
            </div>
          </div>
        </DialogContent>
    </Dialog>
  );
}
