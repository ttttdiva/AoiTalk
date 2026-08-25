const CLOSED_TASK_NOTIFICATION_STATUSES = new Set([
  "closed",
  "done",
  "cancelled",
  "canceled",
]);

const UUID_PATTERN =
  "[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}";
const LEGACY_PYTHON_IN_APP_REMINDER_KEY = new RegExp(
  `^reminder:${UUID_PATTERN}:offset:\\d+:user:${UUID_PATTERN}$`,
  "i",
);

export interface TaskNotificationState {
  taskStatus?: unknown;
  occurrenceStatus?: unknown;
  taskArchivedAt?: unknown;
  taskDeletedAt?: unknown;
  occurrenceDeletedAt?: unknown;
  sourceKind?: unknown;
}

function isClosedStatus(status: unknown): boolean {
  return CLOSED_TASK_NOTIFICATION_STATUSES.has(
    String(status ?? "").toLowerCase(),
  );
}

export function isTaskNotificationSuppressed(
  state: TaskNotificationState,
): boolean {
  return Boolean(
    state.taskArchivedAt ||
      state.taskDeletedAt ||
      state.occurrenceDeletedAt ||
      state.sourceKind === "recurrence_skip" ||
      isClosedStatus(state.taskStatus) ||
      isClosedStatus(state.occurrenceStatus),
  );
}

export function isLegacyPythonInAppReminderDedupeKey(
  dedupeKey: string,
): boolean {
  return LEGACY_PYTHON_IN_APP_REMINDER_KEY.test(dedupeKey);
}
