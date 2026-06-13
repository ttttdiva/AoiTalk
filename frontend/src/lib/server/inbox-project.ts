import { and, eq } from "drizzle-orm";
import { db } from "@/db";
import { projectMembers, projects, spaces } from "@/db/schema";

export async function ensureInboxSpace(userId: string) {
  const existing = await db
    .select()
    .from(spaces)
    .where(eq(spaces.slug, `inbox-${userId}`))
    .limit(1);

  if (existing.length > 0) {
    return existing[0];
  }

  const [created] = await db
    .insert(spaces)
    .values({
      name: "Inbox",
      slug: `inbox-${userId}`,
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
  const existing = await db
    .select()
    .from(projects)
    .where(
      and(eq(projects.spaceId, inboxSpaceId), eq(projects.ownerId, userId)),
    )
    .limit(1);

  if (existing.length > 0) {
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
        role: "admin",
      });
    }

    return existing[0];
  }

  const [project] = await db
    .insert(projects)
    .values({
      name: "Inbox",
      slug: `inbox-project-${userId}`,
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
    role: "admin",
  });

  return project;
}

export async function ensureUserInboxSetup(userId: string) {
  const inboxSpace = await ensureInboxSpace(userId);
  const inboxProject = await ensureInboxDefaultProject(userId, inboxSpace.id);
  return { inboxSpace, inboxProject };
}
