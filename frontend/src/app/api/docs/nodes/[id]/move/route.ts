import { NextRequest, NextResponse } from "next/server";
import { and, eq, max } from "drizzle-orm";
import { db } from "@/db";
import { knowledgeNodePlacements, knowledgeNodes } from "@/db/schema";
import { getSession } from "@/lib/auth";
import {
  appendKnowledgeRevision,
  cleanOptionalString,
  getKnowledgeDisplayDescendantIds,
  getKnowledgeNodeDescendantIds,
  requireDocsNode,
  serializeNode,
} from "@/lib/server/knowledge-docs-utils";
import { updateDocsNode, updateDocsNodesByIds } from "@/lib/server/docs-node-writer";
import { getWritableProject } from "@/lib/server/project-access";
import { isDefaultInboxProject } from "@/lib/server/project-information-hierarchy";

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
  const newParentId = cleanOptionalString(body.new_parent_id, 80);
  if (!newParentId) {
    return NextResponse.json({ detail: "new_parent_idは必須です" }, { status: 400 });
  }
  if (newParentId === access.node.id) {
    return NextResponse.json({ detail: "自分自身を移動先にできません" }, { status: 400 });
  }
  const displayDescendantIds = await getKnowledgeDisplayDescendantIds(db, access.workspace.id, access.node.id);
  if (displayDescendantIds.includes(newParentId)) {
    return NextResponse.json({ detail: "子孫nodeへ移動すると階層が循環します" }, { status: 400 });
  }
  const descendantIds = await getKnowledgeNodeDescendantIds(db, access.workspace.id, access.node.id);

  const [parent] = await db
    .select()
    .from(knowledgeNodes)
    .where(and(eq(knowledgeNodes.id, newParentId), eq(knowledgeNodes.workspaceId, access.workspace.id)))
    .limit(1);
  if (!parent) {
    return NextResponse.json({ detail: "移動先nodeが見つかりません" }, { status: 404 });
  }
  if (parent.projectId) {
    const projectAccess = await getWritableProject(parent.projectId, user);
    if (!projectAccess) {
      return NextResponse.json(
        { detail: "移動先Projectへの書き込み権限がありません" },
        { status: 403 },
      );
    }
    if (isDefaultInboxProject(projectAccess.project)) {
      return NextResponse.json(
        { detail: "InboxはDocsの案件保存先ではありません" },
        { status: 409 },
      );
    }
  }

  const [maxRow] = await db
    .select({ maxSort: max(knowledgeNodes.sortOrder) })
    .from(knowledgeNodes)
    .where(eq(knowledgeNodes.parentId, newParentId));
  const oldParentId = access.node.parentId;
  const leaveReference = body.leave_reference === true;

  const updated = await db.transaction(async (tx) => {
    const row = await updateDocsNode(tx, access.node.id, {
        parentId: parent.id,
        rootPageId: parent.rootPageId ?? parent.id,
        projectId: parent.projectId,
        sortOrder: typeof body.sort_order === "number" ? body.sort_order : (maxRow?.maxSort ?? 0) + 1,
        updatedBy: user.id,
        updatedAt: new Date(),
      });
    if (leaveReference && oldParentId) {
      await tx
        .insert(knowledgeNodePlacements)
        .values({
          nodeId: access.node.id,
          parentNodeId: oldParentId,
          sortOrder: access.node.sortOrder ?? 0,
          collapsed: false,
          createdBy: user.id,
        })
        .onConflictDoNothing();
    }
    if (descendantIds.length > 0) {
      await updateDocsNodesByIds(tx, descendantIds, {
          rootPageId: row.rootPageId,
          projectId: row.projectId,
          updatedBy: user.id,
          updatedAt: new Date(),
        });
    }
    await appendKnowledgeRevision(tx, row, user.id, leaveReference ? "nodeを参照を残して移動" : "nodeを移動");
    return row;
  });

  return NextResponse.json({ node: serializeNode(updated) });
}
