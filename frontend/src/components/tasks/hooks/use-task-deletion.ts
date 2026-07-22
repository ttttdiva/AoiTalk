"use client";

import { useCallback, useState } from "react";

import {
  taskApi,
  type RecurringOccurrenceContext,
  type Task,
} from "@/lib/task-api";
import { normalizeTaskTitle } from "@/components/tasks/task-form-utils";

/**
 * タスク詳細モーダルの削除・複製処理をまとめた hook。
 * 繰り返しタスクの削除確認ダイアログ表示状態も所有する。
 * 挙動は元の TaskDetailModal と完全一致させている。
 */
export function useTaskDeletion({
  effectiveTaskId,
  task,
  activeOccurrenceContext,
  editTitle,
  editDescription,
  onTaskUpdated,
  onOpenChange,
}: {
  effectiveTaskId: string | null;
  task: Task | null;
  activeOccurrenceContext: RecurringOccurrenceContext | null;
  editTitle: string;
  editDescription: string;
  onTaskUpdated: () => void;
  onOpenChange: (open: boolean) => void;
}) {
  const [showRecurringDeletePrompt, setShowRecurringDeletePrompt] =
    useState(false);

  const handleDelete = useCallback(async () => {
    if (!effectiveTaskId) return;
    if (task?.has_recurrence && activeOccurrenceContext?.start_at) {
      setShowRecurringDeletePrompt(true);
      return;
    }
    try {
      await taskApi.deleteTask(effectiveTaskId);
      onTaskUpdated();
      onOpenChange(false);
    } catch (err) {
      console.error("削除失敗:", err);
    }
  }, [
    activeOccurrenceContext,
    effectiveTaskId,
    onOpenChange,
    onTaskUpdated,
    task,
  ]);

  const handleDuplicate = useCallback(async () => {
    if (!task) return;
    try {
      await taskApi.createTask({
        project_id: task.project_id,
        title: `コピー: ${normalizeTaskTitle(editTitle || task.title) || task.title}`,
        description: editDescription.trim() || task.description || "",
        status: task.status,
        priority: task.priority,
        start_at: task.start_at ?? null,
        end_at: task.end_at ?? null,
        all_day: task.all_day,
        notifications_enabled: task.notifications_enabled,
        reminder_offsets: task.reminder_offsets || [],
        parent_task_id: task.parent_task_id ?? null,
        tag_ids: (task.tags || []).map((tag) => tag.id),
      });
      onTaskUpdated();
    } catch (err) {
      console.error("隍・｣ｽ螟ｱ謨・", err);
    }
  }, [editDescription, editTitle, onTaskUpdated, task]);

  const handleDeleteSingleOccurrence = useCallback(async () => {
    if (!effectiveTaskId || !activeOccurrenceContext?.start_at) return;
    try {
      await taskApi.deleteOccurrence(effectiveTaskId, {
        mode: "single",
        occurrence_id: activeOccurrenceContext.occurrence_id ?? null,
        occurrence_start_at: activeOccurrenceContext.start_at,
        occurrence_end_at: activeOccurrenceContext.end_at ?? null,
        original_start_at: activeOccurrenceContext.original_start_at ?? null,
      });
      setShowRecurringDeletePrompt(false);
      onTaskUpdated();
      onOpenChange(false);
    } catch (err) {
      console.error("今回分の削除失敗:", err);
    }
  }, [activeOccurrenceContext, effectiveTaskId, onOpenChange, onTaskUpdated]);

  const handleDeleteFutureOccurrences = useCallback(async () => {
    if (!effectiveTaskId || !activeOccurrenceContext?.start_at) return;
    try {
      await taskApi.deleteOccurrence(effectiveTaskId, {
        mode: "future",
        occurrence_id: activeOccurrenceContext.occurrence_id ?? null,
        occurrence_start_at: activeOccurrenceContext.start_at,
        occurrence_end_at: activeOccurrenceContext.end_at ?? null,
        original_start_at: activeOccurrenceContext.original_start_at ?? null,
      });
      setShowRecurringDeletePrompt(false);
      onTaskUpdated();
      onOpenChange(false);
    } catch (err) {
      console.error("今後分の削除失敗:", err);
    }
  }, [activeOccurrenceContext, effectiveTaskId, onOpenChange, onTaskUpdated]);

  return {
    showRecurringDeletePrompt,
    setShowRecurringDeletePrompt,
    handleDelete,
    handleDuplicate,
    handleDeleteSingleOccurrence,
    handleDeleteFutureOccurrences,
  };
}
