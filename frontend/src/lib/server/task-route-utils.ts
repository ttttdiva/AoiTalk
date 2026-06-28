import { and, eq, inArray, isNull } from "drizzle-orm";
import { db } from "@/db";
import { projectMembers, projects, tags } from "@/db/schema";
import {
  parseDisplayDateAsDbTimestamp,
  serializeDbTimestamp,
} from "@/lib/server/db-time";
import { normalizeTaskStatus } from "@/lib/task-status";

export type SessionUser = { id: string; role?: string | null };
export type ProjectMembership = { role: string | null };

const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export function taskToSnake(
  row: Record<string, unknown>,
): Record<string, unknown> {
  const map: Record<string, string> = {
    id: "id",
    projectId: "project_id",
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
  if (!membership) return false;
  return (
    user.role === "admin" ||
    membership.role === "admin" ||
    membership.role === "owner"
  );
}

export async function getProjectMembership(
  userId: string,
  projectId: string,
): Promise<ProjectMembership | null> {
  const [membership] = await db
    .select({ role: projectMembers.role })
    .from(projectMembers)
    .where(
      and(
        eq(projectMembers.userId, userId),
        eq(projectMembers.projectId, projectId),
      ),
    )
    .limit(1);
  return membership ?? null;
}

export async function canWriteProjectId(
  user: SessionUser,
  projectId: string,
): Promise<boolean> {
  return canWriteMembership(
    user,
    await getProjectMembership(user.id, projectId),
  );
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
  if (scope.projectId) {
    const rows = await db
      .select({ projectId: projectMembers.projectId })
      .from(projectMembers)
      .where(
        and(
          eq(projectMembers.userId, userId),
          eq(projectMembers.projectId, scope.projectId),
        ),
      );
    return rows.map((membership) => membership.projectId);
  }

  if (scope.spaceId) {
    const rows = await db
      .select({ projectId: projectMembers.projectId })
      .from(projectMembers)
      .innerJoin(projects, eq(projectMembers.projectId, projects.id))
      .where(
        and(
          eq(projectMembers.userId, userId),
          eq(projects.spaceId, scope.spaceId),
        ),
      );
    return rows.map((membership) => membership.projectId);
  }

  const rows = await db
    .select({ projectId: projectMembers.projectId })
    .from(projectMembers)
    .where(eq(projectMembers.userId, userId));
  return rows.map((membership) => membership.projectId);
}
