import { NextRequest, NextResponse } from "next/server";
import { and, eq, inArray, sql } from "drizzle-orm";
import { db } from "@/db";
import {
  knowledgeNodes,
  knowledgeNodeSupertags,
  knowledgeSupertags,
} from "@/db/schema";
import { getSession } from "@/lib/auth";
import { reconcileDocsTaskBinding } from "@/lib/server/docs-task-binding";
import {
  requireDocsNode,
  serializeNode,
  serializeNodeSupertag,
  upsertKnowledgeSearchIndex,
} from "@/lib/server/knowledge-docs-utils";
import { insertDocsNode } from "@/lib/server/docs-node-writer";
import {
  lockAndAssertGenericDocsMutationAllowed,
  ManagedDocsMutationError,
} from "@/lib/server/managed-docs-policy";

class FilmWarehouseSupertagError extends Error {}

function templateLines(templateJson: unknown): string[] {
  const record =
    templateJson &&
    typeof templateJson === "object" &&
    !Array.isArray(templateJson)
      ? (templateJson as Record<string, unknown>)
      : {};
  const blocks = Array.isArray(record.blocks) ? record.blocks : [];
  return blocks
    .map((block) =>
      block && typeof block === "object"
        ? String((block as Record<string, unknown>).text ?? "").trim()
        : "",
    )
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
    return NextResponse.json(
      { detail: "nodeが見つからないか権限がありません" },
      { status: 404 },
    );
  }

  const body = await request.json().catch(() => ({}));
  const rawSupertagIds: string[] = Array.isArray(body.supertag_ids)
    ? body.supertag_ids.filter(
        (item: unknown): item is string => typeof item === "string",
      )
    : [];
  const rawAddSupertagIds: string[] = Array.isArray(body.add_supertag_ids)
    ? body.add_supertag_ids.filter(
        (item: unknown): item is string => typeof item === "string",
      )
    : [];
  const rawRemoveSupertagIds: string[] = Array.isArray(body.remove_supertag_ids)
    ? body.remove_supertag_ids.filter(
        (item: unknown): item is string => typeof item === "string",
      )
    : [];
  let previousSupertagIds: string[] = [];
  const supertagIds = Array.from(new Set(rawSupertagIds));
  const idsToValidate =
    rawAddSupertagIds.length > 0 ? rawAddSupertagIds : supertagIds;

  const validTags =
    idsToValidate.length > 0
      ? await db
          .select()
          .from(knowledgeSupertags)
          .where(
            and(
              eq(knowledgeSupertags.docsLibraryId, access.workspace.id),
              inArray(knowledgeSupertags.id, idsToValidate),
            ),
          )
      : [];
  const validRemoveTags =
    rawRemoveSupertagIds.length > 0
      ? await db
          .select({ id: knowledgeSupertags.id })
          .from(knowledgeSupertags)
          .where(
            and(
              eq(knowledgeSupertags.docsLibraryId, access.workspace.id),
              inArray(knowledgeSupertags.id, rawRemoveSupertagIds),
            ),
          )
      : [];
  if (validTags.length !== new Set(idsToValidate).size) {
    return NextResponse.json(
      { detail: "指定されたSupertagは削除済みか利用できません" },
      { status: 409 },
    );
  }
  if (validRemoveTags.length !== new Set(rawRemoveSupertagIds).size) {
    return NextResponse.json(
      { detail: "削除対象のSupertagは削除済みか別のDocs Libraryです" },
      { status: 409 },
    );
  }

  let rows: Array<typeof knowledgeNodeSupertags.$inferSelect>;
  let createdNodes: Array<typeof knowledgeNodes.$inferSelect>;
  try {
    const result = await db.transaction(async (tx) => {
      await lockAndAssertGenericDocsMutationAllowed(access.node, tx);
      const previousRows = await tx
        .select({ supertagId: knowledgeNodeSupertags.supertagId })
        .from(knowledgeNodeSupertags)
        .innerJoin(knowledgeSupertags, eq(knowledgeNodeSupertags.supertagId, knowledgeSupertags.id))
        .where(
          and(
            eq(knowledgeNodeSupertags.nodeId, access.node.id),
            eq(knowledgeSupertags.docsLibraryId, access.workspace.id),
          ),
        );
      previousSupertagIds = previousRows.map((row) => row.supertagId);
      if (validTags.some((tag) => tag.name.trim() === "倉庫")) {
        const filmAncestors = await tx.execute(sql`
        with recursive ancestors as (
          select id,parent_id,system_key,docs_library_id,
                 array[id]::uuid[] as visited_path, 0 as depth
          from knowledge_nodes
          where id=${access.node.id} and docs_library_id=${access.workspace.id}
          union all
          select parent.id,parent.parent_id,parent.system_key,parent.docs_library_id,
                 child.visited_path || array[parent.id]::uuid[], child.depth + 1
          from knowledge_nodes parent
          join ancestors child on child.parent_id=parent.id
          where parent.docs_library_id=${access.workspace.id}
            and child.depth < 512
            and not parent.id = any(child.visited_path)
        )
        select id from ancestors
        where system_key='foam_source_grounded_v1:root.Film'
        limit 1
      `);
        if (filmAncestors.length > 0) {
          throw new FilmWarehouseSupertagError();
        }
      }
      let nextRows: Array<typeof knowledgeNodeSupertags.$inferSelect>;
      if (rawRemoveSupertagIds.length > 0) {
        await tx
          .delete(knowledgeNodeSupertags)
          .where(
            and(
              eq(knowledgeNodeSupertags.nodeId, access.node.id),
              inArray(knowledgeNodeSupertags.supertagId, validRemoveTags.map((tag) => tag.id)),
            ),
          );
        if (rawAddSupertagIds.length > 0 && validTags.length > 0) {
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
        nextRows = await tx
          .select({
            relation: knowledgeNodeSupertags,
            supertagWorkspaceId: knowledgeSupertags.docsLibraryId,
          })
          .from(knowledgeNodeSupertags)
          .innerJoin(knowledgeSupertags, eq(knowledgeNodeSupertags.supertagId, knowledgeSupertags.id))
          .where(
            and(
              eq(knowledgeNodeSupertags.nodeId, access.node.id),
              eq(knowledgeSupertags.docsLibraryId, access.workspace.id),
            ),
          )
          .then((rows) => rows
            .filter((row) => row.supertagWorkspaceId === access.workspace.id)
            .map((row) => row.relation));
      } else if (rawAddSupertagIds.length > 0) {
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
        nextRows = await tx
          .select({
            relation: knowledgeNodeSupertags,
            supertagWorkspaceId: knowledgeSupertags.docsLibraryId,
          })
          .from(knowledgeNodeSupertags)
          .innerJoin(knowledgeSupertags, eq(knowledgeNodeSupertags.supertagId, knowledgeSupertags.id))
          .where(
            and(
              eq(knowledgeNodeSupertags.nodeId, access.node.id),
              eq(knowledgeSupertags.docsLibraryId, access.workspace.id),
            ),
          )
          .then((rows) => rows
            .filter((row) => row.supertagWorkspaceId === access.workspace.id)
            .map((row) => row.relation));
      } else {
        await tx
          .delete(knowledgeNodeSupertags)
          .where(eq(knowledgeNodeSupertags.nodeId, access.node.id));
        nextRows = validTags.length === 0
          ? []
          : await tx
              .insert(knowledgeNodeSupertags)
              .values(
                validTags.map((tag) => ({
                  nodeId: access.node.id,
                  supertagId: tag.id,
                  createdBy: user.id,
                })),
              )
              .returning();
      }

      const newlyApplied = validTags.filter(
        (tag) => !previousSupertagIds.includes(tag.id),
      );
      const existingChildren = await tx
        .select({ id: knowledgeNodes.id })
        .from(knowledgeNodes)
        .where(
          and(
            eq(knowledgeNodes.parentId, access.node.id),
            eq(knowledgeNodes.docsLibraryId, access.workspace.id),
          ),
        )
        .limit(1);
      const nextCreatedNodes: Array<typeof knowledgeNodes.$inferSelect> = [];
      if (existingChildren.length === 0) {
        for (const tag of newlyApplied) {
          const lines = templateLines(tag.templateJson);
          if (lines.length === 0) continue;
          for (const [index, line] of lines.entries()) {
            const child = await insertDocsNode(tx, {
              docsLibraryId: access.workspace.id,
              parentId: access.node.id,
              rootPageId: access.node.rootPageId ?? access.node.id,
              projectId: access.node.projectId,
              title: line,
              bodyJson: {
                format: "doc_block",
                block_type: index === 0 ? "heading_2" : "paragraph",
              },
              nodeType: "node",
              displayProps: {},
              sortOrder: index + 1,
              createdBy: user.id,
              updatedBy: user.id,
            });
            await upsertKnowledgeSearchIndex(tx, child, child.title);
            nextCreatedNodes.push(child);
          }
          break;
        }
      }
      return { rows: nextRows, createdNodes: nextCreatedNodes };
    });
    rows = result.rows;
    createdNodes = result.createdNodes;
  } catch (error) {
    if (error instanceof FilmWarehouseSupertagError) {
      return NextResponse.json(
        { detail: "Film配下へ倉庫Supertagは付けられません" },
        { status: 409 },
      );
    }
    if (error instanceof ManagedDocsMutationError) {
      return NextResponse.json({ detail: error.message }, { status: error.status });
    }
    throw error;
  }

  let taskBindingError: string | null = null;
  try {
    await reconcileDocsTaskBinding({
      user,
      docsLibraryId: access.workspace.id,
      node: access.node,
      previousSupertagIds,
      nextSupertagIds: rows.map((row) => row.supertagId),
    });
  } catch (error) {
    // relation変更は既にcommit済み。未適用を装う502を返すとclientが表示だけ
    // rollbackしてDBと不一致になるため、確定状態と部分失敗を同時に返す。
    taskBindingError = error instanceof Error ? error.message : String(error);
  }

  return NextResponse.json({
    node_supertags: rows.map(serializeNodeSupertag),
    nodes: createdNodes.map(serializeNode),
    committed: true,
    task_binding_error: taskBindingError,
  });
}
