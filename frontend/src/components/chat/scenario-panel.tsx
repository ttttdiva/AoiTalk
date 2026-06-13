"use client";

import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Button } from "@/components/ui/button";
import {
  BookOpen,
  MapPin,
  User,
  ChevronDown,
  ChevronUp,
  Dices,
} from "lucide-react";
import { useState } from "react";

type ScenarioPlaySession = {
  id: string;
  scenario_id: string;
  conversation_session_id: string;
  current_scene_id: string | null;
  player_state: Record<string, unknown>;
  perspective: string;
  status: string;
  scenario?: {
    id: string;
    title: string;
    description: string;
  };
  current_scene?: {
    id: string;
    title: string;
    description: string;
  };
};

type WritingSession = {
  id: string;
  scenario_id: string;
  conversation_session_id: string;
  target_scene_id: string;
  status: string;
  scenario?: {
    id: string;
    title: string;
  };
  target_scene?: {
    id: string;
    title: string;
  };
  target_episode?: {
    id: string;
    title: string;
  };
};

type RoleplaySession = {
  scenario: {
    id: string;
    title: string;
  };
  character: {
    id: string;
    name: string;
    role?: string;
    description?: string;
  };
};

export function ScenarioPanel({
  session,
  writingSession,
  roleplaySession,
  onRoll,
}: {
  session?: ScenarioPlaySession | null;
  writingSession?: WritingSession | null;
  roleplaySession?: RoleplaySession | null;
  onRoll?: (expression: string) => void;
}) {
  const [showSceneDesc, setShowSceneDesc] = useState(true);

  if (!session && !writingSession && !roleplaySession) return null;

  // 執筆モード
  if (writingSession) {
    return (
      <div className="flex flex-col gap-4 w-64 shrink-0 border-l bg-muted/10 h-full overflow-hidden">
        <ScrollArea className="flex-1">
          <div className="p-4 space-y-4">
            <div>
              <div className="flex items-center gap-2 mb-1 text-muted-foreground">
                <BookOpen className="size-3.5" />
                <span className="text-[10px] font-bold uppercase tracking-wider">
                  Writing
                </span>
              </div>
              <Badge variant="default" className="mb-2">
                執筆モード
              </Badge>
              <h3 className="font-semibold text-sm leading-tight">
                {writingSession.scenario?.title || "不明なシナリオ"}
              </h3>
            </div>

            <Separator />

            <div>
              <div className="flex items-center gap-2 mb-1 text-muted-foreground">
                <MapPin className="size-3.5" />
                <span className="text-[10px] font-bold uppercase tracking-wider">
                  対象シーン
                </span>
              </div>
              <h4 className="font-medium text-xs">
                {writingSession.target_scene?.title || "未設定"}
              </h4>
            </div>

            {writingSession.target_episode && (
              <>
                <Separator />
                <div>
                  <div className="flex items-center gap-2 mb-1 text-muted-foreground">
                    <BookOpen className="size-3.5" />
                    <span className="text-[10px] font-bold uppercase tracking-wider">
                      対象エピソード
                    </span>
                  </div>
                  <h4 className="font-medium text-xs">
                    {writingSession.target_episode.title}
                  </h4>
                </div>
              </>
            )}
          </div>
        </ScrollArea>
      </div>
    );
  }

  if (roleplaySession) {
    return (
      <div className="flex flex-col gap-4 w-64 shrink-0 border-l bg-muted/10 h-full overflow-hidden">
        <ScrollArea className="flex-1">
          <div className="p-4 space-y-4">
            <div>
              <div className="flex items-center gap-2 mb-1 text-muted-foreground">
                <BookOpen className="size-3.5" />
                <span className="text-[10px] font-bold uppercase tracking-wider">
                  Scenario
                </span>
              </div>
              <Badge variant="default" className="mb-2">
                ロールプレイ
              </Badge>
              <h3 className="font-semibold text-sm leading-tight">
                {roleplaySession.scenario.title}
              </h3>
            </div>

            <Separator />

            <div>
              <div className="flex items-center gap-2 mb-1 text-muted-foreground">
                <User className="size-3.5" />
                <span className="text-[10px] font-bold uppercase tracking-wider">
                  Character
                </span>
              </div>
              <h4 className="font-medium text-xs">
                {roleplaySession.character.name}
              </h4>
              {roleplaySession.character.role && (
                <Badge variant="outline" className="mt-2 text-[10px]">
                  {roleplaySession.character.role}
                </Badge>
              )}
              {roleplaySession.character.description && (
                <p className="mt-2 text-[11px] text-muted-foreground leading-relaxed">
                  {roleplaySession.character.description}
                </p>
              )}
            </div>
          </div>
        </ScrollArea>
      </div>
    );
  }

  if (!session) return null;

  const playerStateEntries = Object.entries(session.player_state || {});
  const commonDice = ["1d100", "1d20", "2d6", "1d6"];

  return (
    <div className="flex flex-col gap-4 w-64 shrink-0 border-l bg-muted/10 h-full overflow-hidden">
      <ScrollArea className="flex-1">
        <div className="p-4 space-y-4">
          {/* Scenario Info */}
          <div>
            <div className="flex items-center gap-2 mb-1 text-muted-foreground">
              <BookOpen className="size-3.5" />
              <span className="text-[10px] font-bold uppercase tracking-wider">
                Scenario
              </span>
            </div>
            <h3 className="font-semibold text-sm leading-tight">
              {session.scenario?.title || "不明なシナリオ"}
            </h3>
          </div>

          <Separator />

          {/* Current Scene */}
          <div>
            <div className="flex items-center justify-between mb-1">
              <div className="flex items-center gap-2 text-muted-foreground">
                <MapPin className="size-3.5" />
                <span className="text-[10px] font-bold uppercase tracking-wider">
                  Current Scene
                </span>
              </div>
              <button
                onClick={() => setShowSceneDesc(!showSceneDesc)}
                className="text-muted-foreground hover:text-foreground"
              >
                {showSceneDesc ? (
                  <ChevronUp className="size-3.5" />
                ) : (
                  <ChevronDown className="size-3.5" />
                )}
              </button>
            </div>
            <h4 className="font-medium text-xs mb-1">
              {session.current_scene?.title || "探索中..."}
            </h4>
            {showSceneDesc && session.current_scene?.description && (
              <p className="text-[11px] text-muted-foreground leading-relaxed italic">
                {session.current_scene.description}
              </p>
            )}
          </div>

          <Separator />

          {/* Dice Roll UI */}
          <div>
            <div className="flex items-center gap-2 mb-2 text-muted-foreground">
              <Dices className="size-3.5" />
              <span className="text-[10px] font-bold uppercase tracking-wider">
                Quick Dice
              </span>
            </div>
            <div className="grid grid-cols-2 gap-1.5">
              {commonDice.map((dice) => (
                <Button
                  key={dice}
                  variant="outline"
                  size="sm"
                  className="text-[10px] h-7 bg-background/50"
                  onClick={() => onRoll?.(dice)}
                >
                  {dice}
                </Button>
              ))}
            </div>
          </div>

          <Separator />

          {/* Player State */}
          <div>
            <div className="flex items-center gap-2 mb-2 text-muted-foreground">
              <User className="size-3.5" />
              <span className="text-[10px] font-bold uppercase tracking-wider">
                Player Status
              </span>
            </div>
            <div className="space-y-1.5">
              {playerStateEntries.length > 0 ? (
                playerStateEntries.map(([key, value]) => (
                  <div
                    key={key}
                    className="flex justify-between items-center bg-background/50 rounded px-2 py-1 text-xs border"
                  >
                    <span className="text-muted-foreground font-medium">
                      {key}
                    </span>
                    <span className="font-bold">{String(value)}</span>
                  </div>
                ))
              ) : (
                <p className="text-[11px] text-muted-foreground text-center py-2">
                  ステータス情報なし
                </p>
              )}
            </div>
          </div>

          {session.status === "completed" && (
            <Badge
              variant="outline"
              className="w-full justify-center py-1 text-[10px] bg-green-500/10 text-green-600 border-green-200"
            >
              COMPLETED
            </Badge>
          )}
        </div>
      </ScrollArea>
    </div>
  );
}
