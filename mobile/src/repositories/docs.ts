/**
 * Docs Repository（アウトライン型ナレッジ）。
 *
 * 詳細設計書 2.4 / 2.5 / 2.7。Tasks の流儀（`applyRemote*` + `*Repo`）を踏襲するが、
 * Docs は派生更新（暗号化ミラー・検索index・エッジ・リビジョン・タスク連携）が
 * サーバ専用に集約されているため、**書き込みは online 直叩きせず常に
 * 「ローカル反映 + outbox 積み」**に一本化する（online でも次の runSync で push される）。
 *
 * サーバーの updated_at を serverUpdatedAt として保持し、未送信行は pull で
 * 上書きしない。競合時は outbox にサーバー値を保存して両方の編集を残す。
 */

import { and, asc, eq, inArray, isNull, like, or } from "drizzle-orm";
import { getDb, schema } from "../db/client";
import { getToken } from "../lib/auth";
import type {
  DocsEdge,
  DocsField,
  DocsFieldValue,
  DocsNode,
  DocsNodePlacement,
  DocsNodeSupertag,
  DocsSupertag,
  DocsSupertagField,
} from "../types/api";
import {
  enqueueOutbox,
  hasPendingOutbox,
  randomId,
  recordOutboxServerSnapshot,
} from "./outbox";
import {
  docsNodeDeletionIds,
  expandProtectedDocsNodeAncestors,
} from "./docs-reconciliation";

type DbNode = typeof schema.knowledgeNodes.$inferSelect;
type DbSupertag = typeof schema.knowledgeSupertags.$inferSelect;
type DbField = typeof schema.knowledgeFields.$inferSelect;
type DbFieldValue = typeof schema.knowledgeFieldValues.$inferSelect;

async function localDocsRowIsDirty(table: string, entityId: string): Promise<boolean> {
  const db = getDb();
  if (table === "knowledge_nodes") {
    const row = await db
      .select({ dirty: schema.knowledgeNodes.dirty })
      .from(schema.knowledgeNodes)
      .where(eq(schema.knowledgeNodes.id, entityId));
    return Boolean(row[0]?.dirty);
  }
  if (table === "knowledge_supertags") {
    const row = await db
      .select({ dirty: schema.knowledgeSupertags.dirty })
      .from(schema.knowledgeSupertags)
      .where(eq(schema.knowledgeSupertags.id, entityId));
    return Boolean(row[0]?.dirty);
  }
  const [first, second] = entityId.split(":", 2);
  if (!first || !second) return false;
  if (table === "knowledge_node_supertags") {
    const row = await db
      .select({ dirty: schema.knowledgeNodeSupertags.dirty })
      .from(schema.knowledgeNodeSupertags)
      .where(
        and(
          eq(schema.knowledgeNodeSupertags.nodeId, first),
          eq(schema.knowledgeNodeSupertags.supertagId, second),
        ),
      );
    return Boolean(row[0]?.dirty);
  }
  if (table === "knowledge_field_values") {
    const row = await db
      .select({ dirty: schema.knowledgeFieldValues.dirty })
      .from(schema.knowledgeFieldValues)
      .where(
        and(
          eq(schema.knowledgeFieldValues.nodeId, first),
          eq(schema.knowledgeFieldValues.fieldId, second),
        ),
      );
    return Boolean(row[0]?.dirty);
  }
  return false;
}

async function saveLocalDocsServerSnapshot(
  table: string,
  entityId: string,
  payload: unknown,
): Promise<void> {
  const db = getDb();
  const conflictPayload = payload as never;
  if (table === "knowledge_nodes") {
    await db
      .update(schema.knowledgeNodes)
      .set({ conflictPayload })
      .where(eq(schema.knowledgeNodes.id, entityId));
    return;
  }
  if (table === "knowledge_supertags") {
    await db
      .update(schema.knowledgeSupertags)
      .set({ conflictPayload })
      .where(eq(schema.knowledgeSupertags.id, entityId));
    return;
  }
  const [first, second] = entityId.split(":", 2);
  if (!first || !second) return;
  if (table === "knowledge_node_supertags") {
    await db
      .update(schema.knowledgeNodeSupertags)
      .set({ conflictPayload })
      .where(
        and(
          eq(schema.knowledgeNodeSupertags.nodeId, first),
          eq(schema.knowledgeNodeSupertags.supertagId, second),
        ),
      );
    return;
  }
  if (table === "knowledge_field_values") {
    await db
      .update(schema.knowledgeFieldValues)
      .set({ conflictPayload })
      .where(
        and(
          eq(schema.knowledgeFieldValues.nodeId, first),
          eq(schema.knowledgeFieldValues.fieldId, second),
        ),
      );
  }
}

async function shouldPreserveRemoteDocsRow(
  table: string,
  entityId: string,
  serverPayload: unknown,
): Promise<boolean> {
  if (await hasPendingOutbox(table, entityId)) {
    await recordOutboxServerSnapshot(table, entityId, serverPayload);
    return true;
  }
  if (await localDocsRowIsDirty(table, entityId)) {
    await saveLocalDocsServerSnapshot(table, entityId, serverPayload);
    return true;
  }
  return false;
}

// ---------- 行 → API 形マッパ ----------

function toNode(row: DbNode): DocsNode {
  return {
    id: row.id,
    workspace_id: row.workspaceId ?? null,
    parent_id: row.parentId ?? null,
    root_page_id: row.rootPageId ?? null,
    project_id: row.projectId ?? null,
    system_key: row.systemKey ?? null,
    title: row.title,
    aliases: Array.isArray(row.aliases) ? (row.aliases as string[]) : [],
    description: row.description ?? null,
    body_json: (row.bodyJson as Record<string, unknown> | null) ?? null,
    body_text: row.bodyText ?? null,
    node_type: (row.nodeType as DocsNode["node_type"]) ?? "node",
    display_props: (row.displayProps as Record<string, unknown> | null) ?? null,
    query_json: (row.queryJson as Record<string, unknown> | null) ?? null,
    view_json: (row.viewJson as Record<string, unknown> | null) ?? null,
    day_date: row.dayDate ?? null,
    sort_order: row.sortOrder ?? null,
    created_by: row.createdBy ?? null,
    updated_by: row.updatedBy ?? null,
    created_at: row.createdAt ?? null,
    updated_at: row.updatedAt ?? null,
    archived_at: row.archivedAt ?? null,
  };
}

function toSupertag(row: DbSupertag): DocsSupertag {
  return {
    id: row.id,
    workspace_id: row.workspaceId ?? null,
    parent_supertag_id: row.parentSupertagId ?? null,
    system_key: row.systemKey ?? null,
    name: row.name,
    base_type: row.baseType ?? null,
    description: row.description ?? null,
    icon: row.icon ?? null,
    color: row.color ?? null,
    template_json: (row.templateJson as Record<string, unknown> | null) ?? null,
    pinned_field_ids: Array.isArray(row.pinnedFieldIds)
      ? (row.pinnedFieldIds as string[])
      : [],
    config_json: (row.configJson as Record<string, unknown> | null) ?? null,
    title_template: row.titleTemplate ?? null,
    ai_instructions: row.aiInstructions ?? null,
    created_at: row.createdAt ?? null,
    updated_at: row.updatedAt ?? null,
  };
}

function toField(row: DbField): DocsField {
  return {
    id: row.id,
    workspace_id: row.workspaceId ?? null,
    supertag_id: row.supertagId ?? null,
    system_key: row.systemKey ?? null,
    name: row.name,
    field_type: (row.fieldType as DocsField["field_type"]) ?? "text",
    required: Boolean(row.required),
    options_json: (row.optionsJson as Record<string, unknown> | null) ?? null,
    default_value_json: row.defaultValueJson ?? null,
    sort_order: row.sortOrder ?? null,
    created_at: row.createdAt ?? null,
    updated_at: row.updatedAt ?? null,
  };
}

function toFieldValue(row: DbFieldValue): DocsFieldValue {
  return {
    node_id: row.nodeId,
    field_id: row.fieldId,
    value_json: row.valueJson ?? null,
    value_text: row.valueText ?? null,
    value_number: row.valueNumber ?? null,
    value_datetime: row.valueDatetime ?? null,
    target_node_id: row.targetNodeId ?? null,
    updated_at: row.updatedAt ?? null,
    updated_by: row.updatedBy ?? null,
  };
}

// ---------- applyRemote 群（pull / push 応答の反映） ----------

export async function applyRemoteDocsNodes(
  rows: DocsNode[],
  options: { force?: boolean } = {},
): Promise<void> {
  if (!rows.length) return;
  const db = getDb();
  const now = new Date().toISOString();
  for (const n of rows) {
    if (!options.force && await shouldPreserveRemoteDocsRow("knowledge_nodes", n.id, n)) {
      continue;
    }
    const values = {
      id: n.id,
      workspaceId: n.workspace_id ?? null,
      parentId: n.parent_id ?? null,
      rootPageId: n.root_page_id ?? null,
      projectId: n.project_id ?? null,
      systemKey: n.system_key ?? null,
      title: n.title ?? "",
      aliases: (n.aliases as unknown) ?? [],
      description: n.description ?? null,
      bodyJson: (n.body_json as unknown) ?? null,
      bodyText: n.body_text ?? null,
      nodeType: n.node_type ?? "node",
      displayProps: (n.display_props as unknown) ?? null,
      queryJson: (n.query_json as unknown) ?? null,
      viewJson: (n.view_json as unknown) ?? null,
      dayDate: n.day_date ?? null,
      sortOrder: n.sort_order ?? null,
      createdBy: n.created_by ?? null,
      updatedBy: n.updated_by ?? null,
      createdAt: n.created_at ?? now,
      updatedAt: n.updated_at ?? now,
      serverUpdatedAt: n.updated_at ?? now,
      dirty: false,
      conflictPayload: null,
      archivedAt: n.archived_at ?? null,
    };
    await db
      .insert(schema.knowledgeNodes)
      .values(values)
      .onConflictDoUpdate({
        target: schema.knowledgeNodes.id,
        set: {
          workspaceId: values.workspaceId,
          parentId: values.parentId,
          rootPageId: values.rootPageId,
          projectId: values.projectId,
          systemKey: values.systemKey,
          title: values.title,
          aliases: values.aliases,
          description: values.description,
          bodyJson: values.bodyJson,
          bodyText: values.bodyText,
          nodeType: values.nodeType,
          displayProps: values.displayProps,
          queryJson: values.queryJson,
          viewJson: values.viewJson,
          dayDate: values.dayDate,
          sortOrder: values.sortOrder,
          updatedBy: values.updatedBy,
          updatedAt: values.updatedAt,
          serverUpdatedAt: values.serverUpdatedAt,
          dirty: values.dirty,
          conflictPayload: values.conflictPayload,
          archivedAt: values.archivedAt,
        },
      });
  }
}

export async function applyDocsNodeTombstones(
  tombstones: Array<{ id: string; deleted_at?: string | null }>,
): Promise<void> {
  if (!tombstones.length) return;
  const db = getDb();
  // ハード削除の tombstone はローカルから物理削除する（アーカイブは通常行で来る）。
  const ids = tombstones.map((t) => t.id);
  for (const id of ids) {
    if (await shouldPreserveRemoteDocsRow("knowledge_nodes", id, {
        id,
        deleted: true,
        deleted_at: tombstones.find((t) => t.id === id)?.deleted_at ?? null,
      })) {
      continue;
    }
    await db.delete(schema.knowledgeNodes).where(eq(schema.knowledgeNodes.id, id));
  }
}

export async function reconcileDocsNodesWithServer(
  authoritativeIds: string[] | undefined,
  workspaceId: string | undefined,
): Promise<void> {
  // undefined は旧server互換のためno-op。[] は有効な「server node 0件」。
  if (!authoritativeIds || !workspaceId) return;
  const db = getDb();
  const serverIds = new Set(authoritativeIds);
  const localRows = await db
    .select({
      id: schema.knowledgeNodes.id,
      parentId: schema.knowledgeNodes.parentId,
      dirty: schema.knowledgeNodes.dirty,
    })
    .from(schema.knowledgeNodes)
    .where(eq(schema.knowledgeNodes.workspaceId, workspaceId));
  const staleRows = localRows.filter((row) => !serverIds.has(row.id));
  if (!staleRows.length) return;

  // 17k件規模でもN+1にしない。dirty/outboxをtable単位で一括取得してSet照合する。
  const [dirtyTags, dirtyFields, pendingOutbox] = await Promise.all([
    db
      .select({ nodeId: schema.knowledgeNodeSupertags.nodeId })
      .from(schema.knowledgeNodeSupertags)
      .where(eq(schema.knowledgeNodeSupertags.dirty, true)),
    db
      .select({ nodeId: schema.knowledgeFieldValues.nodeId })
      .from(schema.knowledgeFieldValues)
      .where(eq(schema.knowledgeFieldValues.dirty, true)),
    db
      .select({ tableName: schema.outbox.tableName, entityId: schema.outbox.entityId })
      .from(schema.outbox),
  ]);
  const dirtyRelationNodeIds = new Set([
    ...dirtyTags.map((row) => row.nodeId),
    ...dirtyFields.map((row) => row.nodeId),
  ]);
  const nodeOutboxIds = new Set(
    pendingOutbox
      .filter((row) => row.tableName === "knowledge_nodes")
      .map((row) => row.entityId),
  );
  const relationOutboxNodeIds = new Set(
    pendingOutbox
      .filter((row) =>
        row.tableName === "knowledge_node_supertags"
        || row.tableName === "knowledge_field_values",
      )
      .map((row) => row.entityId.split(":", 1)[0]),
  );

  const directlyProtected = new Set<string>();
  for (const row of staleRows) {
    const deletedPayload = {
      id: row.id,
      deleted: true,
      authoritative_scope_id: workspaceId,
    };
    if (nodeOutboxIds.has(row.id)) {
      directlyProtected.add(row.id);
      await recordOutboxServerSnapshot("knowledge_nodes", row.id, deletedPayload);
      continue;
    }
    if (row.dirty || dirtyRelationNodeIds.has(row.id) || relationOutboxNodeIds.has(row.id)) {
      directlyProtected.add(row.id);
      await saveLocalDocsServerSnapshot("knowledge_nodes", row.id, {
        ...deletedPayload,
        preserved_for_dirty_relation: true,
      });
    }
  }

  const protectedIds = expandProtectedDocsNodeAncestors(
    staleRows,
    directlyProtected,
  );
  for (const id of protectedIds) {
    if (directlyProtected.has(id)) continue;
    await saveLocalDocsServerSnapshot("knowledge_nodes", id, {
      id,
      deleted: true,
      authoritative_scope_id: workspaceId,
      preserved_as_dirty_ancestor: true,
    });
  }

  const deletableIds = docsNodeDeletionIds(staleRows, protectedIds);
  // SQLite schemaにFK cascadeがないため、関連rowも先に明示削除する。
  // parameter上限を超えないよう、node ID集合は500件ずつ処理する。
  for (let offset = 0; offset < deletableIds.length; offset += 500) {
    const ids = deletableIds.slice(offset, offset + 500);
    await db
      .delete(schema.knowledgeNodeSupertags)
      .where(inArray(schema.knowledgeNodeSupertags.nodeId, ids));
    await db
      .delete(schema.knowledgeFieldValues)
      .where(inArray(schema.knowledgeFieldValues.nodeId, ids));
    await db
      .delete(schema.knowledgeNodePlacements)
      .where(
        or(
          inArray(schema.knowledgeNodePlacements.nodeId, ids),
          inArray(schema.knowledgeNodePlacements.parentNodeId, ids),
        ),
      );
    await db
      .delete(schema.knowledgeEdges)
      .where(
        or(
          inArray(schema.knowledgeEdges.sourceNodeId, ids),
          inArray(schema.knowledgeEdges.targetNodeId, ids),
        ),
      );
    await db
      .delete(schema.knowledgeNodes)
      .where(inArray(schema.knowledgeNodes.id, ids));
  }
}

export async function applyRemoteDocsSupertags(
  rows: DocsSupertag[],
  options: { force?: boolean } = {},
): Promise<void> {
  if (!rows.length) return;
  const db = getDb();
  const now = new Date().toISOString();
  for (const s of rows) {
    if (!options.force && await shouldPreserveRemoteDocsRow("knowledge_supertags", s.id, s)) {
      continue;
    }
    const values = {
      id: s.id,
      workspaceId: s.workspace_id ?? null,
      parentSupertagId: s.parent_supertag_id ?? null,
      systemKey: s.system_key ?? null,
      name: s.name ?? "",
      baseType: s.base_type ?? null,
      description: s.description ?? null,
      icon: s.icon ?? null,
      color: s.color ?? null,
      templateJson: (s.template_json as unknown) ?? null,
      pinnedFieldIds: (s.pinned_field_ids as unknown) ?? [],
      configJson: (s.config_json as unknown) ?? null,
      titleTemplate: s.title_template ?? null,
      aiInstructions: s.ai_instructions ?? null,
      createdAt: s.created_at ?? now,
      updatedAt: s.updated_at ?? now,
      serverUpdatedAt: s.updated_at ?? now,
      dirty: false,
      conflictPayload: null,
    };
    await db
      .insert(schema.knowledgeSupertags)
      .values(values)
      .onConflictDoUpdate({
        target: schema.knowledgeSupertags.id,
        set: {
          workspaceId: values.workspaceId,
          parentSupertagId: values.parentSupertagId,
          systemKey: values.systemKey,
          name: values.name,
          baseType: values.baseType,
          description: values.description,
          icon: values.icon,
          color: values.color,
          templateJson: values.templateJson,
          pinnedFieldIds: values.pinnedFieldIds,
          configJson: values.configJson,
          titleTemplate: values.titleTemplate,
          aiInstructions: values.aiInstructions,
          updatedAt: values.updatedAt,
          serverUpdatedAt: values.serverUpdatedAt,
          dirty: values.dirty,
          conflictPayload: values.conflictPayload,
        },
      });
  }
}

export async function applyRemoteDocsFields(rows: DocsField[]): Promise<void> {
  if (!rows.length) return;
  const db = getDb();
  const now = new Date().toISOString();
  for (const f of rows) {
    const values = {
      id: f.id,
      workspaceId: f.workspace_id ?? null,
      supertagId: f.supertag_id ?? null,
      systemKey: f.system_key ?? null,
      name: f.name ?? "",
      fieldType: f.field_type ?? "text",
      required: Boolean(f.required),
      optionsJson: (f.options_json as unknown) ?? null,
      defaultValueJson: (f.default_value_json as unknown) ?? null,
      sortOrder: f.sort_order ?? null,
      createdAt: f.created_at ?? now,
      updatedAt: f.updated_at ?? now,
    };
    await db
      .insert(schema.knowledgeFields)
      .values(values)
      .onConflictDoUpdate({
        target: schema.knowledgeFields.id,
        set: {
          workspaceId: values.workspaceId,
          supertagId: values.supertagId,
          systemKey: values.systemKey,
          name: values.name,
          fieldType: values.fieldType,
          required: values.required,
          optionsJson: values.optionsJson,
          defaultValueJson: values.defaultValueJson,
          sortOrder: values.sortOrder,
          updatedAt: values.updatedAt,
        },
      });
  }
}

export async function applyRemoteDocsSupertagFields(
  rows: DocsSupertagField[],
  authoritativeIds?: string[],
): Promise<void> {
  const db = getDb();
  for (const sf of rows) {
    await db
      .insert(schema.knowledgeSupertagFields)
      .values({
        supertagId: sf.supertag_id,
        fieldId: sf.field_id,
        sortOrder: sf.sort_order ?? null,
        required: Boolean(sf.required),
        showInTemplate: Boolean(sf.show_in_template),
        optional: Boolean(sf.optional),
        createdAt: sf.created_at ?? null,
      })
      .onConflictDoUpdate({
        target: [
          schema.knowledgeSupertagFields.supertagId,
          schema.knowledgeSupertagFields.fieldId,
        ],
        set: {
          sortOrder: sf.sort_order ?? null,
          required: Boolean(sf.required),
          showInTemplate: Boolean(sf.show_in_template),
          optional: Boolean(sf.optional),
          createdAt: sf.created_at ?? null,
        },
      });
  }
  if (authoritativeIds) {
    const authoritative = new Set(authoritativeIds);
    const local = await db.select().from(schema.knowledgeSupertagFields);
    for (const row of local) {
      if (!authoritative.has(`${row.supertagId}:${row.fieldId}`)) {
        await db
          .delete(schema.knowledgeSupertagFields)
          .where(
            and(
              eq(schema.knowledgeSupertagFields.supertagId, row.supertagId),
              eq(schema.knowledgeSupertagFields.fieldId, row.fieldId),
            ),
          );
      }
    }
  }
}

export async function applyRemoteDocsNodeSupertags(
  rows: DocsNodeSupertag[],
  authoritativeIds?: string[],
  options: { force?: boolean } = {},
): Promise<void> {
  const db = getDb();
  for (const ns of rows) {
    const entityId = `${ns.node_id}:${ns.supertag_id}`;
    if (!options.force && await shouldPreserveRemoteDocsRow("knowledge_node_supertags", entityId, ns)) {
      continue;
    }
    await db
      .insert(schema.knowledgeNodeSupertags)
      .values({
        nodeId: ns.node_id,
        supertagId: ns.supertag_id,
        createdAt: ns.created_at ?? null,
        updatedAt: ns.updated_at ?? ns.created_at ?? null,
        serverUpdatedAt: ns.updated_at ?? ns.created_at ?? null,
        dirty: false,
        conflictPayload: null,
        createdBy: ns.created_by ?? null,
      })
      .onConflictDoUpdate({
        target: [
          schema.knowledgeNodeSupertags.nodeId,
          schema.knowledgeNodeSupertags.supertagId,
        ],
        set: {
          createdAt: ns.created_at ?? null,
          updatedAt: ns.updated_at ?? ns.created_at ?? null,
          serverUpdatedAt: ns.updated_at ?? ns.created_at ?? null,
          dirty: false,
          conflictPayload: null,
          createdBy: ns.created_by ?? null,
        },
      });
  }
  if (authoritativeIds) {
    const authoritative = new Set(authoritativeIds);
    const local = await db.select().from(schema.knowledgeNodeSupertags);
    for (const row of local) {
      if (!authoritative.has(`${row.nodeId}:${row.supertagId}`)) {
        const entityId = `${row.nodeId}:${row.supertagId}`;
        if (await shouldPreserveRemoteDocsRow("knowledge_node_supertags", entityId, {
            node_id: row.nodeId,
            supertag_id: row.supertagId,
            deleted: true,
          })) {
          continue;
        }
        await db
          .delete(schema.knowledgeNodeSupertags)
          .where(
            and(
              eq(schema.knowledgeNodeSupertags.nodeId, row.nodeId),
              eq(schema.knowledgeNodeSupertags.supertagId, row.supertagId),
            ),
          );
      }
    }
  }
}

export async function applyRemoteDocsFieldValues(
  rows: DocsFieldValue[],
  authoritativeIds?: string[],
  options: { force?: boolean } = {},
): Promise<void> {
  const db = getDb();
  const now = new Date().toISOString();
  for (const v of rows) {
    const entityId = `${v.node_id}:${v.field_id}`;
    if (!options.force && await shouldPreserveRemoteDocsRow("knowledge_field_values", entityId, v)) {
      continue;
    }
    await db
      .insert(schema.knowledgeFieldValues)
      .values({
        nodeId: v.node_id,
        fieldId: v.field_id,
        valueJson: (v.value_json as unknown) ?? null,
        valueText: v.value_text ?? null,
        valueNumber: v.value_number ?? null,
        valueDatetime: v.value_datetime ?? null,
        targetNodeId: v.target_node_id ?? null,
        updatedAt: v.updated_at ?? now,
        serverUpdatedAt: v.updated_at ?? now,
        dirty: false,
        conflictPayload: null,
        updatedBy: v.updated_by ?? null,
      })
      .onConflictDoUpdate({
        target: [
          schema.knowledgeFieldValues.nodeId,
          schema.knowledgeFieldValues.fieldId,
        ],
        set: {
          valueJson: (v.value_json as unknown) ?? null,
          valueText: v.value_text ?? null,
          valueNumber: v.value_number ?? null,
          valueDatetime: v.value_datetime ?? null,
          targetNodeId: v.target_node_id ?? null,
          updatedAt: v.updated_at ?? now,
          serverUpdatedAt: v.updated_at ?? now,
          dirty: false,
          conflictPayload: null,
          updatedBy: v.updated_by ?? null,
        },
      });
  }
  if (authoritativeIds) {
    // サーバ権威セットに無い行（Web 等でクリア済み）をローカルから削除する。
    const authoritative = new Set(authoritativeIds);
    const local = await db
      .select({
        nodeId: schema.knowledgeFieldValues.nodeId,
        fieldId: schema.knowledgeFieldValues.fieldId,
      })
      .from(schema.knowledgeFieldValues);
    for (const row of local) {
      if (!authoritative.has(`${row.nodeId}:${row.fieldId}`)) {
        const entityId = `${row.nodeId}:${row.fieldId}`;
        if (await shouldPreserveRemoteDocsRow("knowledge_field_values", entityId, {
            node_id: row.nodeId,
            field_id: row.fieldId,
            deleted: true,
          })) {
          continue;
        }
        await db
          .delete(schema.knowledgeFieldValues)
          .where(
            and(
              eq(schema.knowledgeFieldValues.nodeId, row.nodeId),
              eq(schema.knowledgeFieldValues.fieldId, row.fieldId),
            ),
          );
      }
    }
  }
}

/** push 応答が deleted=true の field_value をローカルから物理削除する。 */
export async function deleteLocalDocsFieldValue(
  nodeId: string,
  fieldId: string,
): Promise<void> {
  const db = getDb();
  await db
    .delete(schema.knowledgeFieldValues)
    .where(
      and(
        eq(schema.knowledgeFieldValues.nodeId, nodeId),
        eq(schema.knowledgeFieldValues.fieldId, fieldId),
      ),
    );
}

/** push 応答が deleted=true の node_supertag をローカルから物理削除する。 */
export async function deleteLocalDocsNodeSupertag(
  nodeId: string,
  supertagId: string,
): Promise<void> {
  const db = getDb();
  await db
    .delete(schema.knowledgeNodeSupertags)
    .where(
      and(
        eq(schema.knowledgeNodeSupertags.nodeId, nodeId),
        eq(schema.knowledgeNodeSupertags.supertagId, supertagId),
      ),
    );
}

export async function applyRemoteDocsPlacements(
  rows: DocsNodePlacement[],
  authoritativeIds?: string[],
): Promise<void> {
  const db = getDb();
  for (const p of rows) {
    await db
      .insert(schema.knowledgeNodePlacements)
      .values({
        id: p.id,
        nodeId: p.node_id,
        parentNodeId: p.parent_node_id,
        sortOrder: p.sort_order ?? null,
        collapsed: Boolean(p.collapsed),
        createdBy: p.created_by ?? null,
        createdAt: p.created_at ?? null,
      })
      .onConflictDoUpdate({
        target: schema.knowledgeNodePlacements.id,
        set: {
          nodeId: p.node_id,
          parentNodeId: p.parent_node_id,
          sortOrder: p.sort_order ?? null,
          collapsed: Boolean(p.collapsed),
          createdBy: p.created_by ?? null,
          createdAt: p.created_at ?? null,
        },
      });
  }
  if (authoritativeIds) {
    const authoritative = new Set(authoritativeIds);
    const local = await db
      .select({ id: schema.knowledgeNodePlacements.id })
      .from(schema.knowledgeNodePlacements);
    const missing = local
      .map((row) => row.id)
      .filter((id) => !authoritative.has(id));
    if (missing.length) {
      await db
        .delete(schema.knowledgeNodePlacements)
        .where(inArray(schema.knowledgeNodePlacements.id, missing));
    }
  }
}

export async function applyRemoteDocsEdges(
  rows: DocsEdge[],
  authoritativeIds?: string[],
): Promise<void> {
  const db = getDb();
  for (const e of rows) {
    await db
      .insert(schema.knowledgeEdges)
      .values({
        id: e.id,
        sourceNodeId: e.source_node_id,
        targetNodeId: e.target_node_id,
        relationType: e.relation_type ?? null,
        confidence: e.confidence ?? null,
        createdBy: e.created_by ?? null,
        createdAt: e.created_at ?? null,
      })
      .onConflictDoUpdate({
        target: schema.knowledgeEdges.id,
        set: {
          sourceNodeId: e.source_node_id,
          targetNodeId: e.target_node_id,
          relationType: e.relation_type ?? null,
          confidence: e.confidence ?? null,
          createdBy: e.created_by ?? null,
          createdAt: e.created_at ?? null,
        },
      });
  }
  if (authoritativeIds) {
    const authoritative = new Set(authoritativeIds);
    const local = await db
      .select({ id: schema.knowledgeEdges.id })
      .from(schema.knowledgeEdges);
    const missing = local
      .map((row) => row.id)
      .filter((id) => !authoritative.has(id));
    if (missing.length) {
      await db
        .delete(schema.knowledgeEdges)
        .where(inArray(schema.knowledgeEdges.id, missing));
    }
  }
}

// ---------- 書き込みヘルパ（ローカル反映 + outbox） ----------

async function hasToken(): Promise<boolean> {
  return Boolean(await getToken());
}

/** 兄弟末尾の次の sort_order を返す（parentId=null はトップレベル）。 */
async function nextSortOrder(parentId: string | null): Promise<number> {
  const db = getDb();
  const rows = parentId
    ? await db
        .select({ sortOrder: schema.knowledgeNodes.sortOrder })
        .from(schema.knowledgeNodes)
        .where(eq(schema.knowledgeNodes.parentId, parentId))
    : await db
        .select({ sortOrder: schema.knowledgeNodes.sortOrder })
        .from(schema.knowledgeNodes)
        .where(isNull(schema.knowledgeNodes.parentId));
  let max = 0;
  for (const row of rows) {
    if (typeof row.sortOrder === "number" && row.sortOrder > max) {
      max = row.sortOrder;
    }
  }
  return max + 1;
}

async function getNodeRow(id: string): Promise<DbNode | null> {
  const db = getDb();
  const rows = await db
    .select()
    .from(schema.knowledgeNodes)
    .where(eq(schema.knowledgeNodes.id, id));
  return rows[0] ?? null;
}

// ---------- docsRepo ----------

export const docsRepo = {
  // ===== 読み取り（ローカルファースト） =====

  /** トップレベルページ一覧（parent_id null / archived_at null / 通常ノードのみ）。 */
  async listPages(): Promise<DocsNode[]> {
    const db = getDb();
    const rows = await db
      .select()
      .from(schema.knowledgeNodes)
      .where(
        and(
          isNull(schema.knowledgeNodes.parentId),
          isNull(schema.knowledgeNodes.archivedAt),
          eq(schema.knowledgeNodes.nodeType, "node"),
        ),
      );
    return rows.map(toNode).sort(sortBySortThenTitle);
  },

  /** 子ノード一覧（sort_order 昇順、archived を除外）。 */
  async listChildren(parentId: string): Promise<DocsNode[]> {
    const db = getDb();
    const rows = await db
      .select()
      .from(schema.knowledgeNodes)
      .where(
        and(
          eq(schema.knowledgeNodes.parentId, parentId),
          isNull(schema.knowledgeNodes.archivedAt),
        ),
      )
      .orderBy(asc(schema.knowledgeNodes.sortOrder));
    return rows.map(toNode).sort(sortBySortThenTitle);
  },

  /** ルート配下のアウトラインを1クエリで取得する。 */
  async listOutline(rootNodeId: string, includeArchived = false): Promise<DocsNode[]> {
    const db = getDb();
    const root = await getNodeRow(rootNodeId);
    const pageRootId = root?.rootPageId ?? root?.id ?? rootNodeId;
    const rows = await db
      .select()
      .from(schema.knowledgeNodes)
      .where(
        or(
          eq(schema.knowledgeNodes.rootPageId, pageRootId),
          eq(schema.knowledgeNodes.parentId, rootNodeId),
        ),
      );
    return rows
      .map(toNode)
      .filter((node) => node.id !== rootNodeId)
      .filter((node) => includeArchived || !node.archived_at);
  },

  async getNode(id: string): Promise<DocsNode | null> {
    const row = await getNodeRow(id);
    return row ? toNode(row) : null;
  },

  /** ノードに付与されたスーパータグ一覧。 */
  async getNodeTags(nodeId: string): Promise<DocsSupertag[]> {
    const db = getDb();
    const rows = await db
      .select({ supertag: schema.knowledgeSupertags })
      .from(schema.knowledgeNodeSupertags)
      .innerJoin(
        schema.knowledgeSupertags,
        eq(
          schema.knowledgeNodeSupertags.supertagId,
          schema.knowledgeSupertags.id,
        ),
      )
      .where(eq(schema.knowledgeNodeSupertags.nodeId, nodeId));
    return rows.map((r) => toSupertag(r.supertag));
  },

  /** ノードのタグに紐づくフィールド定義と現在値のペア一覧。 */
  async getNodeFieldValues(
    nodeId: string,
  ): Promise<Array<{ field: DocsField; value: DocsFieldValue | null }>> {
    const db = getDb();
    const tagRows = await db
      .select({ supertagId: schema.knowledgeNodeSupertags.supertagId })
      .from(schema.knowledgeNodeSupertags)
      .where(eq(schema.knowledgeNodeSupertags.nodeId, nodeId));
    const supertagIds = tagRows.map((r) => r.supertagId);
    if (!supertagIds.length) return [];

    const stFieldRows = await db
      .select()
      .from(schema.knowledgeSupertagFields)
      .where(inArray(schema.knowledgeSupertagFields.supertagId, supertagIds));
    const fieldIds = Array.from(
      new Set(stFieldRows.map((r) => r.fieldId)),
    );
    if (!fieldIds.length) return [];

    const fieldRows = await db
      .select()
      .from(schema.knowledgeFields)
      .where(inArray(schema.knowledgeFields.id, fieldIds));
    const valueRows = await db
      .select()
      .from(schema.knowledgeFieldValues)
      .where(eq(schema.knowledgeFieldValues.nodeId, nodeId));

    const valueByField = new Map<string, DbFieldValue>();
    for (const v of valueRows) valueByField.set(v.fieldId, v);
    const sortByField = new Map<string, number>();
    for (const sf of stFieldRows) {
      if (typeof sf.sortOrder === "number") {
        sortByField.set(sf.fieldId, sf.sortOrder);
      }
    }

    return fieldRows
      .map((row) => {
        const value = valueByField.get(row.id);
        return {
          field: toField(row),
          value: value ? toFieldValue(value) : null,
        };
      })
      .sort((a, b) => {
        const aSort = sortByField.get(a.field.id) ?? a.field.sort_order ?? 0;
        const bSort = sortByField.get(b.field.id) ?? b.field.sort_order ?? 0;
        return aSort - bSort;
      });
  },

  /** ローカルに存在するスーパータグ全件（name 昇順）。タグ選択 UI 用。 */
  async listSupertags(): Promise<DocsSupertag[]> {
    const db = getDb();
    const rows = await db.select().from(schema.knowledgeSupertags);
    return rows
      .map(toSupertag)
      .sort((a, b) =>
        String(a.name ?? "").localeCompare(String(b.name ?? "")),
      );
  },

  /** バックリンク（target=nodeId のエッジ元ノード）。 */
  async getBacklinks(nodeId: string): Promise<DocsNode[]> {
    const db = getDb();
    const rows = await db
      .select({ node: schema.knowledgeNodes })
      .from(schema.knowledgeEdges)
      .innerJoin(
        schema.knowledgeNodes,
        eq(schema.knowledgeEdges.sourceNodeId, schema.knowledgeNodes.id),
      )
      .where(eq(schema.knowledgeEdges.targetNodeId, nodeId));
    return rows.map((r) => toNode(r.node));
  },

  /** オフライン検索（title / description の部分一致）。 */
  async searchLocal(q: string): Promise<DocsNode[]> {
    const term = q.trim();
    if (!term) return [];
    const db = getDb();
    const pattern = `%${term}%`;
    const rows = await db
      .select()
      .from(schema.knowledgeNodes)
      .where(
        and(
          isNull(schema.knowledgeNodes.archivedAt),
          or(
            like(schema.knowledgeNodes.title, pattern),
            like(schema.knowledgeNodes.description, pattern),
          ),
        ),
      );
    return rows.map(toNode).sort(sortBySortThenTitle);
  },

  // ===== 書き込み（ローカル反映 + outbox） =====

  async createNode(input: {
    parentId?: string | null;
    projectId?: string | null;
    title: string;
    description?: string;
    nodeType?: string;
    dayDate?: string | null;
    sortOrder?: number;
  }): Promise<DocsNode> {
    const db = getDb();
    const id = randomId();
    const now = new Date().toISOString();
    const parentId = input.parentId ?? null;
    const sortOrder =
      typeof input.sortOrder === "number"
        ? input.sortOrder
        : await nextSortOrder(parentId);
    // root_page_id はサーバ権威。ローカルは親から推定し、pull で正規化される。
    let rootPageId: string | null = null;
    if (parentId) {
      const parent = await getNodeRow(parentId);
      rootPageId = parent?.rootPageId ?? parent?.id ?? null;
    }
    const nodeType = input.nodeType ?? "node";
    const node: DocsNode = {
      id,
      workspace_id: null,
      parent_id: parentId,
      root_page_id: rootPageId,
      project_id: input.projectId ?? null,
      system_key: null,
      title: input.title ?? "",
      aliases: [],
      description: input.description ?? null,
      body_json: null,
      body_text: null,
      node_type: nodeType as DocsNode["node_type"],
      display_props: null,
      query_json: null,
      view_json: null,
      day_date: input.dayDate ?? null,
      sort_order: sortOrder,
      created_by: null,
      updated_by: null,
      created_at: now,
      updated_at: now,
      archived_at: null,
    };
    await applyRemoteDocsNodes([node]);
    await db
      .update(schema.knowledgeNodes)
      .set({ dirty: true, serverUpdatedAt: null })
      .where(eq(schema.knowledgeNodes.id, id));
    if (await hasToken()) {
      await enqueueOutbox({
        table: "knowledge_nodes",
        action: "create",
        entityId: id,
        payload: {
          id,
          parent_id: parentId,
          project_id: input.projectId ?? null,
          title: node.title,
          description: node.description,
          node_type: nodeType,
          day_date: node.day_date,
          sort_order: sortOrder,
        },
      });
    }
    return node;
  },

  /** 現在行と同じ階層の直後へノードを作成する。 */
  async createSiblingAfter(id: string, title = ""): Promise<DocsNode> {
    const current = await this.getNode(id);
    if (!current) throw new Error("基準ノードが見つかりません");
    const siblings = current.parent_id
      ? await this.listChildren(current.parent_id)
      : [];
    const index = siblings.findIndex((node) => node.id === id);
    const nextSibling = index >= 0 ? siblings[index + 1] ?? null : null;
    let currentOrder = current.sort_order ?? Math.max(index, 0);
    let nextOrder = nextSibling?.sort_order ?? null;
    let sortOrder =
      nextOrder === null
        ? currentOrder + 1
        : currentOrder + (nextOrder - currentOrder) / 2;

    if (
      !Number.isFinite(sortOrder) ||
      sortOrder <= currentOrder ||
      (nextOrder !== null && sortOrder >= nextOrder)
    ) {
      for (let siblingIndex = 0; siblingIndex < siblings.length; siblingIndex += 1) {
        await this.updateNode(siblings[siblingIndex].id, {
          sortOrder: (siblingIndex + 1) * 1024,
        });
      }
      currentOrder = (Math.max(index, 0) + 1) * 1024;
      nextOrder = index >= 0 && index + 1 < siblings.length ? currentOrder + 1024 : null;
      sortOrder = nextOrder === null ? currentOrder + 1024 : (currentOrder + nextOrder) / 2;
    }

    return this.createNode({
      parentId: current.parent_id,
      projectId: current.project_id,
      title,
      sortOrder,
    });
  },

  async updateNode(
    id: string,
    patch: {
      title?: string;
      description?: string;
      bodyJson?: object;
      sortOrder?: number;
      projectId?: string | null;
    },
  ): Promise<DocsNode> {
    const db = getDb();
    const before = await getNodeRow(id);
    const now = new Date().toISOString();
    const localSet: Partial<typeof schema.knowledgeNodes.$inferInsert> = {
      updatedAt: now,
      dirty: true,
    };
    const payload: Record<string, unknown> = {};
    if ("title" in patch && patch.title !== undefined) {
      localSet.title = patch.title;
      payload.title = patch.title;
    }
    if ("description" in patch && patch.description !== undefined) {
      localSet.description = patch.description;
      payload.description = patch.description;
    }
    if ("bodyJson" in patch && patch.bodyJson !== undefined) {
      localSet.bodyJson = patch.bodyJson as unknown;
      payload.body_json = patch.bodyJson;
    }
    if ("sortOrder" in patch && patch.sortOrder !== undefined) {
      localSet.sortOrder = patch.sortOrder;
      payload.sort_order = patch.sortOrder;
    }
    if ("projectId" in patch && patch.projectId !== undefined) {
      localSet.projectId = patch.projectId ?? null;
      payload.project_id = patch.projectId ?? null;
    }
    await db
      .update(schema.knowledgeNodes)
      .set(localSet)
      .where(eq(schema.knowledgeNodes.id, id));
    if (Object.keys(payload).length && (await hasToken())) {
      await enqueueOutbox({
        table: "knowledge_nodes",
        action: "update",
        entityId: id,
        payload,
        baseUpdatedAt: before?.serverUpdatedAt ?? null,
        basePayload: before ? toNode(before) : null,
      });
    }
    const after = await getNodeRow(id);
    return after ? toNode(after) : (await this.getNode(id))!;
  },

  async moveNode(
    id: string,
    newParentId: string,
    sortOrder?: number,
    leaveReference?: boolean,
  ): Promise<void> {
    const db = getDb();
    const before = await getNodeRow(id);
    const newParent = await getNodeRow(newParentId);
    const nextRootPageId = newParent?.rootPageId ?? newParent?.id ?? newParentId;
    const now = new Date().toISOString();
    const nextSort =
      typeof sortOrder === "number"
        ? sortOrder
        : await nextSortOrder(newParentId);
    const placements = await db
      .select({ id: schema.knowledgeNodes.id, parentId: schema.knowledgeNodes.parentId })
      .from(schema.knowledgeNodes);
    const descendants: string[] = [];
    const pendingParents = [id];
    while (pendingParents.length) {
      const parentId = pendingParents.shift()!;
      for (const placement of placements) {
        if (placement.parentId !== parentId || descendants.includes(placement.id)) continue;
        descendants.push(placement.id);
        pendingParents.push(placement.id);
      }
    }
    await db
      .update(schema.knowledgeNodes)
      .set({ rootPageId: nextRootPageId })
      .where(inArray(schema.knowledgeNodes.id, [id, ...descendants]));
    await db
      .update(schema.knowledgeNodes)
      .set({ parentId: newParentId, sortOrder: nextSort, updatedAt: now, dirty: true })
      .where(eq(schema.knowledgeNodes.id, id));
    if (await hasToken()) {
      const payload: Record<string, unknown> = {
        parent_id: newParentId,
        sort_order: nextSort,
      };
      if (leaveReference) payload.leave_reference = true;
      await enqueueOutbox({
        table: "knowledge_nodes",
        action: "update",
        entityId: id,
        payload,
        baseUpdatedAt: before?.serverUpdatedAt ?? null,
        basePayload: before ? toNode(before) : null,
      });
    }
  },

  /** インデント: 直前の兄弟を新しい親にする。 */
  async indentNode(id: string): Promise<void> {
    const node = await getNodeRow(id);
    if (!node) return;
    const db = getDb();
    const siblings = node.parentId
      ? await db
          .select()
          .from(schema.knowledgeNodes)
          .where(
            and(
              eq(schema.knowledgeNodes.parentId, node.parentId),
              isNull(schema.knowledgeNodes.archivedAt),
            ),
          )
      : await db
          .select()
          .from(schema.knowledgeNodes)
          .where(
            and(
              isNull(schema.knowledgeNodes.parentId),
              isNull(schema.knowledgeNodes.archivedAt),
            ),
          );
    const ordered = siblings
      .map(toNode)
      .sort(sortBySortThenTitle)
      .map((n) => n.id);
    const index = ordered.indexOf(id);
    if (index <= 0) return; // 先頭はインデント不可
    const prevSiblingId = ordered[index - 1];
    await this.moveNode(id, prevSiblingId);
  },

  /** アウトデント: 祖父を新しい親にする。 */
  async outdentNode(id: string): Promise<void> {
    const node = await getNodeRow(id);
    if (!node || !node.parentId) return; // トップレベルはアウトデント不可
    const parent = await getNodeRow(node.parentId);
    if (!parent) return;
    if (!parent.parentId) {
      // 親がトップレベル → 自身もトップレベルへ
      const db = getDb();
      const before = node;
      const now = new Date().toISOString();
      const nextSort = await nextSortOrder(null);
      await db
        .update(schema.knowledgeNodes)
        .set({ parentId: null, sortOrder: nextSort, updatedAt: now, dirty: true })
        .where(eq(schema.knowledgeNodes.id, id));
      if (await hasToken()) {
        await enqueueOutbox({
          table: "knowledge_nodes",
          action: "update",
          entityId: id,
          payload: { parent_id: null, sort_order: nextSort },
          baseUpdatedAt: before.serverUpdatedAt ?? null,
          basePayload: toNode(before),
        });
      }
      return;
    }
    await this.moveNode(id, parent.parentId);
  },

  async archiveNode(id: string): Promise<void> {
    const db = getDb();
    const before = await getNodeRow(id);
    const now = new Date().toISOString();
    // ローカルは archivedAt を立てる（deletedAt は立てない: アーカイブ表示のため）。
    await db
      .update(schema.knowledgeNodes)
      .set({ archivedAt: now, updatedAt: now, dirty: true })
      .where(eq(schema.knowledgeNodes.id, id));
    if (await hasToken()) {
      await enqueueOutbox({
        table: "knowledge_nodes",
        action: "delete",
        entityId: id,
        payload: {},
        baseUpdatedAt: before?.serverUpdatedAt ?? null,
        basePayload: before ? toNode(before) : null,
      });
    }
  },

  async addTag(
    nodeId: string,
    opts: { supertagId?: string; name?: string },
  ): Promise<void> {
    const db = getDb();
    const now = new Date().toISOString();
    const before = opts.supertagId
      ? (
          await db
            .select()
            .from(schema.knowledgeNodeSupertags)
            .where(
              and(
                eq(schema.knowledgeNodeSupertags.nodeId, nodeId),
                eq(schema.knowledgeNodeSupertags.supertagId, opts.supertagId),
              ),
            )
        )[0]
      : undefined;
    // supertagId が既知なら楽観的にローカル反映。name のみ（新規タグ）はサーバ
    // 解決に委ね、pull で回収する。
    if (opts.supertagId) {
      await db
        .insert(schema.knowledgeNodeSupertags)
        .values({
          nodeId,
          supertagId: opts.supertagId,
          createdAt: now,
          updatedAt: now,
          dirty: true,
          createdBy: null,
        })
        .onConflictDoUpdate({
          target: [
            schema.knowledgeNodeSupertags.nodeId,
            schema.knowledgeNodeSupertags.supertagId,
          ],
          set: { createdAt: now, updatedAt: now, dirty: true },
        });
    }
    if (await hasToken()) {
      const key = opts.supertagId ?? "new";
      await enqueueOutbox({
        table: "knowledge_node_supertags",
        action: "create",
        entityId: `${nodeId}:${key}`,
        payload: {
          node_id: nodeId,
          ...(opts.supertagId ? { supertag_id: opts.supertagId } : {}),
          ...(opts.name ? { name: opts.name } : {}),
        },
        baseUpdatedAt: before?.serverUpdatedAt ?? null,
        basePayload: before ?? null,
      });
    }
  },

  async removeTag(nodeId: string, supertagId: string): Promise<void> {
    const db = getDb();
    const before = (
      await db
        .select()
        .from(schema.knowledgeNodeSupertags)
        .where(
          and(
            eq(schema.knowledgeNodeSupertags.nodeId, nodeId),
            eq(schema.knowledgeNodeSupertags.supertagId, supertagId),
          ),
        )
    )[0];
    await db
      .delete(schema.knowledgeNodeSupertags)
      .where(
        and(
          eq(schema.knowledgeNodeSupertags.nodeId, nodeId),
          eq(schema.knowledgeNodeSupertags.supertagId, supertagId),
        ),
      );
    if (await hasToken()) {
      await enqueueOutbox({
        table: "knowledge_node_supertags",
        action: "delete",
        entityId: `${nodeId}:${supertagId}`,
        payload: { node_id: nodeId, supertag_id: supertagId },
        baseUpdatedAt: before?.serverUpdatedAt ?? null,
        basePayload: before ?? null,
      });
    }
  },

  async setField(
    nodeId: string,
    fieldId: string,
    value: unknown,
  ): Promise<void> {
    const db = getDb();
    const now = new Date().toISOString();
    const before = (
      await db
        .select()
        .from(schema.knowledgeFieldValues)
        .where(
          and(
            eq(schema.knowledgeFieldValues.nodeId, nodeId),
            eq(schema.knowledgeFieldValues.fieldId, fieldId),
          ),
        )
    )[0];
    const isEmpty = value === null || value === undefined || value === "";
    if (isEmpty) {
      // 空値はローカルから削除（サーバも update→delete にマップ）。
      await db
        .delete(schema.knowledgeFieldValues)
        .where(
          and(
            eq(schema.knowledgeFieldValues.nodeId, nodeId),
            eq(schema.knowledgeFieldValues.fieldId, fieldId),
          ),
        );
    } else {
      // 型別の派生列はサーバ権威。ローカルは即時表示用に best-effort で格納。
      // checkbox はサーバ格納形（value_json = { value: bool }）に合わせる。
      const valueText = typeof value === "string" ? value : null;
      const valueNumber = typeof value === "number" ? value : null;
      const valueJson =
        typeof value === "boolean" ? { value } : (value as unknown);
      await db
        .insert(schema.knowledgeFieldValues)
        .values({
          nodeId,
          fieldId,
          valueJson,
          valueText,
          valueNumber,
          valueDatetime: null,
          targetNodeId: null,
          updatedAt: now,
          serverUpdatedAt: before?.serverUpdatedAt ?? null,
          dirty: true,
          updatedBy: null,
        })
        .onConflictDoUpdate({
          target: [
            schema.knowledgeFieldValues.nodeId,
            schema.knowledgeFieldValues.fieldId,
          ],
          set: {
            valueJson,
            valueText,
            valueNumber,
            updatedAt: now,
            dirty: true,
          },
        });
    }
    if (await hasToken()) {
      await enqueueOutbox({
        table: "knowledge_field_values",
        action: "update",
        entityId: `${nodeId}:${fieldId}`,
        payload: { node_id: nodeId, field_id: fieldId, value },
        baseUpdatedAt: before?.serverUpdatedAt ?? null,
        basePayload: before ?? null,
      });
    }
  },

  async createSupertag(input: {
    name: string;
    baseType?: string;
    color?: string;
    icon?: string;
  }): Promise<DocsSupertag> {
    const id = randomId();
    const now = new Date().toISOString();
    const supertag: DocsSupertag = {
      id,
      workspace_id: null,
      parent_supertag_id: null,
      system_key: null,
      name: input.name,
      base_type: input.baseType ?? null,
      description: null,
      icon: input.icon ?? null,
      color: input.color ?? null,
      template_json: null,
      pinned_field_ids: [],
      config_json: null,
      title_template: null,
      ai_instructions: null,
      created_at: now,
      updated_at: now,
    };
    await applyRemoteDocsSupertags([supertag]);
    const db = getDb();
    await db
      .update(schema.knowledgeSupertags)
      .set({ dirty: true, serverUpdatedAt: null })
      .where(eq(schema.knowledgeSupertags.id, id));
    if (await hasToken()) {
      await enqueueOutbox({
        table: "knowledge_supertags",
        action: "create",
        entityId: id,
        payload: {
          id,
          name: input.name,
          base_type: input.baseType ?? null,
          color: input.color ?? null,
          icon: input.icon ?? null,
        },
      });
    }
    return supertag;
  },
};

function sortBySortThenTitle(a: DocsNode, b: DocsNode): number {
  const aSort =
    typeof a.sort_order === "number" ? a.sort_order : Number.POSITIVE_INFINITY;
  const bSort =
    typeof b.sort_order === "number" ? b.sort_order : Number.POSITIVE_INFINITY;
  if (aSort !== bSort) return aSort - bSort;
  return String(a.title ?? "").localeCompare(String(b.title ?? ""));
}
