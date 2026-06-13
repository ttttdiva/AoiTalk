import type { Task } from "@/lib/task-api";

export const TASK_COMPLETION_UNDO_EVENT = "task-completion-undo";
export const TASK_COMPLETION_REFRESH_EVENT = "task-completion-refresh";

export type TaskCompletionUndoPrevious = {
  status: string;
  completed_at?: string | null;
};

export type TaskCompletionUndoEntry = {
  taskId: string;
  title: string;
  previous: TaskCompletionUndoPrevious;
};

export type TaskCompletionUndoBatch = {
  entries: TaskCompletionUndoEntry[];
  message?: string;
};

export function isTaskCompletionTransition(
  previousStatus: string,
  nextStatus: string,
): boolean {
  return previousStatus !== "done" && nextStatus === "done";
}

export function createTaskCompletionUndoEntry(
  task: Pick<Task, "id" | "title" | "status" | "completed_at">,
): TaskCompletionUndoEntry {
  return {
    taskId: task.id,
    title: task.title,
    previous: {
      status: task.status,
      completed_at: task.completed_at ?? null,
    },
  };
}

export function buildTaskCompletionUndoMessage(
  entries: TaskCompletionUndoEntry[],
): string {
  if (entries.length === 1) {
    return `「${entries[0]?.title ?? "タスク"}」を完了しました`;
  }
  return `${entries.length}件のタスクを完了しました`;
}

export function dispatchTaskCompletionUndoBatch(
  batch: TaskCompletionUndoBatch,
) {
  if (typeof window === "undefined" || batch.entries.length === 0) return;
  window.dispatchEvent(
    new CustomEvent<TaskCompletionUndoBatch>(TASK_COMPLETION_UNDO_EVENT, {
      detail: batch,
    }),
  );
}

export function dispatchTaskCompletionRefresh() {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new Event(TASK_COMPLETION_REFRESH_EVENT));
}
