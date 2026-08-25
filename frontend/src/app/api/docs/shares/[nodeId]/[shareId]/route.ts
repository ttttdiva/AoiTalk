import { and, eq } from "drizzle-orm";
import { NextRequest, NextResponse } from "next/server";
import { db } from "@/db";
import { knowledgeNodeShares } from "@/db/schema";
import { getSession } from "@/lib/auth";
import { cleanString, getDocsNodeShareManager } from "@/lib/server/knowledge-docs-utils";

const PERMISSIONS = new Set(["read", "write"]);

export async function PATCH(
  request: NextRequest,
  { params }: { params: Promise<{ nodeId: string; shareId: string }> },
) {
  const user = await getSession();
  if (!user) return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  const { nodeId, shareId } = await params;
  const manager = await getDocsNodeShareManager(nodeId, user);
  if (!manager) return NextResponse.json({ detail: "共有設定を管理できません" }, { status: 403 });
  const body = await request.json().catch(() => ({}));
  const nextPermission = cleanString(body.permission, "", 16).toLowerCase();
  if (!PERMISSIONS.has(nextPermission)) {
    return NextResponse.json({ detail: "permission は read または write です" }, { status: 400 });
  }
  const [updated] = await db
    .update(knowledgeNodeShares)
    .set({ permission: nextPermission, updatedAt: new Date() })
    .where(and(eq(knowledgeNodeShares.id, shareId), eq(knowledgeNodeShares.nodeId, nodeId)))
    .returning();
  return updated
    ? NextResponse.json({ share: updated })
    : NextResponse.json({ detail: "共有設定が見つかりません" }, { status: 404 });
}

export async function DELETE(
  _request: NextRequest,
  { params }: { params: Promise<{ nodeId: string; shareId: string }> },
) {
  const user = await getSession();
  if (!user) return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  const { nodeId, shareId } = await params;
  const manager = await getDocsNodeShareManager(nodeId, user);
  if (!manager) return NextResponse.json({ detail: "共有設定を管理できません" }, { status: 403 });
  const deleted = await db
    .delete(knowledgeNodeShares)
    .where(and(eq(knowledgeNodeShares.id, shareId), eq(knowledgeNodeShares.nodeId, nodeId)))
    .returning({ id: knowledgeNodeShares.id });
  return deleted.length > 0
    ? NextResponse.json({ ok: true })
    : NextResponse.json({ detail: "共有設定が見つかりません" }, { status: 404 });
}
