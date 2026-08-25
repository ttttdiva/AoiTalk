"use client";

import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { AlertDialog } from "@/components/ui/alert-dialog";
import type { StoryAssistTarget } from "@/components/story/assist/types";
import type { StoryAssistController } from "@/components/story/assist/use-story-assist";
import { seedInstructionFromSelection } from "@/components/story/assist/use-story-assist";
import { StoryAssistTrigger } from "@/components/story/assist/story-assist-dialog";
import { resolveNativeSelection } from "@/components/story/assist/native-selection";

type StoryAssistFieldProps = {
  assist: StoryAssistController;
  target: StoryAssistTarget;
  children: ReactNode;
  showTrigger?: boolean;
  triggerLabel?: string;
  triggerDisabled?: boolean;
  className?: string;
};

export function StoryAssistField({
  assist,
  target,
  children,
  showTrigger = true,
  triggerLabel,
  triggerDisabled = false,
  className,
}: StoryAssistFieldProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [notesConfirmOpen, setNotesConfirmOpen] = useState(false);

  const launchAssist = useCallback(() => {
    const selection = target.getSelection?.()
      ?? (containerRef.current ? resolveNativeSelection(containerRef.current) : null);
    const payload: StoryAssistTarget = {
      ...target,
      getCurrentText: target.getCurrentText,
      getSelection: () => selection,
    };
    if (target.requiresNotesConfirmation) {
      assist.confirmPrivateNotesAndOpen(payload, seedInstructionFromSelection(selection));
      return;
    }
    assist.openAssist(payload, seedInstructionFromSelection(selection));
  }, [assist, target]);

  const handleAssistClick = useCallback(() => {
    if (target.requiresNotesConfirmation) {
      setNotesConfirmOpen(true);
      return;
    }
    launchAssist();
  }, [launchAssist, target.requiresNotesConfirmation]);

  useEffect(() => {
    const element = containerRef.current;
    if (!element) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (!(event.ctrlKey || event.metaKey) || event.key.toLowerCase() !== "i") return;
      if (event.shiftKey || event.altKey) return;
      event.preventDefault();
      event.stopPropagation();
      handleAssistClick();
    };
    element.addEventListener("keydown", onKeyDown);
    return () => element.removeEventListener("keydown", onKeyDown);
  }, [handleAssistClick]);

  return (
    <div ref={containerRef} className={className} data-story-assist-field={target.fieldKind}>
      {children}
      {showTrigger ? (
        <div className="mt-2 flex justify-end">
          <StoryAssistTrigger
            label={triggerLabel}
            disabled={triggerDisabled}
            onClick={handleAssistClick}
          />
        </div>
      ) : null}
      {target.requiresNotesConfirmation ? (
        <AlertDialog
          open={notesConfirmOpen}
          title="非公開メモをAIに渡します"
          description="この欄の内容は通常AI文脈に入りません。今回の修正依頼にだけ渡してよろしいですか？"
          confirmLabel="渡して修正を依頼"
          cancelLabel="キャンセル"
          onConfirm={() => {
            setNotesConfirmOpen(false);
            launchAssist();
          }}
          onCancel={() => setNotesConfirmOpen(false)}
        />
      ) : null}
    </div>
  );
}
