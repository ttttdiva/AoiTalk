import { NextRequest, NextResponse } from "next/server";
import { db } from "@/db";
import { notificationDeliveries } from "@/db/schema";
import { and, eq } from "drizzle-orm";
import { getSession } from "@/lib/auth";
import { isProjectStewardNotification } from "@/lib/server/project-steward-notification-access";

export async function POST(
  _request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const { id } = await params;

  // Resolve the row before mutating it.  Project Steward results are
  // operational background work and are never readable through the normal
  // notification API.  Ordinary task notifications retain the existing
  // id+user update behavior below.
  const [notification] = await db
    .select({
      projectId: notificationDeliveries.projectId,
      userId: notificationDeliveries.userId,
      notificationType: notificationDeliveries.notificationType,
      payload: notificationDeliveries.payload,
    })
    .from(notificationDeliveries)
    .where(
      and(
        eq(notificationDeliveries.id, id),
        eq(notificationDeliveries.userId, user.id),
      ),
    )
    .limit(1);

  if (!notification) {
    return NextResponse.json(
      { detail: "通知が見つかりません" },
      { status: 404 },
    );
  }

  if (isProjectStewardNotification(notification)) {
    return NextResponse.json(
      { detail: "通知が見つかりません" },
      { status: 404 },
    );
  }

  const [updated] = await db
    .update(notificationDeliveries)
    .set({ readAt: new Date(), updatedAt: new Date() })
    .where(
      and(
        eq(notificationDeliveries.id, id),
        eq(notificationDeliveries.userId, user.id),
      ),
    )
    .returning();

  if (!updated) {
    return NextResponse.json(
      { detail: "通知が見つかりません" },
      { status: 404 }
    );
  }

  return NextResponse.json({ success: true });
}
