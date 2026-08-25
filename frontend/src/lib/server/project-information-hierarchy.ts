import { and, eq, isNull, or, sql } from "drizzle-orm";
import { db } from "@/db";
import {
  knowledgeNodes,
  knowledgeNodeSupertags,
  knowledgeSupertags,
  projects,
} from "@/db/schema";
import { docsLibraries } from "@/lib/server/docs-library-schema";
import { insertDocsNode, updateDocsNode, updateDocsNodesByIds } from "./docs-node-writer";
import { appendKnowledgeRevision, upsertKnowledgeSearchIndex } from "./knowledge-docs-utils";

const PROJECT_INFORMATION_ROOT_SYSTEM_KEY = "project_information_root";
const PROJECT_INFORMATION_TAG_SYSTEM_KEY = "project_info";

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

export async function ensureProjectInformationRoot(docsLibraryId: string, userId: string) {
  return db.transaction(async (tx) => {
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
          eq(knowledgeNodes.systemKey, PROJECT_INFORMATION_ROOT_SYSTEM_KEY),
        ),
      )
      .limit(1);

    if (existing) {
      const canonical =
        existing.parentId === null &&
        existing.rootPageId === existing.id &&
        existing.systemKey === PROJECT_INFORMATION_ROOT_SYSTEM_KEY &&
        existing.title === "案件情報" &&
        existing.archivedAt === null;
      if (!canonical && !isLibraryOwner) {
        throw new Error("案件情報hubの修復にはPersonal Docs Library所有者権限が必要です");
      }
      if (canonical) return existing;
      const repaired = await updateDocsNode(tx, existing.id, {
        title: "案件情報",
        parentId: null,
        rootPageId: existing.id,
        projectId: null,
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
  });
}

/**
 * Read-only lookup for the canonical project-information hierarchy.  It
 * intentionally returns a missing node when the owner's library has not been
 * bootstrapped yet; callers (especially the Project-information GET repair
 * boundary) can distinguish missing state from an unknown project and return
 * an explicit initialization conflict instead of a misleading 404.
 */
export async function getProjectInformationHierarchyNode(options: {
  project: ProjectRow;
  docsLibraryId?: string | null;
}) {
  // Deleted projects are not active hierarchy subjects even if a stale
  // reverse pointer still references a syntactically valid node.
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
        eq(knowledgeNodes.systemKey, PROJECT_INFORMATION_ROOT_SYSTEM_KEY),
        eq(knowledgeNodes.title, "案件情報"),
        isNull(knowledgeNodes.archivedAt),
        isNull(knowledgeNodes.parentId),
        eq(knowledgeNodes.rootPageId, knowledgeNodes.id),
      ),
    )
    .limit(1);
  if (!hub) return { library, hub: null, node: null };

  // Read resolution is intentionally pointer-only. A missing/stale
  // projects.knowledge_node_id must not be silently replaced by an
  // arbitrary candidate: Projects-list links stay null and the Project
  // information GET repair boundary decides whether a writer may repair it.
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
        eq(knowledgeNodes.systemKey, `project_information:${options.project.id}`),
        eq(knowledgeNodes.parentId, hub.id),
        eq(knowledgeNodes.rootPageId, hub.id),
        isNull(knowledgeNodes.archivedAt),
        eq(knowledgeSupertags.docsLibraryId, library.id),
        eq(knowledgeSupertags.systemKey, PROJECT_INFORMATION_TAG_SYSTEM_KEY),
      ),
    )
    .limit(1);
  const node = nodeRow?.node ?? null;
  return { library, hub, node: node ?? null };
}

export async function ensureProjectInformationHierarchyNode(options: {
  docsLibraryId?: string;
  userId: string;
  project: ProjectRow;
}) {
  if (isDefaultInboxProject(options.project)) {
    throw new Error("Inboxは案件情報Docsの保存先にできません。実案件を指定してください。");
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
  const hub = await ensureProjectInformationRoot(resolvedDocsLibrary.id, options.userId);
  return db.transaction(async (tx) => {
    await tx.execute(sql`select pg_advisory_xact_lock(hashtext(${`${resolvedDocsLibrary.id}:project-information:${options.project.id}`}))`);
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
          eq(knowledgeSupertags.systemKey, PROJECT_INFORMATION_TAG_SYSTEM_KEY),
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
    let node: typeof knowledgeNodes.$inferSelect | undefined;
    if (options.project.knowledgeNodeId) {
      const [row] = await tx
        .select()
        .from(knowledgeNodes)
        .innerJoin(knowledgeNodeSupertags, eq(knowledgeNodeSupertags.nodeId, knowledgeNodes.id))
        .where(
          and(
            eq(knowledgeNodes.id, options.project.knowledgeNodeId),
            eq(knowledgeNodes.docsLibraryId, resolvedDocsLibrary.id),
            eq(knowledgeNodes.parentId, hub.id),
            eq(knowledgeNodes.rootPageId, hub.id),
            eq(knowledgeNodes.projectId, options.project.id),
            eq(knowledgeNodes.systemKey, `project_information:${options.project.id}`),
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
            eq(knowledgeNodes.projectId, options.project.id),
            eq(knowledgeNodes.systemKey, `project_information:${options.project.id}`),
            isNull(knowledgeNodes.archivedAt),
            eq(knowledgeNodeSupertags.supertagId, projectInformationTag.id),
          ),
        )
        .limit(1);
      node = row?.knowledge_nodes;
    }

    const created = !node;
    if (!node) {
      node = await insertDocsNode(tx, {
        docsLibraryId: resolvedDocsLibrary.id,
        parentId: hub.id,
        rootPageId: hub.id,
        projectId: options.project.id,
        systemKey: `project_information:${options.project.id}`,
        title: options.project.name,
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
      const legacyTitle = `${options.project.name} 案件情報`;
      node = await updateDocsNode(tx, node.id, {
        parentId: hub.id,
        rootPageId: hub.id,
        projectId: options.project.id,
        systemKey: `project_information:${options.project.id}`,
        title: !node.title.trim() || node.title.trim() === legacyTitle ? options.project.name : node.title,
        updatedBy: options.userId,
        updatedAt: new Date(),
      });
      const descendants = await tx
        .select({ id: knowledgeNodes.id })
        .from(knowledgeNodes)
        .where(
          and(
        eq(knowledgeNodes.docsLibraryId, resolvedDocsLibrary.id),
            or(
              eq(knowledgeNodes.projectId, options.project.id),
              eq(knowledgeNodes.rootPageId, node.id),
              eq(knowledgeNodes.parentId, node.id),
            ),
          ),
        );
      await updateDocsNodesByIds(tx, descendants.map((descendant) => descendant.id), {
        rootPageId: hub.id,
        projectId: options.project.id,
        updatedBy: options.userId,
        updatedAt: new Date(),
      });
    }

    await tx
      .update(projects)
      .set({ knowledgeNodeId: node.id, updatedAt: new Date() })
      .where(eq(projects.id, options.project.id));

    await tx
      .insert(knowledgeNodeSupertags)
      .values({ nodeId: node.id, supertagId: projectInformationTag.id, createdBy: options.userId })
      .onConflictDoNothing();
    await upsertKnowledgeSearchIndex(tx, node, node.title);
    if (created) await appendKnowledgeRevision(tx, node, options.userId, "案件情報Docs正本を作成");
    return node;
  });
}

export async function ensureProjectMeetingSection(options: {
  docsLibraryId: string;
  userId: string;
  projectId: string;
  projectNode: typeof knowledgeNodes.$inferSelect;
}) {
  return db.transaction(async (tx) => {
    await tx.execute(sql`select pg_advisory_xact_lock(hashtext(${`${options.docsLibraryId}:project-meetings:${options.projectId}`}))`);
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
  });
}
