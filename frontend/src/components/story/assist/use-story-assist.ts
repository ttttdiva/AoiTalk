"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { toast } from "sonner";
import { storyApi } from "@/lib/story/api";
import { proposeStoryAssist, selectionPayload } from "@/lib/story/assist-api";
import { useStoryJob } from "@/components/story/hooks/use-story-data";
import {
  applyStoryAssistProposal,
  storyAssistPreviewNewText,
  storyAssistPreviewOldText,
} from "@/components/story/assist/apply-selection";
import type { StoryAssistSelection, StoryAssistTarget } from "@/components/story/assist/types";
import { objectOf } from "@/lib/story/view-model";

type ProposalState = {
  text: string;
  /** episode_body 全文修正時の etag */
  etag?: string;
};

export function useStoryAssist() {
  const [open, setOpen] = useState(false);
  const [target, setTarget] = useState<StoryAssistTarget | null>(null);
  const [instruction, setInstruction] = useState("");
  const [proposal, setProposal] = useState<ProposalState | null>(null);
  const [running, setRunning] = useState(false);
  const [reviseJobId, setReviseJobId] = useState<string | null>(null);
  const [includePrivateNotes, setIncludePrivateNotes] = useState(false);
  const { job: reviseJob } = useStoryJob(reviseJobId);

  const selection = useMemo(
    () => target?.getSelection?.() ?? null,
    [target, open, proposal],
  );

  const resetProposal = useCallback(() => {
    setProposal(null);
    setReviseJobId(null);
  }, []);

  const close = useCallback(() => {
    setOpen(false);
    setTarget(null);
    setInstruction("");
    resetProposal();
    setIncludePrivateNotes(false);
  }, [resetProposal]);

  const openAssist = useCallback((nextTarget: StoryAssistTarget, seedInstruction = "", options?: { preservePrivateNotes?: boolean }) => {
    setTarget(nextTarget);
    setInstruction(seedInstruction);
    resetProposal();
    if (!options?.preservePrivateNotes) {
      setIncludePrivateNotes(false);
    }
    setOpen(true);
  }, [resetProposal]);

  const confirmPrivateNotesAndOpen = useCallback((nextTarget: StoryAssistTarget, seedInstruction = "") => {
    setIncludePrivateNotes(true);
    openAssist(nextTarget, seedInstruction, { preservePrivateNotes: true });
  }, [openAssist]);

  const requestProposal = useCallback(async () => {
    if (!target || !instruction.trim()) return;
    const currentText = target.getCurrentText();
    const activeSelection = target.getSelection?.() ?? null;
    setRunning(true);
    setProposal(null);
    try {
      const usesReviseJob =
        target.fieldKind === "episode_body"
        && target.episodeId
        && !(activeSelection && activeSelection.start < activeSelection.end);

      if (usesReviseJob) {
        const queued = objectOf(
          await storyApi.revise(target.episodeId!, { instruction: instruction.trim() }),
        );
        const id = typeof queued.id === "string" ? queued.id : null;
        if (!id) throw new Error("修正ジョブIDが返りませんでした");
        setReviseJobId(id);
        return;
      }

      const response = await proposeStoryAssist({
        field_kind: target.fieldKind,
        current_text: currentText,
        instruction: instruction.trim(),
        work_id: target.workId,
        episode_id: target.episodeId,
        character_id: target.characterId,
        rulebook_id: target.rulebookId,
        note_id: target.noteId,
        selection: selectionPayload(activeSelection),
        include_private_notes: target.fieldKind === "character_notes" ? includePrivateNotes : false,
      });
      setProposal({ text: response.proposal });
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "AI修正案を取得できませんでした");
    } finally {
      setRunning(false);
    }
  }, [includePrivateNotes, instruction, target]);

  useEffect(() => {
    if (!reviseJob || ["queued", "running"].includes(reviseJob.status)) return;
    setReviseJobId(null);
    const result = objectOf(reviseJob.result);
    if (reviseJob.status === "done" && typeof result.proposal === "string") {
      setProposal({
        text: result.proposal,
        etag: typeof result.base_etag === "string" ? result.base_etag : undefined,
      });
      setRunning(false);
      return;
    }
    setRunning(false);
    toast.error(
      reviseJob.error
      || (reviseJob.status === "canceled" ? "AI修正はキャンセルされました" : "AI修正を取得できませんでした"),
    );
  }, [reviseJob]);

  const preview = useMemo(() => {
    if (!target || !proposal) return null;
    const currentText = target.getCurrentText();
    const activeSelection = target.getSelection?.() ?? null;
    return {
      oldText: storyAssistPreviewOldText(currentText, activeSelection),
      newText: storyAssistPreviewNewText(currentText, proposal.text, activeSelection),
      fullText: applyStoryAssistProposal(currentText, proposal.text, activeSelection),
    };
  }, [proposal, target]);

  const applyProposal = useCallback(async (): Promise<boolean> => {
    if (!target || !proposal || !preview) return false;
    try {
      if (target.fieldKind === "episode_body" && target.episodeId) {
        const etag = proposal.etag ?? target.getBodyEtag?.();
        if (!etag) {
          throw new Error("本文の保存用 etag がありません");
        }
        await storyApi.updateBody(target.episodeId, {
          body: preview.fullText,
          expected_etag: etag,
          commit: true,
          message: "AI修正を適用",
          origin: "ai_edit",
          created_by: "ai",
        });
      }
      return true;
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "AI修正案を適用できませんでした");
      return false;
    }
  }, [preview, proposal, target]);

  return {
    open,
    target,
    instruction,
    setInstruction,
    proposal,
    running: running || Boolean(reviseJobId),
    selection,
    preview,
    openAssist,
    confirmPrivateNotesAndOpen,
    close,
    requestProposal,
    applyProposal,
    discardProposal: resetProposal,
    includePrivateNotes,
  };
}

export type StoryAssistController = ReturnType<typeof useStoryAssist>;

export function seedInstructionFromSelection(selection: StoryAssistSelection | null): string {
  if (!selection?.text.trim()) return "";
  const snippet = selection.text.trim().slice(0, 60);
  return `次の箇所を修正してください: ${snippet}`;
}
