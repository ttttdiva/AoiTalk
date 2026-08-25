import type { Task } from "@/lib/task-api";

import {
  buildTaskHierarchy,
  completeTaskHierarchyVisibility,
  type TaskHierarchy,
} from "./task-hierarchy";

export type TaskViewPredicate = (task: Task) => boolean;

export type TaskViewFilterOptions = {
  scopePredicate?: TaskViewPredicate;
  matchPredicate?: TaskViewPredicate;
  includeAncestorsOfMatches?: boolean;
  includeDescendantsOfMatches?: boolean;
};

export type TaskViewFilterResult = {
  scopedTasks: Task[];
  matchedTaskIds: Set<string>;
  visibleTaskIds: Set<string>;
  contextTaskIds: Set<string>;
  visibleTasks: Task[];
  hierarchy: TaskHierarchy;
};

/**
 * Separates scope, explicit matches and hierarchy context. Results preserve API
 * order and duplicate ids are represented only once.
 */
export function selectTaskViewTasks(
  tasks: readonly Task[],
  options: TaskViewFilterOptions = {},
): TaskViewFilterResult {
  const scopedCandidates = options.scopePredicate
    ? tasks.filter(options.scopePredicate)
    : [...tasks];
  const hierarchy = buildTaskHierarchy(scopedCandidates);
  const scopedTasks = [...hierarchy.nodesById.values()].map((node) => node.task);
  const matchedTaskIds = new Set(
    scopedTasks
      .filter((task) => options.matchPredicate?.(task) ?? true)
      .map((task) => task.id),
  );

  let visibleTaskIds: Set<string>;
  let contextTaskIds: Set<string>;
  if (options.includeAncestorsOfMatches === false) {
    visibleTaskIds = new Set(matchedTaskIds);
    contextTaskIds = new Set();
    if (options.includeDescendantsOfMatches) {
      const completed = completeTaskHierarchyVisibility(
        hierarchy,
        matchedTaskIds,
        { includeDescendantsOfMatches: true },
      );
      for (const id of completed.visibleTaskIds) {
        if (!completed.contextTaskIds.has(id)) continue;
        let parentId = hierarchy.nodesById.get(id)?.parentId ?? null;
        let belongsToMatch = false;
        while (parentId) {
          if (matchedTaskIds.has(parentId)) {
            belongsToMatch = true;
            break;
          }
          parentId = hierarchy.nodesById.get(parentId)?.parentId ?? null;
        }
        if (belongsToMatch) visibleTaskIds.add(id);
      }
      contextTaskIds = new Set(
        [...visibleTaskIds].filter((id) => !matchedTaskIds.has(id)),
      );
    }
  } else {
    ({ visibleTaskIds, contextTaskIds } = completeTaskHierarchyVisibility(
      hierarchy,
      matchedTaskIds,
      {
        includeDescendantsOfMatches:
          options.includeDescendantsOfMatches ?? true,
      },
    ));
  }

  return {
    scopedTasks,
    matchedTaskIds,
    visibleTaskIds,
    contextTaskIds,
    visibleTasks: scopedTasks.filter((task) => visibleTaskIds.has(task.id)),
    hierarchy,
  };
}
