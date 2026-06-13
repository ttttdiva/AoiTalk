"use client";

import { useCallback, useEffect, useRef } from "react";
import { Toaster, toast } from "sonner";
import { taskApi } from "@/lib/task-api";
import {
  buildTaskCompletionUndoMessage,
  dispatchTaskCompletionRefresh,
  TASK_COMPLETION_UNDO_EVENT,
  type TaskCompletionUndoBatch,
} from "@/lib/task-completion-undo";

const COMPLETION_UNDO_TTL_MS = 5000;

type ActiveBatch = TaskCompletionUndoBatch & {
  id: string;
  consumed: boolean;
  createdAt: number;
};

function isEditableTarget(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null;
  if (!el) return false;
  return (
    el.tagName === "INPUT" ||
    el.tagName === "TEXTAREA" ||
    el.tagName === "SELECT" ||
    el.isContentEditable
  );
}

function createBatchId() {
  if (
    typeof crypto !== "undefined" &&
    typeof crypto.randomUUID === "function"
  ) {
    return crypto.randomUUID();
  }
  return `task-complete-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export function TaskCompletionUndoProvider() {
  const batchesRef = useRef<ActiveBatch[]>([]);

  const removeBatch = useCallback((batchId: string) => {
    batchesRef.current = batchesRef.current.filter(
      (batch) => batch.id !== batchId,
    );
  }, []);

  const undoBatch = useCallback(
    async (batchId: string) => {
      const batch = batchesRef.current.find((item) => item.id === batchId);
      if (!batch || batch.consumed) return;
      batch.consumed = true;
      removeBatch(batchId);
      toast.dismiss(batchId);
      try {
        await Promise.all(
          batch.entries.map((entry) =>
            taskApi.updateTask(entry.taskId, {
              status: entry.previous.status,
              completed_at: entry.previous.completed_at ?? null,
            }),
          ),
        );
        dispatchTaskCompletionRefresh();
        window.dispatchEvent(new Event("task-list-refresh"));
      } catch (err) {
        console.error("完了Undo失敗:", err);
        toast.error("Undoに失敗しました");
      }
    },
    [removeBatch],
  );

  const undoLatestBatch = useCallback(async () => {
    const latest = [...batchesRef.current]
      .filter((batch) => !batch.consumed)
      .sort((a, b) => b.createdAt - a.createdAt)[0];
    if (!latest) return false;
    await undoBatch(latest.id);
    return true;
  }, [undoBatch]);

  useEffect(() => {
    const handleBatch = (event: Event) => {
      const detail = (event as CustomEvent<TaskCompletionUndoBatch>).detail;
      if (!detail?.entries?.length) return;
      const batchId = createBatchId();
      const batch: ActiveBatch = {
        ...detail,
        id: batchId,
        consumed: false,
        createdAt: Date.now(),
      };
      batchesRef.current = [...batchesRef.current, batch];
      toast.success(
        detail.message ?? buildTaskCompletionUndoMessage(detail.entries),
        {
          id: batchId,
          duration: COMPLETION_UNDO_TTL_MS,
          description: "数秒間 Undo できます",
          action: {
            label: "Undo",
            onClick: () => {
              void undoBatch(batchId);
            },
          },
          onDismiss: () => removeBatch(batchId),
          onAutoClose: () => removeBatch(batchId),
        },
      );
    };
    window.addEventListener(TASK_COMPLETION_UNDO_EVENT, handleBatch);
    return () => {
      window.removeEventListener(TASK_COMPLETION_UNDO_EVENT, handleBatch);
    };
  }, [removeBatch, undoBatch]);

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented) return;
      if (!(event.ctrlKey || event.metaKey) || event.shiftKey || event.altKey) {
        return;
      }
      if (event.key.toLowerCase() !== "z") return;
      if (isEditableTarget(event.target)) return;
      if (!batchesRef.current.some((batch) => !batch.consumed)) return;
      event.preventDefault();
      void undoLatestBatch();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [undoLatestBatch]);

  return (
    <Toaster
      closeButton
      position="bottom-left"
      richColors
      toastOptions={{
        duration: COMPLETION_UNDO_TTL_MS,
      }}
    />
  );
}
