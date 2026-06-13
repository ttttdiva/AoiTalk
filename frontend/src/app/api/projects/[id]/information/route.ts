import { NextRequest, NextResponse } from "next/server";
import { and, desc, eq, isNull } from "drizzle-orm";
import { db } from "@/db";
import {
  projectDocuments,
  projectFacts,
  projectInfoCategories,
  recordTables,
} from "@/db/schema";
import { getSession } from "@/lib/auth";
import { decryptTextIfNeeded, encryptText } from "@/lib/server/field-crypto";
import {
  getAccessibleProject,
  getWritableProject,
} from "@/lib/server/project-access";
import { normalizeProjectManagementConfig } from "@/lib/server/project-workspace-management";

const DEFAULT_CATEGORIES = [
  {
    key: "overview",
    label: "概要",
    description: "案件の入口として最低限の目的・範囲・前提を置きます。",
    status: "active",
    source: "template",
    sortOrder: 0,
  },
  {
    key: "important_documents",
    label: "重要資料",
    description: "パラメーターシート、構成図、設計書など作業時に参照する正本資料です。",
    status: "active",
    source: "template",
    sortOrder: 10,
  },
  {
    key: "decisions",
    label: "決定事項",
    description: "顧客・社内・ベンダー間で決まったことと、その出典を置きます。",
    status: "active",
    source: "template",
    sortOrder: 20,
  },
  {
    key: "open_questions",
    label: "要確認",
    description: "未確定事項、回答待ち、確認依頼を置きます。",
    status: "active",
    source: "template",
    sortOrder: 30,
  },
  {
    key: "architecture",
    label: "構成",
    description: "構成図、接続関係、環境一覧などがある案件で使います。",
    status: "suggested",
    source: "template",
    sortOrder: 40,
  },
  {
    key: "detail_design",
    label: "詳細設計",
    description: "パラメーターシート、設定値、設計書を扱う案件で使います。",
    status: "suggested",
    source: "template",
    sortOrder: 50,
  },
  {
    key: "verification",
    label: "検証",
    description: "テスト計画、検証項目、結果報告を扱う案件で使います。",
    status: "suggested",
    source: "template",
    sortOrder: 60,
  },
];

const CATEGORY_STATUSES = new Set(["active", "suggested", "hidden", "archived"]);
const ITEM_STATUSES = new Set(["active", "suggested", "archived"]);
const TARGET_KINDS = new Set(["file", "record_table", "url"]);
const AI_ACCESS_LEVELS = new Set(["metadata", "read", "edit", "blocked"]);

function cleanString(value: unknown, fallback = ""): string {
  if (typeof value !== "string") return fallback;
  const trimmed = value.trim();
  return trimmed || fallback;
}

function cleanNullableString(value: unknown): string | null {
  const text = cleanString(value);
  return text || null;
}

function slugKey(value: string): string {
  return (
    value
      .trim()
      .toLowerCase()
      .replace(/[^a-z0-9_\-\u3040-\u30ff\u3400-\u9fff]+/g, "_")
      .replace(/^_+|_+$/g, "")
      .slice(0, 120) || `category_${Date.now().toString(36)}`
  );
}

function oneOf(value: unknown, allowed: Set<string>, fallback: string): string {
  const text = cleanString(value, fallback);
  return allowed.has(text) ? text : fallback;
}

function numberValue(value: unknown, fallback: number): number {
  const n = Number(value);
  return Number.isFinite(n) ? n : fallback;
}

function decryptFact<T extends { content: string }>(fact: T): T {
  return {
    ...fact,
    content: decryptTextIfNeeded(fact.content, "project_facts.content") || "",
  };
}

async function ensureDefaultCategories(projectId: string, userId: string) {
  const existing = await db
    .select({ key: projectInfoCategories.key })
    .from(projectInfoCategories)
    .where(eq(projectInfoCategories.projectId, projectId));
  const existingKeys = new Set(existing.map((row) => row.key));
  const missing = DEFAULT_CATEGORIES.filter((item) => !existingKeys.has(item.key));
  if (missing.length === 0) return;

  await db.insert(projectInfoCategories).values(
    missing.map((item) => ({
      projectId,
      key: item.key,
      label: item.label,
      description: item.description,
      status: item.status,
      source: item.source,
      sortOrder: item.sortOrder,
      createdBy: userId,
    })),
  );
}

async function categoryBelongsToProject(projectId: string, categoryId: unknown) {
  const id = cleanNullableString(categoryId);
  if (!id) return null;
  const [category] = await db
    .select()
    .from(projectInfoCategories)
    .where(
      and(
        eq(projectInfoCategories.id, id),
        eq(projectInfoCategories.projectId, projectId),
      ),
    )
    .limit(1);
  return category ? id : null;
}

function managementDocuments(project: { projectMetadata: unknown }) {
  const config = normalizeProjectManagementConfig(project.projectMetadata);
  const docs: Array<Record<string, unknown>> = [];
  if (config.wbsFile) {
    docs.push({
      id: `management:wbs:${config.wbsFile}`,
      title: "WBS",
      document_type: "wbs",
      target_kind: "file",
      file_path: config.wbsFile,
      role: "management",
      status: "active",
      source_type: "project_management",
      synthetic: true,
    });
  }
  if (config.issueFile) {
    docs.push({
      id: `management:issue:${config.issueFile}`,
      title: "課題管理表",
      document_type: "issue",
      target_kind: "file",
      file_path: config.issueFile,
      role: "management",
      status: "active",
      source_type: "project_management",
      synthetic: true,
    });
  }
  if (config.riskFile) {
    docs.push({
      id: `management:risk:${config.riskFile}`,
      title: "リスク管理表",
      document_type: "risk",
      target_kind: "file",
      file_path: config.riskFile,
      role: "management",
      status: "active",
      source_type: "project_management",
      synthetic: true,
    });
  }
  for (const filePath of config.requestFiles) {
    docs.push({
      id: `management:request:${filePath}`,
      title: filePath.split("/").at(-1) || "補助資料",
      document_type: "support",
      target_kind: "file",
      file_path: filePath,
      role: "reference",
      status: "active",
      source_type: "project_management",
      synthetic: true,
    });
  }
  return docs;
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

  await ensureDefaultCategories(projectId, user.id);

  const [categories, documents, facts, tables] = await Promise.all([
    db
      .select()
      .from(projectInfoCategories)
      .where(eq(projectInfoCategories.projectId, projectId))
      .orderBy(projectInfoCategories.sortOrder, projectInfoCategories.createdAt),
    db
      .select()
      .from(projectDocuments)
      .where(
        and(
          eq(projectDocuments.projectId, projectId),
          isNull(projectDocuments.deletedAt),
        ),
      )
      .orderBy(desc(projectDocuments.isPrimary), desc(projectDocuments.updatedAt)),
    db
      .select()
      .from(projectFacts)
      .where(
        and(eq(projectFacts.projectId, projectId), isNull(projectFacts.deletedAt)),
      )
      .orderBy(desc(projectFacts.importance), desc(projectFacts.updatedAt)),
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
  ]);

  return NextResponse.json({
    project: {
      id: access.project.id,
      name: access.project.name,
      description: access.project.description,
    },
    categories,
    documents,
    management_documents: managementDocuments(access.project),
    facts: facts.map(decryptFact),
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

  const body = await request.json().catch(() => ({}));
  const kind = cleanString(body.kind);

  if (kind === "category") {
    const label = cleanString(body.label);
    if (!label) return NextResponse.json({ detail: "label is required" }, { status: 400 });
    const key = slugKey(cleanString(body.key, label));
    const [created] = await db
      .insert(projectInfoCategories)
      .values({
        projectId,
        key,
        label,
        description: cleanNullableString(body.description),
        status: oneOf(body.status, CATEGORY_STATUSES, "active"),
        source: "manual",
        sortOrder: numberValue(body.sort_order, Date.now()),
        createdBy: user.id,
      })
      .returning();
    return NextResponse.json({ category: created }, { status: 201 });
  }

  if (kind === "document") {
    const title = cleanString(body.title);
    if (!title) return NextResponse.json({ detail: "title is required" }, { status: 400 });
    const targetKind = oneOf(body.target_kind, TARGET_KINDS, "file");
    const categoryId = await categoryBelongsToProject(projectId, body.category_id);
    const [created] = await db
      .insert(projectDocuments)
      .values({
        projectId,
        categoryId,
        title,
        description: cleanNullableString(body.description),
        documentType: cleanString(body.document_type, "document"),
        targetKind,
        filePath: targetKind === "file" ? cleanNullableString(body.file_path) : null,
        recordTableId:
          targetKind === "record_table" ? cleanNullableString(body.record_table_id) : null,
        externalUrl: targetKind === "url" ? cleanNullableString(body.external_url) : null,
        role: cleanString(body.role, "reference"),
        isPrimary: Boolean(body.is_primary),
        aiAccessLevel: oneOf(body.ai_access_level, AI_ACCESS_LEVELS, "metadata"),
        status: oneOf(body.status, ITEM_STATUSES, "active"),
        notes: cleanNullableString(body.notes),
        sourceType: cleanString(body.source_type, "manual"),
        sourceRef: cleanNullableString(body.source_ref),
        createdBy: user.id,
      })
      .returning();
    return NextResponse.json({ document: created }, { status: 201 });
  }

  if (kind === "fact") {
    const title = cleanString(body.title);
    const content = cleanString(body.content);
    if (!title || !content) {
      return NextResponse.json(
        { detail: "title and content are required" },
        { status: 400 },
      );
    }
    const categoryId = await categoryBelongsToProject(projectId, body.category_id);
    const [created] = await db
      .insert(projectFacts)
      .values({
        projectId,
        categoryId,
        title,
        content: encryptText(content, "project_facts.content"),
        factType: cleanString(body.fact_type, "fact"),
        confidence: numberValue(body.confidence, 1),
        importance: Math.max(1, Math.min(10, Math.round(numberValue(body.importance, 5)))),
        status: oneOf(body.status, ITEM_STATUSES, "active"),
        sourceType: cleanString(body.source_type, "manual"),
        sourceRef: cleanNullableString(body.source_ref),
        sourceDocumentId: cleanNullableString(body.source_document_id),
        sourceTaskId: cleanNullableString(body.source_task_id),
        createdBy: user.id,
      })
      .returning();
    return NextResponse.json({ fact: decryptFact(created) }, { status: 201 });
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

  const body = await request.json().catch(() => ({}));
  const kind = cleanString(body.kind);
  const id = cleanString(body.id);
  if (!id) return NextResponse.json({ detail: "id is required" }, { status: 400 });

  if (kind === "category") {
    const updates: Partial<typeof projectInfoCategories.$inferInsert> = {
      updatedAt: new Date(),
    };
    if (body.label !== undefined) updates.label = cleanString(body.label);
    if (body.description !== undefined) {
      updates.description = cleanNullableString(body.description);
    }
    if (body.status !== undefined) {
      updates.status = oneOf(body.status, CATEGORY_STATUSES, "active");
    }
    if (body.sort_order !== undefined) {
      updates.sortOrder = numberValue(body.sort_order, 0);
    }
    const [updated] = await db
      .update(projectInfoCategories)
      .set(updates)
      .where(
        and(
          eq(projectInfoCategories.id, id),
          eq(projectInfoCategories.projectId, projectId),
        ),
      )
      .returning();
    return updated
      ? NextResponse.json({ category: updated })
      : NextResponse.json({ detail: "not found" }, { status: 404 });
  }

  if (kind === "document") {
    const categoryId =
      body.category_id === undefined
        ? undefined
        : await categoryBelongsToProject(projectId, body.category_id);
    const updates: Partial<typeof projectDocuments.$inferInsert> = {
      updatedAt: new Date(),
    };
    if (body.title !== undefined) updates.title = cleanString(body.title);
    if (body.description !== undefined) {
      updates.description = cleanNullableString(body.description);
    }
    if (body.category_id !== undefined) updates.categoryId = categoryId;
    if (body.document_type !== undefined) {
      updates.documentType = cleanString(body.document_type, "document");
    }
    if (body.target_kind !== undefined) {
      updates.targetKind = oneOf(body.target_kind, TARGET_KINDS, "file");
    }
    if (body.file_path !== undefined) updates.filePath = cleanNullableString(body.file_path);
    if (body.record_table_id !== undefined) {
      updates.recordTableId = cleanNullableString(body.record_table_id);
    }
    if (body.external_url !== undefined) {
      updates.externalUrl = cleanNullableString(body.external_url);
    }
    if (body.role !== undefined) updates.role = cleanString(body.role, "reference");
    if (body.is_primary !== undefined) updates.isPrimary = Boolean(body.is_primary);
    if (body.ai_access_level !== undefined) {
      updates.aiAccessLevel = oneOf(body.ai_access_level, AI_ACCESS_LEVELS, "metadata");
    }
    if (body.status !== undefined) {
      updates.status = oneOf(body.status, ITEM_STATUSES, "active");
    }
    if (body.notes !== undefined) updates.notes = cleanNullableString(body.notes);
    const [updated] = await db
      .update(projectDocuments)
      .set(updates)
      .where(
        and(eq(projectDocuments.id, id), eq(projectDocuments.projectId, projectId)),
      )
      .returning();
    return updated
      ? NextResponse.json({ document: updated })
      : NextResponse.json({ detail: "not found" }, { status: 404 });
  }

  if (kind === "fact") {
    const categoryId =
      body.category_id === undefined
        ? undefined
        : await categoryBelongsToProject(projectId, body.category_id);
    const updates: Partial<typeof projectFacts.$inferInsert> = {
      updatedAt: new Date(),
    };
    if (body.title !== undefined) updates.title = cleanString(body.title);
    if (body.content !== undefined) {
      updates.content = encryptText(
        cleanString(body.content),
        "project_facts.content",
      );
    }
    if (body.category_id !== undefined) updates.categoryId = categoryId;
    if (body.fact_type !== undefined) updates.factType = cleanString(body.fact_type, "fact");
    if (body.importance !== undefined) {
      updates.importance = Math.max(
        1,
        Math.min(10, Math.round(numberValue(body.importance, 5))),
      );
    }
    if (body.status !== undefined) {
      updates.status = oneOf(body.status, ITEM_STATUSES, "active");
    }
    if (body.source_ref !== undefined) updates.sourceRef = cleanNullableString(body.source_ref);
    const [updated] = await db
      .update(projectFacts)
      .set(updates)
      .where(and(eq(projectFacts.id, id), eq(projectFacts.projectId, projectId)))
      .returning();
    return updated
      ? NextResponse.json({ fact: decryptFact(updated) })
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

  const body = await request.json().catch(() => ({}));
  const kind = cleanString(body.kind);
  const id = cleanString(body.id);
  if (!id) return NextResponse.json({ detail: "id is required" }, { status: 400 });

  if (kind === "category") {
    await db
      .update(projectInfoCategories)
      .set({ status: "archived", updatedAt: new Date() })
      .where(
        and(
          eq(projectInfoCategories.id, id),
          eq(projectInfoCategories.projectId, projectId),
        ),
      );
    return NextResponse.json({ success: true });
  }
  if (kind === "document") {
    await db
      .update(projectDocuments)
      .set({ deletedAt: new Date(), updatedAt: new Date() })
      .where(
        and(eq(projectDocuments.id, id), eq(projectDocuments.projectId, projectId)),
      );
    return NextResponse.json({ success: true });
  }
  if (kind === "fact") {
    await db
      .update(projectFacts)
      .set({ deletedAt: new Date(), updatedAt: new Date() })
      .where(and(eq(projectFacts.id, id), eq(projectFacts.projectId, projectId)));
    return NextResponse.json({ success: true });
  }

  return NextResponse.json({ detail: "Invalid kind" }, { status: 400 });
}
