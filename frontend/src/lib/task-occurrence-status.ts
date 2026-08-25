import { getTaskDisplayAllDay } from "@/lib/task-effective-date";
import { taskApi, type Task } from "@/lib/task-api";

export function hasEffectiveTaskOccurrence(task: Task): boolean {
  return Boolean(task.has_recurrence && task.effective_occurrence_start_at);
}

export async function updateEffectiveTaskOccurrenceStatus({
  task,
  status,
  applyTaskPatchLocally,
  refreshTasks,
}: {
  task: Task;
  status: string;
  applyTaskPatchLocally: (taskId: string, patch: Partial<Task>) => void;
  refreshTasks?: () => Promise<void>;
}): Promise<void> {
  if (!task.effective_occurrence_start_at) {
    throw new Error("繰り返し発生回の開始日時がありません");
  }
  const previousStatus = task.effective_occurrence_status ?? task.status;
  applyTaskPatchLocally(task.id, { effective_occurrence_status: status });
  try {
    const result = await taskApi.updateOccurrenceStatus(task.id, {
      occurrence_id: task.effective_occurrence_id ?? null,
      occurrence_start_at: task.effective_occurrence_start_at,
      occurrence_end_at: task.effective_occurrence_end_at ?? null,
      original_start_at:
        task.effective_occurrence_original_start_at ??
        task.effective_occurrence_start_at,
      status,
      all_day: getTaskDisplayAllDay(task),
    });
    applyTaskPatchLocally(task.id, {
      effective_occurrence_status: String(
        result.occurrence?.status ?? status,
      ),
    });
    await refreshTasks?.();
  } catch (err) {
    applyTaskPatchLocally(task.id, {
      effective_occurrence_status: previousStatus,
    });
    throw err;
  }
}
