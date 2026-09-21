import { NextResponse } from "next/server";
import { and, asc, eq, inArray, isNull } from "drizzle-orm";
import { db } from "@/db";
import {
  knowledgeFields,
  knowledgeFieldValues,
  knowledgeNodes,
  knowledgeNodeSupertags,
  knowledgeSupertags,
  tasks,
  projects,
} from "@/db/schema";
import { getSession } from "@/lib/auth";
import {
  appendKnowledgeRevision,
  ensureProjectDocsWorkspace,
  getKnowledgeNodeDescendantIds,
  serializeNode,
  upsertKnowledgeSearchIndex,
} from "@/lib/server/knowledge-docs-utils";
import { getWritableProject } from "@/lib/server/project-access";
import { insertDocsNode, updateDocsNode, updateDocsNodesByIds } from "@/lib/server/docs-node-writer";
import {
  ensureProjectInformationHierarchyNode,
  ensureProjectMeetingSection,
  isDefaultInboxProject,
  lockProjectInformationAdvisory,
  lockProjectMeetingAdvisory,
} from "@/lib/server/project-information-hierarchy";
import {
  assertTaskDocsNodeLinkAllowed,
  assertTaskDocsNodeLinkAllowedInTransaction,
  assertTaskProjectAccessInTransaction,
  TaskDocsNodeInvariantError,
  TaskProjectAccessInvariantError,
} from "@/lib/server/task-docs-node-invariant";
import { lockTaskProjectIds } from "@/lib/server/project-move-dependency-invariant";

function formatJstDate(now: Date) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Tokyo",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(now);
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${values.year}-${values.month}-${values.day}`;
}

function formatTitle(template: string | null | undefined, projectName: string, now: Date) {
  const date = formatJstDate(now);
  const fallback = `${projectName} ${date} 議事メモ`;
  if (!template?.trim()) return fallback;
  return template
    .replace(/\{project\}/g, projectName)
    .replace(/\{project_name\}/g, projectName)
    .replace(/\{date\}/g, date)
    .replace(/\s+/g, " ")
    .trim() || fallback;
}

function templateLines(templateJson: unknown): string[] {
  const record = templateJson && typeof templateJson === "object" && !Array.isArray(templateJson)
    ? templateJson as Record<string, unknown>
    : {};
  const blocks = Array.isArray(record.blocks) ? record.blocks : [];
  const lines = blocks
    .map((block) => block && typeof block === "object" ? String((block as Record<string, unknown>).text ?? "").trim() : "")
    .filter(Boolean);
  return lines.length > 0 ? lines : ["日時", "出席者", "議題", "メモ"];
}

class ProjectDocsLifecycleError extends Error {
  readonly status = 409;
  readonly code = "project_lifecycle_conflict";
}

export async function POST(
  _request: Request,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await getSession();
  if (!user) return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  const { id } = await params;
  const [task] = await db
    .select()
    .from(tasks)
    .where(and(eq(tasks.id, id), isNull(tasks.deletedAt)))
    .limit(1);
  if (!task) return NextResponse.json({ detail: "タスクが見つかりません" }, { status: 404 });
  const projectAccess = await getWritableProject(task.projectId, user);
  if (!projectAccess) return NextResponse.json({ detail: "Projectへの書き込み権限がありません" }, { status: 403 });
  const project = projectAccess.project;
  if (isDefaultInboxProject(project)) {
    return NextResponse.json(
      { detail: "Inboxには議事メモDocsを作成できません。実案件を指定してください。" },
      { status: 409 },
    );
  }
  const workspace = await ensureProjectDocsWorkspace(project.id, user);
  if (!workspace) {
    return NextResponse.json({ detail: "Project Docs workspaceへの書き込み権限がありません" }, { status: 403 });
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
    const [existing] = await db
      .select()
      .from(knowledgeNodes)
      .where(eq(knowledgeNodes.id, task.knowledgeNodeId))
      .limit(1);
    if (existing) {
      let repaired: typeof existing;
      try {
        repaired = await db.transaction(async (tx) => {
        await lockTaskProjectIds(tx, [project.id]);
        await lockProjectInformationAdvisory(tx, project.id);
        await lockProjectMeetingAdvisory(tx, workspace.id, project.id);
        const [lockedProject] = await tx
          .select()
          .from(projects)
          .where(eq(projects.id, project.id))
          .limit(1)
          .for("update");
        if (!lockedProject || lockedProject.deletedAt || lockedProject.isCompleted) {
          throw new ProjectDocsLifecycleError("完了/削除済みProjectの会議メモDocsは修復できません");
        }
        await assertTaskProjectAccessInTransaction(tx, [project.id], user);
        const lockedProjectNode = await ensureProjectInformationHierarchyNode({
          docsLibraryId: workspace.id,
          userId: user.id,
          project: lockedProject,
          client: tx,
        });
        const lockedMeetingSection = await ensureProjectMeetingSection({
          docsLibraryId: workspace.id,
          userId: user.id,
          projectId: lockedProject.id,
          projectNode: lockedProjectNode,
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
        if (!lockedTask || lockedTask.knowledgeNodeId !== existing.id) {
          throw new ProjectDocsLifecycleError("タスクのDocs node bindingが同時変更されたため修復できません");
        }
        if (String(lockedTask.projectId) !== String(lockedProject.id)) {
          throw new ProjectDocsLifecycleError(
            "タスクのProjectが同時変更されたためDocs nodeを修復できません",
          );
        }
        let [lockedExisting] = await tx
          .select()
          .from(knowledgeNodes)
          .where(and(eq(knowledgeNodes.id, existing.id), eq(knowledgeNodes.docsLibraryId, workspace.id)))
          .limit(1);
        if (!lockedExisting) throw new ProjectDocsLifecycleError("既存の会議メモDocsが見つかりません");
        // Stabilize the entire bound-note subtree before policy validation and
        // the descendant denormalizer update. This gives concurrent generic
        // moves one deterministic node-lock order and avoids root/child
        // deadlocks during lifecycle repair.
        const descendants = await getKnowledgeNodeDescendantIds(tx, workspace.id, lockedExisting.id);
        const closureIds = Array.from(new Set([lockedExisting.id, ...descendants])).sort();
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
        [lockedExisting] = await tx
          .select()
          .from(knowledgeNodes)
          .where(and(eq(knowledgeNodes.id, existing.id), eq(knowledgeNodes.docsLibraryId, workspace.id)))
          .limit(1)
          .for("update");
        if (!lockedExisting) throw new ProjectDocsLifecycleError("既存の会議メモDocsが同時に削除されました");
        await assertTaskDocsNodeLinkAllowedInTransaction(
          tx,
          lockedExisting.id,
          String(lockedTask.projectId),
          user,
        );
        const repairedNode = await updateDocsNode(tx, lockedExisting.id, {
          parentId: lockedMeetingSection.id,
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
        if (error instanceof ProjectDocsLifecycleError) {
          return NextResponse.json({ detail: error.message, code: error.code }, { status: error.status });
        }
        throw error;
      }
      return NextResponse.json({ node: serializeNode(repaired), created: false });
    }
  }

  let result: typeof knowledgeNodes.$inferSelect;
  try {
    result = await db.transaction(async (tx) => {
      await lockTaskProjectIds(tx, [project.id]);
      await lockProjectInformationAdvisory(tx, project.id);
      await lockProjectMeetingAdvisory(tx, workspace.id, project.id);
      const [lockedProject] = await tx
        .select()
        .from(projects)
        .where(eq(projects.id, project.id))
        .for("update")
        .limit(1);
      if (!lockedProject || lockedProject.deletedAt || lockedProject.isCompleted) {
        throw new ProjectDocsLifecycleError("完了/削除済みProjectの会議メモDocsは作成できません");
      }
      await assertTaskProjectAccessInTransaction(tx, [project.id], user);
      // Re-resolve both canonical parents under the same Project lock used by
      // note insertion. The preflight hierarchy snapshots may have been
      // repaired or reparented before this transaction acquired its lock.
      const lockedProjectNode = await ensureProjectInformationHierarchyNode({
        docsLibraryId: workspace.id,
        userId: user.id,
        project: lockedProject,
        client: tx,
      });
      const lockedMeetingSection = await ensureProjectMeetingSection({
        docsLibraryId: workspace.id,
        userId: user.id,
        projectId: lockedProject.id,
        projectNode: lockedProjectNode,
        client: tx,
      });
      // Resolve/create the meeting supertag under the same Project ACL lock.
      // Members may consume an existing Personal-library definition, but only
      // its owner may initialize missing metadata.
      let [tag] = await tx
        .select()
        .from(knowledgeSupertags)
        .where(
          and(
            eq(knowledgeSupertags.docsLibraryId, workspace.id),
            eq(knowledgeSupertags.systemKey, "meeting_note"),
          ),
        )
        .limit(1)
        .for("update");
      if (!tag) {
        if (workspace.ownerUserId !== user.id) {
          throw new ProjectDocsLifecycleError(
            "会議メモ定義が未初期化のため、Library所有者による初期化が必要です",
          );
        }
        [tag] = await tx
          .insert(knowledgeSupertags)
          .values({
            docsLibraryId: workspace.id,
            systemKey: "meeting_note",
            name: "議事メモ",
            baseType: "meeting",
            icon: "notebook",
            color: "#0ea5e9",
            titleTemplate: "{project} {date} 議事メモ",
            templateJson: { blocks: [{ text: "日時" }, { text: "出席者" }, { text: "議題" }, { text: "メモ" }] },
          })
          .returning();
      }
      if (!tag) throw new ProjectDocsLifecycleError("会議メモ定義を初期化できません");
      const [lockedTask] = await tx
        .select()
        .from(tasks)
        .where(eq(tasks.id, task.id))
        .for("update")
        .limit(1);
      if (!lockedTask || lockedTask.knowledgeNodeId) {
        throw new ProjectDocsLifecycleError("タスクのDocs node bindingが同時変更されたため作成できません");
      }
      if (String(lockedTask.projectId) !== String(lockedProject.id)) {
        throw new ProjectDocsLifecycleError(
          "タスクのProjectが同時変更されたためDocs nodeを作成できません",
        );
      }
      // Compute the title only after the Project row lock.  A rename racing
      // this request must either commit first (and be reflected here) or wait
      // behind this transaction, never leave a stale project name in a note.
      const transactionNow = new Date();
      const title = formatTitle(tag.titleTemplate, lockedProject.name, transactionNow);
      const parentId = lockedMeetingSection.id;
      const rootPageId = lockedProjectNode.rootPageId ?? lockedProjectNode.id;
      const note = await insertDocsNode(tx, {
      docsLibraryId: workspace.id,
      parentId,
      rootPageId,
      projectId: lockedProject.id,
      title,
      bodyJson: { format: "doc_block", block_type: "heading_1" },
      nodeType: "node",
      displayProps: {},
      sortOrder: Date.now(),
      createdBy: user.id,
      updatedBy: user.id,
      });
      const finalNote = await updateDocsNode(tx, note.id, { rootPageId, updatedBy: user.id, updatedAt: transactionNow });
      await tx.insert(knowledgeNodeSupertags).values({ nodeId: note.id, supertagId: tag.id, createdBy: user.id });
      await upsertKnowledgeSearchIndex(tx, finalNote, title);
      await appendKnowledgeRevision(tx, finalNote, user.id, "議事メモを作成");

      const lines = templateLines(tag.templateJson);
      for (const [index, line] of lines.entries()) {
        const child = await insertDocsNode(tx, {
        docsLibraryId: workspace.id,
        parentId: note.id,
        rootPageId: rootPageId,
        projectId: lockedProject.id,
        title: line,
        bodyJson: { format: "doc_block", block_type: index < 2 ? "heading_2" : "paragraph" },
        nodeType: "node",
        displayProps: {},
        sortOrder: index + 1,
        createdBy: user.id,
        updatedBy: user.id,
        });
        await upsertKnowledgeSearchIndex(tx, child, child.title);
      }

      const fields = await tx
        .select()
        .from(knowledgeFields)
        .where(eq(knowledgeFields.docsLibraryId, workspace.id));
      const projectField = fields.find((field) => field.systemKey === "meeting_project");
      const taskField = fields.find((field) => field.systemKey === "meeting_related_task");
      const fieldValues: Array<typeof knowledgeFieldValues.$inferInsert> = [];
      if (projectField) {
        fieldValues.push({
        nodeId: note.id,
        fieldId: projectField.id,
        valueJson: null,
        valueText: null,
        valueNumber: null,
        valueDatetime: null,
        targetNodeId: lockedProjectNode.id,
        updatedBy: user.id,
        });
      }
      if (taskField) {
        fieldValues.push({
        nodeId: note.id,
        fieldId: taskField.id,
        valueJson: null,
        valueText: task.id,
        valueNumber: null,
        valueDatetime: null,
        targetNodeId: lockedTask.knowledgeNodeId ?? null,
        updatedBy: user.id,
        });
      }
      if (fieldValues.length > 0) await tx.insert(knowledgeFieldValues).values(fieldValues).onConflictDoNothing();

      await assertTaskDocsNodeLinkAllowedInTransaction(
        tx,
        note.id,
        String(lockedProject.id),
        user,
      );
      await tx.update(tasks).set({ knowledgeNodeId: note.id, updatedAt: transactionNow }).where(eq(tasks.id, lockedTask.id));
      return finalNote;
    });
  } catch (error) {
    if (error instanceof TaskDocsNodeInvariantError || error instanceof TaskProjectAccessInvariantError) {
      return NextResponse.json(
        { detail: error.message },
        { status: error.status },
      );
    }
    if (error instanceof ProjectDocsLifecycleError) {
      return NextResponse.json(
        { detail: error.message, code: error.code },
        { status: error.status },
      );
    }
    throw error;
  }

  return NextResponse.json({ node: serializeNode(result), created: true });
}
