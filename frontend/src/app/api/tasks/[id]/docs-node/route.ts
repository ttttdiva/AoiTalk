import { NextRequest, NextResponse } from "next/server";
import { and, eq, isNull, max } from "drizzle-orm";
import { db } from "@/db";
import {
  knowledgeNodes,
  knowledgeNodeSupertags,
  knowledgeSupertags,
  tasks,
} from "@/db/schema";
import { getSession } from "@/lib/auth";
import {
  appendKnowledgeRevision,
  cleanOptionalString,
  encryptNodeBodyJson,
  encryptNodeBodyText,
  ensureDocsWorkspace,
  ensureProjectWritable,
  serializeNode,
  syncKnowledgeNodeReferenceEdges,
  upsertKnowledgeSearchIndex,
} from "@/lib/server/knowledge-docs-utils";
import { fetchPythonApi } from "@/lib/server/python-api-proxy";

async function assertPythonOk(response: Response, action: string) {
  if (response.ok) return;
  const detail = await response.text().catch(() => "");
  throw new Error(`${action} failed: ${response.status} ${detail}`);
}

export async function POST(
  _request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const { id } = await params;
  const [task] = await db
    .select()
    .from(tasks)
    .where(and(eq(tasks.id, id), isNull(tasks.deletedAt)))
    .limit(1);
  if (!task) {
    return NextResponse.json({ detail: "タスクが見つかりません" }, { status: 404 });
  }

  const projectAccess = await ensureProjectWritable(task.projectId, user);
  if (!projectAccess) {
    return NextResponse.json(
      { detail: "Projectへの書き込み権限がありません" },
      { status: 403 },
    );
  }

  const workspace = await ensureDocsWorkspace(user);

  if (task.knowledgeNodeId) {
    const [linkedNode] = await db
      .select()
      .from(knowledgeNodes)
      .where(
        and(
          eq(knowledgeNodes.id, task.knowledgeNodeId),
          eq(knowledgeNodes.workspaceId, workspace.id),
          isNull(knowledgeNodes.archivedAt),
        ),
      )
      .limit(1);
    if (linkedNode) {
      return NextResponse.json({ node: serializeNode(linkedNode), created: false });
    }
  }

  const [taskTag] = await db
    .select()
    .from(knowledgeSupertags)
    .where(
      and(
        eq(knowledgeSupertags.workspaceId, workspace.id),
        eq(knowledgeSupertags.systemKey, "task"),
      ),
    )
    .limit(1);
  if (!taskTag) {
    return NextResponse.json(
      { detail: "#Task system tagが見つかりません" },
      { status: 500 },
    );
  }

  const parentId = projectAccess.project.knowledgeNodeId ?? null;
  const bodyText = cleanOptionalString(task.description, 200000) ?? "";
  const node = await db.transaction(async (tx) => {
    const [maxRow] = await tx
      .select({ maxSort: max(knowledgeNodes.sortOrder) })
      .from(knowledgeNodes)
      .where(
        and(
          eq(knowledgeNodes.workspaceId, workspace.id),
          parentId ? eq(knowledgeNodes.parentId, parentId) : isNull(knowledgeNodes.parentId),
        ),
      );

    const [created] = await tx
      .insert(knowledgeNodes)
      .values({
        workspaceId: workspace.id,
        parentId,
        rootPageId: parentId,
        projectId: task.projectId,
        title: task.title,
        description: task.description ?? "",
        bodyText: encryptNodeBodyText(bodyText),
        bodyJson: encryptNodeBodyJson({
          format: "task_note",
          task_id: task.id,
        }),
        nodeType: "node",
        displayProps: { show_checkbox: true },
        queryJson: null,
        viewJson: {},
        sortOrder: (maxRow?.maxSort ?? 0) + 1,
        createdBy: user.id,
        updatedBy: user.id,
      })
      .returning();

    const finalNode = created.rootPageId
      ? created
      : (
          await tx
            .update(knowledgeNodes)
            .set({ rootPageId: created.id, updatedAt: new Date(), updatedBy: user.id })
            .where(eq(knowledgeNodes.id, created.id))
            .returning()
        )[0];

    await tx.insert(knowledgeNodeSupertags).values({
      nodeId: finalNode.id,
      supertagId: taskTag.id,
      createdBy: user.id,
    });
    await upsertKnowledgeSearchIndex(tx, finalNode, bodyText);
    await syncKnowledgeNodeReferenceEdges(tx, { ...finalNode, bodyText }, user.id);
    await appendKnowledgeRevision(tx, finalNode, user.id, "タスクをDocsノート化");
    return finalNode;
  });

  try {
    const response = await fetchPythonApi(`/api/tasks/${task.id}`, {
      method: "PATCH",
      user,
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        knowledge_node_id: node.id,
        task_metadata: {
          ...(task.taskMetadata && typeof task.taskMetadata === "object" && !Array.isArray(task.taskMetadata)
            ? task.taskMetadata
            : {}),
          source: task.source,
          knowledge_node_id: node.id,
        },
      }),
    });
    await assertPythonOk(response, "Task Docs note link");
  } catch (error) {
    await db.delete(knowledgeNodes).where(eq(knowledgeNodes.id, node.id));
    return NextResponse.json(
      {
        detail: "Docs nodeは作成されましたが、タスク連携に失敗したため取り消しました",
        error: error instanceof Error ? error.message : String(error),
      },
      { status: 502 },
    );
  }

  return NextResponse.json({ node: serializeNode(node), created: true }, { status: 201 });
}
