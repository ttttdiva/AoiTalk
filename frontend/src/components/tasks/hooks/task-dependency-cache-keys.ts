export function taskDependencyCacheKey(taskId: string): string {
  return `task-dependencies:${taskId}`;
}

export function projectTaskDependencyCacheKey(projectId: string): string {
  return `task-dependencies:project:${projectId}`;
}
