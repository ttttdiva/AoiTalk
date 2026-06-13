"use client";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Heart, Sparkles, Users, Zap } from "lucide-react";
import { ParticipantAvatar } from "@/components/trpg/participant-avatar";
import type { Participant } from "@/lib/trpg-room-utils";

// 左カラム: 参加者一覧パネル
export function ParticipantsPanel({
  participants,
  myParticipantId,
  currentTurnParticipantId,
  myAvatarDraft,
  avatarSaving,
  onMyAvatarDraftChange,
  onSaveMyAvatar,
  onOpenAddNpc,
}: {
  participants: Participant[];
  myParticipantId: string;
  currentTurnParticipantId: string | null;
  myAvatarDraft: string;
  avatarSaving: boolean;
  onMyAvatarDraftChange: (value: string) => void;
  onSaveMyAvatar: () => void;
  onOpenAddNpc: () => void;
}) {
  return (
        <aside className="min-h-0 overflow-auto border-r p-3">
          <div className="mb-2 flex items-center justify-between text-xs text-muted-foreground">
            <div className="flex items-center gap-1">
              <Users className="h-3 w-3" /> 参加者 ({participants.length})
            </div>
            <Button variant="ghost" size="icon" className="h-5 w-5" onClick={onOpenAddNpc}>
              <Sparkles className="h-3 w-3" />
            </Button>
          </div>
          <div className="space-y-2">
            {participants.map((p) => {
              const pc = p.pc_state as {
                hp?: number;
                max_hp?: number;
                mp?: number;
                max_mp?: number;
                sanity?: number;
                luck?: number;
              };
              const isMe = p.id === myParticipantId;
              const isCurrentTurn = currentTurnParticipantId === p.id;
              return (
                <div
                  key={p.id}
                  className={`rounded border p-2 text-xs ${
                    isCurrentTurn ? "ring-2 ring-primary" : ""
                  } ${isMe ? "bg-primary/5" : ""}`}
                  style={{ borderLeftWidth: 4, borderLeftColor: p.color }}
                >
                  <div className="flex items-start justify-between gap-2">
                    <div className="flex min-w-0 items-center gap-2">
                      <ParticipantAvatar participant={p} size="sm" />
                      <span className="min-w-0 truncate font-semibold">
                        {p.display_name}
                      </span>
                    </div>
                    {isMe && <Badge variant="outline">自分</Badge>}
                  </div>
                  <div className="mt-1 text-muted-foreground">
                    {p.role === "gm"
                      ? "GM"
                      : p.role === "npc"
                      ? "NPC"
                      : p.role === "observer"
                      ? "観戦"
                      : "PC"}{" "}
                    · {p.participant_kind === "ai_character" ? "AI" : "人間"}
                  </div>
                  {isMe && (
                    <div className="mt-2 flex gap-1">
                      <Input
                        type="url"
                        value={myAvatarDraft}
                        onChange={(e) => onMyAvatarDraftChange(e.target.value)}
                        placeholder="アイコンURL"
                        className="h-7 text-xs"
                      />
                      <Button
                        type="button"
                        size="sm"
                        variant="outline"
                        className="h-7 px-2 text-xs"
                        onClick={onSaveMyAvatar}
                        disabled={avatarSaving}
                      >
                        保存
                      </Button>
                    </div>
                  )}
                  {typeof pc.hp === "number" && (
                    <div className="mt-1 flex items-center gap-1">
                      <Heart className="h-3 w-3 text-rose-500" />
                      {pc.hp}/{pc.max_hp ?? pc.hp}
                    </div>
                  )}
                  {typeof pc.mp === "number" && (
                    <div className="flex items-center gap-1">
                      <Zap className="h-3 w-3 text-sky-500" />
                      {pc.mp}/{pc.max_mp ?? pc.mp}
                    </div>
                  )}
                  {(typeof pc.sanity === "number" ||
                    typeof pc.luck === "number") && (
                    <div className="mt-1 flex gap-2 text-[11px] text-muted-foreground">
                      {typeof pc.sanity === "number" && (
                        <span>SAN {pc.sanity}</span>
                      )}
                      {typeof pc.luck === "number" && (
                        <span>幸運 {pc.luck}</span>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </aside>
  );
}
