"use client";

import { useCallback, useEffect, useState } from "react";
import type React from "react";

import { toast } from "sonner";

import { taskApi, type Task } from "@/lib/task-api";
import { isTaskCompletionTransition } from "@/lib/task-completion-undo";
import { cn } from "@/lib/utils";
import { useContextMenuPosition } from "@/hooks/use-context-menu-position";
import type { FetchDataOptions } from "@/components/tasks/hooks/use-tasks-data";
import type { UndoEntry } from "@/components/tasks/hooks/use-task-undo";

/**
 * タスク一覧の右クリックコンテキストメニュー状態と各操作をまとめたフック。
 */
export function useTaskContextMenu({
  fetchData,
  pushUndo,
  queueTaskCompletionUndo,
  applyTaskPatchLocally,
  upsertTaskLocally,
  removeTaskLocally,
  setSelectedIds,
  requestRecurringDelete,
}: {
  fetchData: (options?: FetchDataOptions) => Promise<void>;
  pushUndo: (entry: UndoEntry) => void;
  queueTaskCompletionUndo: (entries: Task[]) => void;
  applyTaskPatchLocally: (taskId: string, patch: Partial<Task>) => void;
  upsertTaskLocally: (task: Task) => void;
  removeTaskLocally: (taskId: string) => void;
  setSelectedIds: React.Dispatch<React.SetStateAction<Set<string>>>;
  requestRecurringDelete?: (task: Task) => boolean;
}) {
  const [contextMenu, setContextMenu] = useState<{
    x: number;
    y: number;
    task: Task;
  } | null>(null);
  const [statusSubmenuOpen, setStatusSubmenuOpen] = useState(false);
  const [prioritySubmenuOpen, setPrioritySubmenuOpen] = useState(false);
  const {
    ref: contextMenuRef,
    style: contextMenuStyle,
    submenuSide: contextSubmenuSide,
  } = useContextMenuPosition(
    contextMenu ? { x: contextMenu.x, y: contextMenu.y } : null,
    { fallbackWidth: 200, fallbackHeight: 300, submenuWidth: 160 },
  );
  const contextSubmenuClassName = cn(
    "absolute top-0 min-w-36 rounded-md border bg-popover p-1 text-popover-foreground shadow-md",
    contextSubmenuSide === "left" ? "right-full mr-1" : "left-full ml-1",
  );

  const handleContextMenu = useCallback((e: React.MouseEvent, task: Task) => {
    e.preventDefault();
    e.stopPropagation();
    setContextMenu({ x: e.clientX, y: e.clientY, task });
    setStatusSubmenuOpen(false);
  }, []);

  const closeContextMenu = useCallback(() => {
    setContextMenu(null);
    setStatusSubmenuOpen(false);
    setPrioritySubmenuOpen(false);
  }, []);

  useEffect(() => {
    if (!contextMenu) return;
    const handleClick = (e: MouseEvent) => {
      if (
        contextMenuRef.current &&
        !contextMenuRef.current.contains(e.target as Node)
      ) {
        closeContextMenu();
      }
    };
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") closeContextMenu();
    };
    document.addEventListener("mousedown", handleClick);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("mousedown", handleClick);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [contextMenu, closeContextMenu, contextMenuRef]);

  const handleContextStatusChange = useCallback(
    async (status: string) => {
      if (!contextMenu) return;
      const task = contextMenu.task;
      closeContextMenu();
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
      contextMenu,
      applyTaskPatchLocally,
      closeContextMenu,
      fetchData,
      pushUndo,
      queueTaskCompletionUndo,
      upsertTaskLocally,
    ],
  );

  const handleContextPriorityChange = useCallback(
    async (priority: string) => {
      if (!contextMenu) return;
      const task = contextMenu.task;
      closeContextMenu();
      try {
        pushUndo({
          type: "update",
          taskId: task.id,
          previous: { priority: task.priority },
        });
        applyTaskPatchLocally(task.id, { priority });
        const updated = await taskApi.updateTask(task.id, { priority });
        upsertTaskLocally(updated);
        await fetchData();
      } catch (err) {
        console.error("優先度更新失敗:", err);
      }
    },
    [
      contextMenu,
      applyTaskPatchLocally,
      closeContextMenu,
      fetchData,
      pushUndo,
      upsertTaskLocally,
    ],
  );

  const handleContextTimer = useCallback(async () => {
    if (!contextMenu) return;
    const task = contextMenu.task;
    closeContextMenu();
    try {
      if (task.active_time_entry) {
        await taskApi.stopTimer(task.active_time_entry.id);
      } else {
        await taskApi.startTimer(task.id);
      }
      await fetchData();
    } catch (err) {
      console.error("タイマー操作失敗:", err);
    }
  }, [contextMenu, closeContextMenu, fetchData]);

  const handleDuplicate = useCallback(async () => {
    if (!contextMenu) return;
    const task = contextMenu.task;
    closeContextMenu();
    try {
      const created = await taskApi.createTask({
        project_id: task.project_id,
        title: `コピー: ${task.title}`,
        description: task.description || "",
        status: task.status,
        priority: task.priority,
        notifications_enabled: task.notifications_enabled,
        tag_ids: (task.tags || []).map((t) => t.id),
      });
      upsertTaskLocally(created);
      await fetchData();
    } catch (err) {
      console.error("タスク複製失敗:", err);
    }
  }, [contextMenu, closeContextMenu, fetchData, upsertTaskLocally]);

  const handleCopyTaskId = useCallback(async () => {
    if (!contextMenu) return;
    const taskId = contextMenu.task.id;
    closeContextMenu();
    try {
      await navigator.clipboard.writeText(taskId);
      toast.success("タスクIDをコピーしました", {
        description: `@${taskId} でタスク候補を検索できます`,
      });
    } catch (err) {
      console.error("タスクIDコピー失敗:", err);
      toast.error("タスクIDのコピーに失敗しました");
    }
  }, [contextMenu, closeContextMenu]);

  const handleContextDelete = useCallback(async () => {
    if (!contextMenu) return;
    const task = contextMenu.task;
    closeContextMenu();
    if (requestRecurringDelete?.(task)) return;
    try {
      pushUndo({ type: "recreate", tasks: [task] });
      await taskApi.deleteTask(task.id);
      removeTaskLocally(task.id);
      // 削除したタスクのIDを選択状態からも除去
      setSelectedIds((prev) => {
        const next = new Set(prev);
        next.delete(task.id);
        return next;
      });
      await fetchData();
    } catch (err) {
      console.error("タスク削除失敗:", err);
    }
  }, [
    contextMenu,
    closeContextMenu,
    fetchData,
    pushUndo,
    removeTaskLocally,
    requestRecurringDelete,
    setSelectedIds,
  ]);

  return {
    contextMenu,
    contextMenuRef,
    contextMenuStyle,
    contextSubmenuClassName,
    statusSubmenuOpen,
    setStatusSubmenuOpen,
    prioritySubmenuOpen,
    setPrioritySubmenuOpen,
    handleContextMenu,
    handleContextStatusChange,
    handleContextPriorityChange,
    handleContextTimer,
    handleDuplicate,
    handleCopyTaskId,
    handleContextDelete,
  };
}
