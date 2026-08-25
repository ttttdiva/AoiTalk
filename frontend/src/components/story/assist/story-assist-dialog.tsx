"use client";

import { Loader2, WandSparkles } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { StoryDiffPreview } from "@/components/story/revisions/story-revisions-panel";
import type { StoryAssistController } from "@/components/story/assist/use-story-assist";

type StoryAssistDialogProps = {
  assist: StoryAssistController;
  onApplied?: (nextText: string) => void | Promise<void>;
};

export function StoryAssistDialog({ assist, onApplied }: StoryAssistDialogProps) {
  const {
    open,
    target,
    instruction,
    setInstruction,
    proposal,
    running,
    selection,
    preview,
    close,
    requestProposal,
    applyProposal,
    discardProposal,
  } = assist;

  const handleApply = async () => {
    if (!preview) return;
    const nextText = preview.fullText;
    const ok = await applyProposal();
    if (!ok) return;
    await onApplied?.(nextText);
    close();
  };

  return (
    <Dialog open={open} onOpenChange={(next) => { if (!next) close(); }}>
      <DialogContent size="3xl" className="max-h-[80vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>
            {target ? `AIに${target.fieldLabel}の修正を依頼` : "AI編集支援"}
          </DialogTitle>
        </DialogHeader>
        {selection?.text ? (
          <p className="text-xs text-muted-foreground">
            選択範囲（{selection.text.length}字）に対する修正案を生成します。
          </p>
        ) : (
          <p className="text-xs text-muted-foreground">
            フィールド全体に対する修正案を生成します。
          </p>
        )}
        <Label>修正指示</Label>
        <Textarea
          value={instruction}
          onChange={(event) => setInstruction(event.target.value)}
          placeholder="例: 視点を三人称に統一し、会話の間を増やす"
          className="min-h-24"
        />
        {preview ? (
          <div className="space-y-2">
            <div className="text-xs font-medium">修正案プレビュー</div>
            <div className="max-h-64 overflow-auto rounded-md border border-border bg-muted/20 p-3 text-sm leading-6">
              <StoryDiffPreview oldBody={preview.oldText} newBody={preview.newText} />
            </div>
          </div>
        ) : null}
        <DialogFooter>
          <Button variant="outline" onClick={close}>閉じる</Button>
          {proposal ? (
            <>
              <Button variant="outline" onClick={discardProposal}>破棄</Button>
              <Button onClick={() => void handleApply()}>この修正案を適用</Button>
            </>
          ) : (
            <Button onClick={() => void requestProposal()} disabled={running || !instruction.trim()}>
              {running && <Loader2 className="size-3.5 animate-spin" />}
              修正案を生成
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

type StoryAssistTriggerProps = {
  label?: string;
  disabled?: boolean;
  onClick: () => void;
  className?: string;
};

export function StoryAssistTrigger({
  label = "AIに修正を依頼",
  disabled = false,
  onClick,
  className,
}: StoryAssistTriggerProps) {
  return (
    <Button
      type="button"
      variant="link"
      size="sm"
      className={className ?? "h-6"}
      disabled={disabled}
      onClick={onClick}
    >
      <WandSparkles className="size-3.5" />
      {label}
    </Button>
  );
}
