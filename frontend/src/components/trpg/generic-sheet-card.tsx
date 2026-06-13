"use client";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { intValue } from "@/lib/trpg-room-utils";

// 右カラム: 汎用キャラクターシートカード
export function GenericSheetCard({
  myGenericState,
  genericSkillNames,
  genericSkillMap,
  onRoll: handleRoll,
}: {
  myGenericState: Record<string, unknown>;
  genericSkillNames: string[];
  genericSkillMap: Record<string, unknown>;
  onRoll: (
    expression?: string,
    options?: {
      target?: number | null;
      difficulty?: "regular" | "hard" | "extreme";
      note?: string;
    },
  ) => void;
}) {
  return (
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm">キャラクターシート</CardTitle>
              </CardHeader>
              <CardContent className="space-y-3 text-xs">
                {typeof myGenericState.description === "string" && myGenericState.description && (
                  <div className="whitespace-pre-wrap text-muted-foreground">
                    {myGenericState.description}
                  </div>
                )}
                <div className="grid grid-cols-2 gap-2">
                  {typeof myGenericState.hp === "number" && (
                    <div className="rounded border p-2">
                      <div className="text-[10px] text-muted-foreground">HP</div>
                      <div className="font-semibold">
                        {myGenericState.hp}/{String(myGenericState.max_hp ?? myGenericState.hp)}
                      </div>
                    </div>
                  )}
                  {typeof myGenericState.mp === "number" && (
                    <div className="rounded border p-2">
                      <div className="text-[10px] text-muted-foreground">MP</div>
                      <div className="font-semibold">
                        {myGenericState.mp}/{String(myGenericState.max_mp ?? myGenericState.mp)}
                      </div>
                    </div>
                  )}
                </div>
                {genericSkillNames.length > 0 && (
                  <div className="space-y-1">
                    <div className="text-[10px] text-muted-foreground">技能 / 能力値</div>
                    <div className="grid grid-cols-2 gap-1">
                      {genericSkillNames.map((skill) => (
                        <Button
                          key={skill}
                          type="button"
                          variant="outline"
                          size="sm"
                          className="h-auto justify-between gap-2 px-2 py-1 text-xs"
                          onClick={() =>
                            handleRoll(undefined, {
                              target: intValue(genericSkillMap[skill], 0) || null,
                              note: skill,
                            })
                          }
                        >
                          <span className="truncate">{skill}</span>
                          <span>{String(genericSkillMap[skill] ?? "")}</span>
                        </Button>
                      ))}
                    </div>
                  </div>
                )}
                {Array.isArray(myGenericState.items) && myGenericState.items.length > 0 && (
                  <div>
                    <div className="text-[10px] text-muted-foreground">所持品</div>
                    <div className="mt-1 flex flex-wrap gap-1">
                      {myGenericState.items.map((item) => (
                        <Badge key={String(item)} variant="outline">
                          {String(item)}
                        </Badge>
                      ))}
                    </div>
                  </div>
                )}
                {typeof myGenericState.notes === "string" && myGenericState.notes && (
                  <div className="whitespace-pre-wrap text-muted-foreground">
                    {myGenericState.notes}
                  </div>
                )}
              </CardContent>
            </Card>
  );
}
