"use client";

import { useCallback, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Sparkles } from "lucide-react";
import {
  py,
  type Participant,
  type QuickNPCSuggestion,
  type Room,
  type ScenarioCharacter,
} from "@/lib/trpg-room-utils";

// NPC追加（AIキャラクター招待）ダイアログ
export function AddNPCDialog({
  open,
  onOpenChange,
  room,
  onAdded,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  room: Room;
  onAdded: () => Promise<void> | void;
}) {
  const [addingNPC, setAddingNPC] = useState(false);
  const [quickNpcName, setQuickNpcName] = useState("");
  const [npcAvatarUrl, setNpcAvatarUrl] = useState("");
  const [quickNpcTheme, setQuickNpcTheme] = useState("");
  const [quickNpcSuggestion, setQuickNpcSuggestion] = useState<QuickNPCSuggestion | null>(null);
  const [suggestingNpcName, setSuggestingNpcName] = useState(false);

  const handleAddNPC = useCallback(async (sc: ScenarioCharacter) => {
    if (!room) return;
    setAddingNPC(true);
    try {
      await py<Participant>(
        `/api/trpg/rooms/${room.id}/join`,
        {
          method: "POST",
          body: JSON.stringify({
            display_name: sc.name,
            character_id: sc.character_id,
            role: "npc",
            avatar_url: npcAvatarUrl.trim(),
            as_npc: true,
          }),
        }
      );
      onOpenChange(false);
      await onAdded();
    } catch (e) {
      console.error(e);
      alert("NPCの追加に失敗しました");
    } finally {
      setAddingNPC(false);
    }
  }, [room, npcAvatarUrl, onOpenChange, onAdded]);

  const handleSuggestNPCName = useCallback(async () => {
    if (!room) return;
    setSuggestingNpcName(true);
    try {
      const result = await py<QuickNPCSuggestion>(
        `/api/trpg/rooms/${room.id}/npc/suggest-name`,
        {
          method: "POST",
          body: JSON.stringify({ theme: quickNpcTheme, name: quickNpcName }),
        },
      );
      setQuickNpcName(result.name || "");
      setQuickNpcSuggestion(result);
    } catch (e) {
      console.error(e);
      alert("NPCの生成に失敗しました");
    } finally {
      setSuggestingNpcName(false);
    }
  }, [room, quickNpcName, quickNpcTheme]);

  const handleAddQuickNPC = useCallback(async () => {
    if (!room || !quickNpcName.trim()) return;
    setAddingNPC(true);
    try {
      let suggestion = quickNpcSuggestion;
      if (!suggestion?.pc_state || suggestion.name !== quickNpcName.trim()) {
        suggestion = await py<QuickNPCSuggestion>(
          `/api/trpg/rooms/${room.id}/npc/suggest-name`,
          {
            method: "POST",
            body: JSON.stringify({ theme: quickNpcTheme, name: quickNpcName }),
          },
        );
      }
      await py<Participant>(
        `/api/trpg/rooms/${room.id}/join`,
        {
          method: "POST",
          body: JSON.stringify({
            display_name: quickNpcName.trim(),
            role: "npc",
            avatar_url: npcAvatarUrl.trim(),
            as_npc: true,
            pc_state: suggestion.pc_state,
          }),
        },
      );
      setQuickNpcName("");
      setNpcAvatarUrl("");
      setQuickNpcTheme("");
      setQuickNpcSuggestion(null);
      onOpenChange(false);
      await onAdded();
    } catch (e) {
      console.error(e);
      alert("即席NPCの追加に失敗しました");
    } finally {
      setAddingNPC(false);
    }
  }, [room, quickNpcName, npcAvatarUrl, quickNpcTheme, quickNpcSuggestion, onOpenChange, onAdded]);

  const handleAddGeneratedQuickNPC = useCallback(async () => {
    if (!room) return;
    setAddingNPC(true);
    try {
      const suggestion = await py<QuickNPCSuggestion>(
        `/api/trpg/rooms/${room.id}/npc/suggest-name`,
        {
          method: "POST",
          body: JSON.stringify({ theme: quickNpcTheme }),
        },
      );
      await py<Participant>(
        `/api/trpg/rooms/${room.id}/join`,
        {
          method: "POST",
          body: JSON.stringify({
            display_name: suggestion.name,
            role: "npc",
            avatar_url: npcAvatarUrl.trim(),
            as_npc: true,
            pc_state: suggestion.pc_state,
          }),
        },
      );
      setQuickNpcName("");
      setNpcAvatarUrl("");
      setQuickNpcTheme("");
      setQuickNpcSuggestion(null);
      onOpenChange(false);
      await onAdded();
    } catch (e) {
      console.error(e);
      alert("即席NPCの自動追加に失敗しました");
    } finally {
      setAddingNPC(false);
    }
  }, [room, npcAvatarUrl, quickNpcTheme, onOpenChange, onAdded]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent className="max-h-[90vh] w-[calc(100vw-2rem)] overflow-y-auto sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>AIキャラクターを招待</DialogTitle>
          </DialogHeader>
          <div className="min-w-0 space-y-4">
            <div className="min-w-0 space-y-2 rounded border p-3">
              <div className="text-sm font-medium">即席NPC</div>
              <div className="grid min-w-0 gap-2 sm:grid-cols-[minmax(0,1fr)_auto]">
                <Input
                  value={quickNpcName}
                  onChange={(e) => {
                    setQuickNpcName(e.target.value);
                    setQuickNpcSuggestion(null);
                  }}
                  placeholder="NPC名"
                />
                <Button
                  variant="outline"
                  onClick={handleSuggestNPCName}
                  disabled={suggestingNpcName}
                  className="w-full sm:w-auto"
                >
                  <Sparkles className="mr-1 h-4 w-4" />
                  {suggestingNpcName ? "生成中…" : "AIで生成"}
                </Button>
              </div>
              <Input
                type="url"
                value={npcAvatarUrl}
                onChange={(e) => setNpcAvatarUrl(e.target.value)}
                placeholder="任意: NPCアイコンURL"
              />
              <Textarea
                value={quickNpcTheme}
                onChange={(e) => {
                  setQuickNpcTheme(e.target.value);
                  setQuickNpcSuggestion(null);
                }}
                rows={3}
                className="resize-none"
                placeholder="任意: 追加指示（例: 怯えた参加者。数字に強い観察役。嘘をついている）"
              />
              {quickNpcSuggestion?.profile && (
                <div className="rounded border bg-muted/40 p-2 text-xs text-muted-foreground">
                  <div className="font-medium text-foreground">
                    {String(quickNpcSuggestion.profile.role || "役割未設定")}
                  </div>
                  <div>{String(quickNpcSuggestion.profile.background || "")}</div>
                  <div>{String(quickNpcSuggestion.profile.motivation || "")}</div>
                </div>
              )}
              <div className="flex flex-wrap justify-end gap-2">
                <Button
                  type="button"
                  variant="secondary"
                  onClick={handleAddGeneratedQuickNPC}
                  disabled={addingNPC}
                >
                  おまかせ追加
                </Button>
                <Button
                  onClick={handleAddQuickNPC}
                  disabled={addingNPC || !quickNpcName.trim()}
                >
                  追加
                </Button>
              </div>
            </div>
            <p className="text-sm text-muted-foreground">
              シナリオに登場するNPCをAIキャラクターとして同席させます。
            </p>
            <div className="grid gap-2">
              {room.scenario?.characters?.map((sc) => (
                <Button
                  key={sc.id}
                  variant="outline"
                  className="h-auto min-w-0 justify-start px-3 py-2 text-left"
                  onClick={() => handleAddNPC(sc)}
                  disabled={addingNPC}
                >
                  <div className="flex min-w-0 flex-col">
                    <span className="font-semibold">{sc.name}</span>
                    <span className="truncate text-[10px] text-muted-foreground">
                      {sc.role} - {sc.description}
                    </span>
                  </div>
                </Button>
              ))}
              {(!room.scenario?.characters || room.scenario.characters.length === 0) && (
                <div className="text-center py-4 text-xs text-muted-foreground">
                  招待可能なNPCが定義されていません。
                </div>
              )}
            </div>
          </div>
        </DialogContent>
    </Dialog>
  );
}
