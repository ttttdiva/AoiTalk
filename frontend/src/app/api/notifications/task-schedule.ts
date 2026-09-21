export function isStaleNonRecurringTaskSchedule(input: {
  occurrenceId: string | null | undefined;
  occurrenceSourceKind: string | null | undefined;
  recurrenceRuleTaskId: string | null | undefined;
}): boolean {
  // ``task_schedule`` rows are legacy mirrors for non-recurring tasks. The
  // task row is their canonical anchor; retaining the mirror would expose an
  // old date after a task edit and can replay a stale reminder.
  return (
    !!input.occurrenceId &&
    input.occurrenceSourceKind === "task_schedule" &&
    !input.recurrenceRuleTaskId
  );
}
