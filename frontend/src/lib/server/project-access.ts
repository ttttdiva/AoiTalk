import { and, eq, isNull } from "drizzle-orm";
import { db } from "@/db";
import { projectMembers, projects } from "@/db/schema";

export async function getAccessibleProject(projectId: string, userId: string) {
  const [project] = await db
    .select()
    .from(projects)
    .where(and(eq(projects.id, projectId), isNull(projects.deletedAt)))
    .limit(1);
  if (!project) return null;

  const [membership] = await db
    .select()
    .from(projectMembers)
    .where(
      and(eq(projectMembers.projectId, projectId), eq(projectMembers.userId, userId)),
    )
    .limit(1);

  if (!membership) return null;
  return { project, membership };
}

export async function getWritableProject(
  projectId: string,
  user: { id: string; role?: string | null },
) {
  const result = await getAccessibleProject(projectId, user.id);
  if (!result) return null;
  if (
    result.membership.role !== "admin" &&
    result.membership.role !== "owner" &&
    user.role !== "admin"
  ) {
    return null;
  }
  return result;
}
