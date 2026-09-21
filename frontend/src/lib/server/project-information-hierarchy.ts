import { and, eq, isNull, or, sql } from "drizzle-orm";
import { db } from "@/db";
import {
  knowledgeNodes,
  knowledgeNodeSupertags,
  knowledgeSupertags,
  projectMembers,
  projects,
  tasks,
  users,
} from "@/db/schema";
import { docsLibraries } from "@/lib/server/docs-library-schema";
import { hasProjectPermission } from "@/lib/server/project-permissions";
import { insertDocsNode, updateDocsNode } from "./docs-node-writer";
import { appendKnowledgeRevision, upsertKnowledgeSearchIndex } from "./knowledge-docs-utils";

export const PROJECT_INFORMATION_ROOT_SYSTEM_KEY = "project_information_root";
export const PROJECT_INFORMATION_TAG_SYSTEM_KEY = "project_info";

export const PROJECT_INFORMATION_HUB_TITLE = "案件情報";

type AdvisoryLockClient = Pick<typeof db, "execute">;

/** Shared transaction advisory keys used by every canonical hierarchy writer. */
export async function lockProjectInformationAdvisory(
  client: AdvisoryLockClient,
  projectId: string,
) {
  await client.execute(
    sql`select pg_advisory_xact_lock(hashtext(${`project-information:${projectId}`}))`,
  );
}

export async function lockProjectMeetingAdvisory(
  client: AdvisoryLockClient,
  docsLibraryId: string,
  projectId: string,
) {
  await client.execute(
    sql`select pg_advisory_xact_lock(hashtext(${`${docsLibraryId}:project-meetings:${projectId}`}))`,
  );
}

export class ProjectInformationAccessError extends Error {
  readonly status = 403;
  readonly code = "project_access_denied";

  constructor(message = "Projectへの書き込み権限がありません") {
    super(message);
    this.name = "ProjectInformationAccessError";
  }
}

/**
 * Docs の保存先は物理 `docs_libraries` table で、アプリケーションの
 * 契約上は DocsLibrary と呼びます。旧 workspace 語彙はこの server 層の
 * source-level alias に閉じ込め、wire DTO では `library`/`docs_library_id`
 * を正本にします。
 */
export type DocsLibrary = typeof docsLibraries.$inferSelect;
export type DocsLibraryId = DocsLibrary["id"];

export const PROJECT_INFORMATION_ROOT_KEY = PROJECT_INFORMATION_ROOT_SYSTEM_KEY;
export function projectInformationSystemKey(projectId: string) {
  return `project_information:${projectId}`;
}

export function canonicalProjectInformationTitle(
  project: Pick<ProjectRow, "name">,
): string {
  // `projects.name` is NOT NULL, but old/imported rows can still contain an
  // empty value.  Never feed a blank title into the Docs writer: the hub title
  // is the safe identity fallback for that malformed legacy case.
  const title = typeof project.name === "string" ? project.name.trim() : "";
  return title || PROJECT_INFORMATION_HUB_TITLE;
}

/** Read-only personal library resolver.  GET 経路からは決して作成しない。 */
export async function getPersonalDocsLibrary(
  ownerUserId: string,
): Promise<DocsLibrary | null> {
  const [library] = await db
    .select()
    .from(docsLibraries)
    .where(
      and(
        eq(docsLibraries.ownerUserId, ownerUserId),
        eq(docsLibraries.libraryType, "personal"),
      ),
    )
    .orderBy(docsLibraries.createdAt, docsLibraries.id)
    .limit(1);
  return library ?? null;
}

/**
 * Resolve the project owner's Personal Docs Library, creating it only on a
 * write/bootstrap path.  Project membership never changes library ownership.
 */
export async function ensurePersonalDocsLibrary(
  ownerUserId: string,
  actorUserId = ownerUserId,
): Promise<DocsLibrary> {
  // Repairing a Project root must not reseed/mutate the owner's personal
  // Home/default nodes when the library already exists. Resolve the existing
  // canonical row first; only a genuinely missing library uses the bootstrap
  // path (which creates the owner-owned defaults exactly once).
  const existing = await getPersonalDocsLibrary(ownerUserId);
  if (existing) return existing;
  // Keep the seed implementation in knowledge-docs-utils as the single
  // source of truth. Dynamic import avoids a module cycle because that module
  // also consumes the hierarchy helpers from API routes.
  const { ensureDocsWorkspace } = await import("./knowledge-docs-utils");
  // Calling the idempotent ensure path for an existing library repairs
  // settings/default tags on write without changing ownership. GET uses the
  // separate `getPersonalDocsLibrary` resolver and therefore remains pure.
  return ensureDocsWorkspace({ id: ownerUserId, role: actorUserId === ownerUserId ? "user" : null });
}

type ProjectRow = typeof projects.$inferSelect;

function metadataObject(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? { ...(value as Record<string, unknown>) }
    : {};
}

export function isDefaultInboxProject(project: Pick<ProjectRow, "ownerId" | "slug" | "projectMetadata">) {
  const metadata = metadataObject(project.projectMetadata);
  return project.slug === `inbox-project-${project.ownerId}` || metadata.isInboxDefault === true;
}

export async function ensureProjectInformationRoot(
  docsLibraryId: string,
  userId: string,
  client?: DocsWriteClient,
) {
  const run = async (tx: DocsWriteClient) => {
    await tx.execute(sql`select pg_advisory_xact_lock(hashtext(${`${docsLibraryId}:project-information-root`}))`);
    const [library] = await tx
      .select()
      .from(docsLibraries)
      .where(
        and(
          eq(docsLibraries.id, docsLibraryId),
          eq(docsLibraries.libraryType, "personal"),
        ),
      )
      .limit(1);
    if (!library) throw new Error("Project owner Personal Docs Library could not be resolved");
    // The hub is owner-private library metadata.  A Project writer/member may
    // create children below an existing canonical hub, but must never create,
    // rename, unarchive, or reparent the owner's hub itself.
    const isLibraryOwner = library.ownerUserId === userId;
    const [existing] = await tx
      .select()
      .from(knowledgeNodes)
      .where(
        and(
          eq(knowledgeNodes.docsLibraryId, docsLibraryId),
          sql`btrim(${knowledgeNodes.systemKey}) = ${PROJECT_INFORMATION_ROOT_SYSTEM_KEY}`,
        ),
      )
      .limit(1);

    if (existing) {
      const canonical =
        existing.parentId === null &&
        existing.rootPageId === existing.id &&
        String(existing.systemKey ?? "").trim() === PROJECT_INFORMATION_ROOT_SYSTEM_KEY &&
        existing.title === "案件情報" &&
        existing.archivedAt === null &&
        existing.nodeType === "node" &&
        existing.isExplicitBlank !== true;
      if (!canonical && !isLibraryOwner) {
        throw new Error("案件情報hubの修復にはPersonal Docs Library所有者権限が必要です");
      }
      if (canonical) {
        // Normalize a padded legacy key while the library advisory lock is
        // held so future strict resolvers cannot create a second hub identity.
        if (existing.systemKey !== PROJECT_INFORMATION_ROOT_SYSTEM_KEY) {
          return updateDocsNode(tx, existing.id, {
            systemKey: PROJECT_INFORMATION_ROOT_SYSTEM_KEY,
            updatedBy: userId,
            updatedAt: new Date(),
          });
        }
        return existing;
      }
      const repaired = await updateDocsNode(tx, existing.id, {
        title: "案件情報",
        parentId: null,
        rootPageId: existing.id,
        projectId: null,
        nodeType: "node",
        archivedAt: null,
        updatedBy: userId,
        updatedAt: new Date(),
      });
      await upsertKnowledgeSearchIndex(tx, repaired, repaired.title);
      return repaired;
    }

    if (!isLibraryOwner) {
      throw new Error("案件情報hubの作成にはPersonal Docs Library所有者権限が必要です");
    }

    const created = await insertDocsNode(tx, {
      docsLibraryId,
      parentId: null,
      rootPageId: null,
      projectId: null,
      systemKey: PROJECT_INFORMATION_ROOT_SYSTEM_KEY,
      title: "案件情報",
      bodyJson: { format: "project_information_collection" },
      nodeType: "node",
      sortOrder: 1,
      createdBy: userId,
      updatedBy: userId,
    });
    const rooted = await updateDocsNode(tx, created.id, {
      rootPageId: created.id,
      updatedBy: userId,
      updatedAt: new Date(),
    });
    await upsertKnowledgeSearchIndex(tx, rooted, rooted.title);
    await appendKnowledgeRevision(tx, rooted, userId, "案件情報hubを作成");
    return rooted;
  };
  return client ? run(client) : db.transaction(run);
}

export type ProjectInformationLifecycleStatus =
  | "active"
  | "retained"
  | "missing"
  | "library_missing"
  | "hub_invalid"
  | "pointer_invalid";

export type ProjectInformationHierarchyResolution = {
  status: ProjectInformationLifecycleStatus;
  project: ProjectRow;
  pointerId: string | null;
  expectedTitle: string;
  titleValid: boolean;
  library: DocsLibrary | null;
  hub: typeof knowledgeNodes.$inferSelect | null;
  node: typeof knowledgeNodes.$inferSelect | null;
  supertag: typeof knowledgeSupertags.$inferSelect | null;
  /** Candidate rows with the exact project system key, used by owner cleanup. */
  staleNodes: Array<typeof knowledgeNodes.$inferSelect>;
};

type DocsQueryClient = Pick<typeof db, "select">;
type DocsWriteClient = Parameters<Parameters<typeof db.transaction>[0]>[0];

/**
 * Recheck Project write access while the lifecycle transaction owns the
 * Project and membership rows.  The route-level preflight is intentionally
 * only an early rejection: hierarchy/section creation can itself mutate the
 * owner's Personal Docs, so a membership revoke racing that work must be
 * serialized with the mutation rather than observed only afterwards.
 */
async function assertProjectWriteAccessInTransaction(
  tx: DocsWriteClient,
  project: Pick<ProjectRow, "id" | "ownerId">,
  userId: string,
) {
  const [actor] = await tx
    .select({ role: users.role })
    .from(users)
    .where(eq(users.id, userId))
    .limit(1)
    .for("update");
  if (actor?.role === "admin" || project.ownerId === userId) return;
  const [membership] = await tx
    .select({ permissions: projectMembers.permissions })
    .from(projectMembers)
    .where(
      and(
        eq(projectMembers.projectId, project.id),
        eq(projectMembers.userId, userId),
      ),
    )
    .limit(1)
    .for("update");
  if (!hasProjectPermission(membership?.permissions, "write")) {
    throw new ProjectInformationAccessError();
  }
}

function lifecycleForProject(project: ProjectRow, node: typeof knowledgeNodes.$inferSelect | null) {
  if (!node) return "missing" as const;
  return !project.deletedAt && !project.isCompleted ? "active" as const : "retained" as const;
}

/**
 * Resolve Project-information identity without guessing from titles/chips.
 *
 * `projects.knowledge_node_id` is a denormalized pointer.  It is considered
 * canonical only when *all* ownership and hierarchy predicates hold:
 * owner Personal Library, canonical 案件情報 hub, exact project/system key,
 * parent/root identity, active archive state, and the project_info supertag
 * attached in the same library.  The pointer is deliberately not replaced by
 * an arbitrary candidate during reads; callers decide whether a writer may
 * repair a missing/invalid pointer.
 */
export async function resolveProjectInformationNode(options: {
  project: ProjectRow;
  docsLibraryId?: string | null;
  client?: DocsQueryClient;
  includeInactive?: boolean;
}): Promise<ProjectInformationHierarchyResolution> {
  const client = options.client ?? db;
  const project = options.project;
  const pointerId = project.knowledgeNodeId ?? null;
  const expectedTitle = canonicalProjectInformationTitle(project);
  const empty = (
    status: ProjectInformationLifecycleStatus,
    values: Partial<ProjectInformationHierarchyResolution> = {},
  ): ProjectInformationHierarchyResolution => ({
    status,
    project,
    pointerId,
    expectedTitle,
    titleValid: false,
    library: null,
    hub: null,
    node: null,
    supertag: null,
    staleNodes: [],
    ...values,
  });

  // A deleted Project is never returned as an active hierarchy subject, but
  // cleanup still needs to inspect its retained pointer/candidate rows.
  const library = options.docsLibraryId
    ? (
        await client
          .select()
          .from(docsLibraries)
          .where(
            and(
              eq(docsLibraries.id, options.docsLibraryId),
              eq(docsLibraries.libraryType, "personal"),
              eq(docsLibraries.ownerUserId, project.ownerId),
            ),
          )
          .limit(1)
      )[0] ?? null
    : (
        await client
          .select()
          .from(docsLibraries)
          .where(
            and(
              eq(docsLibraries.ownerUserId, project.ownerId),
              eq(docsLibraries.libraryType, "personal"),
            ),
          )
          .orderBy(docsLibraries.createdAt, docsLibraries.id)
          .limit(1)
      )[0] ?? null;
  if (!library) return empty("library_missing");

  const [hub] = await client
    .select()
    .from(knowledgeNodes)
    .where(
      and(
        eq(knowledgeNodes.docsLibraryId, library.id),
        sql`btrim(${knowledgeNodes.systemKey}) = ${PROJECT_INFORMATION_ROOT_SYSTEM_KEY}`,
        eq(knowledgeNodes.title, PROJECT_INFORMATION_HUB_TITLE),
        isNull(knowledgeNodes.archivedAt),
        isNull(knowledgeNodes.parentId),
        eq(knowledgeNodes.rootPageId, knowledgeNodes.id),
        eq(knowledgeNodes.nodeType, "node"),
        eq(knowledgeNodes.isExplicitBlank, false),
      ),
    )
    .limit(1);

  const [supertag] = await client
    .select()
    .from(knowledgeSupertags)
    .where(
      and(
        eq(knowledgeSupertags.docsLibraryId, library.id),
        sql`btrim(${knowledgeSupertags.systemKey}) = ${PROJECT_INFORMATION_TAG_SYSTEM_KEY}`,
      ),
    )
    .limit(1);

  const systemKey = projectInformationSystemKey(project.id);
  // Candidate lookup intentionally includes archived rows.  It powers owner
  // cleanup for stale/orphaned Project rows while the canonical `node` below
  // remains pointer-only and active-hierarchy strict.
  const duplicateSystemKeyPrefix = `project_information:duplicate:${project.id}:`;
  let staleNodes = await client
    .select()
    .from(knowledgeNodes)
    .where(
      and(
        eq(knowledgeNodes.docsLibraryId, library.id),
        eq(knowledgeNodes.projectId, project.id),
        or(
          sql`btrim(${knowledgeNodes.systemKey}) = ${systemKey}`,
          sql`btrim(${knowledgeNodes.systemKey}) like ${`${duplicateSystemKeyPrefix}%`}`,
        ),
      ),
    );
  if (pointerId && !staleNodes.some((candidate) => candidate.id === pointerId)) {
    // Keep a same-library pointer target in the cleanup projection even when
    // legacy data lost project_id/system_key or was archived.  It is not
    // adopted as canonical; the cleanup endpoint still requires the exact
    // hub/parent/key proof below before touching it.
    const [pointerCandidate] = await client
      .select()
      .from(knowledgeNodes)
      .where(
        and(
          eq(knowledgeNodes.id, pointerId),
          eq(knowledgeNodes.docsLibraryId, library.id),
        ),
      )
      .limit(1);
    if (pointerCandidate) staleNodes = [...staleNodes, pointerCandidate];
  }

  if (!hub) {
    return empty("hub_invalid", { library, supertag, staleNodes });
  }
  if (!supertag) {
    return empty("pointer_invalid", { library, hub, staleNodes });
  }

  let pointerNode: typeof knowledgeNodes.$inferSelect | null = null;
  if (pointerId) {
    const [nodeRow] = await client
      .select()
      .from(knowledgeNodes)
      .where(
        and(
          eq(knowledgeNodes.id, pointerId),
          eq(knowledgeNodes.docsLibraryId, library.id),
          eq(knowledgeNodes.projectId, project.id),
          sql`btrim(${knowledgeNodes.systemKey}) = ${systemKey}`,
          eq(knowledgeNodes.parentId, hub.id),
          eq(knowledgeNodes.rootPageId, hub.id),
          isNull(knowledgeNodes.archivedAt),
          eq(knowledgeNodes.nodeType, "node"),
          eq(knowledgeNodes.isExplicitBlank, false),
        ),
      )
      .limit(1);
    if (nodeRow) {
      const [tagLink] = await client
        .select()
        .from(knowledgeNodeSupertags)
        .where(
          and(
            eq(knowledgeNodeSupertags.nodeId, nodeRow.id),
            eq(knowledgeNodeSupertags.supertagId, supertag.id),
          ),
        )
        .limit(1);
      if (tagLink && nodeRow.isExplicitBlank !== true) pointerNode = nodeRow;
    }
  }

  if (!pointerNode) {
    return empty(pointerId || staleNodes.length > 0 ? "pointer_invalid" : "missing", {
      library,
      hub,
      supertag,
      staleNodes,
    });
  }

  const status = lifecycleForProject(project, pointerNode);
  if (status === "retained" && options.includeInactive === false) {
    return empty("pointer_invalid", {
      library,
      hub,
      supertag,
      staleNodes,
    });
  }
  return {
    status,
    project,
    pointerId,
    expectedTitle,
    titleValid: pointerNode.title === expectedTitle,
    library,
    hub,
    node: pointerNode,
    supertag,
    staleNodes,
  };
}

/**
 * Read-only hierarchy lookup retained for existing Project-information API
 * callers.  Deleted projects intentionally return no active node; completed
 * projects retain their canonical pointer for cleanup/inspection.
 */
export async function getProjectInformationHierarchyNode(options: {
  project: ProjectRow;
  docsLibraryId?: string | null;
}) {
  // Keep this read path intentionally tiny and pointer-only.  In particular,
  // a null pointer must not trigger a candidate search (which could adopt an
  // arbitrary legacy row as the Project root).  The richer lifecycle resolver
  // above is reserved for explicit classification/cleanup callers.
  if (options.project.deletedAt) return { library: null, hub: null, node: null };
  const library = options.docsLibraryId
    ? (
        await db
          .select()
          .from(docsLibraries)
          .where(
            and(
              eq(docsLibraries.id, options.docsLibraryId),
              eq(docsLibraries.libraryType, "personal"),
              eq(docsLibraries.ownerUserId, options.project.ownerId),
            ),
          )
          .limit(1)
      )[0] ?? null
    : await getPersonalDocsLibrary(options.project.ownerId);
  if (!library) return { library: null, hub: null, node: null };
  const [hub] = await db
    .select()
    .from(knowledgeNodes)
    .where(
      and(
        eq(knowledgeNodes.docsLibraryId, library.id),
        sql`btrim(${knowledgeNodes.systemKey}) = ${PROJECT_INFORMATION_ROOT_SYSTEM_KEY}`,
        eq(knowledgeNodes.title, PROJECT_INFORMATION_HUB_TITLE),
        isNull(knowledgeNodes.archivedAt),
        isNull(knowledgeNodes.parentId),
        eq(knowledgeNodes.rootPageId, knowledgeNodes.id),
      ),
    )
    .limit(1);
  if (!hub) return { library, hub: null, node: null };
  if (!options.project.knowledgeNodeId) return { library, hub, node: null };
  const [nodeRow] = await db
    .select({ node: knowledgeNodes })
    .from(knowledgeNodes)
    .innerJoin(knowledgeNodeSupertags, eq(knowledgeNodeSupertags.nodeId, knowledgeNodes.id))
    .innerJoin(knowledgeSupertags, eq(knowledgeSupertags.id, knowledgeNodeSupertags.supertagId))
    .where(
      and(
        eq(knowledgeNodes.id, options.project.knowledgeNodeId),
        eq(knowledgeNodes.docsLibraryId, library.id),
        eq(knowledgeNodes.projectId, options.project.id),
        sql`btrim(${knowledgeNodes.systemKey}) = ${projectInformationSystemKey(options.project.id)}`,
        eq(knowledgeNodes.parentId, hub.id),
        eq(knowledgeNodes.rootPageId, hub.id),
        isNull(knowledgeNodes.archivedAt),
        eq(knowledgeNodes.nodeType, "node"),
        eq(knowledgeNodes.isExplicitBlank, false),
        eq(knowledgeSupertags.docsLibraryId, library.id),
        sql`btrim(${knowledgeSupertags.systemKey}) = ${PROJECT_INFORMATION_TAG_SYSTEM_KEY}`,
      ),
    )
    .limit(1);
  return { library, hub, node: nodeRow?.node ?? null };
}

/**
 * Synchronize a canonical Project-information title after Project metadata is
 * renamed.  This is deliberately a no-op for missing/invalid pointers: only
 * the strict resolver may identify the node, and callers can surface a repair
 * conflict rather than mutating arbitrary rows.
 */
export async function synchronizeProjectInformationTitle(options: {
  project: ProjectRow;
  userId: string;
  client?: DocsQueryClient;
}) {
  const client = options.client ?? db;
  const resolution = await resolveProjectInformationNode({
    project: options.project,
    client,
    includeInactive: true,
  });
  if (!resolution.node || !resolution.library || !resolution.hub) {
    return { ...resolution, node: null };
  }
  if (resolution.node.title === resolution.expectedTitle) return resolution;
  const updated = await updateDocsNode(client as never, resolution.node.id, {
    title: resolution.expectedTitle,
    updatedBy: options.userId,
    updatedAt: new Date(),
  });
  await upsertKnowledgeSearchIndex(client as never, updated, updated.title);
  await appendKnowledgeRevision(client as never, updated, options.userId, "Project名変更に伴い案件情報タイトルを同期");
  return {
    ...resolution,
    node: updated,
    titleValid: true,
  };
}

export async function ensureProjectInformationHierarchyNode(options: {
  docsLibraryId?: string;
  userId: string;
  project: ProjectRow;
  client?: DocsWriteClient;
}) {
  if (isDefaultInboxProject(options.project)) {
    throw new Error("Inboxは案件情報Docsの保存先にできません。実案件を指定してください。");
  }
  if (options.project.deletedAt || options.project.isCompleted) {
    throw new Error("完了/削除済みProjectのcanonical Docsは専用クリーンアップ経路で管理されます");
  }
  // A project root always lives in its owner's Personal Docs Library.  Never
  // create a project-scoped workspace as a side effect of project information.
  // `docsLibraryId` is accepted for explicit repair/bootstrap calls, but is
  // validated against the project owner before use.
  const docsLibrary = options.docsLibraryId
    ? (
        await db
          .select()
          .from(docsLibraries)
          .where(
            and(
              eq(docsLibraries.id, options.docsLibraryId),
              eq(docsLibraries.libraryType, "personal"),
              eq(docsLibraries.ownerUserId, options.project.ownerId),
            ),
          )
          .limit(1)
      )[0] ?? null
    : await getPersonalDocsLibrary(options.project.ownerId);
  if (!docsLibrary && options.userId !== options.project.ownerId) {
    throw new Error("Project owner Personal Docs Library is not initialized");
  }
  const resolvedDocsLibrary = docsLibrary ?? await ensurePersonalDocsLibrary(options.project.ownerId, options.userId);
  if (!resolvedDocsLibrary) {
    throw new Error("Project owner Personal Docs Library could not be resolved");
  }
  const run = async (tx: DocsWriteClient) => {
    // Serialize all canonical Project identity writers before taking the
    // Project row.  Task/Docs callers acquire this same advisory first, so a
    // concurrent repair cannot invert the lock order.
    await lockProjectInformationAdvisory(tx, options.project.id);
    const [lockedProject] = await tx
      .select()
      .from(projects)
      .where(eq(projects.id, options.project.id))
      .limit(1)
      .for("update");
    if (!lockedProject) {
      throw new Error("Projectが見つかりません");
    }
    if (isDefaultInboxProject(lockedProject)) {
      throw new Error("Inboxは案件情報Docsの保存先にできません");
    }
    if (lockedProject.deletedAt || lockedProject.isCompleted) {
      throw new Error("完了/削除済みProjectのcanonical Docsは専用クリーンアップ経路で管理されます");
    }
    await assertProjectWriteAccessInTransaction(tx, lockedProject, options.userId);
    // Keep hub repair/creation in the same transaction as the lifecycle check;
    // a concurrent completion/deletion must roll back every hierarchy side
    // effect rather than leaving a repaired metadata shell behind.
    const hub = await ensureProjectInformationRoot(
      resolvedDocsLibrary.id,
      options.userId,
      tx,
    );
    const canonicalProjectTitle = canonicalProjectInformationTitle(lockedProject);
    const lockedProjectId = lockedProject.id;
    // The project-information supertag is part of the canonical identity,
    // not optional presentation metadata.  A writer may only create this
    // owner-private definition when they own the Personal Library; members
    // can create a child below an already initialized hub/tag but cannot
    // mutate private library metadata as a side effect.
    let [projectInformationTag] = await tx
      .select()
      .from(knowledgeSupertags)
      .where(
        and(
          eq(knowledgeSupertags.docsLibraryId, resolvedDocsLibrary.id),
          sql`btrim(${knowledgeSupertags.systemKey}) = ${PROJECT_INFORMATION_TAG_SYSTEM_KEY}`,
        ),
      )
      .limit(1);
    if (!projectInformationTag) {
      if (resolvedDocsLibrary.ownerUserId !== options.userId) {
        throw new Error("案件情報スーパータグの作成にはPersonal Docs Library所有者権限が必要です");
      }
      [projectInformationTag] = await tx
        .insert(knowledgeSupertags)
        .values({
          docsLibraryId: resolvedDocsLibrary.id,
          systemKey: PROJECT_INFORMATION_TAG_SYSTEM_KEY,
          name: "案件情報",
          baseType: "project_information",
          description: "案件概要、進捗、課題管理、決定事項、参照、Q&Aをまとめる正本ページ",
          icon: "book-open",
          color: "#2563eb",
          templateJson: {
            format: "project_information_doc_block",
            source: "docs_canonical",
            blocks: [{ type: "project_qa_block", source: "project_qa_entries" }],
          },
          pinnedFieldIds: [],
          configJson: {},
          aiInstructions: "案件情報ページはプロジェクトの正本として扱う。",
        })
        .returning();
    }
    if (!projectInformationTag) {
      throw new Error("案件情報スーパータグを初期化できません");
    }
    if (projectInformationTag.systemKey !== PROJECT_INFORMATION_TAG_SYSTEM_KEY) {
      projectInformationTag.systemKey = PROJECT_INFORMATION_TAG_SYSTEM_KEY;
      await tx
        .update(knowledgeSupertags)
        .set({ systemKey: PROJECT_INFORMATION_TAG_SYSTEM_KEY })
        .where(eq(knowledgeSupertags.id, projectInformationTag.id));
    }
    let node: typeof knowledgeNodes.$inferSelect | undefined;
    if (lockedProject.knowledgeNodeId) {
      const [row] = await tx
        .select()
        .from(knowledgeNodes)
        .innerJoin(knowledgeNodeSupertags, eq(knowledgeNodeSupertags.nodeId, knowledgeNodes.id))
        .where(
          and(
            eq(knowledgeNodes.id, lockedProject.knowledgeNodeId),
            eq(knowledgeNodes.docsLibraryId, resolvedDocsLibrary.id),
            eq(knowledgeNodes.parentId, hub.id),
            eq(knowledgeNodes.rootPageId, hub.id),
            eq(knowledgeNodes.projectId, lockedProjectId),
            sql`btrim(${knowledgeNodes.systemKey}) = ${`project_information:${lockedProjectId}`}`,
            eq(knowledgeNodes.nodeType, "node"),
            eq(knowledgeNodes.isExplicitBlank, false),
            isNull(knowledgeNodes.archivedAt),
            eq(knowledgeNodeSupertags.supertagId, projectInformationTag.id),
          ),
        )
        .limit(1);
      node = row?.knowledge_nodes;
    }
    if (!node) {
      const [row] = await tx
        .select()
        .from(knowledgeNodes)
        .innerJoin(knowledgeNodeSupertags, eq(knowledgeNodeSupertags.nodeId, knowledgeNodes.id))
        .where(
          and(
            eq(knowledgeNodes.docsLibraryId, resolvedDocsLibrary.id),
            eq(knowledgeNodes.parentId, hub.id),
            eq(knowledgeNodes.rootPageId, hub.id),
            eq(knowledgeNodes.projectId, lockedProjectId),
            sql`btrim(${knowledgeNodes.systemKey}) = ${`project_information:${lockedProjectId}`}`,
            eq(knowledgeNodes.nodeType, "node"),
            eq(knowledgeNodes.isExplicitBlank, false),
            isNull(knowledgeNodes.archivedAt),
            eq(knowledgeNodeSupertags.supertagId, projectInformationTag.id),
          ),
        )
        .limit(1);
      node = row?.knowledge_nodes;
    }

    if (!node) {
      // The exact system key is unique per Personal Library even when a
      // previous pointer was archived or left under a malformed parent.  Reuse
      // that locked row during repair instead of attempting an INSERT that
      // fails the unique constraint and strands the Project indefinitely.
      const [staleExact] = await tx
        .select()
        .from(knowledgeNodes)
        .where(
          and(
            eq(knowledgeNodes.docsLibraryId, resolvedDocsLibrary.id),
            sql`btrim(${knowledgeNodes.systemKey}) = ${`project_information:${lockedProjectId}`}`,
          ),
        )
        .limit(1)
        .for("update");
      node = staleExact;
    }

    const created = !node;
    if (!node) {
      node = await insertDocsNode(tx, {
        docsLibraryId: resolvedDocsLibrary.id,
        parentId: hub.id,
        rootPageId: hub.id,
        projectId: lockedProjectId,
        systemKey: `project_information:${lockedProjectId}`,
        title: canonicalProjectTitle,
        bodyJson: {
          format: "project_information_doc_block",
          source: "docs_canonical",
          blocks: [{ type: "project_qa_block", source: "project_qa_entries" }],
        },
        nodeType: "node",
        sortOrder: 0,
        createdBy: options.userId,
        updatedBy: options.userId,
      });
    } else {
      // The Project row is already locked above; acquire the target node lock
      // second, matching generic Docs deletion/move and preventing a late
      // pointer assignment to a row that was archived by a concurrent write.
      const lockedNodeRows = await tx
        .select({ id: knowledgeNodes.id })
        .from(knowledgeNodes)
        .where(eq(knowledgeNodes.id, node.id))
        .for("update");
      if (lockedNodeRows.length === 0) {
        throw new Error("Project canonical node disappeared during repair");
      }
      // A legacy/imported task may still point at an exact
      // `project_information:<id>` row even when the Project reverse pointer
      // is missing. Never promote such a Docs node to canonical: doing so
      // would turn a task's future title-sync target into an identity-owned
      // node and violate the task↔Docs binding invariant.
      if (tasks?.knowledgeNodeId) {
        const [taskReference] = await tx
          .select({ id: tasks.id })
          .from(tasks)
          .where(eq(tasks.knowledgeNodeId, node.id))
          .limit(1)
          .for("update");
        if (taskReference) {
          throw new Error("タスク連携済みDocs nodeはProject canonical identityへ昇格できません");
        }
      }
      node = await updateDocsNode(tx, node.id, {
        parentId: hub.id,
        rootPageId: hub.id,
        projectId: lockedProjectId,
        systemKey: `project_information:${lockedProjectId}`,
        nodeType: "node",
        archivedAt: null,
        // Project name is the canonical identity label.  Generic Docs edits
        // may never leave this root blank or with a stale arbitrary title;
        // project metadata is the authority and is synchronized on repair.
        title: canonicalProjectTitle,
        updatedBy: options.userId,
        updatedAt: new Date(),
      });
      // Rebase the complete structural closure.  Legacy rows can have stale
      // project_id/root_page_id denormalizers, so a shallow OR predicate would
      // leave grandchildren orphaned from the repaired outline.
      const conflictingIdentity = await tx.execute(sql`
        with recursive descendants as (
          select id, system_key
          from knowledge_nodes
          where id = ${node.id} and docs_library_id = ${resolvedDocsLibrary.id}
          union all
          select child.id, child.system_key
          from knowledge_nodes child
          join descendants parent on child.parent_id = parent.id
          where child.docs_library_id = ${resolvedDocsLibrary.id}
        )
        select 1
        from descendants d
        left join projects p on p.knowledge_node_id = d.id
        where d.id <> ${node.id}
          and (
            (p.id is not null and p.id <> ${lockedProjectId})
            or btrim(d.system_key) like 'project_information:%'
          )
        limit 1
      `);
      if (Array.isArray(conflictingIdentity) && conflictingIdentity.length > 0) {
        throw new Error("Project canonical hierarchy contains another Project identity");
      }
      await tx.execute(sql`
        with recursive descendants as (
          select id
          from knowledge_nodes
          where id = ${node.id} and docs_library_id = ${resolvedDocsLibrary.id}
          union all
          select child.id
          from knowledge_nodes child
          join descendants parent on child.parent_id = parent.id
          where child.docs_library_id = ${resolvedDocsLibrary.id}
        )
        update knowledge_nodes
        set root_page_id = ${hub.id},
            project_id = ${lockedProjectId},
            updated_by = ${options.userId},
            updated_at = now()
        where id in (select id from descendants)
          and id <> ${node.id}
      `);
    }

    await tx
      .update(projects)
      .set({ knowledgeNodeId: node.id, updatedAt: new Date() })
      .where(eq(projects.id, lockedProjectId));

    await tx
      .insert(knowledgeNodeSupertags)
      .values({ nodeId: node.id, supertagId: projectInformationTag.id, createdBy: options.userId })
      .onConflictDoNothing();
    await upsertKnowledgeSearchIndex(tx, node, node.title);
    if (created) await appendKnowledgeRevision(tx, node, options.userId, "案件情報Docs正本を作成");
    return node;
  };
  return options.client ? run(options.client) : db.transaction(run);
}

export async function ensureProjectMeetingSection(options: {
  docsLibraryId: string;
  userId: string;
  projectId: string;
  projectNode: typeof knowledgeNodes.$inferSelect;
  client?: DocsWriteClient;
}) {
  const run = async (tx: DocsWriteClient) => {
    await lockProjectMeetingAdvisory(tx, options.docsLibraryId, options.projectId);
    const [lockedProject] = await tx
      .select({
        id: projects.id,
        ownerId: projects.ownerId,
        isCompleted: projects.isCompleted,
        deletedAt: projects.deletedAt,
      })
      .from(projects)
      .where(eq(projects.id, options.projectId))
      .limit(1)
      .for("update");
    if (!lockedProject) throw new Error("Projectが見つかりません");
    if (lockedProject.deletedAt || lockedProject.isCompleted) {
      throw new Error("完了/削除済みProjectの会議メモDocsは作成できません");
    }
    await assertProjectWriteAccessInTransaction(tx, lockedProject, options.userId);
    const [existing] = await tx
      .select()
      .from(knowledgeNodes)
      .where(
        and(
          eq(knowledgeNodes.docsLibraryId, options.docsLibraryId),
          eq(knowledgeNodes.projectId, options.projectId),
          eq(knowledgeNodes.systemKey, `project_meeting_notes:${options.projectId}`),
        ),
      )
      .limit(1);
    if (existing) {
      const repaired = await updateDocsNode(tx, existing.id, {
        parentId: options.projectNode.id,
        rootPageId: options.projectNode.rootPageId ?? options.projectNode.id,
        archivedAt: null,
        updatedBy: options.userId,
        updatedAt: new Date(),
      });
      await upsertKnowledgeSearchIndex(tx, repaired, repaired.title);
      return repaired;
    }
    const created = await insertDocsNode(tx, {
      docsLibraryId: options.docsLibraryId,
      parentId: options.projectNode.id,
      rootPageId: options.projectNode.rootPageId ?? options.projectNode.id,
      projectId: options.projectId,
      systemKey: `project_meeting_notes:${options.projectId}`,
      title: "会議メモ",
      bodyJson: { format: "doc_block", block_type: "heading_2" },
      nodeType: "node",
      sortOrder: 0,
      createdBy: options.userId,
      updatedBy: options.userId,
    });
    await upsertKnowledgeSearchIndex(tx, created, created.title);
    return created;
  };
  return options.client ? run(options.client) : db.transaction(run);
}
