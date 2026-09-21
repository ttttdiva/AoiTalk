import type { Project, Space } from "../types/api";

/** Webと同じ一覧ポリシー。名前ではなく所有者固有の予約slugで既定Inboxを判定する。 */
export function isForeignDefaultInboxProject(
  project: { owner_id?: string | null; slug?: string | null },
  authScope: string | null | undefined,
): boolean {
  if (!authScope?.startsWith("auth:") || authScope.startsWith("auth:opaque:")) return false;
  const userId = authScope.slice("auth:".length);
  return Boolean(
    userId && project.owner_id && project.owner_id !== userId &&
    project.slug === `inbox-project-${project.owner_id}`,
  );
}


/** Operational membership is never implied by administrative visibility. */
export function isParticipatingProject(
  project: Pick<Project, "owner_id" | "slug" | "deleted_at" | "is_participating" | "membership">,
  authScope: string | null | undefined,
): boolean {
  if (project.deleted_at || isForeignDefaultInboxProject(project, authScope)) return false;
  if (typeof project.is_participating === "boolean") return project.is_participating;
  // Owner-less rows are local/offline creations awaiting their server owner.
  if (project.owner_id == null) return true;
  const userId = authScope?.startsWith("auth:") && !authScope.startsWith("auth:opaque:")
    ? authScope.slice("auth:".length) : null;
  return project.owner_id === userId || project.membership?.permissions?.read === true;
}

/** Hide unrelated space headings without hiding an owner's empty space. */
export function participatingScopeSpaces(
  spaces: Space[], projects: Project[], authScope: string | null | undefined,
): Space[] {
  const spaceIds = new Set(projects.map((project) => project.space_id));
  return spaces.filter((space) => spaceIds.has(space.id) || (
    Boolean(space.owner_id) && authScope === `auth:${space.owner_id}`
  ));
}
