"use client";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Archive, Brain, Dices, Shield, Swords } from "lucide-react";
import {
  COC_CHARACTERISTICS,
  intValue,
  isRecord,
  type Participant,
} from "@/lib/trpg-room-utils";
import type { CocActions } from "@/components/trpg/hooks/use-coc-actions";

// 右カラム: CoC キャラクターシートカード
export function CocSheetCard({
  coc,
  myCocState,
  activeParticipants,
  myParticipantId,
}: {
  coc: CocActions;
  myCocState: Record<string, unknown>;
  activeParticipants: Participant[];
  myParticipantId: string;
}) {
  const {
    cocBusy,
    cocSkillMap,
    cocSkillNames,
    selectedCocSkill,
    setCocSelectedSkill,
    selectedDevelopmentSkill,
    setCocDevelopmentSkill,
    cocCheckedSkills,
    cocWeaponNames,
    cocResourceAmount,
    setCocResourceAmount,
    cocResourceReason,
    setCocResourceReason,
    cocResistanceActive,
    setCocResistanceActive,
    cocResistancePassive,
    setCocResistancePassive,
    cocResistanceNote,
    setCocResistanceNote,
    cocCombatWeapon,
    setCocCombatWeapon,
    cocDefenderId,
    setCocDefenderId,
    cocDefenseType,
    setCocDefenseType,
    cocSpellName,
    setCocSpellName,
    cocSpellCosts,
    setCocSpellCosts,
    cocInsanityKind,
    setCocInsanityKind,
    cocInsanityReason,
    setCocInsanityReason,
    cocPostSessionSanExpression,
    setCocPostSessionSanExpression,
    cocPostSessionOutcome,
    setCocPostSessionOutcome,
    cocPostSessionBusy,
    handleCocSkillCheck,
    handleCocResource,
    handleCocResistance,
    handleCocDevelopment,
    handleCocPostSession,
    handleCocAttack,
    handleCocSpellCost,
    handleCocInsanity,
    handleCocResourceStep,
  } = coc;

  return (
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm">CoCキャラクターシート</CardTitle>
              </CardHeader>
              <CardContent className="space-y-2 text-xs">
                <div className="grid grid-cols-4 gap-1">
                  {COC_CHARACTERISTICS.map((key) => {
                    const chars = isRecord(myCocState.characteristics)
                      ? myCocState.characteristics
                      : {};
                    return (
                      <div key={key} className="rounded border px-2 py-1">
                        <div className="text-[10px] text-muted-foreground">{key}</div>
                        <div className="font-semibold">{String(chars[key] ?? "-")}</div>
                      </div>
                    );
                  })}
                </div>
                <div className="grid grid-cols-3 gap-1">
                  {["アイデア", "幸運", "知識"].map((key) => {
                    const stats = isRecord(myCocState.stats) ? myCocState.stats : {};
                    return (
                      <div key={key} className="rounded border px-2 py-1">
                        <div className="text-[10px] text-muted-foreground">{key}</div>
                        <div className="font-semibold">{String(stats[key] ?? "-")}</div>
                      </div>
                    );
                  })}
                </div>
                <div className="grid grid-cols-2 gap-2">
                  {[
                    ["hp", "HP", myCocState.hp, myCocState.max_hp],
                    ["mp", "MP", myCocState.mp, myCocState.max_mp],
                    ["sanity", "SAN", myCocState.sanity, myCocState.max_sanity],
                    ["luck", "幸運", myCocState.luck, 100],
                  ].map(([field, label, value, max]) => (
                    <div key={String(field)} className="rounded border p-2">
                      <div className="mb-1 flex items-center justify-between">
                        <span className="text-[10px] text-muted-foreground">
                          {String(label)}
                        </span>
                        <span className="font-semibold">
                          {String(value ?? 0)}/{String(max ?? 0)}
                        </span>
                      </div>
                      <div className="flex gap-1">
                        <Button
                          type="button"
                          variant="outline"
                          size="sm"
                          className="h-6 flex-1 px-1 text-xs"
                          onClick={() => {
                            if (field === "hp") void handleCocResource("hp", "damage", 1);
                            else if (field === "mp") void handleCocResource("mp", "spend", 1);
                            else if (field === "sanity") void handleCocResource("san", "loss", 1);
                            else void handleCocResourceStep("luck", -1);
                          }}
                          disabled={cocBusy}
                        >
                          -1
                        </Button>
                        <Button
                          type="button"
                          variant="outline"
                          size="sm"
                          className="h-6 flex-1 px-1 text-xs"
                          onClick={() => {
                            if (field === "hp") void handleCocResource("hp", "heal", 1);
                            else if (field === "mp") void handleCocResource("mp", "recover", 1);
                            else if (field === "sanity") void handleCocResource("san", "recover", 1);
                            else void handleCocResourceStep("luck", 1);
                          }}
                          disabled={cocBusy}
                        >
                          +1
                        </Button>
                      </div>
                    </div>
                  ))}
                </div>
                {Array.isArray(myCocState.conditions) && myCocState.conditions.length > 0 && (
                  <div className="flex flex-wrap gap-1">
                    {myCocState.conditions.map((condition) => (
                      <Badge key={String(condition)} variant="outline">
                        {String(condition)}
                      </Badge>
                    ))}
                  </div>
                )}
                <div className="space-y-2 border-t pt-2">
                  <div className="grid grid-cols-[1fr_auto] gap-2">
                    <select
                      value={selectedCocSkill}
                      onChange={(e) => setCocSelectedSkill(e.target.value)}
                      className="h-8 rounded border bg-background px-2 text-xs"
                      aria-label="CoC技能"
                    >
                      {cocSkillNames.map((skill) => (
                        <option key={skill} value={skill}>
                          {skill} {intValue(cocSkillMap[skill], 0)}
                        </option>
                      ))}
                    </select>
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => void handleCocSkillCheck()}
                      disabled={!selectedCocSkill || cocBusy}
                    >
                      <Dices className="mr-1 h-3 w-3" />
                      技能
                    </Button>
                  </div>
                  <div className="grid grid-cols-[72px_1fr] gap-2">
                    <Input
                      type="number"
                      min={0}
                      value={cocResourceAmount}
                      onChange={(e) => setCocResourceAmount(e.target.value)}
                      className="h-8"
                      aria-label="CoCリソース量"
                    />
                    <Input
                      value={cocResourceReason}
                      onChange={(e) => setCocResourceReason(e.target.value)}
                      className="h-8"
                      placeholder="理由"
                    />
                  </div>
                  <div className="grid grid-cols-3 gap-1">
                    <Button size="sm" variant="outline" onClick={() => void handleCocResource("hp", "damage")} disabled={cocBusy}>HP減</Button>
                    <Button size="sm" variant="outline" onClick={() => void handleCocResource("mp", "spend")} disabled={cocBusy}>MP消</Button>
                    <Button size="sm" variant="outline" onClick={() => void handleCocResource("san", "loss")} disabled={cocBusy}>SAN減</Button>
                    <Button size="sm" variant="outline" onClick={() => void handleCocResource("hp", "heal")} disabled={cocBusy}>HP回</Button>
                    <Button size="sm" variant="outline" onClick={() => void handleCocResource("mp", "recover")} disabled={cocBusy}>MP回</Button>
                    <Button size="sm" variant="outline" onClick={() => void handleCocResource("san", "recover")} disabled={cocBusy}>SAN回</Button>
                  </div>
                </div>
                <div className="space-y-2 border-t pt-2">
                  <div className="grid grid-cols-[1fr_1fr_auto] gap-2">
                    <Input
                      type="number"
                      value={cocResistanceActive}
                      onChange={(e) => setCocResistanceActive(e.target.value)}
                      className="h-8"
                      placeholder="能動"
                    />
                    <Input
                      type="number"
                      value={cocResistancePassive}
                      onChange={(e) => setCocResistancePassive(e.target.value)}
                      className="h-8"
                      placeholder="受動"
                    />
                    <Button size="sm" variant="outline" onClick={() => void handleCocResistance()} disabled={cocBusy}>
                      <Shield className="mr-1 h-3 w-3" />
                      抵抗
                    </Button>
                  </div>
                  <Input
                    value={cocResistanceNote}
                    onChange={(e) => setCocResistanceNote(e.target.value)}
                    className="h-8"
                    placeholder="抵抗表メモ"
                  />
                  <div className="grid grid-cols-[1fr_auto] gap-2">
                    <select
                      value={selectedDevelopmentSkill}
                      onChange={(e) => setCocDevelopmentSkill(e.target.value)}
                      className="h-8 rounded border bg-background px-2 text-xs"
                      aria-label="成長チェック技能"
                    >
                      {cocSkillNames.map((skill) => (
                        <option key={skill} value={skill}>
                          {skill}
                        </option>
                      ))}
                    </select>
                    <Button size="sm" variant="outline" onClick={() => void handleCocDevelopment()} disabled={!selectedDevelopmentSkill || cocBusy}>
                      成長
                    </Button>
                  </div>
                </div>
                <div className="space-y-2 border-t pt-2">
                  <div className="flex items-center justify-between gap-2 text-xs">
                    <span className="font-medium">後処理</span>
                    <Badge variant="outline">経験 {cocCheckedSkills.length}</Badge>
                  </div>
                  {cocCheckedSkills.length > 0 && (
                    <div className="flex flex-wrap gap-1">
                      {cocCheckedSkills.slice(0, 8).map((skill) => (
                        <Badge key={skill} variant="secondary">
                          {skill}
                        </Badge>
                      ))}
                      {cocCheckedSkills.length > 8 && (
                        <Badge variant="secondary">+{cocCheckedSkills.length - 8}</Badge>
                      )}
                    </div>
                  )}
                  <div className="grid grid-cols-[1fr_1fr] gap-2">
                    <Input
                      value={cocPostSessionSanExpression}
                      onChange={(e) => setCocPostSessionSanExpression(e.target.value)}
                      className="h-8"
                      placeholder="SAN回復 例: 1d6"
                    />
                    <Input
                      value={cocPostSessionOutcome}
                      onChange={(e) => setCocPostSessionOutcome(e.target.value)}
                      className="h-8"
                      placeholder="結果"
                    />
                  </div>
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => void handleCocPostSession()}
                    disabled={cocPostSessionBusy || (!cocCheckedSkills.length && !cocPostSessionSanExpression.trim())}
                    className="w-full"
                  >
                    <Archive className="mr-1 h-3 w-3" />
                    セッション後処理
                  </Button>
                </div>
                <div className="space-y-2 border-t pt-2">
                  <div className="grid grid-cols-2 gap-2">
                    <select
                      value={cocCombatWeapon}
                      onChange={(e) => setCocCombatWeapon(e.target.value)}
                      className="h-8 rounded border bg-background px-2 text-xs"
                      aria-label="武器"
                    >
                      {cocWeaponNames.map((weapon) => (
                        <option key={weapon} value={weapon}>
                          {weapon}
                        </option>
                      ))}
                    </select>
                    <select
                      value={cocDefenderId}
                      onChange={(e) => setCocDefenderId(e.target.value)}
                      className="h-8 rounded border bg-background px-2 text-xs"
                      aria-label="防御側"
                    >
                      <option value="">防御対象なし</option>
                      {activeParticipants
                        .filter((participant) => participant.id !== myParticipantId)
                        .map((participant) => (
                          <option key={participant.id} value={participant.id}>
                            {participant.display_name}
                          </option>
                        ))}
                    </select>
                  </div>
                  <div className="grid grid-cols-[1fr_auto] gap-2">
                    <select
                      value={cocDefenseType}
                      onChange={(e) => setCocDefenseType(e.target.value)}
                      className="h-8 rounded border bg-background px-2 text-xs"
                      aria-label="防御種別"
                    >
                      <option value="回避">回避</option>
                      <option value="組み付き">組み付き</option>
                      <option value="こぶし（パンチ）">こぶし</option>
                    </select>
                    <Button size="sm" variant="outline" onClick={() => void handleCocAttack()} disabled={cocBusy}>
                      <Swords className="mr-1 h-3 w-3" />
                      攻撃
                    </Button>
                  </div>
                </div>
                <div className="space-y-2 border-t pt-2">
                  <Input
                    value={cocSpellName}
                    onChange={(e) => setCocSpellName(e.target.value)}
                    className="h-8"
                    placeholder="呪文名"
                  />
                  <div className="grid grid-cols-4 gap-1">
                    {(["mp", "san", "hp", "pow"] as const).map((key) => (
                      <Input
                        key={key}
                        type="number"
                        min={0}
                        value={cocSpellCosts[key]}
                        onChange={(e) =>
                          setCocSpellCosts((prev) => ({ ...prev, [key]: e.target.value }))
                        }
                        className="h-8"
                        placeholder={key.toUpperCase()}
                        aria-label={`呪文${key.toUpperCase()}コスト`}
                      />
                    ))}
                  </div>
                  <div className="grid grid-cols-[1fr_auto] gap-2">
                    <select
                      value={cocInsanityKind}
                      onChange={(e) => setCocInsanityKind(e.target.value as "temporary" | "indefinite")}
                      className="h-8 rounded border bg-background px-2 text-xs"
                      aria-label="狂気種別"
                    >
                      <option value="temporary">一時的狂気</option>
                      <option value="indefinite">不定の狂気</option>
                    </select>
                    <Button size="sm" variant="outline" onClick={() => void handleCocInsanity()} disabled={cocBusy}>
                      <Brain className="mr-1 h-3 w-3" />
                      狂気
                    </Button>
                  </div>
                  <div className="grid grid-cols-[1fr_auto] gap-2">
                    <Input
                      value={cocInsanityReason}
                      onChange={(e) => setCocInsanityReason(e.target.value)}
                      className="h-8"
                      placeholder="狂気理由"
                    />
                    <Button size="sm" variant="outline" onClick={() => void handleCocSpellCost()} disabled={!cocSpellName.trim() || cocBusy}>
                      呪文
                    </Button>
                  </div>
                </div>
              </CardContent>
            </Card>
  );
}
