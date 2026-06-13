import { and, eq, isNotNull } from "drizzle-orm";
import { db } from "@/db";
import { projectMembers, projects, spaces } from "@/db/schema";

export type SessionUser = { id: string; role?: string | null };
export type SpaceRow = typeof spaces.$inferSelect;

export function isInboxSpace(space: Pick<SpaceRow, "ownerId" | "slug">) {
  return space.slug === `inbox-${space.ownerId}`;
}

export function canAdminSeeSpace(
  user: SessionUser,
  space: Pick<SpaceRow, "ownerId" | "slug">,
) {
  if (isInboxSpace(space)) {
    return space.ownerId === user.id;
  }
  return user.role === "admin";
}

export async function listReadableSpaces(user: SessionUser) {
  const memberSpaceRows = await db
    .select({ spaceId: projects.spaceId })
    .from(projectMembers)
    .innerJoin(projects, eq(projectMembers.projectId, projects.id))
    .where(
      and(eq(projectMembers.userId, user.id), isNotNull(projects.spaceId)),
    );
  const memberSpaceIds = new Set(
    memberSpaceRows
      .map((row) => row.spaceId)
      .filter((spaceId): spaceId is string => Boolean(spaceId)),
  );

  const rows = await db.select().from(spaces).orderBy(spaces.sortOrder);
  return rows.filter((space) => {
    if (isInboxSpace(space)) {
      return space.ownerId === user.id;
    }
    return (
      space.ownerId === user.id ||
      user.role === "admin" ||
      memberSpaceIds.has(space.id)
    );
  });
}

export async function getReadableSpace(spaceId: string, user: SessionUser) {
  const [space] = await db
    .select()
    .from(spaces)
    .where(eq(spaces.id, spaceId))
    .limit(1);
  if (!space) return null;

  if (isInboxSpace(space)) {
    return space.ownerId === user.id ? space : null;
  }

  if (space.ownerId === user.id || user.role === "admin") {
    return space;
  }

  const [membership] = await db
    .select({ projectId: projectMembers.projectId })
    .from(projectMembers)
    .innerJoin(projects, eq(projectMembers.projectId, projects.id))
    .where(
      and(eq(projectMembers.userId, user.id), eq(projects.spaceId, spaceId)),
    )
    .limit(1);
  return membership ? space : null;
}

export async function canWriteSpace(spaceId: string, user: SessionUser) {
  const [space] = await db
    .select()
    .from(spaces)
    .where(eq(spaces.id, spaceId))
    .limit(1);
  if (!space) return { allowed: false, space: null };
  if (isInboxSpace(space)) {
    return { allowed: space.ownerId === user.id, space };
  }
  return { allowed: space.ownerId === user.id || user.role === "admin", space };
}

export async function getReadableProjectIdsForSpace(
  spaceId: string,
  user: SessionUser,
) {
  const space = await getReadableSpace(spaceId, user);
  if (!space) return null;

  if (space.ownerId === user.id || canAdminSeeSpace(user, space)) {
    const rows = await db
      .select({ id: projects.id })
      .from(projects)
      .where(eq(projects.spaceId, spaceId));
    return rows.map((project) => project.id);
  }

  const rows = await db
    .select({ id: projects.id })
    .from(projectMembers)
    .innerJoin(projects, eq(projectMembers.projectId, projects.id))
    .where(
      and(eq(projectMembers.userId, user.id), eq(projects.spaceId, spaceId)),
    );
  return rows.map((project) => project.id);
}
