"use client";

import { useCallback } from "react";
import type React from "react";

import { toast } from "sonner";

import { taskApi, type Task } from "@/lib/task-api";
import { isTaskCompletionTransition } from "@/lib/task-completion-undo";
import type { FetchDataOptions } from "@/components/tasks/hooks/use-tasks-data";
import type { UndoEntry } from "@/components/tasks/hooks/use-task-undo";

/**
 * 一括操作（ステータス変更 / 削除 / コピー / 移動）と行単位ステータス変更をまとめたフック。
 */
export function useBulkTaskActions({
  tasks,
  setTasks,
  selectedIds,
  setSelectedIds,
  clearSelection,
  fetchData,
  pushUndo,
  queueTaskCompletionUndo,
  applyTaskPatchLocally,
  upsertTaskLocally,
  setBulkLoading,
  setCutTaskIds,
  focusedTaskId,
  focusTaskById,
  filteredTasksRef,
  requestRecurringDelete,
}: {
  tasks: Task[];
  setTasks: React.Dispatch<React.SetStateAction<Task[]>>;
  selectedIds: Set<string>;
  setSelectedIds: React.Dispatch<React.SetStateAction<Set<string>>>;
  clearSelection: () => void;
  fetchData: (options?: FetchDataOptions) => Promise<void>;
  pushUndo: (entry: UndoEntry) => void;
  queueTaskCompletionUndo: (entries: Task[]) => void;
  applyTaskPatchLocally: (taskId: string, patch: Partial<Task>) => void;
  upsertTaskLocally: (task: Task) => void;
  setBulkLoading: React.Dispatch<React.SetStateAction<boolean>>;
  setCutTaskIds: React.Dispatch<React.SetStateAction<Set<string>>>;
  focusedTaskId: string | null;
  focusTaskById: (taskId: string | null) => void;
  filteredTasksRef: React.RefObject<Task[]>;
  requestRecurringDelete?: (task: Task) => boolean;
}) {
  const handleRowStatusChange = useCallback(
    async (task: Task, status: string) => {
      if (status === task.status) return;
      const willComplete = isTaskCompletionTransition(task.status, status);
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
        await fetchData();
        if (willComplete) {
          queueTaskCompletionUndo([task]);
        }
      } catch (err) {
        console.error("ステータス更新失敗:", err);
      }
    },
    [
      applyTaskPatchLocally,
      fetchData,
      pushUndo,
      queueTaskCompletionUndo,
      upsertTaskLocally,
    ],
  );

  const handleBulkStatusChange = useCallback(
    async (status: string) => {
      if (selectedIds.size === 0) return;
      setBulkLoading(true);
      try {
        const targets = tasks.filter((t) => selectedIds.has(t.id));
        const willComplete = targets.some((task) =>
          isTaskCompletionTransition(task.status, status),
        );
        if (!willComplete) {
          pushUndo({
            type: "bulkUpdate",
            entries: targets.map((t) => ({
              taskId: t.id,
              previous: {
                status: t.status,
                completed_at: t.completed_at ?? null,
              },
            })),
          });
        }
        for (const id of selectedIds) {
          applyTaskPatchLocally(id, { status });
        }
        const updatedTasks = await Promise.all(
          [...selectedIds].map((id) => taskApi.updateTask(id, { status })),
        );
        for (const updated of updatedTasks) {
          upsertTaskLocally(updated);
        }
        clearSelection();
        await fetchData();
        if (willComplete) {
          queueTaskCompletionUndo(
            targets.filter((task) =>
              isTaskCompletionTransition(task.status, status),
            ),
          );
        }
      } catch (err) {
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
      fetchData,
      pushUndo,
      queueTaskCompletionUndo,
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
      setTasks((prev) => prev.filter((task) => !selectedIds.has(task.id)));
      clearSelection();
      await fetchData();
    } catch (err) {
      console.error("一括削除失敗:", err);
    } finally {
      setBulkLoading(false);
    }
  }, [
    selectedIds,
    tasks,
    clearSelection,
    fetchData,
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
            notifications_enabled: t.notifications_enabled,
            tag_ids: (t.tags || []).map((tag) => tag.id),
          }),
        ),
      );
      setTasks((prev) => [...createdTasks, ...prev]);
      clearSelection();
      await fetchData();
    } catch (err) {
      console.error("一括コピー失敗:", err);
    } finally {
      setBulkLoading(false);
    }
  }, [selectedIds, tasks, clearSelection, fetchData, setBulkLoading, setTasks]);

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
        await fetchData();
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
      fetchData,
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
        await fetchData();
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
      fetchData,
      filteredTasksRef,
      focusTaskById,
      focusedTaskId,
      pushUndo,
      requestRecurringDelete,
      setBulkLoading,
      setCutTaskIds,
      setSelectedIds,
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
