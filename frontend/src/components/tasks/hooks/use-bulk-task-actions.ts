"use client";

import { useCallback } from "react";
import type React from "react";

import { toast } from "sonner";

import { taskApi, type Task } from "@/lib/task-api";
import { isTaskCompletionTransition } from "@/lib/task-completion-undo";
import {
  hasEffectiveTaskOccurrence,
  updateEffectiveTaskOccurrenceStatus,
} from "@/lib/task-occurrence-status";
import { getTaskDisplayStatus } from "@/lib/tasks-page-utils";
import type { UndoEntry } from "@/components/tasks/hooks/use-task-undo";
import { removeTaskSubtrees } from "@/components/tasks/hooks/use-tasks-data";

/**
 * 一括操作（ステータス変更 / 削除 / コピー / 移動）と行単位ステータス変更をまとめたフック。
 */
export function useBulkTaskActions({
  tasks,
  setTasks,
  selectedIds,
  setSelectedIds,
  clearSelection,
  pushUndo,
  queueTaskCompletionUndo,
  applyTaskPatchLocally,
  upsertTaskLocally,
  setBulkLoading,
  setCutTaskIds,
  focusedTaskId,
  focusTaskById,
  filteredTasksRef,
  refreshTasks,
  requestRecurringDelete,
}: {
  tasks: Task[];
  setTasks: React.Dispatch<React.SetStateAction<Task[]>>;
  selectedIds: Set<string>;
  setSelectedIds: React.Dispatch<React.SetStateAction<Set<string>>>;
  clearSelection: () => void;
  pushUndo: (entry: UndoEntry) => void;
  queueTaskCompletionUndo: (entries: Task[]) => void;
  applyTaskPatchLocally: (taskId: string, patch: Partial<Task>) => void;
  upsertTaskLocally: (task: Task) => void;
  setBulkLoading: React.Dispatch<React.SetStateAction<boolean>>;
  setCutTaskIds: React.Dispatch<React.SetStateAction<Set<string>>>;
  focusedTaskId: string | null;
  focusTaskById: (taskId: string | null) => void;
  filteredTasksRef: React.RefObject<Task[]>;
  refreshTasks?: () => Promise<void>;
  requestRecurringDelete?: (task: Task) => boolean;
}) {
  const handleRowStatusChange = useCallback(
    async (task: Task, status: string) => {
      const currentStatus = getTaskDisplayStatus(task);
      if (status === currentStatus) return;
      if (hasEffectiveTaskOccurrence(task)) {
        try {
          await updateEffectiveTaskOccurrenceStatus({
            task,
            status,
            applyTaskPatchLocally,
            refreshTasks,
          });
        } catch (err) {
          console.error("繰り返し発生回のステータス更新失敗:", err);
        }
        return;
      }

      const willComplete = isTaskCompletionTransition(currentStatus, status);
      try {
        if (!willComplete) {
          pushUndo({
            type: "update",
            taskId: task.id,
            previous: {
              status: task.status,
              completed_at: task.completed_at ?? null,
            },
          });
        }
        applyTaskPatchLocally(task.id, { status });
        const updated = await taskApi.updateTask(task.id, { status });
        upsertTaskLocally(updated);
        if (willComplete) {
          queueTaskCompletionUndo([task]);
        }
      } catch (err) {
        applyTaskPatchLocally(task.id, {
          status: task.status,
          completed_at: task.completed_at ?? null,
        });
        console.error("ステータス更新失敗:", err);
      }
    },
    [
      applyTaskPatchLocally,
      pushUndo,
      queueTaskCompletionUndo,
      refreshTasks,
      upsertTaskLocally,
    ],
  );

  const handleBulkStatusChange = useCallback(
    async (status: string) => {
      if (selectedIds.size === 0) return;
      setBulkLoading(true);
      const targets = tasks
        .filter((task) => selectedIds.has(task.id))
        .filter((task) => getTaskDisplayStatus(task) !== status);
      const occurrenceTargets = targets.filter(hasEffectiveTaskOccurrence);
      const regularTargets = targets.filter(
        (task) => !hasEffectiveTaskOccurrence(task),
      );
      try {
        const willComplete = regularTargets.some((task) =>
          isTaskCompletionTransition(task.status, status),
        );
        if (!willComplete && regularTargets.length > 0) {
          pushUndo({
            type: "bulkUpdate",
            entries: regularTargets.map((t) => ({
              taskId: t.id,
              previous: {
                status: t.status,
                completed_at: t.completed_at ?? null,
              },
            })),
          });
        }
        for (const task of regularTargets) {
          applyTaskPatchLocally(task.id, { status });
        }
        const updatedTasks = await Promise.all(
          regularTargets.map((task) =>
            taskApi.updateTask(task.id, { status }),
          ),
        );
        for (const updated of updatedTasks) {
          upsertTaskLocally(updated);
        }
        for (const task of occurrenceTargets) {
          await updateEffectiveTaskOccurrenceStatus({
            task,
            status,
            applyTaskPatchLocally,
          });
        }
        if (occurrenceTargets.length > 0) {
          await refreshTasks?.();
        }
        clearSelection();
        if (willComplete) {
          queueTaskCompletionUndo(
            regularTargets.filter((task) =>
              isTaskCompletionTransition(task.status, status),
            ),
          );
        }
      } catch (err) {
        for (const task of regularTargets) {
          applyTaskPatchLocally(task.id, {
            status: task.status,
            completed_at: task.completed_at ?? null,
          });
        }
        await refreshTasks?.();
        console.error("一括ステータス更新失敗:", err);
      } finally {
        setBulkLoading(false);
      }
    },
    [
      selectedIds,
      tasks,
      applyTaskPatchLocally,
      clearSelection,
      pushUndo,
      queueTaskCompletionUndo,
      refreshTasks,
      setBulkLoading,
      upsertTaskLocally,
    ],
  );

  const handleBulkDelete = useCallback(async () => {
    if (selectedIds.size === 0) return;
    const targets = tasks.filter((t) => selectedIds.has(t.id));
    const recurringTargets = targets.filter((task) => task.has_recurrence);
    if (
      recurringTargets.length > 1 ||
      (recurringTargets.length === 1 && targets.length > 1)
    ) {
      toast.error("繰り返しタスクは個別に削除してください", {
        description:
          "今回だけ削除するか、今回以降を削除するかを選ぶ必要があります。",
      });
      return;
    }
    if (
      recurringTargets.length === 1 &&
      requestRecurringDelete?.(recurringTargets[0])
    ) {
      return;
    }
    setBulkLoading(true);
    try {
      pushUndo({ type: "recreate", tasks: targets });
      await Promise.all([...selectedIds].map((id) => taskApi.deleteTask(id)));
      setTasks((prev) => removeTaskSubtrees(prev, selectedIds));
      clearSelection();
    } catch (err) {
      console.error("一括削除失敗:", err);
    } finally {
      setBulkLoading(false);
    }
  }, [
    selectedIds,
    tasks,
    clearSelection,
    pushUndo,
    requestRecurringDelete,
    setBulkLoading,
    setTasks,
  ]);

  const handleBulkDuplicate = useCallback(async () => {
    if (selectedIds.size === 0) return;
    setBulkLoading(true);
    try {
      const selectedTasks = tasks.filter((t) => selectedIds.has(t.id));
      const createdTasks = await Promise.all(
        selectedTasks.map((t) =>
          taskApi.createTask({
            project_id: t.project_id,
            title: `コピー: ${t.title}`,
            description: t.description || "",
            status: t.status,
            priority: t.priority,
            ...(t.auto_close_on_due ? { auto_close_on_due: true } : {}),
            notifications_enabled: t.notifications_enabled,
            tag_ids: (t.tags || []).map((tag) => tag.id),
          }),
        ),
      );
      setTasks((prev) => [...createdTasks, ...prev]);
      clearSelection();
    } catch (err) {
      console.error("一括コピー失敗:", err);
    } finally {
      setBulkLoading(false);
    }
  }, [selectedIds, tasks, clearSelection, setBulkLoading, setTasks]);

  const handleBulkMove = useCallback(
    async (targetProjectId: string) => {
      if (selectedIds.size === 0) return;
      setBulkLoading(true);
      try {
        const targets = tasks.filter((t) => selectedIds.has(t.id));
        pushUndo({
          type: "bulkUpdate",
          entries: targets.map((t) => ({
            taskId: t.id,
            previous: { project_id: t.project_id },
          })),
        });
        for (const id of selectedIds) {
          applyTaskPatchLocally(id, { project_id: targetProjectId });
        }
        const updatedTasks = await Promise.all(
          [...selectedIds].map((id) =>
            taskApi.moveTask(id, { project_id: targetProjectId }),
          ),
        );
        for (const updated of updatedTasks) {
          upsertTaskLocally(updated);
        }
        clearSelection();
      } catch (err) {
        console.error("一括移動失敗:", err);
      } finally {
        setBulkLoading(false);
      }
    },
    [
      selectedIds,
      tasks,
      applyTaskPatchLocally,
      clearSelection,
      pushUndo,
      setBulkLoading,
      upsertTaskLocally,
    ],
  );

  const handleDeleteTasks = useCallback(
    async (taskList: Task[]) => {
      if (taskList.length === 0) return;
      const recurringTasks = taskList.filter((task) => task.has_recurrence);
      if (
        recurringTasks.length > 1 ||
        (recurringTasks.length === 1 && taskList.length > 1)
      ) {
        toast.error("繰り返しタスクは個別に削除してください", {
          description:
            "今回だけ削除するか、今回以降を削除するかを選ぶ必要があります。",
        });
        return;
      }
      if (
        recurringTasks.length === 1 &&
        requestRecurringDelete?.(recurringTasks[0])
      ) {
        return;
      }
      setBulkLoading(true);
      try {
        pushUndo({ type: "recreate", tasks: taskList });
        const deletingIds = new Set(taskList.map((task) => task.id));
        await Promise.all(taskList.map((task) => taskApi.deleteTask(task.id)));
        setSelectedIds((prev) => {
          const next = new Set(prev);
          for (const taskId of deletingIds) next.delete(taskId);
          return next;
        });
        setCutTaskIds((prev) => {
          const next = new Set(prev);
          for (const taskId of deletingIds) next.delete(taskId);
          return next;
        });
        setTasks((prev) =>
          prev.filter((task) => !deletingIds.has(task.id)),
        );
        if (focusedTaskId && deletingIds.has(focusedTaskId)) {
          const nextFocus =
            filteredTasksRef.current.find((task) => !deletingIds.has(task.id))
              ?.id ?? null;
          focusTaskById(nextFocus);
        }
      } catch (err) {
        console.error("タスク削除失敗:", err);
      } finally {
        setBulkLoading(false);
      }
    },
    [
      filteredTasksRef,
      focusTaskById,
      focusedTaskId,
      pushUndo,
      requestRecurringDelete,
      setBulkLoading,
      setCutTaskIds,
      setSelectedIds,
      setTasks,
    ],
  );

  return {
    handleRowStatusChange,
    handleBulkStatusChange,
    handleBulkDelete,
    handleBulkDuplicate,
    handleBulkMove,
    handleDeleteTasks,
  };
}
