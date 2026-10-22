"use client";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import type { useChatIntent } from "@/hooks/use-chat-intent";

export function ChatIntentDialog({ intent, onClosed }: {
  intent: ReturnType<typeof useChatIntent>;
  onClosed: () => void;
}) {
  return (
    <Dialog open={intent.visible} onOpenChange={(open) => { if (!open) intent.close(); }}>
      <DialogContent size="sm" className="max-h-[85dvh] grid-rows-[auto_minmax(0,1fr)_auto]" showCloseButton={!intent.sending}
        finalFocus={() => { onClosed(); return false; }}>
        <DialogHeader>
          <DialogTitle>意図を選んで送信</DialogTitle>
          <DialogDescription>
            伝えたい意図を選んでください。%はAIの推定です。
            原文に意図タグを添えて送信します。添付は推定しません。
          </DialogDescription>
        </DialogHeader>
        <div className="min-h-0 space-y-2 overflow-y-auto">
          {intent.loading && <p role="status">意図を推定中…</p>}
          {intent.error && <p role="alert" className="text-destructive">{intent.error}</p>}
          {intent.result && <fieldset disabled={intent.sending} className="space-y-1">
            <legend className="sr-only">意図候補</legend>
            {intent.result.candidates.map((candidate) => (
              <label key={candidate.id} className="flex cursor-pointer items-center gap-3 rounded-md border px-3 py-2 has-checked:border-primary has-checked:bg-primary/10">
                <input type="radio" name="chat-intent" value={candidate.id} checked={intent.selectedId === candidate.id} onChange={() => intent.select(candidate.id)} />
                <span className="flex-1">{candidate.label}</span>
                <span className="text-xs tabular-nums text-muted-foreground" aria-label={`AIの推定 ${Math.round(candidate.score * 100)}%`}>
                  {Math.round(candidate.score * 100)}%
                </span>
              </label>
            ))}
          </fieldset>}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={intent.close} disabled={intent.sending}>キャンセル</Button>
          {intent.error && !intent.result && <Button variant="outline" onClick={intent.open} disabled={intent.loading}>再試行</Button>}
          <Button onClick={() => void intent.send()} disabled={!intent.selectedId || intent.loading || intent.sendDisabled}>
            {intent.sending ? "送信中…" : "この意図で送信"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
