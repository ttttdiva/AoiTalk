import { NextResponse } from "next/server";
import { and, eq, isNull } from "drizzle-orm";
import { db } from "@/db";
import {
  knowledgeFields,
  knowledgeFieldValues,
  knowledgeNodes,
  knowledgeNodeSupertags,
  knowledgeSupertags,
  tasks,
} from "@/db/schema";
import { getSession } from "@/lib/auth";
import {
  appendKnowledgeRevision,
  ensureDocsWorkspace,
  serializeNode,
  upsertKnowledgeSearchIndex,
} from "@/lib/server/knowledge-docs-utils";
import { getWritableProject } from "@/lib/server/project-access";
import { insertDocsNode, updateDocsNode, updateDocsNodesByIds } from "@/lib/server/docs-node-writer";
import {
  ensureProjectInformationHierarchyNode,
  ensureProjectMeetingSection,
  isDefaultInboxProject,
} from "@/lib/server/project-information-hierarchy";

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
  const workspace = await ensureDocsWorkspace(user);
  const projectNode = await ensureProjectInformationHierarchyNode({
    workspaceId: workspace.id,
    userId: user.id,
    project,
  });
  const meetingSection = await ensureProjectMeetingSection({
    workspaceId: workspace.id,
    userId: user.id,
    projectId: project.id,
    projectNode,
  });

  if (task.knowledgeNodeId) {
    const [existing] = await db
      .select()
      .from(knowledgeNodes)
      .where(eq(knowledgeNodes.id, task.knowledgeNodeId))
      .limit(1);
    if (existing) {
      const repaired = await updateDocsNode(db, existing.id, {
        parentId: meetingSection.id,
        rootPageId: projectNode.rootPageId ?? projectNode.id,
        updatedBy: user.id,
        updatedAt: new Date(),
      });
      const descendants = await db
        .select({ id: knowledgeNodes.id })
        .from(knowledgeNodes)
        .where(eq(knowledgeNodes.rootPageId, existing.id));
      await updateDocsNodesByIds(db, descendants.map((node) => node.id), {
        rootPageId: repaired.rootPageId,
        updatedBy: user.id,
        updatedAt: new Date(),
      });
      return NextResponse.json({ node: serializeNode(repaired), created: false });
    }
  }

  const [meetingTag] = await db
    .select()
    .from(knowledgeSupertags)
    .where(and(eq(knowledgeSupertags.workspaceId, workspace.id), eq(knowledgeSupertags.systemKey, "meeting_note")))
    .limit(1);
  const tag = meetingTag ?? (await db.insert(knowledgeSupertags).values({
    workspaceId: workspace.id,
    systemKey: "meeting_note",
    name: "議事メモ",
    baseType: "meeting",
    icon: "notebook",
    color: "#0ea5e9",
    titleTemplate: "{project} {date} 議事メモ",
    templateJson: { blocks: [{ text: "日時" }, { text: "出席者" }, { text: "議題" }, { text: "メモ" }] },
  }).returning())[0];
  const now = new Date();
  const title = formatTitle(tag.titleTemplate, project.name, now);
  const parentId = meetingSection.id;
  const rootPageId = projectNode.rootPageId ?? projectNode.id;

  const result = await db.transaction(async (tx) => {
    const note = await insertDocsNode(tx, {
      workspaceId: workspace.id,
      parentId,
      rootPageId,
      projectId: project.id,
      title,
      bodyJson: { format: "doc_block", block_type: "heading_1" },
      nodeType: "node",
      displayProps: {},
      sortOrder: Date.now(),
      createdBy: user.id,
      updatedBy: user.id,
    });
    const finalNote = await updateDocsNode(tx, note.id, { rootPageId, updatedBy: user.id, updatedAt: new Date() });
    await tx.insert(knowledgeNodeSupertags).values({ nodeId: note.id, supertagId: tag.id, createdBy: user.id });
    await upsertKnowledgeSearchIndex(tx, finalNote, title);
    await appendKnowledgeRevision(tx, finalNote, user.id, "議事メモを作成");

    const lines = templateLines(tag.templateJson);
    for (const [index, line] of lines.entries()) {
      const child = await insertDocsNode(tx, {
        workspaceId: workspace.id,
        parentId: note.id,
        rootPageId: rootPageId,
        projectId: project.id,
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
      .where(eq(knowledgeFields.workspaceId, workspace.id));
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
        targetNodeId: projectNode.id,
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
        targetNodeId: task.knowledgeNodeId ?? null,
        updatedBy: user.id,
      });
    }
    if (fieldValues.length > 0) await tx.insert(knowledgeFieldValues).values(fieldValues).onConflictDoNothing();

    await tx.update(tasks).set({ knowledgeNodeId: note.id, updatedAt: now }).where(eq(tasks.id, task.id));
    return finalNote;
  });

  return NextResponse.json({ node: serializeNode(result), created: true });
}
