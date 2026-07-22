import { and, asc, desc, eq, ilike, inArray, isNull, or, sql } from "drizzle-orm";
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
  knowledgeSearchIndex,
  knowledgeSavedViews,
  knowledgeNodeSupertags,
  knowledgeSupertagFields,
  knowledgeNodes,
  knowledgeRevisions,
  knowledgeSupertags,
  knowledgeWorkspaces,
  projectMembers,
  projects,
} from "@/db/schema";
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
import {
  decryptJsonValueIfNeeded,
  decryptTextIfNeeded,
  encryptJsonValue,
  encryptText,
} from "./field-crypto";
import { listDocsTaskSyntheticFieldValues } from "./docs-task-binding";
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
  row: typeof knowledgeWorkspaces.$inferSelect,
) {
  return {
    id: row.id,
    name: row.name,
    description: row.description,
    owner_user_id: row.ownerUserId,
    settings: row.settingsJson ?? {},
    created_at: serializeDate(row.createdAt),
    updated_at: serializeDate(row.updatedAt),
  };
}

function serializeNodeWithOptions(
  row: typeof knowledgeNodes.$inferSelect,
  options: { includeBody: boolean },
) {
  const includeBody = options.includeBody;
  return {
    id: row.id,
    workspace_id: row.workspaceId,
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
    workspace_id: row.workspaceId,
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
    workspace_id: row.workspaceId,
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
    workspace_id: row.workspaceId,
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
    workspace_id: row.workspaceId,
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
    workspace_id: row.workspaceId,
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
  workspaceId: string,
  nodeId: string,
): Promise<string[]> {
  const rows = await client
    .select({
      id: knowledgeNodes.id,
      parentId: knowledgeNodes.parentId,
    })
    .from(knowledgeNodes)
    .where(eq(knowledgeNodes.workspaceId, workspaceId));
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
  workspaceId: string,
  nodeId: string,
): Promise<string[]> {
  const [nodes, placements] = await Promise.all([
    client
      .select({
        id: knowledgeNodes.id,
        parentId: knowledgeNodes.parentId,
      })
      .from(knowledgeNodes)
      .where(eq(knowledgeNodes.workspaceId, workspaceId)),
    client
      .select({
        nodeId: knowledgeNodePlacements.nodeId,
        parentNodeId: knowledgeNodePlacements.parentNodeId,
      })
      .from(knowledgeNodePlacements)
      .innerJoin(knowledgeNodes, eq(knowledgeNodePlacements.nodeId, knowledgeNodes.id))
      .where(eq(knowledgeNodes.workspaceId, workspaceId)),
  ]);
  return collectKnowledgeDisplayDescendantIds(nodes, placements, nodeId);
}

async function resolveReferenceTargetIds(
  client: DocsDb,
  workspaceId: string,
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
          eq(knowledgeNodes.workspaceId, workspaceId),
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
    node.workspaceId,
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
  workspaceId: string,
  userId: string,
) {
  await tx.execute(
    sql`select pg_advisory_xact_lock(hashtext(${`${workspaceId}:default-docs-seed`}))`,
  );
  const existingTags = await tx
    .select()
    .from(knowledgeSupertags)
    .where(eq(knowledgeSupertags.workspaceId, workspaceId));
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
          workspaceId,
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
            workspaceId,
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
            eq(knowledgeNodes.workspaceId, workspaceId),
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
    .where(eq(knowledgeSavedViews.workspaceId, workspaceId));
  const existingViewNames = new Set(existingViews.map((view) => view.name));
  const missingViews: Array<typeof knowledgeSavedViews.$inferInsert> = [];
  if (!existingViewNames.has("全案件 Task board")) {
    missingViews.push({
      workspaceId,
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
  // 既存DBに残る同名ビュー行は scripts/seed_docs_sample_content.ts が掃除する。
  if (!existingViewNames.has("今月の Meeting list")) {
    missingViews.push({
      workspaceId,
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
        eq(knowledgeNodes.workspaceId, workspaceId),
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
        eq(knowledgeNodes.workspaceId, workspaceId),
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
          eq(knowledgeNodes.workspaceId, workspaceId),
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
    workspaceId,
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
  await upsertKnowledgeSearchIndex(tx, home, home.title);
  await appendKnowledgeRevision(tx, home, userId, "Homeノードを作成");
  for (const [index, templateNode] of HOME_TEMPLATE.entries()) {
    const child = await insertDocsNode(tx, {
      workspaceId,
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
    await upsertKnowledgeSearchIndex(tx, child, child.title);
    await appendKnowledgeRevision(tx, child, userId, "Home初期テンプレートを作成");
  }
}

export async function ensureDocsWorkspace(user: SessionUser) {
  const [existing] = await db
    .select()
    .from(knowledgeWorkspaces)
    .where(eq(knowledgeWorkspaces.ownerUserId, user.id))
    .orderBy(asc(knowledgeWorkspaces.createdAt))
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
            .update(knowledgeWorkspaces)
            .set({
              name: DOCS_WORKSPACE_NAME,
              description: existing.description || DOCS_WORKSPACE_DESCRIPTION,
              settingsJson: mergedSettings,
            })
            .where(eq(knowledgeWorkspaces.id, existing.id))
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
      .insert(knowledgeWorkspaces)
      .values({
        name: DOCS_WORKSPACE_NAME,
        description: DOCS_WORKSPACE_DESCRIPTION,
        ownerUserId: user.id,
        settingsJson: DOCS_WORKSPACE_SETTINGS,
      })
      .onConflictDoUpdate({
        target: knowledgeWorkspaces.ownerUserId,
        set: {
          name: DOCS_WORKSPACE_NAME,
          description: DOCS_WORKSPACE_DESCRIPTION,
          settingsJson: DOCS_WORKSPACE_SETTINGS,
        },
      })
      .returning();
    await seedDefaultDocsWorkspace(tx, workspace.id, user.id);
    return workspace;
  });
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

export async function requireDocsNode(
  nodeId: string,
  user: SessionUser,
  mode: "read" | "write" = "read",
) {
  const [row] = await db
    .select({
      node: knowledgeNodes,
      workspace: knowledgeWorkspaces,
    })
    .from(knowledgeNodes)
    .innerJoin(
      knowledgeWorkspaces,
      eq(knowledgeNodes.workspaceId, knowledgeWorkspaces.id),
    )
    .where(eq(knowledgeNodes.id, nodeId))
    .limit(1);

  if (!row || row.workspace.ownerUserId !== user.id) return null;

  if (row.node.projectId) {
    const access =
      mode === "write"
        ? await ensureProjectWritable(row.node.projectId, user)
        : await ensureProjectReadable(row.node.projectId, user);
    if (!access) return null;
  }

  return row;
}

export async function requireDocsSupertag(
  supertagId: string,
  workspaceId: string,
) {
  const [tag] = await db
    .select()
    .from(knowledgeSupertags)
    .where(
      and(
        eq(knowledgeSupertags.id, supertagId),
        eq(knowledgeSupertags.workspaceId, workspaceId),
      ),
    )
    .limit(1);
  return tag ?? null;
}

export async function requireDocsField(fieldId: string, workspaceId: string) {
  const [field] = await db
    .select()
    .from(knowledgeFields)
    .where(
      and(
        eq(knowledgeFields.id, fieldId),
        eq(knowledgeFields.workspaceId, workspaceId),
      ),
    )
    .limit(1);
  return field ?? null;
}

export async function getKnowledgeNodeChildMetadata(
  workspaceId: string,
  nodeIds: string[],
  accessibleProjectIds: string[],
  includeArchived = false,
) {
  if (nodeIds.length === 0) {
    return { hasChildrenIds: [], loadedChildrenParentIds: [] };
  }
  const accessibleNodeCondition = accessibleProjectIds.length > 0
    ? or(isNull(knowledgeNodes.projectId), inArray(knowledgeNodes.projectId, accessibleProjectIds))
    : isNull(knowledgeNodes.projectId);
  const [childRows, placementRows] = await Promise.all([
    db
      .select({ parentId: knowledgeNodes.parentId })
      .from(knowledgeNodes)
      .where(
        and(
          eq(knowledgeNodes.workspaceId, workspaceId),
          inArray(knowledgeNodes.parentId, nodeIds),
          includeArchived ? undefined : isNull(knowledgeNodes.archivedAt),
          accessibleNodeCondition,
        ),
      ),
    db
      .select({ parentId: knowledgeNodePlacements.parentNodeId })
      .from(knowledgeNodePlacements)
      .innerJoin(knowledgeNodes, eq(knowledgeNodePlacements.nodeId, knowledgeNodes.id))
      .where(
        and(
          inArray(knowledgeNodePlacements.parentNodeId, nodeIds),
          eq(knowledgeNodes.workspaceId, workspaceId),
          includeArchived ? undefined : isNull(knowledgeNodes.archivedAt),
          accessibleNodeCondition,
        ),
      ),
  ]);
  return {
    hasChildrenIds: Array.from(new Set([
      ...childRows.map((row) => row.parentId).filter((id): id is string => Boolean(id)),
      ...placementRows.map((row) => row.parentId),
    ])),
    loadedChildrenParentIds: [],
  };
}

export function serializeNode(row: typeof knowledgeNodes.$inferSelect) {
  return serializeNodeWithOptions(row, { includeBody: true });
}

export function serializeNodeWithoutBody(row: typeof knowledgeNodes.$inferSelect) {
  return serializeNodeWithOptions(row, { includeBody: false });
}

const DOCS_ARCHIVE_RETENTION_MS = 30 * 24 * 60 * 60 * 1000;
const docsArchiveLastPurgedAt = new Map<string, number>();
const docsArchivePurgeInFlight = new Map<string, Promise<number>>();

/**
 * Docsを開いた時に最大1日1回、30日を過ぎたアーカイブだけを物理削除する。
 * activeまたは保存期間内の子孫を持つ親はcascade対象にせず、データ欠損を防ぐ。
 */
export async function purgeExpiredDocsArchive(workspaceId: string, now = new Date()) {
  const lastRun = docsArchiveLastPurgedAt.get(workspaceId) ?? 0;
  if (now.getTime() - lastRun < 24 * 60 * 60 * 1000) return 0;
  const existingRun = docsArchivePurgeInFlight.get(workspaceId);
  if (existingRun) return existingRun;
  const run = (async () => {
    const cutoff = new Date(now.getTime() - DOCS_ARCHIVE_RETENTION_MS);
    const cutoffIso = cutoff.toISOString();
    const rows = await db.execute(sql`
    with recursive purgeable as (
      select n.id
      from knowledge_nodes n
      where n.workspace_id = ${workspaceId}
        and n.archived_at < ${cutoffIso}
        and not exists (
          with recursive descendants as (
            select child.id, child.archived_at
            from knowledge_nodes child
            where child.parent_id = n.id
            union all
            select child.id, child.archived_at
            from knowledge_nodes child
            join descendants parent on child.parent_id = parent.id
          )
          select 1 from descendants
          where archived_at is null or archived_at >= ${cutoffIso}
        )
    ), deleted as (
      delete from knowledge_nodes n
      using purgeable p
      where n.id = p.id
      returning n.id
    )
    select count(*)::int as count from deleted
    `) as Array<{ count: number }>;
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
    docsArchiveLastPurgedAt.set(workspaceId, now.getTime());
    return Number(rows[0]?.count ?? 0);
  })();
  docsArchivePurgeInFlight.set(workspaceId, run);
  try {
    return await run;
  } finally {
    if (docsArchivePurgeInFlight.get(workspaceId) === run) docsArchivePurgeInFlight.delete(workspaceId);
  }
}

export async function listDocsState(
  user: SessionUser,
  filters: {
    search?: string | null;
    projectId?: string | null;
    supertagId?: string | null;
    includeArchived?: boolean;
  } = {},
) {
  const workspace = await ensureDocsWorkspace(user);
  await purgeExpiredDocsArchive(workspace.id);
  const projectsForUser = await getUserProjects(user.id);
  const accessibleProjectIds = projectsForUser.map((project) => project.id);

  if (filters.projectId) {
    const access = await ensureProjectReadable(filters.projectId, user);
    if (!access) return null;
  }

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
                eq(knowledgeSupertags.workspaceId, workspace.id),
                eq(knowledgeNodeSupertags.supertagId, filters.supertagId),
              ),
            )
        ).map((row) => row.nodeId)
      : null;

  if (tagNodeIds && tagNodeIds.length === 0) {
    return {
      workspace,
      nodes: [],
      hasChildrenIds: [],
      loadedChildrenParentIds: [],
      supertags: await getWorkspaceSupertags(workspace.id),
      nodeSupertags: [],
      supertagFields: await getWorkspaceSupertagFields(workspace.id),
      placements: [],
      fields: await getWorkspaceFields(workspace.id),
      fieldValues: [],
      views: await getWorkspaceViews(workspace.id),
      suggestions: [],
      importJobs: [],
      importItems: [],
      attachments: [],
      edges: [],
      projects: projectsForUser,
    };
  }

  const search = filters.search?.trim();
  const nodeConditions = [
    eq(knowledgeNodes.workspaceId, workspace.id),
    // bootstrapはルートだけを返す。ページ配下は近傍APIで遅延ロードする。
    isNull(knowledgeNodes.parentId),
    filters.includeArchived ? undefined : isNull(knowledgeNodes.archivedAt),
    filters.projectId
      ? eq(knowledgeNodes.projectId, filters.projectId)
      : accessibleProjectIds.length > 0
        ? or(isNull(knowledgeNodes.projectId), inArray(knowledgeNodes.projectId, accessibleProjectIds))
        : isNull(knowledgeNodes.projectId),
    tagNodeIds ? inArray(knowledgeNodes.id, tagNodeIds) : undefined,
  ].filter(Boolean);

  let searchNodeIds: string[] | null = null;
  if (search) {
    const rows = await db
      .select({ nodeId: knowledgeSearchIndex.nodeId })
      .from(knowledgeSearchIndex)
      .where(
        and(
          eq(knowledgeSearchIndex.workspaceId, workspace.id),
          or(
            ilike(knowledgeSearchIndex.titleText, `%${search}%`),
            ilike(knowledgeSearchIndex.bodyTextPlain, `%${search}%`),
          ),
        ),
      )
      .limit(300);
    searchNodeIds = rows.map((row) => row.nodeId);
    if (searchNodeIds.length === 0) {
      return {
        workspace,
        nodes: [],
        hasChildrenIds: [],
        loadedChildrenParentIds: [],
        supertags: await getWorkspaceSupertags(workspace.id),
        nodeSupertags: [],
        supertagFields: await getWorkspaceSupertagFields(workspace.id),
        placements: [],
        fields: await getWorkspaceFields(workspace.id),
        fieldValues: [],
        views: await getWorkspaceViews(workspace.id),
        suggestions: [],
        importJobs: [],
        importItems: [],
        attachments: [],
        edges: [],
        projects: projectsForUser,
      };
    }
  }
  if (searchNodeIds) nodeConditions.push(inArray(knowledgeNodes.id, searchNodeIds));

  const nodes = await db
    .select()
    .from(knowledgeNodes)
    .where(and(...nodeConditions))
    .orderBy(asc(knowledgeNodes.sortOrder), desc(knowledgeNodes.updatedAt))
    .limit(1000);
  const nodeIds = nodes.map((node) => node.id);
  const childMetadata = await getKnowledgeNodeChildMetadata(
    workspace.id,
    nodeIds,
    accessibleProjectIds,
    filters.includeArchived,
  );

  const [
    supertags,
    supertagFields,
    fields,
    views,
    suggestions,
    importJobs,
    edges,
  ] = await Promise.all([
    getWorkspaceSupertags(workspace.id),
    getWorkspaceSupertagFields(workspace.id),
    getWorkspaceFields(workspace.id),
    getWorkspaceViews(workspace.id),
    db
      .select()
      .from(knowledgeAiSuggestions)
      .where(
        and(
          eq(knowledgeAiSuggestions.workspaceId, workspace.id),
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
          eq(knowledgeImportJobs.workspaceId, workspace.id),
          accessibleProjectIds.length > 0
            ? or(isNull(knowledgeImportJobs.projectId), inArray(knowledgeImportJobs.projectId, accessibleProjectIds))
            : isNull(knowledgeImportJobs.projectId),
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
  const taskFieldValues = nodeIds.length
    ? await listDocsTaskSyntheticFieldValues({ nodeIds, fields })
    : [];
  const fieldValues = [...storedFieldValues, ...taskFieldValues];

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

  const importItems =
    importJobs.length > 0
      ? await db
          .select()
          .from(knowledgeImportItems)
          .where(
            and(
              inArray(
                knowledgeImportItems.jobId,
                importJobs.map((job) => job.id),
              ),
              nodeIds.length > 0
                ? or(isNull(knowledgeImportItems.nodeId), inArray(knowledgeImportItems.nodeId, nodeIds))
                : isNull(knowledgeImportItems.nodeId),
            ),
          )
      : [];

  return {
    workspace,
    nodes,
    ...childMetadata,
    supertags,
    nodeSupertags,
    supertagFields,
    placements,
    fields,
    fieldValues,
    views,
    suggestions,
    importJobs,
    importItems,
    attachments,
    edges,
    projects: projectsForUser,
  };
}

async function getWorkspaceSupertags(workspaceId: string) {
  return await db
    .select()
    .from(knowledgeSupertags)
    .where(eq(knowledgeSupertags.workspaceId, workspaceId))
    .orderBy(asc(knowledgeSupertags.name));
}

async function getWorkspaceFields(workspaceId: string) {
  return await db
    .select()
    .from(knowledgeFields)
    .where(eq(knowledgeFields.workspaceId, workspaceId))
    .orderBy(asc(knowledgeFields.sortOrder), asc(knowledgeFields.name));
}

async function getWorkspaceSupertagFields(workspaceId: string) {
  const rows = await db
    .select({ relation: knowledgeSupertagFields })
    .from(knowledgeSupertagFields)
    .innerJoin(
      knowledgeSupertags,
      eq(knowledgeSupertagFields.supertagId, knowledgeSupertags.id),
    )
    .where(eq(knowledgeSupertags.workspaceId, workspaceId))
    .orderBy(asc(knowledgeSupertagFields.sortOrder));
  return rows.map((row) => row.relation);
}

export async function getWorkspaceViews(workspaceId: string) {
  return await db
    .select()
    .from(knowledgeSavedViews)
    .where(eq(knowledgeSavedViews.workspaceId, workspaceId))
    .orderBy(asc(knowledgeSavedViews.sortOrder), asc(knowledgeSavedViews.createdAt));
}

export async function getUserProjects(userId: string) {
  const rows = await db
    .select({ project: projects })
    .from(projectMembers)
    .innerJoin(projects, eq(projectMembers.projectId, projects.id))
    .where(and(eq(projectMembers.userId, userId), isNull(projects.deletedAt)));
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

export async function upsertKnowledgeSearchIndex(
  client: DocsDb,
  node: Pick<
    typeof knowledgeNodes.$inferSelect,
    "id" | "workspaceId" | "projectId" | "title"
  >,
  bodyTextPlain: string,
) {
  await client
    .insert(knowledgeSearchIndex)
    .values({
      nodeId: node.id,
      workspaceId: node.workspaceId,
      projectId: node.projectId,
      titleText: node.title ?? "",
      bodyTextPlain,
      updatedAt: new Date(),
    })
    .onConflictDoUpdate({
      target: knowledgeSearchIndex.nodeId,
      set: {
        workspaceId: node.workspaceId,
        projectId: node.projectId,
        titleText: node.title ?? "",
        bodyTextPlain,
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
