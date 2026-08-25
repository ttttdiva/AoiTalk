"use client";

import { useCallback, useEffect, useRef } from "react";

import { taskApi, type Task } from "@/lib/task-api";
import {
  createTaskCompletionUndoEntry,
  dispatchTaskCompletionUndoBatch,
} from "@/lib/task-completion-undo";
import { saveTaskUpdate } from "@/lib/tasks-page-utils";
import type { FetchDataOptions } from "@/components/tasks/hooks/use-tasks-data";

// Undo: 直前の update / delete / bulk 操作を元に戻す
export type UndoEntry =
  | { type: "update"; taskId: string; previous: Record<string, unknown> }
  | {
      type: "bulkUpdate";
      entries: { taskId: string; previous: Record<string, unknown> }[];
    }
  | { type: "recreate"; tasks: Task[] };

/**
 * タスク一覧の Undo スタックと Ctrl+Z ハンドリングをまとめたフック。
 */
export function useTaskUndo({
  tasks,
  fetchData,
}: {
  tasks: Task[];
  fetchData: (options?: FetchDataOptions) => Promise<void>;
}) {
  const undoStackRef = useRef<UndoEntry[]>([]);
  const pushUndo = useCallback((entry: UndoEntry) => {
    const next = [...undoStackRef.current, entry];
    if (next.length > 50) next.shift();
    undoStackRef.current = next;
  }, []);

  const snapshotTask = useCallback(
    (task: Task, fields: (keyof Task)[]): Record<string, unknown> => {
      const snap: Record<string, unknown> = {};
      for (const f of fields) {
        const v = (task as unknown as Record<string, unknown>)[f as string];
        snap[f as string] = v === undefined ? null : v;
      }
      return snap;
    },
    [],
  );

  const queueTaskCompletionUndo = useCallback((entries: Task[]) => {
    dispatchTaskCompletionUndoBatch({
      entries: entries.map((task) => createTaskCompletionUndoEntry(task)),
    });
  }, []);

  const handleUndo = useCallback(async () => {
    const entry = undoStackRef.current.pop();
    if (!entry) return;
    try {
      if (entry.type === "update") {
        const currentTask = tasks.find((task) => task.id === entry.taskId);
        await saveTaskUpdate(
          entry.taskId,
          entry.previous,
          currentTask?.project_id ?? null,
        );
      } else if (entry.type === "bulkUpdate") {
        await Promise.all(
          entry.entries.map((e) => {
            const currentTask = tasks.find((task) => task.id === e.taskId);
            return saveTaskUpdate(
              e.taskId,
              e.previous,
              currentTask?.project_id ?? null,
            );
          }),
        );
      } else if (entry.type === "recreate") {
        await Promise.all(
          entry.tasks.map((t) =>
            taskApi.createTask({
              project_id: t.project_id,
              title: t.title,
              description: t.description || "",
              status: t.status,
              priority: t.priority,
              start_at: t.start_at ?? null,
              end_at: t.end_at ?? null,
              all_day: t.all_day,
              ...(t.auto_close_on_due ? { auto_close_on_due: true } : {}),
              notifications_enabled: t.notifications_enabled,
              parent_task_id: t.parent_task_id ?? null,
              tag_ids: (t.tags || []).map((tag) => tag.id),
            }),
          ),
        );
      }
      await fetchData();
    } catch (err) {
      console.error("Undo失敗:", err);
    }
  }, [fetchData, tasks]);

  // Ctrl+Z (Cmd+Z) で直前の操作を undo
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.defaultPrevented) return;
      if (!(e.ctrlKey || e.metaKey) || e.shiftKey || e.altKey) return;
      if (e.key.toLowerCase() !== "z") return;
      const target = e.target as HTMLElement | null;
      if (
        target &&
        (target.tagName === "INPUT" ||
          target.tagName === "TEXTAREA" ||
          target.tagName === "SELECT" ||
          target.isContentEditable)
      ) {
        return;
      }
      e.preventDefault();
      handleUndo();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [handleUndo]);

  return { pushUndo, snapshotTask, queueTaskCompletionUndo, handleUndo };
}
