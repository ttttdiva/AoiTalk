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
import { hasProjectPermission } from "@/lib/server/project-permissions";

type ManagedNodeLike = {
  id: string;
  docsLibraryId: string;
  parentId: string | null;
  projectId?: string | null;
  systemKey: string | null;
  displayProps: unknown;
};

type ManagedDocsQueryClient = Pick<typeof db, "select">;
type ManagedDocsTransactionClient = ManagedDocsQueryClient & Pick<typeof db, "execute">;
type ManagedDocsActor = { id: string; role?: string | null };

const MANAGED_PREFIXES: ReadonlyArray<readonly [string, string]> = [
  ["project_inbox", "project_inbox"],
  ["project_mail", "project_mail"],
  ["workspace_file_reference:", "workspace_file_reference"],
  // The built-in AoiTalk Guide is a visible, system-managed Docs subtree.
  // Keep both the root key and the namespaced child keys protected so legacy
  // rows that predate display_props metadata still fail closed.
  ["aoitalk_guide", "aoitalk_guide"],
];
const MANAGED_DOCS_MAX_ANCESTOR_DEPTH = 512;

export function managedDocsDomain(
  node: Pick<ManagedNodeLike, "systemKey" | "displayProps">,
): string | null {
  const props =
    node.displayProps && typeof node.displayProps === "object" && !Array.isArray(node.displayProps)
      ? (node.displayProps as Record<string, unknown>)
      : {};
  if (props.system_managed === true && typeof props.managed_domain === "string") {
    return props.managed_domain.trim() || "system_managed";
  }
  const key = node.systemKey ?? "";
  for (const [prefix, domain] of MANAGED_PREFIXES) {
    // AoiTalk Guide owns only its exact root and colon-delimited descendants;
    // similarly named ordinary user keys (for example
    // `aoitalk_guide_backup`) must remain editable.  Other legacy prefixes
    // retain their historical matching semantics.
    const matches = prefix === "aoitalk_guide"
      ? key === prefix || key.startsWith(`${prefix}:`)
      : key === prefix || key.startsWith(prefix);
    if (matches) return domain;
  }
  return null;
}

export class ManagedDocsMutationError extends Error {
  readonly status = 409;

  constructor(readonly domain: string) {
    super(`${domain} は専用機能が管理しているため、通常のDocs編集では変更できません`);
  }
}

/**
 * Generic Docs routes fail closed for the node and each ancestor, including
 * descendants created before managed metadata was introduced.
 */
export async function assertGenericDocsMutationAllowed(
  node: ManagedNodeLike,
  queryClient: ManagedDocsQueryClient = db,
): Promise<void> {
  let current: ManagedNodeLike | undefined = node;
  const visited = new Set<string>();
  for (let depth = 0; current && !visited.has(current.id); depth += 1) {
    if (depth >= MANAGED_DOCS_MAX_ANCESTOR_DEPTH) {
      throw new ManagedDocsMutationError("unresolved_docs_ancestor");
    }
    visited.add(current.id);
    const domain = managedDocsDomain(current);
    if (domain) throw new ManagedDocsMutationError(domain);
    if (!current.parentId) return;
    const [parent] = await queryClient
      .select({
        id: knowledgeNodes.id,
        docsLibraryId: knowledgeNodes.docsLibraryId,
        parentId: knowledgeNodes.parentId,
        systemKey: knowledgeNodes.systemKey,
        displayProps: knowledgeNodes.displayProps,
      })
      .from(knowledgeNodes)
      .where(
        and(
          eq(knowledgeNodes.id, current.parentId),
          eq(knowledgeNodes.docsLibraryId, current.docsLibraryId),
        ),
      )
      .limit(1);
    if (!parent) throw new ManagedDocsMutationError("unresolved_docs_ancestor");
    current = parent;
  }
  if (current && visited.has(current.id)) {
    throw new ManagedDocsMutationError("unresolved_docs_ancestor");
  }
}

export class ManagedDocsAccessError extends Error {
  readonly status = 403;

  constructor(message = "Docs nodeへの書き込み権限がありません") {
    super(message);
    this.name = "ManagedDocsAccessError";
  }
}

/** Lock and reload the target before applying the ancestor policy in a write transaction. */
export async function lockAndAssertGenericDocsMutationAllowed(
  node: ManagedNodeLike,
  transaction: ManagedDocsTransactionClient,
  actor?: ManagedDocsActor,
): Promise<void> {
  // Project-bound Docs mutations share the Project->node lock order used by
  // Task updates and canonical Project repairs.  Acquire that row before
  // touching the target/ancestor chain; otherwise a task bind can hold the
  // Project row while this route holds the node and waits for the Project.
  const preflightProjectId = node.projectId ?? null;
  if (preflightProjectId) {
    const [project] = await transaction
      .select({ id: projects.id, deletedAt: projects.deletedAt })
      .from(projects)
      .where(eq(projects.id, preflightProjectId))
      .limit(1)
      .for("update");
    if (!project || project.deletedAt) {
      throw new ManagedDocsMutationError("unresolved_docs_ancestor");
    }
  }

  // Gather the complete ancestor chain without locks, then lock every row in
  // lexical id order.  Locking target→parent here would deadlock against a
  // concurrent task binding (and against the generic move route) that uses a
  // lexical closure order.  The structural snapshot is checked again after
  // the locks are held, so a concurrent reparent fails closed rather than
  // allowing a stale managed-policy decision.
  const snapshotNodes: ManagedNodeLike[] = [];
  const visited = new Set<string>();
  let currentId: string | null = node.id;
  for (let depth = 0; currentId; depth += 1) {
    if (depth >= MANAGED_DOCS_MAX_ANCESTOR_DEPTH || visited.has(currentId)) {
      throw new ManagedDocsMutationError("unresolved_docs_ancestor");
    }
    visited.add(currentId);
    const [current] = await transaction
      .select({
        id: knowledgeNodes.id,
        docsLibraryId: knowledgeNodes.docsLibraryId,
        parentId: knowledgeNodes.parentId,
        projectId: knowledgeNodes.projectId,
        systemKey: knowledgeNodes.systemKey,
        displayProps: knowledgeNodes.displayProps,
      })
      .from(knowledgeNodes)
      .where(
        and(
          eq(knowledgeNodes.id, currentId),
          eq(knowledgeNodes.docsLibraryId, node.docsLibraryId),
        ),
      )
      .limit(1);
    if (!current) throw new ManagedDocsMutationError("unresolved_docs_node");
    snapshotNodes.push(current);
    currentId = current.parentId;
  }
  if (snapshotNodes.length === 0) {
    throw new ManagedDocsMutationError("unresolved_docs_node");
  }

  const expectedParentById = new Map(
    snapshotNodes.map((snapshotNode) => [snapshotNode.id, snapshotNode.parentId ?? null]),
  );
  const orderedNodeIds = [...new Set(snapshotNodes.map((snapshotNode) => snapshotNode.id))].sort();
  const lockedById = new Map<string, ManagedNodeLike>();
  for (const nodeId of orderedNodeIds) {
    const [locked] = await transaction
      .select({
        id: knowledgeNodes.id,
        docsLibraryId: knowledgeNodes.docsLibraryId,
        parentId: knowledgeNodes.parentId,
        projectId: knowledgeNodes.projectId,
        systemKey: knowledgeNodes.systemKey,
        displayProps: knowledgeNodes.displayProps,
      })
      .from(knowledgeNodes)
      .where(
        and(
          eq(knowledgeNodes.id, nodeId),
          eq(knowledgeNodes.docsLibraryId, node.docsLibraryId),
        ),
      )
      .limit(1)
      .for("update");
    if (
      !locked
      || String(locked.docsLibraryId) !== String(node.docsLibraryId)
      || (locked.parentId ?? null) !== (expectedParentById.get(nodeId) ?? null)
    ) {
      throw new ManagedDocsMutationError("unresolved_docs_ancestor");
    }
    lockedById.set(nodeId, locked);
  }
  const lockedNodes = snapshotNodes.map((snapshotNode) => {
    const locked = lockedById.get(snapshotNode.id);
    if (!locked) throw new ManagedDocsMutationError("unresolved_docs_node");
    return locked;
  });
  for (const lockedNode of lockedNodes) {
    const domain = managedDocsDomain(lockedNode);
    if (domain) throw new ManagedDocsMutationError(domain);
  }
  // If the target changed Project identity after the preflight snapshot, do
  // not acquire a newly discovered Project row after the node lock (that
  // would invert the global order). Fail closed and let the caller retry.
  const lockedProjectId = lockedNodes[0].projectId ?? null;
  if (lockedProjectId !== preflightProjectId) {
    throw new ManagedDocsMutationError("unresolved_docs_ancestor");
  }

  if (!actor) return;

  const [library] = await transaction
    .select({ id: docsLibraries.id, ownerUserId: docsLibraries.ownerUserId })
    .from(docsLibraries)
    .where(eq(docsLibraries.id, node.docsLibraryId))
    .limit(1)
    .for("update");
  if (!library) throw new ManagedDocsAccessError();

  const lockedTarget = lockedNodes[0];
  if (lockedTarget.projectId) {
    const [actorRow] = await transaction
      .select({ role: users.role })
      .from(users)
      .where(eq(users.id, actor.id))
      .limit(1)
      .for("update");
    const [project] = await transaction
      .select({ id: projects.id, ownerId: projects.ownerId, deletedAt: projects.deletedAt })
      .from(projects)
      .where(eq(projects.id, lockedTarget.projectId))
      .limit(1)
      .for("update");
    if (!project || project.deletedAt) throw new ManagedDocsAccessError();
    if (actorRow?.role === "admin" || project.ownerId === actor.id) return;
    const [membership] = await transaction
      .select({ permissions: projectMembers.permissions })
      .from(projectMembers)
      .where(and(eq(projectMembers.projectId, project.id), eq(projectMembers.userId, actor.id)))
      .limit(1)
      .for("update");
    if (!hasProjectPermission(membership?.permissions, "write")) {
      throw new ManagedDocsAccessError();
    }
    return;
  }

  // Personal-library ownership is independent of Project membership.  A
  // non-owner may mutate an unbound node only through the nearest inherited
  // write share; lock every consulted share row before evaluating it.
  if (library.ownerUserId === actor.id) return;
  const ancestorIds = lockedNodes.map((lockedNode) => lockedNode.id);
  // Check inherited ancestors one-by-one so a nearest read share cannot be
  // bypassed by a broader write share and adapters need not expose `inArray`.
  for (const ancestorId of ancestorIds) {
    const [share] = await transaction
      .select({ permission: knowledgeNodeShares.permission })
      .from(knowledgeNodeShares)
      .where(
        and(
          eq(knowledgeNodeShares.userId, actor.id),
          eq(knowledgeNodeShares.nodeId, ancestorId),
        ),
      )
      .limit(1)
      .for("update");
    if (share?.permission === "write") return;
    if (share?.permission === "read") break;
  }
  throw new ManagedDocsAccessError();
}
