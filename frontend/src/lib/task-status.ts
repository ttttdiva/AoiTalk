export const CLOSED_TASK_STATUS = "closed";

export type TaskStatusOption =
  | "open"
  | "in_progress"
  | "on_hold"
  | "review"
  | "closed";

export const TASK_STATUS_OPTIONS: TaskStatusOption[] = [
  "open",
  "in_progress",
  "on_hold",
  "review",
  "closed",
];

export const TASK_STATUS_LABELS: Record<string, string> = {
  todo: "未着手",
  open: "未着手",
  in_progress: "進行中",
  on_hold: "保留",
  review: "確認待ち",
  done: "完了",
  closed: "完了",
};

export const TASK_STATUS_DOT_COLORS: Record<string, string> = {
  todo: "bg-gray-400",
  open: "bg-gray-400",
  in_progress: "bg-red-500",
  on_hold: "bg-pink-400",
  review: "bg-sky-400",
  done: "bg-green-500",
  closed: "bg-green-500",
};

export const TASK_STATUS_KEY_HINTS: Record<string, string> = {
  done: "C",
  closed: "C",
  in_progress: "S",
  review: "R",
  on_hold: "H",
  open: "O",
};

export const TASK_STATUS_SHORTCUT_KEYS: Record<string, TaskStatusOption> = {
  c: "closed",
  s: "in_progress",
  r: "review",
  h: "on_hold",
  o: "open",
};

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

export function normalizeTask<T extends { status: string }>(task: T): T {
  return {
    ...task,
    status: normalizeTaskStatus(task.status),
  };
}

export function normalizeRecurrenceRule<
  T extends { trigger_status: string; reset_status_to: string },
>(rule: T): T {
  return {
    ...rule,
    trigger_status: normalizeTaskStatus(rule.trigger_status),
    reset_status_to: normalizeTaskStatus(rule.reset_status_to),
  };
}
