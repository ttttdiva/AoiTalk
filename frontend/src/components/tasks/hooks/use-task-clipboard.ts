"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type React from "react";

import { taskApi, type Task } from "@/lib/task-api";
import {
  saveTaskUpdate,
  type ClipboardMode,
  type TaskClipboard,
} from "@/lib/tasks-page-utils";
import type { FetchDataOptions } from "@/components/tasks/hooks/use-tasks-data";

/**
 * タスクのコピー / 切り取り / 貼り付け（Ctrl+C / X / V）をまとめたフック。
 */
export function useTaskClipboard({
  tasks,
  focusedTaskId,
  fetchData,
  focusTaskById,
  getKeyboardSelectionTasks,
  setSelectedIds,
  setBulkLoading,
  lastClickedIndexRef,
  prevShiftRangeRef,
  readOnly = false,
}: {
  tasks: Task[];
  focusedTaskId: string | null;
  fetchData: (options?: FetchDataOptions) => Promise<void>;
  focusTaskById: (taskId: string | null) => void;
  getKeyboardSelectionTasks: () => Task[];
  setSelectedIds: React.Dispatch<React.SetStateAction<Set<string>>>;
  setBulkLoading: React.Dispatch<React.SetStateAction<boolean>>;
  lastClickedIndexRef: React.RefObject<number | null>;
  prevShiftRangeRef: React.RefObject<Set<string>>;
  readOnly?: boolean;
}) {
  const clipboardRef = useRef<TaskClipboard>({ tasks: [], mode: "copy" });
  const [cutTaskIds, setCutTaskIds] = useState<Set<string>>(new Set());

  useEffect(() => {
    setCutTaskIds((prev) => {
      const next = new Set(
        [...prev].filter((taskId) => tasks.some((task) => task.id === taskId)),
      );
      return next.size === prev.size ? prev : next;
    });
  }, [tasks]);

  const handleClipboardStore = useCallback(
    (mode: ClipboardMode) => {
      if (readOnly && mode === "cut") return;
      const targetTasks = getKeyboardSelectionTasks();
      if (targetTasks.length === 0) return;

      clipboardRef.current = { tasks: targetTasks, mode };
      if (mode === "cut") {
        setCutTaskIds(new Set(targetTasks.map((task) => task.id)));
      } else {
        setCutTaskIds(new Set());
      }

      const copiedText = targetTasks.map((task) => task.title).join("\n");
      void navigator.clipboard.writeText(copiedText).catch(() => {});
    },
    [getKeyboardSelectionTasks, readOnly],
  );

  const handleClipboardPaste = useCallback(async () => {
    if (readOnly) return;
    const clipboard = clipboardRef.current;
    if (!clipboard.tasks.length || !focusedTaskId) return;

    const targetTask = tasks.find((task) => task.id === focusedTaskId);
    if (!targetTask) return;

    const clipboardIds = new Set(clipboard.tasks.map((task) => task.id));
    if (clipboard.mode === "cut" && clipboardIds.has(targetTask.id)) return;

    setBulkLoading(true);
    try {
      let insertedIds: string[] = [];

      if (clipboard.mode === "cut") {
        await Promise.all(
          clipboard.tasks.map((task) => {
            const patch: Record<string, unknown> = {};
            if (task.parent_task_id) patch.parent_task_id = null;
            if (task.project_id !== targetTask.project_id) {
              patch.project_id = targetTask.project_id;
            }
            return Object.keys(patch).length > 0
              ? saveTaskUpdate(task.id, patch, task.project_id)
              : Promise.resolve(task);
          }),
        );

        const orderedIds = tasks
          .filter(
            (task) =>
              task.project_id === targetTask.project_id &&
              !task.parent_task_id &&
              !clipboardIds.has(task.id),
          )
          .map((task) => task.id);
        const insertIndex = orderedIds.indexOf(targetTask.id);
        if (insertIndex === -1) return;

        insertedIds = clipboard.tasks.map((task) => task.id);
        const nextIds = [...orderedIds];
        nextIds.splice(insertIndex + 1, 0, ...insertedIds);
        await taskApi.reorderTasks(targetTask.project_id, nextIds);

        clipboardRef.current = { tasks: [], mode: "copy" };
        setCutTaskIds(new Set());
      } else {
        const createdTasks = await Promise.all(
          clipboard.tasks.map((task) => {
            const payload: Record<string, unknown> = {
              project_id: targetTask.project_id,
              title: task.title,
              description: task.description || "",
              status: task.status,
              priority: task.priority,
              start_at: task.start_at ?? null,
              end_at: task.end_at ?? null,
              all_day: task.all_day,
              ...(task.auto_close_on_due ? { auto_close_on_due: true } : {}),
              notifications_enabled: task.notifications_enabled,
              metadata: task.metadata ?? {},
            };
            if (task.project_id === targetTask.project_id && task.tags.length) {
              payload.tag_ids = task.tags.map((tag) => tag.id);
            }
            return taskApi.createTask(payload);
          }),
        );

        insertedIds = createdTasks.map((task) => task.id);
        const orderedIds = tasks
          .filter(
            (task) =>
              task.project_id === targetTask.project_id && !task.parent_task_id,
          )
          .map((task) => task.id);
        const insertIndex = orderedIds.indexOf(targetTask.id);
        if (insertIndex === -1) return;

        const nextIds = [...orderedIds];
        nextIds.splice(insertIndex + 1, 0, ...insertedIds);
        await taskApi.reorderTasks(targetTask.project_id, nextIds);
      }

      setSelectedIds(new Set(insertedIds));
      lastClickedIndexRef.current = null;
      prevShiftRangeRef.current = new Set();
      await fetchData();
      focusTaskById(insertedIds[0] ?? targetTask.id);
    } catch (err) {
      console.error("タスク貼り付け失敗:", err);
    } finally {
      setBulkLoading(false);
    }
  }, [
    fetchData,
    focusTaskById,
    focusedTaskId,
    lastClickedIndexRef,
    prevShiftRangeRef,
    setBulkLoading,
    setSelectedIds,
    tasks,
    readOnly,
  ]);

  return {
    clipboardRef,
    cutTaskIds,
    setCutTaskIds,
    handleClipboardStore,
    handleClipboardPaste,
  };
}
