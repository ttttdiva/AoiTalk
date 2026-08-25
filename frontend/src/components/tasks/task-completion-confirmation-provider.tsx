"use client";

import { useEffect, type ReactNode } from "react";

import { useConfirm } from "@/hooks/use-confirm";
import {
  registerTaskCompletionConfirmationHandler,
  type TaskCompletionConfirmationRequest,
} from "@/lib/task-completion-confirmation";

function buildDescription(request: TaskCompletionConfirmationRequest): string {
  const { incompleteSubtasks } = request;
  const visibleTitles = incompleteSubtasks
    .slice(0, 3)
    .map((subtask) => `「${subtask.title}」`)
    .join("、");
  const remaining = incompleteSubtasks.length - 3;
  const titleSummary = remaining > 0
    ? `${visibleTitles} ほか${remaining}件`
    : visibleTitles;
  return `未完了の直下サブタスク${incompleteSubtasks.length}件も同時に完了します。${titleSummary}`;
}

export function TaskCompletionConfirmationProvider({
  children,
}: {
  children: ReactNode;
}) {
  const confirm = useConfirm();

  useEffect(() => {
    let confirmationQueue = Promise.resolve();
    return registerTaskCompletionConfirmationHandler((request) => {
      const result = confirmationQueue.then(() =>
        confirm({
          title: "サブタスクも完了しますか？",
          description: buildDescription(request),
          confirmLabel: "すべて完了",
          cancelLabel: "キャンセル",
        }),
      );
      confirmationQueue = result.then(
        () => undefined,
        () => undefined,
      );
      return result;
    });
  }, [confirm]);

  return children;
}
