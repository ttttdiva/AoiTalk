import { and, asc, desc, eq, ilike, inArray, isNull, lt, or, sql } from "drizzle-orm";
import { existsSync, readdirSync, rmSync, statSync } from "node:fs";
import { basename, resolve, sep } from "node:path";
import { db } from "@/db";
import {
  knowledgeAiSuggestions,
  knowledgeAttachments,
  knowledgeEdges,
  knowledgeFieldValues,
  knowledgeFields,
  knowledgeImportItems,
  knowledgeImportJobs,
  knowledgeNodePlacements,
  knowledgeNodeShares,
  knowledgeSearchIndex,
  knowledgeSavedViews,
  knowledgeNodeSupertags,
  knowledgeSupertagFields,
  knowledgeNodes,
  knowledgeRevisions,
  knowledgeSupertags,
  projectMembers,
  projects,
  users,
} from "@/db/schema";
import { docsLibraries } from "@/lib/server/docs-library-schema";
import {
  DEFAULT_DOCS_SUPERTAGS,
  normalizeDocsFieldType,
  normalizeDocsNodeType,
} from "@/lib/docs-model";
import { collectKnowledgeDisplayDescendantIds } from "@/lib/docs-outline-graph";
import { extractDocsReferenceHints } from "@/lib/docs-references";
import {
  getAccessibleProject,
  getWritableProject,
} from "@/lib/server/project-access";
import { hasEffectiveProjectPermission } from "@/lib/server/project-permissions";
import { getReadableProjectIds } from "@/lib/server/task-route-utils";
import {
  decryptJsonValueIfNeeded,
  decryptTextIfNeeded,
  encryptJsonValue,
  encryptText,
} from "./field-crypto";
import { listDocsTaskSyntheticFieldValues } from "./docs-task-binding";
import {
  appendContentDeletionEvent,
  createDeletionBatchId,
} from "./content-deletion-events";
import { readDeletionRetentionDays } from "./deletion-retention";
import {
  decryptDocsNodeBodyJson,
  decryptDocsNodeBodyText,
  DOCS_NODE_TITLE_MAX,
  insertDocsNode,
  updateDocsNode,
} from "./docs-node-writer";

type SessionUser = {
  id: string;
  role?: string | null;
};

type DocsTransaction = Parameters<Parameters<typeof db.transaction>[0]>[0];
type DocsDb = typeof db | DocsTransaction;

const VALID_SUGGESTION_STATUSES = new Set([
  "proposed",
  "accepted",
  "rejected",
  "stale",
]);

const HOME_SYSTEM_KEY = "home";

const HOME_QUERY_DEFAULTS = {
  projects: {
    and: [
      { tag_system_key: "project_info" },
      { field: "Page Role", op: "=", value: "canonical" },
    ],
    limit: 30,
    sort: "updated_desc",
  },
  recent: { and: [], limit: 20, sort: "updated_desc" },
  tasks: {
    and: [
      { tag_system_key: "task" },
      { field: "task_status", op: "!=", value: "done" },
    ],
    include_virtual_tasks: true,
    limit: 30,
    sort: "updated_desc",
  },
} satisfies Record<string, Record<string, unknown>>;

// 既存Homeの補正対象を明示する。legacyQueryと完全一致する場合だけ新しいqueryへ置き換え、
// ユーザーが条件・ソート・limit等を編集したquery_jsonは意図的に変更しない。
const HOME_QUERY_MIGRATIONS = [
  {
    title: "案件一覧",
    legacyQuery: {
      and: [
        { tag_system_key: "project_info" },
        { field: "Page Role", op: "=", value: "canonical" },
      ],
      limit: 100,
      sort: "updated_desc",
    },
    nextQuery: HOME_QUERY_DEFAULTS.projects,
  },
  {
    title: "最近更新されたノード",
    legacyQuery: { and: [], limit: 20, sort: "updated_desc" },
    nextQuery: HOME_QUERY_DEFAULTS.recent,
  },
  {
    title: "未完了タスク",
    legacyQuery: {
      and: [
        { tag_system_key: "task" },
        { field: "task_status", op: "!=", value: "done" },
      ],
      include_virtual_tasks: true,
      limit: 100,
      sort: "updated_desc",
    },
    nextQuery: HOME_QUERY_DEFAULTS.tasks,
  },
] as const;

const HOME_TEMPLATE: Array<{
  title: string;
  blockType?: string;
  nodeType?: string;
  queryJson?: Record<string, unknown>;
}> = [
  {
    title: "案件",
    blockType: "heading_2",
  },
  {
    title: "案件一覧",
    nodeType: "search",
    queryJson: {
      ...HOME_QUERY_DEFAULTS.projects,
    },
  },
  {
    title: "最近更新",
    blockType: "heading_2",
  },
  {
    title: "最近更新されたノード",
    nodeType: "search",
    queryJson: { ...HOME_QUERY_DEFAULTS.recent },
  },
  {
    title: "タスク",
    blockType: "heading_2",
  },
  {
    title: "未完了タスク",
    nodeType: "search",
    queryJson: {
      ...HOME_QUERY_DEFAULTS.tasks,
    },
  },
];

const VALID_IMPORT_STATUSES = new Set([
  "proposed",
  "importing",
  "imported",
  "failed",
  "rejected",
]);

const NODE_BODY_TEXT_AAD = "knowledge_nodes.body_text";
const NODE_BODY_JSON_AAD = "knowledge_nodes.body_json";
const REVISION_BODY_TEXT_AAD = "knowledge_revisions.body_text";
const REVISION_BODY_JSON_AAD = "knowledge_revisions.body_json";
const DOCS_WORKSPACE_NAME = "Personal Docs";
const DOCS_WORKSPACE_DESCRIPTION = "AoiTalk DBを正本にするDocsワークスペース";
const DOCS_WORKSPACE_SETTINGS = {
  canonical_store: "postgresql",
  derived_index: "qdrant",
};

export function decryptNodeBodyText(value: string | null | undefined) {
  return decryptDocsNodeBodyText(value);
}

export function decryptNodeBodyJson(value: unknown): Record<string, unknown> {
  return decryptDocsNodeBodyJson(value);
}

export function cleanString(
  value: unknown,
  fallback = "",
  maxLength = 500,
): string {
  if (typeof value !== "string") return fallback;
  const trimmed = value.trim();
  if (!trimmed) return fallback;
  return trimmed.slice(0, maxLength);
}

export function cleanOptionalString(
  value: unknown,
  maxLength = 500,
): string | null {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  return trimmed ? trimmed.slice(0, maxLength) : null;
}

export function normalizeJsonObject(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  return { ...(value as Record<string, unknown>) };
}

function stableJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.entries(value as Record<string, unknown>)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, item]) => `${JSON.stringify(key)}:${stableJson(item)}`)
      .join(",")}}`;
  }
  return JSON.stringify(value) ?? "null";
}

function jsonObjectsEqual(left: unknown, right: unknown) {
  return stableJson(left) === stableJson(right);
}

export function deriveKnowledgeBlockTitle(bodyText: string): string {
  const firstLine = String(bodyText ?? "")
    .replace(/\r\n?/g, "\n")
    .split("\n")
    .find((line) => Boolean(line.trim()));
  return (firstLine ?? "").slice(0, DOCS_NODE_TITLE_MAX);
}

function serializeDate(value: unknown): string | null {
  if (!value) return null;
  if (value instanceof Date) return value.toISOString();
  if (typeof value === "string") return value;
  return null;
}

function normalizeStatus(
  value: unknown,
  allowed: Set<string>,
  fallback: string,
): string {
  return typeof value === "string" && allowed.has(value) ? value : fallback;
}

export function serializeWorkspace(
  row: typeof docsLibraries.$inferSelect,
) {
  return {
    id: row.id,
    library_id: row.id,
    docs_library_id: row.id,
    name: row.name,
    description: row.description,
    owner_user_id: row.ownerUserId,
    library_type: row.libraryType ?? "personal",
    settings: row.settingsJson ?? {},
    created_at: serializeDate(row.createdAt),
    updated_at: serializeDate(row.updatedAt),
  };
}

/** Stable API DTO name for the physical workspace row. */
export const serializeDocsLibrary = serializeWorkspace;

function serializeNodeWithOptions(
  row: typeof knowledgeNodes.$inferSelect,
  options: { includeBody: boolean },
) {
  const includeBody = options.includeBody;
  return {
    id: row.id,
    docs_library_id: row.docsLibraryId,
    parent_id: row.parentId,
    root_page_id: row.rootPageId,
    project_id: row.projectId,
    system_key: row.systemKey,
    title: row.title,
    aliases: Array.isArray(row.aliases) ? row.aliases.filter((item): item is string => typeof item === "string") : [],
    description: row.description ?? "",
    body_json: includeBody ? decryptNodeBodyJson(row.bodyJson ?? {}) : {},
    body_text: includeBody ? decryptNodeBodyText(row.bodyText ?? "") : "",
    node_type: normalizeDocsNodeType(row.nodeType),
    display_props: normalizeJsonObject(row.displayProps ?? {}),
    query_json: normalizeDocsNodeType(row.nodeType) === "search" && row.queryJson && typeof row.queryJson === "object" && !Array.isArray(row.queryJson)
      ? { ...(row.queryJson as Record<string, unknown>) }
      : null,
    view_json: normalizeJsonObject(row.viewJson ?? {}),
    day_date: serializeDate(row.dayDate),
    sort_order: row.sortOrder ?? 0,
    created_by: row.createdBy,
    updated_by: row.updatedBy,
    created_at: serializeDate(row.createdAt),
    updated_at: serializeDate(row.updatedAt),
    archived_at: serializeDate(row.archivedAt),
  };
}

export function serializeSupertag(
  row: typeof knowledgeSupertags.$inferSelect,
) {
  return {
    id: row.id,
    docs_library_id: row.docsLibraryId,
    parent_supertag_id: row.parentSupertagId,
    name: row.name,
    base_type: row.baseType ?? "note",
    description: row.description,
    icon: row.icon,
    color: row.color,
    system_key: row.systemKey,
    template_json: row.templateJson ?? {},
    pinned_field_ids: Array.isArray(row.pinnedFieldIds) ? row.pinnedFieldIds : [],
    config_json: normalizeJsonObject(row.configJson ?? {}),
    title_template: row.titleTemplate,
    ai_instructions: row.aiInstructions,
    created_at: serializeDate(row.createdAt),
    updated_at: serializeDate(row.updatedAt),
  };
}

export function serializeField(row: typeof knowledgeFields.$inferSelect) {
  return {
    id: row.id,
    docs_library_id: row.docsLibraryId,
    supertag_id: row.supertagId,
    system_key: row.systemKey,
    name: row.name,
    field_type: normalizeDocsFieldType(row.fieldType),
    required: !!row.required,
    options_json: row.optionsJson ?? {},
    default_value_json: row.defaultValueJson,
    sort_order: row.sortOrder ?? 0,
    created_at: serializeDate(row.createdAt),
    updated_at: serializeDate(row.updatedAt),
  };
}

export function serializeFieldValue(
  row: typeof knowledgeFieldValues.$inferSelect,
) {
  return {
    node_id: row.nodeId,
    field_id: row.fieldId,
    value_json: row.valueJson,
    value_text: row.valueText,
    value_number: row.valueNumber,
    value_datetime: serializeDate(row.valueDatetime),
    target_node_id: row.targetNodeId,
    updated_at: serializeDate(row.updatedAt),
    updated_by: row.updatedBy,
  };
}

export function serializeNodeSupertag(
  row: typeof knowledgeNodeSupertags.$inferSelect,
) {
  return {
    node_id: row.nodeId,
    supertag_id: row.supertagId,
    created_at: serializeDate(row.createdAt),
    created_by: row.createdBy,
  };
}

export function serializeSuggestion(
  row: typeof knowledgeAiSuggestions.$inferSelect,
) {
  return {
    id: row.id,
    docs_library_id: row.docsLibraryId,
    node_id: row.nodeId,
    suggestion_type: row.suggestionType,
    payload_json: row.payloadJson ?? {},
    status: row.status ?? "proposed",
    confidence: row.confidence,
    created_by: row.createdBy,
    created_at: serializeDate(row.createdAt),
    updated_at: serializeDate(row.updatedAt),
  };
}

export function serializeImportJob(
  row: typeof knowledgeImportJobs.$inferSelect,
) {
  return {
    id: row.id,
    docs_library_id: row.docsLibraryId,
    project_id: row.projectId,
    source_type: row.sourceType,
    source_name: row.sourceName,
    status: row.status ?? "proposed",
    options_json: row.optionsJson ?? {},
    summary_json: row.summaryJson ?? {},
    created_by: row.createdBy,
    created_at: serializeDate(row.createdAt),
    updated_at: serializeDate(row.updatedAt),
  };
}

export function serializeImportItem(
  row: typeof knowledgeImportItems.$inferSelect,
) {
  return {
    id: row.id,
    job_id: row.jobId,
    node_id: row.nodeId,
    source_ref: row.sourceRef,
    title: row.title,
    item_type: row.itemType ?? "page",
    status: row.status ?? "proposed",
    preview_json: row.previewJson ?? {},
    error_message: row.errorMessage,
    created_at: serializeDate(row.createdAt),
  };
}

export function serializeView(row: typeof knowledgeSavedViews.$inferSelect) {
  return {
    id: row.id,
    docs_library_id: row.docsLibraryId,
    supertag_id: row.supertagId,
    name: row.name,
    layout: row.layout ?? "table",
    config_json: row.configJson ?? {},
    sort_order: row.sortOrder ?? 0,
    created_by: row.createdBy,
    created_at: serializeDate(row.createdAt),
    updated_at: serializeDate(row.updatedAt),
  };
}

export function serializeSupertagField(
  row: typeof knowledgeSupertagFields.$inferSelect,
) {
  return {
    supertag_id: row.supertagId,
    field_id: row.fieldId,
    sort_order: row.sortOrder ?? 0,
    required: !!row.required,
    show_in_template: row.showInTemplate !== false,
    optional: !!row.optional,
    created_at: serializeDate(row.createdAt),
  };
}

export function serializeNodePlacement(
  row: typeof knowledgeNodePlacements.$inferSelect,
) {
  return {
    id: row.id,
    node_id: row.nodeId,
    parent_node_id: row.parentNodeId,
    sort_order: row.sortOrder ?? 0,
    collapsed: !!row.collapsed,
    created_by: row.createdBy,
    created_at: serializeDate(row.createdAt),
  };
}

export function serializeAttachment(
  row: typeof knowledgeAttachments.$inferSelect,
) {
  return {
    id: row.id,
    node_id: row.nodeId,
    file_name: row.fileName,
    file_path: row.filePath,
    mime_type: row.mimeType,
    size_bytes: row.sizeBytes,
    metadata: row.attachmentMetadata ?? {},
    created_by: row.createdBy,
    created_at: serializeDate(row.createdAt),
  };
}

export function serializeEdge(row: typeof knowledgeEdges.$inferSelect) {
  return {
    id: row.id,
    source_node_id: row.sourceNodeId,
    target_node_id: row.targetNodeId,
    relation_type: row.relationType ?? "related_to",
    confidence: row.confidence ?? 1,
    created_by: row.createdBy,
    created_at: serializeDate(row.createdAt),
  };
}

export async function getKnowledgeNodeDescendantIds(
  client: DocsDb,
  docsLibraryId: string,
  nodeId: string,
): Promise<string[]> {
  const rows = await client
    .select({
      id: knowledgeNodes.id,
      parentId: knowledgeNodes.parentId,
    })
    .from(knowledgeNodes)
    .where(eq(knowledgeNodes.docsLibraryId, docsLibraryId));
  const childrenByParent = new Map<string, string[]>();
  for (const row of rows) {
    if (!row.parentId) continue;
    const children = childrenByParent.get(row.parentId) ?? [];
    children.push(row.id);
    childrenByParent.set(row.parentId, children);
  }

  const descendants: string[] = [];
  const queue = [...(childrenByParent.get(nodeId) ?? [])];
  const seen = new Set<string>();
  while (queue.length > 0) {
    const current = queue.shift();
    if (!current || seen.has(current)) continue;
    seen.add(current);
    descendants.push(current);
    queue.push(...(childrenByParent.get(current) ?? []));
  }
  return descendants;
}

export async function getKnowledgeDisplayDescendantIds(
  client: DocsDb,
  docsLibraryId: string,
  nodeId: string,
): Promise<string[]> {
  const [nodes, placements] = await Promise.all([
    client
      .select({
        id: knowledgeNodes.id,
        parentId: knowledgeNodes.parentId,
      })
      .from(knowledgeNodes)
      .where(eq(knowledgeNodes.docsLibraryId, docsLibraryId)),
    client
      .select({
        nodeId: knowledgeNodePlacements.nodeId,
        parentNodeId: knowledgeNodePlacements.parentNodeId,
      })
      .from(knowledgeNodePlacements)
      .innerJoin(knowledgeNodes, eq(knowledgeNodePlacements.nodeId, knowledgeNodes.id))
      .where(eq(knowledgeNodes.docsLibraryId, docsLibraryId)),
  ]);
  return collectKnowledgeDisplayDescendantIds(nodes, placements, nodeId);
}

async function resolveReferenceTargetIds(
  client: DocsDb,
  docsLibraryId: string,
  sourceNodeId: string,
  bodyText: string,
): Promise<string[]> {
  const hints = extractDocsReferenceHints(bodyText);
  const targetIds = new Set<string>();

  if (hints.docsIds.length > 0) {
    const rows = await client
      .select({ id: knowledgeNodes.id })
      .from(knowledgeNodes)
      .where(
        and(
          eq(knowledgeNodes.docsLibraryId, docsLibraryId),
          isNull(knowledgeNodes.archivedAt),
          inArray(knowledgeNodes.id, hints.docsIds),
        ),
      );
    for (const row of rows) {
      if (row.id !== sourceNodeId) targetIds.add(row.id);
    }
  }

  return Array.from(targetIds);
}

export async function syncKnowledgeNodeReferenceEdges(
  client: DocsDb,
  node: typeof knowledgeNodes.$inferSelect,
  userId: string,
) {
  await client
    .delete(knowledgeEdges)
    .where(
      and(
        eq(knowledgeEdges.sourceNodeId, node.id),
        inArray(knowledgeEdges.relationType, ["inline_ref", "references"]),
      ),
    );

  const bodyText = decryptNodeBodyText(node.bodyText ?? "");
  const referenceText = [node.title, bodyText].filter(Boolean).join("\n");
  const targetIds = await resolveReferenceTargetIds(
    client,
    node.docsLibraryId,
    node.id,
    referenceText,
  );
  if (targetIds.length === 0) return;

  await client.insert(knowledgeEdges).values(
    targetIds.map((targetNodeId) => ({
      sourceNodeId: node.id,
      targetNodeId,
      relationType: "inline_ref",
      confidence: 1,
      createdBy: userId,
    })),
  );
}

async function seedDefaultDocsWorkspace(
  tx: DocsDb,
  docsLibraryId: string,
  userId: string,
) {
  await tx.execute(
    sql`select pg_advisory_xact_lock(hashtext(${`${docsLibraryId}:default-docs-seed`}))`,
  );
  const existingTags = await tx
    .select()
    .from(knowledgeSupertags)
    .where(eq(knowledgeSupertags.docsLibraryId, docsLibraryId));
  const tagsByName = new Map(existingTags.map((tag) => [tag.name, tag]));
  const tagsBySystemKey = new Map(
    existingTags
      .filter((tag) => tag.systemKey)
      .map((tag) => [tag.systemKey as string, tag]),
  );

  for (const tag of DEFAULT_DOCS_SUPERTAGS) {
    let targetTag = tag.systemKey ? tagsBySystemKey.get(tag.systemKey) : undefined;
    targetTag = targetTag ?? tagsByName.get(tag.name);
    if (!targetTag) {
      const [createdTag] = await tx
        .insert(knowledgeSupertags)
        .values({
          docsLibraryId,
          parentSupertagId: null,
          systemKey: tag.systemKey ?? null,
          name: tag.name,
          baseType: tag.baseType,
          description: tag.description,
          icon: tag.icon,
          color: tag.color,
          templateJson: tag.templateJson,
          pinnedFieldIds: tag.pinnedFieldKeys,
          configJson: tag.configJson ?? {},
          aiInstructions: tag.aiInstructions,
        })
        .returning();
      targetTag = createdTag;
      tagsByName.set(tag.name, createdTag);
      if (createdTag.systemKey) tagsBySystemKey.set(createdTag.systemKey, createdTag);
    }

    if (tag.fields.length > 0) {
      const existingFields = await tx
        .select({ id: knowledgeFields.id, name: knowledgeFields.name, systemKey: knowledgeFields.systemKey })
        .from(knowledgeFields)
        .where(eq(knowledgeFields.supertagId, targetTag.id));
      const existingFieldNames = new Set(existingFields.map((field) => field.name));
      const existingFieldKeys = new Set(existingFields.map((field) => field.systemKey).filter(Boolean));
      const missingFields = tag.fields.filter(
        (field) => !existingFieldNames.has(field.name) && !(field.systemKey && existingFieldKeys.has(field.systemKey)),
      );
      if (missingFields.length > 0) {
        await tx.insert(knowledgeFields).values(
          missingFields.map((field, fieldIndex) => ({
            docsLibraryId,
            supertagId: targetTag.id,
            systemKey: field.systemKey ?? null,
            name: field.name,
            fieldType: field.fieldType,
            required: !!field.required,
            optionsJson: field.options ?? {},
            defaultValueJson: field.defaultValue ?? null,
            sortOrder: existingFields.length + fieldIndex,
          })),
        );
      }
      for (const field of tag.fields) {
        const existing = existingFields.find((item) => item.name === field.name);
        if (existing && field.systemKey && existing.systemKey !== field.systemKey) {
          await tx
            .update(knowledgeFields)
            .set({ systemKey: field.systemKey, updatedAt: new Date() })
            .where(eq(knowledgeFields.id, existing.id));
        }
      }
      const refreshedFields = await tx
        .select({ id: knowledgeFields.id, name: knowledgeFields.name, systemKey: knowledgeFields.systemKey })
        .from(knowledgeFields)
        .where(eq(knowledgeFields.supertagId, targetTag.id));
      if (refreshedFields.length > 0) {
        await tx
          .insert(knowledgeSupertagFields)
          .values(
            refreshedFields.map((field, index) => ({
              supertagId: targetTag.id,
              fieldId: field.id,
              sortOrder: index,
              required: !!tag.fields.find((item) => item.name === field.name)?.required,
              showInTemplate: true,
              optional: false,
            })),
          )
          .onConflictDoNothing();
      }
      const pinnedIds = tag.pinnedFieldKeys
        .map((name) => refreshedFields.find((field) => field.name === name)?.id)
        .filter((id): id is string => !!id);
      await tx
        .update(knowledgeSupertags)
        .set({
          baseType: tag.baseType,
          systemKey: tag.systemKey ?? targetTag.systemKey ?? null,
          description: tag.description,
          icon: tag.icon,
          color: tag.color,
          templateJson: tag.templateJson,
          pinnedFieldIds: pinnedIds,
          // 既存 config_json を温存し、seed が宣言したキー(tools 等)だけ浅くマージ上書きする。
          // backend(docs_workspace.py)の {**current_config, "tools": ...} と挙動を揃える。
          ...(tag.configJson
            ? { configJson: { ...normalizeJsonObject(targetTag.configJson ?? {}), ...tag.configJson } }
            : {}),
          aiInstructions: tag.aiInstructions,
          updatedAt: new Date(),
        })
        .where(eq(knowledgeSupertags.id, targetTag.id));
    }
  }

  const projectInformationTag = tagsBySystemKey.get("project_info");
  if (projectInformationTag) {
    const [pageRoleField] = await tx
      .select()
      .from(knowledgeFields)
      .where(
        and(
          eq(knowledgeFields.supertagId, projectInformationTag.id),
          eq(knowledgeFields.name, "Page Role"),
        ),
      )
      .limit(1);
    if (pageRoleField) {
      const canonicalNodes = await tx
        .select({ nodeId: knowledgeNodes.id })
        .from(projects)
        .innerJoin(
          knowledgeNodes,
          eq(projects.knowledgeNodeId, knowledgeNodes.id),
        )
        .innerJoin(
          knowledgeNodeSupertags,
          and(
            eq(knowledgeNodeSupertags.nodeId, knowledgeNodes.id),
            eq(knowledgeNodeSupertags.supertagId, projectInformationTag.id),
          ),
        )
        .where(
          and(
            eq(knowledgeNodes.docsLibraryId, docsLibraryId),
            isNull(knowledgeNodes.archivedAt),
            isNull(projects.deletedAt),
          ),
        );
      if (canonicalNodes.length > 0) {
        const canonicalNodeIds = canonicalNodes.map((row) => row.nodeId);
        const existingValues = await tx
          .select({ nodeId: knowledgeFieldValues.nodeId })
          .from(knowledgeFieldValues)
          .where(
            and(
              eq(knowledgeFieldValues.fieldId, pageRoleField.id),
              inArray(knowledgeFieldValues.nodeId, canonicalNodeIds),
            ),
          );
        const existingNodeIds = new Set(existingValues.map((row) => row.nodeId));
        const missingNodeIds = canonicalNodeIds.filter(
          (nodeId) => !existingNodeIds.has(nodeId),
        );
        if (missingNodeIds.length > 0) {
          await tx
            .insert(knowledgeFieldValues)
            .values(
              missingNodeIds.map((nodeId) => ({
                ...normalizeFieldValueInput(pageRoleField, "canonical"),
                nodeId,
                updatedBy: userId,
              })),
            )
            .onConflictDoNothing();
        }
      }
    }
  }

  const existingViews = await tx
    .select({ name: knowledgeSavedViews.name })
    .from(knowledgeSavedViews)
    .where(eq(knowledgeSavedViews.docsLibraryId, docsLibraryId));
  const existingViewNames = new Set(existingViews.map((view) => view.name));
  const missingViews: Array<typeof knowledgeSavedViews.$inferInsert> = [];
  if (!existingViewNames.has("全案件 Task board")) {
    missingViews.push({
      docsLibraryId,
      name: "全案件 Task board",
      layout: "board",
      configJson: {
        filters: { supertag: "Task" },
        group_by: "状態",
        columns: ["状態", "期日", "担当", "案件"],
      },
      createdBy: userId,
      sortOrder: 1,
    });
  }
  // 「全案件 Risk table」ビューは Risk タグ削除(D11)に伴い dead 定義のため seed から除去した。
  // 既存DBに残る同名ビュー行は過去のデータ移行で掃除済み。
  if (!existingViewNames.has("今月の Meeting list")) {
    missingViews.push({
      docsLibraryId,
      name: "今月の Meeting list",
      layout: "list",
      configJson: { filters: { supertag: "Meeting", date: "this_month" } },
      createdBy: userId,
      sortOrder: 3,
    });
  }

  if (missingViews.length > 0) {
    await tx.insert(knowledgeSavedViews).values(missingViews);
  }

  const [keyedHome] = await tx
    .select({
      id: knowledgeNodes.id,
      archivedAt: knowledgeNodes.archivedAt,
    })
    .from(knowledgeNodes)
    .where(
      and(
        eq(knowledgeNodes.docsLibraryId, docsLibraryId),
        eq(knowledgeNodes.systemKey, HOME_SYSTEM_KEY),
      ),
    )
    .limit(1);
  let existingHome = keyedHome?.archivedAt ? undefined : keyedHome;
  // Always count active root Homes, even when the canonical `home` key is
  // already present. Otherwise a later duplicate could remain silently.
  const activeRootHomes = await tx
    .select({ id: knowledgeNodes.id })
    .from(knowledgeNodes)
    .where(
      and(
        eq(knowledgeNodes.docsLibraryId, docsLibraryId),
        eq(knowledgeNodes.title, "Home"),
        isNull(knowledgeNodes.parentId),
        isNull(knowledgeNodes.archivedAt),
      ),
    )
    .limit(2);

  if (activeRootHomes.length > 1) {
    throw new Error("Docs workspace has multiple active Home roots");
  }

  if (!existingHome) {
    const [activeRootHome] = activeRootHomes;
    if (activeRootHome) {
      if (keyedHome && keyedHome.id !== activeRootHome.id) {
        await updateDocsNode(tx, keyedHome.id, {
          systemKey: null,
          updatedBy: userId,
          updatedAt: new Date(),
        });
      }
      const adoptedHome = await updateDocsNode(tx, activeRootHome.id, {
        systemKey: HOME_SYSTEM_KEY,
        updatedBy: userId,
        updatedAt: new Date(),
      });
      existingHome = adoptedHome ?? activeRootHome;
    } else if (keyedHome) {
      await updateDocsNode(tx, keyedHome.id, {
        systemKey: null,
        updatedBy: userId,
        updatedAt: new Date(),
      });
    }
  }
  if (existingHome) {
    const existingSearchNodes = await tx
      .select({
        id: knowledgeNodes.id,
        title: knowledgeNodes.title,
        queryJson: knowledgeNodes.queryJson,
      })
      .from(knowledgeNodes)
      .where(
        and(
          eq(knowledgeNodes.docsLibraryId, docsLibraryId),
          eq(knowledgeNodes.parentId, existingHome.id),
          eq(knowledgeNodes.nodeType, "search"),
          isNull(knowledgeNodes.archivedAt),
        ),
      );
    for (const migration of HOME_QUERY_MIGRATIONS) {
      const node = existingSearchNodes.find((item) => item.title === migration.title);
      // 旧テンプレートのデフォルト値と完全一致するときだけ補正する。ユーザー編集値は保持する。
      if (!node || !jsonObjectsEqual(node.queryJson, migration.legacyQuery)) continue;
      const updated = await updateDocsNode(tx, node.id, {
        queryJson: migration.nextQuery,
        updatedBy: userId,
        updatedAt: new Date(),
      });
      if (updated) await appendKnowledgeRevision(tx, updated, userId, "Home検索クエリの既定limitを補正");
    }
    return;
  }

  const homeId = crypto.randomUUID();
  const home = await insertDocsNode(tx, {
    id: homeId,
    docsLibraryId,
    parentId: null,
    rootPageId: homeId,
    projectId: null,
    systemKey: HOME_SYSTEM_KEY,
    title: "Home",
    nodeType: "node",
    bodyJson: { format: "doc_block", block_type: "paragraph" },
    displayProps: {},
    queryJson: null,
    viewJson: {},
    sortOrder: 0,
    createdBy: userId,
    updatedBy: userId,
  });
  await upsertKnowledgeSearchIndex(tx, home, effectiveDocsSearchBodyText(home));
  await appendKnowledgeRevision(tx, home, userId, "Homeノードを作成");
  for (const [index, templateNode] of HOME_TEMPLATE.entries()) {
    const child = await insertDocsNode(tx, {
      docsLibraryId,
      parentId: homeId,
      rootPageId: homeId,
      projectId: null,
      systemKey: null,
      title: templateNode.title,
      nodeType: templateNode.nodeType ?? "node",
      bodyJson: {
        format: "doc_block",
        block_type: templateNode.blockType ?? "paragraph",
      },
      displayProps: {},
      queryJson: templateNode.queryJson ?? null,
      viewJson: {},
      sortOrder: index,
      createdBy: userId,
      updatedBy: userId,
    });
    await upsertKnowledgeSearchIndex(tx, child, effectiveDocsSearchBodyText(child));
    await appendKnowledgeRevision(tx, child, userId, "Home初期テンプレートを作成");
  }
}

export async function ensureDocsWorkspace(user: SessionUser) {
  const [existing] = await db
    .select()
    .from(docsLibraries)
    .where(
      and(
        eq(docsLibraries.ownerUserId, user.id),
        eq(docsLibraries.libraryType, "personal"),
      ),
    )
    .orderBy(asc(docsLibraries.createdAt))
    .limit(1);
  if (existing) {
    const settings = normalizeJsonObject(existing.settingsJson);
    const mergedSettings = { ...DOCS_WORKSPACE_SETTINGS, ...settings };
    const needsUpdate =
      existing.name !== DOCS_WORKSPACE_NAME ||
      !existing.description ||
      JSON.stringify(mergedSettings) !== JSON.stringify(settings);
    const workspace = needsUpdate
      ? (
          await db
            .update(docsLibraries)
            .set({
              name: DOCS_WORKSPACE_NAME,
              description: existing.description || DOCS_WORKSPACE_DESCRIPTION,
              settingsJson: mergedSettings,
            })
            .where(eq(docsLibraries.id, existing.id))
            .returning()
        )[0] ?? existing
      : existing;
    await db.transaction(async (tx) => {
      await seedDefaultDocsWorkspace(tx, workspace.id, user.id);
    });
    return workspace;
  }

  return await db.transaction(async (tx) => {
    const [workspace] = await tx
      .insert(docsLibraries)
      .values({
        name: DOCS_WORKSPACE_NAME,
        description: DOCS_WORKSPACE_DESCRIPTION,
        ownerUserId: user.id,
        settingsJson: DOCS_WORKSPACE_SETTINGS,
      })
      // The canonical schema uses a partial unique index (personal owners
      // only), so `ON CONFLICT (owner_user_id)` cannot be inferred on every
      // supported PostgreSQL version. Conflict-do-nothing followed by a
      // deterministic select is safe under the unique index and also works
      // during the workspace→DocsLibrary rename migration.
      .onConflictDoNothing()
      .returning();
    const canonical =
      workspace ??
      (
        await tx
          .select()
          .from(docsLibraries)
          .where(
            and(
              eq(docsLibraries.ownerUserId, user.id),
              eq(docsLibraries.libraryType, "personal"),
            ),
          )
          .orderBy(asc(docsLibraries.createdAt), asc(docsLibraries.id))
          .limit(1)
      )[0];
    if (!canonical) throw new Error("Personal Docs Library could not be created");
    await seedDefaultDocsWorkspace(tx, canonical.id, user.id);
    return canonical;
  });
}

/**
 * Return the canonical membership-scoped Docs Library for a project.
 *
 * Project information is owned by the project's owner and is stored under
 * that user's Personal Docs Library.  The old project-scoped workspace rows
 * remain readable for migration compatibility, but this resolver deliberately
 * never creates or returns one.  The public function name is retained while
 * clients migrate from `workspace`/`workspace_id` to `library`/`docs_library_id`.
 */
export async function ensureProjectDocsWorkspace(
  projectId: string,
  user: SessionUser,
) {
  const access = await ensureProjectReadable(projectId, user);
  if (!access) return null;
  const ownerId = access.project.ownerId;
  const writable = await ensureProjectWritable(projectId, user);

  const existing = await db
    .select()
    .from(docsLibraries)
    .where(
      and(
        eq(docsLibraries.libraryType, "personal"),
        eq(docsLibraries.ownerUserId, ownerId),
      ),
    )
    .limit(1);
  if (existing[0]) {
    // The Personal Library's default metadata (Home, system supertags,
    // fields, views, etc.) is owner-private.  A Project writer/member may
    // use an already initialized library and canonical project child, but
    // must never reseed or repair the owner's global metadata as a side
    // effect of a task Docs-node/meeting-note request.
    if (writable && user.id === ownerId) {
      await db.transaction(async (tx) => {
        await seedDefaultDocsWorkspace(tx, existing[0].id, user.id);
      });
    }
    return existing[0];
  }

  // A read-only caller, or a Project member whose owner library has not been
  // initialized yet, gets a deterministic empty/missing result.  Only the
  // Personal Library owner may bootstrap the missing owner-owned library.
  if (!writable || user.id !== ownerId) return null;

  // Use the same idempotent personal bootstrap path as `/docs`.
  return ensureDocsWorkspace({ id: ownerId, role: ownerId === user.id ? user.role : null });
}

export async function ensureProjectReadable(
  projectId: string | null | undefined,
  user: SessionUser,
) {
  if (!projectId) return null;
  return await getAccessibleProject(projectId, user.id);
}

export async function ensureProjectWritable(
  projectId: string | null | undefined,
  user: SessionUser,
) {
  if (!projectId) return null;
  return await getWritableProject(projectId, user);
}

export type DocsNodeAccess = {
  permission: "owner" | "read" | "write";
  node: typeof knowledgeNodes.$inferSelect;
  workspace: typeof docsLibraries.$inferSelect;
};

export type DocsNodeAccessMap = Map<string, DocsNodeAccess>;

const DOCS_ACL_MAX_ANCESTOR_DEPTH = 512;
const DOCS_ACL_BIND_CHUNK_SIZE = 5000;

function chunkDocsAclIds<T>(items: readonly T[]): T[][] {
  const chunks: T[][] = [];
  for (let offset = 0; offset < items.length; offset += DOCS_ACL_BIND_CHUNK_SIZE) {
    chunks.push(items.slice(offset, offset + DOCS_ACL_BIND_CHUNK_SIZE));
  }
  return chunks;
}

type ProjectPermissionSets = {
  readable: Set<string>;
  writable: Set<string>;
};

/**
 * Resolve project ACLs for a whole candidate set in two bounded queries.
 *
 * Calling getAccessibleProject/getWritableProject for every candidate node
 * used to multiply the project/member lookup by the number of roots.  The
 * ACL decision itself is unchanged; only the lookup is materialized once.
 */
async function getProjectPermissionSets(
  projectIds: string[],
  user: SessionUser,
): Promise<ProjectPermissionSets> {
  const readable = new Set<string>();
  const writable = new Set<string>();
  const uniqueProjectIds = Array.from(new Set(projectIds.filter(Boolean)));
  if (uniqueProjectIds.length === 0) return { readable, writable };

  try {
    const [[principal], projectRows] = await Promise.all([
      db
        .select({ role: users.role })
        .from(users)
        .where(eq(users.id, user.id))
        .limit(1),
      Promise.all(
        chunkDocsAclIds(uniqueProjectIds).map((projectIdChunk) =>
          db
            .select({
              id: projects.id,
              ownerId: projects.ownerId,
              memberPermissions: projectMembers.permissions,
            })
            .from(projects)
            .leftJoin(
              projectMembers,
              and(
                eq(projectMembers.projectId, projects.id),
                eq(projectMembers.userId, user.id),
              ),
            )
            .where(
              and(
                isNull(projects.deletedAt),
                inArray(projects.id, projectIdChunk),
              ),
            ),
        ),
      ),
    ]);

    for (const row of projectRows.flat()) {
      const input = {
        userId: user.id,
        userRole: principal?.role ?? user.role,
        projectOwnerId: row.ownerId,
        memberPermissions: row.memberPermissions,
      };
      try {
        if (hasEffectiveProjectPermission({ ...input, permission: "read" })) {
          readable.add(row.id);
        }
        if (hasEffectiveProjectPermission({ ...input, permission: "write" })) {
          writable.add(row.id);
        }
      } catch {
        // Malformed persisted permissions fail closed for this project.
      }
    }
  } catch {
    // A rolling deploy or malformed project row must not turn a Docs read
    // into a 500.  Empty sets intentionally deny every project-gated node.
  }

  return { readable, writable };
}

/**
 * Resolve ACLs for a set of nodes with request-local/batch lookups.
 *
 * The returned map contains only nodes visible to the actor.  Callers must
 * treat a missing key as deny; this is important for race-safe serialization
 * when a share is revoked between the candidate query and response assembly.
 */
export async function getDocsNodeAccessMap(
  nodeIds: readonly string[],
  user: SessionUser,
  options: { includeArchived?: boolean } = {},
): Promise<DocsNodeAccessMap> {
  const ids = Array.from(new Set(nodeIds.filter(Boolean)));
  const accessByNodeId: DocsNodeAccessMap = new Map();
  if (ids.length === 0) return accessByNodeId;

  let rows: Array<{
    node: typeof knowledgeNodes.$inferSelect;
    workspace: typeof docsLibraries.$inferSelect;
  }>;
  try {
    const rowChunks = await Promise.all(
      chunkDocsAclIds(ids).map((idChunk) =>
        db
          .select({ node: knowledgeNodes, workspace: docsLibraries })
          .from(knowledgeNodes)
          .innerJoin(
            docsLibraries,
            eq(knowledgeNodes.docsLibraryId, docsLibraries.id),
          )
          .where(inArray(knowledgeNodes.id, idChunk)),
      ),
    );
    rows = rowChunks.flat();
  } catch {
    return accessByNodeId;
  }
  if (rows.length === 0) return accessByNodeId;

  // Normalize the joined rows once at the ACL boundary so every decision
  // below is made from docsLibraryId/libraryType and cannot accidentally grant
  // across a library.
  rows = (
    rows as Array<{
      node?: Record<string, unknown> | null;
      workspace?: Record<string, unknown> | null;
    }>
  ).flatMap((row) => {
    const rawNode = row.node;
    const rawWorkspace = row.workspace;
    const docsLibraryId = rawNode?.docsLibraryId;
    if (
      !rawNode ||
      !rawWorkspace ||
      typeof rawNode.id !== "string" ||
      typeof docsLibraryId !== "string" ||
      typeof rawWorkspace.id !== "string" ||
      docsLibraryId !== rawWorkspace.id
    ) {
      return [];
    }
    return [{
      node: {
        ...rawNode,
        docsLibraryId,
      } as typeof knowledgeNodes.$inferSelect,
      workspace: {
        ...rawWorkspace,
        libraryType: rawWorkspace.libraryType,
      } as typeof docsLibraries.$inferSelect,
    }];
  });

  const candidateRows = options.includeArchived === false
    ? rows.filter((row) => !row.node.archivedAt)
    : rows;
  if (candidateRows.length === 0) return accessByNodeId;

  // Resolve owner-controlled rows before touching any ancestor/child query.
  // A Personal Library owner does not need a share graph for ordinary nodes;
  // project-linked rows still require the project membership gate, so those
  // project ids are evaluated below without reading the entire library.
  const ownerProjectRows: typeof candidateRows = [];
  const candidateRowsForAcl: typeof candidateRows = [];
  for (const row of candidateRows) {
    if (row.workspace.ownerUserId !== user.id) {
      candidateRowsForAcl.push(row);
      continue;
    }
    if (row.node.systemKey === "project_information_root" || !row.node.projectId) {
      accessByNodeId.set(row.node.id, {
        node: row.node,
        workspace: row.workspace,
        permission: "owner",
      });
      continue;
    }
    ownerProjectRows.push(row);
  }

  const hubRows = candidateRowsForAcl.filter(
    (row) => row.node.systemKey === "project_information_root",
  );
  let hubChildRows: Array<{
    id: string;
    parentId: string | null;
    docsLibraryId: string;
    projectId: string | null;
    archivedAt: Date | null;
  }> = [];
  if (hubRows.length > 0) {
    try {
      const childChunks = await Promise.all(
        chunkDocsAclIds(hubRows.map((row) => row.node.id)).map((hubIdChunk) =>
          db
            .select({
              id: knowledgeNodes.id,
              parentId: knowledgeNodes.parentId,
              docsLibraryId: knowledgeNodes.docsLibraryId,
              projectId: knowledgeNodes.projectId,
              archivedAt: knowledgeNodes.archivedAt,
            })
            .from(knowledgeNodes)
            .where(inArray(knowledgeNodes.parentId, hubIdChunk)),
        ),
      );
      hubChildRows = childChunks.flat().filter((row) =>
        typeof row.id === "string" &&
        typeof row.docsLibraryId === "string" &&
        typeof row.parentId === "string",
      ) as typeof hubChildRows;
    } catch {
      // A malformed/rolling adapter simply denies member hub access below.
      hubChildRows = [];
    }
  }

  const projectIds = [
    ...ownerProjectRows.map((row) => row.node.projectId),
    ...candidateRowsForAcl.map((row) => row.node.projectId),
    ...hubChildRows.map((row) => row.projectId),
  ].filter((value): value is string => Boolean(value));
  const projectPermissions = await getProjectPermissionSets(projectIds, user);

  for (const row of ownerProjectRows) {
    if (!row.node.projectId || !projectPermissions.readable.has(row.node.projectId)) continue;
    accessByNodeId.set(row.node.id, {
      node: row.node,
      workspace: row.workspace,
      permission: "owner",
    });
  }

  const readableProjectChildByHub = new Set<string>();
  const workspaceByHubId = new Map(hubRows.map((row) => [row.node.id, row.workspace.id]));
  for (const child of hubChildRows) {
    const parentId = child.parentId;
    if (
      !child.archivedAt &&
      parentId &&
      child.projectId &&
      workspaceByHubId.get(parentId) === child.docsLibraryId &&
      projectPermissions.readable.has(child.projectId)
    ) {
      readableProjectChildByHub.add(parentId);
    }
  }

  const shareCandidates = candidateRowsForAcl.filter(
    (row) => row.node.systemKey !== "project_information_root" && !row.node.projectId,
  );
  const ancestorIdsByNodeId = await getDocsAncestorPaths(
    shareCandidates.map((row) => ({
      id: row.node.id,
      parentId: row.node.parentId,
      docsLibraryId: row.node.docsLibraryId,
    })),
  );
  const allAncestorIds = new Set<string>();
  for (const ancestorIds of ancestorIdsByNodeId.values()) {
    for (const ancestorId of ancestorIds) allAncestorIds.add(ancestorId);
  }

  const sharePermissionByNodeId = new Map<string, "read" | "write">();
  if (allAncestorIds.size > 0) {
    try {
      const shares = (
        await Promise.all(
          chunkDocsAclIds(Array.from(allAncestorIds)).map((ancestorChunk) =>
            db
              .select({
                nodeId: knowledgeNodeShares.nodeId,
                permission: knowledgeNodeShares.permission,
              })
              .from(knowledgeNodeShares)
              .where(
                and(
                  eq(knowledgeNodeShares.userId, user.id),
                  inArray(knowledgeNodeShares.nodeId, ancestorChunk),
                ),
              ),
          ),
        )
      ).flat();
      for (const share of shares) {
        if (share.permission === "read" || share.permission === "write") {
          sharePermissionByNodeId.set(share.nodeId, share.permission);
        }
      }
    } catch {
      // Unknown/malformed share storage fails closed.  Owner/project grants
      // below remain available without trusting an unreadable share table.
    }
  }

  for (const row of candidateRowsForAcl) {
    // Evaluate the Personal project-information hub before generic
    // `project_id` handling.  A malformed/stale hub carrying a project id
    // must never grant a member write access; only the library owner may
    // repair such metadata.
    if (row.node.systemKey === "project_information_root") {
      if (
        !row.node.projectId &&
        !row.node.parentId &&
        row.node.rootPageId === row.node.id &&
        readableProjectChildByHub.has(row.node.id)
      ) {
        accessByNodeId.set(row.node.id, {
          node: row.node,
          workspace: row.workspace,
          permission: "read",
        });
      }
      continue;
    }

    // Project identity is carried by the node.  Project libraries were
    // removed by the canonical Docs-library migration, so a missing
    // node.project_id is always a non-project personal node.
    const effectiveProjectId = row.node.projectId;
    const projectReadable = effectiveProjectId
      ? projectPermissions.readable.has(effectiveProjectId)
      : false;
    const projectWritable = effectiveProjectId
      ? projectPermissions.writable.has(effectiveProjectId)
      : false;

    // Project membership is an ACL gate for every node carrying a
    // `project_id`, including the unified personal-library hierarchy.  The
    // project owner receives owner/write access; members receive their
    // project permission directly (write or read) without a separate node
    // share; non-members are denied above. This keeps all personal project
    // roots on the same ACL contract.
    if (effectiveProjectId) {
      if (!projectReadable) continue;
      accessByNodeId.set(row.node.id, {
        node: row.node,
        workspace: row.workspace,
        permission: projectWritable ? "write" : "read",
      });
      continue;
    }

    const ancestorIds = ancestorIdsByNodeId.get(row.node.id) ?? [];
    const explicitPermission = ancestorIds
      .map((ancestorId) => sharePermissionByNodeId.get(ancestorId))
      .find(
        (candidate): candidate is "read" | "write" =>
          candidate === "read" || candidate === "write",
      );
    if (!explicitPermission) continue;
    accessByNodeId.set(row.node.id, {
      node: row.node,
      workspace: row.workspace,
      permission: explicitPermission === "write" ? "write" : "read",
    });
  }

  return accessByNodeId;
}

type DocsAclAncestorCandidate = {
  id: string;
  parentId: string | null;
  docsLibraryId: string;
};

/**
 * Resolve candidate ancestor paths without loading an entire Docs Library.
 * The normal path uses one recursive CTE; adapters without raw execution fall
 * back to level-by-level parent lookups deduplicated across candidates.  Each
 * returned row is checked against the child library before it is added to a
 * path, so a malformed cross-library edge cannot widen a share.
 */
async function getDocsAncestorPaths(
  candidates: readonly DocsAclAncestorCandidate[],
): Promise<Map<string, string[]>> {
  const paths = new Map<string, string[]>();
  const seenByCandidate = new Map<string, Set<string>>();
  const candidateById = new Map(candidates.map((candidate) => [candidate.id, candidate]));

  for (const candidate of candidates) {
    paths.set(candidate.id, [candidate.id]);
    seenByCandidate.set(candidate.id, new Set([candidate.id]));
  }
  if (candidates.length === 0) return paths;

  // Prefer one recursive query for all candidate paths.  The seed list is
  // bounded by the caller's candidate set, and each path is capped at 512
  // parent hops, so this never falls back to a library-wide scan.
  try {
    const seedIds = sql.join(
      candidates.map((candidate) => sql`${candidate.id}`),
      sql`, `,
    );
    const result = await db.execute(sql`
      WITH RECURSIVE ancestors AS (
        SELECT
          n.id,
          n.parent_id,
          n.docs_library_id,
          n.id AS candidate_id,
          ARRAY[n.id]::uuid[] AS visited_path,
          0 AS depth
        FROM knowledge_nodes AS n
        WHERE n.id IN (${seedIds})
        UNION ALL
        SELECT
          parent.id,
          parent.parent_id,
          parent.docs_library_id,
          child.candidate_id,
          child.visited_path || ARRAY[parent.id]::uuid[],
          child.depth + 1
        FROM knowledge_nodes AS parent
        INNER JOIN ancestors AS child ON child.parent_id = parent.id
        WHERE parent.docs_library_id = child.docs_library_id
          AND child.depth < ${DOCS_ACL_MAX_ANCESTOR_DEPTH}
          AND NOT parent.id = ANY(child.visited_path)
      )
      SELECT candidate_id, id, docs_library_id
      FROM ancestors
      ORDER BY candidate_id, depth ASC
    `);
    if (Array.isArray(result)) {
      const seedCandidates = new Set<string>();
      for (const item of result as Array<Record<string, unknown>>) {
        const candidateId = item.candidate_id ?? item.candidateId;
        const id = item.id;
        const docsLibraryId = item.docs_library_id ?? item.docsLibraryId;
        if (
          typeof candidateId !== "string" ||
          typeof id !== "string" ||
          typeof docsLibraryId !== "string"
        ) {
          continue;
        }
        const candidate = candidateById.get(candidateId);
        if (!candidate || candidate.docsLibraryId !== docsLibraryId) continue;
        if (id === candidateId) seedCandidates.add(candidateId);
        const seen = seenByCandidate.get(candidateId);
        if (!seen || seen.has(id)) continue;
        const path = paths.get(candidateId);
        if (!path || path.length >= DOCS_ACL_MAX_ANCESTOR_DEPTH + 1) continue;
        seen.add(id);
        path.push(id);
      }
      // A valid recursive result includes the seed row for every candidate.
      // If an adapter returned an unrecognizable shape, use the compatible
      // bounded fallback rather than silently dropping share access.
      if (seedCandidates.size >= candidates.length) return paths;
    }
  } catch {
    // Fall through to the bounded adapter-compatible lookup below.
  }

  let frontier = new Map<
    string,
    Array<{ candidateId: string; docsLibraryId: string }>
  >();
  for (const candidate of candidates) {
    if (!candidate.parentId) continue;
    const entries = frontier.get(candidate.parentId) ?? [];
    entries.push({ candidateId: candidate.id, docsLibraryId: candidate.docsLibraryId });
    frontier.set(candidate.parentId, entries);
  }

  for (
    let depth = 1;
    depth <= DOCS_ACL_MAX_ANCESTOR_DEPTH && frontier.size > 0;
    depth += 1
  ) {
    const parentIds = Array.from(frontier.keys());
    let rows: Array<{
      id: string;
      parentId: string | null;
      docsLibraryId: string;
    }>;
    try {
      const chunks = await Promise.all(
        chunkDocsAclIds(parentIds).map((idChunk) =>
          db
            .select({
              id: knowledgeNodes.id,
              parentId: knowledgeNodes.parentId,
              docsLibraryId: knowledgeNodes.docsLibraryId,
            })
            .from(knowledgeNodes)
            .where(inArray(knowledgeNodes.id, idChunk)),
        ),
      );
      rows = chunks.flat();
    } catch {
      break;
    }

    const nextFrontier = new Map<
      string,
      Array<{ candidateId: string; docsLibraryId: string }>
    >();
    for (const row of rows) {
      const matches = frontier.get(row.id) ?? [];
      for (const match of matches) {
        if (row.docsLibraryId !== match.docsLibraryId) continue;
        const seen = seenByCandidate.get(match.candidateId);
        if (!seen) continue;
        if (!seen.has(row.id)) {
          seen.add(row.id);
          paths.get(match.candidateId)?.push(row.id);
        }
        if (!row.parentId || depth >= DOCS_ACL_MAX_ANCESTOR_DEPTH) continue;
        if (seen.has(row.parentId)) continue;
        const entries = nextFrontier.get(row.parentId) ?? [];
        entries.push(match);
        nextFrontier.set(row.parentId, entries);
      }
    }
    frontier = nextFrontier;
  }

  return paths;
}

/**
 * Resolve personal subtree shares and project membership in one place.
 * Global admins do not implicitly gain access to another user's personal Docs.
 */
export async function getDocsNodeAccess(
  nodeId: string,
  user: SessionUser,
  accessMap?: DocsNodeAccessMap,
): Promise<DocsNodeAccess | null> {
  if (accessMap) return accessMap.get(nodeId) ?? null;

  // Keep the single-node path independent from the batch resolver.  Both paths
  // now use bounded ACL lookups, but this one can return owner/project grants
  // before constructing a batch closure for callers that need one node only.
  let row: {
    node: typeof knowledgeNodes.$inferSelect;
    workspace: typeof docsLibraries.$inferSelect;
  } | undefined;
  try {
    const rows = await db
      .select({ node: knowledgeNodes, workspace: docsLibraries })
      .from(knowledgeNodes)
      .innerJoin(
        docsLibraries,
        eq(knowledgeNodes.docsLibraryId, docsLibraries.id),
      )
      .where(eq(knowledgeNodes.id, nodeId))
      .limit(1);
    row = rows[0];
  } catch {
    return null;
  }
  if (!row) return null;

  // Do not trust an adapter row whose node/library ids disagree.  Besides
  // failing closed, this prevents a malformed cross-library parent from
  // widening a share below.
  const rawNode = row.node as unknown as Record<string, unknown> | null | undefined;
  const rawWorkspace = row.workspace as unknown as Record<string, unknown> | null | undefined;
  if (
    !rawNode ||
    !rawWorkspace ||
    typeof rawNode.id !== "string" ||
    typeof rawNode.docsLibraryId !== "string" ||
    typeof rawWorkspace.id !== "string" ||
    rawNode.docsLibraryId !== rawWorkspace.id
  ) {
    return null;
  }
  const node = {
    ...rawNode,
    docsLibraryId: rawNode.docsLibraryId,
  } as typeof knowledgeNodes.$inferSelect;
  const workspace = {
    ...rawWorkspace,
    libraryType: rawWorkspace.libraryType,
  } as typeof docsLibraries.$inferSelect;

  const directAccess = (permission: DocsNodeAccess["permission"]): DocsNodeAccess => ({
    node,
    workspace,
    permission,
  });

  // A project-information hub is owner-private metadata.  The owner may
  // always access it (including a stale project_id); members can read only a
  // canonical shell that has a readable active direct project child.
  if (node.systemKey === "project_information_root") {
    if (workspace.ownerUserId === user.id) return directAccess("owner");
    if (
      node.projectId ||
      node.parentId ||
      node.rootPageId !== node.id
    ) {
      return null;
    }

    let childRows: Array<{ projectId: string | null; archivedAt: Date | null }>;
    try {
      childRows = await db
        .select({
          projectId: knowledgeNodes.projectId,
          archivedAt: knowledgeNodes.archivedAt,
        })
        .from(knowledgeNodes)
        .where(
          and(
            eq(knowledgeNodes.docsLibraryId, workspace.id),
            eq(knowledgeNodes.parentId, node.id),
          ),
        );
    } catch {
      return null;
    }
    const projectIds = childRows
      .filter((child) => !child.archivedAt && Boolean(child.projectId))
      .map((child) => child.projectId as string);
    const projectPermissions = await getProjectPermissionSets(projectIds, user);
    return projectIds.some((projectId) => projectPermissions.readable.has(projectId))
      ? directAccess("read")
      : null;
  }

  const effectiveProjectId = node.projectId;
  if (effectiveProjectId) {
    // Project membership is an ACL gate for every node carrying project_id,
    // including nodes in a Personal Library.  This is the same decision as
    // getDocsNodeAccessMap, but scoped to this one project instead of all
    // project references in the library.
    const projectPermissions = await getProjectPermissionSets([effectiveProjectId], user);
    if (!projectPermissions.readable.has(effectiveProjectId)) return null;
    if (workspace.ownerUserId === user.id) return directAccess("owner");
    return directAccess(
      projectPermissions.writable.has(effectiveProjectId) ? "write" : "read",
    );
  }

  if (workspace.ownerUserId === user.id) return directAccess("owner");

  const ancestorIds =
    (await getDocsAncestorPaths([
      {
        id: node.id,
        parentId: node.parentId,
        docsLibraryId: workspace.id,
      },
    ])).get(node.id) ?? [];
  if (ancestorIds.length === 0) return null;

  let shares: Array<{
    nodeId: string;
    permission: string | null;
  }>;
  try {
    shares = await db
      .select({
        nodeId: knowledgeNodeShares.nodeId,
        permission: knowledgeNodeShares.permission,
      })
      .from(knowledgeNodeShares)
      .where(
        and(
          eq(knowledgeNodeShares.userId, user.id),
          inArray(knowledgeNodeShares.nodeId, ancestorIds),
        ),
      );
  } catch {
    return null;
  }
  const permissionByNodeId = new Map(
    shares
      .filter(
        (share): share is { nodeId: string; permission: "read" | "write" } =>
          (share.permission === "read" || share.permission === "write") &&
          typeof share.nodeId === "string",
      )
      .map((share) => [share.nodeId, share.permission]),
  );
  const permission = ancestorIds
    .map((ancestorId) => permissionByNodeId.get(ancestorId))
    .find(
      (candidate): candidate is "read" | "write" =>
        candidate === "read" || candidate === "write",
    );
  return permission ? directAccess(permission) : null;
}

/** Only the personal Docs Library owner may create, update, or revoke shares. */
export async function getDocsNodeShareManager(
  nodeId: string,
  user: SessionUser,
) {
  const access = await getDocsNodeAccess(nodeId, user);
  if (
    !access ||
    access.workspace.libraryType !== "personal"
  ) return null;
  if (access.workspace.ownerUserId !== user.id) return null;
  return access;
}

export async function requireDocsNode(
  nodeId: string,
  user: SessionUser,
  mode: "read" | "write" = "read",
) {
  const access = await getDocsNodeAccess(nodeId, user);
  if (!access) return null;
  if (mode === "write" && access.permission === "read") return null;
  return { node: access.node, workspace: access.workspace };
}

export async function requireDocsSupertag(
  supertagId: string,
  docsLibraryId: string,
) {
  const [tag] = await db
    .select()
    .from(knowledgeSupertags)
    .where(
      and(
        eq(knowledgeSupertags.id, supertagId),
        eq(knowledgeSupertags.docsLibraryId, docsLibraryId),
      ),
    )
    .limit(1);
  return tag ?? null;
}

export async function requireDocsField(fieldId: string, docsLibraryId: string) {
  const [field] = await db
    .select()
    .from(knowledgeFields)
    .where(
      and(
        eq(knowledgeFields.id, fieldId),
        eq(knowledgeFields.docsLibraryId, docsLibraryId),
      ),
    )
    .limit(1);
  return field ?? null;
}

export async function getKnowledgeNodeChildMetadata(
  docsLibraryId: string,
  nodeIds: string[],
  accessibleProjectIds: string[] | null,
  includeArchived = false,
  user?: SessionUser,
  accessMap?: DocsNodeAccessMap,
) {
  if (nodeIds.length === 0) {
    return { hasChildrenIds: [], loadedChildrenParentIds: [] };
  }
  // When an actor is available, defer project/share decisions to the same
  // per-node ACL resolver used by the node routes.  A broad candidate query is
  // required for an explicitly shared personal subtree that carries a stale
  // or otherwise inaccessible project_id.
  const accessibleNodeCondition = user
    ? undefined
    : accessibleProjectIds === null
    ? undefined
    : accessibleProjectIds.length > 0
      ? or(isNull(knowledgeNodes.projectId), inArray(knowledgeNodes.projectId, accessibleProjectIds))
      : isNull(knowledgeNodes.projectId);
  const nodeVisibleOrBridge = sql<boolean>`(
    regexp_replace(trim(${knowledgeNodes.title}), '[[:space:]]+', '', 'g') <> ''
    OR EXISTS (
      WITH RECURSIVE blank_descendants AS (
        SELECT
          id,
          parent_id,
          title,
          archived_at,
          docs_library_id,
          ARRAY[id]::uuid[] AS visited_path,
          0 AS depth
        FROM knowledge_nodes
        WHERE parent_id = ${knowledgeNodes.id}
          AND docs_library_id = ${docsLibraryId}
        UNION ALL
        SELECT
          child.id,
          child.parent_id,
          child.title,
          child.archived_at,
          child.docs_library_id,
          ancestor.visited_path || ARRAY[child.id]::uuid[],
          ancestor.depth + 1
        FROM knowledge_nodes AS child
        INNER JOIN blank_descendants AS ancestor ON child.parent_id = ancestor.id
        WHERE child.docs_library_id = ${docsLibraryId}
          AND ancestor.depth < 512
          AND NOT child.id = ANY(ancestor.visited_path)
      )
      SELECT 1 FROM blank_descendants
      WHERE archived_at IS NULL
        AND regexp_replace(trim(title), '[[:space:]]+', '', 'g') <> ''
    )
  )`;
  const notLegacyEmailBlank = sql<boolean>`NOT (
    ${knowledgeNodes.title} = '（空行）'
    AND EXISTS (
      WITH RECURSIVE email_ancestors AS (
        SELECT
          id,
          parent_id,
          system_key,
          docs_library_id,
          ARRAY[id]::uuid[] AS visited_path,
          0 AS depth
        FROM knowledge_nodes
        WHERE id = ${knowledgeNodes.id}
          AND docs_library_id = ${docsLibraryId}
        UNION ALL
        SELECT
          parent.id,
          parent.parent_id,
          parent.system_key,
          parent.docs_library_id,
          child.visited_path || ARRAY[parent.id]::uuid[],
          child.depth + 1
        FROM knowledge_nodes AS parent
        INNER JOIN email_ancestors AS child ON parent.id = child.parent_id
        WHERE parent.docs_library_id = ${docsLibraryId}
          AND child.depth < 512
          AND NOT parent.id = ANY(child.visited_path)
      )
      SELECT 1 FROM email_ancestors WHERE system_key LIKE 'project_mail:%'
    )
  )`;
  const [childRows, placementRows] = await Promise.all([
    db
      .select({ id: knowledgeNodes.id, parentId: knowledgeNodes.parentId })
      .from(knowledgeNodes)
      .where(
        and(
          eq(knowledgeNodes.docsLibraryId, docsLibraryId),
          inArray(knowledgeNodes.parentId, nodeIds),
          nodeVisibleOrBridge,
          notLegacyEmailBlank,
          includeArchived ? undefined : isNull(knowledgeNodes.archivedAt),
          accessibleNodeCondition,
        ),
      ),
    db
      .select({ nodeId: knowledgeNodePlacements.nodeId, parentId: knowledgeNodePlacements.parentNodeId })
      .from(knowledgeNodePlacements)
      .innerJoin(knowledgeNodes, eq(knowledgeNodePlacements.nodeId, knowledgeNodes.id))
      .where(
        and(
          inArray(knowledgeNodePlacements.parentNodeId, nodeIds),
          eq(knowledgeNodes.docsLibraryId, docsLibraryId),
          nodeVisibleOrBridge,
          notLegacyEmailBlank,
          includeArchived ? undefined : isNull(knowledgeNodes.archivedAt),
          accessibleNodeCondition,
        ),
      ),
  ]);
  let visibleChildIds: Set<string> | null = null;
  if (user) {
    const candidateChildIds = Array.from(
      new Set([
        ...childRows.map((row) => row.id),
        ...placementRows.map((row) => row.nodeId),
      ]),
    );
    const candidateAccessMap = accessMap ?? new Map<string, DocsNodeAccess>();
    const missingIds = candidateChildIds.filter((id) => !candidateAccessMap.has(id));
    // Resolve all missing children in one bounded batch.  The batch resolver
    // no longer scans an entire Personal Library; it evaluates owner/project
    // candidates locally and fetches only hub children/ancestor closure rows.
    const fetchedAccessMap = missingIds.length
      ? await getDocsNodeAccessMap(missingIds, user)
      : new Map<string, DocsNodeAccess>();
    visibleChildIds = new Set(
      candidateChildIds.filter(
        (id) => candidateAccessMap.has(id) || fetchedAccessMap.has(id),
      ),
    );
  }
  const childCountByParent: Record<string, number> = {};
  for (const row of childRows) {
    if (!row.parentId || (visibleChildIds && !visibleChildIds.has(row.id))) continue;
    childCountByParent[row.parentId] = (childCountByParent[row.parentId] ?? 0) + 1;
  }
  for (const row of placementRows) {
    if (!row.parentId || (visibleChildIds && !visibleChildIds.has(row.nodeId))) continue;
    childCountByParent[row.parentId] = (childCountByParent[row.parentId] ?? 0) + 1;
  }
  return {
    hasChildrenIds: Array.from(new Set([
      ...childRows
        .filter((row) => !visibleChildIds || visibleChildIds.has(row.id))
        .map((row) => row.parentId)
        .filter((id): id is string => Boolean(id)),
      ...placementRows
        .filter((row) => !visibleChildIds || visibleChildIds.has(row.nodeId))
        .map((row) => row.parentId),
    ])),
    childCountByParent,
    loadedChildrenParentIds: [],
  };
}

export function serializeNode(row: typeof knowledgeNodes.$inferSelect) {
  return serializeNodeWithOptions(row, { includeBody: true });
}

export function serializeNodeWithoutBody(row: typeof knowledgeNodes.$inferSelect) {
  return serializeNodeWithOptions(row, { includeBody: false });
}

const docsArchiveLastPurgedAt = new Map<string, number>();
const docsArchivePurgeInFlight = new Map<string, Promise<number>>();

/**
 * Docsを開いた時に最大1日1回、30日を過ぎたアーカイブだけを物理削除する。
 * activeまたは保存期間内の子孫を持つ親はcascade対象にせず、データ欠損を防ぐ。
 */
export async function purgeExpiredDocsArchive(docsLibraryId: string, now = new Date()) {
  const lastRun = docsArchiveLastPurgedAt.get(docsLibraryId) ?? 0;
  if (now.getTime() - lastRun < 24 * 60 * 60 * 1000) return 0;
  const existingRun = docsArchivePurgeInFlight.get(docsLibraryId);
  if (existingRun) return existingRun;
  const run = (async () => {
    const retentionDays = readDeletionRetentionDays();
    const cutoff = new Date(
      now.getTime() - retentionDays * 24 * 60 * 60 * 1000,
    );
    const cutoffIso = cutoff.toISOString();
    const rows = await db.execute(sql`
    with recursive purgeable as (
      select n.id
      from knowledge_nodes n
      where n.docs_library_id = ${docsLibraryId}
        and n.archived_at < ${cutoffIso}
        and not exists (
          with recursive descendants as (
            select
              child.id,
              child.archived_at,
              child.docs_library_id,
              array[child.id]::uuid[] as visited_path,
              0 as depth
            from knowledge_nodes child
            where child.parent_id = n.id
              and child.docs_library_id = ${docsLibraryId}
            union all
            select
              child.id,
              child.archived_at,
              child.docs_library_id,
              parent.visited_path || array[child.id]::uuid[],
              parent.depth + 1
            from knowledge_nodes child
            join descendants parent on child.parent_id = parent.id
            where child.docs_library_id = ${docsLibraryId}
              and parent.depth < 512
              and not child.id = any(parent.visited_path)
          )
         select 1 from descendants
         where archived_at is null or archived_at >= ${cutoffIso}
       )
        -- Never let a same-library purge cascade into a foreign child.
        -- Traverse every descendant library with a visited path; a malformed
        -- cross-library edge blocks the candidate root entirely.
        and not exists (
          with recursive all_descendants as (
            select
              child.id,
              child.docs_library_id,
              array[child.id]::uuid[] as visited_path,
              0 as depth
            from knowledge_nodes child
            where child.parent_id = n.id
            union all
            select
              child.id,
              child.docs_library_id,
              parent.visited_path || array[child.id]::uuid[],
              parent.depth + 1
            from knowledge_nodes child
            join all_descendants parent on child.parent_id = parent.id
            where parent.depth < 512
              and not child.id = any(parent.visited_path)
          )
          select 1 from all_descendants
          where docs_library_id <> ${docsLibraryId}
             or depth >= 512
        )
   )
    select id::text as id
    from purgeable
    `) as Array<{ id?: string | null }>;
    const deletedIds = rows
      .map((row) => row.id)
      .filter((value): value is string => typeof value === "string");
    let purgedIds: string[] = [];
    if (deletedIds.length > 0) {
      await db.transaction(async (tx) => {
        // Recheck and lock the candidate rows inside the same transaction as
        // the audit insert/delete. A concurrent restore therefore removes the
        // row from this set instead of being audited and then deleted.
        const lockedRows = await tx
          .select({ id: knowledgeNodes.id, parentId: knowledgeNodes.parentId })
          .from(knowledgeNodes)
          .where(
            and(
              inArray(knowledgeNodes.id, deletedIds),
              lt(knowledgeNodes.archivedAt, cutoff),
            ),
          )
          .for("update");
        const candidateIds = new Set(lockedRows.map((row) => row.id));
        const parentById = new Map<string, string | null>(
          lockedRows.map((row) => [row.id, row.parentId]),
        );
        const blocked = new Set<string>();
        let frontier = lockedRows.map((row) => row.id);
        // Lock and inspect the full descendant closure. A restored or
        // foreign-library child must block its candidate ancestor; otherwise
        // deleting the parent would cascade into the child after the child
        // transaction appeared to restore successfully.
        for (let depth = 0; depth < 512 && frontier.length > 0; depth += 1) {
          const descendants = await tx
            .select({
              id: knowledgeNodes.id,
              parentId: knowledgeNodes.parentId,
              archivedAt: knowledgeNodes.archivedAt,
              docsLibraryId: knowledgeNodes.docsLibraryId,
            })
            .from(knowledgeNodes)
            .where(inArray(knowledgeNodes.parentId, frontier))
            .for("update");
          if (descendants.length === 0) break;
          const next: string[] = [];
          for (const child of descendants) {
            parentById.set(child.id, child.parentId);
            const unsafe =
              child.docsLibraryId !== docsLibraryId ||
              child.archivedAt === null ||
              child.archivedAt >= cutoff;
            if (unsafe) {
              let ancestor = child.parentId;
              while (ancestor && candidateIds.has(ancestor)) {
                blocked.add(ancestor);
                ancestor = parentById.get(ancestor) ?? null;
              }
            }
            next.push(child.id);
          }
          frontier = next;
        }
        purgedIds = lockedRows
          .map((row) => row.id)
          .filter((id) => !blocked.has(id));
        if (purgedIds.length === 0) return;

        if (typeof tx.insert === "function") {
          const batchId = createDeletionBatchId();
          for (const nodeId of purgedIds) {
            await appendContentDeletionEvent(tx, {
              batchId,
              entityType: "docs_node",
              entityId: nodeId,
              rootEntityId: nodeId,
              action: "purged",
              source: "web.docs.archive_cleanup",
              eventAt: now,
              metadata: { retention_days: retentionDays },
            });
          }
        }
        await tx.delete(knowledgeNodes).where(inArray(knowledgeNodes.id, purgedIds));
      });
    }
    const cwd = resolve(process.cwd());
    const repoRoot = basename(cwd).toLowerCase() === "frontend" ? resolve(cwd, "..") : cwd;
    const runRoot = resolve(repoRoot, "artifacts", "foam_curation", "phase3_source_v1", "semantic_overlay_runs");
    if (existsSync(runRoot)) {
      const safePrefix = `${runRoot}${sep}`;
      for (const entry of readdirSync(runRoot, { withFileTypes: true })) {
        if (!entry.isDirectory()) continue;
        const target = resolve(runRoot, entry.name);
        if (!target.startsWith(safePrefix) || !existsSync(target)) continue;
        try {
          if (statSync(target).mtimeMs < cutoff.getTime()) rmSync(target, { recursive: true, force: true });
        } catch (error) {
          if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
        }
      }
    }
    docsArchiveLastPurgedAt.set(docsLibraryId, now.getTime());
    return purgedIds.length;
  })();
  docsArchivePurgeInFlight.set(docsLibraryId, run);
  try {
    return await run;
  } finally {
    if (docsArchivePurgeInFlight.get(docsLibraryId) === run) docsArchivePurgeInFlight.delete(docsLibraryId);
  }
}

export async function listDocsState(
  user: SessionUser,
  filters: {
    search?: string | null;
    supertagId?: string | null;
    includeArchived?: boolean;
  } = {},
) {
  // Generic Docs bootstrap always starts from the actor's Personal Docs
  // Library. Project information is a real child under its 案件情報 hub;
  // project selection belongs to the project-information route, not a hidden
  // library selected by this generic state loader.
  const workspace = await ensureDocsWorkspace(user);
  if (!workspace) return null;
  await purgeExpiredDocsArchive(workspace.id);
  const projectsForUser = await getUserProjects(user.id);
  const accessibleProjectIds = projectsForUser.map((project) => project.id);
  // Include the actor's personal library, explicit personal shares, and all
  // owner-personal/legacy project libraries for projects readable by the
  // actor. Final node ACL checks below decide which candidates are visible.
  const personalWorkspace = workspace;
  const sharedWorkspaceRows = await db
    .select({ docsLibraryId: knowledgeNodes.docsLibraryId })
    .from(knowledgeNodeShares)
    .innerJoin(knowledgeNodes, eq(knowledgeNodeShares.nodeId, knowledgeNodes.id))
    .innerJoin(docsLibraries, eq(knowledgeNodes.docsLibraryId, docsLibraries.id))
    .where(
      and(
        eq(knowledgeNodeShares.userId, user.id),
        eq(docsLibraries.libraryType, "personal"),
      ),
    );
  const workspaceIds = await getDocsLibraryIdsForReadableProjects(user.id, [
    workspace.id,
    personalWorkspace?.id,
    ...sharedWorkspaceRows.map((row) => row.docsLibraryId),
  ].filter((value): value is string => Boolean(value)));

  const emptyState = async () => {
    // The requested canonical workspace is itself ACL-checked above.  It is
    // the only safe fallback when a filter yields no visible node; shared
    // personal workspaces are never included unless a visible node originates
    // there.
    const definitions = await getDocsWorkspaceDefinitions(
      workspace.ownerUserId === user.id ? [workspace.id] : [],
    );
    return {
      workspace,
      nodes: [],
      hasChildrenIds: [],
      childCountByParent: {},
      loadedChildrenParentIds: [],
      supertags: definitions.supertags,
      nodeSupertags: [],
      supertagFields: definitions.supertagFields,
      placements: [],
      fields: definitions.fields,
      fieldValues: [],
      views: definitions.views,
      suggestions: [],
      importJobs: [],
      importItems: [],
      attachments: [],
      edges: [],
      projects: projectsForUser,
    };
  };

  const tagNodeIds =
    filters.supertagId && filters.supertagId !== "all"
      ? (
          await db
            .select({ nodeId: knowledgeNodeSupertags.nodeId })
            .from(knowledgeNodeSupertags)
            .innerJoin(
              knowledgeSupertags,
              eq(knowledgeNodeSupertags.supertagId, knowledgeSupertags.id),
            )
            .where(
              and(
                inArray(knowledgeSupertags.docsLibraryId, workspaceIds),
                eq(knowledgeNodeSupertags.supertagId, filters.supertagId),
              ),
            )
        ).map((row) => row.nodeId)
      : null;

  if (tagNodeIds && tagNodeIds.length === 0) {
    return emptyState();
  }

  const search = filters.search?.trim();
  // Bootstrap must retain a legacy blank root only when it bridges to a
  // meaningful descendant; the client then hoists that descendant.
  const nodeVisibleOrBridge = sql<boolean>`(
    regexp_replace(trim(${knowledgeNodes.title}), '[[:space:]]+', '', 'g') <> ''
    OR EXISTS (
      WITH RECURSIVE blank_descendants AS (
        SELECT
          id,
          parent_id,
          title,
          archived_at,
          docs_library_id,
          ARRAY[id]::uuid[] AS visited_path,
          0 AS depth
        FROM knowledge_nodes
        WHERE parent_id = ${knowledgeNodes.id}
          AND docs_library_id = ${workspace.id}
        UNION ALL
        SELECT
          child.id,
          child.parent_id,
          child.title,
          child.archived_at,
          child.docs_library_id,
          ancestor.visited_path || ARRAY[child.id]::uuid[],
          ancestor.depth + 1
        FROM knowledge_nodes AS child
        INNER JOIN blank_descendants AS ancestor ON child.parent_id = ancestor.id
        WHERE child.docs_library_id = ${workspace.id}
          AND ancestor.depth < 512
          AND NOT child.id = ANY(ancestor.visited_path)
      )
      SELECT 1 FROM blank_descendants
      WHERE archived_at IS NULL
        AND regexp_replace(trim(title), '[[:space:]]+', '', 'g') <> ''
    )
  )`;
  const nodeConditions = [
    inArray(knowledgeNodes.docsLibraryId, workspaceIds),
    nodeVisibleOrBridge,
    sql<boolean>`NOT (
      ${knowledgeNodes.title} = '（空行）'
      AND EXISTS (
        WITH RECURSIVE email_ancestors AS (
          SELECT
            id,
            parent_id,
            system_key,
            docs_library_id,
            ARRAY[id]::uuid[] AS visited_path,
            0 AS depth
          FROM knowledge_nodes
          WHERE id = ${knowledgeNodes.id}
            AND docs_library_id = ${knowledgeNodes.docsLibraryId}
          UNION ALL
          SELECT
            parent.id,
            parent.parent_id,
            parent.system_key,
            parent.docs_library_id,
            child.visited_path || ARRAY[parent.id]::uuid[],
            child.depth + 1
          FROM knowledge_nodes AS parent
          INNER JOIN email_ancestors AS child ON parent.id = child.parent_id
          WHERE parent.docs_library_id = ${knowledgeNodes.docsLibraryId}
            AND child.depth < 512
            AND NOT parent.id = ANY(child.visited_path)
        )
        SELECT 1 FROM email_ancestors WHERE system_key LIKE 'project_mail:%'
      )
    )`,
    // Bootstrap returns only top-level roots. Project information roots are
    // real children under the Personal 案件情報 hub and load through the
    // children route.
    isNull(knowledgeNodes.parentId),
    filters.includeArchived ? undefined : isNull(knowledgeNodes.archivedAt),
    tagNodeIds ? inArray(knowledgeNodes.id, tagNodeIds) : undefined,
  ].filter(Boolean);

  let searchNodeIds: string[] | null = null;
  if (search) {
    const rows = await db
      .select({ nodeId: knowledgeSearchIndex.nodeId })
      .from(knowledgeSearchIndex)
      .where(
        and(
          inArray(knowledgeSearchIndex.docsLibraryId, workspaceIds),
          or(
            ilike(knowledgeSearchIndex.titleText, `%${search}%`),
            ilike(knowledgeSearchIndex.bodyTextPlain, `%${search}%`),
          ),
        ),
      )
      .limit(300);
    searchNodeIds = rows.map((row) => row.nodeId);
    if (searchNodeIds.length === 0) {
      return emptyState();
    }
  }
  if (searchNodeIds) nodeConditions.push(inArray(knowledgeNodes.id, searchNodeIds));

  const candidateNodes = await db
    .select()
    .from(knowledgeNodes)
    .where(and(...nodeConditions))
    .orderBy(asc(knowledgeNodes.sortOrder), desc(knowledgeNodes.updatedAt))
    .limit(1000);
  const nodeAccessMap = await getDocsNodeAccessMap(
    candidateNodes.map((node) => node.id),
    user,
    { includeArchived: filters.includeArchived === true },
  );
  const nodes = candidateNodes.filter((node) => nodeAccessMap.has(node.id));
  const nodeIds = nodes.map((node) => node.id);
  if (nodes.length === 0 && workspace.ownerUserId !== user.id) {
    return emptyState();
  }
  const originWorkspaceIds = nodes.length > 0
    ? Array.from(new Set(nodes.map((node) => node.docsLibraryId)))
    : [workspace.id];
  const childMetadataByWorkspace = await Promise.all(
    workspaceIds.map((docsLibraryId) => {
      const workspaceNodeIds = nodes
        .filter((node) => node.docsLibraryId === docsLibraryId)
        .map((node) => node.id);
      return getKnowledgeNodeChildMetadata(
        docsLibraryId,
        workspaceNodeIds,
        null,
        filters.includeArchived,
        user,
        nodeAccessMap,
      );
    }),
  );
  const childMetadata = childMetadataByWorkspace.reduce(
    (result, current) => {
      for (const id of current.hasChildrenIds) {
        if (!result.hasChildrenIds.includes(id)) result.hasChildrenIds.push(id);
      }
      Object.assign(result.childCountByParent, current.childCountByParent);
      return result;
    },
    {
      hasChildrenIds: [] as string[],
      childCountByParent: {} as Record<string, number>,
      loadedChildrenParentIds: [] as string[],
    },
  );

  const definitions = await getDocsWorkspaceDefinitions(originWorkspaceIds);
  const definitionLibraries = await db
    .select({ id: docsLibraries.id, ownerUserId: docsLibraries.ownerUserId })
    .from(docsLibraries)
    .where(inArray(docsLibraries.id, originWorkspaceIds));
  const ownerByLibraryId = new Map(
    definitionLibraries.map((library) => [library.id, library.ownerUserId]),
  );
  const ownerLibraryIds = new Set(
    originWorkspaceIds.filter((docsLibraryId) => ownerByLibraryId.get(docsLibraryId) === user.id),
  );
  const [suggestions, importJobs, edges] = await Promise.all([
    db
      .select()
      .from(knowledgeAiSuggestions)
      .where(
        and(
          inArray(knowledgeAiSuggestions.docsLibraryId, originWorkspaceIds),
          nodeIds.length > 0
            ? or(isNull(knowledgeAiSuggestions.nodeId), inArray(knowledgeAiSuggestions.nodeId, nodeIds))
            : isNull(knowledgeAiSuggestions.nodeId),
        ),
      )
      .orderBy(desc(knowledgeAiSuggestions.createdAt))
      .limit(50),
    db
      .select()
      .from(knowledgeImportJobs)
      .where(
        and(
          inArray(knowledgeImportJobs.docsLibraryId, originWorkspaceIds),
          ownerLibraryIds.size > 0
            ? accessibleProjectIds.length > 0
              ? or(isNull(knowledgeImportJobs.projectId), inArray(knowledgeImportJobs.projectId, accessibleProjectIds))
              : isNull(knowledgeImportJobs.projectId)
            : accessibleProjectIds.length > 0
              ? and(
                  sql<boolean>`${knowledgeImportJobs.projectId} is not null`,
                  inArray(knowledgeImportJobs.projectId, accessibleProjectIds),
                )
              : sql<boolean>`false`,
        ),
      )
      .orderBy(desc(knowledgeImportJobs.createdAt))
      .limit(20),
    nodeIds.length
      ? db
          .select()
          .from(knowledgeEdges)
          .where(
            and(
              inArray(knowledgeEdges.sourceNodeId, nodeIds),
              inArray(knowledgeEdges.targetNodeId, nodeIds),
            ),
          )
          .limit(200)
      : Promise.resolve([]),
  ]);
  const [nodeSupertags, storedFieldValues, attachments] = nodeIds.length
    ? await Promise.all([
        db
          .select()
          .from(knowledgeNodeSupertags)
          .where(inArray(knowledgeNodeSupertags.nodeId, nodeIds)),
        db
          .select()
          .from(knowledgeFieldValues)
          .where(inArray(knowledgeFieldValues.nodeId, nodeIds)),
        db
          .select()
          .from(knowledgeAttachments)
          .where(inArray(knowledgeAttachments.nodeId, nodeIds)),
      ])
    : [[], [], []];
  const nodeWorkspaceById = new Map(nodes.map((node) => [node.id, node.docsLibraryId]));
  const attachedSupertagIds = new Set(nodeSupertags.map((relation) => relation.supertagId));
  const attachedFieldIds = new Set(storedFieldValues.map((value) => value.fieldId));
  // Non-owner/shared libraries may contribute only definitions that are
  // actually attached to a visible node (plus the canonical project/task
  // supertags needed to render an authorized project shell).  Owners retain
  // the complete definition catalog for their own library.
  const supertags = definitions.supertags.filter(
    (tag) =>
      ownerLibraryIds.has(tag.docsLibraryId) ||
      attachedSupertagIds.has(tag.id) ||
      (tag.systemKey === "project_info" && nodes.some((node) => node.docsLibraryId === tag.docsLibraryId && node.projectId)),
  );
  const supertagWorkspaceById = new Map(supertags.map((tag) => [tag.id, tag.docsLibraryId]));
  const fieldIdsFromAttachedTags = new Set(
    definitions.supertagFields
      .filter((relation) => supertagWorkspaceById.has(relation.supertagId))
      .map((relation) => relation.fieldId),
  );
  const fields = definitions.fields.filter(
    (field) =>
      ownerLibraryIds.has(field.docsLibraryId) ||
      attachedFieldIds.has(field.id) ||
      fieldIdsFromAttachedTags.has(field.id),
  );
  const fieldById = new Map(fields.map((field) => [field.id, field]));
  const supertagFields = definitions.supertagFields.filter(
    (relation) =>
      supertagWorkspaceById.has(relation.supertagId) &&
      fieldById.has(relation.fieldId),
  );
  const views = definitions.views.filter(
    (view) =>
      ownerLibraryIds.has(view.docsLibraryId) ||
      (view.supertagId !== null && supertagWorkspaceById.has(view.supertagId)),
  );
  const validNodeSupertags = nodeSupertags.filter(
    (relation) =>
      nodeWorkspaceById.get(relation.nodeId) !== undefined &&
      supertagWorkspaceById.get(relation.supertagId) === nodeWorkspaceById.get(relation.nodeId),
  );
  const validStoredFieldValues = storedFieldValues.filter((value) => {
    const nodeWorkspaceId = nodeWorkspaceById.get(value.nodeId);
    const field = fieldById.get(value.fieldId);
    if (!nodeWorkspaceId || !field || field.docsLibraryId !== nodeWorkspaceId) return false;
    if (!value.targetNodeId) return true;
    return nodeWorkspaceById.get(value.targetNodeId) === nodeWorkspaceId;
  });
  const taskFieldValues = nodeIds.length
    ? await listDocsTaskSyntheticFieldValues({ nodeIds, fields, user })
    : [];
  const validTaskFieldValues = taskFieldValues.filter((value) => {
    const nodeWorkspaceId = nodeWorkspaceById.get(value.nodeId);
    const field = fieldById.get(value.fieldId);
    if (!nodeWorkspaceId || !field || field.docsLibraryId !== nodeWorkspaceId) return false;
    if (!value.targetNodeId) return true;
    return nodeWorkspaceById.get(value.targetNodeId) === nodeWorkspaceId;
  });
  const fieldValues = [...validStoredFieldValues, ...validTaskFieldValues];

  const placements = nodeIds.length
    ? await db
        .select()
        .from(knowledgeNodePlacements)
        .where(
          and(
            inArray(knowledgeNodePlacements.nodeId, nodeIds),
            inArray(knowledgeNodePlacements.parentNodeId, nodeIds),
          ),
        )
    : [];
  const validPlacements = placements.filter(
    (placement) =>
      nodeWorkspaceById.get(placement.nodeId) !== undefined &&
      nodeWorkspaceById.get(placement.parentNodeId) === nodeWorkspaceById.get(placement.nodeId),
  );

  const validEdges = edges.filter(
    (edge) =>
      nodeWorkspaceById.get(edge.sourceNodeId) !== undefined &&
      nodeWorkspaceById.get(edge.targetNodeId) === nodeWorkspaceById.get(edge.sourceNodeId),
  );

  const visibleSuggestionRows = suggestions.filter(
    (suggestion) =>
      suggestion.nodeId !== null
        ? nodeIds.includes(suggestion.nodeId) &&
          nodeWorkspaceById.get(suggestion.nodeId) === suggestion.docsLibraryId
        : ownerLibraryIds.has(suggestion.docsLibraryId),
  );
  const visibleImportJobs = importJobs.filter((job) => {
    if (ownerLibraryIds.has(job.docsLibraryId)) return true;
    if (!job.projectId || !accessibleProjectIds.includes(job.projectId)) return false;
    return nodes.some(
      (node) => node.docsLibraryId === job.docsLibraryId && node.projectId === job.projectId,
    );
  });

  const importItems =
    visibleImportJobs.length > 0
      ? await db
          .select()
          .from(knowledgeImportItems)
          .where(
            and(
              inArray(
                knowledgeImportItems.jobId,
                visibleImportJobs.map((job) => job.id),
              ),
              nodeIds.length > 0
                ? or(isNull(knowledgeImportItems.nodeId), inArray(knowledgeImportItems.nodeId, nodeIds))
                : isNull(knowledgeImportItems.nodeId),
            ),
          )
      : [];
  const visibleImportItems = importItems.filter((item) => {
    const job = visibleImportJobs.find((candidate) => candidate.id === item.jobId);
    if (!job) return false;
    if (!item.nodeId) return ownerLibraryIds.has(job.docsLibraryId);
    return nodeIds.includes(item.nodeId) && nodeWorkspaceById.get(item.nodeId) === job.docsLibraryId;
  });

  return {
    workspace,
    nodes,
    ...childMetadata,
    supertags,
    nodeSupertags: validNodeSupertags,
    supertagFields,
    placements: validPlacements,
    fields,
    fieldValues,
    views,
    suggestions: visibleSuggestionRows,
    importJobs: visibleImportJobs,
    importItems: visibleImportItems,
    attachments,
    edges: validEdges,
    projects: projectsForUser,
  };
}

type DocsListState = NonNullable<Awaited<ReturnType<typeof listDocsState>>>;

/**
 * Remove every node-derived payload row that is no longer covered by the
 * request's surviving ACL map.  This is intentionally applied at the route
 * boundary as well as in listDocsState: a share can be revoked between the
 * candidate query and serialization.
 */
export function filterDocsStateToVisibleNodes(
  state: DocsListState,
  visibleNodeIds: ReadonlySet<string>,
  visibleWorkspaceIds: ReadonlySet<string>,
): DocsListState {
  const nodes = state.nodes.filter((node) => visibleNodeIds.has(node.id));
  const visibleSupertags = state.supertags.filter((tag) => visibleWorkspaceIds.has(tag.docsLibraryId));
  const supertagIds = new Set(visibleSupertags.map((tag) => tag.id));
  const visibleFields = state.fields.filter((field) => visibleWorkspaceIds.has(field.docsLibraryId));
  const fieldIds = new Set(visibleFields.map((field) => field.id));
  const nodeWorkspaceById = new Map(nodes.map((node) => [node.id, node.docsLibraryId]));
  const supertagWorkspaceById = new Map(
    visibleSupertags.map((tag) => [tag.id, tag.docsLibraryId]),
  );
  const fieldWorkspaceById = new Map(
    visibleFields.map((field) => [field.id, field.docsLibraryId]),
  );
  const visibleViews = state.views.filter(
    (view) =>
      visibleWorkspaceIds.has(view.docsLibraryId) &&
      (!view.supertagId || supertagWorkspaceById.get(view.supertagId) === view.docsLibraryId),
  );
  const visibleProjectIds = new Set(
    nodes
      .map((node) => node.projectId)
      .filter((projectId): projectId is string => Boolean(projectId)),
  );
  const visibleJobIds = new Set(
    state.importJobs
      .filter(
        (job) =>
          visibleWorkspaceIds.has(job.docsLibraryId) &&
          (!job.projectId || visibleProjectIds.has(job.projectId)),
      )
      .map((job) => job.id),
  );
  const nodeSupertags = state.nodeSupertags.filter(
    (relation) => {
      const nodeWorkspaceId = nodeWorkspaceById.get(relation.nodeId);
      return (
        nodeWorkspaceId !== undefined &&
        supertagIds.has(relation.supertagId) &&
        supertagWorkspaceById.get(relation.supertagId) === nodeWorkspaceId
      );
    },
  );
  const supertagFields = state.supertagFields.filter(
    (relation) => {
      const supertagWorkspaceId = supertagWorkspaceById.get(relation.supertagId);
      return (
        supertagWorkspaceId !== undefined &&
        supertagIds.has(relation.supertagId) &&
        fieldIds.has(relation.fieldId) &&
        fieldWorkspaceById.get(relation.fieldId) === supertagWorkspaceId
      );
    },
  );
  const fieldValues = state.fieldValues.filter(
    (value) => {
      const nodeWorkspaceId = nodeWorkspaceById.get(value.nodeId);
      if (
        nodeWorkspaceId === undefined ||
        !fieldIds.has(value.fieldId) ||
        fieldWorkspaceById.get(value.fieldId) !== nodeWorkspaceId
      ) {
        return false;
      }
      return (
        !value.targetNodeId ||
        nodeWorkspaceById.get(value.targetNodeId) === nodeWorkspaceId
      );
    },
  );
  const placements = state.placements.filter(
    (placement) => {
      const nodeWorkspaceId = nodeWorkspaceById.get(placement.nodeId);
      return (
        nodeWorkspaceId !== undefined &&
        nodeWorkspaceById.get(placement.parentNodeId) === nodeWorkspaceId
      );
    },
  );
  const attachments = state.attachments.filter((attachment) => visibleNodeIds.has(attachment.nodeId));
  const edges = state.edges.filter(
    (edge) => {
      const sourceWorkspaceId = nodeWorkspaceById.get(edge.sourceNodeId);
      return (
        sourceWorkspaceId !== undefined &&
        nodeWorkspaceById.get(edge.targetNodeId) === sourceWorkspaceId
      );
    },
  );
  const suggestions = state.suggestions.filter(
    (suggestion) => {
      if (!visibleWorkspaceIds.has(suggestion.docsLibraryId)) return false;
      if (!suggestion.nodeId) return true;
      return nodeWorkspaceById.get(suggestion.nodeId) === suggestion.docsLibraryId;
    },
  );
  const importJobs = state.importJobs.filter((job) => visibleJobIds.has(job.id));
  const importJobWorkspaceById = new Map(importJobs.map((job) => [job.id, job.docsLibraryId]));
  const importItems = state.importItems.filter(
    (item) => {
      const jobWorkspaceId = importJobWorkspaceById.get(item.jobId);
      if (!jobWorkspaceId) return false;
      if (!item.nodeId) return true;
      return nodeWorkspaceById.get(item.nodeId) === jobWorkspaceId;
    },
  );

  return {
    ...state,
    nodes,
    hasChildrenIds: state.hasChildrenIds.filter((id) => visibleNodeIds.has(id)),
    loadedChildrenParentIds: state.loadedChildrenParentIds.filter((id) => visibleNodeIds.has(id)),
    supertags: visibleSupertags,
    nodeSupertags,
    supertagFields,
    fields: visibleFields,
    fieldValues,
    views: visibleViews,
    suggestions,
    importJobs,
    importItems,
    placements,
    attachments,
    edges,
  };
}

type DocsWorkspaceDefinitions = {
  supertags: Array<typeof knowledgeSupertags.$inferSelect>;
  fields: Array<typeof knowledgeFields.$inferSelect>;
  supertagFields: Array<typeof knowledgeSupertagFields.$inferSelect>;
  views: Array<typeof knowledgeSavedViews.$inferSelect>;
};

/**
 * Load definitions for exactly the workspaces that contributed visible
 * nodes.  A shared personal node must bring along its origin definitions, but
 * no unrelated/revoked workspace metadata may be returned.
 */
export async function getDocsWorkspaceDefinitions(
  workspaceIds: string[],
): Promise<DocsWorkspaceDefinitions> {
  const ids = Array.from(new Set(workspaceIds.filter(Boolean)));
  if (ids.length === 0) {
    return { supertags: [], fields: [], supertagFields: [], views: [] };
  }

  const [rawSupertags, rawFields, rawSupertagFields, rawViews] = await Promise.all([
    db
      .select()
      .from(knowledgeSupertags)
      .where(inArray(knowledgeSupertags.docsLibraryId, ids))
      .orderBy(asc(knowledgeSupertags.name)),
    db
      .select()
      .from(knowledgeFields)
      .where(inArray(knowledgeFields.docsLibraryId, ids))
      .orderBy(asc(knowledgeFields.sortOrder), asc(knowledgeFields.name)),
    db
      .select({ relation: knowledgeSupertagFields, supertagWorkspaceId: knowledgeSupertags.docsLibraryId })
      .from(knowledgeSupertagFields)
      .innerJoin(
        knowledgeSupertags,
        eq(knowledgeSupertagFields.supertagId, knowledgeSupertags.id),
      )
      .where(inArray(knowledgeSupertags.docsLibraryId, ids))
      .orderBy(asc(knowledgeSupertagFields.sortOrder)),
    db
      .select()
      .from(knowledgeSavedViews)
      .where(inArray(knowledgeSavedViews.docsLibraryId, ids))
      .orderBy(asc(knowledgeSavedViews.sortOrder), asc(knowledgeSavedViews.createdAt)),
  ]);

  const workspaceIdSet = new Set(ids);
  const rawSupertagById = new Map(rawSupertags.map((tag) => [tag.id, tag]));
  // Keep the definition itself, but sever a corrupt parent relation that
  // points into another workspace rather than exposing that foreign ID.
  const supertags = rawSupertags
    .filter((tag) => workspaceIdSet.has(tag.docsLibraryId))
    .map((tag) => {
      if (!tag.parentSupertagId) return tag;
      const parent = rawSupertagById.get(tag.parentSupertagId);
      return parent && parent.docsLibraryId === tag.docsLibraryId
        ? tag
        : { ...tag, parentSupertagId: null };
    });
  const supertagById = new Map(supertags.map((tag) => [tag.id, tag]));

  const fields = rawFields.filter(
    (field) =>
      workspaceIdSet.has(field.docsLibraryId) &&
      supertagById.get(field.supertagId)?.docsLibraryId === field.docsLibraryId,
  );
  const fieldById = new Map(fields.map((field) => [field.id, field]));
  const supertagFields = rawSupertagFields
    .filter(
      ({ relation, supertagWorkspaceId }) =>
        supertagById.get(relation.supertagId)?.docsLibraryId === supertagWorkspaceId &&
        fieldById.get(relation.fieldId)?.docsLibraryId === supertagWorkspaceId,
    )
    .map(({ relation }) => relation);
  const views = rawViews.filter(
    (view) =>
      workspaceIdSet.has(view.docsLibraryId) &&
      (!view.supertagId || supertagById.get(view.supertagId)?.docsLibraryId === view.docsLibraryId),
  );

  return { supertags, fields, supertagFields, views };
}

export async function getWorkspaceViews(docsLibraryId: string) {
  return await db
    .select()
    .from(knowledgeSavedViews)
    .where(eq(knowledgeSavedViews.docsLibraryId, docsLibraryId))
    .orderBy(asc(knowledgeSavedViews.sortOrder), asc(knowledgeSavedViews.createdAt));
}

export async function getUserProjects(userId: string) {
  const readableProjectIds = await getReadableProjectIds(userId);
  if (readableProjectIds.length === 0) return [];
  const rows = await db
    .select({ project: projects })
    .from(projects)
    .where(
      and(
        isNull(projects.deletedAt),
        inArray(projects.id, readableProjectIds),
      ),
    );
  return rows.map(({ project }) => ({
    id: project.id,
    name: project.name,
    space_id: project.spaceId,
    color:
      project.projectMetadata &&
      typeof project.projectMetadata === "object" &&
      !Array.isArray(project.projectMetadata) &&
      typeof (project.projectMetadata as Record<string, unknown>).color === "string"
        ? ((project.projectMetadata as Record<string, unknown>).color as string)
        : null,
  }));
}

/**
 * Return Docs Library IDs that may contain nodes for projects readable by an
 * actor. Unified project information nodes live in the project owner's
 * Personal Library. Callers must run the resulting node candidates through
 * getDocsNodeAccess(Map) before serializing them.
 */
export async function getDocsLibraryIdsForReadableProjects(
  userId: string,
  seedIds: readonly string[] = [],
) {
  const ids = new Set(seedIds.filter(Boolean));
  const readableProjectIds = await getReadableProjectIds(userId);
  if (readableProjectIds.length === 0) return Array.from(ids);

  const personalRows = await db
    .select({ id: docsLibraries.id })
    .from(docsLibraries)
    .innerJoin(projects, eq(docsLibraries.ownerUserId, projects.ownerId))
    .where(
      and(
        eq(docsLibraries.libraryType, "personal"),
        isNull(projects.deletedAt),
        inArray(projects.id, readableProjectIds),
      ),
    );
  for (const row of personalRows) ids.add(row.id);
  return Array.from(ids);
}

export async function appendKnowledgeRevision(
  client: DocsDb,
  node: typeof knowledgeNodes.$inferSelect,
  userId: string,
  changeSummary: string,
  sourceRefs: unknown[] = [],
) {
  await client.insert(knowledgeRevisions).values({
    nodeId: node.id,
    title: node.title,
    bodyJson: encryptJsonValue(
      decryptJsonValueIfNeeded(node.bodyJson ?? {}, NODE_BODY_JSON_AAD),
      REVISION_BODY_JSON_AAD,
    ) as Record<string, unknown> | string,
    ["bodyText"]: encryptText(
      decryptTextIfNeeded(node.bodyText ?? "", NODE_BODY_TEXT_AAD) ?? "",
      REVISION_BODY_TEXT_AAD,
    ) ?? "",
    changeSummary,
    sourceRefsJson: Array.isArray(sourceRefs) ? sourceRefs : [],
    createdBy: userId,
  });
}

type DocsSearchIndexNode = Pick<
  typeof knowledgeNodes.$inferSelect,
  "id" | "docsLibraryId" | "projectId" | "title"
> &
  Partial<Pick<typeof knowledgeNodes.$inferSelect, "bodyJson" | "bodyText">>;

const EDITABLE_DOC_BLOCK_TYPES = new Set(["markdown", "code"]);

/**
 * Return the user-visible body used by the lexical Docs search index.
 *
 * Ordinary nodes continue to use their body_text/title mirror.  Typed
 * markdown/code blocks are the exception: their independent, editable
 * content is the searchable body while title remains the node label.
 */
export function effectiveDocsSearchBodyText(
  node: Pick<DocsSearchIndexNode, "title" | "bodyJson" | "bodyText">,
  fallbackBodyText = "",
): string {
  const body = decryptNodeBodyJson(node.bodyJson ?? {});
  if (
    body.format === "doc_block" &&
    typeof body.block_type === "string" &&
    EDITABLE_DOC_BLOCK_TYPES.has(body.block_type) &&
    typeof body.content === "string"
  ) {
    return body.content;
  }

  const bodyText = decryptNodeBodyText(node.bodyText ?? "");
  return bodyText || fallbackBodyText || node.title || "";
}

export async function upsertKnowledgeSearchIndex(
  client: DocsDb,
  node: DocsSearchIndexNode,
  bodyTextPlain = "",
) {
  const effectiveBodyText = effectiveDocsSearchBodyText(node, bodyTextPlain);
  await client
    .insert(knowledgeSearchIndex)
    .values({
      nodeId: node.id,
      docsLibraryId: node.docsLibraryId,
      projectId: node.projectId,
      titleText: node.title ?? "",
      bodyTextPlain: effectiveBodyText,
      updatedAt: new Date(),
    })
    .onConflictDoUpdate({
      target: knowledgeSearchIndex.nodeId,
      set: {
        docsLibraryId: node.docsLibraryId,
        projectId: node.projectId,
        titleText: node.title ?? "",
        bodyTextPlain: effectiveBodyText,
        updatedAt: new Date(),
      },
    });
}

export function normalizeFieldValueInput(
  field: typeof knowledgeFields.$inferSelect,
  value: unknown,
): typeof knowledgeFieldValues.$inferInsert {
  const fieldType = normalizeDocsFieldType(field.fieldType);
  const base: typeof knowledgeFieldValues.$inferInsert = {
    nodeId: "",
    fieldId: field.id,
    valueJson: value === undefined ? null : value,
    valueText: null,
    valueNumber: null,
    valueDatetime: null,
    targetNodeId: null,
  };

  if (value === null || value === undefined || value === "") {
    return base;
  }

  if (fieldType === "number") {
    const num = Number(value);
    return { ...base, valueNumber: Number.isFinite(num) ? num : null };
  }

  if (fieldType === "date") {
    const date =
      value instanceof Date
        ? value
        : typeof value === "string"
          ? new Date(value)
          : null;
    return {
      ...base,
      valueDatetime: date && !Number.isNaN(date.getTime()) ? date : null,
      valueText: typeof value === "string" ? value : null,
    };
  }

  if (fieldType === "reference") {
    return {
      ...base,
      valueText: typeof value === "string" ? value : JSON.stringify(value),
      targetNodeId: typeof value === "string" ? value : null,
    };
  }

  if (fieldType === "options") {
    return {
      ...base,
      valueText: typeof value === "string" ? value : String(value),
    };
  }

  if (fieldType === "long_text" || fieldType === "options_from_supertag") {
    return {
      ...base,
      valueText: Array.isArray(value) ? value.join(", ") : JSON.stringify(value),
    };
  }

  return {
    ...base,
    valueText: typeof value === "string" ? value : String(value),
  };
}

export function normalizeSuggestionStatus(value: unknown): string {
  return normalizeStatus(value, VALID_SUGGESTION_STATUSES, "proposed");
}

export function normalizeImportStatus(value: unknown): string {
  return normalizeStatus(value, VALID_IMPORT_STATUSES, "proposed");
}
