import { NextRequest, NextResponse } from "next/server";
import { createHash } from "crypto";
import { and, asc, desc, eq, inArray, isNull, or } from "drizzle-orm";
import { db } from "@/db";
import {
  knowledgeNodes,
  knowledgeFields,
  knowledgeFieldValues,
  knowledgeNodeSupertags,
  knowledgeSupertags,
  projectQaEntries,
  projects,
  recordTables,
} from "@/db/schema";
import { getSession } from "@/lib/auth";
import { decryptTextIfNeeded, encryptText } from "@/lib/server/field-crypto";
import {
  appendKnowledgeRevision,
  ensureDocsWorkspace,
  normalizeFieldValueInput,
  serializeNode,
  serializeNodeSupertag,
  serializeSupertag,
  upsertKnowledgeSearchIndex,
} from "@/lib/server/knowledge-docs-utils";
import {
  getAccessibleProject,
  getWritableProject,
} from "@/lib/server/project-access";
import { normalizeProjectManagementConfig } from "@/lib/server/project-workspace-management";
import { insertDocsNode, updateDocsNode, type DocsNodeWriterUpdate } from "@/lib/server/docs-node-writer";
import {
  ensureProjectInformationHierarchyNode,
  isDefaultInboxProject,
} from "@/lib/server/project-information-hierarchy";

const QA_STATUSES = new Set(["unanswered", "answered", "stale", "archived"]);
const QA_REVIEW_STATES = new Set(["candidate", "accepted", "rejected"]);
const PROJECT_INFORMATION_SUPERTAG = "Project information";
const PROJECT_INFORMATION_SYSTEM_KEY = "project_info";

function cleanString(value: unknown, fallback = "", maxLength = 500): string {
  if (typeof value !== "string") return fallback;
  const trimmed = value.trim();
  return (trimmed || fallback).slice(0, maxLength);
}

function cleanNullableString(value: unknown): string | null {
  const text = cleanString(value);
  return text || null;
}

function oneOf(value: unknown, allowed: Set<string>, fallback: string): string {
  const text = cleanString(value, fallback);
  return allowed.has(text) ? text : fallback;
}

function numberValue(value: unknown, fallback: number): number {
  const n = Number(value);
  return Number.isFinite(n) ? n : fallback;
}

function jsonObject(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  return { ...(value as Record<string, unknown>) };
}

function decryptQaEntry<T extends { question: string; answer: string | null }>(
  entry: T,
): T {
  return {
    ...entry,
    question:
      decryptTextIfNeeded(entry.question, "project_qa_entries.question") || "",
    answer:
      entry.answer == null
        ? null
        : decryptTextIfNeeded(entry.answer, "project_qa_entries.answer") || "",
  };
}

function normalizedQuestionHash(value: string) {
  const normalized = value.replace(/\s+/g, " ").trim().toLowerCase();
  return createHash("sha256").update(normalized).digest("hex");
}

function projectInformationBodyJson() {
  return {
    format: "project_information_doc_block",
    source: "docs_canonical",
    blocks: [{ type: "project_qa_block", source: "project_qa_entries" }],
  };
}

async function ensureProjectInformationFieldValues(
  node: typeof knowledgeNodes.$inferSelect,
  supertag: typeof knowledgeSupertags.$inferSelect,
  project: typeof projects.$inferSelect,
  userId: string,
) {
  const fields = await db
    .select()
    .from(knowledgeFields)
    .where(eq(knowledgeFields.supertagId, supertag.id));
  const projectField = fields.find((field) => field.name === "Project" || field.systemKey === "project");
  if (projectField) {
    const fieldValue = {
      ...normalizeFieldValueInput(projectField, project.id),
      nodeId: node.id,
      targetNodeId: null,
      updatedBy: userId,
    };
    await db
      .insert(knowledgeFieldValues)
      .values(fieldValue)
      .onConflictDoUpdate({
        target: [knowledgeFieldValues.nodeId, knowledgeFieldValues.fieldId],
        set: {
          valueJson: fieldValue.valueJson,
          valueText: fieldValue.valueText,
          valueNumber: fieldValue.valueNumber,
          valueDatetime: fieldValue.valueDatetime,
          targetNodeId: fieldValue.targetNodeId,
          updatedBy: fieldValue.updatedBy,
        },
      });
  }

  const pageRoleField = fields.find(
    (field) => field.name === "Page Role" || field.systemKey === "page_role",
  );
  if (pageRoleField) {
    await db
      .insert(knowledgeFieldValues)
      .values({
        ...normalizeFieldValueInput(pageRoleField, "canonical"),
        nodeId: node.id,
        targetNodeId: null,
        updatedBy: userId,
      })
      .onConflictDoNothing();
  }
}

async function findProjectInformationSection(parentId: string, title: string) {
  const [section] = await db
    .select()
    .from(knowledgeNodes)
    .where(and(eq(knowledgeNodes.parentId, parentId), eq(knowledgeNodes.title, title), isNull(knowledgeNodes.archivedAt)))
    .limit(1);
  return section ?? null;
}

function managementDocuments(project: { projectMetadata: unknown }) {
  const config = normalizeProjectManagementConfig(project.projectMetadata);
  const docs: Array<Record<string, unknown>> = [];
  const pushFile = (kind: string, title: string, filePath?: string | null) => {
    if (!filePath) return;
    docs.push({
      id: `management:${kind}:${filePath}`,
      title,
      document_type: kind,
      target_kind: "file",
      file_path: filePath,
      role: "management",
      status: "active",
      source_type: "project_management",
      synthetic: true,
    });
  };
  pushFile("wbs", "WBS", config.wbsFile);
  pushFile("issue", "課題管理表", config.issueFile);
  pushFile("risk", "リスク管理表", config.riskFile);
  for (const filePath of config.requestFiles) {
    pushFile("support", filePath.split("/").at(-1) || "補助資料", filePath);
  }
  return docs;
}

async function ensureProjectInformationDocument(
  user: { id: string; role?: string | null },
  project: typeof projects.$inferSelect,
) {
  const workspace = await ensureDocsWorkspace(user);
  const [supertag] = await db
    .select()
    .from(knowledgeSupertags)
    .where(
      and(
        eq(knowledgeSupertags.workspaceId, workspace.id),
        or(
          eq(knowledgeSupertags.systemKey, PROJECT_INFORMATION_SYSTEM_KEY),
          eq(knowledgeSupertags.name, PROJECT_INFORMATION_SUPERTAG),
          eq(knowledgeSupertags.name, "案件情報"),
        ),
      ),
    )
    .limit(1);

  if (!supertag) {
    throw new Error("案件情報スーパータグが見つかりません");
  }
  const hierarchyNode = await ensureProjectInformationHierarchyNode({
    workspaceId: workspace.id,
    userId: user.id,
    project,
  });
  const node = await updateDocsNode(db, hierarchyNode.id, {
    bodyJson: projectInformationBodyJson(),
    updatedBy: user.id,
    updatedAt: new Date(),
  });
  await ensureProjectInformationFieldValues(node, supertag, project, user.id);
  await upsertKnowledgeSearchIndex(db, node, node.title);
  return { node, supertag };
}

export async function GET(
  _request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const { id: projectId } = await params;
  const access = await getAccessibleProject(projectId, user.id);
  if (!access) {
    return NextResponse.json(
      { detail: "プロジェクトが見つからないか権限がありません" },
      { status: 404 },
    );
  }
  if (isDefaultInboxProject(access.project)) {
    return NextResponse.json(
      { detail: "Inboxは案件情報Docsの保存先ではありません" },
      { status: 409 },
    );
  }

  const { node, supertag } = await ensureProjectInformationDocument(
    user,
    access.project,
  );
  const [qaEntries, tables, treeNodes, projectSupertags] = await Promise.all([
    db
      .select()
      .from(projectQaEntries)
      .where(
        and(
          eq(projectQaEntries.projectId, projectId),
          isNull(projectQaEntries.deletedAt),
        ),
      )
      .orderBy(desc(projectQaEntries.updatedAt)),
    db
      .select({
        id: recordTables.id,
        name: recordTables.name,
        description: recordTables.description,
        updatedAt: recordTables.updatedAt,
      })
      .from(recordTables)
      .where(and(eq(recordTables.projectId, projectId), isNull(recordTables.deletedAt)))
      .orderBy(recordTables.sortOrder, recordTables.createdAt),
    db
      .select()
      .from(knowledgeNodes)
      .where(and(eq(knowledgeNodes.projectId, projectId), eq(knowledgeNodes.workspaceId, node.workspaceId), isNull(knowledgeNodes.archivedAt)))
      .orderBy(asc(knowledgeNodes.sortOrder), asc(knowledgeNodes.createdAt)),
    db
      .select()
      .from(knowledgeSupertags)
      .where(eq(knowledgeSupertags.workspaceId, node.workspaceId)),
  ]);
  const treeNodeIds = treeNodes.map((entry) => entry.id);
  const treeNodeSupertags = treeNodeIds.length > 0
    ? await db
        .select()
        .from(knowledgeNodeSupertags)
        .where(inArray(knowledgeNodeSupertags.nodeId, treeNodeIds))
    : [];

  return NextResponse.json({
    project: {
      id: access.project.id,
      name: access.project.name,
      description: access.project.description,
      knowledge_node_id: node.id,
    },
    node: serializeNode(node),
    tree_nodes: treeNodes.map(serializeNode),
    node_supertags: treeNodeSupertags.map(serializeNodeSupertag),
    supertags: projectSupertags.map(serializeSupertag),
    supertag: serializeSupertag(supertag),
    qa_entries: qaEntries.map(decryptQaEntry),
    management_documents: managementDocuments(access.project),
    record_tables: tables,
  });
}

export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const { id: projectId } = await params;
  const access = await getWritableProject(projectId, user);
  if (!access) {
    return NextResponse.json(
      { detail: "プロジェクトが見つからないか権限がありません" },
      { status: 404 },
    );
  }
  if (isDefaultInboxProject(access.project)) {
    return NextResponse.json(
      { detail: "Inboxは案件情報Docsの保存先ではありません" },
      { status: 409 },
    );
  }

  const body = await request.json().catch(() => ({}));
  const kind = cleanString(body.kind);
  const { node } = await ensureProjectInformationDocument(user, access.project);

  if (kind === "qa") {
    const question = cleanString(body.question);
    if (!question) {
      return NextResponse.json({ detail: "question is required" }, { status: 400 });
    }
    const answer = cleanNullableString(body.answer);
    const [created] = await db
      .insert(projectQaEntries)
      .values({
        projectId,
        knowledgeNodeId: node.id,
        question: encryptText(question, "project_qa_entries.question"),
        answer: answer ? encryptText(answer, "project_qa_entries.answer") : null,
        normalizedQuestionHash: normalizedQuestionHash(question),
        status: oneOf(body.status, QA_STATUSES, answer ? "answered" : "unanswered"),
        reviewState: oneOf(body.review_state, QA_REVIEW_STATES, "accepted"),
        confidence: numberValue(body.confidence, 1),
        askedCount: Math.max(1, Math.round(numberValue(body.asked_count, 1))),
        sourceMessageIds: Array.isArray(body.source_message_ids)
          ? body.source_message_ids
          : [],
        sourceAgentRunIds: Array.isArray(body.source_agent_run_ids)
          ? body.source_agent_run_ids
          : [],
        sourceToolCallIds: Array.isArray(body.source_tool_call_ids)
          ? body.source_tool_call_ids
          : [],
        answerSourceRefs: Array.isArray(body.answer_source_refs)
          ? body.answer_source_refs
          : [],
        createdBy: user.id,
        updatedBy: user.id,
        createdByAgent: Boolean(body.created_by_agent),
      })
      .returning();
    return NextResponse.json({ qa_entry: decryptQaEntry(created) }, { status: 201 });
  }

  if (kind === "reference") {
    const title = cleanString(body.title);
    const target =
      cleanNullableString(body.file_path) ||
      cleanNullableString(body.external_url) ||
      cleanNullableString(body.record_table_id);
    if (!title || !target) {
      return NextResponse.json(
        { detail: "title and reference target are required" },
        { status: 400 },
      );
    }
    const description = cleanNullableString(body.description);
    const section = await findProjectInformationSection(node.id, "参照");
    const created = await insertDocsNode(db, {
        workspaceId: node.workspaceId,
        projectId,
        parentId: section?.id ?? node.id,
        rootPageId: node.rootPageId ?? node.id,
        title,
        bodyJson: {
          format: "project_information_reference",
          target,
          description,
          source: {
            file_path: cleanNullableString(body.file_path),
            external_url: cleanNullableString(body.external_url),
            record_table_id: cleanNullableString(body.record_table_id),
          },
        },
        nodeType: "reference",
        sortOrder: Date.now(),
        createdBy: user.id,
        updatedBy: user.id,
      });
    await upsertKnowledgeSearchIndex(db, created, created.title);
    const referenceChildren = [
      `参照先: ${target}`,
      description ? `説明: ${description}` : null,
    ].filter((item): item is string => !!item);
    for (const [index, childTitle] of referenceChildren.entries()) {
      const child = await insertDocsNode(db, {
        workspaceId: node.workspaceId,
        projectId,
        parentId: created.id,
        rootPageId: node.rootPageId ?? node.id,
        title: childTitle,
        bodyJson: { format: "doc_block", block_type: "paragraph" },
        nodeType: "node",
        sortOrder: index,
        createdBy: user.id,
        updatedBy: user.id,
      });
      await upsertKnowledgeSearchIndex(db, child, child.title);
    }
    await appendKnowledgeRevision(db, created, user.id, "Project information reference added");
    return NextResponse.json({ node: serializeNode(created) }, { status: 201 });
  }

  return NextResponse.json({ detail: "Invalid kind" }, { status: 400 });
}

export async function PATCH(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const { id: projectId } = await params;
  const access = await getWritableProject(projectId, user);
  if (!access) {
    return NextResponse.json(
      { detail: "プロジェクトが見つからないか権限がありません" },
      { status: 404 },
    );
  }
  if (isDefaultInboxProject(access.project)) {
    return NextResponse.json(
      { detail: "Inboxは案件情報Docsの保存先ではありません" },
      { status: 409 },
    );
  }

  const body = await request.json().catch(() => ({}));
  const kind = cleanString(body.kind);

  if (kind === "document_node") {
    const nodeId = cleanString(body.node_id);
    if (!nodeId) return NextResponse.json({ detail: "node_id is required" }, { status: 400 });
    const [node] = await db
      .select()
      .from(knowledgeNodes)
      .where(
        and(
          eq(knowledgeNodes.id, nodeId),
          eq(knowledgeNodes.projectId, projectId),
          isNull(knowledgeNodes.archivedAt),
        ),
      )
      .limit(1);
    if (!node) {
      return NextResponse.json({ detail: "node not found" }, { status: 404 });
    }

    const updates: DocsNodeWriterUpdate = {
      updatedBy: user.id,
      updatedAt: new Date(),
    };
    if (body.title !== undefined) {
      updates.title = cleanString(body.title, node.title, 500);
    }
    if (body.body_text !== undefined) {
      updates.title = cleanString(body.body_text, updates.title ?? node.title, 500);
    }
    if (body.body_json !== undefined) {
      updates.bodyJson = jsonObject(body.body_json);
    }

    const updated = await updateDocsNode(db, node.id, updates);
    await upsertKnowledgeSearchIndex(db, updated, updated.title);
    await appendKnowledgeRevision(
      db,
      updated,
      user.id,
      cleanString(body.update_reason, "案件情報Docs正本を更新", 200),
    );
    return NextResponse.json({ node: serializeNode(updated) });
  }

  const id = cleanString(body.id);
  if (!id) return NextResponse.json({ detail: "id is required" }, { status: 400 });

  if (kind === "qa") {
    const updates: Partial<typeof projectQaEntries.$inferInsert> = {
      updatedBy: user.id,
      updatedAt: new Date(),
    };
    if (body.question !== undefined) {
      const question = cleanString(body.question);
      if (!question) {
        return NextResponse.json({ detail: "question is required" }, { status: 400 });
      }
      updates.question = encryptText(question, "project_qa_entries.question");
      updates.normalizedQuestionHash = normalizedQuestionHash(question);
    }
    if (body.answer !== undefined) {
      const answer = cleanNullableString(body.answer);
      updates.answer = answer ? encryptText(answer, "project_qa_entries.answer") : null;
      if (answer && body.status === undefined) updates.status = "answered";
    }
    if (body.status !== undefined) {
      updates.status = oneOf(body.status, QA_STATUSES, "unanswered");
    }
    if (body.review_state !== undefined) {
      updates.reviewState = oneOf(body.review_state, QA_REVIEW_STATES, "candidate");
    }
    if (body.confidence !== undefined) {
      updates.confidence = numberValue(body.confidence, 1);
    }
    if (body.answer_source_refs !== undefined) {
      updates.answerSourceRefs = Array.isArray(body.answer_source_refs)
        ? body.answer_source_refs
        : [];
    }
    const [updated] = await db
      .update(projectQaEntries)
      .set(updates)
      .where(and(eq(projectQaEntries.id, id), eq(projectQaEntries.projectId, projectId)))
      .returning();
    return updated
      ? NextResponse.json({ qa_entry: decryptQaEntry(updated) })
      : NextResponse.json({ detail: "not found" }, { status: 404 });
  }

  return NextResponse.json({ detail: "Invalid kind" }, { status: 400 });
}

export async function DELETE(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }
  const { id: projectId } = await params;
  const access = await getWritableProject(projectId, user);
  if (!access) {
    return NextResponse.json(
      { detail: "プロジェクトが見つからないか権限がありません" },
      { status: 404 },
    );
  }
  const { searchParams } = new URL(request.url);
  const kind = searchParams.get("kind");
  const id = searchParams.get("id");
  if (kind !== "qa" || !id) {
    return NextResponse.json({ detail: "Invalid kind or id" }, { status: 400 });
  }

  const now = new Date();
  const [updated] = await db
    .update(projectQaEntries)
    .set({ status: "archived", deletedAt: now, updatedAt: now, updatedBy: user.id })
    .where(and(eq(projectQaEntries.id, id), eq(projectQaEntries.projectId, projectId)))
    .returning();
  return updated
    ? NextResponse.json({ ok: true })
    : NextResponse.json({ detail: "not found" }, { status: 404 });
}
