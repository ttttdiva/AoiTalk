"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { taskApi, type RecurrenceRule, type Task } from "@/lib/task-api";
import {
  buildRrule,
  parseRrule,
  recurrenceEndDateInputValue,
} from "@/components/tasks/task-date-picker";
import { supportsSkipWeekend } from "@/lib/recurrence-rrule";
import {
  normalizeSkipMode,
  type RecurrenceSkipMode,
} from "@/lib/recurrence-preview";

/**
 * タスク詳細モーダルの繰り返し設定 state とロジックをまとめた hook。
 * fetch とリセットは関数として提供し、呼び出しタイミング（effect）は呼び出し側に委ねる。
 * 挙動は元の TaskDetailModal と完全一致させている。
 */
export function useTaskRecurrence({
  effectiveTaskId,
  onTaskUpdated,
  setTask,
  ensureTaskId,
  lifecycleGeneration = 0,
}: {
  effectiveTaskId: string | null;
  onTaskUpdated: () => void;
  setTask: React.Dispatch<React.SetStateAction<Task | null>>;
  ensureTaskId: () => Promise<string | null>;
  lifecycleGeneration?: number;
}) {
  const [recurrenceRule, setRecurrenceRule] = useState<RecurrenceRule | null>(
    null,
  );
  const [recFreq, setRecFreq] = useState("WEEKLY");
  const [recInterval, setRecInterval] = useState(1);
  const [recByDay, setRecByDay] = useState<string[]>([]);
  const [recTriggerStatus, setRecTriggerStatus] = useState("closed");
  const [recCreateNew, setRecCreateNew] = useState(false);
  const [recRecurForever, setRecRecurForever] = useState(true);
  const [recResetStatusTo, setRecResetStatusTo] = useState("open");
  const [recEndCount, setRecEndCount] = useState<number | null>(null);
  const [recEndDate, setRecEndDate] = useState<string | null>(null);
  const [recSkipWeekend, setRecSkipWeekend] = useState(false);
  const [recSkipHoliday, setRecSkipHoliday] = useState(false);
  const [recSkipMode, setRecSkipMode] =
    useState<RecurrenceSkipMode>("shift_forward");
  const [recurrenceSaving, setRecurrenceSaving] = useState(false);
  const requestScopeRef = useRef({
    taskId: effectiveTaskId,
    lifecycleGeneration,
    generation: 0,
  });
  if (
    requestScopeRef.current.taskId !== effectiveTaskId ||
    requestScopeRef.current.lifecycleGeneration !== lifecycleGeneration
  ) {
    requestScopeRef.current = {
      taskId: effectiveTaskId,
      lifecycleGeneration,
      generation: requestScopeRef.current.generation + 1,
    };
  }

  const isCurrentScope = useCallback(
    (scope: {
      taskId: string | null;
      lifecycleGeneration: number;
      generation: number;
    }) =>
      requestScopeRef.current.taskId === scope.taskId &&
      requestScopeRef.current.lifecycleGeneration ===
        scope.lifecycleGeneration &&
      requestScopeRef.current.generation === scope.generation,
    [],
  );

  const isCurrentSaveTarget = useCallback(
    (
      scope: {
        taskId: string | null;
        lifecycleGeneration: number;
        generation: number;
      },
      targetTaskId: string | null,
    ) => {
      const current = requestScopeRef.current;
      if (current.lifecycleGeneration !== scope.lifecycleGeneration) {
        return false;
      }
      if (scope.taskId === null) {
        return current.taskId === null || current.taskId === targetTaskId;
      }
      return isCurrentScope(scope);
    },
    [isCurrentScope],
  );

  useEffect(() => {
    setRecurrenceSaving(false);
  }, [effectiveTaskId]);

  // 繰り返し設定を初期値へ戻す（open 時のリセット用）。
  const resetRecurrenceState = useCallback(() => {
    requestScopeRef.current = {
      ...requestScopeRef.current,
      generation: requestScopeRef.current.generation + 1,
    };
    setRecurrenceRule(null);
    setRecFreq("WEEKLY");
    setRecInterval(1);
    setRecByDay([]);
    setRecTriggerStatus("closed");
    setRecCreateNew(false);
    setRecRecurForever(true);
    setRecResetStatusTo("open");
    setRecEndCount(null);
    setRecEndDate(null);
    setRecSkipWeekend(false);
    setRecSkipHoliday(false);
    setRecSkipMode("shift_forward");
    setRecurrenceSaving(false);
  }, []);

  // 繰り返し設定取得
  const fetchRecurrence = useCallback(async () => {
    if (!effectiveTaskId) return;
    const requestScope = requestScopeRef.current;
    const requestedTaskId = effectiveTaskId;
    try {
      const rule = await taskApi.getRecurrence(requestedTaskId);
      if (!isCurrentScope(requestScope)) return;
      setRecurrenceRule(rule);
      if (rule) {
        const parsed = parseRrule(rule.rrule);
        setRecFreq(parsed.freq);
        setRecInterval(parsed.interval);
        setRecByDay(parsed.byDay);
        setRecEndCount(
          rule.end_count !== undefined
            ? (rule.end_count ?? null)
            : parsed.count,
        );
        setRecEndDate(
          rule.end_date !== undefined
            ? recurrenceEndDateInputValue(rule.end_date)
            : parsed.until,
        );
        setRecTriggerStatus(rule.trigger_status || "closed");
        setRecCreateNew(rule.create_new ?? false);
        setRecRecurForever(rule.recur_forever ?? true);
        setRecResetStatusTo(rule.reset_status_to || "open");
        setRecSkipWeekend(rule.skip_weekend ?? false);
        setRecSkipHoliday(rule.skip_holiday ?? false);
        setRecSkipMode(normalizeSkipMode(rule.skip_mode));
      }
    } catch (err) {
      if (isCurrentScope(requestScope)) {
        console.error("繰り返し設定取得失敗:", err);
      }
    }
  }, [effectiveTaskId, isCurrentScope]);

  // 曜日トグル
  const toggleWeekday = useCallback((dayKey: string) => {
    setRecByDay((prev) =>
      prev.includes(dayKey)
        ? prev.filter((d) => d !== dayKey)
        : [...prev, dayKey],
    );
  }, []);

  // 頻度変更（DAILYへ切替時はスキップ・新規作成の既定値を適用）
  const handleFreqChange = useCallback((newFreq: string) => {
    setRecFreq((prev) => {
      if (newFreq === "DAILY" && prev !== "DAILY") {
        setRecSkipWeekend(true);
        setRecSkipHoliday(true);
        setRecCreateNew(true);
      }
      return newFreq;
    });
  }, []);

  // 繰り返し設定の保存
  const handleSaveRecurrence = useCallback(async () => {
    const requestScope = requestScopeRef.current;
    let targetTaskId: string | null = effectiveTaskId;
    setRecurrenceSaving(true);
    try {
      targetTaskId = effectiveTaskId ?? (await ensureTaskId());
      if (!targetTaskId) return;
      if (!isCurrentSaveTarget(requestScope, targetTaskId)) return;

      const rrule = buildRrule(
        recFreq,
        recInterval,
        recByDay,
        recRecurForever ? null : recEndCount,
        recRecurForever ? null : recEndDate,
      );
      const rule = await taskApi.saveRecurrence(targetTaskId, {
        rrule,
        trigger_status: recTriggerStatus,
        create_new: recCreateNew,
        recur_forever: recRecurForever,
        reset_status_to: recResetStatusTo,
        end_count: recRecurForever ? null : recEndCount,
        end_date: recRecurForever ? null : recEndDate,
        skip_weekend: supportsSkipWeekend(recFreq, recByDay)
          ? recSkipWeekend
          : false,
        skip_holiday: recSkipHoliday,
        skip_mode: recSkipMode,
      });
      if (!isCurrentSaveTarget(requestScope, targetTaskId)) return;
      setRecurrenceRule(rule);
      setTask((prev) => (prev ? { ...prev, has_recurrence: true } : prev));
      onTaskUpdated();
    } catch (err) {
      if (isCurrentSaveTarget(requestScope, targetTaskId)) {
        console.error("繰り返し設定の保存に失敗:", err);
      }
    } finally {
      if (isCurrentSaveTarget(requestScope, targetTaskId)) {
        setRecurrenceSaving(false);
      }
    }
  }, [
    recFreq,
    recInterval,
    recByDay,
    recTriggerStatus,
    recCreateNew,
    recRecurForever,
    recResetStatusTo,
    recEndCount,
    recEndDate,
    recSkipWeekend,
    recSkipHoliday,
    recSkipMode,
    effectiveTaskId,
    ensureTaskId,
    isCurrentSaveTarget,
    onTaskUpdated,
    setTask,
  ]);

  // 繰り返し設定の削除
  const handleDeleteRecurrence = useCallback(async () => {
    if (!effectiveTaskId) return;
    const requestScope = requestScopeRef.current;
    const requestedTaskId = effectiveTaskId;
    setRecurrenceSaving(true);
    try {
      await taskApi.deleteRecurrence(requestedTaskId);
      if (!isCurrentScope(requestScope)) return;
      setRecurrenceRule(null);
      setTask((prev) => (prev ? { ...prev, has_recurrence: false } : prev));

      // リセット
      setRecFreq("WEEKLY");
      setRecInterval(1);
      setRecByDay([]);
      setRecTriggerStatus("closed");
      setRecCreateNew(false);
      setRecRecurForever(true);
      setRecResetStatusTo("open");
      setRecEndCount(null);
      setRecEndDate(null);
      setRecSkipWeekend(false);
      setRecSkipHoliday(false);
      setRecSkipMode("shift_forward");
      onTaskUpdated();
    } catch (err) {
      if (isCurrentScope(requestScope)) {
        console.error("繰り返し設定の削除に失敗:", err);
      }
    } finally {
      if (isCurrentScope(requestScope)) {
        setRecurrenceSaving(false);
      }
    }
  }, [effectiveTaskId, isCurrentScope, onTaskUpdated, setTask]);

  return {
    recurrenceRule,
    setRecurrenceRule,
    recFreq,
    recInterval,
    recByDay,
    recTriggerStatus,
    recCreateNew,
    recRecurForever,
    recResetStatusTo,
    recEndCount,
    recEndDate,
    recSkipWeekend,
    recSkipHoliday,
    recSkipMode,
    recurrenceSaving,
    setRecInterval,
    setRecTriggerStatus,
    setRecCreateNew,
    setRecRecurForever,
    setRecResetStatusTo,
    setRecEndCount,
    setRecEndDate,
    setRecSkipWeekend,
    setRecSkipHoliday,
    setRecSkipMode,
    resetRecurrenceState,
    fetchRecurrence,
    toggleWeekday,
    handleFreqChange,
    handleSaveRecurrence,
    handleDeleteRecurrence,
  };
}
