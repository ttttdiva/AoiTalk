import type { Task } from "@/lib/task-api";

export type TaskHierarchyIssue =
  | "duplicate"
  | "orphan"
  | "self-parent"
  | "cycle";

export type TaskHierarchyNode = {
  task: Task;
  parentId: string | null;
  originalParentId: string | null;
  children: TaskHierarchyNode[];
  issues: Set<TaskHierarchyIssue>;
};

export type TaskHierarchy = {
  roots: TaskHierarchyNode[];
  nodesById: Map<string, TaskHierarchyNode>;
  duplicateTaskIds: Set<string>;
  orphanTaskIds: Set<string>;
  selfParentTaskIds: Set<string>;
  cycleTaskIds: Set<string>;
};

export type TaskHierarchyRow = {
  task: Task;
  depth: number;
  parentId: string | null;
  hasChildren: boolean;
  expanded: boolean;
  depthLimited: boolean;
  issues: Set<TaskHierarchyIssue>;
};

export type TaskHierarchyProgress = {
  childCount: number;
  completedChildCount: number;
  completionRate: number | null;
  descendantCount: number;
  completedDescendantCount: number;
  descendantCompletionRate: number | null;
};

function taskIsClosed(task: Task): boolean {
  return (task.effective_occurrence_status ?? task.status) === "closed";
}

/**
 * Builds a deterministic forest in O(n). The first task with an id wins.
 * Broken parent references, self references and every node in a parent cycle
 * are promoted to roots so each task can be rendered at most once.
 */
export function buildTaskHierarchy(tasks: readonly Task[]): TaskHierarchy {
  const nodesById = new Map<string, TaskHierarchyNode>();
  const duplicateTaskIds = new Set<string>();
  const orphanTaskIds = new Set<string>();
  const selfParentTaskIds = new Set<string>();
  const cycleTaskIds = new Set<string>();

  for (const task of tasks) {
    if (nodesById.has(task.id)) {
      duplicateTaskIds.add(task.id);
      nodesById.get(task.id)?.issues.add("duplicate");
      continue;
    }
    nodesById.set(task.id, {
      task,
      parentId: null,
      originalParentId: task.parent_task_id ?? null,
      children: [],
      issues: new Set(),
    });
  }

  for (const node of nodesById.values()) {
    const parentId = node.originalParentId;
    if (!parentId) continue;
    if (parentId === node.task.id) {
      selfParentTaskIds.add(node.task.id);
      node.issues.add("self-parent");
      continue;
    }
    if (!nodesById.has(parentId)) {
      orphanTaskIds.add(node.task.id);
      node.issues.add("orphan");
      continue;
    }
    node.parentId = parentId;
  }

  const settled = new Set<string>();
  for (const id of nodesById.keys()) {
    if (settled.has(id)) continue;
    const path: string[] = [];
    const pathIndexes = new Map<string, number>();
    let currentId: string | null = id;

    while (currentId && !settled.has(currentId)) {
      const cycleStart = pathIndexes.get(currentId);
      if (cycleStart !== undefined) {
        for (let index = cycleStart; index < path.length; index += 1) {
          cycleTaskIds.add(path[index]);
        }
        break;
      }
      pathIndexes.set(currentId, path.length);
      path.push(currentId);
      currentId = nodesById.get(currentId)?.parentId ?? null;
    }

    for (const pathId of path) settled.add(pathId);
  }

  for (const cycleId of cycleTaskIds) {
    const node = nodesById.get(cycleId);
    if (!node) continue;
    node.parentId = null;
    node.issues.add("cycle");
  }

  const roots: TaskHierarchyNode[] = [];
  for (const node of nodesById.values()) {
    const parent = node.parentId ? nodesById.get(node.parentId) : undefined;
    if (parent) parent.children.push(node);
    else roots.push(node);
  }

  return {
    roots,
    nodesById,
    duplicateTaskIds,
    orphanTaskIds,
    selfParentTaskIds,
    cycleTaskIds,
  };
}

export function completeTaskHierarchyVisibility(
  hierarchy: TaskHierarchy,
  matchedTaskIds: ReadonlySet<string>,
  options: { includeDescendantsOfMatches?: boolean } = {},
): { visibleTaskIds: Set<string>; contextTaskIds: Set<string> } {
  const visibleTaskIds = new Set<string>();

  for (const matchedId of matchedTaskIds) {
    let current = hierarchy.nodesById.get(matchedId);
    while (current && !visibleTaskIds.has(current.task.id)) {
      visibleTaskIds.add(current.task.id);
      current = current.parentId
        ? hierarchy.nodesById.get(current.parentId)
        : undefined;
    }
  }

  if (options.includeDescendantsOfMatches) {
    const stack = [...matchedTaskIds]
      .map((id) => hierarchy.nodesById.get(id))
      .filter((node): node is TaskHierarchyNode => Boolean(node));
    while (stack.length > 0) {
      const node = stack.pop()!;
      if (!visibleTaskIds.has(node.task.id)) visibleTaskIds.add(node.task.id);
      for (const child of node.children) {
        if (!visibleTaskIds.has(child.task.id)) stack.push(child);
      }
    }
  }

  const contextTaskIds = new Set(
    [...visibleTaskIds].filter((id) => !matchedTaskIds.has(id)),
  );
  return { visibleTaskIds, contextTaskIds };
}

export function flattenTaskHierarchy(
  hierarchy: TaskHierarchy,
  options: {
    visibleTaskIds?: ReadonlySet<string>;
    expandedTaskIds?: ReadonlySet<string>;
    maxDepth?: number;
  } = {},
): TaskHierarchyRow[] {
  const rows: TaskHierarchyRow[] = [];
  const visited = new Set<string>();
  const maxDepth = Math.max(0, options.maxDepth ?? Number.POSITIVE_INFINITY);
  const stack = hierarchy.roots
    .slice()
    .reverse()
    .map((node) => ({ node, depth: 0 }));

  while (stack.length > 0) {
    const { node, depth } = stack.pop()!;
    if (visited.has(node.task.id)) continue;
    visited.add(node.task.id);
    if (options.visibleTaskIds && !options.visibleTaskIds.has(node.task.id)) {
      continue;
    }

    const expanded =
      !options.expandedTaskIds || options.expandedTaskIds.has(node.task.id);
    const depthLimited = depth >= maxDepth && node.children.length > 0;
    rows.push({
      task: node.task,
      depth,
      parentId: node.parentId,
      hasChildren: node.children.length > 0,
      expanded,
      depthLimited,
      issues: node.issues,
    });

    if (!expanded || depthLimited) continue;
    for (let index = node.children.length - 1; index >= 0; index -= 1) {
      const child = node.children[index];
      if (!options.visibleTaskIds || options.visibleTaskIds.has(child.task.id)) {
        stack.push({ node: child, depth: depth + 1 });
      }
    }
  }

  return rows;
}

export function calculateTaskHierarchyProgress(
  hierarchy: TaskHierarchy,
): Map<string, TaskHierarchyProgress> {
  const progress = new Map<string, TaskHierarchyProgress>();
  const stack = hierarchy.roots.map((node) => ({ node, visited: false }));

  while (stack.length > 0) {
    const entry = stack.pop()!;
    if (!entry.visited) {
      stack.push({ node: entry.node, visited: true });
      for (const child of entry.node.children) {
        stack.push({ node: child, visited: false });
      }
      continue;
    }

    const childCount = entry.node.children.length;
    let completedChildCount = 0;
    let descendantCount = 0;
    let completedDescendantCount = 0;
    for (const child of entry.node.children) {
      if (taskIsClosed(child.task)) completedChildCount += 1;
      const childProgress = progress.get(child.task.id);
      descendantCount += 1 + (childProgress?.descendantCount ?? 0);
      completedDescendantCount +=
        (taskIsClosed(child.task) ? 1 : 0) +
        (childProgress?.completedDescendantCount ?? 0);
    }

    progress.set(entry.node.task.id, {
      childCount,
      completedChildCount,
      completionRate:
        childCount > 0 ? completedChildCount / childCount : null,
      descendantCount,
      completedDescendantCount,
      descendantCompletionRate:
        descendantCount > 0
          ? completedDescendantCount / descendantCount
          : null,
    });
  }

  return progress;
}
