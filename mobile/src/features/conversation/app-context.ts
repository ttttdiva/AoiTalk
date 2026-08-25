import type { ProjectAppBinding } from "../../lib/apps-api";

/** App context may move only into a Project with an enabled binding. */
export function appContextCompatibleWithProject(
  appId: string | null | undefined,
  projectId: string | null | undefined,
  bindings: readonly ProjectAppBinding[],
): boolean {
  if (!appId || !projectId) return true;
  return bindings.some((binding) => binding.enabled && binding.app_id === appId);
}

