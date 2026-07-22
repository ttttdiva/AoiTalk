import { and, eq, isNull, or, sql } from "drizzle-orm";
import { db } from "@/db";
import {
  knowledgeNodes,
  knowledgeNodeSupertags,
  knowledgeSupertags,
  projects,
} from "@/db/schema";
import { insertDocsNode, updateDocsNode, updateDocsNodesByIds } from "./docs-node-writer";
import { appendKnowledgeRevision, upsertKnowledgeSearchIndex } from "./knowledge-docs-utils";

const PROJECT_INFORMATION_ROOT_SYSTEM_KEY = "project_information_root";
const PROJECT_INFORMATION_TAG_SYSTEM_KEY = "project_info";

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

export async function ensureProjectInformationRoot(workspaceId: string, userId: string) {
  return db.transaction(async (tx) => {
    await tx.execute(sql`select pg_advisory_xact_lock(hashtext(${`${workspaceId}:project-information-root`}))`);
    const [existing] = await tx
      .select()
      .from(knowledgeNodes)
      .where(
        and(
          eq(knowledgeNodes.workspaceId, workspaceId),
          eq(knowledgeNodes.systemKey, PROJECT_INFORMATION_ROOT_SYSTEM_KEY),
        ),
      )
      .limit(1);

    if (existing) {
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

    const created = await insertDocsNode(tx, {
      workspaceId,
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

export async function ensureProjectInformationHierarchyNode(options: {
  workspaceId: string;
  userId: string;
  project: ProjectRow;
}) {
  if (isDefaultInboxProject(options.project)) {
    throw new Error("Inboxは案件情報Docsの保存先にできません。実案件を指定してください。");
  }
  const hub = await ensureProjectInformationRoot(options.workspaceId, options.userId);
  return db.transaction(async (tx) => {
    await tx.execute(sql`select pg_advisory_xact_lock(hashtext(${`${options.workspaceId}:project-information:${options.project.id}`}))`);
    let node: typeof knowledgeNodes.$inferSelect | undefined;
    if (options.project.knowledgeNodeId) {
      [node] = await tx
        .select()
        .from(knowledgeNodes)
        .where(
          and(
            eq(knowledgeNodes.id, options.project.knowledgeNodeId),
            eq(knowledgeNodes.workspaceId, options.workspaceId),
            eq(knowledgeNodes.projectId, options.project.id),
            isNull(knowledgeNodes.archivedAt),
          ),
        )
        .limit(1);
    }
    if (!node) {
      [node] = await tx
        .select()
        .from(knowledgeNodes)
        .where(
          and(
            eq(knowledgeNodes.workspaceId, options.workspaceId),
            eq(knowledgeNodes.projectId, options.project.id),
            eq(knowledgeNodes.systemKey, `project_information:${options.project.id}`),
            isNull(knowledgeNodes.archivedAt),
          ),
        )
        .limit(1);
    }

    const created = !node;
    if (!node) {
      node = await insertDocsNode(tx, {
        workspaceId: options.workspaceId,
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
            eq(knowledgeNodes.workspaceId, options.workspaceId),
            eq(knowledgeNodes.projectId, options.project.id),
            or(eq(knowledgeNodes.rootPageId, node.id), eq(knowledgeNodes.parentId, node.id)),
          ),
        );
      await updateDocsNodesByIds(tx, descendants.map((descendant) => descendant.id), {
        rootPageId: hub.id,
        updatedBy: options.userId,
        updatedAt: new Date(),
      });
    }

    await tx
      .update(projects)
      .set({ knowledgeNodeId: node.id, updatedAt: new Date() })
      .where(eq(projects.id, options.project.id));

    const [projectInformationTag] = await tx
      .select({ id: knowledgeSupertags.id })
      .from(knowledgeSupertags)
      .where(
        and(
          eq(knowledgeSupertags.workspaceId, options.workspaceId),
          eq(knowledgeSupertags.systemKey, PROJECT_INFORMATION_TAG_SYSTEM_KEY),
        ),
      )
      .limit(1);
    if (projectInformationTag) {
      await tx
        .insert(knowledgeNodeSupertags)
        .values({ nodeId: node.id, supertagId: projectInformationTag.id, createdBy: options.userId })
        .onConflictDoNothing();
    }
    await upsertKnowledgeSearchIndex(tx, node, node.title);
    if (created) await appendKnowledgeRevision(tx, node, options.userId, "案件情報Docs正本を作成");
    return node;
  });
}

export async function ensureProjectMeetingSection(options: {
  workspaceId: string;
  userId: string;
  projectId: string;
  projectNode: typeof knowledgeNodes.$inferSelect;
}) {
  return db.transaction(async (tx) => {
    await tx.execute(sql`select pg_advisory_xact_lock(hashtext(${`${options.workspaceId}:project-meetings:${options.projectId}`}))`);
    const [existing] = await tx
      .select()
      .from(knowledgeNodes)
      .where(
        and(
          eq(knowledgeNodes.workspaceId, options.workspaceId),
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
      workspaceId: options.workspaceId,
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
