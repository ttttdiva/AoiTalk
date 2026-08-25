import { and, eq, inArray, isNull } from "drizzle-orm";
import { db } from "@/db";
import { projectMembers, projects, tags, users } from "@/db/schema";
import {
  parseDisplayDateAsDbTimestamp,
  serializeDbTimestamp,
} from "@/lib/server/db-time";
import { normalizeTaskStatus } from "@/lib/task-status";
import { hasProjectPermission } from "./project-permissions";
import { getParticipatingProjectIds as getParticipatingProjectIdsForScope } from "./project-access";

export type SessionUser = { id: string; role?: string | null };
export type ProjectMembership = {
  role: string | null;
  permissions?: unknown;
};

const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export function taskToSnake(
  row: Record<string, unknown>,
): Record<string, unknown> {
  const map: Record<string, string> = {
    id: "id",
    projectId: "project_id",
    knowledgeNodeId: "knowledge_node_id",
    title: "title",
    description: "description",
    status: "status",
    priority: "priority",
    startAt: "start_at",
    endAt: "end_at",
    allDay: "all_day",
    reminderOffsets: "reminder_offsets",
    notificationsEnabled: "notifications_enabled",
    source: "source",
    createdBy: "created_by",
    completedAt: "completed_at",
    archivedAt: "archived_at",
    createdAt: "created_at",
    updatedAt: "updated_at",
    deletedAt: "deleted_at",
    taskMetadata: "metadata",
    autoCloseOnDue: "auto_close_on_due",
    estimatedHours: "estimated_hours",
    sortOrder: "sort_order",
    parentTaskId: "parent_task_id",
  };
  const out: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(row)) {
    if (
      key === "startAt" ||
      key === "endAt" ||
      key === "completedAt" ||
      key === "archivedAt" ||
      key === "deletedAt"
    ) {
      const serialized = serializeDbTimestamp(
        value as Date | string | null | undefined,
      );
      out[map[key] ?? key] =
        (key === "startAt" || key === "endAt") &&
        row.allDay === true &&
        serialized
          ? serialized.slice(0, 10)
          : serialized;
    } else {
      out[map[key] ?? key] = value;
    }
  }
  if ("status" in out) {
    out.status = normalizeTaskStatus(out.status);
  }
  return out;
}

export function extractProjectColor(value: unknown): string | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  const color = (value as Record<string, unknown>).color;
  return typeof color === "string" && color.trim() ? color : null;
}

export function normalizeTaskTitle(rawTitle: unknown): string | null {
  const title = String(rawTitle ?? "").trim();
  if (!title) return null;
  if (title === "無題のタスク" || title === "Untitled task") return null;
  return title;
}

export function normalizeOptionalUuid(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  if (!trimmed) return null;
  return UUID_PATTERN.test(trimmed) ? trimmed : null;
}

export function parseTaskWallClockDate(value: unknown): Date | null {
  if (value === null || value === undefined || value === "") return null;
  if (value instanceof Date || typeof value === "string") {
    return parseDisplayDateAsDbTimestamp(value);
  }
  return null;
}

export function isDateOnlyTaskInput(value: unknown): boolean {
  if (typeof value !== "string") return false;
  const trimmed = value.trim();
  return /^\d{4}-\d{2}-\d{2}$/.test(trimmed);
}

export function stripGoogleCalendarMetadata(
  value: unknown,
): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  const metadata = { ...(value as Record<string, unknown>) };
  delete metadata.google_calendar;
  return metadata;
}

export function canWriteMembership(
  user: SessionUser,
  membership: ProjectMembership | null,
): boolean {
  if (user.role === "admin") return true;
  if (!membership) return false;
  return hasProjectPermission(membership.permissions, "write");
}

export async function getProjectMembership(
  userId: string,
  projectId: string,
): Promise<ProjectMembership | null> {
  const [membership] = await db
    .select({ role: projectMembers.role, permissions: projectMembers.permissions })
    .from(projectMembers)
    .innerJoin(projects, eq(projects.id, projectMembers.projectId))
    .where(
      and(
        eq(projectMembers.userId, userId),
        eq(projectMembers.projectId, projectId),
        isNull(projects.deletedAt),
      ),
    )
    .limit(1);
  return membership ?? null;
}

export async function getProjectAccess(
  user: SessionUser,
  projectId: string,
): Promise<{
  project: typeof projects.$inferSelect;
  membership: ProjectMembership | null;
  canRead: boolean;
  canWrite: boolean;
} | null> {
  const [project] = await db
    .select()
    .from(projects)
    .where(and(eq(projects.id, projectId), isNull(projects.deletedAt)))
    .limit(1);
  if (!project) return null;

  const membership = await getProjectMembership(user.id, projectId);
  const elevated = user.role === "admin" || project.ownerId === user.id;
  return {
    project,
    membership,
    canRead: elevated || hasProjectPermission(membership?.permissions, "read"),
    canWrite:
      elevated || hasProjectPermission(membership?.permissions, "write"),
  };
}

export async function canReadProjectId(
  user: SessionUser,
  projectId: string,
): Promise<boolean> {
  return Boolean((await getProjectAccess(user, projectId))?.canRead);
}

export async function canWriteProjectId(
  user: SessionUser,
  projectId: string,
): Promise<boolean> {
  return Boolean((await getProjectAccess(user, projectId))?.canWrite);
}

export async function getProjectSpaceId(
  projectId: string,
): Promise<string | null> {
  const [project] = await db
    .select({ spaceId: projects.spaceId })
    .from(projects)
    .where(and(eq(projects.id, projectId), isNull(projects.deletedAt)))
    .limit(1);
  return project?.spaceId ?? null;
}

export async function resolveProjectTagIds(
  projectId: string,
  rawTagIds: unknown,
): Promise<{ tagIds: string[]; invalidTagIds: string[] }> {
  const requestedTagIds = Array.isArray(rawTagIds)
    ? [
        ...new Set(
          rawTagIds.filter(
            (tagId): tagId is string =>
              typeof tagId === "string" && UUID_PATTERN.test(tagId),
          ),
        ),
      ]
    : [];

  if (requestedTagIds.length === 0) {
    return { tagIds: [], invalidTagIds: [] };
  }

  const spaceId = await getProjectSpaceId(projectId);
  if (!spaceId) {
    return { tagIds: [], invalidTagIds: requestedTagIds };
  }

  const validRows = await db
    .select({ id: tags.id })
    .from(tags)
    .where(and(eq(tags.spaceId, spaceId), inArray(tags.id, requestedTagIds)));
  const validTagIds = new Set(validRows.map((tag) => tag.id));

  return {
    tagIds: requestedTagIds.filter((tagId) => validTagIds.has(tagId)),
    invalidTagIds: requestedTagIds.filter((tagId) => !validTagIds.has(tagId)),
  };
}

export async function getReadableProjectIds(
  userId: string,
  scope: { projectId?: string | null; spaceId?: string | null } = {},
): Promise<string[]> {
  const [principal] = await db
    .select({ role: users.role })
    .from(users)
    .where(eq(users.id, userId))
    .limit(1);
  const isGlobalAdmin = principal?.role === "admin";
  const conditions = [isNull(projects.deletedAt)];
  if (scope.projectId) conditions.push(eq(projects.id, scope.projectId));
  if (scope.spaceId) conditions.push(eq(projects.spaceId, scope.spaceId));

  const projectRows = await db
    .select({ projectId: projects.id, ownerId: projects.ownerId })
    .from(projects)
    .where(and(...conditions));
  if (projectRows.length === 0) return [];

  const membershipRows = await db
    .select({ projectId: projectMembers.projectId, permissions: projectMembers.permissions })
    .from(projectMembers)
    .where(
      and(
        eq(projectMembers.userId, userId),
        inArray(projectMembers.projectId, projectRows.map((row) => row.projectId)),
      ),
    );
  const permissionsByProject = new Map(
    membershipRows.map((row) => [row.projectId, row.permissions]),
  );
  return projectRows
    .filter(
      (row) =>
        isGlobalAdmin ||
        row.ownerId === userId ||
        hasProjectPermission(permissionsByProject.get(row.projectId), "read"),
    )
    .map((row) => row.projectId);
}

/** Operational (participating) project scope; excludes global-admin-only access. */
export async function getParticipatingProjectIds(
  userId: string,
  scope: { projectId?: string | null; spaceId?: string | null } = {},
): Promise<string[]> {
  return getParticipatingProjectIdsForScope(userId, scope);
}
