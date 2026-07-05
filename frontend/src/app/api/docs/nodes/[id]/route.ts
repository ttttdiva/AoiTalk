import { NextRequest, NextResponse } from "next/server";
import { and, eq, inArray } from "drizzle-orm";
import { db } from "@/db";
import { knowledgeNodes } from "@/db/schema";
import { getSession } from "@/lib/auth";
import { normalizeDocsNodeType } from "@/lib/docs-model";
import {
  appendKnowledgeRevision,
  cleanOptionalString,
  cleanString,
  decryptNodeBodyText,
  encryptNodeBodyJson,
  encryptNodeBodyText,
  ensureProjectWritable,
  getKnowledgeDisplayDescendantIds,
  getKnowledgeNodeDescendantIds,
  normalizeJsonObject,
  requireDocsNode,
  serializeNode,
  syncKnowledgeNodeReferenceEdges,
  upsertKnowledgeSearchIndex,
} from "@/lib/server/knowledge-docs-utils";
import {
  syncDocsTaskTitle,
  unlinkDocsTaskBinding,
} from "@/lib/server/docs-task-binding";

export async function PATCH(
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
  const updates: Partial<typeof knowledgeNodes.$inferInsert> = {
    updatedBy: user.id,
    updatedAt: new Date(),
  };
  let bodyTextPlain = decryptNodeBodyText(access.node.bodyText ?? "");

  if ("title" in body) {
    updates.title = cleanString(body.title, access.node.title, 500);
  }
  if ("description" in body) {
    updates.description = cleanOptionalString(body.description, 200000) ?? "";
  }
  if ("body_text" in body) {
    bodyTextPlain = cleanOptionalString(body.body_text, 200000) ?? "";
    updates.bodyText = encryptNodeBodyText(bodyTextPlain);
  }
  if ("body_json" in body) {
    updates.bodyJson = encryptNodeBodyJson(normalizeJsonObject(body.body_json));
  }
  const nextNodeType = "node_type" in body
    ? normalizeDocsNodeType(body.node_type)
    : normalizeDocsNodeType(access.node.nodeType);
  if ("node_type" in body) {
    updates.nodeType = nextNodeType;
  }
  if ("display_props" in body) {
    updates.displayProps = normalizeJsonObject(body.display_props);
  }
  if ("query_json" in body) {
    updates.queryJson = nextNodeType === "search" ? normalizeJsonObject(body.query_json) : null;
  } else if ("node_type" in body && nextNodeType !== "search") {
    updates.queryJson = null;
  }
  if ("view_json" in body) {
    updates.viewJson = normalizeJsonObject(body.view_json);
  }
  if ("day_date" in body) {
    updates.dayDate = cleanOptionalString(body.day_date, 40) ?? null;
  }
  if ("sort_order" in body) {
    const sortOrder = Number(body.sort_order);
    if (!Number.isFinite(sortOrder)) {
      return NextResponse.json({ detail: "sort_orderが不正です" }, { status: 400 });
    }
    updates.sortOrder = sortOrder;
  }
  if ("project_id" in body) {
    const projectId = cleanOptionalString(body.project_id, 80);
    if (projectId) {
      const projectAccess = await ensureProjectWritable(projectId, user);
      if (!projectAccess) {
        return NextResponse.json(
          { detail: "Projectへの書き込み権限がありません" },
          { status: 403 },
        );
      }
    }
    updates.projectId = projectId;
  }
  if ("archived" in body && body.archived === false) {
    updates.archivedAt = null;
  }

  let descendantIds: string[] = [];
  if ("parent_id" in body) {
    const parentId = cleanOptionalString(body.parent_id, 80);
    if (parentId === access.node.id) {
      return NextResponse.json({ detail: "自分自身を親にはできません" }, { status: 400 });
    }
    const displayDescendantIds = await getKnowledgeDisplayDescendantIds(
      db,
      access.workspace.id,
      access.node.id,
    );
    if (parentId && displayDescendantIds.includes(parentId)) {
      return NextResponse.json(
        { detail: "子孫nodeを親にすると階層が破綻します" },
        { status: 400 },
      );
    }
    descendantIds = await getKnowledgeNodeDescendantIds(
      db,
      access.workspace.id,
      access.node.id,
    );
    if (parentId) {
      const [parent] = await db
        .select()
        .from(knowledgeNodes)
        .where(
          and(
            eq(knowledgeNodes.id, parentId),
            eq(knowledgeNodes.workspaceId, access.workspace.id),
          ),
        )
        .limit(1);
      if (!parent) {
        return NextResponse.json({ detail: "親nodeが見つかりません" }, { status: 404 });
      }
      if (parent.projectId) {
        const projectAccess = await ensureProjectWritable(parent.projectId, user);
        if (!projectAccess) {
          return NextResponse.json(
            { detail: "親nodeのProject書き込み権限がありません" },
            { status: 403 },
          );
        }
      }
      updates.parentId = parent.id;
      updates.rootPageId = parent.rootPageId ?? parent.id;
      if (!updates.projectId && parent.projectId) {
        updates.projectId = parent.projectId;
      }
    } else {
      updates.parentId = null;
      updates.rootPageId = access.node.id;
    }
  }

  const updated = await db.transaction(async (tx) => {
    const [row] = await tx
      .update(knowledgeNodes)
      .set(updates)
      .where(eq(knowledgeNodes.id, id))
      .returning();

    if ("parent_id" in body && descendantIds.length > 0) {
      const descendantUpdates: Partial<typeof knowledgeNodes.$inferInsert> = {
        rootPageId: row.rootPageId,
        updatedBy: user.id,
        updatedAt: new Date(),
      };
      if (updates.projectId !== undefined) {
        descendantUpdates.projectId = row.projectId;
      }
      await tx
        .update(knowledgeNodes)
        .set(descendantUpdates)
        .where(inArray(knowledgeNodes.id, descendantIds));
    }

    await upsertKnowledgeSearchIndex(tx, row, bodyTextPlain);
    await syncKnowledgeNodeReferenceEdges(tx, { ...row, bodyText: bodyTextPlain }, user.id);
    await appendKnowledgeRevision(tx, row, user.id, "nodeを更新");
    return row;
  });

  if ("title" in body && updated.title !== access.node.title) {
    try {
      await syncDocsTaskTitle({
        user,
        nodeId: updated.id,
        title: updated.title,
      });
    } catch (err) {
      return NextResponse.json(
        { detail: "タスクタイトル同期に失敗しました", error: String(err) },
        { status: 502 },
      );
    }
  }

  return NextResponse.json({ node: serializeNode(updated) });
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

  if (request.nextUrl.searchParams.get("permanent") === "1") {
    await db.delete(knowledgeNodes).where(eq(knowledgeNodes.id, id));
    return NextResponse.json({ ok: true });
  }

  const [updated] = await db
    .update(knowledgeNodes)
    .set({ archivedAt: new Date(), updatedBy: user.id, updatedAt: new Date() })
    .where(eq(knowledgeNodes.id, id))
    .returning();

  await appendKnowledgeRevision(db, updated, user.id, "nodeをアーカイブ");

  try {
    await unlinkDocsTaskBinding({ user, nodeId: updated.id });
  } catch (err) {
    return NextResponse.json(
      { detail: "タスク連携解除に失敗しました", error: String(err) },
      { status: 502 },
    );
  }

  return NextResponse.json({ node: serializeNode(updated) });
}
