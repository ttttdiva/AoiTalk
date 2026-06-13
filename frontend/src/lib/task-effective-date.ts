import type { Task } from "@/lib/task-api";

function isDueOnlyRecurringTask(task: Task): boolean {
  if (!task.end_at) return false;
  return !task.start_at;
}

function isGeneratedDueOnlyRecurringTask(task: Task): boolean {
  if (
    !task.has_recurrence ||
    !isDueOnlyRecurringTask(task) ||
    !task.effective_occurrence_start_at
  ) {
    return false;
  }

  const sourceKind = task.effective_occurrence_source_kind ?? "task_schedule";
  return sourceKind === "task_schedule" || sourceKind === "rrule";
}

export function getTaskDisplayStartAt(
  task: Task,
): string | null | undefined {
  if (isGeneratedDueOnlyRecurringTask(task)) return null;
  return task.effective_start_at ?? task.start_at;
}

export function getTaskDisplayEndAt(task: Task): string | null | undefined {
  if (isGeneratedDueOnlyRecurringTask(task) && !task.start_at) {
    return (
      task.effective_end_at ??
      task.effective_occurrence_start_at ??
      task.end_at
    );
  }
  return task.effective_end_at ?? task.end_at;
}

export function getTaskDisplayAllDay(task: Task): boolean {
  return task.effective_all_day ?? task.all_day;
}
