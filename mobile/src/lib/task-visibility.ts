import type { Task } from "../types/api";

export const COMPLETED_TASK_STATUSES = new Set(["closed", "cancelled"]);

export function isFutureTask(task: Pick<Task, "start_at">): boolean {
  if (!task.start_at) return false;

  const start = new Date(task.start_at);
  if (Number.isNaN(start.getTime())) return false;

  const tomorrow = new Date();
  tomorrow.setHours(0, 0, 0, 0);
  tomorrow.setDate(tomorrow.getDate() + 1);

  const taskDay = new Date(
    start.getFullYear(),
    start.getMonth(),
    start.getDate(),
  );

  return taskDay >= tomorrow;
}
