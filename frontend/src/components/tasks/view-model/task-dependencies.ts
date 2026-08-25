export type TaskDependency = {
  id?: string;
  task_id: string;
  depends_on_task_id: string;
};

export type DirectedTaskDependencyEdge = {
  dependencyId?: string;
  source: string;
  target: string;
};

export type TaskDependencyGraph = {
  taskIds: Set<string>;
  outgoingDependentIdsByPrerequisiteId: Map<string, Set<string>>;
  incomingPrerequisiteIdsByDependentId: Map<string, Set<string>>;
  duplicateDependencyKeys: Set<string>;
  selfDependencyKeys: Set<string>;
  unknownDependencyKeys: Set<string>;
};

export type TaskDependencyValidationReason =
  | "self"
  | "unknown-task"
  | "duplicate"
  | "cycle";

export type TaskDependencyValidation =
  | { valid: true }
  | { valid: false; reason: TaskDependencyValidationReason };

function dependencyKey(prerequisiteId: string, dependentId: string): string {
  return `${prerequisiteId}\u0000${dependentId}`;
}

export function taskDependencyToDirectedEdge(
  dependency: TaskDependency,
): DirectedTaskDependencyEdge {
  return {
    dependencyId: dependency.id,
    source: dependency.depends_on_task_id,
    target: dependency.task_id,
  };
}

/** Edges always point from prerequisite to dependent. */
export function buildTaskDependencyGraph(
  taskIds: Iterable<string>,
  dependencies: readonly TaskDependency[],
): TaskDependencyGraph {
  const knownTaskIds = new Set(taskIds);
  const outgoingDependentIdsByPrerequisiteId = new Map<
    string,
    Set<string>
  >();
  const incomingPrerequisiteIdsByDependentId = new Map<
    string,
    Set<string>
  >();
  const duplicateDependencyKeys = new Set<string>();
  const selfDependencyKeys = new Set<string>();
  const unknownDependencyKeys = new Set<string>();
  const acceptedKeys = new Set<string>();

  for (const taskId of knownTaskIds) {
    outgoingDependentIdsByPrerequisiteId.set(taskId, new Set());
    incomingPrerequisiteIdsByDependentId.set(taskId, new Set());
  }

  for (const dependency of dependencies) {
    const prerequisiteId = dependency.depends_on_task_id;
    const dependentId = dependency.task_id;
    const key = dependencyKey(prerequisiteId, dependentId);
    if (!knownTaskIds.has(prerequisiteId) || !knownTaskIds.has(dependentId)) {
      unknownDependencyKeys.add(key);
      continue;
    }
    if (acceptedKeys.has(key)) {
      duplicateDependencyKeys.add(key);
      continue;
    }
    acceptedKeys.add(key);
    if (prerequisiteId === dependentId) selfDependencyKeys.add(key);
    outgoingDependentIdsByPrerequisiteId
      .get(prerequisiteId)!
      .add(dependentId);
    incomingPrerequisiteIdsByDependentId
      .get(dependentId)!
      .add(prerequisiteId);
  }

  return {
    taskIds: knownTaskIds,
    outgoingDependentIdsByPrerequisiteId,
    incomingPrerequisiteIdsByDependentId,
    duplicateDependencyKeys,
    selfDependencyKeys,
    unknownDependencyKeys,
  };
}

export function wouldCreateTaskDependencyCycle(
  graph: TaskDependencyGraph,
  prerequisiteId: string,
  dependentId: string,
): boolean {
  if (prerequisiteId === dependentId) return true;
  const stack = [dependentId];
  const visited = new Set<string>();
  while (stack.length > 0) {
    const current = stack.pop()!;
    if (current === prerequisiteId) return true;
    if (visited.has(current)) continue;
    visited.add(current);
    for (const next of
      graph.outgoingDependentIdsByPrerequisiteId.get(current) ?? []) {
      if (!visited.has(next)) stack.push(next);
    }
  }
  return false;
}

export function validateTaskDependencyCandidate(
  graph: TaskDependencyGraph,
  prerequisiteId: string,
  dependentId: string,
): TaskDependencyValidation {
  if (prerequisiteId === dependentId) return { valid: false, reason: "self" };
  if (!graph.taskIds.has(prerequisiteId) || !graph.taskIds.has(dependentId)) {
    return { valid: false, reason: "unknown-task" };
  }
  if (
    graph.outgoingDependentIdsByPrerequisiteId
      .get(prerequisiteId)
      ?.has(dependentId)
  ) {
    return { valid: false, reason: "duplicate" };
  }
  if (wouldCreateTaskDependencyCycle(graph, prerequisiteId, dependentId)) {
    return { valid: false, reason: "cycle" };
  }
  return { valid: true };
}

export function findTaskDependencyCycleIds(
  graph: TaskDependencyGraph,
): Set<string> {
  const cycleIds = new Set<string>();
  const state = new Map<string, 0 | 1 | 2>();

  for (const startId of graph.taskIds) {
    if (state.has(startId)) continue;
    const stack: Array<{
      id: string;
      neighbors: string[];
      nextIndex: number;
    }> = [
      {
        id: startId,
        neighbors: [
          ...(graph.outgoingDependentIdsByPrerequisiteId.get(startId) ?? []),
        ],
        nextIndex: 0,
      },
    ];
    const activeIndexes = new Map<string, number>();
    state.set(startId, 1);
    activeIndexes.set(startId, 0);

    while (stack.length > 0) {
      const frame = stack[stack.length - 1];
      if (frame.nextIndex >= frame.neighbors.length) {
        state.set(frame.id, 2);
        activeIndexes.delete(frame.id);
        stack.pop();
        continue;
      }

      const nextId = frame.neighbors[frame.nextIndex];
      frame.nextIndex += 1;
      const nextState = state.get(nextId);
      if (nextState === 1) {
        const cycleStart = activeIndexes.get(nextId);
        if (cycleStart !== undefined) {
          for (let index = cycleStart; index < stack.length; index += 1) {
            cycleIds.add(stack[index].id);
          }
        }
        continue;
      }
      if (nextState === 2) continue;
      state.set(nextId, 1);
      activeIndexes.set(nextId, stack.length);
      stack.push({
        id: nextId,
        neighbors: [
          ...(graph.outgoingDependentIdsByPrerequisiteId.get(nextId) ?? []),
        ],
        nextIndex: 0,
      });
    }
  }

  return cycleIds;
}
