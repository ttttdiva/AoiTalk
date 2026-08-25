import { NextRequest, NextResponse } from "next/server";
import { createHash } from "crypto";
import { and, asc, desc, eq, inArray, isNull, or, sql } from "drizzle-orm";
import { db } from "@/db";
import {
  knowledgeNodes,
  knowledgeFields,
  knowledgeFieldValues,
  knowledgeNodeSupertags,
  knowledgeSupertagFields,
  knowledgeSupertags,
  projectQaEntries,
  projects,
  recordTables,
} from "@/db/schema";
import { docsLibraries } from "@/lib/server/docs-library-schema";
import { getSession } from "@/lib/auth";
import { decryptTextIfNeeded, encryptText } from "@/lib/server/field-crypto";
import {
  appendKnowledgeRevision,
  getKnowledgeNodeDescendantIds,
  normalizeFieldValueInput,
  serializeNode,
  serializeNodeSupertag,
  serializeField,
  serializeSupertagField,
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
  getProjectInformationHierarchyNode,
  ensureProjectInformationHierarchyNode,
  isDefaultInboxProject,
} from "@/lib/server/project-information-hierarchy";
import { proxyRequestToPythonApi } from "@/lib/server/python-api-proxy";

const QA_STATUSES = new Set([
  "unanswered",
  "answered",
  "stale",
  "cancelled",
  "archived",
]);
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

/**
 * Invalidate the derived Project Context Pack only after the surrounding
 * Project Information mutation has completed successfully.  The canonical
 * Docs write remains the source of truth; this bridge merely marks the
 * projection stale and deliberately does not make a failed invalidation turn
 * an otherwise committed mutation into an error.
 */
async function invalidateProjectContextPack(
  request: NextRequest,
  projectId: string,
  user: { id: string; username?: string | null; role?: string | null },
) {
  try {
    // The mutation request itself may be POST, PATCH, or DELETE.  Build a
    // separate POST bridge request so the FastAPI invalidation endpoint never
    // accidentally receives the original mutation method/body.
    const bridgeRequest = new NextRequest(
      new URL(
        `/api/projects/${encodeURIComponent(projectId)}/context-pack/invalidate`,
        request.url,
      ),
      {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ reason: "project_information_changed" }),
      },
    );
    const response = await proxyRequestToPythonApi(bridgeRequest, {
      path: ["projects", projectId, "context-pack", "invalidate"],
      user,
    });
    if (!response.ok) {
      console.warn(
        "Project Context Pack invalidation failed after Project Information mutation:",
        response.status,
      );
    }
  } catch (error) {
    console.warn(
      "Project Context Pack invalidation bridge failed after Project Information mutation:",
      error,
    );
  }
}

export async function ensureProjectInformationFieldValues(
  node: typeof knowledgeNodes.$inferSelect,
  supertag: typeof knowledgeSupertags.$inferSelect,
  project: typeof projects.$inferSelect,
  userId: string,
) {
  const fields = (await db
    .select()
    .from(knowledgeFields)
    .where(
      and(
        eq(knowledgeFields.supertagId, supertag.id),
        // A malformed field can point at the canonical supertag while still
        // belonging to another Docs Library.  Definitions used for the
        // canonical Project node must stay inside the tag/library boundary.
        eq(knowledgeFields.docsLibraryId, supertag.docsLibraryId),
        eq(knowledgeFields.docsLibraryId, node.docsLibraryId),
      ),
    ))
    // Keep a defensive in-memory boundary as well.  It protects repair code
    // if a malformed row is returned by a compatibility/migration query or
    // a future query refactor accidentally drops one of the SQL predicates.
    .filter(
      (field) =>
        field.docsLibraryId === node.docsLibraryId &&
        field.docsLibraryId === supertag.docsLibraryId,
    );
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

function serializeLibrary(library: typeof docsLibraries.$inferSelect | null | undefined) {
  if (!library) return null;
  return {
    id: library.id,
    library_id: library.id,
    docs_library_id: library.id,
    name: library.name,
    description: library.description,
    owner_user_id: library.ownerUserId,
    library_type: library.libraryType ?? "personal",
    settings: library.settingsJson ?? {},
    created_at: library.createdAt instanceof Date ? library.createdAt.toISOString() : library.createdAt,
    updated_at: library.updatedAt instanceof Date ? library.updatedAt.toISOString() : library.updatedAt,
  };
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
  const hierarchyNode = await ensureProjectInformationHierarchyNode({
    userId: user.id,
    project,
  });
  const [library] = await db
    .select()
    .from(docsLibraries)
    .where(eq(docsLibraries.id, hierarchyNode.docsLibraryId))
    .limit(1);
  if (!library) throw new Error("Project owner Personal Docs Library is missing");
  const candidateSupertags = await db
    .select()
    .from(knowledgeSupertags)
    .where(
      and(
        eq(knowledgeSupertags.docsLibraryId, library.id),
        or(
          eq(knowledgeSupertags.systemKey, PROJECT_INFORMATION_SYSTEM_KEY),
          eq(knowledgeSupertags.name, PROJECT_INFORMATION_SUPERTAG),
          eq(knowledgeSupertags.name, "案件情報"),
        ),
      ),
    )
  const supertag = candidateSupertags.find((tag) => tag.systemKey === PROJECT_INFORMATION_SYSTEM_KEY)
    ?? candidateSupertags.find((tag) => tag.name === PROJECT_INFORMATION_SUPERTAG || tag.name === "案件情報")
    ?? null;

  if (!supertag) {
    throw new Error("案件情報スーパータグが見つかりません");
  }
  const node = await updateDocsNode(db, hierarchyNode.id, {
    bodyJson: projectInformationBodyJson(),
    updatedBy: user.id,
    updatedAt: new Date(),
  });
  await ensureProjectInformationFieldValues(node, supertag, project, user.id);
  await upsertKnowledgeSearchIndex(db, node, node.title);
  return { node, supertag, library };
}

/**
 * Read phase used by GET.  It does not mutate the hierarchy itself; the GET
 * handler may follow up with the idempotent repair helper for active projects
 * whose canonical node/pointer is missing or stale.
 */
async function readProjectInformationDocument(
  _user: { id: string; role?: string | null },
  project: typeof projects.$inferSelect,
) {
  const hierarchy = await getProjectInformationHierarchyNode({ project });
  const library = hierarchy.library;
  if (!library) return { ...hierarchy, supertag: null };
  const candidateSupertags = await db
    .select()
    .from(knowledgeSupertags)
    .where(
      and(
        eq(knowledgeSupertags.docsLibraryId, library.id),
        or(
          eq(knowledgeSupertags.systemKey, PROJECT_INFORMATION_SYSTEM_KEY),
          eq(knowledgeSupertags.name, PROJECT_INFORMATION_SUPERTAG),
          eq(knowledgeSupertags.name, "案件情報"),
        ),
      ),
    )
  const supertag = candidateSupertags.find((tag) => tag.systemKey === PROJECT_INFORMATION_SYSTEM_KEY)
    ?? candidateSupertags.find((tag) => tag.name === PROJECT_INFORMATION_SUPERTAG || tag.name === "案件情報")
    ?? null;
  return { ...hierarchy, supertag };
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
  if (access === undefined) {
    return NextResponse.json(
      { detail: "プロジェクトが見つかりません" },
      { status: 404 },
    );
  }
  if (access === null) {
    return NextResponse.json({ detail: "権限がありません" }, { status: 403 });
  }
  if (isDefaultInboxProject(access.project)) {
    return NextResponse.json(
      { detail: "Inboxは案件情報Docsの保存先ではありません" },
      { status: 409 },
    );
  }

  let document: Awaited<ReturnType<typeof readProjectInformationDocument>>;
  try {
    document = await readProjectInformationDocument(user, access.project);
  } catch (error) {
    console.warn("Project information Docs GET read failed:", error);
    return NextResponse.json(
      {
        detail: "案件情報Docsを読み込めませんでした",
        code: "project_information_unavailable",
      },
      { status: 500 },
    );
  }
  // The Projects tab is a repair boundary for active, non-Inbox projects
  // created before the canonical Personal Docs hierarchy was introduced.
  // A missing/stale reverse pointer must be repaired before returning the
  // DTO; if the caller cannot repair it, return an explicit conflict rather
  // than serializing a null/stale node that would crash the UI.
  if (!access.project.isCompleted && !isDefaultInboxProject(access.project)) {
    const pointerNeedsRepair =
      !document.node || document.node.id !== access.project.knowledgeNodeId;
    if (pointerNeedsRepair) {
      // A read-only member may navigate an existing hierarchy, but GET must
      // not create/repair the owner's Personal hub. Restrict this idempotent
      // repair path to a Project writer (owner or write member).
      let writable: Awaited<ReturnType<typeof getWritableProject>> = null;
      try {
        writable = await getWritableProject(projectId, user);
      } catch (error) {
        console.warn("Project information Docs write access check failed:", error);
      }
      if (!writable) {
        return NextResponse.json(
          {
            detail: "案件情報Docsの初期化にはProject書き込み権限が必要です",
            code: "project_information_unavailable",
          },
          { status: 409 },
        );
      }
      try {
        const repaired = await ensureProjectInformationDocument(user, access.project);
        document = {
          ...document,
          node: repaired.node,
          supertag: repaired.supertag,
          library: repaired.library,
        } as typeof document;
      } catch (error) {
        console.warn("Project information Docs GET self-repair failed:", error);
        return NextResponse.json(
          {
            detail: "案件情報Docsの初期化に失敗しました",
            code: "project_information_unavailable",
          },
          { status: 409 },
        );
      }
    }
  }
  if (!document.node) {
    return NextResponse.json(
      {
        detail: "案件情報Docsが未初期化です",
        code: "project_information_unavailable",
      },
      { status: 409 },
    );
  }
  const node = document.node;
  const supertag = document.supertag;
  const library = document.library;
  const hierarchyNodeIds = node
    ? [node.id, ...(await getKnowledgeNodeDescendantIds(db, node.docsLibraryId, node.id))]
    : [];
  const [qaEntries, tables, treeNodes] = await Promise.all([
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
    node
      ? db
          .select()
          .from(knowledgeNodes)
          .where(and(
            eq(knowledgeNodes.projectId, projectId),
            eq(knowledgeNodes.docsLibraryId, node.docsLibraryId),
            hierarchyNodeIds.length > 0 ? inArray(knowledgeNodes.id, hierarchyNodeIds) : undefined,
            isNull(knowledgeNodes.archivedAt),
          ))
          .orderBy(asc(knowledgeNodes.sortOrder), asc(knowledgeNodes.createdAt))
      : Promise.resolve([]),
  ]);
  const treeNodeIds = treeNodes.map((entry) => entry.id);
  const treeNodeSupertags = treeNodeIds.length > 0
    ? await db
        .select()
        .from(knowledgeNodeSupertags)
        .where(inArray(knowledgeNodeSupertags.nodeId, treeNodeIds))
    : [];
  // Project-information metadata is scoped to definitions actually attached
  // to this project's visible subtree.  Returning every definition in the
  // owner's Personal Library would expose unrelated/private tags and fields
  // to a Project member.
  const attachedSupertagIds = Array.from(new Set(treeNodeSupertags.map((row) => row.supertagId)));
  const projectSupertags = library && attachedSupertagIds.length > 0
    ? await db
        .select()
        .from(knowledgeSupertags)
        .where(
          and(
            eq(knowledgeSupertags.docsLibraryId, library.id),
            inArray(knowledgeSupertags.id, attachedSupertagIds),
          ),
        )
    : [];
  const validProjectSupertagIds = new Set(projectSupertags.map((tag) => tag.id));
  const validTreeNodeSupertags = treeNodeSupertags.filter((row) => validProjectSupertagIds.has(row.supertagId));
  const projectFields = library && attachedSupertagIds.length > 0
    ? await db
        .select()
        .from(knowledgeFields)
        .where(
          and(
            eq(knowledgeFields.docsLibraryId, library.id),
            inArray(knowledgeFields.supertagId, attachedSupertagIds),
          ),
        )
    : [];
  const projectSupertagFields = library && attachedSupertagIds.length > 0 && projectFields.length > 0
    ? await db
        .select({ relation: knowledgeSupertagFields })
        .from(knowledgeSupertagFields)
        .innerJoin(knowledgeSupertags, eq(knowledgeSupertagFields.supertagId, knowledgeSupertags.id))
        .where(
          and(
            eq(knowledgeSupertags.docsLibraryId, library.id),
            inArray(knowledgeSupertagFields.supertagId, attachedSupertagIds),
            inArray(knowledgeSupertagFields.fieldId, projectFields.map((field) => field.id)),
          ),
        )
        .then((rows) => rows.map((row) => row.relation))
    : [];

  return NextResponse.json({
    project: {
      id: access.project.id,
      name: access.project.name,
      description: access.project.description,
      // The resolved canonical node is the only value safe to expose as the
      // authoritative reverse pointer. A stale legacy pointer must not make a
      // missing document look initialized.
      knowledge_node_id: node?.id ?? null,
      docs_library_id: library?.id ?? null,
      library: serializeLibrary(library),
    },
    library: serializeLibrary(library),
    docs_library_id: library?.id ?? null,
    node: node ? serializeNode(node) : null,
    tree_nodes: treeNodes.map(serializeNode),
    node_supertags: validTreeNodeSupertags.map(serializeNodeSupertag),
    fields: projectFields.map(serializeField),
    supertag_fields: projectSupertagFields.map(serializeSupertagField),
    supertags: projectSupertags.map(serializeSupertag),
    supertag: supertag && validProjectSupertagIds.has(supertag.id)
      ? serializeSupertag(supertag)
      : null,
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
  if (access === undefined) {
    return NextResponse.json(
      { detail: "プロジェクトが見つかりません" },
      { status: 404 },
    );
  }
  if (access === null) {
    return NextResponse.json({ detail: "権限がありません" }, { status: 403 });
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
    const questionHash = normalizedQuestionHash(question);
    const result = await db.transaction(async (tx) => {
      await tx.execute(
        sql`select pg_advisory_xact_lock(hashtext(${`project-qa:${access.project.id}`}))`,
      );
      const candidates = await tx
        .select()
        .from(projectQaEntries)
        .where(
          and(
            eq(projectQaEntries.projectId, projectId),
            or(
              eq(projectQaEntries.normalizedQuestionHash, questionHash),
              isNull(projectQaEntries.normalizedQuestionHash),
            ),
          ),
        );
      const existing = candidates.find((entry) => {
        if (entry.normalizedQuestionHash === questionHash) return true;
        if (entry.normalizedQuestionHash !== null) return false;
        const plainQuestion =
          decryptTextIfNeeded(entry.question, "project_qa_entries.question") || "";
        return normalizedQuestionHash(plainQuestion) === questionHash;
      });
      if (existing) return { existing, created: null };

      const [created] = await tx
        .insert(projectQaEntries)
        .values({
          projectId,
          knowledgeNodeId: node.id,
          question: encryptText(question, "project_qa_entries.question"),
          answer: answer ? encryptText(answer, "project_qa_entries.answer") : null,
          normalizedQuestionHash: questionHash,
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
      return { existing: null, created };
    });
    if (result.existing) {
      return NextResponse.json(
        {
          detail: "The same project question already exists.",
          qa_entry: decryptQaEntry(result.existing),
        },
        { status: 409 },
      );
    }
    const created = result.created!;
    await invalidateProjectContextPack(request, projectId, user);
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
        docsLibraryId: node.docsLibraryId,
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
        docsLibraryId: node.docsLibraryId,
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
    await invalidateProjectContextPack(request, projectId, user);
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
  if (access === undefined) {
    return NextResponse.json(
      { detail: "プロジェクトが見つかりません" },
      { status: 404 },
    );
  }
  if (access === null) {
    return NextResponse.json({ detail: "権限がありません" }, { status: 403 });
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
    const hierarchy = await getProjectInformationHierarchyNode({ project: access.project });
    const canonicalNode = hierarchy.node;
    if (!canonicalNode) {
      return NextResponse.json(
        { detail: "案件情報Docsのcanonical nodeが未初期化または不正です" },
        { status: 409 },
      );
    }
    // `ensureProjectInformationDocument` returns the strict canonical node:
    // owner Personal Library + active hub parent/root + exact project/system
    // key + attached project_info tag.  A generic (or foreign-library) node
    // must never be accepted merely because its project_id matches.
    if (nodeId !== canonicalNode.id) {
      return NextResponse.json(
        { detail: "案件情報Docsのcanonical nodeのみ更新できます" },
        { status: 409 },
      );
    }
    if (!canonicalNode.parentId || !canonicalNode.rootPageId) {
      return NextResponse.json(
        { detail: "案件情報Docsのcanonical hierarchyが不正です" },
        { status: 409 },
      );
    }
    const [candidateNode] = await db
      .select()
      .from(knowledgeNodes)
      .where(
        and(
          eq(knowledgeNodes.id, nodeId),
          eq(knowledgeNodes.docsLibraryId, canonicalNode.docsLibraryId),
          eq(knowledgeNodes.projectId, projectId),
          eq(knowledgeNodes.parentId, canonicalNode.parentId),
          eq(knowledgeNodes.rootPageId, canonicalNode.rootPageId),
          eq(knowledgeNodes.systemKey, `project_information:${projectId}`),
          isNull(knowledgeNodes.archivedAt),
        ),
      )
      .limit(1);
    if (!candidateNode) {
      return NextResponse.json({ detail: "node not found" }, { status: 404 });
    }

    const updates: DocsNodeWriterUpdate = {
      updatedBy: user.id,
      updatedAt: new Date(),
    };
    if (body.title !== undefined) {
      updates.title = cleanString(body.title, canonicalNode.title, 500);
    }
    if (body.body_text !== undefined) {
      updates.title = cleanString(body.body_text, updates.title ?? canonicalNode.title, 500);
    }
    if (body.body_json !== undefined) {
      updates.bodyJson = jsonObject(body.body_json);
    }

    const updated = await updateDocsNode(db, canonicalNode.id, updates);
    await upsertKnowledgeSearchIndex(db, updated, updated.title);
    await appendKnowledgeRevision(
      db,
      updated,
      user.id,
      cleanString(body.update_reason, "案件情報Docs正本を更新", 200),
    );
    await invalidateProjectContextPack(request, projectId, user);
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
    const result = await db.transaction(async (tx) => {
      await tx.execute(
        sql`select pg_advisory_xact_lock(hashtext(${`project-qa:${access.project.id}`}))`,
      );
      if (updates.normalizedQuestionHash) {
        const candidates = await tx
          .select()
          .from(projectQaEntries)
          .where(
            and(
              eq(projectQaEntries.projectId, projectId),
              or(
                eq(
                  projectQaEntries.normalizedQuestionHash,
                  updates.normalizedQuestionHash,
                ),
                isNull(projectQaEntries.normalizedQuestionHash),
              ),
            ),
          );
        const collision = candidates.find((entry) => {
          if (entry.id === id) return false;
          if (entry.normalizedQuestionHash === updates.normalizedQuestionHash) return true;
          if (entry.normalizedQuestionHash !== null) return false;
          const plainQuestion =
            decryptTextIfNeeded(entry.question, "project_qa_entries.question") || "";
          return normalizedQuestionHash(plainQuestion) === updates.normalizedQuestionHash;
        });
        if (collision) return { collision, updated: null };
      }
      const [updated] = await tx
        .update(projectQaEntries)
        .set(updates)
        .where(and(eq(projectQaEntries.id, id), eq(projectQaEntries.projectId, projectId)))
        .returning();
      return { collision: null, updated };
    });
    if (result.collision) {
      return NextResponse.json(
        {
          detail: "The same project question already exists.",
          qa_entry: decryptQaEntry(result.collision),
        },
        { status: 409 },
      );
    }
    const updated = result.updated;
    if (!updated) {
      return NextResponse.json({ detail: "not found" }, { status: 404 });
    }
    await invalidateProjectContextPack(request, projectId, user);
    return NextResponse.json({ qa_entry: decryptQaEntry(updated) });
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
  if (access === undefined) {
    return NextResponse.json(
      { detail: "プロジェクトが見つかりません" },
      { status: 404 },
    );
  }
  if (access === null) {
    return NextResponse.json({ detail: "権限がありません" }, { status: 403 });
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
  if (!updated) {
    return NextResponse.json({ detail: "not found" }, { status: 404 });
  }
  await invalidateProjectContextPack(request, projectId, user);
  return NextResponse.json({ ok: true });
}
