"use client";

import {
  useCallback,
  useEffect,
  useState,
  type Dispatch,
  type SetStateAction,
} from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Brain, Clock } from "lucide-react";
import {
  getNpcStrategyState,
  npcStrategyStatusLabel,
  py,
  type Participant,
  type PlayLog,
  type Room,
} from "@/lib/trpg-room-utils";

// 右カラム: NPC作戦フェーズカード
export function NpcStrategyCard({
  room,
  setRoom,
}: {
  room: Room;
  setRoom: Dispatch<SetStateAction<Room | null>>;
}) {
  const [npcStrategyBusy, setNpcStrategyBusy] = useState(false);
  const npcStrategyState = getNpcStrategyState(room.shared_state || {});

  const handleScheduleNpcStrategy = useCallback(
    async (delaySeconds: number) => {
      if (!room) return;
      setNpcStrategyBusy(true);
      try {
        const result = await py<{
          shared_state: Record<string, unknown>;
        }>(`/api/trpg/rooms/${room.id}/npc/strategy/schedule`, {
          method: "POST",
          body: JSON.stringify({
            phase: "作戦タイム",
            delay_seconds: delaySeconds,
            focus: "現在の勝ち筋、協定、監査、裏切りリスク",
          }),
        });
        setRoom((prev) =>
          prev ? { ...prev, shared_state: result.shared_state || {} } : prev,
        );
      } catch (e) {
        console.error(e);
        alert("NPC作戦フェーズの予約に失敗しました");
      } finally {
        setNpcStrategyBusy(false);
      }
    },
    [room, setRoom],
  );

  const handleProcessNpcStrategy = useCallback(
    async (force = false) => {
      if (!room) return;
      setNpcStrategyBusy(true);
      try {
        const result = await py<{
          logs?: PlayLog[];
          participants?: Participant[];
          shared_state?: Record<string, unknown>;
        }>(`/api/trpg/rooms/${room.id}/npc/strategy/process`, {
          method: "POST",
          body: JSON.stringify({
            schedule_id: npcStrategyState?.id,
            force,
          }),
        });
        setRoom((prev) => {
          if (!prev) return prev;
          const nextLogs = [...prev.logs];
          for (const log of result.logs || []) {
            if (!nextLogs.some((item) => item.id === log.id)) {
              nextLogs.push(log);
            }
          }
          const nextParticipants = [...prev.participants];
          for (const participant of result.participants || []) {
            const idx = nextParticipants.findIndex((item) => item.id === participant.id);
            if (idx >= 0) nextParticipants[idx] = participant;
          }
          return {
            ...prev,
            logs: nextLogs,
            participants: nextParticipants,
            shared_state: result.shared_state || prev.shared_state,
          };
        });
      } catch (e) {
        console.error(e);
        alert("NPC作戦フェーズの処理に失敗しました");
      } finally {
        setNpcStrategyBusy(false);
      }
    },
    [npcStrategyState?.id, room, setRoom],
  );

  useEffect(() => {
    if (!npcStrategyState?.id || npcStrategyState.status !== "scheduled") return;
    const dueAt = npcStrategyState.due_at ? Date.parse(npcStrategyState.due_at) : NaN;
    if (Number.isNaN(dueAt)) return;
    const delay = Math.max(0, dueAt - Date.now() + 250);
    const timer = window.setTimeout(() => {
      void handleProcessNpcStrategy(false);
    }, delay);
    return () => window.clearTimeout(timer);
  }, [
    handleProcessNpcStrategy,
    npcStrategyState?.due_at,
    npcStrategyState?.id,
    npcStrategyState?.status,
  ]);

  return (
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="flex items-center gap-2 text-sm">
                <Brain className="h-4 w-4" />
                NPC作戦
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-3 text-xs">
              <div className="flex items-center justify-between gap-2">
                <span className="text-muted-foreground">
                  {npcStrategyState?.phase || "作戦タイム"}
                </span>
                <Badge variant="outline">
                  {npcStrategyStatusLabel(npcStrategyState?.status)}
                </Badge>
              </div>
              {npcStrategyState?.focus && (
                <div className="line-clamp-2 text-muted-foreground">
                  {npcStrategyState.focus}
                </div>
              )}
              {npcStrategyState?.due_at && npcStrategyState.status === "scheduled" && (
                <div className="flex items-center gap-1 text-muted-foreground">
                  <Clock className="h-3 w-3" />
                  {new Date(npcStrategyState.due_at).toLocaleTimeString()}
                </div>
              )}
              <div className="grid grid-cols-2 gap-2">
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  className="h-8 text-xs"
                  onClick={() => void handleScheduleNpcStrategy(30)}
                  disabled={npcStrategyBusy}
                >
                  30秒後
                </Button>
                <Button
                  type="button"
                  size="sm"
                  className="h-8 text-xs"
                  onClick={() =>
                    npcStrategyState?.status === "scheduled"
                      ? void handleProcessNpcStrategy(true)
                      : void handleScheduleNpcStrategy(0)
                  }
                  disabled={npcStrategyBusy}
                >
                  即時
                </Button>
              </div>
            </CardContent>
          </Card>
  );
}
