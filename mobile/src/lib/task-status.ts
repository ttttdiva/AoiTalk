export const CLOSED_TASK_STATUS = "closed";

export function normalizeTaskStatus(status: unknown): string {
  const normalized = String(status ?? "")
    .trim()
    .toLowerCase();
  if (!normalized) return normalized;
  if (normalized === "done") return CLOSED_TASK_STATUS;
  return normalized;
}

export function isClosedTaskStatus(status: unknown): boolean {
  return normalizeTaskStatus(status) === CLOSED_TASK_STATUS;
}
