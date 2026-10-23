"use client";

import { useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import type {
  TaskProposal,
  TaskProposalDecisionAction,
} from "@/lib/task-proposals-api";
import type {
  TaskProposalDecisionResult,
  TaskProposalDecisionState,
  TaskProposalViewState,
} from "@/components/tasks/hooks/use-task-proposals";
import { TASK_PROPOSAL_UNCERTAIN_MESSAGE } from "@/components/tasks/hooks/use-task-proposals";
import { cn } from "@/lib/utils";

export function TaskProposalIndicators({
  count = 0,
  childCount = 0,
  className,
}: {
  count?: number;
  childCount?: number;
  className?: string;
}) {
  if (count <= 0 && childCount <= 0) return null;

  const badgeClassName =
    "h-4 shrink-0 border-amber-400/70 bg-amber-50 px-1.5 text-[10px] font-medium text-amber-900 dark:border-amber-500/50 dark:bg-amber-950/40 dark:text-amber-200";

  return (
    <span className={cn("inline-flex min-w-0 items-center gap-1", className)}>
      {count > 0 ? (
        <Badge
          variant="outline"
          className={badgeClassName}
          aria-label={`${count}件の承認待ち提案`}
        >
          承認待ち{count > 1 ? ` ${count}` : ""}
        </Badge>
      ) : null}
      {childCount > 0 ? (
        <Badge
          variant="outline"
          className={badgeClassName}
          aria-label={`子タスクに${childCount}件の承認待ち提案`}
        >
          子タスク承認待ち{childCount > 1 ? ` ${childCount}` : ""}
        </Badge>
      ) : null}
    </span>
  );
}

export type TaskProposalCardProps = {
  proposal: TaskProposal;
  decisionState?: TaskProposalDecisionState;
  decisionDisabled?: boolean;
  onDecision: (
    action: TaskProposalDecisionAction,
  ) => Promise<TaskProposalDecisionResult>;
  onConfirm: (proposal: TaskProposal) => void;
  className?: string;
};

export function TaskProposalCard({
  proposal,
  decisionState = "idle",
  decisionDisabled = false,
  onDecision,
  onConfirm,
  className,
}: TaskProposalCardProps) {
  const [uncertain, setUncertain] = useState(false);
  const [decisionMessage, setDecisionMessage] = useState<string | null>(null);
  const [localDecision, setLocalDecision] = useState(false);
  const [pendingAction, setPendingAction] = useState<TaskProposalDecisionAction | null>(null);
  const isDecisionDisabled =
    decisionDisabled || decisionState !== "idle" || localDecision;
  // The one-line title is what the agent will do; the longer summary and the
  // server-owned plan text stay folded so the card remains compact.
  const detailText = [proposal.summary.trim(), proposal.plan_text.trim()]
    .filter((text, index, texts) => text && texts.indexOf(text) === index)
    .join("\n\n");

  const decide = async (action: TaskProposalDecisionAction) => {
    if (isDecisionDisabled) return;
    setLocalDecision(true);
    setPendingAction(action);
    try {
      const result = await onDecision(action);
      setPendingAction(null);
      if (result.uncertain) {
        setUncertain(true);
        setDecisionMessage(result.message ?? TASK_PROPOSAL_UNCERTAIN_MESSAGE);
        return;
      }
      if (result.message) {
        setDecisionMessage(result.message);
        // A settled/stale card is removed by the follow-up GET, so keep the
        // server outcome visible after the card disappears.
        toast.info(result.message);
        return;
      }
      if (!result.accepted) return;
      if (action === "approve") {
        toast.success("承認を受け付けました。実行を開始します。");
      } else {
        toast.success("拒否を受け付けました。");
      }
    } catch {
      setPendingAction(null);
      setUncertain(true);
    }
  };

  return (
    <article
      className={cn(
        "space-y-3 rounded-md border bg-background/70 p-3",
        className,
      )}
      data-testid={`task-proposal-card-${proposal.id}`}
      data-proposal-id={proposal.id}
      data-proposal-revision={String(proposal.revision)}
    >
      <div className="min-w-0">
        <h3
          className="truncate text-sm font-semibold"
          title={proposal.title}
        >
          {proposal.title}
        </h3>
        {detailText ? (
          <details
            className="mt-1 text-xs text-muted-foreground"
            data-testid={`task-proposal-details-${proposal.id}`}
          >
            <summary className="cursor-pointer select-none">詳細</summary>
            <p className="mt-1 max-h-40 overflow-y-auto whitespace-pre-wrap break-words">
              {detailText}
            </p>
          </details>
        ) : null}
      </div>
      {pendingAction || decisionState === "submitting" ? (
        <p role="status" className="text-xs text-muted-foreground">
          {pendingAction === "reject" ? "拒否" : "承認"}を処理しています…
        </p>
      ) : uncertain || decisionState === "uncertain" ? (
        <p role="status" className="text-xs text-amber-700 dark:text-amber-300">
          {decisionMessage ?? TASK_PROPOSAL_UNCERTAIN_MESSAGE}
        </p>
      ) : decisionMessage ? (
        <p role="status" className="text-xs text-amber-700 dark:text-amber-300">
          {decisionMessage}
        </p>
      ) : null}
      <div className="flex flex-wrap items-center gap-2">
        <Button
          type="button"
          size="sm"
          onClick={() => void decide("approve")}
          disabled={isDecisionDisabled}
        >
          承認
        </Button>
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={() => void decide("reject")}
          disabled={isDecisionDisabled}
        >
          拒否
        </Button>
        <Button
          type="button"
          size="sm"
          variant="ghost"
          onClick={() => onConfirm(proposal)}
        >
          確認
        </Button>
      </div>
    </article>
  );
}

export type TaskProposalListProps = {
  state: TaskProposalViewState;
  getDecisionState: (proposal: TaskProposal) => TaskProposalDecisionState;
  onDecision: (
    proposal: TaskProposal,
    action: TaskProposalDecisionAction,
  ) => Promise<TaskProposalDecisionResult>;
  onConfirm: (proposal: TaskProposal) => void;
  onRefresh?: () => void;
  decisionDisabled?: (proposal: TaskProposal) => boolean;
  heading?: string | null;
  className?: string;
};

export function TaskProposalList({
  state,
  getDecisionState,
  onDecision,
  onConfirm,
  onRefresh,
  decisionDisabled,
  heading = "承認待ちのAgent提案",
  className,
}: TaskProposalListProps) {
  if (state.status === "idle") return null;

  const hasDecisionRecovery = state.proposals.some((proposal) => {
    const decisionState = getDecisionState(proposal);
    return decisionState === "uncertain" || decisionState === "rejected";
  });
  const showRefresh =
    Boolean(onRefresh) &&
    (state.status === "error" ||
      state.status === "unavailable" ||
      state.status === "uncertain" ||
      Boolean(state.uncertainNotice) ||
      hasDecisionRecovery);
  const refreshButton = showRefresh ? (
    <Button type="button" size="xs" variant="outline" onClick={onRefresh}>
      更新
    </Button>
  ) : null;
  const headerRefreshButton =
    hasDecisionRecovery && !state.uncertainNotice ? refreshButton : null;

  return (
    <section
      aria-label={heading ?? "承認待ちのAgent提案"}
      className={cn("space-y-2", className)}
      data-testid="task-proposal-list"
      data-proposal-state={state.status}
    >
      {heading || headerRefreshButton ? (
        <div className="flex items-center justify-between gap-2">
          {heading ? <h2 className="text-sm font-semibold">{heading}</h2> : <span />}
          {headerRefreshButton}
        </div>
      ) : null}
      {state.uncertainNotice ? (
        <div className="flex items-center justify-between gap-2 rounded-md border border-amber-300 bg-amber-50 p-3 text-xs text-amber-800 dark:border-amber-700 dark:bg-amber-950/30 dark:text-amber-200">
          <p role="alert">{state.uncertainNotice}</p>
          {refreshButton}
        </div>
      ) : null}
      {state.status === "loading" ? (
        <p role="status" className="rounded-md border border-dashed p-3 text-xs text-muted-foreground">
          承認待ちの提案を読み込み中…
        </p>
      ) : state.status === "uncertain" ? (
        state.uncertainNotice ? null : (
          <div className="flex items-center justify-between gap-2 rounded-md border border-amber-300 bg-amber-50 p-3 text-xs text-amber-800 dark:border-amber-700 dark:bg-amber-950/30 dark:text-amber-200">
            <p role="alert">{TASK_PROPOSAL_UNCERTAIN_MESSAGE}</p>
            {refreshButton}
          </div>
        )
      ) : state.status === "error" ? (
        <div className="flex items-center justify-between gap-2 rounded-md border border-destructive/30 bg-destructive/10 p-3 text-xs text-destructive">
          <p role="alert">
            {state.error || "承認待ちの提案を取得できませんでした。"}
          </p>
          {state.uncertainNotice ? null : refreshButton}
        </div>
      ) : state.status === "unavailable" ? (
        <div className="flex items-center justify-between gap-2 rounded-md border border-dashed p-3 text-xs text-muted-foreground">
          <p role="status">提案機能は利用できません。</p>
          {state.uncertainNotice ? null : refreshButton}
        </div>
      ) : state.status === "empty" ? (
        state.uncertainNotice ? null : (
          <p role="status" className="rounded-md border border-dashed p-3 text-xs text-muted-foreground">
            承認待ちの提案はありません。
          </p>
        )
      ) : (
        <div className="space-y-2">
          {state.proposals.map((proposal) => (
            <TaskProposalCard
              key={`${proposal.id}:${proposal.revision}`}
              proposal={proposal}
              decisionState={getDecisionState(proposal)}
              decisionDisabled={decisionDisabled?.(proposal) ?? false}
              onDecision={(action) => onDecision(proposal, action)}
              onConfirm={onConfirm}
            />
          ))}
        </div>
      )}
    </section>
  );
}
