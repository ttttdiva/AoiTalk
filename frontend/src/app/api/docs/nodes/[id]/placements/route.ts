import { NextRequest, NextResponse } from "next/server";
import { and, eq, max } from "drizzle-orm";
import { db } from "@/db";
import { knowledgeNodePlacements, knowledgeNodes } from "@/db/schema";
import { getSession } from "@/lib/auth";
import {
  cleanOptionalString,
  getKnowledgeDisplayDescendantIds,
  requireDocsNode,
  serializeNodePlacement,
} from "@/lib/server/knowledge-docs-utils";

export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const { id } = await params;
  const access = await requireDocsNode(id, user, "write");
  if (!access) {
    return NextResponse.json({ detail: "nodeが見つからないか権限がありません" }, { status: 404 });
  }

  const body = await request.json().catch(() => ({}));
  const parentNodeId = cleanOptionalString(body.parent_node_id, 80);
  if (!parentNodeId) {
    return NextResponse.json({ detail: "parent_node_idは必須です" }, { status: 400 });
  }
  const parentAccess = await requireDocsNode(parentNodeId, user, "write");
  if (!parentAccess || parentAccess.workspace.id !== access.workspace.id) {
    return NextResponse.json({ detail: "配置先nodeへの書き込み権限がありません" }, { status: 403 });
  }
  if (parentNodeId === access.node.id) {
    return NextResponse.json({ detail: "自分自身へ参照配置できません" }, { status: 400 });
  }
  const descendantIds = await getKnowledgeDisplayDescendantIds(db, access.workspace.id, access.node.id);
  if (descendantIds.includes(parentNodeId)) {
    return NextResponse.json({ detail: "子孫nodeへ参照配置すると表示階層が循環します" }, { status: 400 });
  }

  const [parent] = await db
    .select()
    .from(knowledgeNodes)
    .where(and(eq(knowledgeNodes.id, parentNodeId), eq(knowledgeNodes.docsLibraryId, access.workspace.id)))
    .limit(1);
  if (!parent) {
    return NextResponse.json({ detail: "配置先nodeが見つかりません" }, { status: 404 });
  }

  const [maxRow] = await db
    .select({ maxSort: max(knowledgeNodePlacements.sortOrder) })
    .from(knowledgeNodePlacements)
    .where(eq(knowledgeNodePlacements.parentNodeId, parentNodeId));

  const [placement] = await db
    .insert(knowledgeNodePlacements)
    .values({
      nodeId: access.node.id,
      parentNodeId,
      sortOrder: typeof body.sort_order === "number" ? body.sort_order : (maxRow?.maxSort ?? 0) + 1,
      collapsed: !!body.collapsed,
      createdBy: user.id,
    })
    .onConflictDoUpdate({
      target: [knowledgeNodePlacements.nodeId, knowledgeNodePlacements.parentNodeId],
      set: {
        sortOrder: typeof body.sort_order === "number" ? body.sort_order : (maxRow?.maxSort ?? 0) + 1,
        collapsed: !!body.collapsed,
      },
    })
    .returning();

  return NextResponse.json({ placement: serializeNodePlacement(placement) }, { status: 201 });
}

export async function DELETE(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }
  const { id } = await params;
  const access = await requireDocsNode(id, user, "write");
  if (!access) {
    return NextResponse.json({ detail: "nodeが見つからないか権限がありません" }, { status: 404 });
  }
  const parentNodeId = cleanOptionalString(request.nextUrl.searchParams.get("parent_node_id"), 80);
  if (!parentNodeId) {
    return NextResponse.json({ detail: "parent_node_idは必須です" }, { status: 400 });
  }
  const parentAccess = await requireDocsNode(parentNodeId, user, "write");
  if (!parentAccess || parentAccess.workspace.id !== access.workspace.id) {
    return NextResponse.json({ detail: "配置先nodeへの書き込み権限がありません" }, { status: 403 });
  }
  await db
    .delete(knowledgeNodePlacements)
    .where(
      and(
        eq(knowledgeNodePlacements.nodeId, access.node.id),
        eq(knowledgeNodePlacements.parentNodeId, parentNodeId),
      ),
    );
  return NextResponse.json({ ok: true });
}
