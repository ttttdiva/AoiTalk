import { NextRequest, NextResponse } from "next/server";
import { and, eq, inArray } from "drizzle-orm";
import { db } from "@/db";
import { knowledgeNodes, knowledgeNodeSupertags, knowledgeSupertags } from "@/db/schema";
import { getSession } from "@/lib/auth";
import { reconcileDocsTaskBinding } from "@/lib/server/docs-task-binding";
import {
  requireDocsNode,
  serializeNode,
  serializeNodeSupertag,
  upsertKnowledgeSearchIndex,
} from "@/lib/server/knowledge-docs-utils";
import { insertDocsNode } from "@/lib/server/docs-node-writer";

function templateLines(templateJson: unknown): string[] {
  const record = templateJson && typeof templateJson === "object" && !Array.isArray(templateJson)
    ? templateJson as Record<string, unknown>
    : {};
  const blocks = Array.isArray(record.blocks) ? record.blocks : [];
  return blocks
    .map((block) => block && typeof block === "object" ? String((block as Record<string, unknown>).text ?? "").trim() : "")
    .filter(Boolean);
}

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
  const rawAddSupertagIds: string[] = Array.isArray(body.add_supertag_ids)
    ? body.add_supertag_ids.filter((item: unknown): item is string => typeof item === "string")
    : [];
  const rawRemoveSupertagIds: string[] = Array.isArray(body.remove_supertag_ids)
    ? body.remove_supertag_ids.filter((item: unknown): item is string => typeof item === "string")
    : [];
  const previousRows = await db
    .select({ supertagId: knowledgeNodeSupertags.supertagId })
    .from(knowledgeNodeSupertags)
    .where(eq(knowledgeNodeSupertags.nodeId, access.node.id));
  const previousSupertagIds = previousRows.map((row) => row.supertagId);
  const supertagIds = Array.from(new Set(rawSupertagIds));
  const idsToValidate = rawAddSupertagIds.length > 0 ? rawAddSupertagIds : supertagIds;

  const validTags =
    idsToValidate.length > 0
      ? await db
          .select()
          .from(knowledgeSupertags)
          .where(
            and(
              eq(knowledgeSupertags.workspaceId, access.workspace.id),
              inArray(knowledgeSupertags.id, idsToValidate),
            ),
          )
      : [];
  if (validTags.length !== new Set(idsToValidate).size) {
    return NextResponse.json({ detail: "指定されたSupertagは削除済みか利用できません" }, { status: 409 });
  }

  const rows = await db.transaction(async (tx) => {
    if (rawRemoveSupertagIds.length > 0) {
      await tx.delete(knowledgeNodeSupertags).where(and(
        eq(knowledgeNodeSupertags.nodeId, access.node.id),
        inArray(knowledgeNodeSupertags.supertagId, rawRemoveSupertagIds),
      ));
      if (rawAddSupertagIds.length > 0 && validTags.length > 0) {
        await tx.insert(knowledgeNodeSupertags).values(validTags.map((tag) => ({
          nodeId: access.node.id,
          supertagId: tag.id,
          createdBy: user.id,
        }))).onConflictDoNothing();
      }
      return tx.select().from(knowledgeNodeSupertags).where(eq(knowledgeNodeSupertags.nodeId, access.node.id));
    }
    if (rawAddSupertagIds.length > 0) {
      if (validTags.length > 0) {
        await tx
          .insert(knowledgeNodeSupertags)
          .values(
            validTags.map((tag) => ({
              nodeId: access.node.id,
              supertagId: tag.id,
              createdBy: user.id,
            })),
          )
          .onConflictDoNothing();
      }
      return tx
        .select()
        .from(knowledgeNodeSupertags)
        .where(eq(knowledgeNodeSupertags.nodeId, access.node.id));
    }
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

  let taskBindingError: string | null = null;
  try {
    await reconcileDocsTaskBinding({
      user,
      workspaceId: access.workspace.id,
      node: access.node,
      previousSupertagIds,
      nextSupertagIds: rows.map((row) => row.supertagId),
    });
  } catch (error) {
    // relation変更は既にcommit済み。未適用を装う502を返すとclientが表示だけ
    // rollbackしてDBと不一致になるため、確定状態と部分失敗を同時に返す。
    taskBindingError = error instanceof Error ? error.message : String(error);
  }

  const newlyApplied = validTags.filter((tag) => !previousSupertagIds.includes(tag.id));
  const existingChildren = await db
    .select({ id: knowledgeNodes.id })
    .from(knowledgeNodes)
    .where(eq(knowledgeNodes.parentId, access.node.id))
    .limit(1);
  const createdNodes: Array<typeof knowledgeNodes.$inferSelect> = [];
  if (existingChildren.length === 0) {
    for (const tag of newlyApplied) {
      const lines = templateLines(tag.templateJson);
      if (lines.length === 0) continue;
      for (const [index, line] of lines.entries()) {
        const child = await insertDocsNode(db, {
          workspaceId: access.workspace.id,
          parentId: access.node.id,
          rootPageId: access.node.rootPageId ?? access.node.id,
          projectId: access.node.projectId,
          title: line,
          bodyJson: { format: "doc_block", block_type: index === 0 ? "heading_2" : "paragraph" },
          nodeType: "node",
          displayProps: {},
          sortOrder: index + 1,
          createdBy: user.id,
          updatedBy: user.id,
        });
        await upsertKnowledgeSearchIndex(db, child, child.title);
        createdNodes.push(child);
      }
      break;
    }
  }

  return NextResponse.json({
    node_supertags: rows.map(serializeNodeSupertag),
    nodes: createdNodes.map(serializeNode),
    committed: true,
    task_binding_error: taskBindingError,
  });
}
