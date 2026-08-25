"use client";

import { useEffect, useRef, useState } from "react";
import type React from "react";

import type { RecurringOccurrenceContext, Task } from "@/lib/task-api";
import {
  isEditableTarget,
  type ClipboardMode,
  type TaskClipboard,
} from "@/lib/tasks-page-utils";

type TaskExpansionState = {
  hasSubtasks: boolean;
  expanded: boolean;
};

/**
 * タスク一覧のグローバルキーボードショートカット
 * （検索 / タブ切替 / 行移動 / 選択 / コピー&ペースト / 削除 等）をまとめたフック。
 */
export function useTaskListKeyboard({
  tasks,
  filteredTasks,
  keyboardTasks,
  rangeTasks,
  getTaskExpansionState,
  setTaskExpanded,
  handleKeyboardRangeSelection,
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
  readOnly = false,
}: {
  tasks: Task[];
  filteredTasks: Task[];
  /**
   * キーボードで移動できる表示行。通常の filteredTasks は親タスクだけを
   * 収めているため、展開中のサブタスクを含める場合に別配列を渡す。
   */
  keyboardTasks?: Task[];
  /**
   * Shift+矢印/Home/End の範囲操作で対象にする、書き込み可能な
   * トップレベルタスクの表示順。未指定時は従来どおり filteredTasks を使う。
   */
  rangeTasks?: Task[];
  /** フォーカス中の親タスクのサブタスク展開状態を返す。 */
  getTaskExpansionState?: (
    taskId: string,
  ) => TaskExpansionState | null | undefined;
  /** 親タスクのサブタスク展開状態を明示的に更新する。 */
  setTaskExpanded?: (taskId: string, expanded: boolean) => void;
  /** 一時的なキーボード範囲を一括選択へ確定する。 */
  handleKeyboardRangeSelection?: (taskIds: string[]) => void;
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
  readOnly?: boolean;
}) {
  const focusableTasks = keyboardTasks ?? filteredTasks;
  const selectableRangeTasks = rangeTasks ?? filteredTasks;
  const [rangeFocusedIds, setRangeFocusedIds] = useState<Set<string>>(
    () => new Set(),
  );
  // The anchor deliberately lives outside render state.  Shift+navigation
  // keeps it fixed while the transient range is extended or contracted.
  const rangeAnchorIdRef = useRef<string | null>(null);
  const rangeTaskIdsRef = useRef<string[] | null>(null);

  // Filter changes, permission changes, and task mutations can leave a
  // transient range containing IDs that are no longer writable/visible. Keep
  // the returned state honest and discard an anchor that left the domain.
  useEffect(() => {
    const currentIds = selectableRangeTasks.map((task) => task.id);
    const previousIds = rangeTaskIdsRef.current;
    const domainOrderChanged =
      previousIds !== null &&
      (previousIds.length !== currentIds.length ||
        previousIds.some((taskId, index) => taskId !== currentIds[index]));
    rangeTaskIdsRef.current = currentIds;

    const validIds = new Set(currentIds);
    setRangeFocusedIds((previous) => {
      const hasInvalidMember = [...previous].some(
        (taskId) => !validIds.has(taskId),
      );
      const hasInvalidAnchor =
        rangeAnchorIdRef.current !== null &&
        !validIds.has(rangeAnchorIdRef.current);
      const hasActiveRange =
        previous.size > 0 || rangeAnchorIdRef.current !== null;
      // A filtered/read-only task invalidates the whole transient range;
      // retaining a partial range would make its anchor ambiguous.  A domain
      // reorder/insert can likewise leave a non-contiguous visual range even
      // when every old ID is still present, so discard the whole session.
      if (
        hasActiveRange &&
        (domainOrderChanged || hasInvalidMember || hasInvalidAnchor)
      ) {
        rangeAnchorIdRef.current = null;
        return new Set();
      }
      if (hasInvalidAnchor) rangeAnchorIdRef.current = null;
      return previous;
    });
  }, [selectableRangeTasks]);

  useEffect(() => {
    const focusFirstRow = () => {
      if (selectedTaskId || draftTask) return;
      if (focusableTasks.length === 0) return;
      focusTaskById(focusableTasks[0].id);
    };

    const clearTransientRange = () => {
      rangeAnchorIdRef.current = null;
      setRangeFocusedIds((previous) =>
        previous.size === 0 ? previous : new Set(),
      );
    };

    const getVisibleIndex = (taskId: string | null) =>
      taskId ? focusableTasks.findIndex((task) => task.id === taskId) : -1;

    const focusVisibleByDirection = (direction: -1 | 1) => {
      if (focusableTasks.length === 0) return false;
      const currentIndex = getVisibleIndex(focusedTaskId);
      const nextIndex = Math.max(
        0,
        Math.min(
          focusableTasks.length - 1,
          currentIndex < 0
            ? direction > 0
              ? 0
              : focusableTasks.length - 1
            : currentIndex + direction,
        ),
      );
      focusTaskById(focusableTasks[nextIndex]?.id ?? null);
      return true;
    };

    const focusVisibleEdge = (edge: "first" | "last") => {
      if (focusableTasks.length === 0) return false;
      focusTaskById(
        focusableTasks[edge === "first" ? 0 : focusableTasks.length - 1]?.id ??
          null,
      );
      return true;
    };

    const updateKeyboardRange = (
      targetIndex: number,
      anchorTaskId: string,
    ) => {
      if (selectableRangeTasks.length === 0) return false;

      const boundedTargetIndex = Math.max(
        0,
        Math.min(selectableRangeTasks.length - 1, targetIndex),
      );
      const anchorIndex = selectableRangeTasks.findIndex(
        (task) => task.id === anchorTaskId,
      );
      if (anchorIndex < 0) {
        // A stale anchor can occur while the filter/domain is changing. Do
        // not synthesize a new anchor or silently select an unrelated task.
        rangeAnchorIdRef.current = null;
        setRangeFocusedIds(new Set());
        return false;
      }

      const start = Math.min(anchorIndex, boundedTargetIndex);
      const end = Math.max(anchorIndex, boundedTargetIndex);
      const next = new Set(
        selectableRangeTasks.slice(start, end + 1).map((task) => task.id),
      );
      setRangeFocusedIds(next);
      return true;
    };

    const handleShiftNavigation = (
      key: "ArrowDown" | "ArrowUp" | "Home" | "End",
    ) => {
      if (selectableRangeTasks.length === 0) {
        // There is no range domain. Shift navigation remains ordinary visible
        // navigation and must never manufacture a range.
        if (key === "Home") return focusVisibleEdge("first");
        if (key === "End") return focusVisibleEdge("last");
        return focusVisibleByDirection(key === "ArrowUp" ? -1 : 1);
      }

      const currentRangeIndex = focusedTaskId
        ? selectableRangeTasks.findIndex((task) => task.id === focusedTaskId)
        : -1;
      const currentIsInRangeDomain = currentRangeIndex >= 0;
      if (!currentIsInRangeDomain) {
        // A subtask or read-only row is outside the range domain. Preserve
        // any existing anchor and perform only visible-row navigation.
        if (key === "Home") return focusVisibleEdge("first");
        if (key === "End") return focusVisibleEdge("last");
        return focusVisibleByDirection(key === "ArrowUp" ? -1 : 1);
      }

      const targetIndex =
        key === "Home"
          ? 0
          : key === "End"
            ? selectableRangeTasks.length - 1
            : Math.max(
                0,
                Math.min(
                  selectableRangeTasks.length - 1,
                  currentRangeIndex + (key === "ArrowUp" ? -1 : 1),
                ),
              );
      const anchorTaskId = rangeAnchorIdRef.current ?? focusedTaskId;
      if (!anchorTaskId) {
        return true;
      }
      if (!rangeAnchorIdRef.current) {
        rangeAnchorIdRef.current = anchorTaskId;
      }
      if (!updateKeyboardRange(targetIndex, anchorTaskId)) return false;
      focusTaskById(selectableRangeTasks[targetIndex]?.id ?? null);
      return true;
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
        if (readOnly) return;
        e.preventDefault();
        openTaskCommandDialog(focusedTaskId);
        return;
      }

      if (inDialog || isEditableTarget(target)) return;

      if (e.key === "Escape") {
        const hasTransientRange =
          rangeFocusedIds.size > 0 || rangeAnchorIdRef.current !== null;
        if (hasTransientRange) {
          e.preventDefault();
          clearTransientRange();
          if (selectedIds.size > 0) {
            clearSelection();
          }
          return;
        }
        if (selectedIds.size > 0) {
          e.preventDefault();
          clearSelection();
          return;
        }
      }

      const isPlainNavigation =
        !e.ctrlKey &&
        !e.metaKey &&
        !e.altKey &&
        !e.shiftKey;
      const isShiftNavigation =
        e.shiftKey && !e.ctrlKey && !e.metaKey && !e.altKey;

      if (
        isShiftNavigation &&
        (e.key === "ArrowDown" ||
          e.key === "ArrowUp" ||
          e.key === "Home" ||
          e.key === "End")
      ) {
        e.preventDefault();
        handleShiftNavigation(e.key);
        return;
      }

      if (
        isPlainNavigation &&
        (e.key === "ArrowLeft" || e.key === "ArrowRight")
      ) {
        if (
          focusedTaskId &&
          getTaskExpansionState &&
          setTaskExpanded
        ) {
          const expansion = getTaskExpansionState(focusedTaskId);
          if (expansion?.hasSubtasks) {
            e.preventDefault();
            const shouldExpand = e.key === "ArrowRight";
            if (expansion.expanded !== shouldExpand) {
              setTaskExpanded(focusedTaskId, shouldExpand);
            }
            return;
          }
        }
      }

      if (isPlainNavigation && (e.key === "Home" || e.key === "End")) {
        e.preventDefault();
        clearTransientRange();
        focusVisibleEdge(e.key === "Home" ? "first" : "last");
        return;
      }

      if (isPlainNavigation && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
        clearTransientRange();
        if (focusableTasks.length === 0) return;
        e.preventDefault();
        focusVisibleByDirection(e.key === "ArrowDown" ? 1 : -1);
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
        if (rangeFocusedIds.size > 0) {
          const ids = selectableRangeTasks
            .filter((rangeTask) => rangeFocusedIds.has(rangeTask.id))
            .map((rangeTask) => rangeTask.id);
          if (ids.length > 0 && handleKeyboardRangeSelection) {
            handleKeyboardRangeSelection(ids);
          }
          return;
        }
        const focusedIndex = filteredTasks.findIndex(
          (task) => task.id === focusedTaskId,
        );
        if (focusedIndex >= 0) {
          handleCheckboxClick(focusedTaskId, focusedIndex, false);
        }
        return;
      }

      if (e.key === "Delete") {
        if (readOnly) return;
        const targetTasks = getKeyboardSelectionTasks();
        if (targetTasks.length === 0) return;
        e.preventDefault();
        void handleDeleteTasks(targetTasks);
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
        if (readOnly) return;
        e.preventDefault();
        void handleFocusedTaskTimerStart();
        return;
      }

      if ((e.ctrlKey || e.metaKey) && e.key === "c") {
        e.preventDefault();
        handleClipboardStore("copy");
        return;
      }

      if ((e.ctrlKey || e.metaKey) && e.key === "x") {
        if (readOnly) return;
        e.preventDefault();
        handleClipboardStore("cut");
        return;
      }

      if (
        (e.ctrlKey || e.metaKey) &&
        e.key === "v" &&
        clipboardRef.current.tasks.length > 0
      ) {
        if (readOnly) return;
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
    focusableTasks,
    focusedTaskId,
    focusTaskById,
    getKeyboardSelectionTasks,
    getTaskExpansionState,
    handleCheckboxClick,
    handleClipboardPaste,
    handleClipboardStore,
    handleDeleteTasks,
    handleFocusedTaskTimerStart,
    handleKeyboardRangeSelection,
    openTaskCommandDialog,
    openTask,
    openTaskById,
    rangeFocusedIds,
    readOnly,
    searchInputRef,
    selectedIds,
    selectedTaskId,
    selectableRangeTasks,
    setTaskExpanded,
    tasks,
  ]);

  return { rangeFocusedIds };
}
