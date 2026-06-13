"use client";

import { useCallback, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Textarea } from "@/components/ui/textarea";
import { MessageSquare, Send } from "lucide-react";
import {
  extractMentionTargets,
  py,
  targetLabel,
  type Participant,
  type PrivateMessage,
  type Room,
} from "@/lib/trpg-room-utils";

// 右カラム: 個別チャットパネル
export function PrivateChatPanel({
  room,
  myParticipantId,
  privateMessages,
  activeParticipants,
  visibleTargets,
  loadPrivateMessages,
}: {
  room: Room;
  myParticipantId: string;
  privateMessages: PrivateMessage[];
  activeParticipants: Participant[];
  visibleTargets: Participant[];
  loadPrivateMessages: () => Promise<void> | void;
}) {
  const [privateTargets, setPrivateTargets] = useState<string[]>([]);
  const [privateText, setPrivateText] = useState("");
  const [privateBusy, setPrivateBusy] = useState(false);

  const togglePrivateTarget = useCallback((targetId: string) => {
    setPrivateTargets((prev) =>
      prev.includes(targetId)
        ? prev.filter((id) => id !== targetId)
        : [...prev, targetId],
    );
  }, []);

  const handleSendPrivateMessage = useCallback(async () => {
    if (!room || !myParticipantId || !privateText.trim()) return;
    const mentioned = extractMentionTargets(privateText, activeParticipants);
    const targets = Array.from(new Set([...privateTargets, ...mentioned])).filter(
      (id) => id !== myParticipantId,
    );
    if (targets.length === 0) {
      alert("個別チャットの宛先を選ぶか、@名前 でメンションしてください");
      return;
    }
    setPrivateBusy(true);
    try {
      await py<{ message: PrivateMessage; gm_reply?: PrivateMessage | null }>(
        `/api/trpg/rooms/${room.id}/private-messages`,
        {
          method: "POST",
          body: JSON.stringify({
            sender_participant_id: myParticipantId,
            target_participant_ids: targets,
            content: privateText.trim(),
            message_type: targets.includes("gm") ? "gm" : "private",
            request_gm_reply: true,
          }),
        },
      );
      setPrivateText("");
      setPrivateTargets([]);
      await loadPrivateMessages();
    } catch (e) {
      console.error(e);
      alert("個別チャットの送信に失敗しました");
    } finally {
      setPrivateBusy(false);
    }
  }, [
    activeParticipants,
    loadPrivateMessages,
    myParticipantId,
    privateTargets,
    privateText,
    room,
  ]);

  return (
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="flex items-center gap-2 text-sm">
                <MessageSquare className="h-4 w-4" />
                個別チャット
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-3 text-xs">
              <div className="max-h-56 space-y-2 overflow-auto pr-1">
                {privateMessages.map((message) => {
                  const mine = message.sender_participant_id === myParticipantId;
                  return (
                    <div
                      key={message.id}
                      className={`rounded border p-2 ${mine ? "bg-primary/10" : "bg-muted/30"}`}
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span className="truncate font-semibold">
                          {message.sender_label || "AI GM"}
                        </span>
                        <Badge variant="outline">
                          {message.target_participant_ids.map((id) => targetLabel(id, room.participants)).join(", ")}
                        </Badge>
                      </div>
                      <div className="mt-1 whitespace-pre-wrap text-muted-foreground">
                        {message.content}
                      </div>
                    </div>
                  );
                })}
                {privateMessages.length === 0 && (
                  <div className="rounded border border-dashed p-3 text-center text-muted-foreground">
                    個別チャットはまだありません。
                  </div>
                )}
              </div>
              <div className="space-y-2 border-t pt-2">
                <div className="grid grid-cols-2 gap-1">
                  <label className="flex items-center gap-2 rounded border px-2 py-1">
                    <Checkbox
                      checked={privateTargets.includes("gm")}
                      onCheckedChange={() => togglePrivateTarget("gm")}
                    />
                    <span>AI GM</span>
                  </label>
                  {visibleTargets
                    .filter((participant) => participant.id !== myParticipantId)
                    .map((participant) => (
                      <label key={participant.id} className="flex items-center gap-2 rounded border px-2 py-1">
                        <Checkbox
                          checked={privateTargets.includes(participant.id)}
                          onCheckedChange={() => togglePrivateTarget(participant.id)}
                        />
                        <span className="min-w-0 truncate">{participant.display_name}</span>
                      </label>
                    ))}
                </div>
                <Textarea
                  value={privateText}
                  onChange={(e) => setPrivateText(e.target.value)}
                  rows={3}
                  className="resize-none"
                  placeholder="@PC名 でも宛先追加"
                />
                <Button
                  type="button"
                  size="sm"
                  className="w-full"
                  onClick={handleSendPrivateMessage}
                  disabled={privateBusy || !myParticipantId || !privateText.trim()}
                >
                  <Send className="mr-1 h-3 w-3" />
                  個別送信
                </Button>
              </div>
            </CardContent>
          </Card>
  );
}
