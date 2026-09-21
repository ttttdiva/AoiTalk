import { and, eq } from "drizzle-orm";
import { db } from "@/db";
import {
  docsLibraries,
  knowledgeNodeShares,
  knowledgeNodes,
  projectMembers,
  projects,
  users,
} from "@/db/schema";
import { requireDocsNode } from "@/lib/server/knowledge-docs-utils";
import { hasProjectPermission } from "@/lib/server/project-permissions";
import {
  assertGenericDocsMutationAllowed,
  lockAndAssertGenericDocsMutationAllowed,
  ManagedDocsMutationError,
} from "@/lib/server/managed-docs-policy";
import type { SessionUser } from "@/lib/server/task-route-utils";

type TaskDocsTransaction = Parameters<Parameters<typeof db.transaction>[0]>[0];
const TASK_DOCS_MAX_ANCESTOR_DEPTH = 512;

export class TaskDocsNodeInvariantError extends Error {
  readonly status = 409;

  constructor(message: string) {
    super(message);
    this.name = "TaskDocsNodeInvariantError";
  }
}

async function assertTaskDocsNodeNotAoiTalkGuide(
  node: typeof knowledgeNodes.$inferSelect,
  client: Pick<typeof db, "select">,
) {
  try {
    // Walk the full ancestor chain.  A legacy descendant may not carry the
    // managed display metadata or a namespaced system key itself, but it is
    // still part of the repository-owned Guide subtree and must not become a
    // task title/meeting-note synchronization target.
    await assertGenericDocsMutationAllowed(node, client);
  } catch (error) {
    if (
      error instanceof ManagedDocsMutationError
      && error.domain === "aoitalk_guide"
    ) {
      throw new TaskDocsNodeInvariantError(
        "AoiTalk ガイドはタスク連携先にできません",
      );
    }
    if (
      error instanceof ManagedDocsMutationError
      && error.domain === "unresolved_docs_ancestor"
    ) {
      throw new TaskDocsNodeInvariantError(
        "Docs nodeの親階層を検証できないためタスク連携できません",
      );
    }
    if (error instanceof ManagedDocsMutationError) return;
    throw error;
  }
}

async function assertTaskDocsNodeNotAoiTalkGuideInTransaction(
  node: typeof knowledgeNodes.$inferSelect,
  tx: TaskDocsTransaction,
) {
  try {
    await lockAndAssertGenericDocsMutationAllowed(node, tx);
  } catch (error) {
    if (
      error instanceof ManagedDocsMutationError
      && error.domain === "aoitalk_guide"
    ) {
      throw new TaskDocsNodeInvariantError(
        "AoiTalk ガイドはタスク連携先にできません",
      );
    }
    if (
      error instanceof ManagedDocsMutationError
      && error.domain === "unresolved_docs_ancestor"
    ) {
      throw new TaskDocsNodeInvariantError(
        "Docs nodeの親階層を検証できないためタスク連携できません",
      );
    }
    if (error instanceof ManagedDocsMutationError) return;
    throw error;
  }
}

/**
 * Recheck task-project ACLs while the mutation transaction owns the relevant
 * Project/member rows.  Request-level `canWriteProjectId` checks are only an
 * early rejection; a membership revoke may commit between that check and the
 * Task INSERT/UPDATE unless this row-locking check runs immediately before
 * the write.
 */
export class TaskProjectAccessInvariantError extends Error {
  readonly status: number;

  constructor(status = 403, message = "権限がありません") {
    super(message);
    this.name = "TaskProjectAccessInvariantError";
    this.status = status;
  }
}

export async function assertTaskProjectAccessInTransaction(
  tx: TaskDocsTransaction,
  projectIds: Iterable<string | null | undefined>,
  user: SessionUser,
  options: {
    requireRead?: boolean;
    statusByProjectId?: Readonly<Record<string, number>>;
  } = {},
): Promise<void> {
  const orderedProjectIds = [
    ...new Set(
      [...projectIds]
        .filter((projectId): projectId is string => Boolean(projectId))
        .map(String),
    ),
  ].sort();
  if (orderedProjectIds.length === 0) {
    throw new TaskProjectAccessInvariantError(403);
  }

  // Lock every Project in lexical order before the principal/member rows. All
  // project-management routes use this Project→principal→member ordering, so
  // ACL revocations serialize with Task writes rather than racing them.
  const lockedProjects: Array<{
    id: string;
    ownerId: string;
    deletedAt: Date | string | null;
  }> = [];
  for (const projectId of orderedProjectIds) {
    const [project] = await tx
      .select({
        id: projects.id,
        ownerId: projects.ownerId,
        deletedAt: projects.deletedAt,
      })
      .from(projects)
      .where(eq(projects.id, projectId))
      .limit(1)
      .for("update");
    if (!project || project.deletedAt) {
      throw new TaskProjectAccessInvariantError(
        options.statusByProjectId?.[projectId] ?? 403,
        "権限がありません",
      );
    }
    lockedProjects.push(project);
  }

  const [actor] = await tx
    .select({ role: users.role })
    .from(users)
    .where(eq(users.id, user.id))
    .limit(1)
    .for("update");

  const memberships = new Map<string, unknown>();
  for (const project of lockedProjects) {
    const [membership] = await tx
      .select({ permissions: projectMembers.permissions })
      .from(projectMembers)
      .where(
        and(
          eq(projectMembers.projectId, project.id),
          eq(projectMembers.userId, user.id),
        ),
      )
      .limit(1)
      .for("update");
    memberships.set(project.id, membership?.permissions);
  }

  for (const project of lockedProjects) {
    const elevated = actor?.role === "admin" || project.ownerId === user.id;
    const canRead = elevated || hasProjectPermission(memberships.get(project.id), "read");
    const canWrite = elevated || hasProjectPermission(memberships.get(project.id), "write");
    if (!canWrite || (options.requireRead && !canRead)) {
      throw new TaskProjectAccessInvariantError(
        options.statusByProjectId?.[project.id] ?? 403,
        "権限がありません",
      );
    }
  }
}

/**
 * Validate a task↔Docs link at the mutation boundary.
 *
 * The task API accepts an opaque node id, so a project_id match alone is not
 * sufficient: an ordinary foreign Personal node must also be writable by the
 * actor.  Identity-bearing Project nodes are never task destinations.
 */
export async function assertTaskDocsNodeLinkAllowed(
  nodeId: string,
  taskProjectId: string,
  user: SessionUser,
  client: typeof db = db,
) {
  // requireDocsNode is the shared ACL authority.  It also verifies the
  // node/library join, preventing a foreign Personal node from being mutated
  // by a task title-sync call.
  const access = await requireDocsNode(nodeId, user, "write");
  if (!access) throw new TaskDocsNodeInvariantError("Docs nodeが見つからないか書き込み権限がありません");
  await assertTaskDocsNodeNotAoiTalkGuide(access.node, client);
  const [node] = await client
    .select()
    .from(knowledgeNodes)
    .where(eq(knowledgeNodes.id, nodeId))
    .limit(1);
  if (!node) throw new TaskDocsNodeInvariantError("Docs nodeが見つかりません");
  if (node.archivedAt) {
    throw new TaskDocsNodeInvariantError("アーカイブ済みDocs nodeはタスクへ連携できません");
  }
  const systemKey = String(node.systemKey ?? "").trim();
  const pointerRows = await client
    .select({ id: projects.id })
    .from(projects)
    .where(eq(projects.knowledgeNodeId, node.id))
    .limit(2);
  if (
    systemKey === "project_information_root"
    || systemKey.startsWith("project_information:")
    || pointerRows.length > 0
  ) {
    throw new TaskDocsNodeInvariantError(
      "Project canonical Docs nodeはタスク連携先にできません",
    );
  }
  if (node.projectId && String(node.projectId) !== String(taskProjectId)) {
    throw new TaskDocsNodeInvariantError("別ProjectのDocs nodeはタスクへ連携できません");
  }
  return { node, workspace: access.workspace };
}

/**
 * Recheck the actor's Docs write ACL inside the Task transaction.
 *
 * The preflight `requireDocsNode` call is intentionally retained for the
 * normal response path, but a share/member grant can be revoked after that
 * call and before the Task row is written.  Locking the library, relevant
 * Project/member row, or every ancestor/share row gives the transaction a
 * linearization point: a concurrent revoke either commits first (and is
 * observed here) or waits until this binding has committed.
 */
async function assertTaskDocsWriteAccessInTransaction(
  tx: TaskDocsTransaction,
  node: typeof knowledgeNodes.$inferSelect,
  user: SessionUser,
) : Promise<typeof knowledgeNodes.$inferSelect> {
  // Generic Docs writes acquire the complete node closure in lexical order
  // before locking the library.  Task↔Docs binding must use the same order:
  // walking/locking target→parent here would deadlock against a concurrent
  // generic move that already owns the parent and is waiting for the target.
  // Gather the structural snapshot without locks, then acquire every row in
  // deterministic order and fail closed if any parent changed while doing so.
  const expectedParentById = new Map<string, string | null>();
  const ancestorIds: string[] = [];
  const seen = new Set<string>();
  let current: typeof knowledgeNodes.$inferSelect | {
    id: string;
    docsLibraryId: string;
    parentId: string | null;
  } | undefined = node;
  for (let depth = 0; current; depth += 1) {
    if (depth >= TASK_DOCS_MAX_ANCESTOR_DEPTH || seen.has(current.id)) {
      throw new TaskDocsNodeInvariantError(
        "Docs nodeの親階層を検証できないためタスク連携できません",
      );
    }
    if (String(current.docsLibraryId) !== String(node.docsLibraryId)) {
      throw new TaskDocsNodeInvariantError(
        "Docs nodeの親階層を検証できないためタスク連携できません",
      );
    }
    seen.add(current.id);
    ancestorIds.push(current.id);
    expectedParentById.set(current.id, current.parentId ?? null);
    if (!current.parentId) break;
    const [parent] = await tx
      .select({
        id: knowledgeNodes.id,
        docsLibraryId: knowledgeNodes.docsLibraryId,
        parentId: knowledgeNodes.parentId,
      })
      .from(knowledgeNodes)
      .where(eq(knowledgeNodes.id, current.parentId))
      .limit(1);
    if (!parent) {
      throw new TaskDocsNodeInvariantError(
        "Docs nodeの親階層を検証できないためタスク連携できません",
      );
    }
    current = parent;
  }

  const orderedAncestorIds = [...ancestorIds].sort();
  const lockedById = new Map<string, typeof knowledgeNodes.$inferSelect>();
  for (const ancestorId of orderedAncestorIds) {
    const [locked] = await tx
      .select()
      .from(knowledgeNodes)
      .where(eq(knowledgeNodes.id, ancestorId))
      .limit(1)
      .for("update");
    if (
      !locked
      || String(locked.docsLibraryId) !== String(node.docsLibraryId)
      || (locked.parentId ?? null) !== (expectedParentById.get(ancestorId) ?? null)
    ) {
      throw new TaskDocsNodeInvariantError(
        "Docs nodeの親階層が同時に変更されたためタスク連携を中止しました",
      );
    }
    lockedById.set(ancestorId, locked);
  }
  const lockedNode = lockedById.get(node.id);
  if (!lockedNode) {
    throw new TaskDocsNodeInvariantError(
      "Docs nodeが見つからないか書き込み権限がありません",
    );
  }

  // Match generic Docs writes: node closure first, then the mutable library
  // and its ACL rows.  This avoids the library↔node inversion during moves.
  const [workspace] = await tx
    .select()
    .from(docsLibraries)
    .where(eq(docsLibraries.id, lockedNode.docsLibraryId))
    .limit(1)
    .for("update");
  if (!workspace) {
    throw new TaskDocsNodeInvariantError(
      "Docs nodeが見つからないか書き込み権限がありません",
    );
  }

  if (lockedNode.projectId) {
    const [actor] = await tx
      .select({ role: users.role })
      .from(users)
      .where(eq(users.id, user.id))
      .limit(1)
      .for("update");
    const [project] = await tx
      .select({
        id: projects.id,
        ownerId: projects.ownerId,
        deletedAt: projects.deletedAt,
      })
      .from(projects)
      .where(eq(projects.id, lockedNode.projectId))
      .limit(1)
      .for("update");
    if (!project || project.deletedAt) {
      throw new TaskDocsNodeInvariantError(
        "Docs nodeが見つからないか書き込み権限がありません",
      );
    }
    let canWrite = actor?.role === "admin" || project.ownerId === user.id;
    let canRead = canWrite;
    if (!canWrite) {
      const [membership] = await tx
        .select({ permissions: projectMembers.permissions })
        .from(projectMembers)
        .where(
          and(
            eq(projectMembers.projectId, project.id),
            eq(projectMembers.userId, user.id),
          ),
        )
        .limit(1)
        .for("update");
      canRead = hasProjectPermission(membership?.permissions, "read");
      canWrite = hasProjectPermission(membership?.permissions, "write");
    }
    if (!canRead || !canWrite) {
      throw new TaskDocsNodeInvariantError(
        "Docs nodeが見つからないか書き込み権限がありません",
      );
    }
    return lockedNode;
  }

  // Personal-library ownership is independent of Project membership for
  // unbound nodes.  Admin does not implicitly grant access to another user's
  // Personal Docs.
  if (workspace.ownerUserId === user.id) return lockedNode;

  // Unbound Personal nodes inherit the nearest ancestor share.  Lock the
  // complete bounded chain before reading shares so a revoke cannot race the
  // binding.  A malformed/cyclic/deep chain fails closed rather than turning
  // the write path into an unbounded query loop.
  const permissionByNodeId = new Map<string, "read" | "write">();
  // Lock shares in the same lexical order as the node closure.  The generic
  // policy may then inspect nearest→root without ever creating an opposite
  // lock order with this binding path.
  for (const ancestorId of orderedAncestorIds) {
    const [share] = await tx
      .select({
        nodeId: knowledgeNodeShares.nodeId,
        permission: knowledgeNodeShares.permission,
      })
      .from(knowledgeNodeShares)
      .where(
        and(
          eq(knowledgeNodeShares.userId, user.id),
          eq(knowledgeNodeShares.nodeId, ancestorId),
        ),
      )
      .limit(1)
      .for("update");
    if (
      (share?.permission === "read" || share?.permission === "write")
      && typeof share.nodeId === "string"
    ) {
      permissionByNodeId.set(share.nodeId, share.permission);
    }
  }
  const nearestPermission = ancestorIds
    .map((ancestorId) => permissionByNodeId.get(ancestorId))
    .find((permission): permission is "read" | "write" =>
      permission === "read" || permission === "write");
  if (nearestPermission !== "write") {
    throw new TaskDocsNodeInvariantError(
      "Docs nodeが見つからないか書き込み権限がありません",
    );
  }
  return lockedNode;
}

/** Recheck identity and actor ACL inside the Task insert/update transaction. */
export async function assertTaskDocsNodeLinkAllowedInTransaction(
  tx: TaskDocsTransaction,
  nodeId: string,
  taskProjectId: string,
  user: SessionUser,
) {
  const [project] = await tx
    .select({ id: projects.id })
    .from(projects)
    .where(eq(projects.id, taskProjectId))
    .limit(1)
    .for("update");
  if (!project) throw new TaskDocsNodeInvariantError("Projectが見つかりません");
  const [node] = await tx
    .select()
    .from(knowledgeNodes)
    .where(eq(knowledgeNodes.id, nodeId))
    .limit(1);
  if (!node || node.archivedAt) {
    throw new TaskDocsNodeInvariantError("アーカイブ済みDocs nodeはタスクへ連携できません");
  }
  // Reject a foreign Project binding before the Docs ACL walk can lock that
  // Project.  Callers commonly hold the task Project row already; acquiring a
  // second Project in an arbitrary order would permit a cross-project
  // deadlock under concurrent moves/bind attempts.
  if (node.projectId && String(node.projectId) !== String(taskProjectId)) {
    throw new TaskDocsNodeInvariantError("別ProjectのDocs nodeはタスクへ連携できません");
  }
  // Reverse Project pointers are part of the canonical identity.  Lock them
  // before the node closure, matching Project-information repair and generic
  // Docs moves; otherwise a concurrent repair can hold the pointer while
  // this binding holds a node and both transactions wait on the other.
  const pointers = await tx
    .select({ id: projects.id })
    .from(projects)
    .where(eq(projects.knowledgeNodeId, nodeId))
    .limit(2)
    .for("update");
  const lockedNode = await assertTaskDocsWriteAccessInTransaction(tx, node, user);
  await assertTaskDocsNodeNotAoiTalkGuideInTransaction(lockedNode, tx);
  const systemKey = String(lockedNode.systemKey ?? "").trim();
  if (
    systemKey === "project_information_root"
    || systemKey.startsWith("project_information:")
    || pointers.length > 0
  ) {
    throw new TaskDocsNodeInvariantError(
      "Project canonical Docs nodeはタスク連携先にできません",
    );
  }
  return lockedNode;
}
