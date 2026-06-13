import { create } from "zustand";
import type { Task } from "../types/api";
import { tasksRepo } from "../repositories";

const COMPLETION_UNDO_TTL_MS = 5000;

export type TaskCompletionUndoEntry = {
  taskId: string;
  title: string;
  previous: {
    status: string;
    completed_at?: string | null;
  };
};

type TaskCompletionUndoBatch = {
  id: string;
  entries: TaskCompletionUndoEntry[];
  message: string;
};

type TaskCompletionUndoState = {
  batches: TaskCompletionUndoBatch[];
  refreshToken: number;
  enqueueBatch: (batch: {
    entries: TaskCompletionUndoEntry[];
    message?: string;
  }) => void;
  removeBatch: (batchId: string) => void;
  undoBatch: (batchId: string) => Promise<void>;
};

const timers = new Map<string, ReturnType<typeof setTimeout>>();

function createBatchId() {
  return `task-complete-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function clearBatchTimer(batchId: string) {
  const timer = timers.get(batchId);
  if (timer) {
    clearTimeout(timer);
    timers.delete(batchId);
  }
}

function normalizeCompletionStatus(status: string) {
  return status === "done" ? "closed" : status;
}

export function isTaskCompletionTransition(
  previousStatus: string,
  nextStatus: string,
) {
  return (
    normalizeCompletionStatus(previousStatus) !== "closed" &&
    normalizeCompletionStatus(nextStatus) === "closed"
  );
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

function buildCompletionMessage(entries: TaskCompletionUndoEntry[]) {
  if (entries.length === 1) {
    return `「${entries[0]?.title ?? "タスク"}」を完了しました`;
  }
  return `${entries.length}件のタスクを完了しました`;
}

export const useTaskCompletionUndoStore = create<TaskCompletionUndoState>(
  (set, get) => ({
    batches: [],
    refreshToken: 0,
    enqueueBatch: ({ entries, message }) => {
      if (!entries.length) return;
      const batchId = createBatchId();
      set((state) => ({
        batches: [
          ...state.batches,
          {
            id: batchId,
            entries,
            message: message ?? buildCompletionMessage(entries),
          },
        ],
      }));
      clearBatchTimer(batchId);
      timers.set(
        batchId,
        setTimeout(() => {
          get().removeBatch(batchId);
        }, COMPLETION_UNDO_TTL_MS),
      );
    },
    removeBatch: (batchId) => {
      clearBatchTimer(batchId);
      set((state) => ({
        batches: state.batches.filter((batch) => batch.id !== batchId),
      }));
    },
    undoBatch: async (batchId) => {
      const batch = get().batches.find((item) => item.id === batchId);
      if (!batch) return;
      clearBatchTimer(batchId);
      set((state) => ({
        batches: state.batches.filter((item) => item.id !== batchId),
      }));
      try {
        await Promise.all(
          batch.entries.map((entry) =>
            tasksRepo.update(entry.taskId, {
              status: entry.previous.status,
              completed_at: entry.previous.completed_at ?? null,
            }),
          ),
        );
        set((state) => ({ refreshToken: state.refreshToken + 1 }));
      } catch (error) {
        console.error("完了Undo失敗:", error);
      }
    },
  }),
);

export function enqueueTaskCompletionUndoBatch(batch: {
  entries: TaskCompletionUndoEntry[];
  message?: string;
}) {
  useTaskCompletionUndoStore.getState().enqueueBatch(batch);
}
