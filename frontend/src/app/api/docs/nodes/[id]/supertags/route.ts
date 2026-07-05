import { NextRequest, NextResponse } from "next/server";
import { and, eq, inArray } from "drizzle-orm";
import { db } from "@/db";
import { knowledgeNodeSupertags, knowledgeSupertags } from "@/db/schema";
import { getSession } from "@/lib/auth";
import { reconcileDocsTaskBinding } from "@/lib/server/docs-task-binding";
import {
  requireDocsNode,
  serializeNodeSupertag,
} from "@/lib/server/knowledge-docs-utils";

export async function PUT(
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
  const rawSupertagIds: string[] = Array.isArray(body.supertag_ids)
    ? body.supertag_ids.filter(
        (item: unknown): item is string => typeof item === "string",
      )
    : [];
  const supertagIds = Array.from(new Set(rawSupertagIds));
  const previousRows = await db
    .select({ supertagId: knowledgeNodeSupertags.supertagId })
    .from(knowledgeNodeSupertags)
    .where(eq(knowledgeNodeSupertags.nodeId, access.node.id));
  const previousSupertagIds = previousRows.map((row) => row.supertagId);

  const validTags =
    supertagIds.length > 0
      ? await db
          .select({ id: knowledgeSupertags.id })
          .from(knowledgeSupertags)
          .where(
            and(
              eq(knowledgeSupertags.workspaceId, access.workspace.id),
              inArray(knowledgeSupertags.id, supertagIds),
            ),
          )
      : [];

  const rows = await db.transaction(async (tx) => {
    await tx
      .delete(knowledgeNodeSupertags)
      .where(eq(knowledgeNodeSupertags.nodeId, access.node.id));
    if (validTags.length === 0) return [];
    return await tx
      .insert(knowledgeNodeSupertags)
      .values(
        validTags.map((tag) => ({
          nodeId: access.node.id,
          supertagId: tag.id,
          createdBy: user.id,
        })),
      )
      .returning();
  });

  try {
    await reconcileDocsTaskBinding({
      user,
      workspaceId: access.workspace.id,
      node: access.node,
      previousSupertagIds,
      nextSupertagIds: validTags.map((tag) => tag.id),
    });
  } catch (error) {
    return NextResponse.json(
      {
        detail: "Supertagは更新されましたが、タスク連携に失敗しました",
        error: error instanceof Error ? error.message : String(error),
      },
      { status: 502 },
    );
  }

  return NextResponse.json({ node_supertags: rows.map(serializeNodeSupertag) });
}
