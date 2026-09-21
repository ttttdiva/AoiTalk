import { and, eq, inArray, isNull, ne } from "drizzle-orm";
import { db } from "@/db";
import { notificationDeliveries, projects } from "@/db/schema";

/**
 * The durable Project Steward notification contract is intentionally kept
 * small here.  Rows created by older workers may identify the notification
 * through either `notification_type` or the payload kind, so callers must use
 * this predicate rather than checking only one field.
 */
export type ProjectStewardNotificationScope = {
  projectId?: unknown;
  userId?: unknown;
  notificationType?: unknown;
  payload?: unknown;
};

function normalizeId(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const normalized = value.trim();
  return normalized || null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Return whether a delivery row is a Project Steward alert. */
export function isProjectStewardNotification(
  notification: ProjectStewardNotificationScope,
): boolean {
  const notificationType = normalizeId(notification.notificationType)?.toLowerCase();
  if (
    notificationType === "project_steward" ||
    notificationType === "project_steward_alert"
  ) {
    return true;
  }

  return (
    isRecord(notification.payload) &&
    notification.payload.kind === "project_steward_alert"
  );
}

/**
 * Load active project owners for a set of notification project ids.
 *
 * Missing rows and soft-deleted projects are deliberately absent from the
 * map.  This makes a missing map entry fail closed for Steward notifications
 * without changing visibility of ordinary task notifications.
 */
export async function getActiveProjectOwners(
  projectIds: readonly unknown[],
): Promise<Map<string, string>> {
  const normalizedProjectIds = [
    ...new Set(
      projectIds
        .map(normalizeId)
        .filter((projectId): projectId is string => projectId !== null),
    ),
  ];
  if (normalizedProjectIds.length === 0) return new Map();

  const rows = await db
    .select({ projectId: projects.id, ownerId: projects.ownerId })
    .from(projects)
    .where(
      and(
        inArray(projects.id, normalizedProjectIds),
        isNull(projects.deletedAt),
      ),
    );

  return new Map(
    rows.map((row) => [String(row.projectId), String(row.ownerId)]),
  );
}

/**
 * Revalidate the owner scope of one notification against the authenticated
 * user.  Ordinary task notifications are always considered current here.
 */
export function isCurrentProjectStewardNotification(
  notification: ProjectStewardNotificationScope,
  authenticatedUserId: string,
  activeProjectOwners: ReadonlyMap<string, string>,
): boolean {
  if (!isProjectStewardNotification(notification)) return true;

  const projectId = normalizeId(notification.projectId);
  const notificationUserId = normalizeId(notification.userId);
  if (!projectId || !notificationUserId) return false;

  return (
    notificationUserId === authenticatedUserId &&
    activeProjectOwners.get(projectId) === authenticatedUserId
  );
}

/**
 * Cancel unread, stale Steward rows before the Python read-all proxy mutates
 * anything.  The Python service performs the same check, but this BFF-side
 * cleanup closes the gap for mixed-version deployments and suppresses stale
 * pending rows from a later delivery attempt.  Ordinary task notifications
 * are never selected for this update.
 */
export async function cancelStaleProjectStewardNotifications(
  authenticatedUserId: string,
): Promise<number> {
  const candidates = await db
    .select({
      id: notificationDeliveries.id,
      projectId: notificationDeliveries.projectId,
      userId: notificationDeliveries.userId,
      notificationType: notificationDeliveries.notificationType,
      payload: notificationDeliveries.payload,
    })
    .from(notificationDeliveries)
    .where(
      and(
        eq(notificationDeliveries.userId, authenticatedUserId),
        eq(notificationDeliveries.channel, "in_app"),
        isNull(notificationDeliveries.readAt),
        ne(notificationDeliveries.status, "cancelled"),
      ),
    );

  const stewardCandidates = candidates.filter(isProjectStewardNotification);
  if (stewardCandidates.length === 0) return 0;

  const activeProjectOwners = await getActiveProjectOwners(
    stewardCandidates.map((candidate) => candidate.projectId),
  );
  const staleIds = stewardCandidates
    .filter(
      (candidate) =>
        !isCurrentProjectStewardNotification(
          candidate,
          authenticatedUserId,
          activeProjectOwners,
        ),
    )
    .map((candidate) => normalizeId(candidate.id))
    .filter((id): id is string => id !== null);

  if (staleIds.length === 0) return 0;

  await db
    .update(notificationDeliveries)
    .set({ status: "cancelled", updatedAt: new Date() })
    .where(
      and(
        inArray(notificationDeliveries.id, staleIds),
        eq(notificationDeliveries.userId, authenticatedUserId),
        eq(notificationDeliveries.channel, "in_app"),
        isNull(notificationDeliveries.readAt),
        ne(notificationDeliveries.status, "cancelled"),
      ),
    );

  return staleIds.length;
}

/**
 * Cancel every unread Project Steward row addressed to this user.
 *
 * The normal notification API must not mark Steward rows read.  During a
 * rolling deployment the Python read-all endpoint may still run an older
 * implementation, so the BFF cancels these legacy rows before proxying the
 * request.  The update is deliberately scoped by authenticated user and
 * in-app channel; ordinary task/system notifications are never selected.
 */
export async function cancelProjectStewardNotifications(
  authenticatedUserId: string,
): Promise<number> {
  const candidates = await db
    .select({
      id: notificationDeliveries.id,
      userId: notificationDeliveries.userId,
      notificationType: notificationDeliveries.notificationType,
      payload: notificationDeliveries.payload,
    })
    .from(notificationDeliveries)
    .where(
      and(
        eq(notificationDeliveries.userId, authenticatedUserId),
        eq(notificationDeliveries.channel, "in_app"),
        isNull(notificationDeliveries.readAt),
        ne(notificationDeliveries.status, "cancelled"),
      ),
    );

  const stewardIds = candidates
    .filter(isProjectStewardNotification)
    .map((candidate) => normalizeId(candidate.id))
    .filter((id): id is string => id !== null);
  if (stewardIds.length === 0) return 0;

  await db
    .update(notificationDeliveries)
    .set({ status: "cancelled", updatedAt: new Date() })
    .where(
      and(
        inArray(notificationDeliveries.id, stewardIds),
        eq(notificationDeliveries.userId, authenticatedUserId),
        eq(notificationDeliveries.channel, "in_app"),
        isNull(notificationDeliveries.readAt),
        ne(notificationDeliveries.status, "cancelled"),
      ),
    );

  return stewardIds.length;
}
