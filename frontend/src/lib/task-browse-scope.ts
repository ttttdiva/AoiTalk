/**
 * Ephemeral, explicit read scope owned by Tasks/Calendar/Reports.
 *
 * This is intentionally separate from ProjectContext's normal selection. A
 * browse target is never persisted as selectedSpaceId/selectedProjectId or in
 * the per-space restore map.
 */
export type TaskBrowseScope =
  | { kind: "project"; id: string }
  | { kind: "space"; id: string };

export type TaskBrowseScopeQuery =
  | { browse_project_id: string; browse_space_id?: never }
  | { browse_space_id: string; browse_project_id?: never };

export function taskBrowseScopeToQuery(
  scope: TaskBrowseScope | null | undefined,
): TaskBrowseScopeQuery | undefined {
  if (!scope?.id) return undefined;
  return scope.kind === "project"
    ? { browse_project_id: scope.id }
    : { browse_space_id: scope.id };
}

export function taskBrowseScopeKey(
  scope: TaskBrowseScope | null | undefined,
): string {
  return scope ? `${scope.kind}:${scope.id}` : "normal";
}
