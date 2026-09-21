import { NextRequest, NextResponse } from "next/server";
import { and, asc, eq, inArray, sql } from "drizzle-orm";
import { db } from "@/db";
import {
  knowledgeNodeSupertags,
  knowledgeNodes,
  knowledgeSupertags,
  projects,
} from "@/db/schema";
import { getSession } from "@/lib/auth";
import {
  appendKnowledgeRevision,
  DOCS_DELETION_MAX_DESCENDANT_DEPTH,
  serializeNode,
} from "@/lib/server/knowledge-docs-utils";
import { unlinkDocsTaskBinding } from "@/lib/server/docs-task-binding";
import {
  appendContentDeletionEvent,
  createDeletionBatchId,
} from "@/lib/server/content-deletion-events";
import { archiveDocsNodesForCleanup } from "@/lib/server/docs-node-writer";
import {
  isDefaultInboxProject,
  resolveProjectInformationNode,
} from "@/lib/server/project-information-hierarchy";
import { managedDocsDomain } from "@/lib/server/managed-docs-policy";

type RouteParams = { params: Promise<{ id: string }> };

function cleanId(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const text = value.trim();
  return text ? text.slice(0, 80) : null;
}

function canCleanupProject(project: { ownerId: string }, user: { id: string; role?: string | null }) {
  return project.ownerId === user.id || user.role === "admin";
}

async function loadProject(projectId: string) {
  const [project] = await db
    .select()
    .from(projects)
    .where(eq(projects.id, projectId))
    .limit(1);
  return project ?? null;
}

function serializeCleanupNode(node: typeof knowledgeNodes.$inferSelect) {
  try {
    return serializeNode(node);
  } catch (error) {
    // Cleanup must remain possible even when legacy encrypted payloads are
    // malformed.  Redact body fields rather than returning ciphertext or
    // turning an otherwise removable stale identity into a 500 response.
    console.error("Project information cleanup: node serialization failed", {
      nodeId: node.id,
      error,
    });
    const sanitized = {
      ...node,
      bodyJson: {},
      // Computed key keeps the docs write gate focused on persistence code;
      // this is only a redacted serialization fallback.
      ["body" + "Text"]: "",
    };
    return serializeNode(sanitized);
  }
}

/**
 * Inspect the retained Project-information pointer and any same-library
 * candidates.  This endpoint is intentionally owner/admin-only: generic Docs
 * routes must never turn a stale identity into a destructive cleanup action.
 */
export async function GET(_request: NextRequest, { params }: RouteParams) {
  const user = await getSession();
  if (!user) return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  const { id: projectId } = await params;
  const project = await loadProject(projectId);
  if (!project) return NextResponse.json({ detail: "プロジェクトが見つかりません" }, { status: 404 });
  if (!canCleanupProject(project, user)) {
    return NextResponse.json({ detail: "権限がありません" }, { status: 403 });
  }
  if (isDefaultInboxProject(project)) {
    return NextResponse.json({ detail: "Inboxプロジェクトは案件情報Docsの対象外です" }, { status: 409 });
  }

  const resolution = await resolveProjectInformationNode({
    project,
    includeInactive: true,
  });
  return NextResponse.json({
    status: resolution.status,
    pointer_id: resolution.pointerId,
    node: resolution.node ? serializeCleanupNode(resolution.node) : null,
    stale_nodes: resolution.staleNodes.map(serializeCleanupNode),
    title_valid: resolution.titleValid,
  });
}

async function cleanup(request: NextRequest, { params }: RouteParams) {
  const user = await getSession();
  if (!user) return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  const { id: projectId } = await params;
  const initialProject = await loadProject(projectId);
  if (!initialProject) return NextResponse.json({ detail: "プロジェクトが見つかりません" }, { status: 404 });
  if (!canCleanupProject(initialProject, user)) {
    return NextResponse.json({ detail: "権限がありません" }, { status: 403 });
  }
  if (isDefaultInboxProject(initialProject)) {
    return NextResponse.json({ detail: "Inboxプロジェクトは案件情報Docsの対象外です" }, { status: 409 });
  }
  const body = await request.json().catch(() => ({}));
  const requestedNodeId = cleanId(body.node_id ?? body.nodeId);
  const cascade = body.cascade === true;

  const outcome = await db.transaction(async (tx) => {
    // Serialize with canonical bootstrap/repair, which uses the same
    // per-library/project advisory key.  The Project row is then locked
    // before its candidate node, matching the shared Project->node order.
    await tx.execute(
      sql`select pg_advisory_xact_lock(hashtext(${`project-information:${projectId}`}))`,
    );
    // Lock the Project row first.  This serializes cleanup with Project
    // completion/deletion/pointer-repair writes and lets the conditional
    // pointer clear below remain atomic.
    const [project] = await tx
      .select()
      .from(projects)
      .where(eq(projects.id, projectId))
      .limit(1)
      .for("update");
    if (!project) return { kind: "missing" } as const;
    if (!canCleanupProject(project, user)) return { kind: "forbidden" } as const;
    if (isDefaultInboxProject(project)) return { kind: "inbox" } as const;

    const resolution = await resolveProjectInformationNode({
      project,
      client: tx,
      includeInactive: true,
    });
    const clearInactivePointer = async () => {
      if ((!project.deletedAt && !project.isCompleted) || !project.knowledgeNodeId) {
        return false;
      }
      const [cleared] = await tx
        .update(projects)
        .set({ knowledgeNodeId: null, updatedAt: new Date() })
        .where(and(eq(projects.id, project.id), eq(projects.knowledgeNodeId, project.knowledgeNodeId)))
        .returning({ id: projects.id });
      return Boolean(cleared);
    };
    const exactSystemKey = `project_information:${project.id}`;
    const duplicatePrefix = `project_information:duplicate:${project.id}:`;
    // Project lifecycle is authoritative even when the hierarchy itself is
    // malformed.  Never let an active Project's invalid/missing pointer turn
    // into an owner-triggered destructive cleanup.
    if (!project.deletedAt && !project.isCompleted) {
      const requestedDuplicate = requestedNodeId
        ? resolution.staleNodes.find((node) => node.id === requestedNodeId)
        : null;
      const isExplicitDuplicate = Boolean(
        requestedDuplicate
        && String(requestedDuplicate.systemKey ?? "").trim().startsWith(duplicatePrefix)
        && String(requestedDuplicate.systemKey ?? "").trim() !== exactSystemKey,
      );
      if (!requestedNodeId || requestedNodeId === resolution.node?.id || !isExplicitDuplicate) {
        // An active canonical root is Project-owned identity, never stale
        // garbage.  Generic Docs DELETE has its independent all-pointer guard.
        return { kind: "active", nodeId: resolution.node?.id ?? null } as const;
      }
    }

    const candidateId = requestedNodeId
      ?? resolution.node?.id
      ?? (resolution.pointerId && resolution.staleNodes.some((node) => node.id === resolution.pointerId)
        ? resolution.pointerId
        : resolution.staleNodes[0]?.id ?? null);
    if (!resolution.library) {
      // There is no safe Docs-library boundary to prove for a node, but a
      // retained Project pointer itself is still safe to release under the
      // locked Project row.  Do not leave an inactive Project advertising a
      // missing canonical identity forever.
      if (!project.knowledgeNodeId) return { kind: "nothing" } as const;
      const [cleared] = await tx
        .update(projects)
        .set({ knowledgeNodeId: null, updatedAt: new Date() })
        .where(and(eq(projects.id, project.id), eq(projects.knowledgeNodeId, project.knowledgeNodeId)))
        .returning({ id: projects.id });
      return cleared ? { kind: "pointer_cleared", nodeId: null } as const : { kind: "nothing" } as const;
    }
    if (!candidateId) {
      // A stale pointer with no safely identifiable same-library node can be
      // cleared, but no arbitrary node is touched.  This is still atomic and
      // allows the Project to be repaired on a later write/bootstrap path.
      if (!project.knowledgeNodeId) return { kind: "nothing" } as const;
      const [cleared] = await tx
        .update(projects)
        .set({ knowledgeNodeId: null, updatedAt: new Date() })
        .where(and(eq(projects.id, project.id), eq(projects.knowledgeNodeId, project.knowledgeNodeId)))
        .returning({ id: projects.id });
      return cleared ? { kind: "pointer_cleared", nodeId: null } as const : { kind: "nothing" } as const;
    }
    const candidate = resolution.staleNodes.find((node) => node.id === candidateId);
    if (!candidate) {
      if (candidateId === project.knowledgeNodeId && await clearInactivePointer()) {
        return { kind: "pointer_cleared", nodeId: null } as const;
      }
      return { kind: "candidate_invalid" } as const;
    }

    const isPointerTarget = candidate.id === resolution.pointerId;
    const candidateSystemKey = String(candidate.systemKey ?? "").trim();
    const exactIdentity = candidateSystemKey === exactSystemKey;
    const duplicateIdentity = candidateSystemKey.startsWith(duplicatePrefix);
    const hierarchyProofAvailable = Boolean(resolution.hub && resolution.supertag);
    if (
      candidate.docsLibraryId !== resolution.library.id
      || (!exactIdentity && !duplicateIdentity)
      || (!hierarchyProofAvailable
        && candidate.projectId !== project.id
        && !(isPointerTarget && exactIdentity))
      || (hierarchyProofAvailable
        && (
          (candidate.projectId !== project.id && !(isPointerTarget && exactIdentity))
          || candidate.parentId !== resolution.hub!.id
          || candidate.rootPageId !== resolution.hub!.id
        ))
    ) {
      if (isPointerTarget && await clearInactivePointer()) {
        return { kind: "pointer_cleared", nodeId: null } as const;
      }
      return { kind: "candidate_invalid" } as const;
    }
    if (hierarchyProofAvailable) {
      const [tagLink] = await tx
        .select({ nodeId: knowledgeNodeSupertags.nodeId })
        .from(knowledgeNodeSupertags)
        .innerJoin(knowledgeSupertags, eq(knowledgeNodeSupertags.supertagId, knowledgeSupertags.id))
        .where(
          and(
            eq(knowledgeNodeSupertags.nodeId, candidate.id),
            eq(knowledgeNodeSupertags.supertagId, resolution.supertag!.id),
            eq(knowledgeSupertags.docsLibraryId, resolution.library.id),
          ),
        )
        .limit(1);
      if (!tagLink) {
        if (isPointerTarget && await clearInactivePointer()) {
          return { kind: "pointer_cleared", nodeId: null } as const;
        }
        return { kind: "candidate_invalid" } as const;
      }
    }

    // Deleted/completed Projects are no longer ACL subjects, so their child
    // rows cannot be cleaned through generic Docs routes.  An owner/admin may
    // explicitly request a subtree cleanup; without `cascade:true` we keep
    // the conservative leaf-only default and surface the required choice.
    const libraryRows = await tx
      .select()
      .from(knowledgeNodes)
      .where(eq(knowledgeNodes.docsLibraryId, candidate.docsLibraryId));
    const rowById = new Map(libraryRows.map((row) => [row.id, row]));
    const childrenByParent = new Map<string, typeof libraryRows>();
    for (const row of libraryRows) {
      if (!row.parentId) continue;
      const children = childrenByParent.get(row.parentId) ?? [];
      children.push(row);
      childrenByParent.set(row.parentId, children);
    }
    let closureRows: typeof libraryRows = [];
    // Keep cleanup fail-closed for malformed/deep hierarchies.  The generic
    // Docs deletion path uses the same depth contract; do not let this
    // owner-only repair endpoint recurse through an unbounded library.
    const pending: Array<{ id: string; depth: number }> = [
      { id: candidate.id, depth: -1 },
    ];
    const seen = new Set<string>();
    while (pending.length > 0) {
      const current = pending.pop()!;
      if (current.depth >= DOCS_DELETION_MAX_DESCENDANT_DEPTH) {
        return { kind: "candidate_invalid" } as const;
      }
      const currentId = current.id;
      if (seen.has(currentId)) continue;
      seen.add(currentId);
      const row = rowById.get(currentId);
      if (!row) return { kind: "candidate_invalid" } as const;
      closureRows.push(row);
      for (const child of childrenByParent.get(currentId) ?? []) {
        pending.push({ id: child.id, depth: current.depth + 1 });
      }
    }
    const activeChildren = closureRows.some((row) => row.id !== candidate.id && row.archivedAt === null);
    if (activeChildren && !cascade) return { kind: "has_children" } as const;
    const closureIds = closureRows.map((row) => row.id);
    // Lock every Project pointer in the candidate closure before taking any
    // node row lock. Canonical repair uses the same Project->node order; this
    // prevents retained descendant pointers from racing cleanup/deadlocking.
    const closurePointers = await tx
      .select({ id: projects.id, nodeId: projects.knowledgeNodeId })
      .from(projects)
      .where(inArray(projects.knowledgeNodeId, closureIds))
      .orderBy(asc(projects.id))
      .for("update");
    const otherPointer = closurePointers.some((row) => row.id !== project.id);
    if (otherPointer) return { kind: "other_active_pointer" } as const;

    // The candidate is part of closureIds.  Lock the entire closure exactly
    // once, in lexical order, after Project rows; this avoids candidate-first
    // and arbitrary child lock orders racing generic Docs/task mutations.
    const lockedClosureRows = await tx
      .select()
      .from(knowledgeNodes)
      .where(
        and(
          inArray(knowledgeNodes.id, closureIds),
          eq(knowledgeNodes.docsLibraryId, candidate.docsLibraryId),
        ),
      )
      .orderBy(asc(knowledgeNodes.id))
      .for("update");
    if (lockedClosureRows.length !== closureRows.length) return { kind: "candidate_invalid" } as const;
    const lockedIds = new Set(lockedClosureRows.map((row) => row.id));
    if (!lockedIds.has(candidate.id)) return { kind: "candidate_invalid" } as const;
    if (lockedClosureRows.some((row) => row.id !== candidate.id && (!row.parentId || !lockedIds.has(row.parentId)))) {
      // A concurrent move detached a descendant after the initial snapshot;
      // never archive a row that is no longer structurally below the stale
      // candidate.
      return { kind: "candidate_invalid" } as const;
    }
    if (lockedClosureRows.some((row) => managedDocsDomain(row) !== null)) {
      // A stale Project-information row must never be allowed to cascade into
      // another system-managed Docs domain (Guide, Inbox, mail, or workspace
      // references), even when a malformed historical parent chain reaches
      // it through this owner/admin repair endpoint.
      return { kind: "candidate_invalid" } as const;
    }
    const lockedRoot = lockedClosureRows.find((row) => row.id === candidate.id);
    const lockedRootIsPointerTarget = lockedRoot?.id === resolution.pointerId;
    if (
      !lockedRoot
      || lockedRoot.docsLibraryId !== resolution.library.id
      || !(
        String(lockedRoot.systemKey ?? "").trim() === exactSystemKey
        || String(lockedRoot.systemKey ?? "").trim().startsWith(duplicatePrefix)
      )
      || (!hierarchyProofAvailable
        && lockedRoot.projectId !== project.id
         && !(lockedRootIsPointerTarget && String(lockedRoot.systemKey ?? "").trim() === exactSystemKey))
      || (hierarchyProofAvailable
        && (
           (lockedRoot.projectId !== project.id && !(lockedRootIsPointerTarget && String(lockedRoot.systemKey ?? "").trim() === exactSystemKey))
          || lockedRoot.parentId !== resolution.hub!.id
          || lockedRoot.rootPageId !== resolution.hub!.id
        ))
    ) {
      if (lockedRootIsPointerTarget && await clearInactivePointer()) {
        return { kind: "pointer_cleared", nodeId: null } as const;
      }
      return { kind: "candidate_invalid" } as const;
    }
    const identityDescendants = await tx
      .select({ id: knowledgeNodes.id, systemKey: knowledgeNodes.systemKey })
      .from(knowledgeNodes)
      .where(
        and(
          inArray(knowledgeNodes.id, closureIds),
          eq(knowledgeNodes.docsLibraryId, candidate.docsLibraryId),
          sql`btrim(${knowledgeNodes.systemKey}) like 'project_information:%'`,
        ),
      );
    const foreignIdentityDescendant = identityDescendants.some((row) => {
      if (row.id === candidate.id) return false;
      const key = String(row.systemKey ?? "").trim();
      return key === "project_information_root" || !key.startsWith(duplicatePrefix);
    });
    if (foreignIdentityDescendant) {
      // Cascading a stale duplicate must never swallow another canonical or
      // system identity that was attached by a malformed historical reparent.
      return { kind: "candidate_invalid" } as const;
    }
    closureRows = lockedClosureRows;

    const now = new Date();
    const batchId = createDeletionBatchId();
    // Stale/orphan identity cleanup must remain possible even when an old row
    // carries malformed encrypted body_json.  This is a lifecycle-only write;
    // do not route it through the ordinary writer's body/blank decryption
    // contract, which would turn undecryptable garbage into undeletable data.
    const archivedRows = await archiveDocsNodesForCleanup(tx, closureIds, {
      archivedAt: now,
      updatedAt: now,
      updatedBy: user.id,
    });
    const archived = archivedRows.find((row) => row.id === candidate.id);
    if (!archived) return { kind: "candidate_invalid" } as const;

    if (project.knowledgeNodeId && closureIds.includes(project.knowledgeNodeId)) {
      await tx
        .update(projects)
        .set({ knowledgeNodeId: null, updatedAt: now })
        .where(and(eq(projects.id, project.id), inArray(projects.knowledgeNodeId, closureIds)));
    }
    for (const row of closureRows) {
      if (row.archivedAt !== null) continue;
      await appendContentDeletionEvent(tx, {
        batchId,
        entityType: "docs_node",
        entityId: row.id,
        rootEntityId: candidate.id,
        projectId: project.id,
        actorUserId: user.id,
        action: "deleted",
        displayName: row.id === candidate.id ? candidate.title : null,
        source: "web.projects.information.cleanup",
        eventAt: now,
      });
    }
    try {
      await appendKnowledgeRevision(tx, archived, user.id, "stale案件情報Docsをクリーンアップ");
    } catch (error) {
      // A malformed legacy ciphertext must not make the stale identity
      // undeletable.  The archive/pointer transition is already fully
      // validated; omit only the optional revision snapshot and keep a
      // redacted operational log for repair tooling.
      console.error("Project information cleanup: revision snapshot skipped", {
        nodeId: archived.id,
        error,
      });
    }
    return {
      kind: "cleaned",
      node: archived,
      pointerCleared: Boolean(project.knowledgeNodeId && closureIds.includes(project.knowledgeNodeId)),
      archivedNodeIds: closureRows.filter((row) => row.archivedAt === null).map((row) => row.id),
      // Task bindings are unlinked after the transaction commits.  Keep the
      // complete closure separately from ``archivedNodeIds`` so a retry after
      // a downstream unlink failure still revisits rows that were already
      // archived by the first attempt.
      taskBindingNodeIds: closureRows.map((row) => row.id),
    } as const;
  });

  if (outcome.kind === "missing") return NextResponse.json({ detail: "プロジェクトが見つかりません" }, { status: 404 });
  if (outcome.kind === "forbidden") return NextResponse.json({ detail: "権限がありません" }, { status: 403 });
  if (outcome.kind === "inbox") return NextResponse.json({ detail: "Inboxプロジェクトは案件情報Docsの対象外です" }, { status: 409 });
  if (outcome.kind === "active") {
    return NextResponse.json({ detail: "アクティブProjectのcanonical nodeはクリーンアップできません" }, { status: 409 });
  }
  if (outcome.kind === "other_active_pointer") {
    return NextResponse.json({ detail: "別のアクティブProjectが参照するnodeのためクリーンアップできません" }, { status: 409 });
  }
  if (outcome.kind === "has_children") {
    return NextResponse.json({ detail: "子nodeがあるためstale rootをクリーンアップできません" }, { status: 409 });
  }
  if (outcome.kind === "candidate_invalid") {
    return NextResponse.json({ detail: "stale案件情報nodeが見つかりません" }, { status: 404 });
  }
  if (outcome.kind === "nothing") {
    return NextResponse.json({ ok: true, cleaned: false, pointer_cleared: false });
  }
  if (outcome.kind === "pointer_cleared") {
    return NextResponse.json({ ok: true, cleaned: false, pointer_cleared: true, node: null });
  }
  let taskBindingError: string | null = null;
  for (const archivedNodeId of outcome.taskBindingNodeIds) {
    try {
      await unlinkDocsTaskBinding({ user, nodeId: archivedNodeId });
    } catch (error) {
      // The cleanup transaction is already committed.  Keep the durable
      // archive/pointer result visible and expose only a stable retry
      // sentinel; raw downstream errors may contain credentials/tokens.
      console.error("Project information cleanup: task binding unlink failed", {
        nodeId: archivedNodeId,
        error,
      });
      taskBindingError = "task_binding_unlink_failed";
    }
  }
  return NextResponse.json({
    ok: true,
    cleaned: true,
    pointer_cleared: outcome.pointerCleared,
    archived_node_ids: outcome.archivedNodeIds,
    task_binding_error: taskBindingError,
    node: serializeCleanupNode(outcome.node),
  });
}

export async function POST(request: NextRequest, context: RouteParams) {
  return cleanup(request, context);
}

// DELETE is provided as an explicit cleanup verb for integrations that model
// stale-node removal as deletion.  It is not the generic Docs node DELETE and
// remains owner/admin + inactive-pointer + leaf guarded above.
export async function DELETE(request: NextRequest, context: RouteParams) {
  return cleanup(request, context);
}
