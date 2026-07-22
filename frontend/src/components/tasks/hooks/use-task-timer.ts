"use client";

import { useCallback, useEffect, useState } from "react";

import { taskApi, type Task } from "@/lib/task-api";
import { getElapsedTimerSeconds } from "@/lib/task-time";

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
}: {
  task: Task | null;
  effectiveTaskId: string | null;
  open: boolean;
  fetchTask: () => Promise<void>;
  onTaskUpdated: () => void;
  setTask: React.Dispatch<React.SetStateAction<Task | null>>;
}) {
  const [timerLoading, setTimerLoading] = useState(false);
  const [elapsedSeconds, setElapsedSeconds] = useState(0);

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
    if (!effectiveTaskId) return;
    setTimerLoading(true);
    try {
      if (task?.active_time_entry) {
        await taskApi.stopTimer(task.active_time_entry.id);
        setElapsedSeconds(0);
        setTask((prev) => (prev ? { ...prev, active_time_entry: null } : prev));
        window.dispatchEvent(
          new CustomEvent("timer-changed", {
            detail: { activeEntry: null },
          }),
        );
      } else {
        const started = await taskApi.startTimer(effectiveTaskId);
        setElapsedSeconds(0);
        setTask((prev) =>
          prev ? { ...prev, active_time_entry: started } : prev,
        );
        window.dispatchEvent(
          new CustomEvent("timer-changed", {
            detail: { activeEntry: started },
          }),
        );
      }
      await fetchTask();
      onTaskUpdated();
    } catch (err) {
      console.error("タイマー操作失敗:", err);
    } finally {
      setTimerLoading(false);
    }
  }, [effectiveTaskId, fetchTask, onTaskUpdated, task, setTask]);

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
      if (e.altKey && (e.key === "s" || e.key === "S")) {
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
