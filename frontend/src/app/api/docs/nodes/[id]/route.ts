import { NextRequest, NextResponse } from "next/server";
import { and, asc, eq, inArray } from "drizzle-orm";
import { db } from "@/db";
import { knowledgeNodes, projects } from "@/db/schema";
import { getSession } from "@/lib/auth";
import { normalizeDocsNodeType } from "@/lib/docs-model";
import { isExplicitBlankParagraph } from "@/lib/docs-block-model";
import {
  appendKnowledgeRevision,
  cleanOptionalString,
  DOCS_DELETION_MAX_DESCENDANT_DEPTH,
  effectiveDocsSearchBodyText,
  ensureProjectWritable,
  getAllProjectKnowledgeNodePointerIds,
  getKnowledgeDisplayDescendantIds,
  getKnowledgeNodeDescendantIds,
  lockDocsDeletionClosureSnapshot,
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
  DocsNodeInvariantError,
  normalizeDocsNodeBodyJson,
  updateDocsNodeLifecycle,
  updateDocsNode,
  updateDocsNodesByIds,
  type DocsNodeWriterUpdate,
} from "@/lib/server/docs-node-writer";
import * as projectInformationHierarchy from "@/lib/server/project-information-hierarchy";
import {
  appendContentDeletionEvent,
  createDeletionBatchId,
} from "@/lib/server/content-deletion-events";
import {
  assertGenericDocsMutationAllowed,
  lockAndAssertGenericDocsMutationAllowed,
  managedDocsDomain,
  ManagedDocsAccessError,
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
 * project must keep its canonical information root addressable; the generic
 * Docs PATCH route is not allowed to change that identity. Keep this lookup
 * fail-closed: a database error while validating the denormalized
 * pointer must never turn into an ordinary-node mutation that can archive,
 * reparent, or delete a canonical Project root.
 */
type ActiveProjectPointerLookup = {
  project: typeof projects.$inferSelect | null;
  retainedProject: typeof projects.$inferSelect | null;
  failed: boolean;
};

async function getActiveProjectPointer(nodeId: string): Promise<ActiveProjectPointerLookup> {
  try {
    const projectsForNode = await db
      .select()
      .from(projects)
      .where(
        and(
          eq(projects.knowledgeNodeId, nodeId),
        ),
      )
      .limit(2);
    if (projectsForNode.length > 1) {
      return { project: null, retainedProject: null, failed: true };
    }
    const project = projectsForNode[0];
    return {
      project: project && !project.deletedAt && !project.isCompleted ? project : null,
      retainedProject: project && (project.deletedAt || project.isCompleted) ? project : null,
      failed: false,
    };
  } catch {
    return { project: null, retainedProject: null, failed: true };
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

/**
 * Resolve the reverse pointer against the complete Project-information
 * hierarchy.  The strict resolver is optional at runtime so rolling deploys
 * and focused route-test doubles that predate it continue to use the legacy
 * structural fallback; production always takes the fail-closed strict path.
 */
async function resolveCanonicalProjectRoot(
  node: typeof knowledgeNodes.$inferSelect,
  pointer: typeof projects.$inferSelect | null,
) {
  if (!pointer) return null;
  let strictResolver: typeof projectInformationHierarchy.resolveProjectInformationNode | undefined;
  try {
    strictResolver = projectInformationHierarchy.resolveProjectInformationNode;
  } catch {
    // Vitest/rolling-deploy module doubles may omit this newer export.
    strictResolver = undefined;
  }
  // Legacy route-test doubles (and rolling deployments before the strict
  // resolver landed) often expose only `{id,isCompleted}`.  A real Project
  // row always carries owner/name metadata; use the structural fallback only
  // for those intentionally incomplete doubles.
  if (
    typeof strictResolver === "function"
    && typeof pointer.ownerId === "string"
    && typeof pointer.name === "string"
  ) {
    try {
      const resolution = await strictResolver({
        project: pointer,
        includeInactive: true,
      });
      if (
        resolution.node?.id === node.id &&
        (resolution.status === "active" || resolution.status === "retained")
      ) {
        return { project: pointer, resolution };
      }
      return null;
    } catch {
      // A strict resolver/database failure is not an ordinary-node signal.
      // Returning null lets the caller fail closed before mutation below.
      throw new Error("Project canonical identityを確認できないためDocs操作を中止しました");
    }
  }
  return isCanonicalProjectRoot(node, pointer) ? { project: pointer, resolution: null } : null;
}

function projectPointerLookupFailure() {
  return NextResponse.json(
    { detail: "Project canonical identityを確認できないためDocs操作を中止しました" },
    { status: 503 },
  );
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
  const retainedProjectPointer = pointerLookup.retainedProject;
  const pointerProject = activeProjectPointer ?? retainedProjectPointer;
  let canonicalProjectResolution: Awaited<ReturnType<typeof resolveCanonicalProjectRoot>> = null;
  try {
    canonicalProjectResolution = await resolveCanonicalProjectRoot(
      access.node,
      pointerProject,
    );
  } catch {
    return projectPointerLookupFailure();
  }
  const canonicalProjectRoot = Boolean(canonicalProjectResolution);
  const systemKey = String(access.node.systemKey ?? "").trim();
  if (systemKey === "project_information_root") {
    return NextResponse.json(
      { detail: "案件情報hubは通常のDocs PATCHでは変更できません" },
      { status: 409 },
    );
  }
  if (systemKey.startsWith("project_information:") && !canonicalProjectRoot) {
    return NextResponse.json(
      { detail: "stale案件情報の正本nodeは専用クリーンアップ/修復経路でのみ変更できます" },
      { status: 409 },
    );
  }
  if (retainedProjectPointer && canonicalProjectRoot) {
    return NextResponse.json(
      { detail: "完了/削除済みProjectのcanonical情報rootは通常のDocs PATCHで変更できません" },
      { status: 409 },
    );
  }
  if (pointerProject && !canonicalProjectRoot) {
    // A persisted Project pointer is authoritative even when the target row
    // no longer satisfies the hierarchy contract (wrong library/parent/tag,
    // archived, or stale).  Do not fail open and mutate it as ordinary Docs.
    return NextResponse.json(
      {
        detail: retainedProjectPointer
          ? "完了/削除済みProjectのstale canonical nodeは通常のDocs PATCHで変更できません"
          : "Project canonical hierarchyが不正なためDocs PATCHを中止しました",
      },
      { status: 409 },
    );
  }

  const body = await request.json().catch(() => ({}));
  if (
    canonicalProjectRoot
    && ["node_type", "query_json", "day_date", "aliases", "display_props", "view_json"]
      .some((field) => field in body)
  ) {
    return NextResponse.json(
      { detail: "アクティブProjectのcanonical情報rootの構造/表示属性は専用APIで管理されます" },
      { status: 409 },
    );
  }
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
  if (canonicalProjectRoot && canonicalProjectResolution) {
    // Project metadata owns the canonical root label.  Generic Docs autosave
    // may send an empty string (or a stale arbitrary rename), but must never
    // pass that value through the writer and leave a blank/corrupted identity.
    const projectName = canonicalProjectResolution.project.name;
    requestedTitle = typeof projectName === "string" && projectName.trim()
      ? projectName.trim()
      : "案件情報";
    // The outline editor sends its ordinary blank envelope when a row is
    // cleared.  A canonical Project root is not an ordinary paragraph: keep
    // its Project-information body intact rather than replacing the
    // encrypted canonical document metadata with a paragraph blank marker.
  }
  if (
    canonicalProjectRoot
    && normalizedBodyJson
    && isExplicitBlankParagraph("", normalizedBodyJson, nextNodeType)
  ) {
    delete updates.bodyJson;
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
      if (projectInformationHierarchy.isDefaultInboxProject(projectAccess.project)) {
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
  // 削除は子孫ごとアーカイブするので、復元も同じ操作の timestamp を
  // transaction 内で再確認して戻す。 preflight の配列は競合中に stale になり得る。
  let restoreDescendantIds: string[] = [];

  let descendantIds: string[] = [];
  if ("parent_id" in body) {
    if (String(access.node.systemKey ?? "").trim() === "project_information_root") {
      return NextResponse.json(
        { detail: "案件情報hubは通常のDocs PATCHでは移動できません" },
        { status: 409 },
      );
    }
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
    if (descendantIds.length > 0) {
      try {
        const pointerRows = await db
          .select({ id: projects.id })
          .from(projects)
          .where(inArray(projects.knowledgeNodeId, [access.node.id, ...descendantIds]))
          .limit(1);
        if (pointerRows.length > 0) {
          return NextResponse.json(
            { detail: "Projectが参照するDocs nodeを含むため通常のDocs PATCHでは移動できません" },
            { status: 409 },
          );
        }
      } catch {
        return projectPointerLookupFailure();
      }
    }
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
      if (parent.archivedAt) {
        return NextResponse.json({ detail: "アーカイブ済みnodeの下には移動できません" }, { status: 409 });
      }
      if (String(parent.systemKey ?? "").trim() === "project_information_root") {
        return NextResponse.json({ detail: "案件情報hub直下への通常のDocs PATCH移動はできません" }, { status: 409 });
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
        if (projectInformationHierarchy.isDefaultInboxProject(projectAccess.project)) {
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

  let updated: typeof knowledgeNodes.$inferSelect;
  try {
    updated = await db.transaction(async (tx) => {
      let lockedPointerRows: Array<typeof projects.$inferSelect> = [];
      let lockedTarget: typeof knowledgeNodes.$inferSelect = access.node;
      let restoreParentId: string | null = null;
      let earlyDescendantSnapshotIds: string[] = [];
      const lifecycleOnlyRestoreRequest =
        "archived" in body
        && body.archived === false
        && Object.keys(body).every((key) => key === "archived")
        && updates.archivedAt === null;
      if (typeof tx.select === "function") {
        // Project pointer writers use the Project->node lock order.  Acquire
        // the reverse pointer (and, for a strict canonical resolution, the
        // identified Project row) before the target node so a concurrent
        // repair cannot assign a pointer to a row that is being mutated.
        if (access.node.projectId) {
          const [lockedEffectiveProject] = await tx
            .select({
              id: projects.id,
              knowledgeNodeId: projects.knowledgeNodeId,
              isCompleted: projects.isCompleted,
              deletedAt: projects.deletedAt,
            })
            .from(projects)
            .where(eq(projects.id, access.node.projectId))
            .for("update")
            .limit(1);
          if (
            !lockedEffectiveProject
            || lockedEffectiveProject.deletedAt
            || lockedEffectiveProject.isCompleted
          ) {
            throw new DocsNodeInvariantError(
              "完了/削除済みProjectのDocs nodeは通常の更新で変更できません",
            );
          }
          if (
            canonicalProjectResolution
            && lockedEffectiveProject.knowledgeNodeId !== id
          ) {
            throw new DocsNodeInvariantError(
              "Project canonical identityが同時変更されたため更新を中止しました",
            );
          }
        }
        // Every PATCH path eventually rechecks the managed policy for the
        // target.  Gather the target's ancestor chain up front and lock the
        // whole node set lexically before any individual target/parent lock;
        // content-only PATCH and lifecycle restore must not take target-first
        // locks that invert the generic Docs policy.
        if (lockedPointerRows.length === 0) {
          lockedPointerRows = await tx
            .select()
            .from(projects)
            .where(eq(projects.knowledgeNodeId, id))
            .for("update");
          if (canonicalProjectResolution && lockedPointerRows.length === 0) {
            const [lockedCanonicalProject] = await tx
              .select()
              .from(projects)
              .where(
                and(
                  eq(projects.id, canonicalProjectResolution.project.id),
                  eq(projects.knowledgeNodeId, id),
                ),
              )
              .limit(1)
              .for("update");
            if (lockedCanonicalProject) lockedPointerRows = [lockedCanonicalProject];
          }
        }
        {
          // Opposite concurrent reparents can otherwise lock each source
          // target first and then wait on the other's destination ancestor.
          // Gather both paths before locking any node and acquire their union
          // in deterministic lexical order. Project/reverse-pointer rows
          // remain locked first to preserve the canonical repair order.
          const earlyDescendantIds = "parent_id" in body
            ? await getKnowledgeNodeDescendantIds(
              tx,
              access.workspace.id,
              id,
            )
            : [];
          earlyDescendantSnapshotIds = earlyDescendantIds;
          // A lifecycle-only restore also mutates the complete archived
          // subtree. Include that closure in this first lexical node-lock
          // pass; otherwise the later descendant lock could invert the
          // target/descendant order against a concurrent delete/archive.
          const earlyRestoreDescendantIds = lifecycleOnlyRestoreRequest
            ? await getKnowledgeNodeDescendantIds(
              tx,
              access.workspace.id,
              id,
            )
            : [];
          const earlyClosureIds = [
            id,
            ...earlyDescendantIds,
            ...earlyRestoreDescendantIds,
          ];
          if ("parent_id" in body) {
            const earlyPointers = await tx
              .select({ id: projects.id })
              .from(projects)
              .where(inArray(projects.knowledgeNodeId, earlyClosureIds))
              .orderBy(asc(projects.id))
              .for("update");
            if (earlyPointers.length > 0) {
              throw new DocsNodeInvariantError(
                "Projectが参照するDocs nodeを含むため通常のPATCH移動では変更できません",
              );
            }
          }
          const earlyDestinationIds: string[] = [];
          const earlySeen = new Set<string>();
          let earlyDestinationId = updates.parentId ?? null;
          for (let depth = 0; earlyDestinationId; depth += 1) {
            if (depth >= 512 || earlySeen.has(earlyDestinationId)) {
              throw new DocsNodeInvariantError("移動先Docsの親階層が循環しています");
            }
            earlySeen.add(earlyDestinationId);
            const [destination] = await tx
              .select({ id: knowledgeNodes.id, parentId: knowledgeNodes.parentId })
              .from(knowledgeNodes)
              .where(
                and(
                  eq(knowledgeNodes.id, earlyDestinationId),
                  eq(knowledgeNodes.docsLibraryId, access.workspace.id),
                ),
              )
              .limit(1);
            if (!destination) {
              throw new DocsNodeInvariantError("移動先Docsの親階層を確認できません");
            }
            earlyDestinationIds.push(destination.id);
            earlyDestinationId = destination.parentId;
          }
          const earlyNodeLockIds = new Set<string>([
            ...earlyClosureIds,
            ...earlyDestinationIds,
          ]);
          // Content/lifecycle PATCHes do not have a destination path, but the
          // managed policy still consults the target's complete ancestor
          // chain. Include that chain here so target/parent rows are acquired
          // in the same lexical order for every mutation shape.
          const ancestorSeen = new Set<string>();
          let ancestorId: string | null = access.node.parentId;
          for (let depth = 0; ancestorId; depth += 1) {
            if (depth >= 512 || ancestorSeen.has(ancestorId)) {
              throw new DocsNodeInvariantError("Docs parent hierarchyが深すぎるか循環しています");
            }
            ancestorSeen.add(ancestorId);
            earlyNodeLockIds.add(ancestorId);
            const [ancestor] = await tx
              .select({ id: knowledgeNodes.id, parentId: knowledgeNodes.parentId })
              .from(knowledgeNodes)
              .where(
                and(
                  eq(knowledgeNodes.id, ancestorId),
                  eq(knowledgeNodes.docsLibraryId, access.workspace.id),
                ),
              )
              .limit(1);
            if (!ancestor) {
              throw new DocsNodeInvariantError("Docs nodeの親階層を確認できません");
            }
            ancestorId = ancestor.parentId;
          }
          const orderedEarlyNodeLockIds = [...earlyNodeLockIds].sort();
          if (orderedEarlyNodeLockIds.length > 0) {
            await tx
              .select({ id: knowledgeNodes.id })
              .from(knowledgeNodes)
              .where(
                and(
                  eq(knowledgeNodes.docsLibraryId, access.workspace.id),
                  inArray(knowledgeNodes.id, orderedEarlyNodeLockIds),
                ),
              )
              .orderBy(asc(knowledgeNodes.id))
              .for("update");
          }
        }
        if (lifecycleOnlyRestoreRequest) {
          const [targetSnapshot] = await tx
            .select({ id: knowledgeNodes.id, parentId: knowledgeNodes.parentId })
            .from(knowledgeNodes)
            .where(eq(knowledgeNodes.id, id))
            .limit(1);
          restoreParentId = targetSnapshot?.parentId ?? null;
          const ancestors: Array<{ id: string; parentId: string | null }> = [];
          const seenAncestors = new Set<string>();
          let ancestorId = restoreParentId;
          while (ancestorId && !seenAncestors.has(ancestorId) && ancestors.length < 512) {
            seenAncestors.add(ancestorId);
            const [ancestor] = await tx
              .select({ id: knowledgeNodes.id, parentId: knowledgeNodes.parentId })
              .from(knowledgeNodes)
              .where(and(eq(knowledgeNodes.id, ancestorId), eq(knowledgeNodes.docsLibraryId, access.workspace.id)))
              .limit(1);
            if (!ancestor) break;
            ancestors.push(ancestor);
            ancestorId = ancestor.parentId;
          }
          if (ancestorId) {
            throw new DocsNodeInvariantError("Docs parent hierarchyが深すぎるか循環しています");
          }
          // The complete target/ancestor closure was already locked in
          // lexical order above. Re-read each parent without taking a second
          // root→target lock sequence and fail closed on an archived parent.
          for (const ancestor of ancestors) {
            const [lockedAncestor] = await tx
              .select({ id: knowledgeNodes.id, archivedAt: knowledgeNodes.archivedAt })
              .from(knowledgeNodes)
              .where(and(eq(knowledgeNodes.id, ancestor.id), eq(knowledgeNodes.docsLibraryId, access.workspace.id)))
              .limit(1);
            if (!lockedAncestor || lockedAncestor.archivedAt) {
              throw new DocsNodeInvariantError("アーカイブ済み親nodeの子だけを復元することはできません");
            }
          }
        }
        // Lock and re-read the target node after the Project row. A concurrent
        // archive/delete that changed its lifecycle since preflight must make
        // this request retry, not produce a partial restore.
        const [targetRow] = await tx
          .select()
          .from(knowledgeNodes)
          .where(eq(knowledgeNodes.id, id))
          .for("update");
        if (!targetRow) {
          throw new DocsNodeInvariantError("Docs nodeが同時に削除されたため更新できません");
        }
        lockedTarget = targetRow;
        // The preflight managed-policy check is not a write boundary. Re-read
        // and lock the complete target/ancestor chain before any lifecycle or
        // content update so a concurrent reparent cannot attach this ordinary
        // node below the AoiTalk Guide between the two checks.
        await lockAndAssertGenericDocsMutationAllowed(lockedTarget, tx, user);
        if (lockedTarget.projectId !== access.node.projectId) {
          throw new DocsNodeInvariantError(
            "Docs nodeのProject所属が同時変更されたため更新を中止しました",
          );
        }
        if (lifecycleOnlyRestoreRequest && lockedTarget.parentId !== restoreParentId) {
          throw new DocsNodeInvariantError("Docs nodeの親階層が同時変更されたため復元を中止しました");
        }
        const accessArchivedAt = access.node.archivedAt?.getTime() ?? null;
        const lockedArchivedAt = lockedTarget.archivedAt?.getTime() ?? null;
        if (accessArchivedAt !== lockedArchivedAt) {
          throw new DocsNodeInvariantError("Docs nodeのライフサイクルが同時変更されたため更新を中止しました");
        }
        if (updates.archivedAt === null && lockedTarget.archivedAt) {
          const restoreCandidates = await getKnowledgeNodeDescendantIds(
            tx,
            access.workspace.id,
            id,
          );
          if (restoreCandidates.length > 0) {
            // Compare a second structural snapshot before locking any
            // descendant.  A child committed after the first snapshot may be
            // locked by a concurrent archive/delete while that writer waits
            // for our target; taking the child lock here would invert the
            // lexical closure order and create a deadlock.  Fail closed while
            // only the already-locked target/ancestor rows are held.
            const freshRestoreCandidates = await getKnowledgeNodeDescendantIds(
              tx,
              access.workspace.id,
              id,
            );
            const candidateSet = new Set(restoreCandidates);
            if (
              freshRestoreCandidates.length !== candidateSet.size
              || freshRestoreCandidates.some((nodeId) => !candidateSet.has(nodeId))
            ) {
              throw new DocsNodeInvariantError("復元対象のDocs subtreeが同時変更されたため復元を中止しました");
            }
            const lockedRestoreRows = await tx
              .select({ id: knowledgeNodes.id, archivedAt: knowledgeNodes.archivedAt })
              .from(knowledgeNodes)
              .where(
                and(
                  eq(knowledgeNodes.docsLibraryId, access.workspace.id),
                  inArray(knowledgeNodes.id, restoreCandidates),
                ),
              )
              .orderBy(asc(knowledgeNodes.id))
              .for("update");
            restoreDescendantIds = lockedRestoreRows
              .filter((row) => row.archivedAt && row.archivedAt.getTime() === lockedTarget.archivedAt!.getTime())
              .map((row) => row.id);
          }
        }
        if (lockedPointerRows.length > 0 && !canonicalProjectRoot) {
          throw new DocsNodeInvariantError(
            "Project canonical identityが変更されたためDocs更新を中止しました",
          );
        }
        if (canonicalProjectRoot && lockedPointerRows.length !== 1) {
          throw new DocsNodeInvariantError(
            "Project canonical identityが変更されたためDocs更新を中止しました",
          );
        }
        const lockedPointer = lockedPointerRows[0];
        if (lockedPointer && (lockedPointer.deletedAt || lockedPointer.isCompleted)) {
          throw new DocsNodeInvariantError(
            "完了/削除済みProjectのcanonical情報rootは通常のDocs PATCHで変更できません",
          );
        }
        if (lockedPointer) updates.title = lockedPointer.name.trim() || "案件情報";
      }
      if ("parent_id" in body && typeof tx.select === "function") {
        // Recompute the structural closure inside the transaction instead of
        // trusting the preflight snapshot.  A child inserted between those
        // reads must either be included in the mutation or cause a safe
        // retry, never become an orphan with stale root/project metadata.
        const lockedSnapshotDescendantIds = await getKnowledgeNodeDescendantIds(
          tx,
          access.workspace.id,
          id,
        );
        // The target/known closure is already locked lexically above.  Do
        // not lock a newly discovered child after the target: a concurrent
        // delete/move may own that child and wait for the target, which would
        // invert the global lexical order.  A changed snapshot is safe to
        // reject before acquiring any additional row lock.
        const earlyDescendantSet = new Set(earlyDescendantSnapshotIds);
        if (
          lockedSnapshotDescendantIds.length !== earlyDescendantSet.size
          || lockedSnapshotDescendantIds.some((nodeId) => !earlyDescendantSet.has(nodeId))
        ) {
          throw new DocsNodeInvariantError(
            "Docs subtreeが同時更新されたためreparentを中止しました",
          );
        }
        const closureIds = [id, ...lockedSnapshotDescendantIds];
        const identityRows = await tx
          .select({ id: knowledgeNodes.id, systemKey: knowledgeNodes.systemKey })
          .from(knowledgeNodes)
          .where(inArray(knowledgeNodes.id, closureIds));
        if (identityRows.some((candidate) =>
          candidate.id !== id
          && (String(candidate.systemKey ?? "").trim() === "project_information_root"
            || String(candidate.systemKey ?? "").trim().startsWith("project_information:"))
        )) {
          throw new DocsNodeInvariantError(
            "Project canonical/stale identityを含むDocs subtreeは通常のPATCH移動で変更できません",
          );
        }
        const pointers = await tx
          .select({ id: projects.id })
          .from(projects)
          .where(inArray(projects.knowledgeNodeId, closureIds))
          .for("update");
        if (pointers.length > 0) {
          throw new DocsNodeInvariantError(
            "Projectが参照するDocs nodeを含むため通常のDocs PATCHでは移動できません",
          );
        }
        await tx
          .select({ id: knowledgeNodes.id })
          .from(knowledgeNodes)
          .where(inArray(knowledgeNodes.id, closureIds))
          .orderBy(asc(knowledgeNodes.id))
          .for("update");
        if (updates.parentId) {
          const [lockedParent] = await tx
            .select()
            .from(knowledgeNodes)
            .where(
              and(
                eq(knowledgeNodes.id, updates.parentId),
                eq(knowledgeNodes.docsLibraryId, access.workspace.id),
              ),
            )
            .limit(1)
            .for("update");
          if (!lockedParent || lockedParent.archivedAt) {
            throw new DocsNodeInvariantError("アーカイブ済みnodeの下には移動できません");
          }
          if (String(lockedParent.systemKey ?? "").trim() === "project_information_root") {
            throw new DocsNodeInvariantError("案件情報hub直下への通常のDocs PATCH移動はできません");
          }
          if (lockedParent.projectId !== access.node.projectId) {
            throw new DocsNodeInvariantError("親nodeと異なるProjectには移動できません");
          }
          if (
            access.node.projectId
            && lockedParent.id !== access.node.id
            && lockedParent.rootPageId !== access.node.rootPageId
          ) {
            throw new DocsNodeInvariantError("Projectの正規サブツリー外へは移動できません");
          }
          await lockAndAssertGenericDocsMutationAllowed(lockedParent, tx, user);
          // Rebase denormalized values from the locked parent so a concurrent
          // hierarchy repair cannot leave this subtree with stale root/project
          // metadata.
          updates.rootPageId = lockedParent.rootPageId ?? lockedParent.id;
          updates.projectId = lockedParent.projectId;
        }
        if (effectiveParentId && lockedSnapshotDescendantIds.includes(effectiveParentId)) {
          throw new DocsNodeInvariantError("子孫nodeを親にすると階層が破綻します");
        }
        descendantIds = lockedSnapshotDescendantIds;
      }
      if (canonicalProjectRoot && canonicalProjectResolution) {
        // Serialize generic autosave with Project.rename.  The resolver above
        // is a preflight; the locked Project row is the final title authority
        // immediately before the node write.
        if (typeof tx.select === "function" && lockedPointerRows.length === 0) {
          const [lockedProject] = await tx
            .select()
            .from(projects)
            .where(and(eq(projects.id, canonicalProjectResolution.project.id), eq(projects.knowledgeNodeId, id)))
            .limit(1)
            .for("update");
          if (!lockedProject || lockedProject.deletedAt || lockedProject.isCompleted) {
            throw new DocsNodeInvariantError("Project canonical identityが変更されたためDocs更新を中止しました");
          }
          updates.title = lockedProject.name.trim() || "案件情報";
        }
      }
      // A pure restore is lifecycle metadata, not a content edit.  Route it
      // through the lifecycle writer so archived legacy blank rows (which may
      // predate the explicit-blank discriminator) can be restored without
      // being rejected by the strict ordinary-paragraph writer.  Any mixed
      // PATCH still goes through the full writer and therefore retains the
      // normal blank/content invariants.
      const row = lifecycleOnlyRestoreRequest
        ? await updateDocsNodeLifecycle(tx, id, {
          archivedAt: null,
          updatedBy: user.id,
          updatedAt: new Date(),
        })
        : await updateDocsNode(tx, id, updates);
      if (!row) {
        throw new DocsNodeInvariantError("Docs nodeが同時に削除されたため更新できません");
      }

    if (restoreDescendantIds.length > 0) {
      const restoredRows = await updateDocsNodesByIds(tx, restoreDescendantIds, {
        archivedAt: null,
        updatedBy: user.id,
        updatedAt: new Date(),
      });
      if (restoredRows.length !== restoreDescendantIds.length) {
        throw new DocsNodeInvariantError("復元対象のDocs subtreeが同時に変更されたため復元を中止しました");
      }
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
      lockedTarget.archivedAt &&
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
  } catch (error) {
    if (typeof DocsNodeInvariantError === "function" && error instanceof DocsNodeInvariantError) {
      return NextResponse.json(
        { detail: error.message, code: error.code },
        { status: error.status },
      );
    }
    if (error instanceof ManagedDocsMutationError) {
      return NextResponse.json({ detail: error.message }, { status: error.status });
    }
    if (error instanceof ManagedDocsAccessError) {
      return NextResponse.json({ detail: error.message }, { status: error.status });
    }
    console.error("Docs node PATCH failed", { nodeId: id, error });
    return NextResponse.json(
      { detail: "Docs nodeの更新に失敗しました", code: "docs_node_update_failed" },
      { status: 500 },
    );
  }

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
      // The Docs transaction has already committed.  Do not return a plain
      // 502 that makes the save queue roll back an optimistic title which is
      // already durable; expose a stable retry sentinel instead and keep raw
      // downstream errors out of the response.
      console.error("Docs node PATCH: task title sync failed", {
        nodeId: updated.id,
        error: err,
      });
      return NextResponse.json(
        {
          node: serializeNode(updated),
          committed: true,
          task_binding_error: "task_title_sync_failed",
          detail: "Docs nodeは保存されましたが、タスクタイトル同期に失敗しました",
        },
        { status: 200 },
      );
    }
  }

  return NextResponse.json({
    node: serializeNode(updated),
    ...(restoreDescendantIds.length > 0 ? { restored_node_ids: [id, ...restoreDescendantIds] } : {}),
  });
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

  const identitySystemKey = String(access.node.systemKey ?? "").trim();
  if (identitySystemKey === "project_information_root" || identitySystemKey.startsWith("project_information:")) {
    return NextResponse.json(
      { detail: "案件情報の正本/stale nodeは専用クリーンアップ経路でのみ変更できます" },
      { status: 409 },
    );
  }

  if (request.nextUrl.searchParams.get("permanent") === "1") {
    const batchId = createDeletionBatchId();

    const outcome = await db.transaction(async (tx) => {
      // The deletion helper prelocks the effective Project identity and every
      // reverse pointer in one lexical Project→node phase before taking the
      // closure. This keeps permanent deletion in the same order as generic
      // PATCH/task binding even when a descendant carries the pointer.
      const closure = await lockDocsDeletionClosureSnapshot(tx, [id], {
        projectId: access.node.projectId ?? undefined,
      });
      const root = closure.find((row) => row.rootId === id && row.id === id);
      if (!root) return { kind: "missing" } as const;
      if (closure.some((row) => row.depth >= DOCS_DELETION_MAX_DESCENDANT_DEPTH)) {
        return { kind: "depth_cap" } as const;
      }
      if (closure.some((row) => row.docsLibraryId !== access.workspace.id)) {
        return { kind: "foreign_library" } as const;
      }
      if (typeof tx.select === "function") {
        const [lockedTarget] = await tx
          .select()
          .from(knowledgeNodes)
          .where(
            and(
              eq(knowledgeNodes.id, id),
              eq(knowledgeNodes.docsLibraryId, access.workspace.id),
            ),
          )
          .limit(1)
          .for("update");
        try {
          if (!lockedTarget) return { kind: "missing" } as const;
          await lockAndAssertGenericDocsMutationAllowed(lockedTarget, tx, user);
        } catch (error) {
          if (error instanceof ManagedDocsMutationError) {
            return { kind: "managed", detail: error.message } as const;
          }
          if (error instanceof ManagedDocsAccessError) {
            return { kind: "access", detail: error.message } as const;
          }
          throw error;
        }
      }
      if (typeof tx.select === "function") {
        // The ancestor policy above cannot see managed rows below the target.
        // A corrupt/legacy ordinary ancestor must not become a bypass for a
        // Guide, Inbox, mail, or workspace-reference descendant, so inspect
        // the already-locked closure before the destructive statement.
        const lockedClosureNodes = await tx
          .select({
            id: knowledgeNodes.id,
            docsLibraryId: knowledgeNodes.docsLibraryId,
            parentId: knowledgeNodes.parentId,
            projectId: knowledgeNodes.projectId,
            systemKey: knowledgeNodes.systemKey,
            displayProps: knowledgeNodes.displayProps,
          })
          .from(knowledgeNodes)
          .where(inArray(knowledgeNodes.id, closure.map((row) => row.id)))
          .orderBy(asc(knowledgeNodes.id))
          .for("update");
        const managedClosureNode = lockedClosureNodes.find((node) =>
          managedDocsDomain(node) !== null,
        );
        if (managedClosureNode) {
          const domain = managedDocsDomain(managedClosureNode);
          if (domain) {
            return {
              kind: "managed",
              detail: new ManagedDocsMutationError(domain).message,
            } as const;
          }
        }
      }
      if (typeof tx.select === "function") {
        const identityDescendants = await tx
          .select({ id: knowledgeNodes.id, systemKey: knowledgeNodes.systemKey })
          .from(knowledgeNodes)
          .where(inArray(knowledgeNodes.id, closure.map((row) => row.id)));
        if (identityDescendants.some((row) =>
          row.id !== id
          && (String(row.systemKey ?? "").trim() === "project_information_root"
            || String(row.systemKey ?? "").trim().startsWith("project_information:"))
        )) {
          return { kind: "identity_descendant" } as const;
        }
      }

      // The pointer is authoritative for active, completed, and soft-deleted
      // Projects alike.  A pointer on a descendant also blocks this ancestor
      // because the parent FK is ON DELETE CASCADE.
      const projectPointerIds = await getAllProjectKnowledgeNodePointerIds(
        tx,
        closure.map((row) => row.id),
      );
      if (closure.some((row) => projectPointerIds.has(row.id))) {
        return { kind: "project_pointer" } as const;
      }

      // Write the audit row before the destructive statement.  If the ledger
      // is unavailable, fail closed rather than deleting without provenance.
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
        // Lightweight route-test doubles may not expose transaction.delete;
        // production Drizzle transactions always take the atomic branch.
        await db.delete(knowledgeNodes).where(eq(knowledgeNodes.id, id));
      }
      return {
        kind: "deleted",
        deletedNodeIds: closure.map((row) => row.id),
      } as const;
    });

    if (outcome.kind === "missing") {
      return NextResponse.json({ detail: "nodeが見つかりません" }, { status: 404 });
    }
    if (outcome.kind === "depth_cap") {
      return NextResponse.json(
        { detail: "Docs subtreeが安全確認の深さ上限を超えるため完全削除できません" },
        { status: 409 },
      );
    }
    if (outcome.kind === "foreign_library") {
      return NextResponse.json(
        { detail: "別のDocs Libraryの子nodeがあるため完全削除できません" },
        { status: 409 },
      );
    }
    if (outcome.kind === "identity_descendant") {
      return NextResponse.json(
        { detail: "Project canonical/stale identityを含むDocs subtreeは通常のDELETEで変更できません" },
        { status: 409 },
      );
    }
    if (outcome.kind === "project_pointer") {
      return NextResponse.json(
        { detail: "Projectが参照するDocs nodeを含むため通常のDocs DELETEでは完全削除できません" },
        { status: 409 },
      );
    }
    if (outcome.kind === "managed") {
      return NextResponse.json({ detail: outcome.detail }, { status: 409 });
    }
    if (outcome.kind === "access") {
      return NextResponse.json({ detail: outcome.detail }, { status: 403 });
    }
    return NextResponse.json({ ok: true, deleted_node_ids: outcome.deletedNodeIds });
  }

  // 子孫も同時にアーカイブする。node だけを消すと、outline からは見えないのに
  // ページ検索や Search nodes には残り続ける孤児ノードができる。
  // 復元時に同一操作の分だけ戻せるよう、archived_at は全件同じ時刻にする。
  const archivedAt = new Date();
  const batchId = createDeletionBatchId();
  const outcome = await db.transaction(async (tx) => {
    // The deletion helper prelocks the effective Project identity and every
    // reverse pointer in one lexical Project→node phase before taking the
    // closure. The closure-scoped pointer query below still rechecks every
    // descendant under lock.
    const closure = await lockDocsDeletionClosureSnapshot(
      tx,
      [id],
      {
        docsLibraryId: access.workspace.id,
        projectId: access.node.projectId ?? undefined,
      },
    );
    const root = closure.find((row) => row.rootId === id && row.id === id);
    if (!root) return { kind: "missing" } as const;
    if (root.docsLibraryId !== access.workspace.id) {
      return { kind: "integrity_failure" } as const;
    }
    if (typeof tx.select === "function") {
      const [lockedTarget] = await tx
        .select()
        .from(knowledgeNodes)
        .where(
          and(
            eq(knowledgeNodes.id, id),
            eq(knowledgeNodes.docsLibraryId, access.workspace.id),
          ),
        )
        .limit(1)
        .for("update");
      try {
        if (!lockedTarget) return { kind: "missing" } as const;
        await lockAndAssertGenericDocsMutationAllowed(lockedTarget, tx, user);
      } catch (error) {
        if (error instanceof ManagedDocsMutationError) {
          return { kind: "managed", detail: error.message } as const;
        }
        if (error instanceof ManagedDocsAccessError) {
          return { kind: "access", detail: error.message } as const;
        }
        throw error;
      }
    }
    if (typeof tx.select === "function") {
      // The ancestor policy above cannot see managed rows below the target.
      // Inspect the already-locked closure so an ordinary ancestor cannot
      // bypass Guide/Inbox/mail/workspace-reference mutation protections.
      const lockedClosureNodes = await tx
        .select({
          id: knowledgeNodes.id,
          docsLibraryId: knowledgeNodes.docsLibraryId,
          parentId: knowledgeNodes.parentId,
          projectId: knowledgeNodes.projectId,
          systemKey: knowledgeNodes.systemKey,
          displayProps: knowledgeNodes.displayProps,
        })
        .from(knowledgeNodes)
        .where(inArray(knowledgeNodes.id, closure.map((row) => row.id)))
        .orderBy(asc(knowledgeNodes.id))
        .for("update");
      const managedClosureNode = lockedClosureNodes.find((node) =>
        managedDocsDomain(node) !== null,
      );
      if (managedClosureNode) {
        const domain = managedDocsDomain(managedClosureNode);
        if (domain) {
          return {
            kind: "managed",
            detail: new ManagedDocsMutationError(domain).message,
          } as const;
        }
      }
    }
    if (typeof tx.select === "function") {
      const identityDescendants = await tx
        .select({ id: knowledgeNodes.id, systemKey: knowledgeNodes.systemKey })
        .from(knowledgeNodes)
        .where(inArray(knowledgeNodes.id, closure.map((row) => row.id)));
      if (identityDescendants.some((row) =>
        row.id !== id
        && (String(row.systemKey ?? "").trim() === "project_information_root"
          || String(row.systemKey ?? "").trim().startsWith("project_information:"))
      )) {
        return { kind: "identity_descendant" } as const;
      }
    }
    if (closure.some((row) => row.depth >= DOCS_DELETION_MAX_DESCENDANT_DEPTH)) {
      return { kind: "depth_cap" } as const;
    }

    // Keep every persisted Project pointer protected. The deletion helper
    // prelocked the rows before the node closure; this closure-scoped query
    // additionally catches retained/malformed pointers on descendants.
    const projectPointerIds = await getAllProjectKnowledgeNodePointerIds(
      tx,
      closure.map((row) => row.id),
    );
    if (closure.some((row) => projectPointerIds.has(row.id))) {
      return { kind: "project_pointer" } as const;
    }

    const descendantIds = Array.from(
      new Set(closure.filter((row) => row.id !== id).map((row) => row.id)),
    );
    const row = await updateDocsNodeLifecycle(tx, id, {
      archivedAt,
      updatedBy: user.id,
      updatedAt: archivedAt,
    });
    if (!row) {
      throw new DocsNodeInvariantError("Docs nodeが同時に削除されたためアーカイブできません");
    }
    if (descendantIds.length > 0) {
      const archivedRows = await updateDocsNodesByIds(tx, descendantIds, {
        archivedAt,
        updatedBy: user.id,
        updatedAt: archivedAt,
      });
      if (archivedRows.length !== descendantIds.length) {
        throw new DocsNodeInvariantError("アーカイブ対象のDocs subtreeが同時に変更されたため中止しました");
      }
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

    return {
      kind: "archived",
      row,
      archivedNodeIds: [id, ...descendantIds],
    } as const;
  });

  if (outcome.kind === "missing") {
    return NextResponse.json({ detail: "nodeが見つかりません" }, { status: 404 });
  }
  if (outcome.kind === "integrity_failure") {
    return NextResponse.json(
      { detail: "Docs subtree integrityを確認できないためアーカイブを中止しました" },
      { status: 409 },
    );
  }
  if (outcome.kind === "depth_cap") {
    return NextResponse.json(
      { detail: "Docs subtreeが安全確認の深さ上限を超えるためアーカイブできません" },
      { status: 409 },
    );
  }
  if (outcome.kind === "project_pointer") {
    return NextResponse.json(
      { detail: "Projectが参照するDocs nodeを含むため通常のDocs DELETEではアーカイブできません" },
      { status: 409 },
    );
  }
  if (outcome.kind === "identity_descendant") {
    return NextResponse.json(
      { detail: "Project canonical/stale identityを含むDocs subtreeは通常のDELETEで変更できません" },
      { status: 409 },
    );
  }

  if (outcome.kind === "managed") {
    return NextResponse.json({ detail: outcome.detail }, { status: 409 });
  }
  if (outcome.kind === "access") {
    return NextResponse.json({ detail: outcome.detail }, { status: 403 });
  }

  const updated = outcome.row;

  let taskBindingError: string | null = null;
  for (const archivedNodeId of outcome.archivedNodeIds) {
    try {
      await unlinkDocsTaskBinding({ user, nodeId: archivedNodeId });
    } catch (error) {
      // archiveは既にcommit済み。未適用を装う502を返すとclientが表示だけ
      // rollbackしてDBと不一致になるため、確定状態と部分失敗を同時に返す。
      console.error("Docs node archive: task binding unlink failed", archivedNodeId, error);
      taskBindingError = TASK_BINDING_UNLINK_FAILED;
    }
  }

  return NextResponse.json({
    node: serializeNode(updated),
    committed: true,
    archived_node_ids: outcome.archivedNodeIds,
    task_binding_error: taskBindingError,
  });
}
