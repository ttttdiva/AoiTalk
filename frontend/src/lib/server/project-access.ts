import { and, eq, isNull } from "drizzle-orm";
import { db } from "@/db";
import { projectMembers, projects, users } from "@/db/schema";
import {
  hasEffectiveProjectPermission,
  type ProjectPermission,
} from "./project-permissions";

type ProjectPrincipal = { id: string; role?: string | null };

export async function getProjectWithPermission(
  projectId: string,
  user: ProjectPrincipal,
  permission: ProjectPermission,
) {
  const [project] = await db
    .select()
    .from(projects)
    .where(and(eq(projects.id, projectId), isNull(projects.deletedAt)))
    .limit(1);
  if (!project) return undefined;

  const [principal] = await db
    .select({ role: users.role })
    .from(users)
    .where(eq(users.id, user.id))
    .limit(1);
  const [membership] = await db
    .select()
    .from(projectMembers)
    .where(
      and(eq(projectMembers.projectId, projectId), eq(projectMembers.userId, user.id)),
    )
    .limit(1);

  if (!hasEffectiveProjectPermission({
    userId: user.id,
    userRole: principal?.role ?? user.role,
    projectOwnerId: project.ownerId,
    memberPermissions: membership?.permissions,
    permission,
  })) {
    return null;
  }
  return { project, membership: membership ?? null };
}

export async function getAccessibleProject(projectId: string, userId: string) {
  return getProjectWithPermission(projectId, { id: userId }, "read");
}

export async function getWritableProject(
  projectId: string,
  user: { id: string; role?: string | null },
) {
  return getProjectWithPermission(projectId, user, "write");
}

export async function getManageableProject(
  projectId: string,
  user: { id: string; role?: string | null },
) {
  return getProjectWithPermission(projectId, user, "manage_members");
}

export async function getProjectSettingsProject(
  projectId: string,
  user: ProjectPrincipal,
) {
  return getProjectWithPermission(projectId, user, "manage_settings");
}

/**
 * Return the projects that belong to the user's normal operational scope.
 *
 * This intentionally does not apply the global-admin elevation used by the
 * authorization helpers above.  A global admin is participating only when
 * they own a project or have an explicit ProjectMember row with effective
 * read permission.  Keep this separate from getReadableProjectIds(), which is
 * used for direct authorization and must continue to include global-admin
 * access.
 */
export async function getParticipatingProjectIds(
  userId: string,
  scope: { projectId?: string | null; spaceId?: string | null } = {},
): Promise<string[]> {
  const conditions = [isNull(projects.deletedAt)];
  if (scope.projectId) conditions.push(eq(projects.id, scope.projectId));
  if (scope.spaceId) conditions.push(eq(projects.spaceId, scope.spaceId));

  const rows = await db
    .select({
      projectId: projects.id,
      ownerId: projects.ownerId,
      memberPermissions: projectMembers.permissions,
    })
    .from(projects)
    .leftJoin(
      projectMembers,
      and(
        eq(projectMembers.projectId, projects.id),
        eq(projectMembers.userId, userId),
      ),
    )
    .where(and(...conditions));

  return rows
    .filter(
      (row) =>
        row.ownerId === userId ||
        hasEffectiveProjectPermission({
          userId,
          projectOwnerId: row.ownerId,
          memberPermissions: row.memberPermissions,
          permission: "read",
        }) && row.ownerId !== userId,
    )
    .map((row) => row.projectId);
}
