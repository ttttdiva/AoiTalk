import { db } from "@/db";
import {
  notificationDeliveries,
  taskActivities,
  taskAssignees,
  taskAttachments,
  taskReferences,
  taskComments,
  taskDependencies,
  taskOccurrences,
  taskRecurrenceRules,
  tasks,
  taskTags,
  timeEntries,
  knowledgeNodeSupertags,
  knowledgeSupertags,
} from "@/db/schema";
import { and, eq, inArray, isNotNull, or } from "drizzle-orm";

export async function collectTaskTreeIds(rootTaskId: string): Promise<string[]> {
  const taskIds = [rootTaskId];
  const seen = new Set(taskIds);
  let queue = [rootTaskId];

  while (queue.length > 0) {
    const rows = await db
      .select({ id: tasks.id })
      .from(tasks)
      .where(inArray(tasks.parentTaskId, queue));
    const childIds = rows
      .map((row) => row.id)
      .filter((id) => !seen.has(id));
    if (childIds.length === 0) break;

    for (const id of childIds) seen.add(id);
    taskIds.push(...childIds);
    queue = childIds;
  }

  return taskIds;
}

export async function deleteTaskTreeRows(taskIds: string[]) {
  if (taskIds.length === 0) return [];

  const linkedNodeRows = await db
    .select({ nodeId: tasks.knowledgeNodeId })
    .from(tasks)
    .where(and(inArray(tasks.id, taskIds), isNotNull(tasks.knowledgeNodeId)));
  const linkedNodeIds = linkedNodeRows
    .map((row) => row.nodeId)
    .filter((nodeId): nodeId is string => typeof nodeId === "string");
  if (linkedNodeIds.length > 0) {
    const taskSupertagRows = await db
      .select({ id: knowledgeSupertags.id })
      .from(knowledgeSupertags)
      .where(eq(knowledgeSupertags.systemKey, "task"));
    const taskSupertagIds = taskSupertagRows.map((row) => row.id);
    if (taskSupertagIds.length > 0) {
      await db
        .delete(knowledgeNodeSupertags)
        .where(
          and(
            inArray(knowledgeNodeSupertags.nodeId, linkedNodeIds),
            inArray(knowledgeNodeSupertags.supertagId, taskSupertagIds),
          ),
        );
    }
  }

  await db
    .delete(notificationDeliveries)
    .where(inArray(notificationDeliveries.taskId, taskIds));
  await db.delete(timeEntries).where(inArray(timeEntries.taskId, taskIds));
  await db
    .delete(taskOccurrences)
    .where(inArray(taskOccurrences.taskId, taskIds));
  await db.delete(taskDependencies).where(
    or(
      inArray(taskDependencies.taskId, taskIds),
      inArray(taskDependencies.dependsOnTaskId, taskIds),
    ),
  );
  await db.delete(taskActivities).where(inArray(taskActivities.taskId, taskIds));
  await db
    .delete(taskRecurrenceRules)
    .where(inArray(taskRecurrenceRules.taskId, taskIds));
  await db.delete(taskComments).where(inArray(taskComments.taskId, taskIds));
  await db
    .delete(taskAttachments)
    .where(inArray(taskAttachments.taskId, taskIds));
  await db
    .delete(taskReferences)
    .where(inArray(taskReferences.taskId, taskIds));
  await db.delete(taskTags).where(inArray(taskTags.taskId, taskIds));
  await db.delete(taskAssignees).where(inArray(taskAssignees.taskId, taskIds));
  return db.delete(tasks).where(inArray(tasks.id, taskIds)).returning({
    id: tasks.id,
  });
}
