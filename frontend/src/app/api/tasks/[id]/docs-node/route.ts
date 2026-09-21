import { NextRequest, NextResponse } from "next/server";
import { and, asc, eq, inArray, isNull, max } from "drizzle-orm";
import { db } from "@/db";
import {
  knowledgeNodes,
  knowledgeNodeSupertags,
  knowledgeSupertags,
  tasks,
  projects,
} from "@/db/schema";
import { getSession } from "@/lib/auth";
import {
  appendKnowledgeRevision,
  cleanOptionalString,
  ensureProjectDocsWorkspace,
  ensureProjectWritable,
  getKnowledgeNodeDescendantIds,
  serializeNode,
  syncKnowledgeNodeReferenceEdges,
  upsertKnowledgeSearchIndex,
} from "@/lib/server/knowledge-docs-utils";
import { insertDocsNode, updateDocsNode, updateDocsNodesByIds } from "@/lib/server/docs-node-writer";
import {
  ensureProjectInformationHierarchyNode,
  isDefaultInboxProject,
  lockProjectInformationAdvisory,
} from "@/lib/server/project-information-hierarchy";
import {
  assertTaskDocsNodeLinkAllowed,
  assertTaskDocsNodeLinkAllowedInTransaction,
  assertTaskProjectAccessInTransaction,
  TaskDocsNodeInvariantError,
  TaskProjectAccessInvariantError,
} from "@/lib/server/task-docs-node-invariant";
import { lockTaskProjectIds } from "@/lib/server/project-move-dependency-invariant";

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

  const workspace = await ensureProjectDocsWorkspace(task.projectId, user);
  if (!workspace) {
    return NextResponse.json(
      { detail: "Project Docs workspaceへの書き込み権限がありません" },
      { status: 403 },
    );
  }
  if (isDefaultInboxProject(projectAccess.project)) {
    return NextResponse.json(
      { detail: "Inboxタスクは案件情報Docsへ変換できません。実案件へ移してから実行してください" },
      { status: 409 },
    );
  }
  if (task.knowledgeNodeId) {
    try {
      const link = await assertTaskDocsNodeLinkAllowed(
        task.knowledgeNodeId,
        String(task.projectId),
        user,
      );
      if (link.workspace.id !== workspace.id) {
        throw new TaskDocsNodeInvariantError("別Docs LibraryのnodeはこのProjectへ移動できません");
      }
    } catch (error) {
      if (error instanceof TaskDocsNodeInvariantError) {
        return NextResponse.json({ detail: error.message }, { status: error.status });
      }
      throw error;
    }
    const [linkedNode] = await db
      .select()
      .from(knowledgeNodes)
      .where(
        and(
          eq(knowledgeNodes.id, task.knowledgeNodeId),
          eq(knowledgeNodes.docsLibraryId, workspace.id),
          isNull(knowledgeNodes.archivedAt),
        ),
      )
      .limit(1);
    if (linkedNode) {
      let repaired: typeof linkedNode;
      try {
        repaired = await db.transaction(async (tx) => {
        // Match every task/dependency writer's advisory -> Project -> Task
        // order before entering the canonical hierarchy repair.
        await lockTaskProjectIds(tx, [task.projectId]);
        await lockProjectInformationAdvisory(tx, task.projectId);
        const [lockedProject] = await tx
          .select()
          .from(projects)
          .where(eq(projects.id, task.projectId))
          .limit(1)
          .for("update");
        if (!lockedProject || lockedProject.deletedAt || lockedProject.isCompleted) {
          throw new Error("完了/削除済みProjectのDocs nodeは修復できません");
        }
        await assertTaskProjectAccessInTransaction(tx, [task.projectId], user);
        // Re-resolve the canonical hierarchy while the Project row is held;
        // the preflight projectNode may have been repaired/reparented before
        // this transaction acquired its lock.
        const lockedProjectNode = await ensureProjectInformationHierarchyNode({
          docsLibraryId: workspace.id,
          userId: user.id,
          project: lockedProject,
          client: tx,
        });
        const [lockedTask] = await tx
          .select({
            knowledgeNodeId: tasks.knowledgeNodeId,
            projectId: tasks.projectId,
          })
          .from(tasks)
          .where(eq(tasks.id, task.id))
          .for("update")
          .limit(1);
        if (!lockedTask || lockedTask.knowledgeNodeId !== linkedNode.id) {
          throw new Error("タスクのDocs node bindingが同時変更されたため修復できません");
        }
        if (String(lockedTask.projectId) !== String(lockedProject.id)) {
          throw new TaskDocsNodeInvariantError(
            "タスクのProjectが同時変更されたためDocs nodeを修復できません",
          );
        }
        let [lockedNode] = await tx
          .select()
          .from(knowledgeNodes)
          .where(and(eq(knowledgeNodes.id, linkedNode.id), eq(knowledgeNodes.docsLibraryId, workspace.id)))
          .limit(1);
        if (!lockedNode) throw new Error("既存のDocs nodeが見つかりません");
        // Lock the complete bound-node closure in deterministic order before
        // the managed-policy walk and bulk denormalizer update.  A concurrent
        // generic move otherwise can hold a child while this repair holds the
        // root (or vice versa), producing a 40P01 cycle and stale descendants.
        const descendants = await getKnowledgeNodeDescendantIds(tx, workspace.id, lockedNode.id);
        const closureIds = Array.from(new Set([lockedNode.id, ...descendants])).sort();
        await tx
          .select({ id: knowledgeNodes.id })
          .from(knowledgeNodes)
          .where(
            and(
              eq(knowledgeNodes.docsLibraryId, workspace.id),
              inArray(knowledgeNodes.id, closureIds),
            ),
          )
          .orderBy(asc(knowledgeNodes.id))
          .for("update");
        [lockedNode] = await tx
          .select()
          .from(knowledgeNodes)
          .where(and(eq(knowledgeNodes.id, linkedNode.id), eq(knowledgeNodes.docsLibraryId, workspace.id)))
          .limit(1)
          .for("update");
        if (!lockedNode) throw new Error("既存のDocs nodeが同時に削除されました");
        await assertTaskDocsNodeLinkAllowedInTransaction(
          tx,
          lockedNode.id,
          String(lockedTask.projectId),
          user,
        );
        const repairedNode = await updateDocsNode(tx, lockedNode.id, {
          parentId: lockedProjectNode.id,
          rootPageId: lockedProjectNode.rootPageId ?? lockedProjectNode.id,
          projectId: lockedProject.id,
          updatedBy: user.id,
          updatedAt: new Date(),
        });
        await updateDocsNodesByIds(tx, descendants, {
          rootPageId: repairedNode.rootPageId,
          projectId: lockedTask.projectId,
          updatedBy: user.id,
          updatedAt: new Date(),
        });
        return repairedNode;
        });
      } catch (error) {
        if (error instanceof TaskDocsNodeInvariantError || error instanceof TaskProjectAccessInvariantError) {
          return NextResponse.json({ detail: error.message }, { status: error.status });
        }
        throw error;
      }
      return NextResponse.json({ node: serializeNode(repaired), created: false });
    }
  }

  const [taskTag] = await db
    .select()
    .from(knowledgeSupertags)
    .where(
      and(
        eq(knowledgeSupertags.docsLibraryId, workspace.id),
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

  let node: typeof knowledgeNodes.$inferSelect;
  try {
    node = await db.transaction(async (tx) => {
    await lockTaskProjectIds(tx, [task.projectId]);
    await lockProjectInformationAdvisory(tx, task.projectId);
    const [lockedProject] = await tx
      .select()
      .from(projects)
      .where(eq(projects.id, task.projectId))
      .limit(1)
      .for("update");
    if (!lockedProject || lockedProject.deletedAt || lockedProject.isCompleted) {
      throw new Error("完了/削除済みProjectのDocs nodeは作成できません");
    }
    await assertTaskProjectAccessInTransaction(tx, [task.projectId], user);
    // Resolve and lock the canonical Project hierarchy in this same
    // transaction. The earlier projectNode snapshot can become stale after a
    // repair/reparent and must never be used as a new note's parent.
    const lockedProjectNode = await ensureProjectInformationHierarchyNode({
      docsLibraryId: workspace.id,
      userId: user.id,
      project: lockedProject,
      client: tx,
    });
    const [lockedTask] = await tx
      .select()
      .from(tasks)
      .where(eq(tasks.id, task.id))
      .for("update")
      .limit(1);
    if (!lockedTask || lockedTask.knowledgeNodeId) {
      throw new Error("タスクのDocs node bindingが同時変更されたため作成できません");
    }
    if (String(lockedTask.projectId) !== String(lockedProject.id)) {
      throw new TaskDocsNodeInvariantError(
        "タスクのProjectが同時変更されたためDocs nodeを作成できません",
      );
    }
    const lockedParentId = lockedProjectNode.id;
    const lockedRootPageId = lockedProjectNode.rootPageId ?? lockedProjectNode.id;
    const lockedBodyText = cleanOptionalString(lockedTask.description, 200000) ?? "";
    const [maxRow] = await tx
      .select({ maxSort: max(knowledgeNodes.sortOrder) })
      .from(knowledgeNodes)
      .where(
        and(
          eq(knowledgeNodes.docsLibraryId, workspace.id),
          lockedParentId ? eq(knowledgeNodes.parentId, lockedParentId) : isNull(knowledgeNodes.parentId),
        ),
      );

      const created = await insertDocsNode(tx, {
        docsLibraryId: workspace.id,
        parentId: lockedParentId,
        rootPageId: lockedRootPageId,
        projectId: lockedProject.id,
        title: lockedTask.title,
        description: lockedTask.description ?? "",
        bodyJson: {
          format: "task_note",
          task_id: lockedTask.id,
        },
        nodeType: "node",
        displayProps: { show_checkbox: true },
        queryJson: null,
        viewJson: {},
        sortOrder: (maxRow?.maxSort ?? 0) + 1,
        createdBy: user.id,
        updatedBy: user.id,
      });

    const finalNode = created;

    await tx.insert(knowledgeNodeSupertags).values({
      nodeId: finalNode.id,
      supertagId: taskTag.id,
      createdBy: user.id,
    });
    await upsertKnowledgeSearchIndex(tx, finalNode, finalNode.title);
    if (lockedBodyText) {
      const detailNode = await insertDocsNode(tx, {
        docsLibraryId: workspace.id,
        parentId: finalNode.id,
        rootPageId: finalNode.rootPageId ?? finalNode.id,
        projectId: lockedProject.id,
        title: lockedBodyText,
        bodyJson: { format: "doc_block", block_type: "paragraph" },
        nodeType: "node",
        sortOrder: 1,
        createdBy: user.id,
        updatedBy: user.id,
      });
      await upsertKnowledgeSearchIndex(tx, detailNode, detailNode.title);
    }
    await syncKnowledgeNodeReferenceEdges(tx, finalNode, user.id);
    await appendKnowledgeRevision(tx, finalNode, user.id, "タスクをDocsノート化");
    await assertTaskDocsNodeLinkAllowedInTransaction(
      tx,
      finalNode.id,
      String(lockedTask.projectId),
      user,
    );
    const existingMetadata =
      lockedTask.taskMetadata
      && typeof lockedTask.taskMetadata === "object"
      && !Array.isArray(lockedTask.taskMetadata)
        ? lockedTask.taskMetadata
        : {};
    const [boundTask] = await tx
      .update(tasks)
      .set({
        knowledgeNodeId: finalNode.id,
        taskMetadata: {
          ...existingMetadata,
          source: lockedTask.source,
          knowledge_node_id: finalNode.id,
        },
        updatedAt: new Date(),
      })
      .where(
        and(
          eq(tasks.id, lockedTask.id),
          isNull(tasks.knowledgeNodeId),
          isNull(tasks.deletedAt),
        ),
      )
      .returning();
    if (!boundTask) {
      throw new TaskDocsNodeInvariantError(
        "タスクのDocs node bindingが同時変更されたため作成できません",
      );
    }
    return finalNode;
    });
  } catch (error) {
    if (error instanceof TaskDocsNodeInvariantError || error instanceof TaskProjectAccessInvariantError) {
      return NextResponse.json({ detail: error.message }, { status: error.status });
    }
    throw error;
  }

  return NextResponse.json({ node: serializeNode(node), created: true }, { status: 201 });
}
