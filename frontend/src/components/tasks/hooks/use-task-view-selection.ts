"use client";

import { useMemo } from "react";

import {
  applyTaskFilter,
  type FilterConfig,
} from "@/components/tasks/task-filter-builder";
import { selectTaskViewTasks } from "@/components/tasks/view-model/task-view-filter";
import type { Project, Task } from "@/lib/task-api";
import {
  getTaskDisplayStatus,
  isFutureTask,
  isOverdue,
  type FilterTab,
} from "@/lib/tasks-page-utils";

export type TaskHierarchyViewFilter = {
  filter: FilterTab;
  showClosed: boolean;
  showFuture: boolean;
  showOnlyMine: boolean;
  customFilter: FilterConfig;
  search: string;
  currentUserId: string | null;
};

type UseTaskViewSelectionOptions = {
  tasks: readonly Task[];
  projects: readonly Project[];
  projectTab: string;
  appFilterId: string;
  appTaskIds: ReadonlySet<string>;
  filterState: TaskHierarchyViewFilter;
  includeFuture: boolean;
};

function collectAppHierarchyIds(
  tasks: readonly Task[],
  appTaskIds: ReadonlySet<string>,
): Set<string> {
  const childrenByParent = new Map<string, string[]>();
  for (const task of tasks) {
    if (!task.parent_task_id) continue;
    const children = childrenByParent.get(task.parent_task_id) ?? [];
    children.push(task.id);
    childrenByParent.set(task.parent_task_id, children);
  }

  const included = new Set(appTaskIds);
  const stack = [...appTaskIds];
  while (stack.length > 0) {
    const parentId = stack.pop()!;
    for (const childId of childrenByParent.get(parentId) ?? []) {
      if (included.has(childId)) continue;
      included.add(childId);
      stack.push(childId);
    }
  }
  return included;
}

export function useTaskViewSelection({
  tasks,
  projects,
  projectTab,
  appFilterId,
  appTaskIds,
  filterState,
  includeFuture,
}: UseTaskViewSelectionOptions) {
  const {
    filter,
    showClosed,
    showFuture,
    showOnlyMine,
    customFilter,
    search,
    currentUserId,
  } = filterState;
  return useMemo(() => {
    const projectIds = new Set(projects.map((project) => project.id));
    const projectNames = new Map(
      projects.map((project) => [project.id, project.name]),
    );
    const appHierarchyIds = appFilterId
      ? collectAppHierarchyIds(tasks, appTaskIds)
      : null;
    const inScope = (task: Task) => {
      if (
        projectTab !== "all"
          ? task.project_id !== projectTab
          : !projectIds.has(task.project_id)
      ) {
        return false;
      }
      return !appHierarchyIds || appHierarchyIds.has(task.id);
    };
    const scopedTasks = tasks.filter(inScope);
    const customMatchedIds =
      customFilter.rules.length > 0
        ? new Set(
            applyTaskFilter(scopedTasks, customFilter, projectNames).map(
              (task) => task.id,
            ),
          )
        : null;
    const normalizedSearch = search.trim().toLowerCase();

    return selectTaskViewTasks(tasks, {
      scopePredicate: inScope,
      matchPredicate: (task) => {
        if (!showClosed && getTaskDisplayStatus(task) === "closed") {
          return false;
        }
        if (!includeFuture && !showFuture && isFutureTask(task)) {
          return false;
        }
        if (
          showOnlyMine &&
          currentUserId &&
          !task.assignees.some((assignee) => assignee.user_id === currentUserId)
        ) {
          return false;
        }
        if (filter === "overdue" && !isOverdue(task)) return false;
        if (customMatchedIds && !customMatchedIds.has(task.id)) return false;
        if (
          normalizedSearch &&
          !task.title.toLowerCase().includes(normalizedSearch) &&
          !task.description?.toLowerCase().includes(normalizedSearch) &&
          !task.tags.some((tag) =>
            tag.name.toLowerCase().includes(normalizedSearch),
          )
        ) {
          return false;
        }
        return true;
      },
      includeAncestorsOfMatches: true,
      includeDescendantsOfMatches: true,
    });
  }, [
    appFilterId,
    appTaskIds,
    currentUserId,
    customFilter,
    filter,
    includeFuture,
    projectTab,
    projects,
    search,
    showClosed,
    showFuture,
    showOnlyMine,
    tasks,
  ]);
}
