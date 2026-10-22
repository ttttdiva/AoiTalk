"use client";

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

import { taskApi, type Task } from "@/lib/task-api";
import { applyTaskTimerStart } from "@/lib/tasks-page-utils";
import { getElapsedTimerSeconds } from "@/lib/task-time";

export type TaskTimerOccurrence = {
  id?: string | null;
  startAt?: string | null;
  endAt?: string | null;
  originalStartAt?: string | null;
  allDay?: boolean;
};

/**
 * タスク詳細モーダルのタイマー処理をまとめた hook。
 * 経過時間表示・タイマー操作・タイマー変更イベント購読・Alt+S ショートカットを所有する。
 * 挙動は元の TaskDetailModal と完全一致させている。
 */
export function useTaskTimer({
  task,
  effectiveTaskId,
  open,
  fetchTask,
  onTaskUpdated,
  setTask,
  occurrence,
  onTimerStatusApplied,
  onTimerStatusFailed,
}: {
  task: Task | null;
  effectiveTaskId: string | null;
  open: boolean;
  fetchTask: () => Promise<void>;
  onTaskUpdated: () => void;
  setTask: React.Dispatch<React.SetStateAction<Task | null>>;
  occurrence?: TaskTimerOccurrence | null;
  onTimerStatusApplied?: () => void;
  onTimerStatusFailed?: () => void;
}) {
  const [timerLoading, setTimerLoading] = useState(false);
  const [elapsedSeconds, setElapsedSeconds] = useState(0);

  // Task A -> B and close/reopen of A are different owners, even when the
  // task id eventually matches again. Invalidate completions on unmount too.
  const lifecycleRef = useRef<symbol | null>(null);
  const operationRef = useRef<symbol | null>(null);
  useLayoutEffect(() => {
    lifecycleRef.current = Symbol("task-timer");
    operationRef.current = null;
    setTimerLoading(false);
    return () => {
      lifecycleRef.current = null;
      operationRef.current = null;
    };
  }, [effectiveTaskId, open]);

  // タイマー表示更新
  useEffect(() => {
    if (!task?.active_time_entry?.started_at) {
      setElapsedSeconds(0);
      return;
    }
    const updateElapsed = () => {
      setElapsedSeconds(
        getElapsedTimerSeconds(task.active_time_entry?.started_at),
      );
    };
    updateElapsed();
    const interval = setInterval(updateElapsed, 1000);
    return () => clearInterval(interval);
  }, [task?.active_time_entry?.started_at]);

  // タイマー操作
  const handleTimer = useCallback(async () => {
    const lifecycle = lifecycleRef.current;
    if (
      !open || !effectiveTaskId || task?.id !== effectiveTaskId ||
      !lifecycle || operationRef.current
    ) return;
    const operation = Symbol("timer-operation");
    operationRef.current = operation;
    const isCurrent = () =>
      lifecycleRef.current === lifecycle && operationRef.current === operation;
    setTimerLoading(true);
    try {
      if (task?.active_time_entry) {
        await taskApi.stopTimer(task.active_time_entry.id);
        if (isCurrent()) {
          setElapsedSeconds(0);
          setTask((prev) =>
            lifecycleRef.current === lifecycle && prev?.id === effectiveTaskId
              ? { ...prev, active_time_entry: null }
              : prev,
          );
        }
        window.dispatchEvent(
          new CustomEvent("timer-changed", isCurrent()
            ? { detail: { activeEntry: null } }
            : undefined),
        );
      } else {
        const previousStatus = task?.status;
        const previousOccurrenceStatus = task?.effective_occurrence_status;
        const started = await taskApi.startTimer(
          effectiveTaskId,
          occurrence?.id || undefined,
        );
        if (isCurrent()) {
          onTimerStatusApplied?.();
          setElapsedSeconds(0);
          setTask((prev) =>
            lifecycleRef.current === lifecycle && prev?.id === effectiveTaskId
              ? applyTaskTimerStart(prev, started)
              : prev,
          );
        }
        try {
          if (occurrence?.startAt) {
            await taskApi.updateOccurrenceStatus(effectiveTaskId, {
              occurrence_id: occurrence.id ?? null,
              occurrence_start_at: occurrence.startAt,
              occurrence_end_at: occurrence.endAt ?? null,
              original_start_at:
                occurrence.originalStartAt ?? occurrence.startAt,
              status: "in_progress",
              all_day: occurrence.allDay,
            });
          }
        } catch (err) {
          console.error("タイマー開始後の発生回ステータス更新失敗:", err);
          if (isCurrent()) {
            onTimerStatusFailed?.();
            setTask((prev) =>
              lifecycleRef.current === lifecycle && prev?.id === effectiveTaskId
                ? {
                    ...prev,
                    status: previousStatus ?? prev.status,
                    effective_occurrence_status: previousOccurrenceStatus,
                  }
                : prev,
            );
          }
        }
        window.dispatchEvent(
          new CustomEvent(
            "timer-changed",
            isCurrent()
              ? { detail: { activeEntry: started } }
              : undefined,
          ),
        );
      }
      // A stale operation emits no activeEntry: the header must refetch the
      // current timer instead of adopting an obsolete start/stop response.
      // The old detail callback must never run.
      if (isCurrent()) await fetchTask();
      onTaskUpdated();
    } catch (err) {
      console.error("タイマー操作失敗:", err);
    } finally {
      if (isCurrent()) {
        operationRef.current = null;
        setTimerLoading(false);
      }
    }
  }, [
    effectiveTaskId,
    fetchTask,
    occurrence,
    onTaskUpdated,
    onTimerStatusApplied,
    onTimerStatusFailed,
    open,
    setTask,
    task,
  ]);

  // ヘッダー等でタイマーが変わったらタスク情報を再取得
  useEffect(() => {
    if (!open || !effectiveTaskId) return;
    const onTimerChanged = () => {
      fetchTask();
    };
    window.addEventListener("timer-changed", onTimerChanged);
    return () => window.removeEventListener("timer-changed", onTimerChanged);
  }, [effectiveTaskId, fetchTask, open]);

  // Alt+S でタイマー開始/停止
  useEffect(() => {
    if (!open) return;
    const handleKeydown = (e: KeyboardEvent) => {
      if (
        e.altKey &&
        !e.ctrlKey &&
        !e.metaKey &&
        !e.shiftKey &&
        (e.key === "s" || e.key === "S")
      ) {
        e.preventDefault();
        handleTimer();
      }
    };
    window.addEventListener("keydown", handleKeydown);
    return () => window.removeEventListener("keydown", handleKeydown);
  }, [open, handleTimer]);

  return {
    elapsedSeconds,
    timerLoading,
    handleTimer,
  };
}
