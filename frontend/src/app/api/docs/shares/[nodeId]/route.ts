import { and, eq, isNull, or } from "drizzle-orm";
import { NextRequest, NextResponse } from "next/server";
import { db } from "@/db";
import { knowledgeNodeShares, users } from "@/db/schema";
import { getSession } from "@/lib/auth";
import {
  cleanString,
  getDocsNodeShareManager,
} from "@/lib/server/knowledge-docs-utils";

const PERMISSIONS = new Set(["read", "write"]);

function permission(value: unknown) {
  const candidate = cleanString(value, "read", 16).toLowerCase();
  return PERMISSIONS.has(candidate) ? candidate : null;
}

function serializeShare(row: {
  share: typeof knowledgeNodeShares.$inferSelect;
  user: Pick<typeof users.$inferSelect, "id" | "username" | "displayName" | "email" | "avatarPath">;
}) {
  return {
    id: row.share.id,
    node_id: row.share.nodeId,
    user_id: row.share.userId,
    permission: row.share.permission,
    created_by: row.share.createdBy,
    created_at: row.share.createdAt,
    updated_at: row.share.updatedAt,
    user: {
      id: row.user.id,
      username: row.user.username,
      display_name: row.user.displayName,
      email: row.user.email,
      avatar_path: row.user.avatarPath,
    },
  };
}

export async function GET(
  _request: NextRequest,
  { params }: { params: Promise<{ nodeId: string }> },
) {
  const user = await getSession();
  if (!user) return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  const { nodeId } = await params;
  const manager = await getDocsNodeShareManager(nodeId, user);
  if (!manager) {
    return NextResponse.json({ detail: "共有設定を管理できません" }, { status: 403 });
  }
  const rows = await db
    .select({ share: knowledgeNodeShares, user: users })
    .from(knowledgeNodeShares)
    .innerJoin(users, eq(knowledgeNodeShares.userId, users.id))
    .where(eq(knowledgeNodeShares.nodeId, nodeId));
  return NextResponse.json({ shares: rows.map(serializeShare) });
}

export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ nodeId: string }> },
) {
  const user = await getSession();
  if (!user) return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  const { nodeId } = await params;
  const manager = await getDocsNodeShareManager(nodeId, user);
  if (!manager) {
    return NextResponse.json({ detail: "共有設定を管理できません" }, { status: 403 });
  }
  const body = await request.json().catch(() => ({}));
  const userId = cleanString(body.user_id ?? body.userId, "", 80);
  const selectedPermission = permission(body.permission);
  if (!userId || !selectedPermission) {
    return NextResponse.json(
      { detail: "user_id と permission(read/write) は必須です" },
      { status: 400 },
    );
  }
  const [targetUser] = await db
    .select({ id: users.id })
    .from(users)
    .where(
      and(eq(users.id, userId), or(eq(users.isActive, true), isNull(users.isActive))),
    )
    .limit(1);
  if (!targetUser) {
    return NextResponse.json({ detail: "ユーザーが見つかりません" }, { status: 404 });
  }

  const [share] = await db
    .insert(knowledgeNodeShares)
    .values({
      nodeId,
      userId,
      permission: selectedPermission,
      createdBy: user.id,
    })
    .onConflictDoUpdate({
      target: [knowledgeNodeShares.nodeId, knowledgeNodeShares.userId],
      set: { permission: selectedPermission, updatedAt: new Date(), createdBy: user.id },
    })
    .returning();
  const [withUser] = await db
    .select({ share: knowledgeNodeShares, user: users })
    .from(knowledgeNodeShares)
    .innerJoin(users, eq(knowledgeNodeShares.userId, users.id))
    .where(eq(knowledgeNodeShares.id, share.id))
    .limit(1);
  return NextResponse.json({ share: withUser ? serializeShare(withUser) : share }, { status: 201 });
}

export async function PATCH(
  request: NextRequest,
  { params }: { params: Promise<{ nodeId: string }> },
) {
  const user = await getSession();
  if (!user) return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  const { nodeId } = await params;
  const manager = await getDocsNodeShareManager(nodeId, user);
  if (!manager) return NextResponse.json({ detail: "共有設定を管理できません" }, { status: 403 });
  const body = await request.json().catch(() => ({}));
  const targetUserId = cleanString(body.user_id ?? body.userId, "", 80);
  const nextPermission = permission(body.permission);
  if (!targetUserId || !nextPermission) {
    return NextResponse.json({ detail: "user_id と permission(read/write) は必須です" }, { status: 400 });
  }
  const [updated] = await db
    .update(knowledgeNodeShares)
    .set({ permission: nextPermission, updatedAt: new Date() })
    .where(and(eq(knowledgeNodeShares.nodeId, nodeId), eq(knowledgeNodeShares.userId, targetUserId)))
    .returning();
  return updated
    ? NextResponse.json({ share: updated })
    : NextResponse.json({ detail: "共有設定が見つかりません" }, { status: 404 });
}

export async function DELETE(
  request: NextRequest,
  { params }: { params: Promise<{ nodeId: string }> },
) {
  const user = await getSession();
  if (!user) return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  const { nodeId } = await params;
  const manager = await getDocsNodeShareManager(nodeId, user);
  if (!manager) return NextResponse.json({ detail: "共有設定を管理できません" }, { status: 403 });
  const shareId = cleanString(request.nextUrl.searchParams.get("share_id"), "", 80);
  const targetUserId = cleanString(request.nextUrl.searchParams.get("user_id"), "", 80);
  const predicate = shareId
    ? and(eq(knowledgeNodeShares.id, shareId), eq(knowledgeNodeShares.nodeId, nodeId))
    : targetUserId
      ? and(eq(knowledgeNodeShares.userId, targetUserId), eq(knowledgeNodeShares.nodeId, nodeId))
      : null;
  if (!predicate) return NextResponse.json({ detail: "share_id または user_id が必要です" }, { status: 400 });
  const deleted = await db.delete(knowledgeNodeShares).where(predicate).returning({ id: knowledgeNodeShares.id });
  return deleted.length > 0
    ? NextResponse.json({ ok: true })
    : NextResponse.json({ detail: "共有設定が見つかりません" }, { status: 404 });
}
