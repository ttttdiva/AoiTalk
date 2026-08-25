import { NextRequest, NextResponse } from "next/server";
import fs from "node:fs";
import path from "node:path";
import { and, desc, eq, isNull, or } from "drizzle-orm";
import { db } from "@/db";
import {
  conversationMessages,
  knowledgeNodes,
  projects,
  taskAttachments,
  taskReferences,
  taskRelations,
  tasks,
} from "@/db/schema";
import { docsLibraries } from "@/lib/server/docs-library-schema";
import { getSession } from "@/lib/auth";
import {
  canReadProjectId,
  canWriteProjectId,
  type SessionUser,
} from "@/lib/server/task-route-utils";
import { ensureProjectStorageRoot } from "@/lib/server/project-workspace-management";
import { getLiveConversationSession } from "@/lib/server/conversation-route-utils";
import {
  canonicalizeTaskRelationIds,
  relatedTaskId,
  taskRelationReferenceId,
} from "@/lib/server/task-relations";

const REFERENCE_TYPES = new Set([
  "conversation_session",
  "conversation_message",
  "docs_node",
  "task",
  "workspace_file",
  "url",
]);
const RELATION_TYPES = new Set(["source", "related"]);
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

function missingReference(displayName?: string | null) {
  return {
    display_name: displayName || "参照先が見つかりません",
    subtitle: "参照先が見つかりません",
    exists: false,
  };
}

async function canReadProject(user: SessionUser, projectId: string | null) {
  if (!projectId) return true;
  return canReadProjectId(user, projectId);
}

async function loadTask(taskId: string) {
  const [task] = await db
    .select()
    .from(tasks)
    .where(and(eq(tasks.id, taskId), isNull(tasks.deletedAt)))
    .limit(1);
  return task ?? null;
}

async function requireTaskAccess(taskId: string, user: SessionUser, write = false) {
  const task = await loadTask(taskId);
  if (!task) return { task: null, response: NextResponse.json({ detail: "タスクが見つかりません" }, { status: 404 }) };
  if (!(await canReadProjectId(user, task.projectId))) {
    return { task: null, response: NextResponse.json({ detail: "タスクが見つかりません" }, { status: 404 }) };
  }
  if (write && !(await canWriteProjectId(user, task.projectId))) {
    return { task: null, response: NextResponse.json({ detail: "権限がありません" }, { status: 403 }) };
  }
  return { task, response: null };
}

async function serializeReference(
  row: typeof taskReferences.$inferSelect,
  user: SessionUser,
  canRemove: boolean,
) {
  const base = {
    id: row.id,
    reference_type: row.referenceType,
    relation_type: row.relationType,
    display_name: row.displayName,
    subtitle: null as string | null,
    target_id: row.targetId,
    target_path: row.targetPath,
    target_url: row.targetUrl,
    metadata: row.referenceMetadata ?? {},
    created_by: row.createdBy,
    created_at: row.createdAt,
    can_remove: canRemove,
    exists: true,
    open: {
      id: row.targetId,
      path: row.targetPath,
      url: row.targetUrl,
    },
  };

  if (row.referenceType === "conversation_session" || row.referenceType === "conversation_message") {
    const targetSessionId = row.targetId && UUID_RE.test(row.targetId) ? row.targetId : null;
    const session = targetSessionId
      ? await getLiveConversationSession(targetSessionId, user.id)
      : null;
    if (!session) return { ...base, ...missingReference(null) };
    const messageId = row.referenceType === "conversation_message"
      ? String((row.referenceMetadata as Record<string, unknown> | null)?.message_id ?? "")
      : "";
    if (row.referenceType === "conversation_message") {
      const [message] = messageId && UUID_RE.test(messageId)
        ? await db
            .select({ id: conversationMessages.id })
            .from(conversationMessages)
            .where(and(eq(conversationMessages.id, messageId), eq(conversationMessages.sessionId, session.id)))
            .limit(1)
        : [];
      if (!message) return { ...base, ...missingReference(row.displayName) };
    }
    return {
      ...base,
      display_name: session.title || row.displayName || "無題の会話",
      subtitle: messageId ? "発生元メッセージ" : "チャットセッション",
      exists: true,
      open: {
        id: session.id,
        path: messageId ? `/chat?s=${session.id}&message=${encodeURIComponent(messageId)}` : `/chat?s=${session.id}`,
        url: null,
      },
    };
  }

  if (row.referenceType === "docs_node") {
    const [node] = row.targetId && UUID_RE.test(row.targetId)
      ? await db
          .select({ node: knowledgeNodes, workspaceOwner: docsLibraries.ownerUserId })
          .from(knowledgeNodes)
          .innerJoin(docsLibraries, eq(knowledgeNodes.docsLibraryId, docsLibraries.id))
          .where(and(eq(knowledgeNodes.id, row.targetId), isNull(knowledgeNodes.archivedAt)))
          .limit(1)
      : [];
    const canRead = node
      ? node.node.projectId
        ? await canReadProject(user, node.node.projectId)
        : node.workspaceOwner === user.id || user.role === "admin"
      : false;
    if (!node || !canRead) return { ...base, ...missingReference(null) };
    return {
      ...base,
      display_name: node.node.title || row.displayName,
      subtitle: "Docs",
      open: { id: node.node.id, path: `/docs/${node.node.id}`, url: null },
    };
  }

  if (row.referenceType === "workspace_file") {
    const canRead = await canReadProject(user, row.projectId);
    if (!canRead || !row.targetPath) return { ...base, ...missingReference(null) };
    let exists = false;
    try {
      const root = ensureProjectStorageRoot(row.projectId);
      const target = path.resolve(root, row.targetPath);
      exists = target === root || target.startsWith(`${root}${path.sep}`) ? fs.existsSync(target) : false;
    } catch {
      exists = false;
    }
    return {
      ...base,
      ...(!exists ? missingReference(row.displayName) : {}),
      subtitle: "workspace",
      open: { id: row.projectId, path: row.targetPath, url: null },
    };
  }

  return {
    ...base,
    subtitle: "URL",
    exists: Boolean(row.targetUrl),
    open: { id: null, path: null, url: row.targetUrl },
  };
}

async function serializeTaskRelation(
  row: typeof taskRelations.$inferSelect,
  currentTaskId: string,
  user: SessionUser,
  canRemove: boolean,
) {
  const targetId = relatedTaskId(currentTaskId, row);
  const base = {
    id: taskRelationReferenceId(row.id),
    reference_type: "task",
    relation_type: row.relationType,
    display_name: "参照先が見つかりません",
    subtitle: "参照先が見つかりません",
    target_id: null as string | null,
    target_path: null,
    target_url: null,
    metadata: {},
    created_by: row.createdBy,
    created_at: row.createdAt,
    can_remove: canRemove,
    exists: false,
    open: {
      id: null as string | null,
      path: null as string | null,
      url: null,
    },
  };
  if (!targetId) return base;

  const [target] = await db
    .select({ task: tasks, projectName: projects.name })
    .from(tasks)
    .innerJoin(projects, eq(tasks.projectId, projects.id))
    .where(and(eq(tasks.id, targetId), isNull(tasks.deletedAt)))
    .limit(1);
  if (!target || !(await canReadProject(user, target.task.projectId))) {
    return base;
  }

  return {
    ...base,
    display_name: target.task.title,
    subtitle: `${target.projectName} · ${target.task.status}`,
    target_id: target.task.id,
    exists: true,
    open: {
      id: target.task.id,
      path: `/tasks?detail=${encodeURIComponent(target.task.id)}`,
      url: null,
    },
  };
}

async function serializeAttachment(
  row: typeof taskAttachments.$inferSelect,
  canRemove: boolean,
) {
  const root = ensureProjectStorageRoot(row.projectId);
  const target = path.resolve(root, row.filePath);
  const exists = target === root || target.startsWith(`${root}${path.sep}`) ? fs.existsSync(target) : false;
  return {
    id: `attachment:${row.id}`,
    reference_type: "task_attachment",
    relation_type: "related",
    display_name: row.displayName,
    subtitle: `${row.kind} · ${row.sizeBytes ?? 0} bytes`,
    target_id: row.id,
    target_path: row.filePath,
    target_url: `/api/tasks/${row.taskId}/attachments/${row.id}`,
    metadata: row.attachmentMetadata ?? {},
    created_by: row.createdBy,
    created_at: row.createdAt,
    can_remove: canRemove,
    exists,
    open: { id: row.id, path: row.filePath, url: `/api/tasks/${row.taskId}/attachments/${row.id}` },
    attachment: {
      id: row.id,
      task_id: row.taskId,
      project_id: row.projectId,
      file_path: row.filePath,
      display_name: row.displayName,
      mime_type: row.mimeType,
      size_bytes: row.sizeBytes,
      kind: row.kind,
      created_by: row.createdBy,
      created_at: row.createdAt,
      metadata: row.attachmentMetadata ?? {},
      url: `/api/tasks/${row.taskId}/attachments/${row.id}`,
    },
  };
}

export async function GET(
  _request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await getSession();
  if (!user) return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  const { id } = await params;
  const access = await requireTaskAccess(id, user);
  if (access.response) return access.response;
  const canRemove = access.task
    ? await canWriteProjectId(user, access.task.projectId)
    : false;
  const [refs, relations, attachments] = await Promise.all([
    db.select().from(taskReferences).where(eq(taskReferences.taskId, id)).orderBy(desc(taskReferences.createdAt)),
    db
      .select()
      .from(taskRelations)
      .where(or(eq(taskRelations.taskAId, id), eq(taskRelations.taskBId, id)))
      .orderBy(desc(taskRelations.createdAt)),
    db.select().from(taskAttachments).where(eq(taskAttachments.taskId, id)).orderBy(desc(taskAttachments.createdAt)),
  ]);
  const result = [
    ...(await Promise.all(refs.map((row) => serializeReference(row, user, canRemove)))),
    ...(await Promise.all(relations.map((row) => serializeTaskRelation(row, id, user, canRemove)))),
    ...(await Promise.all(attachments.map((row) => serializeAttachment(row, canRemove)))),
  ];
  if (access.task?.knowledgeNodeId) {
    const [node] = await db
      .select({
        id: knowledgeNodes.id,
        title: knowledgeNodes.title,
        projectId: knowledgeNodes.projectId,
        workspaceOwner: docsLibraries.ownerUserId,
      })
      .from(knowledgeNodes)
      .innerJoin(docsLibraries, eq(knowledgeNodes.docsLibraryId, docsLibraries.id))
      .where(and(eq(knowledgeNodes.id, access.task.knowledgeNodeId), isNull(knowledgeNodes.archivedAt)))
      .limit(1);
    const canRead = node
      ? node.projectId
        ? await canReadProject(user, node.projectId)
        : node.workspaceOwner === user.id || user.role === "admin"
      : false;
    result.push({
      id: `knowledge-node:${access.task.knowledgeNodeId}`,
      reference_type: "docs_node",
      relation_type: "related",
      display_name: canRead ? node?.title || "Docsノード" : "参照先が見つかりません",
      subtitle: canRead ? "タスクのDocs" : "参照先が見つかりません",
      target_id: access.task.knowledgeNodeId,
      target_path: null,
      target_url: null,
      metadata: {},
      created_by: null,
      created_at: access.task.createdAt,
      can_remove: canRead && canRemove,
      exists: canRead,
      open: { id: access.task.knowledgeNodeId, path: canRead ? `/docs/${access.task.knowledgeNodeId}` : null, url: null },
    });
  }
  return NextResponse.json(result);
}

export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await getSession();
  if (!user) return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  const { id } = await params;
  const access = await requireTaskAccess(id, user, true);
  if (access.response || !access.task) return access.response;
  const body = await request.json().catch(() => ({}));
  const referenceType = typeof body.reference_type === "string" ? body.reference_type : "";
  const relationType = typeof body.relation_type === "string" ? body.relation_type : "related";
  if (!REFERENCE_TYPES.has(referenceType) || !RELATION_TYPES.has(relationType)) {
    return NextResponse.json({ detail: "参照種別または関係種別が不正です" }, { status: 400 });
  }
  const targetId = typeof body.target_id === "string" && body.target_id.trim() ? body.target_id.trim() : null;
  const targetPath = typeof body.target_path === "string" && body.target_path.trim() ? body.target_path.trim() : null;
  const targetUrl = typeof body.target_url === "string" && body.target_url.trim() ? body.target_url.trim() : null;
  if ((referenceType === "conversation_session" || referenceType === "conversation_message" || referenceType === "docs_node" || referenceType === "task") && !targetId) {
    return NextResponse.json({ detail: "target_idは必須です" }, { status: 400 });
  }
  if (referenceType === "workspace_file" && !targetPath) {
    return NextResponse.json({ detail: "target_pathは必須です" }, { status: 400 });
  }
  if (referenceType === "workspace_file" && targetPath) {
    const normalized = targetPath.replaceAll("\\", "/");
    if (path.isAbsolute(targetPath) || normalized.split("/").some((part: string) => part === "..")) {
      return NextResponse.json({ detail: "プロジェクト内の相対パスを指定してください" }, { status: 400 });
    }
  }
  if (referenceType === "url" && (!targetUrl || !/^https?:\/\//i.test(targetUrl))) {
    return NextResponse.json({ detail: "http(s) URLを指定してください" }, { status: 400 });
  }
  if (referenceType === "task") {
    if (relationType !== "related") {
      return NextResponse.json({ detail: "タスクには関連のみ指定できます" }, { status: 400 });
    }
    if (!targetId || !UUID_RE.test(targetId)) {
      return NextResponse.json({ detail: "参照先IDが不正です" }, { status: 400 });
    }
    const canonicalIds = canonicalizeTaskRelationIds(id, targetId);
    if (!canonicalIds) {
      return NextResponse.json({ detail: "同じタスクは関連付けできません" }, { status: 400 });
    }
    const targetTask = await loadTask(targetId);
    if (!targetTask || !(await canReadProject(user, targetTask.projectId))) {
      return NextResponse.json({ detail: "参照先が見つからないか権限がありません" }, { status: 404 });
    }
    const [taskAId, taskBId] = canonicalIds;
    const [created] = await db
      .insert(taskRelations)
      .values({
        taskAId,
        taskBId,
        relationType: "related",
        createdBy: user.id,
      })
      .onConflictDoNothing({
        target: [
          taskRelations.taskAId,
          taskRelations.taskBId,
          taskRelations.relationType,
        ],
      })
      .returning();
    if (created) {
      return NextResponse.json(
        await serializeTaskRelation(created, id, user, true),
        { status: 201 },
      );
    }
    const [existing] = await db
      .select()
      .from(taskRelations)
      .where(
        and(
          eq(taskRelations.taskAId, taskAId),
          eq(taskRelations.taskBId, taskBId),
          eq(taskRelations.relationType, "related"),
        ),
      )
      .limit(1);
    if (!existing) {
      return NextResponse.json({ detail: "関連付けの作成に失敗しました" }, { status: 500 });
    }
    return NextResponse.json(
      await serializeTaskRelation(existing, id, user, true),
    );
  }
  if (referenceType === "conversation_session" || referenceType === "conversation_message") {
    if (!targetId || !UUID_RE.test(targetId)) {
      return NextResponse.json({ detail: "参照先IDが不正です" }, { status: 400 });
    }
    const session = targetId
      ? await getLiveConversationSession(targetId, user.id)
      : null;
    if (!session) {
      return NextResponse.json({ detail: "参照先が見つからないか権限がありません" }, { status: 404 });
    }
    if (referenceType === "conversation_message") {
      const messageId = body.metadata && typeof body.metadata === "object"
        ? (body.metadata as Record<string, unknown>).message_id
        : null;
      if (typeof messageId !== "string" || !UUID_RE.test(messageId)) {
        return NextResponse.json({ detail: "message_idが必要です" }, { status: 400 });
      }
      const [message] = await db
        .select({ id: conversationMessages.id })
        .from(conversationMessages)
        .where(and(eq(conversationMessages.id, messageId), eq(conversationMessages.sessionId, session.id)))
        .limit(1);
      if (!message) return NextResponse.json({ detail: "参照先メッセージが見つかりません" }, { status: 404 });
    }
  }
  if (referenceType === "docs_node") {
    if (!targetId || !UUID_RE.test(targetId)) {
      return NextResponse.json({ detail: "参照先IDが不正です" }, { status: 400 });
    }
    const [node] = targetId
      ? await db
          .select({ node: knowledgeNodes, workspaceOwner: docsLibraries.ownerUserId })
          .from(knowledgeNodes)
          .innerJoin(docsLibraries, eq(knowledgeNodes.docsLibraryId, docsLibraries.id))
          .where(and(eq(knowledgeNodes.id, targetId), isNull(knowledgeNodes.archivedAt)))
          .limit(1)
      : [];
    const canRead = node
      ? node.node.projectId
        ? await canReadProject(user, node.node.projectId)
        : node.workspaceOwner === user.id || user.role === "admin"
      : false;
    if (!node || !canRead) return NextResponse.json({ detail: "参照先が見つからないか権限がありません" }, { status: 404 });
  }
  if (referenceType === "workspace_file" && !(await canReadProject(user, access.task.projectId))) {
    return NextResponse.json({ detail: "参照先プロジェクトの権限がありません" }, { status: 403 });
  }
  const dedupeKey = `${targetId ?? ""}|${targetPath ?? ""}|${targetUrl ?? ""}`;
  const [existing] = await db.select().from(taskReferences).where(and(eq(taskReferences.taskId, id), eq(taskReferences.referenceType, referenceType), eq(taskReferences.relationType, relationType), eq(taskReferences.dedupeKey, dedupeKey))).limit(1);
  if (existing) return NextResponse.json(await serializeReference(existing, user, true));
  const [row] = await db.insert(taskReferences).values({
    taskId: id,
    projectId: access.task.projectId,
    referenceType,
    relationType,
    targetId,
    targetPath,
    targetUrl,
    displayName: typeof body.display_name === "string" && body.display_name.trim() ? body.display_name.trim() : (targetUrl || targetPath || targetId || "参照"),
    dedupeKey,
    referenceMetadata: body.metadata && typeof body.metadata === "object" ? body.metadata : {},
    createdBy: user.id,
  }).returning();
  return NextResponse.json(await serializeReference(row, user, true), { status: 201 });
}
