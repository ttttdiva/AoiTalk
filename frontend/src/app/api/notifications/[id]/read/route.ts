import { NextRequest, NextResponse } from "next/server";
import { db } from "@/db";
import { notificationDeliveries } from "@/db/schema";
import { and, eq } from "drizzle-orm";
import { getSession } from "@/lib/auth";

export async function POST(
  _request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const { id } = await params;

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
