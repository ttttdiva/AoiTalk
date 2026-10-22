import { and, eq, inArray, isNull, notInArray, sql } from "drizzle-orm";

import { projects, taskOccurrences, tasks, timeEntries } from "@/db/schema";

const CLOSED_TASK_STORAGE_STATUSES = ["closed", "done"];

export function todayTaskListCondition() {
  return and(
    isNull(tasks.archivedAt),
    notInArray(tasks.status, CLOSED_TASK_STORAGE_STATUSES),
    sql`exists (
      select 1
      from ${projects}
      where ${projects.id} = ${tasks.projectId}
        and ${projects.deletedAt} is null
        and ${projects.isCompleted} = false
    )`,
  );
}

export function currentUserLiveTimerCondition(
  taskIds: string[],
  userId: string,
) {
  return and(
    inArray(timeEntries.taskId, taskIds),
    eq(timeEntries.userId, userId),
    isNull(timeEntries.deletedAt),
    isNull(timeEntries.endedAt),
  );
}

export function todayOccurrenceCondition(
  projectIds: string[],
  options: { stored: boolean },
) {
  return and(
    inArray(tasks.projectId, projectIds),
    isNull(tasks.deletedAt),
    isNull(tasks.archivedAt),
    notInArray(tasks.status, CLOSED_TASK_STORAGE_STATUSES),
    options.stored ? isNull(taskOccurrences.deletedAt) : undefined,
    isNull(projects.deletedAt),
    eq(projects.isCompleted, false),
  );
}
