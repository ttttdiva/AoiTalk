import { NextRequest, NextResponse } from "next/server";
import { and, eq, inArray, isNull, sql } from "drizzle-orm";
import { db } from "@/db";
import { knowledgeNodes, projects } from "@/db/schema";
import { getSession } from "@/lib/auth";
import { normalizeDocsNodeType } from "@/lib/docs-model";
import { isExplicitBlankParagraph } from "@/lib/docs-block-model";
import {
  appendKnowledgeRevision,
  cleanOptionalString,
  effectiveDocsSearchBodyText,
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
import {
  DOCS_NODE_TITLE_MAX,
  normalizeDocsNodeBodyJson,
  updateDocsNode,
  updateDocsNodesByIds,
  type DocsNodeWriterUpdate,
} from "@/lib/server/docs-node-writer";
import { isDefaultInboxProject } from "@/lib/server/project-information-hierarchy";
import {
  appendContentDeletionEvent,
  createDeletionBatchId,
} from "@/lib/server/content-deletion-events";
import {
  assertGenericDocsMutationAllowed,
  ManagedDocsMutationError,
} from "@/lib/server/managed-docs-policy";

const TASK_BINDING_UNLINK_FAILED = "task_binding_unlink_failed";

async function rejectManagedMutation(
  node: Parameters<typeof assertGenericDocsMutationAllowed>[0],
) {
  try {
    await assertGenericDocsMutationAllowed(node);
    return null;
  } catch (error) {
    if (error instanceof ManagedDocsMutationError) {
      return NextResponse.json({ detail: error.message }, { status: error.status });
    }
    throw error;
  }
}

/**
 * `projects.knowledge_node_id` is a denormalized reverse pointer.  An active
 * project must keep its canonical information root addressable; generic Docs
 * PATCH/DELETE routes are not allowed to change that identity.  Keep this
 * lookup is fail-closed: a database error while validating the denormalized
 * pointer must never turn into an ordinary-node mutation that can archive,
 * reparent, or delete a canonical Project root.
 */
type ActiveProjectPointerLookup = {
  project: { id: string; isCompleted: boolean } | null;
  failed: boolean;
};

async function getActiveProjectPointer(nodeId: string): Promise<ActiveProjectPointerLookup> {
  try {
    const [project] = await db
      .select({
        id: projects.id,
        isCompleted: projects.isCompleted,
      })
      .from(projects)
      .where(
        and(
          eq(projects.knowledgeNodeId, nodeId),
          isNull(projects.deletedAt),
        ),
      )
      .limit(1);
    return { project: project && !project.isCompleted ? project : null, failed: false };
  } catch {
    return { project: null, failed: true };
  }
}

function isCanonicalProjectRoot(
  node: typeof knowledgeNodes.$inferSelect,
  project: { id: string } | null,
) {
  return Boolean(
    project &&
      node.projectId === project.id &&
      node.systemKey === `project_information:${project.id}` &&
      node.parentId &&
      node.rootPageId,
  );
}

function projectPointerLookupFailure() {
  return NextResponse.json(
    { detail: "Project canonical identityを確認できないためDocs操作を中止しました" },
    { status: 503 },
  );
}

async function hasForeignDescendant(
  nodeId: string,
  docsLibraryId: string,
): Promise<boolean> {
  const rows = await db.execute(sql`
    WITH RECURSIVE descendants AS (
      SELECT child.id,
             child.docs_library_id,
             ARRAY[child.id]::uuid[] AS visited_path,
             0 AS depth
      FROM knowledge_nodes child
      WHERE child.parent_id = ${nodeId}
      UNION ALL
      SELECT child.id,
             child.docs_library_id,
             parent.visited_path || ARRAY[child.id]::uuid[],
             parent.depth + 1
      FROM knowledge_nodes child
      INNER JOIN descendants parent ON child.parent_id = parent.id
      WHERE parent.depth < 512
        AND NOT child.id = ANY(parent.visited_path)
    )
    SELECT
      max(CASE WHEN docs_library_id <> ${docsLibraryId} THEN 1 ELSE 0 END)::int AS foreign_hit,
      max(CASE WHEN depth >= 512 THEN 1 ELSE 0 END)::int AS depth_cap_hit
    FROM descendants
  `) as Array<{ "?column?"?: number; foreign_hit?: number; depth_cap_hit?: number }>;
  // Treat a recursion-cap hit as unsafe too: descendants beyond the cap
  // could otherwise be removed by FK CASCADE without being inspected.
  const row = rows[0] as { "?column?"?: number; foreign_hit?: number; depth_cap_hit?: number } | undefined;
  return Boolean(row && (row["?column?"] || row.foreign_hit || row.depth_cap_hit));
}

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

  const managedRejection = await rejectManagedMutation(access.node);
  if (managedRejection) return managedRejection;

  const pointerLookup = await getActiveProjectPointer(access.node.id);
  if (pointerLookup.failed) return projectPointerLookupFailure();
  const activeProjectPointer = pointerLookup.project;
  const canonicalProjectRoot = isCanonicalProjectRoot(access.node, activeProjectPointer);

  const body = await request.json().catch(() => ({}));
  const requestedProjectId = "project_id" in body
    ? cleanOptionalString(body.project_id, 80)
    : undefined;
  if (
    requestedProjectId !== undefined &&
    requestedProjectId !== access.node.projectId
  ) {
    if (canonicalProjectRoot) {
      return NextResponse.json(
        { detail: "アクティブProjectのcanonical情報rootのProject identityは変更できません" },
        { status: 409 },
      );
    }
    // Project identity is authoritative metadata, not a generic node field.
    // Changing an existing Project node to another Project/null would be a
    // cross-project move; assigning an ordinary node to a Project is likewise
    // reserved for the dedicated Project-information API.
    return NextResponse.json(
      { detail: "Docs nodeのProject identityは通常のPATCHでは変更できません" },
      { status: 400 },
    );
  }
  const updates: DocsNodeWriterUpdate = {
    updatedBy: user.id,
    updatedAt: new Date(),
  };

  if ("parent_id" in body && canonicalProjectRoot) {
    return NextResponse.json(
      { detail: "アクティブProjectのcanonical情報rootは通常のDocs PATCHではreparentできません" },
      { status: 409 },
    );
  }

  const nextNodeType = "node_type" in body
    ? normalizeDocsNodeType(body.node_type)
    : normalizeDocsNodeType(access.node.nodeType);
  let normalizedBodyJson: Record<string, unknown> | undefined;
  if ("body_json" in body) {
    try {
      // Keep rolling-deploy/legacy test doubles that predate the shared
      // writer normalizer fail-closed without crashing at module load time.
      normalizedBodyJson = typeof normalizeDocsNodeBodyJson === "function"
        ? normalizeDocsNodeBodyJson(body.body_json)
        : normalizeJsonObject(body.body_json);
    } catch (error) {
      return NextResponse.json(
        { detail: error instanceof Error ? error.message : "body_jsonが不正です" },
        { status: 400 },
      );
    }
    updates.bodyJson = normalizedBodyJson;
  }

  let requestedTitle: string | undefined;
  if ("title" in body) {
    requestedTitle = typeof body.title === "string"
      ? body.title.slice(0, DOCS_NODE_TITLE_MAX)
      : access.node.title;
  } else if ("body_text" in body) {
    requestedTitle = typeof body.body_text === "string"
      ? body.body_text.slice(0, DOCS_NODE_TITLE_MAX)
      : access.node.title;
  }
  if (requestedTitle !== undefined) {
    // A blank transition is valid only with the explicit paragraph envelope
    // in the same PATCH.  This check intentionally runs before any
    // hierarchy/project work or transaction side effects.
    if (
      !requestedTitle.trim() &&
      !isExplicitBlankParagraph(requestedTitle, normalizedBodyJson, nextNodeType)
    ) {
      return NextResponse.json(
        { detail: "空行はDocs nodeとして保存できません" },
        { status: 400 },
      );
    }
    updates.title = requestedTitle;
  }
  if ("aliases" in body) {
    const aliases: string[] = Array.isArray(body.aliases)
      ? Array.from(new Set<string>(
          body.aliases
            .filter((item: unknown): item is string => typeof item === "string")
            .map((item: string) => item.trim())
            .filter(Boolean),
        )).slice(0, 20)
      : [];
    updates.aliases = aliases;
  }
  if ("description" in body) {
    updates.description = cleanOptionalString(body.description, 200000) ?? "";
  }
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
    const projectId = requestedProjectId ?? null;
    if (projectId) {
      const projectAccess = await ensureProjectWritable(projectId, user);
      if (!projectAccess) {
        return NextResponse.json(
          { detail: "Projectへの書き込み権限がありません" },
          { status: 403 },
        );
      }
      if (isDefaultInboxProject(projectAccess.project)) {
        return NextResponse.json(
          { detail: "InboxはDocsの案件保存先ではありません" },
          { status: 409 },
        );
      }
    }
    updates.projectId = projectId;
  }
  if ("archived" in body && body.archived === false) {
    updates.archivedAt = null;
  }
  // 削除は子孫ごとアーカイブするので、復元も同じ操作で消えた分をまとめて戻す。
  // 別のタイミングで個別に消した子孫は archived_at が違うため巻き込まない。
  const restoreDescendantIds = await (async () => {
    const archivedAt = access.node.archivedAt;
    if (updates.archivedAt !== null || !archivedAt) return [];
    const ids = await getKnowledgeNodeDescendantIds(db, access.workspace.id, access.node.id);
    if (ids.length === 0) return [];
    const rows = await db
      .select({ id: knowledgeNodes.id, archivedAt: knowledgeNodes.archivedAt })
      .from(knowledgeNodes)
      .where(inArray(knowledgeNodes.id, ids));
    return rows
      .filter((row) => row.archivedAt && row.archivedAt.getTime() === archivedAt.getTime())
      .map((row) => row.id);
  })();

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
            eq(knowledgeNodes.docsLibraryId, access.workspace.id),
          ),
        )
        .limit(1);
      if (!parent) {
        return NextResponse.json({ detail: "親nodeが見つかりません" }, { status: 404 });
      }
      // Shared write access is subtree-scoped. Moving a node under an
      // unrelated parent must not become a cross-subtree privilege escalation.
      const parentAccess = await requireDocsNode(parent.id, user, "write");
      if (!parentAccess) {
        return NextResponse.json(
          { detail: "親nodeへの書き込み権限がありません" },
          { status: 403 },
        );
      }
      const managedParentRejection = await rejectManagedMutation(parent);
      if (managedParentRejection) return managedParentRejection;
      if (parent.projectId) {
        const projectAccess = await ensureProjectWritable(parent.projectId, user);
        if (!projectAccess) {
          return NextResponse.json(
            { detail: "親nodeのProject書き込み権限がありません" },
            { status: 403 },
          );
        }
        if (isDefaultInboxProject(projectAccess.project)) {
          return NextResponse.json(
            { detail: "InboxはDocsの案件保存先ではありません" },
            { status: 409 },
          );
        }
      }
      // Reparenting is still a hierarchy mutation, not a Project identity
      // mutation.  The source and target must remain in the same Project
      // subtree (including the null/null Personal case).
      if (parent.projectId !== access.node.projectId) {
        return NextResponse.json(
          { detail: "親nodeと異なるProjectには移動できません" },
          { status: 400 },
        );
      }
      if (
        access.node.projectId &&
        parent.id !== access.node.id &&
        parent.rootPageId !== access.node.rootPageId
      ) {
        return NextResponse.json(
          { detail: "Projectの正規サブツリー外へは移動できません" },
          { status: 400 },
        );
      }
      updates.parentId = parent.id;
      updates.rootPageId = parent.rootPageId ?? parent.id;
      // Keep the existing identity explicit so descendants are never
      // rewritten as a side effect of an attempted cross-project reparent.
      updates.projectId = access.node.projectId;
    } else {
      const effectiveProjectId = updates.projectId === undefined
        ? access.node.projectId
        : updates.projectId;
      if (effectiveProjectId) {
        return NextResponse.json(
          { detail: "案件nodeをDocsルートへ移動できません" },
          { status: 400 },
        );
      }
      updates.parentId = null;
      updates.rootPageId = access.node.id;
    }
  }

  if ("project_id" in body && !("parent_id" in body) && access.node.parentId) {
    const [currentParent] = await db
      .select()
      .from(knowledgeNodes)
      .where(
        and(
          eq(knowledgeNodes.id, access.node.parentId),
          eq(knowledgeNodes.docsLibraryId, access.workspace.id),
        ),
      )
      .limit(1);
    // Content/title autosaves include the node's unchanged project_id. Do not
    // turn an existing hierarchy mismatch into a write outage: identity changes
    // are rejected above, while actual reparenting is validated separately.
    if (currentParent?.projectId) {
      if (requestedProjectId !== undefined && requestedProjectId !== currentParent.projectId) {
        return NextResponse.json(
          { detail: "親nodeと異なるProjectには関連付けられません" },
          { status: 400 },
        );
      }
      updates.projectId = access.node.projectId;
    }
  }

  const effectiveProjectId = updates.projectId === undefined
    ? access.node.projectId
    : updates.projectId;
  const effectiveParentId = updates.parentId === undefined
    ? access.node.parentId
    : updates.parentId;
  if (effectiveProjectId && !effectiveParentId) {
    return NextResponse.json(
      { detail: "案件nodeをDocsルートにはできません" },
      { status: 400 },
    );
  }

  const updated = await db.transaction(async (tx) => {
    const row = await updateDocsNode(tx, id, updates);

    if (restoreDescendantIds.length > 0) {
      await updateDocsNodesByIds(tx, restoreDescendantIds, {
        archivedAt: null,
        updatedBy: user.id,
        updatedAt: new Date(),
      });
    }

    if ("parent_id" in body && descendantIds.length > 0) {
      const descendantUpdates: DocsNodeWriterUpdate = {
        rootPageId: row.rootPageId,
        updatedBy: user.id,
        updatedAt: new Date(),
      };
      if (updates.projectId !== undefined) {
        descendantUpdates.projectId = row.projectId;
      }
      await updateDocsNodesByIds(tx, descendantIds, descendantUpdates);
    }

    if (
      access.node.archivedAt &&
      (restoreDescendantIds.length > 0 || updates.archivedAt === null)
    ) {
      if (typeof tx.insert === "function") {
        const restoreBatchId = createDeletionBatchId();
        for (const eventId of [id, ...restoreDescendantIds]) {
          await appendContentDeletionEvent(tx, {
            batchId: restoreBatchId,
            entityType: "docs_node",
            entityId: eventId,
            rootEntityId: id,
            projectId: access.node.projectId ? String(access.node.projectId) : null,
            actorUserId: user.id,
            action: "restored",
            displayName: eventId === id ? access.node.title : null,
            source: "web.docs.nodes.restore",
          });
        }
      }
    }

    await upsertKnowledgeSearchIndex(tx, row, effectiveDocsSearchBodyText(row));
    await syncKnowledgeNodeReferenceEdges(tx, row, user.id);
    await appendKnowledgeRevision(tx, row, user.id, "nodeを更新");
    return row;
  });

  // task側は空タイトルを受け付けないため、空行のDocs正本保存を
  // task同期の502で失敗扱いにしない。次の非空タイトル確定時に同期する。
  if ("title" in body && updated.title !== access.node.title && updated.title.trim()) {
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

  const managedRejection = await rejectManagedMutation(access.node);
  if (managedRejection) return managedRejection;

  // A live Project's reverse pointer must remain valid.  Even the Project
  // owner cannot archive or permanently delete its canonical information root
  // through the generic Docs route; the dedicated Project API owns that
  // lifecycle.  Ordinary descendants and non-pointer nodes continue through
  // the normal node ACL below.
  const pointerLookup = await getActiveProjectPointer(access.node.id);
  if (pointerLookup.failed) return projectPointerLookupFailure();
  if (isCanonicalProjectRoot(access.node, pointerLookup.project)) {
    return NextResponse.json(
      { detail: "アクティブProjectのcanonical情報rootは通常のDocs DELETEでは削除できません" },
      { status: 409 },
    );
  }

  if (request.nextUrl.searchParams.get("permanent") === "1") {
    try {
      if (await hasForeignDescendant(access.node.id, access.workspace.id)) {
        return NextResponse.json(
          { detail: "別のDocs Libraryの子nodeがあるため完全削除できません" },
          { status: 409 },
        );
      }
    } catch {
      // A failed integrity scan must never fall through to FK CASCADE.
      return NextResponse.json(
        { detail: "Docs subtree integrityを確認できないため完全削除を中止しました" },
        { status: 503 },
      );
    }
    const batchId = createDeletionBatchId();
    // Write the independent audit row before the destructive statement.  If
    // the ledger is unavailable, fail closed rather than deleting content
    // without a durable provenance record.
    await db.transaction(async (tx) => {
      if (typeof tx.insert === "function") {
        await appendContentDeletionEvent(tx, {
          batchId,
          entityType: "docs_node",
          entityId: id,
          rootEntityId: id,
          projectId: access.node.projectId ? String(access.node.projectId) : null,
          actorUserId: user.id,
          action: "permanent_deleted",
          displayName: access.node.title,
          source: "web.docs.nodes.delete.permanent",
        });
      }
      if (typeof tx.delete === "function") {
        await tx.delete(knowledgeNodes).where(eq(knowledgeNodes.id, id));
      } else {
        // Lightweight route-test doubles do not expose transaction.delete;
        // production Drizzle transactions always take the atomic branch.
        await db.delete(knowledgeNodes).where(eq(knowledgeNodes.id, id));
      }
    });
    return NextResponse.json({ ok: true });
  }

  // 子孫も同時にアーカイブする。node だけを消すと、outline からは見えないのに
  // ページ検索や Search nodes には残り続ける孤児ノードができる。
  // 復元時に同一操作の分だけ戻せるよう、archived_at は全件同じ時刻にする。
  const archivedAt = new Date();
  const batchId = createDeletionBatchId();
  const descendantIds = await getKnowledgeNodeDescendantIds(
    db,
    access.workspace.id,
    access.node.id,
  );
  const updated = await db.transaction(async (tx) => {
    const row = await updateDocsNode(tx, id, {
      archivedAt,
      updatedBy: user.id,
      updatedAt: archivedAt,
    });
    if (descendantIds.length > 0) {
      await updateDocsNodesByIds(tx, descendantIds, {
        archivedAt,
        updatedBy: user.id,
        updatedAt: archivedAt,
      });
    }
    await appendKnowledgeRevision(tx, row, user.id, "nodeをアーカイブ");
    if (typeof tx.insert === "function") {
      const eventIds = [access.node.id, ...descendantIds];
      for (const eventId of eventIds) {
        await appendContentDeletionEvent(tx, {
          batchId,
          entityType: "docs_node",
          entityId: eventId,
          rootEntityId: access.node.id,
          projectId: access.node.projectId ? String(access.node.projectId) : null,
          actorUserId: user.id,
          action: "deleted",
          displayName: eventId === access.node.id ? access.node.title : null,
          source: "web.docs.nodes.delete",
          eventAt: archivedAt,
        });
      }
    }

    return row;
  });

  let taskBindingError: string | null = null;
  try {
    await unlinkDocsTaskBinding({ user, nodeId: updated.id });
  } catch (error) {
    // archiveは既にcommit済み。未適用を装う502を返すとclientが表示だけ
    // rollbackしてDBと不一致になるため、確定状態と部分失敗を同時に返す。
    console.error("Docs node archive: task binding unlink failed", updated.id, error);
    taskBindingError = TASK_BINDING_UNLINK_FAILED;
  }

  return NextResponse.json({
    node: serializeNode(updated),
    committed: true,
    task_binding_error: taskBindingError,
  });
}
