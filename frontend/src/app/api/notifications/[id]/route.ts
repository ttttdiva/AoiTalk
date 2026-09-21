import { and, eq, ne } from "drizzle-orm";
import { NextRequest, NextResponse } from "next/server";
import { db } from "@/db";
import { notificationDeliveries } from "@/db/schema";
import { getSession } from "@/lib/auth";
import { isUuid } from "@/lib/knowledge-capture";
import { isProjectStewardNotification } from "@/lib/server/project-steward-notification-access";

export const dynamic = "force-dynamic";
export const fetchCache = "force-no-store";

function notFound() {
  return NextResponse.json(
    { detail: "通知が見つかりません" },
    { status: 404 },
  );
}

export async function GET(
  _request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const { id } = await params;
  const notificationId = id.trim();
  if (!isUuid(notificationId)) return notFound();

  const [notification] = await db
    .select()
    .from(notificationDeliveries)
    .where(
      and(
        eq(notificationDeliveries.id, notificationId),
        eq(notificationDeliveries.userId, user.id),
        eq(notificationDeliveries.channel, "in_app"),
        ne(notificationDeliveries.status, "cancelled"),
      ),
    )
    .limit(1);

  if (
    !notification ||
    notification.userId !== user.id ||
    notification.channel !== "in_app" ||
    notification.status === "cancelled" ||
    isProjectStewardNotification(notification)
  ) {
    return notFound();
  }

  return NextResponse.json({
    id: notification.id,
    project_id: notification.projectId,
    task_id: notification.taskId,
    occurrence_id: notification.occurrenceId,
    user_id: notification.userId,
    channel: notification.channel,
    notification_type: notification.notificationType,
    type: notification.notificationType,
    dedupe_key: notification.dedupeKey,
    title: notification.title,
    message: notification.message,
    scheduled_for: notification.scheduledFor,
    delivered_at: notification.deliveredAt,
    read_at: notification.readAt,
    is_read: notification.readAt !== null,
    status: notification.status,
    payload: notification.payload ?? {},
    created_at: notification.createdAt,
    updated_at: notification.updatedAt,
  });
}
