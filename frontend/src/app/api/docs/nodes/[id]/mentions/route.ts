import { NextRequest, NextResponse } from "next/server";
import { and, eq, inArray } from "drizzle-orm";
import { db } from "@/db";
import { notificationDeliveries, users } from "@/db/schema";
import { getSession } from "@/lib/auth";
import { requireDocsNode } from "@/lib/server/knowledge-docs-utils";

const USER_TOKEN_RE = /\[\[user:([0-9a-f-]{36})\|([^\]\n]+)\]\]/giu;

export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await getSession();
  if (!user) return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });

  const { id } = await params;
  const access = await requireDocsNode(id, user, "read");
  if (!access) return NextResponse.json({ detail: "nodeが見つかりません" }, { status: 404 });

  const body = await request.json().catch(() => ({}));
  const text = typeof body.title === "string" ? body.title : access.node.title;
  const mentionedIds = Array.from(new Set([...text.matchAll(USER_TOKEN_RE)].map((match) => match[1]).filter(Boolean)));
  if (mentionedIds.length === 0) return NextResponse.json({ delivered: 0 });
  if (!access.node.projectId) return NextResponse.json({ delivered: 0, skipped: "no_project" });

  const activeUsers = await db
    .select({ id: users.id })
    .from(users)
    .where(and(inArray(users.id, mentionedIds), eq(users.isActive, true)));
  const now = new Date();
  let delivered = 0;
  for (const target of activeUsers) {
    if (target.id === user.id) continue;
    const dedupeKey = `docs_mention:${access.node.id}:user:${target.id}:title:${Buffer.from(text).toString("base64url").slice(0, 80)}`;
    try {
      const existing = await db
        .select({ id: notificationDeliveries.id })
        .from(notificationDeliveries)
        .where(eq(notificationDeliveries.dedupeKey, dedupeKey))
        .limit(1);
      if (existing.length > 0) continue;
      await db.insert(notificationDeliveries).values({
        projectId: access.node.projectId,
        taskId: null,
        occurrenceId: null,
        userId: target.id,
        channel: "in_app",
        notificationType: "docs_mention",
        dedupeKey,
        title: "Docsでメンションされました",
        message: `${user.displayName || user.username} が「${access.node.title || "Untitled"}」であなたをメンションしました。`,
        scheduledFor: now,
        deliveredAt: now,
        status: "delivered",
        payload: { kind: "docs_mention", node_id: access.node.id },
        createdAt: now,
        updatedAt: now,
      });
      delivered += 1;
    } catch {
      // dedupeKeyのunique制約がない環境でも、通知失敗で編集保存を巻き戻さない。
    }
  }
  return NextResponse.json({ delivered });
}
