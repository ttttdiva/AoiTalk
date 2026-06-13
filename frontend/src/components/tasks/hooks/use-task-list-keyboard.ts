"use client";

import { useEffect } from "react";
import type React from "react";

import type { RecurringOccurrenceContext, Task } from "@/lib/task-api";
import {
  isEditableTarget,
  type ClipboardMode,
  type TaskClipboard,
} from "@/lib/tasks-page-utils";

/**
 * タスク一覧のグローバルキーボードショートカット
 * （検索 / タブ切替 / 行移動 / 選択 / コピー&ペースト / 削除 等）をまとめたフック。
 */
export function useTaskListKeyboard({
  tasks,
  filteredTasks,
  focusedTaskId,
  focusTaskById,
  selectedTaskId,
  draftTask,
  selectedIds,
  clearSelection,
  cycleProjectTab,
  openTaskCommandDialog,
  openTask,
  openTaskById,
  handleFocusedTaskTimerStart,
  handleCheckboxClick,
  getKeyboardSelectionTasks,
  handleDeleteTasks,
  handleClipboardStore,
  handleClipboardPaste,
  clipboardRef,
  searchInputRef,
}: {
  tasks: Task[];
  filteredTasks: Task[];
  focusedTaskId: string | null;
  focusTaskById: (taskId: string | null) => void;
  selectedTaskId: string | null;
  draftTask: Partial<Task> | null;
  selectedIds: Set<string>;
  clearSelection: () => void;
  cycleProjectTab: (direction: 1 | -1) => void;
  openTaskCommandDialog: (taskId: string) => void;
  openTask: (task: Task) => void;
  openTaskById: (
    taskId: string,
    occurrenceContext?: RecurringOccurrenceContext | null,
  ) => void;
  handleFocusedTaskTimerStart: () => Promise<void>;
  handleCheckboxClick: (
    taskId: string,
    index: number,
    shiftKey: boolean,
  ) => void;
  getKeyboardSelectionTasks: () => Task[];
  handleDeleteTasks: (taskList: Task[]) => Promise<void>;
  handleClipboardStore: (mode: ClipboardMode) => void;
  handleClipboardPaste: () => Promise<void>;
  clipboardRef: React.RefObject<TaskClipboard>;
  searchInputRef: React.RefObject<HTMLInputElement | null>;
}) {
  useEffect(() => {
    const focusFirstRow = () => {
      if (selectedTaskId || draftTask) return;
      if (filteredTasks.length === 0) return;
      focusTaskById(filteredTasks[0].id);
    };

    const handleKeyDown = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      const inDialog =
        !!selectedTaskId || !!draftTask || !!target?.closest('[role="dialog"]');

      if (
        (e.ctrlKey || e.metaKey) &&
        !e.altKey &&
        !e.shiftKey &&
        e.key.toLowerCase() === "f" &&
        !inDialog
      ) {
        e.preventDefault();
        searchInputRef.current?.focus();
        searchInputRef.current?.select();
        return;
      }

      if (
        (e.ctrlKey || e.metaKey) &&
        !e.altKey &&
        e.shiftKey &&
        (e.key === "ArrowLeft" || e.key === "ArrowRight") &&
        !inDialog
      ) {
        e.preventDefault();
        cycleProjectTab(e.key === "ArrowRight" ? 1 : -1);
        return;
      }

      if (
        !inDialog &&
        !e.ctrlKey &&
        !e.metaKey &&
        !e.altKey &&
        e.key === "/" &&
        focusedTaskId
      ) {
        e.preventDefault();
        openTaskCommandDialog(focusedTaskId);
        return;
      }

      if (inDialog || isEditableTarget(target)) return;

      if (e.key === "Escape" && selectedIds.size > 0) {
        e.preventDefault();
        clearSelection();
        return;
      }

      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        if (filteredTasks.length === 0) return;
        e.preventDefault();
        const currentIndex = focusedTaskId
          ? filteredTasks.findIndex((task) => task.id === focusedTaskId)
          : -1;
        const nextIndex =
          e.key === "ArrowDown"
            ? Math.min(
                filteredTasks.length - 1,
                currentIndex < 0 ? 0 : currentIndex + 1,
              )
            : currentIndex < 0
              ? filteredTasks.length - 1
              : Math.max(0, currentIndex - 1);
        focusTaskById(filteredTasks[nextIndex]?.id ?? null);
        return;
      }

      if (
        e.altKey &&
        !e.ctrlKey &&
        !e.metaKey &&
        !e.shiftKey &&
        e.key.toLowerCase() === "s" &&
        focusedTaskId
      ) {
        e.preventDefault();
        void handleFocusedTaskTimerStart();
        return;
      }

      if (e.key === "Enter" && focusedTaskId) {
        e.preventDefault();
        const focusedTask = tasks.find((task) => task.id === focusedTaskId);
        if (focusedTask) {
          openTask(focusedTask);
        } else {
          openTaskById(focusedTaskId);
        }
        return;
      }

      if (
        (e.ctrlKey || e.metaKey) &&
        !e.altKey &&
        !e.shiftKey &&
        e.code === "Space" &&
        focusedTaskId
      ) {
        e.preventDefault();
        const focusedIndex = filteredTasks.findIndex(
          (task) => task.id === focusedTaskId,
        );
        if (focusedIndex >= 0) {
          handleCheckboxClick(focusedTaskId, focusedIndex, false);
        }
        return;
      }

      if (e.key === "Delete") {
        const targetTasks = getKeyboardSelectionTasks();
        if (targetTasks.length === 0) return;
        e.preventDefault();
        void handleDeleteTasks(targetTasks);
        return;
      }

      if ((e.ctrlKey || e.metaKey) && e.key === "c") {
        e.preventDefault();
        handleClipboardStore("copy");
        return;
      }

      if ((e.ctrlKey || e.metaKey) && e.key === "x") {
        e.preventDefault();
        handleClipboardStore("cut");
        return;
      }

      if (
        (e.ctrlKey || e.metaKey) &&
        e.key === "v" &&
        clipboardRef.current.tasks.length > 0
      ) {
        e.preventDefault();
        void handleClipboardPaste();
      }
    };

    window.addEventListener("tasks-focus-first-row", focusFirstRow);
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("tasks-focus-first-row", focusFirstRow);
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [
    clearSelection,
    clipboardRef,
    cycleProjectTab,
    draftTask,
    filteredTasks,
    focusedTaskId,
    focusTaskById,
    getKeyboardSelectionTasks,
    handleCheckboxClick,
    handleClipboardPaste,
    handleClipboardStore,
    handleDeleteTasks,
    handleFocusedTaskTimerStart,
    openTaskCommandDialog,
    openTask,
    openTaskById,
    searchInputRef,
    selectedIds,
    selectedTaskId,
    tasks,
  ]);
}
