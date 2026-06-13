import type { Task } from "@/lib/task-api";

export type DropMode =
  | "reorder-before"
  | "reorder-after"
  | "subtask-before"
  | "subtask-after";

export function resolveTaskDropMode(
  pointer: { clientX: number; clientY: number },
  targetRect: DOMRect,
): DropMode {
  const relX = (pointer.clientX - targetRect.left) / targetRect.width;
  const relY = (pointer.clientY - targetRect.top) / targetRect.height;
  const isBeforeBoundary = relY < 0.5;
  const wantsIndent = relX > 0.25;
  if (wantsIndent) {
    return isBeforeBoundary ? "subtask-before" : "subtask-after";
  }
  return isBeforeBoundary ? "reorder-before" : "reorder-after";
}

export function buildTopLevelReorderIds({
  taskList,
  projectId,
  movingIds,
  dropTask,
  dropMode,
}: {
  taskList: Task[];
  projectId: string;
  movingIds: string[];
  dropTask: Task;
  dropMode: DropMode;
}): string[] | null {
  const movingSet = new Set(movingIds);
  const anchorId = dropTask.parent_task_id || dropTask.id;
  if (movingSet.has(anchorId)) return null;

  const baseIds = taskList
    .filter(
      (task) =>
        task.project_id === projectId &&
        !task.parent_task_id &&
        !movingSet.has(task.id),
    )
    .map((task) => task.id);
  const insertAtAnchor = baseIds.indexOf(anchorId);
  if (insertAtAnchor === -1) return null;

  const taskIds = new Set(taskList.map((task) => task.id));
  const orderedMovingIds = movingIds.filter((id) => taskIds.has(id));
  if (orderedMovingIds.length === 0) return null;

  let insertIndex = insertAtAnchor;
  if (dropMode === "reorder-after") insertIndex += 1;
  insertIndex = Math.max(0, Math.min(baseIds.length, insertIndex));

  const nextIds = [...baseIds];
  nextIds.splice(insertIndex, 0, ...orderedMovingIds);
  return nextIds;
}

export function buildAllTopLevelReorderIds({
  taskList,
  projectIds,
  movingIds,
  dropTask,
  dropMode,
}: {
  taskList: Task[];
  projectIds: Set<string>;
  movingIds: string[];
  dropTask: Task;
  dropMode: DropMode;
}): string[] | null {
  const movingSet = new Set(movingIds);
  const anchorId = dropTask.parent_task_id || dropTask.id;
  if (movingSet.has(anchorId)) return null;

  const baseIds = taskList
    .filter(
      (task) =>
        projectIds.has(task.project_id) &&
        !task.parent_task_id &&
        !movingSet.has(task.id),
    )
    .map((task) => task.id);
  const insertAtAnchor = baseIds.indexOf(anchorId);
  if (insertAtAnchor === -1) return null;

  const taskIds = new Set(taskList.map((task) => task.id));
  const orderedMovingIds = movingIds.filter((id) => taskIds.has(id));
  if (orderedMovingIds.length === 0) return null;

  let insertIndex = insertAtAnchor;
  if (dropMode === "reorder-after") insertIndex += 1;
  insertIndex = Math.max(0, Math.min(baseIds.length, insertIndex));

  const nextIds = [...baseIds];
  nextIds.splice(insertIndex, 0, ...orderedMovingIds);
  return nextIds;
}
