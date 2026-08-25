export const TASK_RELATION_REFERENCE_PREFIX = "task-relation:";

export function canonicalizeTaskRelationIds(
  firstTaskId: string,
  secondTaskId: string,
): [string, string] | null {
  const normalizedFirstTaskId = firstTaskId.toLowerCase();
  const normalizedSecondTaskId = secondTaskId.toLowerCase();
  if (normalizedFirstTaskId === normalizedSecondTaskId) return null;
  return normalizedFirstTaskId < normalizedSecondTaskId
    ? [normalizedFirstTaskId, normalizedSecondTaskId]
    : [normalizedSecondTaskId, normalizedFirstTaskId];
}

export function taskRelationReferenceId(relationId: string): string {
  return `${TASK_RELATION_REFERENCE_PREFIX}${relationId}`;
}

export function parseTaskRelationReferenceId(
  referenceId: string,
): string | null {
  return referenceId.startsWith(TASK_RELATION_REFERENCE_PREFIX)
    ? referenceId.slice(TASK_RELATION_REFERENCE_PREFIX.length)
    : null;
}

export function relatedTaskId(
  currentTaskId: string,
  relation: { taskAId: string; taskBId: string },
): string | null {
  const normalizedCurrentTaskId = currentTaskId.toLowerCase();
  if (relation.taskAId.toLowerCase() === normalizedCurrentTaskId) {
    return relation.taskBId;
  }
  if (relation.taskBId.toLowerCase() === normalizedCurrentTaskId) {
    return relation.taskAId;
  }
  return null;
}
