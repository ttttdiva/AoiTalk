import { and, eq } from "drizzle-orm";
import { db } from "@/db";
import { projectMembers, projects, spaces } from "@/db/schema";
import { getDefaultProjectPermissions } from "./project-permissions";

export async function ensureInboxSpace(userId: string) {
  const slug = `inbox-${userId}`;
  const existing = await db
    .select()
    .from(spaces)
    .where(eq(spaces.slug, slug))
    .limit(1);

  if (existing.length > 0) {
    if (existing[0].ownerId !== userId) {
      throw new Error("Reserved Inbox space slug is owned by another user");
    }
    return existing[0];
  }

  const [created] = await db
    .insert(spaces)
    .values({
      name: "Inbox",
      slug,
      description: "未整理のタスクを一時的に置く場所",
      color: "#6b7280",
      ownerId: userId,
      sortOrder: 9999,
    })
    .returning();

  return created;
}

export async function ensureInboxDefaultProject(
  userId: string,
  inboxSpaceId: string,
) {
  const slug = `inbox-project-${userId}`;
  const existing = await db
    .select()
    .from(projects)
    .where(eq(projects.slug, slug))
    .limit(1);

  if (existing.length > 0) {
    if (
      existing[0].ownerId !== userId ||
      existing[0].spaceId !== inboxSpaceId
    ) {
      throw new Error("Reserved Inbox project slug is owned by another user");
    }

    const [repairedProject] = await db
      .update(projects)
      .set({
        name: "Inbox",
        deletedAt: null,
        projectMetadata: {
          ...(existing[0].projectMetadata ?? {}),
          aliases: ["inbox"],
          color: "#6b7280",
          isInboxDefault: true,
        },
      })
      .where(eq(projects.id, existing[0].id))
      .returning();

    const member = await db
      .select()
      .from(projectMembers)
      .where(
        and(
          eq(projectMembers.projectId, existing[0].id),
          eq(projectMembers.userId, userId),
        ),
      )
      .limit(1);

    if (member.length === 0) {
      await db.insert(projectMembers).values({
        projectId: existing[0].id,
        userId,
        role: "owner",
        permissions: getDefaultProjectPermissions("owner"),
      });
    } else {
      await db
        .update(projectMembers)
        .set({
          role: "owner",
          permissions: getDefaultProjectPermissions("owner"),
        })
        .where(
          and(
            eq(projectMembers.projectId, existing[0].id),
            eq(projectMembers.userId, userId),
          ),
        );
    }

    return repairedProject ?? existing[0];
  }

  const [project] = await db
    .insert(projects)
    .values({
      name: "Inbox",
      slug,
      description: "未整理のタスクを一時的に置く場所",
      ownerId: userId,
      spaceId: inboxSpaceId,
      projectMetadata: {
        aliases: ["inbox"],
        color: "#6b7280",
        isInboxDefault: true,
      },
    })
    .returning();

  await db.insert(projectMembers).values({
    projectId: project.id,
    userId,
    role: "owner",
    permissions: getDefaultProjectPermissions("owner"),
  });

  return project;
}

export async function ensureUserInboxSetup(userId: string) {
  const inboxSpace = await ensureInboxSpace(userId);
  const inboxProject = await ensureInboxDefaultProject(userId, inboxSpace.id);
  return { inboxSpace, inboxProject };
}
