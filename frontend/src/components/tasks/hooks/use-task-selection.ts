"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type React from "react";

import type { Task } from "@/lib/task-api";

/**
 * タスク一覧の一括選択（チェックボックス / Shift+クリック範囲選択）をまとめたフック。
 */
export function useTaskSelection({
  filteredTasksRef,
}: {
  filteredTasksRef: React.RefObject<Task[]>;
}) {
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const selectedIdsRef = useRef<Set<string>>(new Set());
  const lastClickedIndexRef = useRef<number | null>(null);
  const prevShiftRangeRef = useRef<Set<string>>(new Set());

  useEffect(() => {
    selectedIdsRef.current = selectedIds;
  }, [selectedIds]);

  const toggleSelect = useCallback((taskId: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(taskId)) next.delete(taskId);
      else next.add(taskId);
      return next;
    });
  }, []);

  // Shift+クリックで範囲選択
  const handleCheckboxClick = useCallback(
    (taskId: string, index: number, shiftKey: boolean) => {
      if (shiftKey && lastClickedIndexRef.current !== null) {
        setSelectedIds((prev) => {
          const next = new Set(prev);
          for (const id of prevShiftRangeRef.current) {
            next.delete(id);
          }
          const newRange = new Set<string>();
          const start = Math.min(lastClickedIndexRef.current!, index);
          const end = Math.max(lastClickedIndexRef.current!, index);
          for (let i = start; i <= end; i++) {
            if (filteredTasksRef.current[i]) {
              const id = filteredTasksRef.current[i].id;
              next.add(id);
              newRange.add(id);
            }
          }
          prevShiftRangeRef.current = newRange;
          return next;
        });
        // Shift+クリック時はアンカーを更新しない
      } else {
        toggleSelect(taskId);
        prevShiftRangeRef.current = new Set();
        lastClickedIndexRef.current = index;
      }
    },
    [filteredTasksRef, toggleSelect],
  );

  const clearSelection = useCallback(() => setSelectedIds(new Set()), []);

  const toggleSelectAll = useCallback(() => {
    setSelectedIds((prev) =>
      prev.size === filteredTasksRef.current.length
        ? new Set()
        : new Set(filteredTasksRef.current.map((t) => t.id)),
    );
  }, [filteredTasksRef]);

  return {
    selectedIds,
    setSelectedIds,
    selectedIdsRef,
    lastClickedIndexRef,
    prevShiftRangeRef,
    toggleSelect,
    handleCheckboxClick,
    clearSelection,
    toggleSelectAll,
  };
}
