"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  taskProposalsApi,
  TASK_PROPOSALS_REFRESH_EVENT,
  TaskProposalsApiError,
  type TaskProposal,
  type TaskProposalDecisionAction,
} from "@/lib/task-proposals-api";

export type TaskProposalCollectionStatus =
  | "idle"
  | "loading"
  | "ready"
  | "empty"
  | "error"
  | "unavailable"
  | "uncertain";

export const TASK_PROPOSAL_UNCERTAIN_MESSAGE =
  "提案の状態を確認できませんでした。再送せず、更新して確認してください。";

export type TaskProposalViewState = {
  status: TaskProposalCollectionStatus;
  proposals: TaskProposal[];
  error: string | null;
  uncertainNotice?: string | null;
};

export type TaskProposalDecisionState =
  | "idle"
  | "submitting"
  | "accepted"
  | "rejected"
  | "uncertain";

export type TaskProposalDecisionResult = {
  action: TaskProposalDecisionAction;
  accepted: boolean;
  uncertain: boolean;
  message?: string;
};

type UseTaskProposalsOptions = {
  taskIds?: readonly string[];
  enabled?: boolean;
  loading?: boolean;
};

type LoadOptions = {
  silent?: boolean;
};

function normalizedIds(taskIds: readonly string[] | undefined): string[] {
  return [
    ...new Set(
      (taskIds ?? []).map((taskId) => taskId.trim()).filter(Boolean),
    ),
  ];
}

function proposalKey(proposal: Pick<TaskProposal, "id" | "revision">): string {
  return `${proposal.id}:${proposal.revision}`;
}

function messageForError(error: unknown): string {
  return error instanceof Error ? error.message : "承認待ちの提案を取得できませんでした";
}

function proposalErrorCode(error: unknown): string | null {
  if (!(error instanceof TaskProposalsApiError)) return null;
  if (typeof error.responseBody !== "object" || error.responseBody === null) {
    return null;
  }
  const body = error.responseBody as { code?: unknown; detail?: unknown };
  if (typeof body.code === "string") return body.code;
  if (typeof body.detail !== "object" || body.detail === null) return null;
  const detailCode = (body.detail as { code?: unknown }).code;
  return typeof detailCode === "string" ? detailCode : null;
}

function isProposalUncertainError(error: unknown): boolean {
  return proposalErrorCode(error) === "proposal_uncertain";
}

function decisionFailureMessage(error: unknown): {
  uncertain: boolean;
  message: string;
} {
  if (isProposalUncertainError(error)) {
    return { uncertain: true, message: TASK_PROPOSAL_UNCERTAIN_MESSAGE };
  }
  if (error instanceof TaskProposalsApiError) {
    if (error.status === 403) {
      return { uncertain: false, message: "この提案を決定する権限がありません。" };
    }
    if (error.status === 404 || error.status === 409) {
      return {
        uncertain: false,
        message: "提案は最新状態ではありません。更新して確認してください。",
      };
    }
    if (error.status >= 400 && error.status < 500) {
      return {
        uncertain: false,
        message: "提案を決定できませんでした。更新して確認してください。",
      };
    }
  }
  return {
    uncertain: true,
    message: "結果を確認できませんでした。更新して状態を確認してください。",
  };
}

export function useTaskProposals({
  taskIds = [],
  enabled = true,
  loading = false,
}: UseTaskProposalsOptions = {}) {
  const ids = useMemo(() => normalizedIds(taskIds), [taskIds]);
  const [state, setState] = useState<TaskProposalViewState>({
    status: enabled && (loading || ids.length > 0) ? "loading" : "empty",
    proposals: [],
    error: null,
    uncertainNotice: null,
  });
  const [decisionStates, setDecisionStates] = useState<
    Record<string, TaskProposalDecisionState>
  >({});
  const generationRef = useRef(0);
  const decisionLocksRef = useRef(new Set<string>());

  const load = useCallback(
    async ({ silent = false }: LoadOptions = {}) => {
      const requestGeneration = ++generationRef.current;
      if (!enabled) {
        setState((current) => ({
          status: "empty",
          proposals: [],
          error: null,
          uncertainNotice: silent ? current.uncertainNotice : null,
        }));
        return;
      }
      if (loading) {
        setState((current) => ({
          ...current,
          status: "loading",
          error: null,
          uncertainNotice: silent ? current.uncertainNotice : null,
        }));
        return;
      }
      if (ids.length === 0) {
        setState((current) => ({
          status: "empty",
          proposals: [],
          error: null,
          uncertainNotice: silent ? current.uncertainNotice : null,
        }));
        return;
      }
      if (!silent) {
        setState((current) => ({
          ...current,
          status: "loading",
          error: null,
          uncertainNotice: null,
        }));
      }
      try {
        const proposals = await taskProposalsApi.list(ids);
        if (generationRef.current !== requestGeneration) return;
        setState((current) => ({
          status: proposals.length > 0 ? "ready" : "empty",
          proposals,
          error: null,
          uncertainNotice: silent ? current.uncertainNotice : null,
        }));
      } catch (error) {
        if (generationRef.current !== requestGeneration) return;
        const unavailable =
          error instanceof TaskProposalsApiError &&
          [404, 405, 501].includes(error.status);
        const uncertain = isProposalUncertainError(error);
        setState((current) => ({
          status: uncertain ? "uncertain" : unavailable ? "unavailable" : "error",
          proposals: [],
          error: uncertain ? TASK_PROPOSAL_UNCERTAIN_MESSAGE : messageForError(error),
          uncertainNotice: silent ? current.uncertainNotice : null,
        }));
      }
    },
    [enabled, ids, loading],
  );

  useEffect(() => {
    let cancelled = false;
    void Promise.resolve().then(() => {
      if (!cancelled) void load();
    });
    return () => {
      cancelled = true;
    };
  }, [load]);

  useEffect(() => {
    if (!enabled || loading || typeof window === "undefined") return;
    const refreshFromDecision = () => {
      void load({ silent: true });
    };
    window.addEventListener(TASK_PROPOSALS_REFRESH_EVENT, refreshFromDecision);
    return () =>
      window.removeEventListener(
        TASK_PROPOSALS_REFRESH_EVENT,
        refreshFromDecision,
      );
  }, [enabled, load, loading]);

  const refresh = useCallback(() => load(), [load]);

  const getDecisionState = useCallback(
    (proposal: TaskProposal): TaskProposalDecisionState =>
      decisionStates[proposalKey(proposal)] ?? "idle",
    [decisionStates],
  );

  const decide = useCallback(
    async (
      proposal: TaskProposal,
      action: TaskProposalDecisionAction,
    ): Promise<TaskProposalDecisionResult> => {
      const key = proposalKey(proposal);
      if (decisionLocksRef.current.has(key)) {
        const current = decisionStates[key];
        return {
          action,
          accepted: current === "accepted" || current === "rejected",
          uncertain: current === "uncertain",
        };
      }
      decisionLocksRef.current.add(key);
      setDecisionStates((current) => ({
        ...current,
        [key]: "submitting",
      }));

      try {
        await taskProposalsApi.decide(proposal.task_id, proposal.id, {
          action,
          revision: proposal.revision,
          plan_id: proposal.plan_id,
          plan_digest: proposal.plan_digest,
          action_digest: proposal.action_digest,
          material_action_preview_digest:
            proposal.material_action_preview_digest,
        });
        setDecisionStates((current) => ({
          ...current,
          [key]: action === "approve" ? "accepted" : "rejected",
        }));
        setState((current) => {
          const proposals = current.proposals.filter(
            (item) => proposalKey(item) !== key,
          );
          return {
            ...current,
            status: proposals.length > 0 ? "ready" : "empty",
            proposals,
            uncertainNotice: null,
          };
        });
        if (typeof window !== "undefined") {
          window.dispatchEvent(new Event(TASK_PROPOSALS_REFRESH_EVENT));
        }
        return { action, accepted: true, uncertain: false };
      } catch (error) {
        // A timeout/network failure can happen after the server accepted the
        // decision. Keep this card locked and reconcile from a fresh GET; the
        // button must never resend the same decision from this render.
        const failure = decisionFailureMessage(error);
        setDecisionStates((current) => ({
          ...current,
          [key]: failure.uncertain ? "uncertain" : "rejected",
        }));
        if (failure.uncertain) {
          setState((current) => ({
            ...current,
            uncertainNotice: failure.message,
          }));
        }
        if (typeof window !== "undefined") {
          window.dispatchEvent(new Event(TASK_PROPOSALS_REFRESH_EVENT));
        }
        return {
          action,
          accepted: false,
          uncertain: failure.uncertain,
          message: failure.message,
        };
      }
    },
    [decisionStates],
  );

  const exposedState: TaskProposalViewState =
    enabled && loading
      ? {
          status: "loading",
          proposals: [],
          error: null,
          uncertainNotice: state.uncertainNotice,
        }
      : state;

  return {
    ...exposedState,
    refresh,
    decide,
    getDecisionState,
  };
}
