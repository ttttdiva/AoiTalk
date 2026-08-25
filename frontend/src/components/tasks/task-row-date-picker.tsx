"use client";

import { useCallback, useRef, useState } from "react";

import { taskApi, type RecurrenceRule } from "@/lib/task-api";
import {
  TaskDatePicker,
  buildRrule,
  parseRrule,
  recurrenceEndDateInputValue,
} from "@/components/tasks/task-date-picker";
import { supportsSkipWeekend } from "@/lib/recurrence-rrule";
import {
  normalizeSkipMode,
  type RecurrenceSkipMode,
} from "@/lib/recurrence-preview";

interface TaskRowDatePickerProps {
  taskId: string;
  startAt: string | null;
  endAt: string | null;
  onRangeChange: (values: {
    startAt: string | null;
    endAt: string | null;
  }) => void;
  onRecurrenceChange?: (hasRecurrence: boolean) => void;
  allDay?: boolean;
  startPlaceholder?: string;
  endPlaceholder?: string;
  startButtonClassName?: string;
  endButtonClassName?: string;
  showStartDate?: boolean;
  showEndDate?: boolean;
}

export function TaskRowDatePicker({
  taskId,
  startAt,
  endAt,
  onRangeChange,
  onRecurrenceChange,
  allDay,
  startPlaceholder,
  endPlaceholder,
  startButtonClassName,
  endButtonClassName,
  showStartDate = true,
  showEndDate = true,
}: TaskRowDatePickerProps) {
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
  const loadedRef = useRef(false);

  const resetRecurrenceState = useCallback(() => {
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
  }, []);

  const applyRule = useCallback((rule: RecurrenceRule | null) => {
    setRecurrenceRule(rule);
    if (rule) {
      const parsed = parseRrule(rule.rrule);
      setRecFreq(parsed.freq);
      setRecInterval(parsed.interval);
      setRecByDay(parsed.byDay);
      setRecEndCount(
        rule.end_count !== undefined ? (rule.end_count ?? null) : parsed.count,
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
  }, []);

  const handleOpenChange = useCallback(
    async (open: boolean) => {
      if (!open || loadedRef.current) return;
      loadedRef.current = true;
      try {
        const rule = await taskApi.getRecurrence(taskId);
        applyRule(rule);
      } catch (err) {
        console.error("繰り返し設定取得失敗:", err);
      }
    },
    [taskId, applyRule],
  );

  const toggleWeekday = useCallback((dayKey: string) => {
    setRecByDay((prev) =>
      prev.includes(dayKey)
        ? prev.filter((d) => d !== dayKey)
        : [...prev, dayKey],
    );
  }, []);

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

  const handleSaveRecurrence = useCallback(async () => {
    setRecurrenceSaving(true);
    try {
      const rrule = buildRrule(
        recFreq,
        recInterval,
        recByDay,
        recRecurForever ? null : recEndCount,
        recRecurForever ? null : recEndDate,
      );
      const rule = await taskApi.saveRecurrence(taskId, {
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
      setRecurrenceRule(rule);
      onRecurrenceChange?.(true);
    } catch (err) {
      console.error("繰り返し設定の保存に失敗:", err);
    } finally {
      setRecurrenceSaving(false);
    }
  }, [
    taskId,
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
    onRecurrenceChange,
  ]);

  const handleDeleteRecurrence = useCallback(async () => {
    setRecurrenceSaving(true);
    try {
      await taskApi.deleteRecurrence(taskId);
      resetRecurrenceState();
      onRecurrenceChange?.(false);
    } catch (err) {
      console.error("繰り返し設定の削除に失敗:", err);
    } finally {
      setRecurrenceSaving(false);
    }
  }, [taskId, resetRecurrenceState, onRecurrenceChange]);

  return (
    <TaskDatePicker
      startAt={startAt}
      endAt={endAt}
      onStartAtChange={() => {}}
      onEndAtChange={() => {}}
      onRangeChange={onRangeChange}
      deferCommitUntilClose
      allDay={allDay}
      startPlaceholder={startPlaceholder}
      endPlaceholder={endPlaceholder}
      startButtonClassName={startButtonClassName}
      endButtonClassName={endButtonClassName}
      showStartDate={showStartDate}
      showEndDate={showEndDate}
      onOpenChange={handleOpenChange}
      recurrence={{
        recurrenceRule,
        freq: recFreq,
        interval: recInterval,
        byDay: recByDay,
        triggerStatus: recTriggerStatus,
        createNew: recCreateNew,
        recurForever: recRecurForever,
        resetStatusTo: recResetStatusTo,
        endCount: recEndCount,
        endDate: recEndDate,
        skipWeekend: recSkipWeekend,
        skipHoliday: recSkipHoliday,
        skipMode: recSkipMode,
        saving: recurrenceSaving,
        onFreqChange: handleFreqChange,
        onIntervalChange: setRecInterval,
        onToggleWeekday: toggleWeekday,
        onTriggerStatusChange: setRecTriggerStatus,
        onCreateNewChange: setRecCreateNew,
        onRecurForeverChange: setRecRecurForever,
        onResetStatusToChange: setRecResetStatusTo,
        onEndCountChange: setRecEndCount,
        onEndDateChange: setRecEndDate,
        onSkipWeekendChange: setRecSkipWeekend,
        onSkipHolidayChange: setRecSkipHoliday,
        onSkipModeChange: setRecSkipMode,
        onSave: handleSaveRecurrence,
        onDelete: handleDeleteRecurrence,
      }}
    />
  );
}
