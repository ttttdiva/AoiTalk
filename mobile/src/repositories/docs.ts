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

import * as drizzleOrm from "drizzle-orm";
import { and, asc, eq, gt, inArray, isNull, like, or } from "drizzle-orm";
import { getDb, schema } from "../db/client";
import {
  docsSqliteAsyncAvailable,
  encodeDocsBoolean,
  encodeDocsJson,
  type DocsSqliteAsyncTransaction,
  withDocsExclusiveTransaction,
} from "../db/docs-sync-async";
import { getToken, getTokenAuthScope } from "../lib/auth";
import { canonicalizeIngestUrl, isSafeSourceRefUrl } from "../lib/clip-url";
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

async function getSavedDocsWorkspaceIds(): Promise<string[]> {
  const token = await getToken();
  if (!token) return [];
  const authScope = getTokenAuthScope(token);
  const userId = authScope.slice("auth:".length);
  if (!userId) return [];
  const db = getDb();
  const rows = await db
    .select({ tableName: schema.syncState.tableName, cursor: schema.syncState.cursor })
    .from(schema.syncState)
    .where(like(schema.syncState.tableName, `docs_workspace:v2:${userId}%`));
  return rows
    .filter((row) => row.cursor)
    .map((row) => row.cursor as string);
}

/** Build an account/scope visibility predicate for local Docs reads. */
async function getDocsNodeVisibilityCondition(): Promise<unknown> {
  let workspaceIds: string[] = [];
  try {
    workspaceIds = await getSavedDocsWorkspaceIds();
  } catch {
    // A legacy/in-memory database may not have sync_state yet.  Fail closed
    // for clean rows rather than returning another account's cache.
    workspaceIds = [];
  }
  if (workspaceIds.length === 1) {
    return or(
      eq(schema.knowledgeNodes.workspaceId, workspaceIds[0]),
      eq(schema.knowledgeNodes.dirty, true),
    );
  }
  if (workspaceIds.length > 1) {
    return or(
      inArray(schema.knowledgeNodes.workspaceId, workspaceIds),
      eq(schema.knowledgeNodes.dirty, true),
    );
  }
  return eq(schema.knowledgeNodes.dirty, true);
}

type DocsVisibility = {
  membershipAvailable: boolean;
  entityKeys: Set<string>;
  dirtyEntityKeys: Set<string>;
  workspaceIds: string[];
};

/**
 * Relation rows are often created optimistically before the first validated
 * pull can persist a membership row.  Keep those rows visible to the current
 * account when their outbox operation is present, while still hiding clean
 * rows that only belong to a sibling scope.  `entityKeys` contains the
 * active/readonly server projection; `dirtyEntityKeys` is the conservative
 * local-edit projection loaded from this account's outbox.
 */
function docsRelationVisible(
  entityKey: string,
  visibility: DocsVisibility,
): boolean {
  return !visibility.membershipAvailable
    || visibility.entityKeys.has(entityKey)
    || visibility.dirtyEntityKeys.has(entityKey);
}

/**
 * Resolve the current account's active/readonly entity projection.  Keeping
 * this as a small JS-side filter lets legacy SQLite/test doubles continue to
 * work while the real database enforces composite (library, project) keys.
 */
async function getDocsVisibility(
  tableName: string,
  allowedScopeKeys?: ReadonlySet<string>,
): Promise<DocsVisibility> {
  let workspaceIds: string[] = [];
  try {
    workspaceIds = await getSavedDocsWorkspaceIds();
  } catch {
    workspaceIds = [];
  }
  if (!schema.docsScopeMembership) {
    return {
      membershipAvailable: false,
      entityKeys: new Set(),
      dirtyEntityKeys: new Set(),
      workspaceIds,
    };
  }
  let authScope: string | null = null;
  try {
    const token = await getToken();
    authScope = token && typeof getTokenAuthScope === "function"
      ? getTokenAuthScope(token)
      : null;
    if (!authScope) {
      return {
        membershipAvailable: true,
        entityKeys: new Set(),
        dirtyEntityKeys: new Set(),
        workspaceIds,
      };
    }
    const db = getDb();
    const rows = await db
      .select({
        entityKey: schema.docsScopeMembership.entityKey,
        scopeKey: schema.docsScopeMembership.scopeKey,
      })
      .from(schema.docsScopeMembership)
      .where(
        and(
          eq(schema.docsScopeMembership.authScope, authScope),
          eq(schema.docsScopeMembership.tableName, tableName),
          or(
            eq(schema.docsScopeMembership.state, "active"),
            eq(schema.docsScopeMembership.state, "readonly"),
          ),
        ),
      );
    const activeScopeKeys = new Set(
      rows
        .map((row) => row.scopeKey)
        .filter((scopeKey): scopeKey is string => typeof scopeKey === "string" && scopeKey.length > 0),
    );
    const visibleRows = allowedScopeKeys?.size
      ? rows.filter((row) =>
          typeof row.scopeKey === "string" && allowedScopeKeys.has(row.scopeKey),
        )
      : rows;
    const visibleScopeKeys = allowedScopeKeys?.size
      ? new Set(
          [...activeScopeKeys].filter((scopeKey) => allowedScopeKeys.has(scopeKey)),
        )
      : activeScopeKeys;
    let dirtyEntityKeys = new Set<string>();
    try {
      const dirtyRows = await db
        .select({
          entityId: schema.outbox.entityId,
          docsScopeKey: schema.outbox.docsScopeKey,
          blockedReason: schema.outbox.blockedReason,
        })
        .from(schema.outbox)
        .where(
          and(
            eq(schema.outbox.authScope, authScope),
            eq(schema.outbox.tableName, tableName),
          ),
        );
      // A pending edit protects a relation only when its composite scope is
      // one of the account's active/readonly projections.  Legacy NULL scope
      // rows are intentionally fail-closed here; migration/backfill can
      // resolve them, while an ambiguous sibling UUID must not become visible
      // merely because it has an outbox entry.
      dirtyEntityKeys = new Set(
        dirtyRows
          .filter((row) =>
            row.blockedReason == null
            && (
              (typeof row.docsScopeKey === "string" && visibleScopeKeys.has(row.docsScopeKey))
              // Legacy test/upgrade projections may expose membership without
              // a scope_key yet.  Only retain their NULL-key edits while no
              // active composite scope is known; migration blocks ambiguous
              // rows once the new scope metadata is available.
              || (row.docsScopeKey == null && visibleScopeKeys.size === 0)
            ),
          )
          .map((row) => row.entityId),
      );
    } catch {
      // Older schemas may have auth_scope/outbox projection absent; retain
      // only null-workspace dirty rows in docsRowVisible below.
    }
    return {
      membershipAvailable: true,
      entityKeys: new Set(visibleRows.map((row) => row.entityKey)),
      dirtyEntityKeys,
      workspaceIds,
    };
  } catch {
    // A rolling upgrade may expose the Drizzle symbol before the table exists.
    return {
      membershipAvailable: false,
      entityKeys: new Set(),
      dirtyEntityKeys: new Set(),
      workspaceIds,
    };
  }
}

function docsRowVisible(
  row: { id?: string | null; workspaceId?: string | null; dirty?: boolean | null },
  visibility: DocsVisibility,
): boolean {
  const entityKey = row.id == null ? null : String(row.id);
  if (visibility.membershipAvailable) {
    // Dirty rows with no server workspace are local edits created in the
    // current account; clean rows require an active/readonly membership.
    return Boolean(
      (entityKey && visibility.entityKeys.has(entityKey))
      || (
        row.dirty === true
        && Boolean(entityKey && visibility.dirtyEntityKeys.has(entityKey))
      )
      || (row.dirty === true && !row.workspaceId),
    );
  }
  if (visibility.workspaceIds.length) {
    return Boolean(
      (row.workspaceId && visibility.workspaceIds.includes(row.workspaceId))
      || row.dirty === true,
    );
  }
  return row.dirty === true;
}

/**
 * Resolve the composite scope(s) for a node-centric read.  A live row carries
 * its canonical library/project identity; membership is the fallback for an
 * optimistic local row whose workspace has not been assigned yet.
 */
async function getDocsNodeScopeKeys(
  nodeId: string,
  row?: DbNode | null,
): Promise<Set<string> | undefined> {
  const resolvedRow = row === undefined ? await getNodeRow(nodeId) : row;
  const directScope = resolvedRow && Object.prototype.hasOwnProperty.call(resolvedRow, "projectId")
    ? docsScopeKeyForRow(resolvedRow)
    : null;
  if (directScope) return new Set([directScope]);
  if (!schema.docsScopeMembership) return undefined;
  try {
    const token = await getToken();
    const authScope = token ? getTokenAuthScope(token) : null;
    if (!authScope) return undefined;
    const db = getDb();
    const rows = await db
      .select({ scopeKey: schema.docsScopeMembership.scopeKey })
      .from(schema.docsScopeMembership)
      .where(and(
        eq(schema.docsScopeMembership.authScope, authScope),
        eq(schema.docsScopeMembership.tableName, "knowledge_nodes"),
        eq(schema.docsScopeMembership.entityKey, nodeId),
        or(
          eq(schema.docsScopeMembership.state, "active"),
          eq(schema.docsScopeMembership.state, "readonly"),
        ),
      ));
    const keys = new Set(
      rows
        .map((entry) => entry.scopeKey)
        .filter((scopeKey): scopeKey is string => typeof scopeKey === "string" && scopeKey.length > 0),
    );
    return keys.size ? keys : undefined;
  } catch {
    return undefined;
  }
}

// openDatabaseSync 上では drizzle の await は実質同期実行になるため、数万行の
// apply を 1 マイクロタスクで回すと JS スレッドを数十秒占有する。200 行ごとに
// イベントループへ yield して UI をブロックしない。
// テストからチャンク境界の挙動を検証できるよう公開する（本番値は 200）。
export const DOCS_APPLY_CHUNK_SIZE = 200;

// preserve 判定を伴わない apply/reconcile 用の単純チャンク。
async function forEachDocsChunk<T>(
  items: readonly T[],
  handler: (item: T) => Promise<void>,
): Promise<void> {
  for (let index = 0; index < items.length; index += 1) {
    await handler(items[index]);
    if (
      (index + 1) % DOCS_APPLY_CHUNK_SIZE === 0 &&
      index + 1 < items.length
    ) {
      await new Promise((resolve) => setTimeout(resolve, 0));
    }
  }
}

/**
 * preserve 判定など「開始時スナップショット」に依存する処理向けのチャンク実行。
 * yield 中にユーザーが行を編集（dirty 化 / outbox 追加）してもすり抜けないよう、
 * チャンク境界ごとに context（outbox/dirty スナップショット等）を再読込する。
 */
async function forEachDocsChunkWithContext<T, C>(
  items: readonly T[],
  loadContext: () => Promise<C>,
  handler: (item: T, context: C) => Promise<void>,
): Promise<void> {
  let context = await loadContext();
  for (let index = 0; index < items.length; index += 1) {
    await handler(items[index], context);
    if (
      (index + 1) % DOCS_APPLY_CHUNK_SIZE === 0 &&
      index + 1 < items.length
    ) {
      await new Promise((resolve) => setTimeout(resolve, 0));
      // yield 中の並行編集を取りこぼさないよう再読込する。
      context = await loadContext();
    }
  }
}

type DocsPreservationSets = {
  /** outbox に pending がある entityId 集合（未送信の編集を pull で潰さない）。 */
  outbox: Set<string>;
  /** dirty=true のローカル行 id 集合（entityId 形式は複合キーで "a:b"）。 */
  dirty: Set<string>;
};

/**
 * table 単位で「pull 上書きから守るべき行」を 2 クエリで一括ロードする。
 * 旧 `hasPendingOutbox` + `localDocsRowIsDirty` の行ごと N+1 を置き換える。
 * 判定意味・複合キーの entityId 形式（"nodeId:fieldId" 等）は従来と同一。
 */
async function loadDocsPreservationSets(
  table: string,
  requestedAuthScope?: string,
  requestedScopeKey?: string | null,
): Promise<DocsPreservationSets> {
  const db = getDb();
  let authScope: string | null = requestedAuthScope ?? null;
  if (requestedAuthScope === undefined) {
    const token = await getToken();
    authScope = token && typeof getTokenAuthScope === "function"
      ? getTokenAuthScope(token)
      : null;
  }
  const outboxRows = await db
    .select({
      entityId: schema.outbox.entityId,
      authScope: schema.outbox.authScope,
      docsScopeKey: schema.outbox.docsScopeKey,
    })
    .from(schema.outbox)
    // Filter the account in JavaScript after selecting the legacy nullable
    // auth_scope column.  Besides preserving pre-auth rows safely, this keeps
    // old in-memory test doubles (which only understand tableName=) honest.
    .where(eq(schema.outbox.tableName, table));
  const outbox = new Set(
    outboxRows
      .filter((row) =>
        (authScope
          ? row.authScope == null || row.authScope === authScope
          : row.authScope == null)
        && (
          !requestedScopeKey
          || !table.startsWith("knowledge_")
          || row.docsScopeKey === requestedScopeKey
        ),
      )
      .map((row) => row.entityId),
  );

  const dirty = new Set<string>();
  if (table === "knowledge_nodes") {
    const rows = await db
      .select({ id: schema.knowledgeNodes.id })
      .from(schema.knowledgeNodes)
      .where(eq(schema.knowledgeNodes.dirty, true));
    for (const row of rows) dirty.add(row.id);
  } else if (table === "knowledge_supertags") {
    const rows = await db
      .select({ id: schema.knowledgeSupertags.id })
      .from(schema.knowledgeSupertags)
      .where(eq(schema.knowledgeSupertags.dirty, true));
    for (const row of rows) dirty.add(row.id);
  } else if (table === "knowledge_node_supertags") {
    const rows = await db
      .select({
        nodeId: schema.knowledgeNodeSupertags.nodeId,
        supertagId: schema.knowledgeNodeSupertags.supertagId,
      })
      .from(schema.knowledgeNodeSupertags)
      .where(eq(schema.knowledgeNodeSupertags.dirty, true));
    for (const row of rows) dirty.add(`${row.nodeId}:${row.supertagId}`);
  } else if (table === "knowledge_field_values") {
    const rows = await db
      .select({
        nodeId: schema.knowledgeFieldValues.nodeId,
        fieldId: schema.knowledgeFieldValues.fieldId,
      })
      .from(schema.knowledgeFieldValues)
      .where(eq(schema.knowledgeFieldValues.dirty, true));
    for (const row of rows) dirty.add(`${row.nodeId}:${row.fieldId}`);
  }
  return { outbox, dirty };
}

/**
 * 事前ロードした Set で「この行を pull 上書きから守るか」を判定する。
 * 旧 `shouldPreserveRemoteDocsRow` と同じく outbox → dirty の順で優先し、
 * 守る場合はサーバ版 snapshot を従来同様に記録する。
 */
async function preserveDocsRow(
  sets: DocsPreservationSets,
  table: string,
  entityId: string,
  serverPayload: unknown,
): Promise<boolean> {
  if (sets.outbox.has(entityId)) {
    await recordOutboxServerSnapshot(table, entityId, serverPayload);
    return true;
  }
  if (sets.dirty.has(entityId)) {
    await saveLocalDocsServerSnapshot(table, entityId, serverPayload);
    return true;
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

// ---------- 行 → API 形マッパ ----------

function toNode(row: DbNode): DocsNode {
  return {
    id: row.id,
    workspace_id: row.workspaceId ?? null,
    parent_id: row.parentId ?? null,
    root_page_id: row.rootPageId ?? null,
    project_id: row.projectId ?? null,
    source: row.source ?? null,
    access: row.access ?? null,
    read_only: Boolean(row.readOnly),
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

function remoteDocsNodeValues(n: DocsNode, now: string) {
  return {
    id: n.id,
    workspaceId: n.workspace_id ?? null,
    parentId: n.parent_id ?? null,
    rootPageId: n.root_page_id ?? null,
    projectId: n.project_id ?? null,
    source: n.source ?? null,
    access: n.access ?? null,
    readOnly: n.read_only ?? n.access === "read",
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
}

function remoteDocsNodeUpdateSet(
  values: ReturnType<typeof remoteDocsNodeValues>,
) {
  return {
    workspaceId: values.workspaceId,
    parentId: values.parentId,
    rootPageId: values.rootPageId,
    projectId: values.projectId,
    source: values.source,
    access: values.access,
    readOnly: values.readOnly,
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
  };
}

export async function applyRemoteDocsNodes(
  rows: DocsNode[],
  options: { force?: boolean } = {},
): Promise<void> {
  if (!rows.length) return;
  const db = getDb();
  const now = new Date().toISOString();
  if (options.force) {
    // 強制再同期は開始前にDocs outbox/cacheを破棄済みで preserve 判定が不要。
    // 1行ごとの自動commitを避け、200行ずつ1transactionで適用する。
    // 37万件規模でもJS threadへ定期的に制御を返しつつ完走できるようにする。
    for (let index = 0; index < rows.length; index += DOCS_APPLY_CHUNK_SIZE) {
      const chunk = rows.slice(index, index + DOCS_APPLY_CHUNK_SIZE);
      db.transaction((tx) => {
        for (const node of chunk) {
          const values = remoteDocsNodeValues(node, now);
          tx
            .insert(schema.knowledgeNodes)
            .values(values)
            .onConflictDoUpdate({
              target: schema.knowledgeNodes.id,
              set: remoteDocsNodeUpdateSet(values),
            })
            .run();
        }
      });
      if (index + DOCS_APPLY_CHUNK_SIZE < rows.length) {
        await new Promise((resolve) => setTimeout(resolve, 0));
      }
    }
    return;
  }
  await forEachDocsChunkWithContext<DocsNode, DocsPreservationSets | null>(
    rows,
    () =>
      options.force
        ? Promise.resolve(null)
        : loadDocsPreservationSets("knowledge_nodes"),
    async (n, sets) => {
    if (sets && (await preserveDocsRow(sets, "knowledge_nodes", n.id, n))) {
      return;
    }
    const values = remoteDocsNodeValues(n, now);
    await db
      .insert(schema.knowledgeNodes)
      .values(values)
      .onConflictDoUpdate({
        target: schema.knowledgeNodes.id,
        set: remoteDocsNodeUpdateSet(values),
      });
  });
}

export type ClipIngestOutlineLine = {
  text: string;
  children?: string[];
};

/**
 * A user-visible multiline block created by ClipIngest.  The content is kept
 * in the normal knowledge-node body_json contract instead of a system-owned
 * verbatim collection on the topic node.
 */
export type ClipIngestDocBlockInput = {
  label: string;
  blockType: "markdown" | "code";
  content: string;
  /** Optional ClipIngest provenance metadata copied into the block body. */
  clipIngest?: Record<string, unknown>;
};

export type DocsSyncStagedRow = {
  tableName: string;
  entityKey: string;
  payload: Record<string, unknown> | null;
  isTombstone: boolean;
};

/** Maximum number of staged rows retained by one promotion read. */
export const DOCS_STAGED_PROMOTION_BATCH_SIZE = 256;

export type DocsSyncPromotionTelemetry = {
  /** Whether the compatibility in-memory input or SQLite staging was used. */
  source: "memory" | "staging";
  /** Number of staged rows applied during this promotion. */
  rowsRead: number;
  /** Number of bounded SQLite reads used by the promotion. */
  batches: number;
  /** Largest SQLite read retained at once. */
  maxBatchSize: number;
};

export type DocsSyncAuthoritative = {
  ids?: string[];
  scopeId?: string;
  digest?: string;
};

export type DocsScopeSetReconcile = {
  previousScopes: Array<{
    workspace_id: string;
    project_id?: string | null;
    source?: string;
    access?: string;
    read_only?: boolean;
  }>;
  newScopes: Array<{
    workspace_id: string;
    project_id?: string | null;
    source?: string;
    access?: string;
    read_only?: boolean;
  }>;
};

export type DocsSyncPromotionOptions = {
  runId: string;
  authScope: string;
  scopeId?: string;
  projectId?: string | null;
  scopeKey?: string;
  /**
   * Compatibility input for older callers/tests.  The sync engine omits this
   * property so production promotion streams rows directly from SQLite.
   */
  staged?: DocsSyncStagedRow[];
  authoritative: Record<string, DocsSyncAuthoritative>;
  /** Full authoritative scope projection from the root page. */
  scopes?: Array<{
    workspace_id: string;
    project_id?: string | null;
    source?: string;
    access?: string;
    read_only?: boolean;
  }>;
  /** Reconcile the complete ACL scope set in this same promotion tx. */
  scopeSet?: DocsScopeSetReconcile;
  /** Commit metadata atomically with live promotion. */
  finalize?: {
    scopeDigest: string;
    serverTime: string;
    digestByTable: Record<string, string>;
    scopeDigestKey: string;
    scopeRevision?: string | null;
    scopeRevisionKey?: string;
    lastPulledKey: string;
    digestKeys: Record<string, string>;
    scopesKey?: string;
    /** Workspace identity used by local list/read filters. */
    workspaceKey?: string;
    workspaceId?: string;
  };
  force?: boolean;
};

/**
 * Bounded provenance metadata forwarded through Docs outbox.  This is
 * intentionally not a body/content transport: raw source text, local paths,
 * secrets and arbitrary planner fields are discarded before SQLite/HTTP.
 */
export type ClipIngestSourceRef = Record<string, unknown>;

const CLIP_SOURCE_REF_TYPES = new Set([
  "input",
  "direct",
  "supplemental",
  "attachment",
]);
const CLIP_SOURCE_REF_KEYS = new Set([
  "source_id",
  "source_type",
  "url",
  "used",
  "acquisition_status",
  "provider",
  "upload_id",
  "file_name",
  "mime_type",
  "sha256",
  "kind",
  "label",
  "start_line",
  "end_line",
  "char_count",
  "line_count",
  "blank_line_count",
]);

// Keep local provenance as metadata only. The server rejects absolute paths
// and traversal; applying the same guard before SQLite avoids leaking host
// directories/usernames and prevents a known 400 from being retried.
const SOURCE_REF_ABSOLUTE_PATH_PATTERN = /^(?:[A-Za-z]:[\\/]|\\\\|\/|~(?:[\\/]|$))/;
const SOURCE_REF_PATH_TRAVERSAL_PATTERN = /(?:^|[\\/])\.\.(?:[\\/]|$)/;

function boundedSourceRefText(value: unknown, limit: number): string {
  return String(value ?? "")
    .replace(/[\r\n\t]+/g, " ")
    .replace(/\s{2,}/g, " ")
    .trim()
    .slice(0, limit);
}

function sanitizeSourceRefFileName(value: unknown): string {
  const text = boundedSourceRefText(value, 500);
  if (
    !text
    || SOURCE_REF_ABSOLUTE_PATH_PATTERN.test(text)
    || SOURCE_REF_PATH_TRAVERSAL_PATTERN.test(text)
    || /^[A-Za-z]:/.test(text)
    || /[\\/]/.test(text)
  ) {
    return "";
  }
  return text;
}

/** Sanitize optional source_refs before they enter a local outbox payload. */
export function sanitizeClipIngestSourceRefs(value: unknown): ClipIngestSourceRef[] {
  if (!Array.isArray(value)) return [];
  const result: ClipIngestSourceRef[] = [];
  for (const item of value.slice(0, 32)) {
    if (!item || typeof item !== "object" || Array.isArray(item)) continue;
    const raw = item as Record<string, unknown>;
    const ref: ClipIngestSourceRef = {};
    let rejectRef = false;
    for (const key of CLIP_SOURCE_REF_KEYS) {
      if (!(key in raw)) continue;
      const current = raw[key];
      if (["used"].includes(key)) {
        if (typeof current === "boolean") ref[key] = current;
        continue;
      }
      if (["start_line", "end_line", "char_count", "line_count", "blank_line_count"].includes(key)) {
        const numeric = Number(current);
        if (Number.isInteger(numeric) && numeric >= 0 && numeric <= 1_000_000) {
          ref[key] = numeric;
        }
        continue;
      }
      const text = boundedSourceRefText(current, key === "url" ? 2048 : 500);
      if (!text) continue;
      if (key === "file_name") {
        const safeFileName = sanitizeSourceRefFileName(current);
        if (!safeFileName) continue;
        ref[key] = safeFileName;
        continue;
      }
      if (
        SOURCE_REF_ABSOLUTE_PATH_PATTERN.test(text)
        || SOURCE_REF_PATH_TRAVERSAL_PATTERN.test(text)
        || /^[A-Za-z]:/.test(text)
      ) {
        continue;
      }
      // docs_sync rejects credentials and secret query keys in any
      // URL-looking value. Drop the field locally so an invalid provenance
      // ref can never be retried from SQLite/outbox.
      if (text.includes("://") && !isSafeSourceRefUrl(text)) {
        rejectRef = true;
        break;
      }
      if (key === "source_id") {
        if (text === "input") {
          ref[key] = "source:0";
          continue;
        }
        const direct = /^direct:(\d+)$/.exec(text);
        if (direct) {
          ref[key] = `source:${Number(direct[1])}`;
          continue;
        }
      }
      if (key === "source_type" && !CLIP_SOURCE_REF_TYPES.has(text)) continue;
      if (key === "url") {
        const canonical = canonicalizeIngestUrl(text);
        if (!canonical) {
          rejectRef = true;
          break;
        }
        ref[key] = canonical;
        continue;
      }
      if (key === "sha256" && !/^[0-9a-f]{64}$/i.test(text)) continue;
      ref[key] = text;
    }
    if (rejectRef) continue;
    if (ref.source_id || ref.url || ref.upload_id) result.push(ref);
  }
  return result;
}

export type ClipIngestTreeInput = {
  parentId: string;
  rootPageId?: string | null;
  projectId?: string | null;
  title: string;
  bodyJson?: Record<string, unknown> | null;
  sourceRefs?: readonly unknown[];
  sortOrder: number;
  outline: readonly ClipIngestOutlineLine[];
  blocks?: readonly ClipIngestDocBlockInput[];
};

/**
 * ローカル ClipIngest 専用の一括作成。
 *
 * 取り込み親・アウトライン子・各 knowledge_nodes の outbox を同じ SQLite
 * transaction に載せる。通常の createNode を連続呼び出しすると途中の
 * outline 作成失敗で親だけが残り、次の保留再送で二重取り込みになるため、
 * この経路では transaction の外へ書き込みを出さない。
 */
export async function createClipIngestTree(
  input: ClipIngestTreeInput,
): Promise<DocsNode> {
  const title = String(input.title ?? "").trim();
  const parentId = String(input.parentId ?? "").trim();
  const sortOrder = Number(input.sortOrder);
  if (!parentId || !title || !Number.isFinite(sortOrder)) {
    throw new Error("ClipIngestツリーの入力が不正です");
  }
  const bodyJson = input.bodyJson ?? null;
  const sourceRefs = sanitizeClipIngestSourceRefs(input.sourceRefs);
  // SQLite/Drizzle の JSON mode が commit 時に失敗しないよう、循環値を
  // transaction 開始前に検証しておく。
  try {
    if (bodyJson !== null) JSON.stringify(bodyJson);
  } catch {
    throw new Error("ClipIngest本文JSONをシリアライズできません");
  }
  // Shared Personal subtree / Project Docs may be read-only.  Check the
  // parent before creating the local tree so an offline edit cannot enqueue
  // a mutation that the server will reject after ACL revocation.
  const parentRow = await getNodeRow(parentId);
  assertDocsWritable(parentRow);
  const outline = input.outline.map((line) => ({
    text: String(line.text ?? "").trim(),
    children: (line.children ?? [])
      .map((child) => String(child ?? "").trim())
      .filter(Boolean),
  })).filter((line) => line.text.length > 0);
  const blocks = (input.blocks ?? []).map((block) => {
    const label = String(block.label ?? "").trim().slice(0, 500);
    const content = String(block.content ?? "").replace(/\r\n?/g, "\n");
    if (!label || !content) throw new Error("ClipIngest本文blockの入力が不正です");
    if (block.blockType !== "markdown" && block.blockType !== "code") {
      throw new Error("ClipIngest本文blockの種別が不正です");
    }
    let clipIngest: Record<string, unknown> | undefined;
    if (block.clipIngest !== undefined) {
      try {
        const serialized = JSON.stringify(block.clipIngest);
        clipIngest = JSON.parse(serialized) as Record<string, unknown>;
      } catch {
        throw new Error("ClipIngest本文blockのメタデータをシリアライズできません");
      }
    }
    return { label, content, blockType: block.blockType, clipIngest };
  });
  const tokenValue = await getToken();
  const token = Boolean(tokenValue);
  const authScope = tokenValue ? getTokenAuthScope(tokenValue) : null;
  const db = getDb();
  const now = new Date().toISOString();
  const rootPageId = input.rootPageId ?? parentId;
  const inheritedWorkspaceId = parentRow?.workspaceId ?? null;
  const inheritedProjectId = input.projectId !== undefined
    ? input.projectId ?? null
    : parentRow?.projectId ?? null;
  const inheritedSource = parentRow?.source ?? (inheritedProjectId ? "project" : "personal");
  const inheritedAccess = parentRow?.access ?? "write";
  const inheritedReadOnly = parentRow?.readOnly ?? false;
  const rootId = randomId();
  const rootNode: DocsNode = {
    id: rootId,
    workspace_id: inheritedWorkspaceId,
    parent_id: parentId,
    root_page_id: rootPageId,
    project_id: inheritedProjectId,
    source: inheritedSource,
    access: inheritedAccess,
    read_only: inheritedReadOnly,
    system_key: null,
    title,
    aliases: [],
    description: null,
    body_json: bodyJson,
    // Keep the same title/body_text mirror as the server writer.  Typed
    // multiline content lives in body_json.content, never in this field.
    body_text: title,
    node_type: "node",
    display_props: null,
    query_json: null,
    view_json: null,
    day_date: null,
    sort_order: sortOrder,
    created_by: null,
    updated_by: null,
    created_at: now,
    updated_at: now,
    archived_at: null,
  };
  const nodes: DocsNode[] = [rootNode];
  let lineOrder = 1024;
  for (const line of outline) {
    const lineId = randomId();
    nodes.push({
      ...rootNode,
      id: lineId,
      parent_id: rootId,
      root_page_id: rootPageId,
      title: line.text,
      body_text: line.text,
      body_json: null,
      sort_order: lineOrder,
    });
    lineOrder += 1024;
    let childOrder = 1024;
    for (const childTitle of line.children ?? []) {
      nodes.push({
        ...rootNode,
        id: randomId(),
        parent_id: lineId,
        root_page_id: rootPageId,
        title: childTitle,
        body_text: childTitle,
        body_json: null,
        sort_order: childOrder,
      });
      childOrder += 1024;
    }
  }
  // Typed blocks are regular editable child nodes.  Keep them after the
  // semantic outline so the topic/title and its claims remain the first rows,
  // while preserving block order exactly as supplied by ClipIngest.
  let blockOrder = Math.max(lineOrder, 1024);
  for (const block of blocks) {
    const blockId = randomId();
    const blockBody: Record<string, unknown> = {
      format: "doc_block",
      block_type: block.blockType,
      content: block.content,
      label: block.label,
      ...(block.clipIngest ? { clip_ingest: block.clipIngest } : {}),
    };
    nodes.push({
      ...rootNode,
      id: blockId,
      parent_id: rootId,
      root_page_id: rootPageId,
      title: block.label,
      body_text: block.label,
      body_json: blockBody,
      sort_order: blockOrder,
    });
    blockOrder += 1024;
  }
  const nodeValues = (node: DocsNode) => ({
    id: node.id,
    workspaceId: node.workspace_id ?? null,
    parentId: node.parent_id,
    rootPageId: node.root_page_id,
    projectId: node.project_id,
    source: node.source ?? "personal",
    access: node.access ?? "write",
    readOnly: node.read_only ?? false,
    systemKey: null,
    title: node.title,
    aliases: [],
    description: null,
    bodyJson: node.body_json,
    bodyText: node.body_text,
    nodeType: "node",
    displayProps: null,
    queryJson: null,
    viewJson: null,
    dayDate: null,
    sortOrder: node.sort_order,
    createdBy: null,
    updatedBy: null,
    createdAt: now,
    updatedAt: now,
    serverUpdatedAt: null,
    dirty: true,
    conflictPayload: null,
    archivedAt: null,
  });
  const outboxValues = (node: DocsNode) => ({
    opId: randomId(),
    createdAt: Date.now(),
    tableName: "knowledge_nodes",
    action: "create",
    entityId: node.id,
    authScope,
    docsScopeKey: node.workspace_id
      ? `${node.workspace_id}|project:${node.project_id ?? ""}`
      : null,
    payload: JSON.stringify({
      id: node.id,
      workspace_id: node.workspace_id,
      parent_id: node.parent_id,
      project_id: node.project_id,
      title: node.title,
      ...(node.body_text !== null ? { body_text: node.body_text } : {}),
      description: null,
      node_type: "node",
      day_date: null,
      sort_order: node.sort_order,
      ...(node.body_json ? { body_json: node.body_json } : {}),
      ...(node.id === rootId && sourceRefs.length
        ? { source_refs: sourceRefs }
        : {}),
    }),
    baseUpdatedAt: null,
    basePayload: null,
    conflictPayload: null,
    retryCount: 0,
    lastError: null,
    blockedReason: node.workspace_id ? null : "docs_scope_ambiguous",
  });

  // ここから先は同期 transaction。callback 内で await せず、例外は SQLite
  // が自動 rollback するため nodes/outbox が部分的に残らない。
  db.transaction((tx) => {
    for (const node of nodes) {
      tx.insert(schema.knowledgeNodes).values(nodeValues(node)).run();
    }
    if (token) {
      for (const node of nodes) {
        tx.insert(schema.outbox).values(outboxValues(node)).run();
      }
    }
  });
  return rootNode;
}

export async function applyDocsNodeTombstones(
  tombstones: Array<{ id: string; deleted_at?: string | null }>,
): Promise<void> {
  if (!tombstones.length) return;
  const db = getDb();
  // ハード削除の tombstone はローカルから物理削除する（アーカイブは通常行で来る）。
  await forEachDocsChunkWithContext(
    tombstones,
    () => loadDocsPreservationSets("knowledge_nodes"),
    async (t, sets) => {
      if (
        await preserveDocsRow(sets, "knowledge_nodes", t.id, {
          id: t.id,
          deleted: true,
          deleted_at: t.deleted_at ?? null,
        })
      ) {
        return;
      }
      await db
        .delete(schema.knowledgeNodes)
        .where(eq(schema.knowledgeNodes.id, t.id));
    },
  );
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
  // yield 中に dirty 化/outbox 追加された node を stale 削除で取りこぼさないよう、
  // このスナップショットはチャンク境界ごとに再読込する。
  const loadNodeReconcileSets = async (): Promise<{
    nodeOutboxIds: Set<string>;
    dirtyRelationNodeIds: Set<string>;
    relationOutboxNodeIds: Set<string>;
  }> => {
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
    return {
      dirtyRelationNodeIds: new Set([
        ...dirtyTags.map((row) => row.nodeId),
        ...dirtyFields.map((row) => row.nodeId),
      ]),
      nodeOutboxIds: new Set(
        pendingOutbox
          .filter((row) => row.tableName === "knowledge_nodes")
          .map((row) => row.entityId),
      ),
      relationOutboxNodeIds: new Set(
        pendingOutbox
          .filter((row) =>
            row.tableName === "knowledge_node_supertags"
            || row.tableName === "knowledge_field_values",
          )
          .map((row) => row.entityId.split(":", 1)[0]),
      ),
    };
  };

  const directlyProtected = new Set<string>();
  await forEachDocsChunkWithContext(
    staleRows,
    loadNodeReconcileSets,
    async (row, ctx) => {
      const deletedPayload = {
        id: row.id,
        deleted: true,
        authoritative_scope_id: workspaceId,
      };
      if (ctx.nodeOutboxIds.has(row.id)) {
        directlyProtected.add(row.id);
        await recordOutboxServerSnapshot("knowledge_nodes", row.id, deletedPayload);
        return;
      }
      if (
        row.dirty ||
        ctx.dirtyRelationNodeIds.has(row.id) ||
        ctx.relationOutboxNodeIds.has(row.id)
      ) {
        directlyProtected.add(row.id);
        await saveLocalDocsServerSnapshot("knowledge_nodes", row.id, {
          ...deletedPayload,
          preserved_for_dirty_relation: true,
        });
      }
    },
  );

  const protectedIds = expandProtectedDocsNodeAncestors(
    staleRows,
    directlyProtected,
  );
  // A revoked scope must not remain editable while its dirty/outbox snapshot
  // is waiting for a server-side conflict response.  Keep the row and outbox
  // for recovery, but flip the local ACL guard to read-only first.
  const protectedIdList = [...protectedIds];
  for (let offset = 0; offset < protectedIdList.length; offset += 500) {
    const ids = protectedIdList.slice(offset, offset + 500);
    await db
      .update(schema.knowledgeNodes)
      .set({ access: "read", readOnly: true })
      .where(inArray(schema.knowledgeNodes.id, ids));
  }
  await forEachDocsChunk([...protectedIds], async (id) => {
    if (directlyProtected.has(id)) return;
    await saveLocalDocsServerSnapshot("knowledge_nodes", id, {
      id,
      deleted: true,
      authoritative_scope_id: workspaceId,
      preserved_as_dirty_ancestor: true,
    });
  });

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
  await forEachDocsChunkWithContext<DocsSupertag, DocsPreservationSets | null>(
    rows,
    () =>
      options.force
        ? Promise.resolve(null)
        : loadDocsPreservationSets("knowledge_supertags"),
    async (s, sets) => {
    if (sets && (await preserveDocsRow(sets, "knowledge_supertags", s.id, s))) {
      return;
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
  },
  );
}

export async function reconcileDocsSupertagsWithServer(
  authoritativeIds: string[] | undefined,
  workspaceId: string | undefined,
): Promise<void> {
  if (!authoritativeIds || !workspaceId) return;
  const db = getDb();
  const serverIds = new Set(authoritativeIds);
  const localRows = await db
    .select({
      id: schema.knowledgeSupertags.id,
      dirty: schema.knowledgeSupertags.dirty,
    })
    .from(schema.knowledgeSupertags)
    .where(eq(schema.knowledgeSupertags.workspaceId, workspaceId));
  const staleRows = localRows.filter((row) => !serverIds.has(row.id));
  const sets = await loadDocsPreservationSets("knowledge_supertags");
  const deletableIds: string[] = [];
  for (const row of staleRows) {
    if (sets.outbox.has(row.id) || row.dirty) {
      await preserveDocsRow(sets, "knowledge_supertags", row.id, {
        id: row.id,
        deleted: true,
        authoritative_scope_id: workspaceId,
      });
      continue;
    }
    deletableIds.push(row.id);
  }
  for (let offset = 0; offset < deletableIds.length; offset += 500) {
    const ids = deletableIds.slice(offset, offset + 500);
    await db
      .delete(schema.knowledgeNodeSupertags)
      .where(inArray(schema.knowledgeNodeSupertags.supertagId, ids));
    await db
      .delete(schema.knowledgeSupertagFields)
      .where(inArray(schema.knowledgeSupertagFields.supertagId, ids));
    await db
      .delete(schema.knowledgeSupertags)
      .where(inArray(schema.knowledgeSupertags.id, ids));
  }
}

export async function applyRemoteDocsFields(rows: DocsField[]): Promise<void> {
  if (!rows.length) return;
  const db = getDb();
  const now = new Date().toISOString();
  await forEachDocsChunk(rows, async (f) => {
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
  });
}

export async function reconcileDocsFieldsWithServer(
  authoritativeIds: string[] | undefined,
  workspaceId: string | undefined,
): Promise<void> {
  if (!authoritativeIds || !workspaceId) return;
  const db = getDb();
  const serverIds = new Set(authoritativeIds);
  const localRows = await db
    .select({ id: schema.knowledgeFields.id })
    .from(schema.knowledgeFields)
    .where(eq(schema.knowledgeFields.workspaceId, workspaceId));
  const staleIds = localRows
    .filter((row) => !serverIds.has(row.id))
    .map((row) => row.id);
  for (let offset = 0; offset < staleIds.length; offset += 500) {
    const ids = staleIds.slice(offset, offset + 500);
    await db
      .delete(schema.knowledgeFieldValues)
      .where(inArray(schema.knowledgeFieldValues.fieldId, ids));
    await db
      .delete(schema.knowledgeSupertagFields)
      .where(inArray(schema.knowledgeSupertagFields.fieldId, ids));
    await db
      .delete(schema.knowledgeFields)
      .where(inArray(schema.knowledgeFields.id, ids));
  }
}

export async function applyRemoteDocsSupertagFields(
  rows: DocsSupertagField[],
  authoritativeIds?: string[],
): Promise<void> {
  const db = getDb();
  await forEachDocsChunk(rows, async (sf) => {
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
  });
  if (authoritativeIds) {
    const authoritative = new Set(authoritativeIds);
    const local = await db.select().from(schema.knowledgeSupertagFields);
    const stale = local.filter(
      (row) => !authoritative.has(`${row.supertagId}:${row.fieldId}`),
    );
    await forEachDocsChunk(stale, async (row) => {
      await db
        .delete(schema.knowledgeSupertagFields)
        .where(
          and(
            eq(schema.knowledgeSupertagFields.supertagId, row.supertagId),
            eq(schema.knowledgeSupertagFields.fieldId, row.fieldId),
          ),
        );
    });
  }
}

export async function applyRemoteDocsNodeSupertags(
  rows: DocsNodeSupertag[],
  authoritativeIds?: string[],
  options: { force?: boolean } = {},
): Promise<void> {
  const db = getDb();
  await forEachDocsChunkWithContext<
    DocsNodeSupertag,
    DocsPreservationSets | null
  >(
    rows,
    () =>
      options.force
        ? Promise.resolve(null)
        : loadDocsPreservationSets("knowledge_node_supertags"),
    async (ns, sets) => {
    const entityId = `${ns.node_id}:${ns.supertag_id}`;
    if (
      sets &&
      (await preserveDocsRow(sets, "knowledge_node_supertags", entityId, ns))
    ) {
      return;
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
  },
  );
  if (authoritativeIds) {
    const authoritative = new Set(authoritativeIds);
    const local = await db.select().from(schema.knowledgeNodeSupertags);
    const stale = local.filter(
      (row) => !authoritative.has(`${row.nodeId}:${row.supertagId}`),
    );
    await forEachDocsChunkWithContext(
      stale,
      () => loadDocsPreservationSets("knowledge_node_supertags"),
      async (row, reconcileSets) => {
      const entityId = `${row.nodeId}:${row.supertagId}`;
      if (
        await preserveDocsRow(
          reconcileSets,
          "knowledge_node_supertags",
          entityId,
          {
            node_id: row.nodeId,
            supertag_id: row.supertagId,
            deleted: true,
          },
        )
      ) {
        return;
      }
      await db
        .delete(schema.knowledgeNodeSupertags)
        .where(
          and(
            eq(schema.knowledgeNodeSupertags.nodeId, row.nodeId),
            eq(schema.knowledgeNodeSupertags.supertagId, row.supertagId),
          ),
        );
    });
  }
}

export async function applyRemoteDocsFieldValues(
  rows: DocsFieldValue[],
  authoritativeIds?: string[],
  options: { force?: boolean } = {},
): Promise<void> {
  const db = getDb();
  const now = new Date().toISOString();
  await forEachDocsChunkWithContext<
    DocsFieldValue,
    DocsPreservationSets | null
  >(
    rows,
    () =>
      options.force
        ? Promise.resolve(null)
        : loadDocsPreservationSets("knowledge_field_values"),
    async (v, sets) => {
    const entityId = `${v.node_id}:${v.field_id}`;
    if (
      sets &&
      (await preserveDocsRow(sets, "knowledge_field_values", entityId, v))
    ) {
      return;
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
  },
  );
  if (authoritativeIds) {
    // サーバ権威セットに無い行（Web 等でクリア済み）をローカルから削除する。
    const authoritative = new Set(authoritativeIds);
    const local = await db
      .select({
        nodeId: schema.knowledgeFieldValues.nodeId,
        fieldId: schema.knowledgeFieldValues.fieldId,
      })
      .from(schema.knowledgeFieldValues);
    const stale = local.filter(
      (row) => !authoritative.has(`${row.nodeId}:${row.fieldId}`),
    );
    await forEachDocsChunkWithContext(
      stale,
      () => loadDocsPreservationSets("knowledge_field_values"),
      async (row, reconcileSets) => {
      const entityId = `${row.nodeId}:${row.fieldId}`;
      if (
        await preserveDocsRow(reconcileSets, "knowledge_field_values", entityId, {
          node_id: row.nodeId,
          field_id: row.fieldId,
          deleted: true,
        })
      ) {
        return;
      }
      await db
        .delete(schema.knowledgeFieldValues)
        .where(
          and(
            eq(schema.knowledgeFieldValues.nodeId, row.nodeId),
            eq(schema.knowledgeFieldValues.fieldId, row.fieldId),
          ),
        );
    },
    );
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
  await forEachDocsChunk(rows, async (p) => {
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
  });
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
  await forEachDocsChunk(rows, async (e) => {
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
  });
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

function assertDocsWritable(row: DbNode | null): void {
  if (!row) throw new Error("Docs node not found");
  if (row.readOnly === true || row.access === "read") {
    throw new Error("このDocsは読み取り専用です");
  }
}

function docsCompositeKey(value: Record<string, unknown>): string | null {
  return value.node_id != null && value.field_id != null
    ? `${String(value.node_id)}:${String(value.field_id)}`
    : value.node_id != null && value.supertag_id != null
      ? `${String(value.node_id)}:${String(value.supertag_id)}`
      : value.supertag_id != null && value.field_id != null
        ? `${String(value.supertag_id)}:${String(value.field_id)}`
      : value.id == null
        ? null
        : String(value.id);
}

function docsScopeKeyForRow(
  row: { workspaceId?: string | null; projectId?: string | null } | null | undefined,
): string | null {
  if (!row?.workspaceId) return null;
  return `${row.workspaceId}|project:${row.projectId ?? ""}`;
}

type DocsScopeSetEntry = {
  workspace_id: string;
  project_id?: string | null;
  source?: string;
  access?: string;
  read_only?: boolean;
};

const DOCS_SCOPE_TABLE_NAMES = [
  "knowledge_nodes",
  "knowledge_supertags",
  "knowledge_node_supertags",
  "knowledge_supertag_fields",
  "knowledge_fields",
  "knowledge_field_values",
  "knowledge_node_placements",
  "knowledge_edges",
] as const;

function docsScopeKeyFromEntry(scope: DocsScopeSetEntry): string {
  return `${scope.workspace_id}|project:${scope.project_id ?? ""}`;
}

function docsTxRows(query: any): any[] | null {
  if (typeof query?.all !== "function") return null;
  try {
    return query.all() as any[];
  } catch {
    return null;
  }
}

function deleteDocsLiveRowTx(tx: any, table: string, key: string): void {
  if (table === "knowledge_nodes") {
    tx.delete(schema.knowledgeNodes).where(eq(schema.knowledgeNodes.id, key)).run();
  } else if (table === "knowledge_supertags") {
    tx.delete(schema.knowledgeSupertags).where(eq(schema.knowledgeSupertags.id, key)).run();
  } else if (table === "knowledge_fields") {
    tx.delete(schema.knowledgeFields).where(eq(schema.knowledgeFields.id, key)).run();
  } else if (table === "knowledge_node_supertags") {
    const [nodeId, supertagId] = key.split(":", 2);
    tx.delete(schema.knowledgeNodeSupertags)
      .where(and(eq(schema.knowledgeNodeSupertags.nodeId, nodeId), eq(schema.knowledgeNodeSupertags.supertagId, supertagId)))
      .run();
  } else if (table === "knowledge_supertag_fields") {
    const [supertagId, fieldId] = key.split(":", 2);
    tx.delete(schema.knowledgeSupertagFields)
      .where(and(eq(schema.knowledgeSupertagFields.supertagId, supertagId), eq(schema.knowledgeSupertagFields.fieldId, fieldId)))
      .run();
  } else if (table === "knowledge_field_values") {
    const [nodeId, fieldId] = key.split(":", 2);
    tx.delete(schema.knowledgeFieldValues)
      .where(and(eq(schema.knowledgeFieldValues.nodeId, nodeId), eq(schema.knowledgeFieldValues.fieldId, fieldId)))
      .run();
  } else if (table === "knowledge_node_placements") {
    tx.delete(schema.knowledgeNodePlacements).where(eq(schema.knowledgeNodePlacements.id, key)).run();
  } else if (table === "knowledge_edges") {
    tx.delete(schema.knowledgeEdges).where(eq(schema.knowledgeEdges.id, key)).run();
  }
}

/**
 * Apply one scope revoke/downgrade from a transaction snapshot.  This helper
 * is intentionally synchronous: Expo's SQLite transaction exposes `.all()`
 * reads, allowing the ACL, membership, outbox and live-row decisions to share
 * the same snapshot as the root promotion.
 */
function quarantineDocsScopeTx(
  tx: any,
  authScope: string,
  scope: DocsScopeSetEntry,
  mode: "revoke" | "downgrade",
  now: string,
): void {
  if (!schema.docsScopeMembership || typeof tx.select !== "function") return;
  const scopeKey = docsScopeKeyFromEntry(scope);
  const membershipRows = docsTxRows(
    tx.select()
      .from(schema.docsScopeMembership)
      .where(and(
        eq(schema.docsScopeMembership.authScope, authScope),
        eq(schema.docsScopeMembership.scopeKey, scopeKey),
      )),
  );
  const allMembershipRows = docsTxRows(
    tx.select()
      .from(schema.docsScopeMembership)
      .where(eq(schema.docsScopeMembership.authScope, authScope)),
  );
  const outboxRows = docsTxRows(
    tx.select({
      opId: schema.outbox.opId,
      tableName: schema.outbox.tableName,
      entityId: schema.outbox.entityId,
      docsScopeKey: schema.outbox.docsScopeKey,
    })
      .from(schema.outbox)
      .where(eq(schema.outbox.authScope, authScope)),
  );
  const dirtyNodes = docsTxRows(
    tx.select({ id: schema.knowledgeNodes.id })
      .from(schema.knowledgeNodes)
      .where(eq(schema.knowledgeNodes.dirty, true)),
  );
  const dirtySupertags = docsTxRows(
    tx.select({ id: schema.knowledgeSupertags.id })
      .from(schema.knowledgeSupertags)
      .where(eq(schema.knowledgeSupertags.dirty, true)),
  );
  const dirtyNodeSupertags = docsTxRows(
    tx.select({
      nodeId: schema.knowledgeNodeSupertags.nodeId,
      supertagId: schema.knowledgeNodeSupertags.supertagId,
    })
      .from(schema.knowledgeNodeSupertags)
      .where(eq(schema.knowledgeNodeSupertags.dirty, true)),
  );
  const dirtyFieldValues = docsTxRows(
    tx.select({
      nodeId: schema.knowledgeFieldValues.nodeId,
      fieldId: schema.knowledgeFieldValues.fieldId,
    })
      .from(schema.knowledgeFieldValues)
      .where(eq(schema.knowledgeFieldValues.dirty, true)),
  );
  // A rolling test double without synchronous reads cannot safely make a
  // scope decision; the normal async quarantine path remains available there.
  if (!membershipRows || !allMembershipRows || !outboxRows) return;
  const refs = new Map<string, number>();
  for (const row of allMembershipRows) {
    if (row.state === "deleted") continue;
    const key = `${row.tableName}:${row.entityKey}`;
    refs.set(key, (refs.get(key) ?? 0) + 1);
  }
  const scopedKeys = new Set(
    membershipRows
      .filter((row) => mode === "revoke" ? row.state !== "deleted" : row.state === "active")
      .map((row) => `${row.tableName}:${row.entityKey}`),
  );
  const scopedOutboxRows = outboxRows.filter((row) =>
    (DOCS_SCOPE_TABLE_NAMES as readonly string[]).includes(row.tableName)
    && row.docsScopeKey === scopeKey,
  );
  const dirtyKeys = new Set<string>();
  for (const row of dirtyNodes ?? []) dirtyKeys.add(`knowledge_nodes:${row.id}`);
  for (const row of dirtySupertags ?? []) dirtyKeys.add(`knowledge_supertags:${row.id}`);
  for (const row of dirtyNodeSupertags ?? []) dirtyKeys.add(`knowledge_node_supertags:${row.nodeId}:${row.supertagId}`);
  for (const row of dirtyFieldValues ?? []) dirtyKeys.add(`knowledge_field_values:${row.nodeId}:${row.fieldId}`);
  for (const compound of scopedKeys) {
    const separator = compound.indexOf(":");
    const table = compound.slice(0, separator);
    const key = compound.slice(separator + 1);
    const protectedRow = dirtyKeys.has(compound)
      || scopedOutboxRows.some((row) => `${row.tableName}:${row.entityId}` === compound);
    const hasOtherWritableMembership = allMembershipRows.some(
      (row) => row.tableName === table
        && row.entityKey === key
        && row.scopeKey !== scopeKey
        && row.state === "active"
        && row.readOnly !== true
        && row.access !== "read",
    );
    if (protectedRow) {
      if (mode === "downgrade" && table === "knowledge_nodes" && !hasOtherWritableMembership) {
        tx.update(schema.knowledgeNodes)
          .set({ access: "read", readOnly: true })
          .where(eq(schema.knowledgeNodes.id, key))
          .run();
      }
      tx.update(schema.docsScopeMembership)
        .set({ state: "blocked", updatedAt: now })
        .where(and(
          eq(schema.docsScopeMembership.authScope, authScope),
          eq(schema.docsScopeMembership.scopeKey, scopeKey),
          eq(schema.docsScopeMembership.tableName, table),
          eq(schema.docsScopeMembership.entityKey, key),
        ))
        .run();
      continue;
    }
    if (mode === "downgrade") {
      if (table === "knowledge_nodes" && !hasOtherWritableMembership) {
        tx.update(schema.knowledgeNodes)
          .set({ access: "read", readOnly: true })
          .where(eq(schema.knowledgeNodes.id, key))
          .run();
      }
      tx.update(schema.docsScopeMembership)
        .set({ state: "readonly", access: "read", readOnly: true, updatedAt: now })
        .where(and(
          eq(schema.docsScopeMembership.authScope, authScope),
          eq(schema.docsScopeMembership.scopeKey, scopeKey),
          eq(schema.docsScopeMembership.tableName, table),
          eq(schema.docsScopeMembership.entityKey, key),
        ))
        .run();
      continue;
    }
    tx.delete(schema.docsScopeMembership)
      .where(and(
        eq(schema.docsScopeMembership.authScope, authScope),
        eq(schema.docsScopeMembership.scopeKey, scopeKey),
        eq(schema.docsScopeMembership.tableName, table),
        eq(schema.docsScopeMembership.entityKey, key),
      ))
      .run();
    if ((refs.get(compound) ?? 0) > 1) continue;
    deleteDocsLiveRowTx(tx, table, key);
  }
  for (const row of scopedOutboxRows) {
    tx.update(schema.outbox)
      .set({
        retryCount: 5,
        lastError: `quarantine:scope_${mode === "revoke" ? "revoked" : "downgraded"}`,
        blockedReason: mode === "revoke" ? "docs_scope_revoked" : "docs_scope_downgraded",
        docsScopeKey: scopeKey,
      })
      .where(eq(schema.outbox.opId, row.opId))
      .run();
  }
}

function reconcileDocsScopeSetTx(
  tx: any,
  authScope: string,
  previousScopes: DocsScopeSetEntry[],
  newScopes: DocsScopeSetEntry[],
  now: string,
): void {
  const nextKeys = new Set(newScopes.map(docsScopeKeyFromEntry));
  for (const previous of previousScopes) {
    if (!nextKeys.has(docsScopeKeyFromEntry(previous))) {
      quarantineDocsScopeTx(tx, authScope, previous, "revoke", now);
    }
  }
  const previousByKey = new Map(previousScopes.map((scope) => [docsScopeKeyFromEntry(scope), scope]));
  for (const next of newScopes) {
    const previous = previousByKey.get(docsScopeKeyFromEntry(next));
    if (previous && !previous.read_only && next.read_only) {
      quarantineDocsScopeTx(tx, authScope, next, "downgrade", now);
    }
  }
}

/**
 * Atomically apply one fully validated staged Docs snapshot.  The sync engine
 * owns the run/cursor rows; this helper deliberately receives immutable staged
 * values so a failed promotion leaves both live rows and staging untouched.
 * Dirty/outbox rows are never overwritten, including force resyncs; the server
 * payload is retained as a conflict snapshot instead.
 */
async function promoteDocsSyncRunCompatibility(
  options: DocsSyncPromotionOptions,
): Promise<DocsSyncPromotionTelemetry> {
  const db = getDb();
  const now = new Date().toISOString();
  const tableNames = [
    "knowledge_nodes",
    "knowledge_supertags",
    "knowledge_node_supertags",
    "knowledge_supertag_fields",
    "knowledge_fields",
    "knowledge_field_values",
    "knowledge_node_placements",
    "knowledge_edges",
  ];
  const preservation = new Map<string, DocsPreservationSets>();
  const scopeKey = options.scopeKey
    ?? `${options.scopeId ?? "personal"}|project:${options.projectId ?? ""}`;
  for (const table of tableNames) {
    preservation.set(
      table,
      await loadDocsPreservationSets(table, options.authScope, scopeKey),
    );
  }
  const dirtyNodes = new Set(
    (
      await db
        .select({ id: schema.knowledgeNodes.id })
        .from(schema.knowledgeNodes)
        .where(eq(schema.knowledgeNodes.dirty, true))
    ).map((row) => row.id),
  );
  const dirtySupertags = new Set(
    (
      await db
        .select({ id: schema.knowledgeSupertags.id })
        .from(schema.knowledgeSupertags)
        .where(eq(schema.knowledgeSupertags.dirty, true))
    ).map((row) => row.id),
  );
  const dirtyNodeSupertags = new Set(
    (
      await db
        .select({
          nodeId: schema.knowledgeNodeSupertags.nodeId,
          supertagId: schema.knowledgeNodeSupertags.supertagId,
        })
        .from(schema.knowledgeNodeSupertags)
        .where(eq(schema.knowledgeNodeSupertags.dirty, true))
    ).map((row) => `${row.nodeId}:${row.supertagId}`),
  );
  const dirtyFieldValues = new Set(
    (
      await db
        .select({
          nodeId: schema.knowledgeFieldValues.nodeId,
          fieldId: schema.knowledgeFieldValues.fieldId,
        })
        .from(schema.knowledgeFieldValues)
        .where(eq(schema.knowledgeFieldValues.dirty, true))
    ).map((row) => `${row.nodeId}:${row.fieldId}`),
  );
  const isProtected = (table: string, key: string): boolean => {
    const sets = preservation.get(table);
    if (sets?.outbox.has(key)) return true;
    if (table === "knowledge_nodes" && dirtyNodes.has(key)) return true;
    if (table === "knowledge_supertags" && dirtySupertags.has(key)) return true;
    if (table === "knowledge_node_supertags" && dirtyNodeSupertags.has(key)) return true;
    if (table === "knowledge_field_values" && dirtyFieldValues.has(key)) return true;
    return Boolean(sets?.dirty.has(key));
  };
  // Do not materialize the complete staging table for production-sized
  // snapshots.  The compatibility `staged` path remains available to small
  // repository tests and older callers; the sync engine intentionally omits
  // it and uses the bounded SQLite iterator below.
  const stagedTelemetry: DocsSyncPromotionTelemetry = {
    source: options.staged ? "memory" : "staging",
    rowsRead: options.staged?.length ?? 0,
    batches: options.staged ? (options.staged.length ? 1 : 0) : 0,
    maxBatchSize: options.staged?.length ?? 0,
  };
  let membershipRows = schema.docsScopeMembership
    ? await db
        .select()
        .from(schema.docsScopeMembership)
        .where(
          and(
            eq(schema.docsScopeMembership.authScope, options.authScope),
            eq(schema.docsScopeMembership.scopeKey, scopeKey),
          ),
        )
    : [];
  // Include all account memberships to avoid deleting a shared UUID while a
  // different project/library scope still references it.
  let allMembershipRows = schema.docsScopeMembership
    ? await db
        .select()
        .from(schema.docsScopeMembership)
        .where(eq(schema.docsScopeMembership.authScope, options.authScope))
    : [];
  const membershipByTable = new Map<string, Set<string>>();
  const membershipRefs = new Map<string, number>();
  for (const row of allMembershipRows) {
    if (row.state === "deleted") continue;
    const bucket = membershipByTable.get(row.tableName) ?? new Set<string>();
    bucket.add(row.entityKey);
    membershipByTable.set(row.tableName, bucket);
    const refKey = `${row.tableName}:${row.entityKey}`;
    membershipRefs.set(refKey, (membershipRefs.get(refKey) ?? 0) + 1);
  }
  const scopedMembership = new Map<string, Set<string>>();
  for (const row of membershipRows) {
    // Readonly/blocked rows still belong to this scope until an explicit
    // revoke/tombstone removes them.  Keeping them in the current-scope set
    // lets a later tombstone remove the row while preserving sibling refs.
    if (row.state === "deleted") continue;
    const bucket = scopedMembership.get(row.tableName) ?? new Set<string>();
    bucket.add(row.entityKey);
    scopedMembership.set(row.tableName, bucket);
  }
  const ensureMembership = (table: string, key: string): void => {
    const scopeBucket = scopedMembership.get(table) ?? new Set<string>();
    if (scopeBucket.has(key)) return;
    scopeBucket.add(key);
    scopedMembership.set(table, scopeBucket);
    const bucket = membershipByTable.get(table) ?? new Set<string>();
    bucket.add(key);
    membershipByTable.set(table, bucket);
    const refKey = `${table}:${key}`;
    membershipRefs.set(refKey, (membershipRefs.get(refKey) ?? 0) + 1);
  };
  const removeMembership = (table: string, key: string): void => {
    const scopeBucket = scopedMembership.get(table);
    if (!scopeBucket?.has(key)) return;
    scopeBucket.delete(key);
    const bucket = membershipByTable.get(table);
    if (!bucket?.has(key)) return;
    bucket.delete(key);
    const refKey = `${table}:${key}`;
    const count = (membershipRefs.get(refKey) ?? 1) - 1;
    if (count > 0) membershipRefs.set(refKey, count);
    else membershipRefs.delete(refKey);
  };
  const scopeAuthoritative = (table: string): Set<string> | null => {
    const ids = options.authoritative[table]?.ids;
    return ids == null ? null : new Set(ids);
  };
  const workspaceId = options.scopeId
    ?? options.authoritative.knowledge_nodes?.scopeId
    ?? options.authoritative.knowledge_supertags?.scopeId
    ?? options.authoritative.knowledge_fields?.scopeId;
  let legacyScopedNodeIds: string[] = [];
  let legacyScopedSupertagIds: string[] = [];
  let legacyScopedFieldIds: string[] = [];
  if (!schema.docsScopeMembership && workspaceId) {
    [legacyScopedNodeIds, legacyScopedSupertagIds, legacyScopedFieldIds] = await Promise.all([
      db
        .select({ id: schema.knowledgeNodes.id })
        .from(schema.knowledgeNodes)
        .where(eq(schema.knowledgeNodes.workspaceId, workspaceId))
        .then((rows) => rows.map((row) => row.id)),
      db
        .select({ id: schema.knowledgeSupertags.id })
        .from(schema.knowledgeSupertags)
        .where(eq(schema.knowledgeSupertags.workspaceId, workspaceId))
        .then((rows) => rows.map((row) => row.id)),
      db
        .select({ id: schema.knowledgeFields.id })
        .from(schema.knowledgeFields)
        .where(eq(schema.knowledgeFields.workspaceId, workspaceId))
        .then((rows) => rows.map((row) => row.id)),
    ]);
  }
  const scopedNodeIds = schema.docsScopeMembership
    ? [...(scopedMembership.get("knowledge_nodes") ?? new Set<string>())]
    : legacyScopedNodeIds;
  const scopedSupertagIds = schema.docsScopeMembership
    ? [...(scopedMembership.get("knowledge_supertags") ?? new Set<string>())]
    : legacyScopedSupertagIds;
  const scopedFieldIds = schema.docsScopeMembership
    ? [...(scopedMembership.get("knowledge_fields") ?? new Set<string>())]
    : legacyScopedFieldIds;
  // Membership-aware promotion reconciles relation keys from the scoped
  // projection below.  Scanning every relation table is both unnecessary and
  // expensive on a large library; retain the full-table fallback only for
  // pre-membership databases that have no scope discriminator at all.
  let localNodeSupertags: Array<{ nodeId: string; supertagId: string }> = [];
  let localFieldValues: Array<{ nodeId: string; fieldId: string }> = [];
  let localSupertagFields: Array<{ supertagId: string; fieldId: string }> = [];
  let localPlacements: Array<{ id: string; nodeId: string; parentNodeId: string }> = [];
  let localEdges: Array<{ id: string; sourceNodeId: string; targetNodeId: string }> = [];
  if (!schema.docsScopeMembership) {
    [localNodeSupertags, localFieldValues, localSupertagFields, localPlacements, localEdges] = await Promise.all([
      db.select({ nodeId: schema.knowledgeNodeSupertags.nodeId, supertagId: schema.knowledgeNodeSupertags.supertagId })
        .from(schema.knowledgeNodeSupertags),
      db.select({ nodeId: schema.knowledgeFieldValues.nodeId, fieldId: schema.knowledgeFieldValues.fieldId })
        .from(schema.knowledgeFieldValues),
      db.select({ supertagId: schema.knowledgeSupertagFields.supertagId, fieldId: schema.knowledgeSupertagFields.fieldId })
        .from(schema.knowledgeSupertagFields),
      db.select({ id: schema.knowledgeNodePlacements.id, nodeId: schema.knowledgeNodePlacements.nodeId, parentNodeId: schema.knowledgeNodePlacements.parentNodeId })
        .from(schema.knowledgeNodePlacements),
      db.select({ id: schema.knowledgeEdges.id, sourceNodeId: schema.knowledgeEdges.sourceNodeId, targetNodeId: schema.knowledgeEdges.targetNodeId })
        .from(schema.knowledgeEdges),
    ]);
  }
  const authoritativeIds = scopeAuthoritative;
  let staleNodes = authoritativeIds("knowledge_nodes") && scopedNodeIds.filter(
    (id) => !authoritativeIds("knowledge_nodes")!.has(id),
  );
  let staleSupertags = authoritativeIds("knowledge_supertags") && scopedSupertagIds.filter(
    (id) => !authoritativeIds("knowledge_supertags")!.has(id),
  );
  let staleFields = authoritativeIds("knowledge_fields") && scopedFieldIds.filter(
    (id) => !authoritativeIds("knowledge_fields")!.has(id),
  );
  // The terminal page is the authoritative membership set even when no row
  // changed in this run (digest-equal pages may have empty `changes`).
  if (schema.docsScopeMembership) {
    for (const table of tableNames) {
      for (const key of authoritativeIds(table) ?? []) {
        ensureMembership(table, key);
      }
    }
  }
  const txAny = (tx: any, table: any) => tx.insert(table);
  const setConflict = (tx: any, table: string, key: string, payload: unknown) => {
    const conflictPayload = payload as never;
    if (table === "knowledge_nodes") {
      tx.update(schema.knowledgeNodes)
        .set({ conflictPayload })
        .where(eq(schema.knowledgeNodes.id, key))
        .run();
    } else if (table === "knowledge_supertags") {
      tx.update(schema.knowledgeSupertags)
        .set({ conflictPayload })
        .where(eq(schema.knowledgeSupertags.id, key))
        .run();
    } else if (table === "knowledge_node_supertags") {
      const [nodeId, supertagId] = key.split(":", 2);
      tx.update(schema.knowledgeNodeSupertags)
        .set({ conflictPayload })
        .where(and(eq(schema.knowledgeNodeSupertags.nodeId, nodeId), eq(schema.knowledgeNodeSupertags.supertagId, supertagId)))
        .run();
    } else if (table === "knowledge_field_values") {
      const [nodeId, fieldId] = key.split(":", 2);
      tx.update(schema.knowledgeFieldValues)
        .set({ conflictPayload })
        .where(and(eq(schema.knowledgeFieldValues.nodeId, nodeId), eq(schema.knowledgeFieldValues.fieldId, fieldId)))
        .run();
    }
    tx.update(schema.outbox)
      .set({ conflictPayload: conflictPayload as never })
      .where(
        and(
          eq(schema.outbox.authScope, options.authScope),
          eq(schema.outbox.tableName, table),
          eq(schema.outbox.entityId, key),
          schema.outbox.docsScopeKey
            ? eq(schema.outbox.docsScopeKey, scopeKey)
            : undefined,
        ),
      )
      .run();
  };
  const upsertMembership = (tx: any, table: string, key: string, state = "active") => {
    if (!schema.docsScopeMembership || !workspaceId) return;
    const scopeMeta = options.scopes?.find(
      (scope) => scope.workspace_id === workspaceId
        && (scope.project_id ?? null) === (options.projectId ?? null),
    );
    tx.insert(schema.docsScopeMembership)
      .values({
        authScope: options.authScope,
        scopeKey,
        scopeId: workspaceId,
        projectId: options.projectId ?? null,
        tableName: table,
        entityKey: key,
        state,
        access: scopeMeta?.access ?? null,
        readOnly: scopeMeta?.read_only ?? null,
        updatedAt: now,
      })
      .onConflictDoUpdate({
        target: [
          schema.docsScopeMembership.authScope,
          schema.docsScopeMembership.scopeKey,
          schema.docsScopeMembership.tableName,
          schema.docsScopeMembership.entityKey,
        ],
        set: {
          scopeId: workspaceId,
          projectId: options.projectId ?? null,
          state,
          access: scopeMeta?.access ?? null,
          readOnly: scopeMeta?.read_only ?? null,
          updatedAt: now,
        },
      })
      .run();
  };
  const removeMembershipTx = (tx: any, table: string, key: string): void => {
    if (!schema.docsScopeMembership) return;
    tx.delete(schema.docsScopeMembership)
      .where(
        and(
          eq(schema.docsScopeMembership.authScope, options.authScope),
          eq(schema.docsScopeMembership.scopeKey, scopeKey),
          eq(schema.docsScopeMembership.tableName, table),
          eq(schema.docsScopeMembership.entityKey, key),
        ),
      )
      .run();
  };
  const blockMembershipTx = (tx: any, table: string, key: string): void => {
    if (!schema.docsScopeMembership) return;
    tx.update(schema.docsScopeMembership)
      .set({ state: "blocked", updatedAt: now })
      .where(
        and(
          eq(schema.docsScopeMembership.authScope, options.authScope),
          eq(schema.docsScopeMembership.scopeKey, scopeKey),
          eq(schema.docsScopeMembership.tableName, table),
          eq(schema.docsScopeMembership.entityKey, key),
        ),
      )
      .run();
  };
  const applyOne = (tx: any, row: DocsSyncStagedRow): void => {
    const payload = row.payload ?? {};
    const key = row.entityKey || docsCompositeKey(payload);
    if (!key) return;
    if (isProtected(row.tableName, key)) {
      upsertMembership(tx, row.tableName, key, "blocked");
      setConflict(tx, row.tableName, key, payload);
      return;
    }
    if (row.isTombstone || payload.deleted === true) {
      removeMembershipTx(tx, row.tableName, key);
      removeMembership(row.tableName, key);
      if (schema.docsScopeMembership && membershipRefs.has(`${row.tableName}:${key}`)) {
        return;
      }
      if (row.tableName === "knowledge_nodes") tx.delete(schema.knowledgeNodes).where(eq(schema.knowledgeNodes.id, key)).run();
      else if (row.tableName === "knowledge_supertags") tx.delete(schema.knowledgeSupertags).where(eq(schema.knowledgeSupertags.id, key)).run();
      else if (row.tableName === "knowledge_node_supertags") {
        const [nodeId, supertagId] = key.split(":", 2);
        tx.delete(schema.knowledgeNodeSupertags).where(and(eq(schema.knowledgeNodeSupertags.nodeId, nodeId), eq(schema.knowledgeNodeSupertags.supertagId, supertagId))).run();
      } else if (row.tableName === "knowledge_field_values") {
        const [nodeId, fieldId] = key.split(":", 2);
        tx.delete(schema.knowledgeFieldValues).where(and(eq(schema.knowledgeFieldValues.nodeId, nodeId), eq(schema.knowledgeFieldValues.fieldId, fieldId))).run();
      } else if (row.tableName === "knowledge_fields") {
        tx.delete(schema.knowledgeFields).where(eq(schema.knowledgeFields.id, key)).run();
      } else if (row.tableName === "knowledge_supertag_fields") {
        const [supertagId, fieldId] = key.split(":", 2);
        tx.delete(schema.knowledgeSupertagFields).where(and(eq(schema.knowledgeSupertagFields.supertagId, supertagId), eq(schema.knowledgeSupertagFields.fieldId, fieldId))).run();
      } else if (row.tableName === "knowledge_node_placements") tx.delete(schema.knowledgeNodePlacements).where(eq(schema.knowledgeNodePlacements.id, key)).run();
      else if (row.tableName === "knowledge_edges") tx.delete(schema.knowledgeEdges).where(eq(schema.knowledgeEdges.id, key)).run();
      return;
    }
    upsertMembership(tx, row.tableName, key);
    if (row.tableName === "knowledge_nodes") {
      const values = remoteDocsNodeValues(payload as unknown as DocsNode, now);
      txAny(tx, schema.knowledgeNodes).values(values).onConflictDoUpdate({ target: schema.knowledgeNodes.id, set: remoteDocsNodeUpdateSet(values) }).run();
    } else if (row.tableName === "knowledge_supertags") {
      txAny(tx, schema.knowledgeSupertags).values({
        id: payload.id,
        workspaceId: payload.workspace_id ?? null,
        parentSupertagId: payload.parent_supertag_id ?? null,
        systemKey: payload.system_key ?? null,
        name: payload.name ?? "",
        baseType: payload.base_type ?? null,
        description: payload.description ?? null,
        icon: payload.icon ?? null,
        color: payload.color ?? null,
        templateJson: payload.template_json ?? null,
        pinnedFieldIds: payload.pinned_field_ids ?? [],
        configJson: payload.config_json ?? null,
        titleTemplate: payload.title_template ?? null,
        aiInstructions: payload.ai_instructions ?? null,
        createdAt: payload.created_at ?? now,
        updatedAt: payload.updated_at ?? now,
        serverUpdatedAt: payload.updated_at ?? now,
        dirty: false,
        conflictPayload: null,
      }).onConflictDoUpdate({ target: schema.knowledgeSupertags.id, set: {
        workspaceId: payload.workspace_id ?? null,
        parentSupertagId: payload.parent_supertag_id ?? null,
        systemKey: payload.system_key ?? null,
        name: payload.name ?? "",
        baseType: payload.base_type ?? null,
        description: payload.description ?? null,
        icon: payload.icon ?? null,
        color: payload.color ?? null,
        templateJson: payload.template_json ?? null,
        pinnedFieldIds: payload.pinned_field_ids ?? [],
        configJson: payload.config_json ?? null,
        titleTemplate: payload.title_template ?? null,
        aiInstructions: payload.ai_instructions ?? null,
        updatedAt: payload.updated_at ?? now,
        serverUpdatedAt: payload.updated_at ?? now,
        dirty: false,
        conflictPayload: null,
      } }).run();
    } else if (row.tableName === "knowledge_fields") {
      const values = {
        id: payload.id,
        workspaceId: payload.workspace_id ?? null,
        supertagId: payload.supertag_id ?? null,
        systemKey: payload.system_key ?? null,
        name: payload.name ?? "",
        fieldType: payload.field_type ?? "text",
        required: Boolean(payload.required),
        optionsJson: payload.options_json ?? null,
        defaultValueJson: payload.default_value_json ?? null,
        sortOrder: payload.sort_order ?? null,
        createdAt: payload.created_at ?? now,
        updatedAt: payload.updated_at ?? now,
      };
      txAny(tx, schema.knowledgeFields).values(values).onConflictDoUpdate({ target: schema.knowledgeFields.id, set: values }).run();
    } else if (row.tableName === "knowledge_supertag_fields") {
      txAny(tx, schema.knowledgeSupertagFields).values({
        supertagId: payload.supertag_id,
        fieldId: payload.field_id,
        sortOrder: payload.sort_order ?? null,
        required: Boolean(payload.required),
        showInTemplate: Boolean(payload.show_in_template),
        optional: Boolean(payload.optional),
        createdAt: payload.created_at ?? null,
      }).onConflictDoUpdate({ target: [schema.knowledgeSupertagFields.supertagId, schema.knowledgeSupertagFields.fieldId], set: {
        sortOrder: payload.sort_order ?? null,
        required: Boolean(payload.required),
        showInTemplate: Boolean(payload.show_in_template),
        optional: Boolean(payload.optional),
        createdAt: payload.created_at ?? null,
      } }).run();
    } else if (row.tableName === "knowledge_node_supertags") {
      txAny(tx, schema.knowledgeNodeSupertags).values({
        nodeId: payload.node_id,
        supertagId: payload.supertag_id,
        createdAt: payload.created_at ?? null,
        updatedAt: payload.updated_at ?? payload.created_at ?? null,
        serverUpdatedAt: payload.updated_at ?? payload.created_at ?? null,
        dirty: false,
        conflictPayload: null,
        createdBy: payload.created_by ?? null,
      }).onConflictDoUpdate({ target: [schema.knowledgeNodeSupertags.nodeId, schema.knowledgeNodeSupertags.supertagId], set: {
        createdAt: payload.created_at ?? null,
        updatedAt: payload.updated_at ?? payload.created_at ?? null,
        serverUpdatedAt: payload.updated_at ?? payload.created_at ?? null,
        dirty: false,
        conflictPayload: null,
        createdBy: payload.created_by ?? null,
      } }).run();
    } else if (row.tableName === "knowledge_field_values") {
      txAny(tx, schema.knowledgeFieldValues).values({
        nodeId: payload.node_id,
        fieldId: payload.field_id,
        valueJson: payload.value_json ?? null,
        valueText: payload.value_text ?? null,
        valueNumber: payload.value_number ?? null,
        valueDatetime: payload.value_datetime ?? null,
        targetNodeId: payload.target_node_id ?? null,
        updatedAt: payload.updated_at ?? now,
        serverUpdatedAt: payload.updated_at ?? now,
        dirty: false,
        conflictPayload: null,
        updatedBy: payload.updated_by ?? null,
      }).onConflictDoUpdate({ target: [schema.knowledgeFieldValues.nodeId, schema.knowledgeFieldValues.fieldId], set: {
        valueJson: payload.value_json ?? null,
        valueText: payload.value_text ?? null,
        valueNumber: payload.value_number ?? null,
        valueDatetime: payload.value_datetime ?? null,
        targetNodeId: payload.target_node_id ?? null,
        updatedAt: payload.updated_at ?? now,
        serverUpdatedAt: payload.updated_at ?? now,
        dirty: false,
        conflictPayload: null,
        updatedBy: payload.updated_by ?? null,
      } }).run();
    } else if (row.tableName === "knowledge_node_placements") {
      txAny(tx, schema.knowledgeNodePlacements).values({
        id: payload.id,
        nodeId: payload.node_id,
        parentNodeId: payload.parent_node_id,
        sortOrder: payload.sort_order ?? null,
        collapsed: Boolean(payload.collapsed),
        createdBy: payload.created_by ?? null,
        createdAt: payload.created_at ?? null,
      }).onConflictDoUpdate({ target: schema.knowledgeNodePlacements.id, set: {
        nodeId: payload.node_id,
        parentNodeId: payload.parent_node_id,
        sortOrder: payload.sort_order ?? null,
        collapsed: Boolean(payload.collapsed),
        createdBy: payload.created_by ?? null,
        createdAt: payload.created_at ?? null,
      } }).run();
    } else if (row.tableName === "knowledge_edges") {
      txAny(tx, schema.knowledgeEdges).values({
        id: payload.id,
        sourceNodeId: payload.source_node_id,
        targetNodeId: payload.target_node_id,
        relationType: payload.relation_type ?? null,
        confidence: payload.confidence ?? null,
        createdBy: payload.created_by ?? null,
        createdAt: payload.created_at ?? null,
      }).onConflictDoUpdate({ target: schema.knowledgeEdges.id, set: {
        sourceNodeId: payload.source_node_id,
        targetNodeId: payload.target_node_id,
        relationType: payload.relation_type ?? null,
        confidence: payload.confidence ?? null,
        createdBy: payload.created_by ?? null,
        createdAt: payload.created_at ?? null,
      } }).run();
    }
  };
  const applyStagedRows = (tx: any): void => {
    if (options.staged) {
      // Keep the old table ordering for compatibility callers.  This path is
      // intentionally only used when a caller explicitly supplies an array.
      const grouped = new Map<string, DocsSyncStagedRow[]>();
      for (const row of options.staged) {
        const bucket = grouped.get(row.tableName) ?? [];
        bucket.push(row);
        grouped.set(row.tableName, bucket);
      }
      for (const table of tableNames) {
        for (const row of grouped.get(table) ?? []) applyOne(tx, row);
      }
      return;
    }

    // Expo SQLite exposes synchronous `.all()` reads inside a transaction.
    // Read each table with a keyset cursor so only one bounded batch of JSON
    // payloads is retained at any point.  The staging rows are not deleted
    // until the transaction reaches its finalization block, so this cursor is
    // stable for the whole atomic promotion.
    if (typeof tx.select !== "function") {
      throw new Error("Docs staging promotion requires transactional SQLite reads");
    }
    for (const table of tableNames) {
      let lastEntityKey: string | null = null;
      while (true) {
        const predicates: any[] = [
          eq(schema.docsSyncStaging.runId, options.runId),
          eq(schema.docsSyncStaging.authScope, options.authScope),
          eq(schema.docsSyncStaging.tableName, table),
          ...(lastEntityKey == null
            ? []
            : [gt(schema.docsSyncStaging.entityKey, lastEntityKey)]),
        ];
        const query: any = tx
          .select()
          .from(schema.docsSyncStaging)
          .where(and(...predicates))
          .orderBy(asc(schema.docsSyncStaging.entityKey))
          .limit(DOCS_STAGED_PROMOTION_BATCH_SIZE);
        const rows = docsTxRows(query);
        if (rows == null) {
          throw new Error("Docs staging promotion query does not support bounded reads");
        }
        if (!rows.length) break;
        stagedTelemetry.batches += 1;
        stagedTelemetry.rowsRead += rows.length;
        stagedTelemetry.maxBatchSize = Math.max(
          stagedTelemetry.maxBatchSize,
          rows.length,
        );
        for (const row of rows) {
          let payload: Record<string, unknown> | null = null;
          if (row.payloadJson && typeof row.payloadJson === "object") {
            payload = row.payloadJson as Record<string, unknown>;
          } else if (typeof row.payloadJson === "string") {
            try {
              const parsed = JSON.parse(row.payloadJson) as unknown;
              payload = parsed && typeof parsed === "object"
                ? parsed as Record<string, unknown>
                : null;
            } catch {
              payload = null;
            }
          }
          applyOne(tx, {
            tableName: String(row.tableName ?? table),
            entityKey: String(row.entityKey ?? ""),
            payload,
            isTombstone: Boolean(row.isTombstone),
          });
        }
        const nextEntityKey: unknown = rows[rows.length - 1]?.entityKey;
        if (typeof nextEntityKey !== "string" || !nextEntityKey.length) break;
        if (nextEntityKey === lastEntityKey) break;
        lastEntityKey = nextEntityKey;
        if (rows.length < DOCS_STAGED_PROMOTION_BATCH_SIZE) break;
      }
    }
  };
  db.transaction((tx) => {
    // Expo SQLite transactions expose synchronous `.all()` reads.  Refresh
    // protection state at the transaction boundary so an outbox/dirty edit
    // created after the preflight snapshot cannot be overwritten by this
    // promotion.  Legacy test doubles may not expose `.all()`; their preflight
    // sets remain the compatibility fallback.
    if (typeof tx.select === "function") {
      const txSelectAll = (query: any): any[] => {
        try {
          return typeof query?.all === "function" ? query.all() : [];
        } catch {
          return [];
        }
      };
      for (const table of tableNames) {
        const rows = txSelectAll(
          tx
            .select({
              entityId: schema.outbox.entityId,
              docsScopeKey: schema.outbox.docsScopeKey,
            })
            .from(schema.outbox)
            .where(
              and(
                eq(schema.outbox.authScope, options.authScope),
                eq(schema.outbox.tableName, table),
              ),
            ),
        );
        const current = preservation.get(table) ?? {
          outbox: new Set<string>(),
          dirty: new Set<string>(),
        };
        for (const row of rows) {
          if (
            row.entityId != null
            && row.docsScopeKey === scopeKey
          ) current.outbox.add(String(row.entityId));
        }
        preservation.set(table, current);
      }
      const dirtyNodeRows = txSelectAll(
        tx.select({ id: schema.knowledgeNodes.id })
          .from(schema.knowledgeNodes)
          .where(eq(schema.knowledgeNodes.dirty, true)),
      );
      const dirtySupertagRows = txSelectAll(
        tx.select({ id: schema.knowledgeSupertags.id })
          .from(schema.knowledgeSupertags)
          .where(eq(schema.knowledgeSupertags.dirty, true)),
      );
      const dirtyNodeSupertagRows = txSelectAll(
        tx.select({
          nodeId: schema.knowledgeNodeSupertags.nodeId,
          supertagId: schema.knowledgeNodeSupertags.supertagId,
        })
          .from(schema.knowledgeNodeSupertags)
          .where(eq(schema.knowledgeNodeSupertags.dirty, true)),
      );
      const dirtyFieldValueRows = txSelectAll(
        tx.select({
          nodeId: schema.knowledgeFieldValues.nodeId,
          fieldId: schema.knowledgeFieldValues.fieldId,
        })
          .from(schema.knowledgeFieldValues)
          .where(eq(schema.knowledgeFieldValues.dirty, true)),
      );
      for (const row of dirtyNodeRows) dirtyNodes.add(row.id);
      for (const row of dirtySupertagRows) dirtySupertags.add(row.id);
      for (const row of dirtyNodeSupertagRows) {
        dirtyNodeSupertags.add(`${row.nodeId}:${row.supertagId}`);
      }
      for (const row of dirtyFieldValueRows) {
        dirtyFieldValues.add(`${row.nodeId}:${row.fieldId}`);
      }
      if (schema.docsScopeMembership) {
        // Membership can change while preflight queries are awaiting.  Rebuild
        // the scope/ref maps from the transaction snapshot before deriving
        // stale IDs so a concurrent sibling project keeps a shared UUID alive.
        const currentRows = txSelectAll(
          tx
            .select()
            .from(schema.docsScopeMembership)
            .where(
              and(
                eq(schema.docsScopeMembership.authScope, options.authScope),
                eq(schema.docsScopeMembership.scopeKey, scopeKey),
              ),
            ),
        );
        const currentAllRows = txSelectAll(
          tx
            .select()
            .from(schema.docsScopeMembership)
            .where(eq(schema.docsScopeMembership.authScope, options.authScope)),
        );
        membershipByTable.clear();
        membershipRefs.clear();
        scopedMembership.clear();
        for (const row of currentAllRows) {
          if (row.state === "deleted") continue;
          const bucket = membershipByTable.get(row.tableName) ?? new Set<string>();
          bucket.add(row.entityKey);
          membershipByTable.set(row.tableName, bucket);
          const refKey = `${row.tableName}:${row.entityKey}`;
          membershipRefs.set(refKey, (membershipRefs.get(refKey) ?? 0) + 1);
        }
        for (const row of currentRows) {
          if (row.state === "deleted") continue;
          const bucket = scopedMembership.get(row.tableName) ?? new Set<string>();
          bucket.add(row.entityKey);
          scopedMembership.set(row.tableName, bucket);
        }
        const currentNodeIds = [...(scopedMembership.get("knowledge_nodes") ?? new Set<string>())];
        const currentSupertagIds = [...(scopedMembership.get("knowledge_supertags") ?? new Set<string>())];
        const currentFieldIds = [...(scopedMembership.get("knowledge_fields") ?? new Set<string>())];
        staleNodes = authoritativeIds("knowledge_nodes")
          && currentNodeIds.filter((id) => !authoritativeIds("knowledge_nodes")!.has(id));
        staleSupertags = authoritativeIds("knowledge_supertags")
          && currentSupertagIds.filter((id) => !authoritativeIds("knowledge_supertags")!.has(id));
        staleFields = authoritativeIds("knowledge_fields")
          && currentFieldIds.filter((id) => !authoritativeIds("knowledge_fields")!.has(id));
      }
    }
    // Persist the complete authoritative membership set in the same
    // transaction as live rows.  This is what makes same-UUID entities from
    // two project scopes safe across a restart or partial page failure.
    if (schema.docsScopeMembership) {
      for (const table of tableNames) {
        for (const key of authoritativeIds(table) ?? []) {
          upsertMembership(tx, table, key);
        }
      }
    }
    applyStagedRows(tx);
    // Backend sends tombstones as an empty list and authoritative_ids on the
    // terminal page.  Reconcile only the requested workspace; never delete a
    // row belonging to another account/scope that happens to share SQLite.
    for (const id of staleNodes || []) {
      if (isProtected("knowledge_nodes", id)) {
        blockMembershipTx(tx, "knowledge_nodes", id);
        setConflict(tx, "knowledge_nodes", id, {
          id,
          deleted: true,
          authoritative_scope_id: workspaceId,
        });
        tx.update(schema.knowledgeNodes)
          .set({ access: "read", readOnly: true })
          .where(eq(schema.knowledgeNodes.id, id))
          .run();
        continue;
      }
      removeMembershipTx(tx, "knowledge_nodes", id);
      removeMembership("knowledge_nodes", id);
      if (schema.docsScopeMembership && membershipRefs.has(`knowledge_nodes:${id}`)) {
        continue;
      }
      if (!schema.docsScopeMembership) {
        tx.delete(schema.knowledgeNodeSupertags)
          .where(inArray(schema.knowledgeNodeSupertags.nodeId, [id]))
          .run();
        tx.delete(schema.knowledgeFieldValues)
          .where(inArray(schema.knowledgeFieldValues.nodeId, [id]))
          .run();
        tx.delete(schema.knowledgeNodePlacements)
          .where(
            drizzleOrm.or(
              eq(schema.knowledgeNodePlacements.nodeId, id),
              eq(schema.knowledgeNodePlacements.parentNodeId, id),
            ),
          )
          .run();
        tx.delete(schema.knowledgeEdges)
          .where(
            drizzleOrm.or(
              eq(schema.knowledgeEdges.sourceNodeId, id),
              eq(schema.knowledgeEdges.targetNodeId, id),
            ),
          )
          .run();
      }
      tx.delete(schema.knowledgeNodes).where(eq(schema.knowledgeNodes.id, id)).run();
    }
    for (const id of staleSupertags || []) {
      if (isProtected("knowledge_supertags", id)) {
        blockMembershipTx(tx, "knowledge_supertags", id);
        setConflict(tx, "knowledge_supertags", id, {
          id,
          deleted: true,
          authoritative_scope_id: workspaceId,
        });
        continue;
      }
      removeMembershipTx(tx, "knowledge_supertags", id);
      removeMembership("knowledge_supertags", id);
      if (schema.docsScopeMembership && membershipRefs.has(`knowledge_supertags:${id}`)) {
        continue;
      }
      if (!schema.docsScopeMembership) {
        tx.delete(schema.knowledgeNodeSupertags)
          .where(eq(schema.knowledgeNodeSupertags.supertagId, id))
          .run();
        tx.delete(schema.knowledgeSupertagFields)
          .where(eq(schema.knowledgeSupertagFields.supertagId, id))
          .run();
      }
      tx.delete(schema.knowledgeSupertags)
        .where(eq(schema.knowledgeSupertags.id, id))
        .run();
    }
    for (const id of staleFields || []) {
      if (isProtected("knowledge_fields", id)) {
        blockMembershipTx(tx, "knowledge_fields", id);
        setConflict(tx, "knowledge_fields", id, {
          id,
          deleted: true,
          authoritative_scope_id: workspaceId,
        });
        continue;
      }
      removeMembershipTx(tx, "knowledge_fields", id);
      removeMembership("knowledge_fields", id);
      if (schema.docsScopeMembership && membershipRefs.has(`knowledge_fields:${id}`)) {
        continue;
      }
      if (!schema.docsScopeMembership) {
        tx.delete(schema.knowledgeFieldValues)
          .where(eq(schema.knowledgeFieldValues.fieldId, id))
          .run();
        tx.delete(schema.knowledgeSupertagFields)
          .where(eq(schema.knowledgeSupertagFields.fieldId, id))
          .run();
      }
      tx.delete(schema.knowledgeFields).where(eq(schema.knowledgeFields.id, id)).run();
    }
    const nodeScope = new Set(
      schema.docsScopeMembership
        ? [...(scopedMembership.get("knowledge_nodes") ?? new Set<string>())]
        : scopedNodeIds,
    );
    const supertagScope = new Set(
      schema.docsScopeMembership
        ? [...(scopedMembership.get("knowledge_supertags") ?? new Set<string>())]
        : scopedSupertagIds,
    );
    const fieldScope = new Set(
      schema.docsScopeMembership
        ? [...(scopedMembership.get("knowledge_fields") ?? new Set<string>())]
        : scopedFieldIds,
    );
    const reconcileRelation = (
      table: string,
      key: string,
      inScope: boolean,
      authoritative: Set<string> | null,
      remove: () => void,
    ) => {
      if (!inScope || !authoritative || authoritative.has(key)) return;
      if (isProtected(table, key)) {
        blockMembershipTx(tx, table, key);
        const [first, second] = key.split(":", 2);
        setConflict(tx, table, key, {
          ...(table === "knowledge_node_supertags"
            ? { node_id: first, supertag_id: second }
            : table === "knowledge_field_values"
              ? { node_id: first, field_id: second }
              : { supertag_id: first, field_id: second }),
          deleted: true,
        });
      } else {
        removeMembershipTx(tx, table, key);
        removeMembership(table, key);
        if (!schema.docsScopeMembership || !membershipRefs.has(`${table}:${key}`)) {
          remove();
        }
      }
    };
    const nodeSupertagAuth = authoritativeIds("knowledge_node_supertags");
    for (const row of localNodeSupertags) {
      const key = `${row.nodeId}:${row.supertagId}`;
      reconcileRelation(
        "knowledge_node_supertags",
        key,
        nodeScope.has(row.nodeId) || supertagScope.has(row.supertagId),
        nodeSupertagAuth,
        () => tx.delete(schema.knowledgeNodeSupertags).where(and(eq(schema.knowledgeNodeSupertags.nodeId, row.nodeId), eq(schema.knowledgeNodeSupertags.supertagId, row.supertagId))).run(),
      );
    }
    const fieldValueAuth = authoritativeIds("knowledge_field_values");
    for (const row of localFieldValues) {
      const key = `${row.nodeId}:${row.fieldId}`;
      reconcileRelation(
        "knowledge_field_values",
        key,
        nodeScope.has(row.nodeId) || fieldScope.has(row.fieldId),
        fieldValueAuth,
        () => tx.delete(schema.knowledgeFieldValues).where(and(eq(schema.knowledgeFieldValues.nodeId, row.nodeId), eq(schema.knowledgeFieldValues.fieldId, row.fieldId))).run(),
      );
    }
    const supertagFieldAuth = authoritativeIds("knowledge_supertag_fields");
    for (const row of localSupertagFields) {
      const key = `${row.supertagId}:${row.fieldId}`;
      reconcileRelation(
        "knowledge_supertag_fields",
        key,
        supertagScope.has(row.supertagId) || fieldScope.has(row.fieldId),
        supertagFieldAuth,
        () => tx.delete(schema.knowledgeSupertagFields).where(and(eq(schema.knowledgeSupertagFields.supertagId, row.supertagId), eq(schema.knowledgeSupertagFields.fieldId, row.fieldId))).run(),
      );
    }
    const placementAuth = authoritativeIds("knowledge_node_placements");
    for (const row of localPlacements) {
      if (!placementAuth || placementAuth.has(row.id)) continue;
      if (nodeScope.has(row.nodeId) || nodeScope.has(row.parentNodeId)) {
        if (isProtected("knowledge_node_placements", row.id)) {
          blockMembershipTx(tx, "knowledge_node_placements", row.id);
          setConflict(tx, "knowledge_node_placements", row.id, {
            id: row.id,
            deleted: true,
          });
          continue;
        }
        removeMembershipTx(tx, "knowledge_node_placements", row.id);
        removeMembership("knowledge_node_placements", row.id);
        if (!schema.docsScopeMembership || !membershipRefs.has(`knowledge_node_placements:${row.id}`)) {
          tx.delete(schema.knowledgeNodePlacements).where(eq(schema.knowledgeNodePlacements.id, row.id)).run();
        }
      }
    }
    const edgeAuth = authoritativeIds("knowledge_edges");
    for (const row of localEdges) {
      if (!edgeAuth || edgeAuth.has(row.id)) continue;
      if (nodeScope.has(row.sourceNodeId) || nodeScope.has(row.targetNodeId)) {
        if (isProtected("knowledge_edges", row.id)) {
          blockMembershipTx(tx, "knowledge_edges", row.id);
          setConflict(tx, "knowledge_edges", row.id, {
            id: row.id,
            deleted: true,
          });
          continue;
        }
        removeMembershipTx(tx, "knowledge_edges", row.id);
        removeMembership("knowledge_edges", row.id);
        if (!schema.docsScopeMembership || !membershipRefs.has(`knowledge_edges:${row.id}`)) {
          tx.delete(schema.knowledgeEdges).where(eq(schema.knowledgeEdges.id, row.id)).run();
        }
      }
    }
    if (schema.docsScopeMembership) {
      // With membership available, stale relations are reconciled directly
      // from the current scope's entity keys.  This preserves hard deletes
      // even when the server sends an empty changes/tombstones page, without
      // scanning every relation row in the local library.
      for (const key of scopedMembership.get("knowledge_node_supertags") ?? []) {
        reconcileRelation(
          "knowledge_node_supertags",
          key,
          true,
          nodeSupertagAuth,
          () => {
            const [nodeId, supertagId] = key.split(":", 2);
            tx.delete(schema.knowledgeNodeSupertags)
              .where(and(eq(schema.knowledgeNodeSupertags.nodeId, nodeId), eq(schema.knowledgeNodeSupertags.supertagId, supertagId)))
              .run();
          },
        );
      }
      for (const key of scopedMembership.get("knowledge_field_values") ?? []) {
        reconcileRelation(
          "knowledge_field_values",
          key,
          true,
          fieldValueAuth,
          () => {
            const [nodeId, fieldId] = key.split(":", 2);
            tx.delete(schema.knowledgeFieldValues)
              .where(and(eq(schema.knowledgeFieldValues.nodeId, nodeId), eq(schema.knowledgeFieldValues.fieldId, fieldId)))
              .run();
          },
        );
      }
      for (const key of scopedMembership.get("knowledge_supertag_fields") ?? []) {
        reconcileRelation(
          "knowledge_supertag_fields",
          key,
          true,
          supertagFieldAuth,
          () => {
            const [supertagId, fieldId] = key.split(":", 2);
            tx.delete(schema.knowledgeSupertagFields)
              .where(and(eq(schema.knowledgeSupertagFields.supertagId, supertagId), eq(schema.knowledgeSupertagFields.fieldId, fieldId)))
              .run();
          },
        );
      }
      for (const key of scopedMembership.get("knowledge_node_placements") ?? []) {
        if (!placementAuth || placementAuth.has(key)) continue;
        if (isProtected("knowledge_node_placements", key)) {
          blockMembershipTx(tx, "knowledge_node_placements", key);
          setConflict(tx, "knowledge_node_placements", key, { id: key, deleted: true });
          continue;
        }
        removeMembershipTx(tx, "knowledge_node_placements", key);
        removeMembership("knowledge_node_placements", key);
        if (!membershipRefs.has(`knowledge_node_placements:${key}`)) {
          tx.delete(schema.knowledgeNodePlacements)
            .where(eq(schema.knowledgeNodePlacements.id, key))
            .run();
        }
      }
      for (const key of scopedMembership.get("knowledge_edges") ?? []) {
        if (!edgeAuth || edgeAuth.has(key)) continue;
        if (isProtected("knowledge_edges", key)) {
          blockMembershipTx(tx, "knowledge_edges", key);
          setConflict(tx, "knowledge_edges", key, { id: key, deleted: true });
          continue;
        }
        removeMembershipTx(tx, "knowledge_edges", key);
        removeMembership("knowledge_edges", key);
        if (!membershipRefs.has(`knowledge_edges:${key}`)) {
          tx.delete(schema.knowledgeEdges)
            .where(eq(schema.knowledgeEdges.id, key))
            .run();
        }
      }
    }
    if (options.scopeSet) {
      reconcileDocsScopeSetTx(
        tx,
        options.authScope,
        options.scopeSet.previousScopes,
        options.scopeSet.newScopes,
        options.finalize?.serverTime ?? now,
      );
    }
    if (options.finalize) {
      const finalize = options.finalize;
      const saveSyncState = (
        tableName: string,
        values: { lastPulledAt?: string | null; cursor?: string | null },
      ) => {
        tx.insert(schema.syncState)
          .values({
            tableName,
            lastPulledAt: values.lastPulledAt ?? null,
            lastPushedAt: null,
            cursor: values.cursor ?? null,
          })
          .onConflictDoUpdate({
            target: schema.syncState.tableName,
            set: values,
          })
          .run();
      };
      for (const [table, digest] of Object.entries(finalize.digestByTable)) {
        const key = finalize.digestKeys[table];
        if (key && digest) saveSyncState(key, { cursor: digest });
      }
      saveSyncState(finalize.scopeDigestKey, { cursor: finalize.scopeDigest });
      if (finalize.scopeRevisionKey && finalize.scopeRevision) {
        saveSyncState(finalize.scopeRevisionKey, {
          cursor: finalize.scopeRevision,
        });
      }
      saveSyncState(finalize.lastPulledKey, { lastPulledAt: finalize.serverTime });
      if (finalize.scopesKey && options.scopes) {
        saveSyncState(finalize.scopesKey, {
          cursor: JSON.stringify(options.scopes),
        });
      }
      if (finalize.workspaceKey && finalize.workspaceId) {
        saveSyncState(finalize.workspaceKey, {
          cursor: finalize.workspaceId,
        });
      }
      tx.update(schema.docsSyncRuns)
        .set({ state: "completed", updatedAt: finalize.serverTime })
        .where(
          and(
            eq(schema.docsSyncRuns.runId, options.runId),
            eq(schema.docsSyncRuns.authScope, options.authScope),
          ),
        )
        .run();
      tx.delete(schema.docsSyncStaging)
        .where(
          and(
            eq(schema.docsSyncStaging.runId, options.runId),
            eq(schema.docsSyncStaging.authScope, options.authScope),
          ),
        )
        .run();
    }
  });
  return stagedTelemetry;
}

/* ------------------------------------------------------------------------- *
 * Native async promotion
 * ------------------------------------------------------------------------- *
 * The compatibility implementation above is intentionally retained for
 * callers that explicitly pass `options.staged`.  The sync engine never does
 * that: production snapshots use the native async implementation below so
 * every read and write participates in one SQLite exclusive transaction.
 */

type RawDocsMembership = {
  auth_scope: string;
  scope_key: string;
  scope_id: string;
  project_id: string | null;
  table_name: string;
  entity_key: string;
  state: string;
  access: string | null;
  read_only: unknown;
  updated_at?: string;
};

type RawDocsOutbox = {
  op_id: string;
  table_name: string;
  entity_id: string;
  docs_scope_key: string | null;
  auth_scope?: string | null;
};

type RawDocsStaging = {
  table_name: string;
  entity_key: string;
  payload_json: unknown;
  is_tombstone: unknown;
};

type RawDirtyRows = {
  nodes: Set<string>;
  supertags: Set<string>;
  nodeSupertags: Set<string>;
  fieldValues: Set<string>;
};

function sqliteBoolean(value: unknown): boolean {
  return value === true || value === 1 || value === "1";
}

function decodeDocsSqlJson<T>(value: unknown, fallback: T): T {
  if (typeof value !== "string") return (value as T) ?? fallback;
  try {
    return (JSON.parse(value) as T) ?? fallback;
  } catch {
    return fallback;
  }
}

function sqlJson(value: unknown): string | null {
  return encodeDocsJson(value);
}

function sqlBoolean(value: unknown): number | null {
  return encodeDocsBoolean(value);
}

async function docsTxAll<T>(
  tx: DocsSqliteAsyncTransaction,
  source: string,
  ...params: unknown[]
): Promise<T[]> {
  return tx.getAllAsync<T>(source, ...params);
}

async function loadDocsDirtyRowsTx(
  tx: DocsSqliteAsyncTransaction,
): Promise<RawDirtyRows> {
  const nodes = await docsTxAll<{ id: string }>(
    tx,
    "SELECT id FROM knowledge_nodes WHERE dirty = 1",
  );
  const supertags = await docsTxAll<{ id: string }>(
    tx,
    "SELECT id FROM knowledge_supertags WHERE dirty = 1",
  );
  const nodeSupertags = await docsTxAll<{ node_id: string; supertag_id: string }>(
    tx,
    "SELECT node_id, supertag_id FROM knowledge_node_supertags WHERE dirty = 1",
  );
  const fieldValues = await docsTxAll<{ node_id: string; field_id: string }>(
    tx,
    "SELECT node_id, field_id FROM knowledge_field_values WHERE dirty = 1",
  );
  return {
    nodes: new Set(nodes.map((row) => row.id)),
    supertags: new Set(supertags.map((row) => row.id)),
    nodeSupertags: new Set(
      nodeSupertags.map((row) => `${row.node_id}:${row.supertag_id}`),
    ),
    fieldValues: new Set(
      fieldValues.map((row) => `${row.node_id}:${row.field_id}`),
    ),
  };
}

async function loadDocsPreservationSetsTx(
  tx: DocsSqliteAsyncTransaction,
  table: string,
  authScope: string,
  scopeKey: string,
): Promise<DocsPreservationSets> {
  const outboxRows = await docsTxAll<{
    entity_id: string;
    auth_scope: string | null;
    docs_scope_key: string | null;
  }>(
    tx,
    "SELECT entity_id, auth_scope, docs_scope_key FROM outbox WHERE table_name = ?",
    table,
  );
  const outbox = new Set(
    outboxRows
      .filter((row) =>
        (row.auth_scope == null || row.auth_scope === authScope)
        && (
          !scopeKey
          || !table.startsWith("knowledge_")
          || row.docs_scope_key === scopeKey
        ),
      )
      .map((row) => row.entity_id),
  );
  const dirty = new Set<string>();
  if (table === "knowledge_nodes") {
    const rows = await docsTxAll<{ id: string }>(
      tx,
      "SELECT id FROM knowledge_nodes WHERE dirty = 1",
    );
    for (const row of rows) dirty.add(row.id);
  } else if (table === "knowledge_supertags") {
    const rows = await docsTxAll<{ id: string }>(
      tx,
      "SELECT id FROM knowledge_supertags WHERE dirty = 1",
    );
    for (const row of rows) dirty.add(row.id);
  } else if (table === "knowledge_node_supertags") {
    const rows = await docsTxAll<{ node_id: string; supertag_id: string }>(
      tx,
      "SELECT node_id, supertag_id FROM knowledge_node_supertags WHERE dirty = 1",
    );
    for (const row of rows) dirty.add(`${row.node_id}:${row.supertag_id}`);
  } else if (table === "knowledge_field_values") {
    const rows = await docsTxAll<{ node_id: string; field_id: string }>(
      tx,
      "SELECT node_id, field_id FROM knowledge_field_values WHERE dirty = 1",
    );
    for (const row of rows) dirty.add(`${row.node_id}:${row.field_id}`);
  }
  return { outbox, dirty };
}

async function deleteDocsLiveRowAsync(
  tx: DocsSqliteAsyncTransaction,
  table: string,
  key: string,
): Promise<void> {
  if (table === "knowledge_nodes") {
    await tx.runAsync("DELETE FROM knowledge_nodes WHERE id = ?", key);
  } else if (table === "knowledge_supertags") {
    await tx.runAsync("DELETE FROM knowledge_supertags WHERE id = ?", key);
  } else if (table === "knowledge_fields") {
    await tx.runAsync("DELETE FROM knowledge_fields WHERE id = ?", key);
  } else if (table === "knowledge_node_supertags") {
    const [nodeId, supertagId] = key.split(":", 2);
    await tx.runAsync(
      "DELETE FROM knowledge_node_supertags WHERE node_id = ? AND supertag_id = ?",
      nodeId,
      supertagId,
    );
  } else if (table === "knowledge_supertag_fields") {
    const [supertagId, fieldId] = key.split(":", 2);
    await tx.runAsync(
      "DELETE FROM knowledge_supertag_fields WHERE supertag_id = ? AND field_id = ?",
      supertagId,
      fieldId,
    );
  } else if (table === "knowledge_field_values") {
    const [nodeId, fieldId] = key.split(":", 2);
    await tx.runAsync(
      "DELETE FROM knowledge_field_values WHERE node_id = ? AND field_id = ?",
      nodeId,
      fieldId,
    );
  } else if (table === "knowledge_node_placements") {
    await tx.runAsync("DELETE FROM knowledge_node_placements WHERE id = ?", key);
  } else if (table === "knowledge_edges") {
    await tx.runAsync("DELETE FROM knowledge_edges WHERE id = ?", key);
  }
}

async function setDocsConflictAsync(
  tx: DocsSqliteAsyncTransaction,
  options: DocsSyncPromotionOptions,
  scopeKey: string,
  table: string,
  key: string,
  payload: unknown,
): Promise<void> {
  const conflictPayload = sqlJson(payload);
  if (table === "knowledge_nodes") {
    await tx.runAsync(
      "UPDATE knowledge_nodes SET conflict_payload = ? WHERE id = ?",
      conflictPayload,
      key,
    );
  } else if (table === "knowledge_supertags") {
    await tx.runAsync(
      "UPDATE knowledge_supertags SET conflict_payload = ? WHERE id = ?",
      conflictPayload,
      key,
    );
  } else if (table === "knowledge_node_supertags") {
    const [nodeId, supertagId] = key.split(":", 2);
    await tx.runAsync(
      "UPDATE knowledge_node_supertags SET conflict_payload = ? WHERE node_id = ? AND supertag_id = ?",
      conflictPayload,
      nodeId,
      supertagId,
    );
  } else if (table === "knowledge_field_values") {
    const [nodeId, fieldId] = key.split(":", 2);
    await tx.runAsync(
      "UPDATE knowledge_field_values SET conflict_payload = ? WHERE node_id = ? AND field_id = ?",
      conflictPayload,
      nodeId,
      fieldId,
    );
  }
  await tx.runAsync(
    `UPDATE outbox SET conflict_payload = ?
       WHERE auth_scope = ? AND table_name = ? AND entity_id = ?
         AND docs_scope_key = ?`,
    conflictPayload,
    options.authScope,
    table,
    key,
    scopeKey,
  );
}

async function upsertDocsMembershipAsync(
  tx: DocsSqliteAsyncTransaction,
  options: DocsSyncPromotionOptions,
  scopeKey: string,
  workspaceId: string | undefined,
  table: string,
  key: string,
  state: string,
  now: string,
): Promise<void> {
  if (!workspaceId) return;
  const scopeMeta = options.scopes?.find(
    (scope) => scope.workspace_id === workspaceId
      && (scope.project_id ?? null) === (options.projectId ?? null),
  );
  await tx.runAsync(
    `INSERT INTO docs_scope_membership(
       auth_scope, scope_key, scope_id, project_id, table_name, entity_key,
       state, access, read_only, updated_at
     ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
     ON CONFLICT(auth_scope, scope_key, table_name, entity_key) DO UPDATE SET
       scope_id = excluded.scope_id,
       project_id = excluded.project_id,
       state = excluded.state,
       access = excluded.access,
       read_only = excluded.read_only,
       updated_at = excluded.updated_at`,
    options.authScope,
    scopeKey,
    workspaceId,
    options.projectId ?? null,
    table,
    key,
    state,
    scopeMeta?.access ?? null,
    sqlBoolean(scopeMeta?.read_only),
    now,
  );
}

async function removeDocsMembershipAsync(
  tx: DocsSqliteAsyncTransaction,
  authScope: string,
  scopeKey: string,
  table: string,
  key: string,
): Promise<void> {
  await tx.runAsync(
    `DELETE FROM docs_scope_membership
       WHERE auth_scope = ? AND scope_key = ? AND table_name = ? AND entity_key = ?`,
    authScope,
    scopeKey,
    table,
    key,
  );
}

async function blockDocsMembershipAsync(
  tx: DocsSqliteAsyncTransaction,
  authScope: string,
  scopeKey: string,
  table: string,
  key: string,
  now: string,
): Promise<void> {
  await tx.runAsync(
    `UPDATE docs_scope_membership SET state = 'blocked', updated_at = ?
       WHERE auth_scope = ? AND scope_key = ? AND table_name = ? AND entity_key = ?`,
    now,
    authScope,
    scopeKey,
    table,
    key,
  );
}

async function quarantineDocsScopeAsync(
  tx: DocsSqliteAsyncTransaction,
  authScope: string,
  scope: DocsScopeSetEntry,
  mode: "revoke" | "downgrade",
  now: string,
  requestedScopeKey?: string,
): Promise<void> {
  const scopeKey = requestedScopeKey ?? docsScopeKeyFromEntry(scope);
  const membershipRows = await docsTxAll<RawDocsMembership>(
    tx,
    `SELECT auth_scope, scope_key, scope_id, project_id, table_name, entity_key,
            state, access, read_only, updated_at
       FROM docs_scope_membership
      WHERE auth_scope = ? AND scope_key = ?`,
    authScope,
    scopeKey,
  );
  const allMembershipRows = await docsTxAll<RawDocsMembership>(
    tx,
    `SELECT auth_scope, scope_key, scope_id, project_id, table_name, entity_key,
            state, access, read_only, updated_at
       FROM docs_scope_membership
      WHERE auth_scope = ?`,
    authScope,
  );
  const outboxRows = await docsTxAll<RawDocsOutbox>(
    tx,
    `SELECT op_id, table_name, entity_id, docs_scope_key
       FROM outbox WHERE auth_scope = ?`,
    authScope,
  );
  const dirtyRows = await loadDocsDirtyRowsTx(tx);
  const refs = new Map<string, number>();
  for (const row of allMembershipRows) {
    if (row.state === "deleted") continue;
    const refKey = `${row.table_name}:${row.entity_key}`;
    refs.set(refKey, (refs.get(refKey) ?? 0) + 1);
  }
  const scopedKeys = new Set(
    membershipRows
      .filter((row) => mode === "revoke" ? row.state !== "deleted" : row.state === "active")
      .map((row) => `${row.table_name}:${row.entity_key}`),
  );
  const scopedOutboxRows = outboxRows.filter((row) =>
    (DOCS_SCOPE_TABLE_NAMES as readonly string[]).includes(row.table_name)
    && row.docs_scope_key === scopeKey,
  );
  const dirtyKeys = new Set<string>();
  for (const id of dirtyRows.nodes) dirtyKeys.add(`knowledge_nodes:${id}`);
  for (const id of dirtyRows.supertags) dirtyKeys.add(`knowledge_supertags:${id}`);
  for (const id of dirtyRows.nodeSupertags) dirtyKeys.add(`knowledge_node_supertags:${id}`);
  for (const id of dirtyRows.fieldValues) dirtyKeys.add(`knowledge_field_values:${id}`);

  for (const compound of scopedKeys) {
    const separator = compound.indexOf(":");
    const table = compound.slice(0, separator);
    const key = compound.slice(separator + 1);
    const protectedRow = dirtyKeys.has(compound)
      || scopedOutboxRows.some((row) => `${row.table_name}:${row.entity_id}` === compound);
    const hasOtherWritableMembership = allMembershipRows.some(
      (row) => row.table_name === table
        && row.entity_key === key
        && row.scope_key !== scopeKey
        && row.state === "active"
        && !sqliteBoolean(row.read_only)
        && row.access !== "read",
    );
    if (protectedRow) {
      if (mode === "downgrade" && table === "knowledge_nodes" && !hasOtherWritableMembership) {
        await tx.runAsync(
          "UPDATE knowledge_nodes SET access = 'read', read_only = 1 WHERE id = ?",
          key,
        );
      }
      await blockDocsMembershipAsync(tx, authScope, scopeKey, table, key, now);
      continue;
    }
    if (mode === "downgrade") {
      if (table === "knowledge_nodes" && !hasOtherWritableMembership) {
        await tx.runAsync(
          "UPDATE knowledge_nodes SET access = 'read', read_only = 1 WHERE id = ?",
          key,
        );
      }
      await tx.runAsync(
        `UPDATE docs_scope_membership
            SET state = 'readonly', access = 'read', read_only = 1, updated_at = ?
          WHERE auth_scope = ? AND scope_key = ? AND table_name = ? AND entity_key = ?`,
        now,
        authScope,
        scopeKey,
        table,
        key,
      );
      continue;
    }
    await removeDocsMembershipAsync(tx, authScope, scopeKey, table, key);
    if ((refs.get(compound) ?? 0) > 1) continue;
    await deleteDocsLiveRowAsync(tx, table, key);
  }
  for (const row of scopedOutboxRows) {
    await tx.runAsync(
      `UPDATE outbox SET retry_count = 5,
          last_error = ?, blocked_reason = ?, docs_scope_key = ?
        WHERE op_id = ?`,
      `quarantine:scope_${mode === "revoke" ? "revoked" : "downgraded"}`,
      mode === "revoke" ? "docs_scope_revoked" : "docs_scope_downgraded",
      scopeKey,
      row.op_id,
    );
  }
}

async function upsertDocsNodeAsync(
  tx: DocsSqliteAsyncTransaction,
  payload: Record<string, unknown>,
  now: string,
): Promise<void> {
  const node = payload as unknown as DocsNode;
  const values = remoteDocsNodeValues(node, now);
  await tx.runAsync(
    `INSERT INTO knowledge_nodes(
       id, workspace_id, parent_id, root_page_id, project_id, source, access,
       read_only, system_key, title, aliases, description, body_json, body_text,
       node_type, display_props, query_json, view_json, day_date, sort_order,
       created_by, updated_by, created_at, updated_at, server_updated_at,
       dirty, conflict_payload, archived_at
     ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
               ?, ?, ?, ?, ?, ?, ?, ?)
     ON CONFLICT(id) DO UPDATE SET
       workspace_id = excluded.workspace_id,
       parent_id = excluded.parent_id,
       root_page_id = excluded.root_page_id,
       project_id = excluded.project_id,
       source = excluded.source,
       access = excluded.access,
       read_only = excluded.read_only,
       system_key = excluded.system_key,
       title = excluded.title,
       aliases = excluded.aliases,
       description = excluded.description,
       body_json = excluded.body_json,
       body_text = excluded.body_text,
       node_type = excluded.node_type,
       display_props = excluded.display_props,
       query_json = excluded.query_json,
       view_json = excluded.view_json,
       day_date = excluded.day_date,
       sort_order = excluded.sort_order,
       updated_by = excluded.updated_by,
       updated_at = excluded.updated_at,
       server_updated_at = excluded.server_updated_at,
       dirty = excluded.dirty,
       conflict_payload = excluded.conflict_payload,
       archived_at = excluded.archived_at`,
    values.id,
    values.workspaceId,
    values.parentId,
    values.rootPageId,
    values.projectId,
    values.source,
    values.access,
    sqlBoolean(values.readOnly),
    values.systemKey,
    values.title,
    sqlJson(values.aliases),
    values.description,
    sqlJson(values.bodyJson),
    values.bodyText,
    values.nodeType,
    sqlJson(values.displayProps),
    sqlJson(values.queryJson),
    sqlJson(values.viewJson),
    values.dayDate,
    values.sortOrder,
    values.createdBy,
    values.updatedBy,
    values.createdAt,
    values.updatedAt,
    values.serverUpdatedAt,
    sqlBoolean(values.dirty),
    sqlJson(values.conflictPayload),
    values.archivedAt,
  );
}

async function upsertDocsSupertagAsync(
  tx: DocsSqliteAsyncTransaction,
  payload: Record<string, unknown>,
  now: string,
): Promise<void> {
  await tx.runAsync(
    `INSERT INTO knowledge_supertags(
       id, workspace_id, parent_supertag_id, system_key, name, base_type,
       description, icon, color, template_json, pinned_field_ids, config_json,
       title_template, ai_instructions, created_at, updated_at, server_updated_at,
       dirty, conflict_payload
     ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
     ON CONFLICT(id) DO UPDATE SET
       workspace_id = excluded.workspace_id,
       parent_supertag_id = excluded.parent_supertag_id,
       system_key = excluded.system_key,
       name = excluded.name,
       base_type = excluded.base_type,
       description = excluded.description,
       icon = excluded.icon,
       color = excluded.color,
       template_json = excluded.template_json,
       pinned_field_ids = excluded.pinned_field_ids,
       config_json = excluded.config_json,
       title_template = excluded.title_template,
       ai_instructions = excluded.ai_instructions,
       updated_at = excluded.updated_at,
       server_updated_at = excluded.server_updated_at,
       dirty = excluded.dirty,
       conflict_payload = excluded.conflict_payload`,
    payload.id,
    payload.workspace_id ?? null,
    payload.parent_supertag_id ?? null,
    payload.system_key ?? null,
    payload.name ?? "",
    payload.base_type ?? null,
    payload.description ?? null,
    payload.icon ?? null,
    payload.color ?? null,
    sqlJson(payload.template_json),
    sqlJson(payload.pinned_field_ids ?? []),
    sqlJson(payload.config_json),
    payload.title_template ?? null,
    payload.ai_instructions ?? null,
    payload.created_at ?? now,
    payload.updated_at ?? now,
    payload.updated_at ?? now,
    sqlBoolean(false),
    null,
  );
}

async function upsertDocsFieldAsync(
  tx: DocsSqliteAsyncTransaction,
  payload: Record<string, unknown>,
  now: string,
): Promise<void> {
  await tx.runAsync(
    `INSERT INTO knowledge_fields(
       id, workspace_id, supertag_id, system_key, name, field_type, required,
       options_json, default_value_json, sort_order, created_at, updated_at
     ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
     ON CONFLICT(id) DO UPDATE SET
       workspace_id = excluded.workspace_id,
       supertag_id = excluded.supertag_id,
       system_key = excluded.system_key,
       name = excluded.name,
       field_type = excluded.field_type,
       required = excluded.required,
       options_json = excluded.options_json,
       default_value_json = excluded.default_value_json,
       sort_order = excluded.sort_order,
       created_at = excluded.created_at,
       updated_at = excluded.updated_at`,
    payload.id,
    payload.workspace_id ?? null,
    payload.supertag_id ?? null,
    payload.system_key ?? null,
    payload.name ?? "",
    payload.field_type ?? "text",
    sqlBoolean(Boolean(payload.required)),
    sqlJson(payload.options_json),
    sqlJson(payload.default_value_json),
    payload.sort_order ?? null,
    payload.created_at ?? now,
    payload.updated_at ?? now,
  );
}

async function upsertDocsSupertagFieldAsync(
  tx: DocsSqliteAsyncTransaction,
  payload: Record<string, unknown>,
): Promise<void> {
  await tx.runAsync(
    `INSERT INTO knowledge_supertag_fields(
       supertag_id, field_id, sort_order, required, show_in_template, optional, created_at
     ) VALUES (?, ?, ?, ?, ?, ?, ?)
     ON CONFLICT(supertag_id, field_id) DO UPDATE SET
       sort_order = excluded.sort_order,
       required = excluded.required,
       show_in_template = excluded.show_in_template,
       optional = excluded.optional,
       created_at = excluded.created_at`,
    payload.supertag_id,
    payload.field_id,
    payload.sort_order ?? null,
    sqlBoolean(Boolean(payload.required)),
    sqlBoolean(Boolean(payload.show_in_template)),
    sqlBoolean(Boolean(payload.optional)),
    payload.created_at ?? null,
  );
}

async function upsertDocsNodeSupertagAsync(
  tx: DocsSqliteAsyncTransaction,
  payload: Record<string, unknown>,
): Promise<void> {
  await tx.runAsync(
    `INSERT INTO knowledge_node_supertags(
       node_id, supertag_id, created_at, updated_at, server_updated_at,
       dirty, conflict_payload, created_by
     ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
     ON CONFLICT(node_id, supertag_id) DO UPDATE SET
       created_at = excluded.created_at,
       updated_at = excluded.updated_at,
       server_updated_at = excluded.server_updated_at,
       dirty = excluded.dirty,
       conflict_payload = excluded.conflict_payload,
       created_by = excluded.created_by`,
    payload.node_id,
    payload.supertag_id,
    payload.created_at ?? null,
    payload.updated_at ?? payload.created_at ?? null,
    payload.updated_at ?? payload.created_at ?? null,
    sqlBoolean(false),
    null,
    payload.created_by ?? null,
  );
}

async function upsertDocsFieldValueAsync(
  tx: DocsSqliteAsyncTransaction,
  payload: Record<string, unknown>,
  now: string,
): Promise<void> {
  await tx.runAsync(
    `INSERT INTO knowledge_field_values(
       node_id, field_id, value_json, value_text, value_number, value_datetime,
       target_node_id, updated_at, server_updated_at, dirty, conflict_payload, updated_by
     ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
     ON CONFLICT(node_id, field_id) DO UPDATE SET
       value_json = excluded.value_json,
       value_text = excluded.value_text,
       value_number = excluded.value_number,
       value_datetime = excluded.value_datetime,
       target_node_id = excluded.target_node_id,
       updated_at = excluded.updated_at,
       server_updated_at = excluded.server_updated_at,
       dirty = excluded.dirty,
       conflict_payload = excluded.conflict_payload,
       updated_by = excluded.updated_by`,
    payload.node_id,
    payload.field_id,
    sqlJson(payload.value_json),
    payload.value_text ?? null,
    payload.value_number ?? null,
    payload.value_datetime ?? null,
    payload.target_node_id ?? null,
    payload.updated_at ?? now,
    payload.updated_at ?? now,
    sqlBoolean(false),
    null,
    payload.updated_by ?? null,
  );
}

async function upsertDocsPlacementAsync(
  tx: DocsSqliteAsyncTransaction,
  payload: Record<string, unknown>,
): Promise<void> {
  await tx.runAsync(
    `INSERT INTO knowledge_node_placements(
       id, node_id, parent_node_id, sort_order, collapsed, created_by, created_at
     ) VALUES (?, ?, ?, ?, ?, ?, ?)
     ON CONFLICT(id) DO UPDATE SET
       node_id = excluded.node_id,
       parent_node_id = excluded.parent_node_id,
       sort_order = excluded.sort_order,
       collapsed = excluded.collapsed,
       created_by = excluded.created_by,
       created_at = excluded.created_at`,
    payload.id,
    payload.node_id,
    payload.parent_node_id,
    payload.sort_order ?? null,
    sqlBoolean(Boolean(payload.collapsed)),
    payload.created_by ?? null,
    payload.created_at ?? null,
  );
}

async function upsertDocsEdgeAsync(
  tx: DocsSqliteAsyncTransaction,
  payload: Record<string, unknown>,
): Promise<void> {
  await tx.runAsync(
    `INSERT INTO knowledge_edges(
       id, source_node_id, target_node_id, relation_type, confidence, created_by, created_at
     ) VALUES (?, ?, ?, ?, ?, ?, ?)
     ON CONFLICT(id) DO UPDATE SET
       source_node_id = excluded.source_node_id,
       target_node_id = excluded.target_node_id,
       relation_type = excluded.relation_type,
       confidence = excluded.confidence,
       created_by = excluded.created_by,
       created_at = excluded.created_at`,
    payload.id,
    payload.source_node_id,
    payload.target_node_id,
    payload.relation_type ?? null,
    payload.confidence ?? null,
    payload.created_by ?? null,
    payload.created_at ?? null,
  );
}

async function promoteDocsSyncRunAsync(
  options: DocsSyncPromotionOptions,
): Promise<DocsSyncPromotionTelemetry> {
  const now = new Date().toISOString();
  const tableNames = [...DOCS_SCOPE_TABLE_NAMES];
  const scopeKey = options.scopeKey
    ?? `${options.scopeId ?? "personal"}|project:${options.projectId ?? ""}`;

  return withDocsExclusiveTransaction(async (tx) => {
    // Everything below, including the protection snapshot, runs on the same
    // native transaction object.  No Drizzle query is allowed to observe or
    // mutate the promotion while it is in flight.
    const membershipRows = await docsTxAll<RawDocsMembership>(
      tx,
      `SELECT auth_scope, scope_key, scope_id, project_id, table_name, entity_key,
              state, access, read_only, updated_at
         FROM docs_scope_membership
        WHERE auth_scope = ? AND scope_key = ?`,
      options.authScope,
      scopeKey,
    );
    const allMembershipRows = await docsTxAll<RawDocsMembership>(
      tx,
      `SELECT auth_scope, scope_key, scope_id, project_id, table_name, entity_key,
              state, access, read_only, updated_at
         FROM docs_scope_membership
        WHERE auth_scope = ?`,
      options.authScope,
    );

    const preservation = new Map<string, DocsPreservationSets>();
    for (const table of tableNames) {
      preservation.set(
        table,
        await loadDocsPreservationSetsTx(tx, table, options.authScope, scopeKey),
      );
    }
    const dirty = await loadDocsDirtyRowsTx(tx);
    const isProtected = (table: string, key: string): boolean => {
      const sets = preservation.get(table);
      if (sets?.outbox.has(key)) return true;
      if (table === "knowledge_nodes" && dirty.nodes.has(key)) return true;
      if (table === "knowledge_supertags" && dirty.supertags.has(key)) return true;
      if (table === "knowledge_node_supertags" && dirty.nodeSupertags.has(key)) return true;
      if (table === "knowledge_field_values" && dirty.fieldValues.has(key)) return true;
      return Boolean(sets?.dirty.has(key));
    };

    const membershipRefs = new Map<string, number>();
    for (const row of allMembershipRows) {
      if (row.state === "deleted") continue;
      const refKey = `${row.table_name}:${row.entity_key}`;
      membershipRefs.set(refKey, (membershipRefs.get(refKey) ?? 0) + 1);
    }
    const scopedMembership = new Map<string, Set<string>>();
    for (const row of membershipRows) {
      if (row.state === "deleted") continue;
      const bucket = scopedMembership.get(row.table_name) ?? new Set<string>();
      bucket.add(row.entity_key);
      scopedMembership.set(row.table_name, bucket);
    }
    const ensureMembership = (table: string, key: string): void => {
      const scopeBucket = scopedMembership.get(table) ?? new Set<string>();
      if (scopeBucket.has(key)) return;
      scopeBucket.add(key);
      scopedMembership.set(table, scopeBucket);
      const refKey = `${table}:${key}`;
      membershipRefs.set(refKey, (membershipRefs.get(refKey) ?? 0) + 1);
    };
    const removeMembership = (table: string, key: string): void => {
      const scopeBucket = scopedMembership.get(table);
      if (!scopeBucket?.has(key)) return;
      scopeBucket.delete(key);
      const refKey = `${table}:${key}`;
      const count = (membershipRefs.get(refKey) ?? 1) - 1;
      if (count > 0) membershipRefs.set(refKey, count);
      else membershipRefs.delete(refKey);
    };
    const authoritativeIds = (table: string): Set<string> | null => {
      const ids = options.authoritative[table]?.ids;
      return ids == null ? null : new Set(ids);
    };
    const workspaceId = options.scopeId
      ?? options.authoritative.knowledge_nodes?.scopeId
      ?? options.authoritative.knowledge_supertags?.scopeId
      ?? options.authoritative.knowledge_fields?.scopeId;

    const staleNodes = authoritativeIds("knowledge_nodes")
      && [...(scopedMembership.get("knowledge_nodes") ?? new Set<string>())]
        .filter((id) => !authoritativeIds("knowledge_nodes")!.has(id));
    const staleSupertags = authoritativeIds("knowledge_supertags")
      && [...(scopedMembership.get("knowledge_supertags") ?? new Set<string>())]
        .filter((id) => !authoritativeIds("knowledge_supertags")!.has(id));
    const staleFields = authoritativeIds("knowledge_fields")
      && [...(scopedMembership.get("knowledge_fields") ?? new Set<string>())]
        .filter((id) => !authoritativeIds("knowledge_fields")!.has(id));

    const upsertMembership = async (
      table: string,
      key: string,
      state = "active",
    ): Promise<void> => {
      await upsertDocsMembershipAsync(
        tx,
        options,
        scopeKey,
        workspaceId,
        table,
        key,
        state,
        now,
      );
    };
    const removeMembershipTx = (table: string, key: string): Promise<void> =>
      removeDocsMembershipAsync(tx, options.authScope, scopeKey, table, key);
    const blockMembershipTx = (table: string, key: string): Promise<void> =>
      blockDocsMembershipAsync(tx, options.authScope, scopeKey, table, key, now);
    const setConflict = (
      table: string,
      key: string,
      payload: unknown,
    ): Promise<void> => setDocsConflictAsync(tx, options, scopeKey, table, key, payload);

    for (const table of tableNames) {
      for (const key of authoritativeIds(table) ?? []) {
        ensureMembership(table, key);
      }
    }

    const stagedTelemetry: DocsSyncPromotionTelemetry = {
      source: "staging",
      rowsRead: 0,
      batches: 0,
      maxBatchSize: 0,
    };

    const applyOne = async (row: DocsSyncStagedRow): Promise<void> => {
      const payload = row.payload ?? {};
      const key = row.entityKey || docsCompositeKey(payload);
      if (!key) return;
      if (isProtected(row.tableName, key)) {
        await upsertMembership(row.tableName, key, "blocked");
        await setConflict(row.tableName, key, payload);
        return;
      }
      if (row.isTombstone || payload.deleted === true) {
        await removeMembershipTx(row.tableName, key);
        removeMembership(row.tableName, key);
        if (membershipRefs.has(`${row.tableName}:${key}`)) return;
        await deleteDocsLiveRowAsync(tx, row.tableName, key);
        return;
      }
      await upsertMembership(row.tableName, key);
      if (row.tableName === "knowledge_nodes") {
        await upsertDocsNodeAsync(tx, payload, now);
      } else if (row.tableName === "knowledge_supertags") {
        await upsertDocsSupertagAsync(tx, payload, now);
      } else if (row.tableName === "knowledge_fields") {
        await upsertDocsFieldAsync(tx, payload, now);
      } else if (row.tableName === "knowledge_supertag_fields") {
        await upsertDocsSupertagFieldAsync(tx, payload);
      } else if (row.tableName === "knowledge_node_supertags") {
        await upsertDocsNodeSupertagAsync(tx, payload);
      } else if (row.tableName === "knowledge_field_values") {
        await upsertDocsFieldValueAsync(tx, payload, now);
      } else if (row.tableName === "knowledge_node_placements") {
        await upsertDocsPlacementAsync(tx, payload);
      } else if (row.tableName === "knowledge_edges") {
        await upsertDocsEdgeAsync(tx, payload);
      }
    };

    const applyStagedRows = async (): Promise<void> => {
      for (const table of tableNames) {
        let lastEntityKey: string | null = null;
        while (true) {
          const rows: RawDocsStaging[] = lastEntityKey == null
            ? await docsTxAll<RawDocsStaging>(
              tx,
              `SELECT table_name, entity_key, payload_json, is_tombstone
                 FROM docs_sync_staging
                WHERE run_id = ? AND auth_scope = ? AND table_name = ?
                ORDER BY entity_key ASC LIMIT ${DOCS_STAGED_PROMOTION_BATCH_SIZE}`,
              options.runId,
              options.authScope,
              table,
            )
            : await docsTxAll<RawDocsStaging>(
              tx,
              `SELECT table_name, entity_key, payload_json, is_tombstone
                 FROM docs_sync_staging
                WHERE run_id = ? AND auth_scope = ? AND table_name = ?
                  AND entity_key > ?
                ORDER BY entity_key ASC LIMIT ${DOCS_STAGED_PROMOTION_BATCH_SIZE}`,
              options.runId,
              options.authScope,
              table,
              lastEntityKey,
            );
          if (!rows.length) break;
          stagedTelemetry.rowsRead += rows.length;
          stagedTelemetry.batches += 1;
          stagedTelemetry.maxBatchSize = Math.max(
            stagedTelemetry.maxBatchSize,
            rows.length,
          );
          for (const row of rows) {
            const parsed = decodeDocsSqlJson<Record<string, unknown> | null>(
              row.payload_json,
              null,
            );
            await applyOne({
              tableName: row.table_name,
              entityKey: row.entity_key,
              payload: parsed,
              isTombstone: sqliteBoolean(row.is_tombstone),
            });
          }
          const nextKey: string | undefined = rows[rows.length - 1]?.entity_key;
          if (typeof nextKey !== "string" || !nextKey.length || nextKey === lastEntityKey) break;
          lastEntityKey = nextKey;
          if (rows.length < DOCS_STAGED_PROMOTION_BATCH_SIZE) break;
        }
      }
    };

    // The terminal page may carry an authoritative id without a changed row;
    // persist that membership before reconciliation just like the legacy
    // promotion path did.
    for (const table of tableNames) {
      for (const key of authoritativeIds(table) ?? []) {
        await upsertMembership(table, key);
      }
    }
    await applyStagedRows();

    for (const id of staleNodes || []) {
      if (isProtected("knowledge_nodes", id)) {
        await blockMembershipTx("knowledge_nodes", id);
        await setConflict("knowledge_nodes", id, {
          id,
          deleted: true,
          authoritative_scope_id: workspaceId,
        });
        await tx.runAsync(
          "UPDATE knowledge_nodes SET access = 'read', read_only = 1 WHERE id = ?",
          id,
        );
        continue;
      }
      await removeMembershipTx("knowledge_nodes", id);
      removeMembership("knowledge_nodes", id);
      if (membershipRefs.has(`knowledge_nodes:${id}`)) continue;
      await deleteDocsLiveRowAsync(tx, "knowledge_nodes", id);
    }
    for (const id of staleSupertags || []) {
      if (isProtected("knowledge_supertags", id)) {
        await blockMembershipTx("knowledge_supertags", id);
        await setConflict("knowledge_supertags", id, {
          id,
          deleted: true,
          authoritative_scope_id: workspaceId,
        });
        continue;
      }
      await removeMembershipTx("knowledge_supertags", id);
      removeMembership("knowledge_supertags", id);
      if (membershipRefs.has(`knowledge_supertags:${id}`)) continue;
      await deleteDocsLiveRowAsync(tx, "knowledge_supertags", id);
    }
    for (const id of staleFields || []) {
      if (isProtected("knowledge_fields", id)) {
        await blockMembershipTx("knowledge_fields", id);
        await setConflict("knowledge_fields", id, {
          id,
          deleted: true,
          authoritative_scope_id: workspaceId,
        });
        continue;
      }
      await removeMembershipTx("knowledge_fields", id);
      removeMembership("knowledge_fields", id);
      if (membershipRefs.has(`knowledge_fields:${id}`)) continue;
      await deleteDocsLiveRowAsync(tx, "knowledge_fields", id);
    }

    const nodeScope = scopedMembership.get("knowledge_nodes") ?? new Set<string>();
    const supertagScope = scopedMembership.get("knowledge_supertags") ?? new Set<string>();
    const fieldScope = scopedMembership.get("knowledge_fields") ?? new Set<string>();
    const reconcileRelation = async (
      table: string,
      key: string,
      inScope: boolean,
      authoritative: Set<string> | null,
      remove: () => Promise<void>,
    ): Promise<void> => {
      if (!inScope || !authoritative || authoritative.has(key)) return;
      if (isProtected(table, key)) {
        await blockMembershipTx(table, key);
        const [first, second] = key.split(":", 2);
        await setConflict(
          table,
          key,
          table === "knowledge_node_supertags"
            ? { node_id: first, supertag_id: second, deleted: true }
            : table === "knowledge_field_values"
              ? { node_id: first, field_id: second, deleted: true }
              : { supertag_id: first, field_id: second, deleted: true },
        );
        return;
      }
      await removeMembershipTx(table, key);
      removeMembership(table, key);
      if (!membershipRefs.has(`${table}:${key}`)) await remove();
    };
    const nodeSupertagAuth = authoritativeIds("knowledge_node_supertags");
    for (const key of [...(scopedMembership.get("knowledge_node_supertags") ?? new Set<string>())]) {
      const [nodeId, supertagId] = key.split(":", 2);
      await reconcileRelation(
        "knowledge_node_supertags",
        key,
        nodeScope.has(nodeId) || supertagScope.has(supertagId),
        nodeSupertagAuth,
        () => deleteDocsLiveRowAsync(tx, "knowledge_node_supertags", key),
      );
    }
    const fieldValueAuth = authoritativeIds("knowledge_field_values");
    for (const key of [...(scopedMembership.get("knowledge_field_values") ?? new Set<string>())]) {
      const [nodeId, fieldId] = key.split(":", 2);
      await reconcileRelation(
        "knowledge_field_values",
        key,
        nodeScope.has(nodeId) || fieldScope.has(fieldId),
        fieldValueAuth,
        () => deleteDocsLiveRowAsync(tx, "knowledge_field_values", key),
      );
    }
    const supertagFieldAuth = authoritativeIds("knowledge_supertag_fields");
    for (const key of [...(scopedMembership.get("knowledge_supertag_fields") ?? new Set<string>())]) {
      const [supertagId, fieldId] = key.split(":", 2);
      await reconcileRelation(
        "knowledge_supertag_fields",
        key,
        supertagScope.has(supertagId) || fieldScope.has(fieldId),
        supertagFieldAuth,
        () => deleteDocsLiveRowAsync(tx, "knowledge_supertag_fields", key),
      );
    }
    const placementAuth = authoritativeIds("knowledge_node_placements");
    for (const key of [...(scopedMembership.get("knowledge_node_placements") ?? new Set<string>())]) {
      if (!placementAuth || placementAuth.has(key)) continue;
      if (isProtected("knowledge_node_placements", key)) {
        await blockMembershipTx("knowledge_node_placements", key);
        await setConflict("knowledge_node_placements", key, { id: key, deleted: true });
        continue;
      }
      await removeMembershipTx("knowledge_node_placements", key);
      removeMembership("knowledge_node_placements", key);
      if (!membershipRefs.has(`knowledge_node_placements:${key}`)) {
        await deleteDocsLiveRowAsync(tx, "knowledge_node_placements", key);
      }
    }
    const edgeAuth = authoritativeIds("knowledge_edges");
    for (const key of [...(scopedMembership.get("knowledge_edges") ?? new Set<string>())]) {
      if (!edgeAuth || edgeAuth.has(key)) continue;
      if (isProtected("knowledge_edges", key)) {
        await blockMembershipTx("knowledge_edges", key);
        await setConflict("knowledge_edges", key, { id: key, deleted: true });
        continue;
      }
      await removeMembershipTx("knowledge_edges", key);
      removeMembership("knowledge_edges", key);
      if (!membershipRefs.has(`knowledge_edges:${key}`)) {
        await deleteDocsLiveRowAsync(tx, "knowledge_edges", key);
      }
    }

    if (options.scopeSet) {
      const nextKeys = new Set(options.scopeSet.newScopes.map(docsScopeKeyFromEntry));
      for (const previous of options.scopeSet.previousScopes) {
        if (!nextKeys.has(docsScopeKeyFromEntry(previous))) {
          await quarantineDocsScopeAsync(
            tx,
            options.authScope,
            previous,
            "revoke",
            options.finalize?.serverTime ?? now,
          );
        }
      }
      const previousByKey = new Map(
        options.scopeSet.previousScopes.map((scope) => [docsScopeKeyFromEntry(scope), scope]),
      );
      for (const next of options.scopeSet.newScopes) {
        const previous = previousByKey.get(docsScopeKeyFromEntry(next));
        if (previous && !previous.read_only && next.read_only) {
          await quarantineDocsScopeAsync(
            tx,
            options.authScope,
            next,
            "downgrade",
            options.finalize?.serverTime ?? now,
          );
        }
      }
    }

    if (options.finalize) {
      const finalize = options.finalize;
      const saveSyncState = async (
        tableName: string,
        values: { lastPulledAt?: string | null; cursor?: string | null },
      ): Promise<void> => {
        const lastPulledAt = values.lastPulledAt ?? null;
        const cursor = values.cursor ?? null;
        const updates: string[] = [];
        const params: unknown[] = [tableName, lastPulledAt, null, cursor];
        if (values.lastPulledAt !== undefined) {
          updates.push("last_pulled_at = excluded.last_pulled_at");
        }
        if (values.cursor !== undefined) updates.push("cursor = excluded.cursor");
        await tx.runAsync(
          `INSERT INTO sync_state(table_name, last_pulled_at, last_pushed_at, cursor)
             VALUES (?, ?, ?, ?)
           ON CONFLICT(table_name) DO UPDATE SET ${updates.join(", ")}`,
          ...params,
        );
      };
      for (const [table, digest] of Object.entries(finalize.digestByTable)) {
        const key = finalize.digestKeys[table];
        if (key && digest) await saveSyncState(key, { cursor: digest });
      }
      await saveSyncState(finalize.scopeDigestKey, { cursor: finalize.scopeDigest });
      if (finalize.scopeRevisionKey && finalize.scopeRevision) {
        await saveSyncState(finalize.scopeRevisionKey, { cursor: finalize.scopeRevision });
      }
      await saveSyncState(finalize.lastPulledKey, { lastPulledAt: finalize.serverTime });
      if (finalize.scopesKey && options.scopes) {
        await saveSyncState(finalize.scopesKey, { cursor: JSON.stringify(options.scopes) });
      }
      if (finalize.workspaceKey && finalize.workspaceId) {
        await saveSyncState(finalize.workspaceKey, { cursor: finalize.workspaceId });
      }
      await tx.runAsync(
        `UPDATE docs_sync_runs SET state = 'completed', updated_at = ?
           WHERE run_id = ? AND auth_scope = ?`,
        finalize.serverTime,
        options.runId,
        options.authScope,
      );
      await tx.runAsync(
        "DELETE FROM docs_sync_staging WHERE run_id = ? AND auth_scope = ?",
        options.runId,
        options.authScope,
      );
    }
    return stagedTelemetry;
  });
}

/** Promote a validated Docs run using native async SQLite in production. */
export async function promoteDocsSyncRun(
  options: DocsSyncPromotionOptions,
): Promise<DocsSyncPromotionTelemetry> {
  if (options.staged) {
    return promoteDocsSyncRunCompatibility(options);
  }
  return promoteDocsSyncRunAsync(options);
}

/** Mark a revoked scope's pending Docs operations terminal without deleting
 * the local edit or its conflict payload.  Subsequent syncs must not replay
 * mutations after ACL revocation. */
async function quarantineRevokedDocsScopeCompatibility(
  authScope: string,
  workspaceId: string,
  projectId?: string | null,
  requestedScopeKey?: string,
  mode: "revoke" | "downgrade" = "revoke",
): Promise<void> {
  const db = getDb();
  const scopeKey = requestedScopeKey
    ?? `${workspaceId}|project:${projectId ?? ""}`;
  const tableNames = [
    "knowledge_nodes",
    "knowledge_supertags",
    "knowledge_node_supertags",
    "knowledge_supertag_fields",
    "knowledge_fields",
    "knowledge_field_values",
    "knowledge_node_placements",
    "knowledge_edges",
  ];
  // Membership rows are the only authoritative scope selector.  For a
  // pre-membership database keep the old workspace fallback, but never use it
  // when the new table exists (that would merge two project scopes).
  let membershipRows = schema.docsScopeMembership
    ? await db
        .select()
        .from(schema.docsScopeMembership)
        .where(
          and(
            eq(schema.docsScopeMembership.authScope, authScope),
            eq(schema.docsScopeMembership.scopeKey, scopeKey),
          ),
        )
    : [];
  let allMembershipRows = schema.docsScopeMembership
    ? await db
        .select()
        .from(schema.docsScopeMembership)
        .where(eq(schema.docsScopeMembership.authScope, authScope))
    : [];
  let refs = new Map<string, number>();
  for (const row of allMembershipRows) {
    if (row.state === "deleted") continue;
    const key = `${row.tableName}:${row.entityKey}`;
    refs.set(key, (refs.get(key) ?? 0) + 1);
  }
  let outboxRows = await db
    .select({
      opId: schema.outbox.opId,
      tableName: schema.outbox.tableName,
      entityId: schema.outbox.entityId,
      docsScopeKey: schema.outbox.docsScopeKey,
    })
    .from(schema.outbox)
    .where(eq(schema.outbox.authScope, authScope));
  // Keep the operation identity, not only a table/entity compound key.  Two
  // project scopes may legitimately share the same UUID; a compound-key set
  // would quarantine the sibling operation when the target scope is revoked.
  let scopedOutboxRows = outboxRows.filter((row) =>
    tableNames.includes(row.tableName)
    // A NULL legacy key is ambiguous when two project scopes share a
    // workspace/entity UUID.  Migration either recovers it from payload or
    // unique membership, or marks it docs_scope_ambiguous; never quarantine
    // such a row on behalf of one scope.
    && row.docsScopeKey === scopeKey,
  );
  let scopedKeys = membershipRows.length
    ? new Set(
        membershipRows
          .filter((row) => mode === "revoke" ? row.state !== "deleted" : row.state === "active")
          .map((row) => `${row.tableName}:${row.entityKey}`),
      )
    : new Set<string>();
  const dirtyKeys = new Set<string>();
  if (!schema.docsScopeMembership) {
    const [nodes, supertags, fields] = await Promise.all([
      db.select({ id: schema.knowledgeNodes.id }).from(schema.knowledgeNodes)
        .where(eq(schema.knowledgeNodes.workspaceId, workspaceId)),
      db.select({ id: schema.knowledgeSupertags.id }).from(schema.knowledgeSupertags)
        .where(eq(schema.knowledgeSupertags.workspaceId, workspaceId)),
      db.select({ id: schema.knowledgeFields.id }).from(schema.knowledgeFields)
        .where(eq(schema.knowledgeFields.workspaceId, workspaceId)),
    ]);
    for (const row of nodes) scopedKeys.add(`knowledge_nodes:${row.id}`);
    for (const row of supertags) scopedKeys.add(`knowledge_supertags:${row.id}`);
    for (const row of fields) scopedKeys.add(`knowledge_fields:${row.id}`);
  }
  const [dirtyNodes, dirtySupertags, dirtyNodeSupertags, dirtyFieldValues] = await Promise.all([
    db.select({ id: schema.knowledgeNodes.id }).from(schema.knowledgeNodes)
      .where(eq(schema.knowledgeNodes.dirty, true)),
    db.select({ id: schema.knowledgeSupertags.id }).from(schema.knowledgeSupertags)
      .where(eq(schema.knowledgeSupertags.dirty, true)),
    db.select({ nodeId: schema.knowledgeNodeSupertags.nodeId, supertagId: schema.knowledgeNodeSupertags.supertagId })
      .from(schema.knowledgeNodeSupertags).where(eq(schema.knowledgeNodeSupertags.dirty, true)),
    db.select({ nodeId: schema.knowledgeFieldValues.nodeId, fieldId: schema.knowledgeFieldValues.fieldId })
      .from(schema.knowledgeFieldValues).where(eq(schema.knowledgeFieldValues.dirty, true)),
  ]);
  for (const row of dirtyNodes) dirtyKeys.add(`knowledge_nodes:${row.id}`);
  for (const row of dirtySupertags) dirtyKeys.add(`knowledge_supertags:${row.id}`);
  for (const row of dirtyNodeSupertags) dirtyKeys.add(`knowledge_node_supertags:${row.nodeId}:${row.supertagId}`);
  for (const row of dirtyFieldValues) dirtyKeys.add(`knowledge_field_values:${row.nodeId}:${row.fieldId}`);
  db.transaction((tx) => {
    // Re-read the revoke protection set at the transaction boundary.  A
    // sibling membership or local outbox edit may have been created while the
    // preflight SELECTs above were awaiting; using those stale snapshots could
    // otherwise delete the shared live row or lose the newly queued edit.
    if (schema.docsScopeMembership && typeof tx.select === "function") {
      const txSelectAll = (query: any): any[] | null => {
        if (typeof query?.all !== "function") return null;
        try {
          return query.all() as any[];
        } catch {
          return null;
        }
      };
      const currentRows = txSelectAll(
        tx.select()
          .from(schema.docsScopeMembership)
          .where(
            and(
              eq(schema.docsScopeMembership.authScope, authScope),
              eq(schema.docsScopeMembership.scopeKey, scopeKey),
            ),
          ),
      );
      const currentAllRows = txSelectAll(
        tx.select()
          .from(schema.docsScopeMembership)
          .where(eq(schema.docsScopeMembership.authScope, authScope)),
      );
      if (currentRows && currentAllRows) {
        membershipRows = currentRows;
        allMembershipRows = currentAllRows;
        refs = new Map<string, number>();
        for (const row of allMembershipRows) {
          if (row.state === "deleted") continue;
          const key = `${row.tableName}:${row.entityKey}`;
          refs.set(key, (refs.get(key) ?? 0) + 1);
        }
        scopedKeys = new Set(
          membershipRows
            .filter((row) => mode === "revoke" ? row.state !== "deleted" : row.state === "active")
            .map((row) => `${row.tableName}:${row.entityKey}`),
        );
      }
      const currentOutboxRows = txSelectAll(
        tx.select({
          opId: schema.outbox.opId,
          tableName: schema.outbox.tableName,
          entityId: schema.outbox.entityId,
          docsScopeKey: schema.outbox.docsScopeKey,
        })
          .from(schema.outbox)
          .where(eq(schema.outbox.authScope, authScope)),
      );
      if (currentOutboxRows) {
        outboxRows = currentOutboxRows;
        scopedOutboxRows = outboxRows.filter((row) =>
          tableNames.includes(row.tableName)
          && row.docsScopeKey === scopeKey,
        );
      }
      const currentDirtyNodes = txSelectAll(
        tx.select({ id: schema.knowledgeNodes.id })
          .from(schema.knowledgeNodes)
          .where(eq(schema.knowledgeNodes.dirty, true)),
      );
      const currentDirtySupertags = txSelectAll(
        tx.select({ id: schema.knowledgeSupertags.id })
          .from(schema.knowledgeSupertags)
          .where(eq(schema.knowledgeSupertags.dirty, true)),
      );
      const currentDirtyNodeSupertags = txSelectAll(
        tx.select({
          nodeId: schema.knowledgeNodeSupertags.nodeId,
          supertagId: schema.knowledgeNodeSupertags.supertagId,
        })
          .from(schema.knowledgeNodeSupertags)
          .where(eq(schema.knowledgeNodeSupertags.dirty, true)),
      );
      const currentDirtyFieldValues = txSelectAll(
        tx.select({
          nodeId: schema.knowledgeFieldValues.nodeId,
          fieldId: schema.knowledgeFieldValues.fieldId,
        })
          .from(schema.knowledgeFieldValues)
          .where(eq(schema.knowledgeFieldValues.dirty, true)),
      );
      if (
        currentDirtyNodes
        && currentDirtySupertags
        && currentDirtyNodeSupertags
        && currentDirtyFieldValues
      ) {
        dirtyKeys.clear();
        for (const row of currentDirtyNodes) dirtyKeys.add(`knowledge_nodes:${row.id}`);
        for (const row of currentDirtySupertags) dirtyKeys.add(`knowledge_supertags:${row.id}`);
        for (const row of currentDirtyNodeSupertags) {
          dirtyKeys.add(`knowledge_node_supertags:${row.nodeId}:${row.supertagId}`);
        }
        for (const row of currentDirtyFieldValues) {
          dirtyKeys.add(`knowledge_field_values:${row.nodeId}:${row.fieldId}`);
        }
      }
    }
    for (const compound of scopedKeys) {
      const separator = compound.indexOf(":");
      const table = compound.slice(0, separator);
      const key = compound.slice(separator + 1);
      const protectedRow = dirtyKeys.has(compound)
        || scopedOutboxRows.some(
          (row) => `${row.tableName}:${row.entityId}` === compound,
        );
      const hasOtherWritableMembership = allMembershipRows.some(
        (row) => row.tableName === table
          && row.entityKey === key
          && row.scopeKey !== scopeKey
          && row.state === "active"
          && row.readOnly !== true
          && row.access !== "read",
      );
      if (protectedRow) {
        if (mode === "downgrade" && table === "knowledge_nodes" && !hasOtherWritableMembership) {
          tx.update(schema.knowledgeNodes)
            .set({ access: "read", readOnly: true })
            .where(eq(schema.knowledgeNodes.id, key))
            .run();
        }
        if (schema.docsScopeMembership) {
          tx.update(schema.docsScopeMembership)
            .set({ state: "blocked", updatedAt: new Date().toISOString() })
            .where(and(
              eq(schema.docsScopeMembership.authScope, authScope),
              eq(schema.docsScopeMembership.scopeKey, scopeKey),
              eq(schema.docsScopeMembership.tableName, table),
              eq(schema.docsScopeMembership.entityKey, key),
            )).run();
        }
        continue;
      }
      if (mode === "downgrade") {
        if (table === "knowledge_nodes" && !hasOtherWritableMembership) {
          tx.update(schema.knowledgeNodes)
            .set({ access: "read", readOnly: true })
            .where(eq(schema.knowledgeNodes.id, key))
            .run();
        }
        if (schema.docsScopeMembership) {
          tx.update(schema.docsScopeMembership)
            .set({ state: "readonly", access: "read", readOnly: true, updatedAt: new Date().toISOString() })
            .where(and(
              eq(schema.docsScopeMembership.authScope, authScope),
              eq(schema.docsScopeMembership.scopeKey, scopeKey),
              eq(schema.docsScopeMembership.tableName, table),
              eq(schema.docsScopeMembership.entityKey, key),
            )).run();
        }
        continue;
      }
      if (schema.docsScopeMembership) {
        tx.delete(schema.docsScopeMembership).where(and(
          eq(schema.docsScopeMembership.authScope, authScope),
          eq(schema.docsScopeMembership.scopeKey, scopeKey),
          eq(schema.docsScopeMembership.tableName, table),
          eq(schema.docsScopeMembership.entityKey, key),
        )).run();
      }
      const hasOtherMembership = (refs.get(compound) ?? 0) > 1;
      if (hasOtherMembership) continue;
      if (table === "knowledge_nodes") {
        tx.delete(schema.knowledgeNodes).where(eq(schema.knowledgeNodes.id, key)).run();
      } else if (table === "knowledge_supertags") {
        tx.delete(schema.knowledgeSupertags).where(eq(schema.knowledgeSupertags.id, key)).run();
      } else if (table === "knowledge_fields") {
        tx.delete(schema.knowledgeFields).where(eq(schema.knowledgeFields.id, key)).run();
      } else if (table === "knowledge_node_supertags") {
        const [nodeId, supertagId] = key.split(":", 2);
        tx.delete(schema.knowledgeNodeSupertags).where(and(eq(schema.knowledgeNodeSupertags.nodeId, nodeId), eq(schema.knowledgeNodeSupertags.supertagId, supertagId))).run();
      } else if (table === "knowledge_supertag_fields") {
        const [supertagId, fieldId] = key.split(":", 2);
        tx.delete(schema.knowledgeSupertagFields).where(and(eq(schema.knowledgeSupertagFields.supertagId, supertagId), eq(schema.knowledgeSupertagFields.fieldId, fieldId))).run();
      } else if (table === "knowledge_field_values") {
        const [nodeId, fieldId] = key.split(":", 2);
        tx.delete(schema.knowledgeFieldValues).where(and(eq(schema.knowledgeFieldValues.nodeId, nodeId), eq(schema.knowledgeFieldValues.fieldId, fieldId))).run();
      } else if (table === "knowledge_node_placements") {
        tx.delete(schema.knowledgeNodePlacements).where(eq(schema.knowledgeNodePlacements.id, key)).run();
      } else if (table === "knowledge_edges") {
        tx.delete(schema.knowledgeEdges).where(eq(schema.knowledgeEdges.id, key)).run();
      }
    }
    for (const row of scopedOutboxRows) {
      tx.update(schema.outbox)
        .set({
          retryCount: 5,
          lastError: "quarantine:scope_revoked",
          blockedReason: "docs_scope_revoked",
          docsScopeKey: scopeKey,
        })
        .where(eq(schema.outbox.opId, row.opId))
        .run();
    }
  });
}

/**
 * Quarantine a revoked/downgraded scope on the same native async transaction
 * used by production promotion.  The compatibility implementation remains
 * available for small legacy test doubles that do not expose getSqlite().
 */
async function quarantineRevokedDocsScopeAsync(
  authScope: string,
  workspaceId: string,
  projectId: string | null | undefined,
  requestedScopeKey: string | undefined,
  mode: "revoke" | "downgrade",
): Promise<void> {
  await withDocsExclusiveTransaction(async (tx) => {
    await quarantineDocsScopeAsync(
      tx,
      authScope,
      { workspace_id: workspaceId, project_id: projectId ?? null },
      mode,
      new Date().toISOString(),
      requestedScopeKey,
    );
  });
}

export async function quarantineRevokedDocsScope(
  authScope: string,
  workspaceId: string,
  projectId?: string | null,
  requestedScopeKey?: string,
  mode: "revoke" | "downgrade" = "revoke",
): Promise<void> {
  if (docsSqliteAsyncAvailable()) {
    return quarantineRevokedDocsScopeAsync(
      authScope,
      workspaceId,
      projectId,
      requestedScopeKey,
      mode,
    );
  }
  return quarantineRevokedDocsScopeCompatibility(
    authScope,
    workspaceId,
    projectId,
    requestedScopeKey,
    mode,
  );
}

// ---------- docsRepo ----------

export const docsRepo = {
  // ===== 読み取り（ローカルファースト） =====

  /**
   * ローカルキャッシュから指定日の Day ノードを探す（オフライン時の
   * デイリーページ表示フォールバック用。作成はサーバ ensure に委ねる）。
   */
  async findDayNode(date: string): Promise<DocsNode | null> {
    const db = getDb();
    const visibility = await getDocsNodeVisibilityCondition();
    const rowVisibility = await getDocsVisibility("knowledge_nodes");
    const rows = await db
      .select()
      .from(schema.knowledgeNodes)
      .where(
        and(
          eq(schema.knowledgeNodes.nodeType, "day"),
          eq(schema.knowledgeNodes.dayDate, date),
          isNull(schema.knowledgeNodes.archivedAt),
          visibility as never,
        ),
      );
    const row = rows.find((candidate) => docsRowVisible(candidate, rowVisibility));
    return row ? toNode(row) : null;
  },

  /** トップレベルページ一覧（parent_id null / archived_at null / 通常ノードのみ）。 */
  async listPages(): Promise<DocsNode[]> {
    const db = getDb();
    const rowVisibility = await getDocsVisibility("knowledge_nodes");
    const workspaceIds = rowVisibility.workspaceIds;
    const conditions = [
      isNull(schema.knowledgeNodes.parentId),
      isNull(schema.knowledgeNodes.archivedAt),
      eq(schema.knowledgeNodes.nodeType, "node"),
    ];
    if (workspaceIds.length) {
      // dirty=true の workspace_id null 行は、まだサーバー採番前のローカル作成を
      // 先読み表示するため残す。cleanな旧workspace行は現在のscopeから除外する。
      const workspaceCondition =
        workspaceIds.length === 1
          ? eq(schema.knowledgeNodes.workspaceId, workspaceIds[0])
          : inArray(schema.knowledgeNodes.workspaceId, workspaceIds);
      conditions.push(
        or(workspaceCondition, eq(schema.knowledgeNodes.dirty, true)) as never,
      );
    } else {
      // A fresh/auth-switched account has no trusted workspace projection yet.
      // Never fall back to an unscoped SELECT that would expose another
      // account's clean cache; only unsynced local edits remain visible until
      // the first validated Docs promotion records the current workspace.
      conditions.push(eq(schema.knowledgeNodes.dirty, true) as never);
    }
    const rows = await db
      .select()
      .from(schema.knowledgeNodes)
      .where(and(...conditions));
    return rows
      .filter((row) => docsRowVisible(row, rowVisibility))
      .map(toNode)
      .sort(sortBySortThenTitle);
  },

  /** 子ノード一覧（sort_order 昇順、archived を除外）。 */
  async listChildren(parentId: string): Promise<DocsNode[]> {
    const db = getDb();
    const visibility = await getDocsNodeVisibilityCondition();
    const rowVisibility = await getDocsVisibility(
      "knowledge_nodes",
      await getDocsNodeScopeKeys(parentId),
    );
    const rows = await db
      .select()
      .from(schema.knowledgeNodes)
      .where(
        and(
          eq(schema.knowledgeNodes.parentId, parentId),
          isNull(schema.knowledgeNodes.archivedAt),
          visibility as never,
        ),
      )
      .orderBy(asc(schema.knowledgeNodes.sortOrder));
    return rows
      .filter((row) => docsRowVisible(row, rowVisibility))
      .map(toNode)
      .sort(sortBySortThenTitle);
  },

  /** ルート配下のアウトラインを1クエリで取得する。 */
  async listOutline(rootNodeId: string, includeArchived = false): Promise<DocsNode[]> {
    const db = getDb();
    const root = await getNodeRow(rootNodeId);
    const pageRootId = root?.rootPageId ?? root?.id ?? rootNodeId;
    const visibility = await getDocsNodeVisibilityCondition();
    const rowVisibility = await getDocsVisibility(
      "knowledge_nodes",
      await getDocsNodeScopeKeys(rootNodeId, root),
    );
    const rows = await db
      .select()
      .from(schema.knowledgeNodes)
      .where(
        and(
          or(
            eq(schema.knowledgeNodes.rootPageId, pageRootId),
            eq(schema.knowledgeNodes.parentId, rootNodeId),
          ),
          visibility as never,
        ),
      );
    return rows
      .filter((row) => docsRowVisible(row, rowVisibility))
      .map(toNode)
      .filter((node) => node.id !== rootNodeId)
      .filter((node) => includeArchived || !node.archived_at);
  },

  async getNode(id: string): Promise<DocsNode | null> {
    const row = await getNodeRow(id);
    if (!row) return null;
    // getNodeRow is also used by local mutation guards and intentionally
    // remains a raw lookup.  Public reads must apply the current account
    // projection before exposing a cached clean row.
    const rowVisibility = await getDocsVisibility(
      "knowledge_nodes",
      await getDocsNodeScopeKeys(id, row),
    );
    if (!docsRowVisible(row, rowVisibility)) return null;
    return toNode(row);
  },

  /** system_key からノードを引く（クリップ取り込み先のローカル解決に使う）。 */
  async getNodeBySystemKey(systemKey: string): Promise<DocsNode | null> {
    const key = systemKey.trim();
    if (!key) return null;
    const db = getDb();
    const visibility = await getDocsNodeVisibilityCondition();
    const rowVisibility = await getDocsVisibility("knowledge_nodes");
    const rows = await db
      .select()
      .from(schema.knowledgeNodes)
      .where(
        and(
          eq(schema.knowledgeNodes.systemKey, key),
          isNull(schema.knowledgeNodes.archivedAt),
          visibility as never,
        ),
      );
    const row = rows.find((candidate) => docsRowVisible(candidate, rowVisibility));
    return row ? toNode(row) : null;
  },

  /** ノードに付与されたスーパータグ一覧。 */
  async getNodeTags(nodeId: string): Promise<DocsSupertag[]> {
    const nodeRow = await getNodeRow(nodeId);
    if (!nodeRow) return [];
    const nodeScopeKeys = await getDocsNodeScopeKeys(nodeId, nodeRow);
    const nodeVisibility = await getDocsVisibility("knowledge_nodes", nodeScopeKeys);
    if (!docsRowVisible(nodeRow, nodeVisibility)) return [];
    const db = getDb();
    const visibility = await getDocsVisibility("knowledge_supertags", nodeScopeKeys);
    const relationVisibility = await getDocsVisibility("knowledge_node_supertags", nodeScopeKeys);
    const rows = await db
      .select({
        supertag: schema.knowledgeSupertags,
        nodeId: schema.knowledgeNodeSupertags.nodeId,
        supertagId: schema.knowledgeNodeSupertags.supertagId,
      })
      .from(schema.knowledgeNodeSupertags)
      .innerJoin(
        schema.knowledgeSupertags,
        eq(
          schema.knowledgeNodeSupertags.supertagId,
          schema.knowledgeSupertags.id,
        ),
      )
      .where(eq(schema.knowledgeNodeSupertags.nodeId, nodeId));
    return rows
      .filter((row) =>
        docsRelationVisible(`${row.nodeId}:${row.supertagId}`, relationVisibility),
      )
      .filter((row) => docsRowVisible(row.supertag, visibility))
      .map((r) => toSupertag(r.supertag));
  },

  /** ノードのタグに紐づくフィールド定義と現在値のペア一覧。 */
  async getNodeFieldValues(
    nodeId: string,
  ): Promise<Array<{ field: DocsField; value: DocsFieldValue | null }>> {
    const nodeRow = await getNodeRow(nodeId);
    if (!nodeRow) return [];
    const nodeScopeKeys = await getDocsNodeScopeKeys(nodeId, nodeRow);
    const nodeVisibility = await getDocsVisibility("knowledge_nodes", nodeScopeKeys);
    if (!docsRowVisible(nodeRow, nodeVisibility)) return [];
    const db = getDb();
    const visibility = await getDocsVisibility("knowledge_fields", nodeScopeKeys);
    const nodeSupertagVisibility = await getDocsVisibility("knowledge_node_supertags", nodeScopeKeys);
    const supertagFieldVisibility = await getDocsVisibility("knowledge_supertag_fields", nodeScopeKeys);
    const fieldValueVisibility = await getDocsVisibility("knowledge_field_values", nodeScopeKeys);
    const tagRows = await db
      .select({ supertagId: schema.knowledgeNodeSupertags.supertagId })
      .from(schema.knowledgeNodeSupertags)
      .where(eq(schema.knowledgeNodeSupertags.nodeId, nodeId));
    const supertagIds = tagRows
      .filter((row) =>
        docsRelationVisible(`${nodeId}:${row.supertagId}`, nodeSupertagVisibility),
      )
      .map((r) => r.supertagId);
    if (!supertagIds.length) return [];

    const stFieldRows = await db
      .select()
      .from(schema.knowledgeSupertagFields)
      .where(inArray(schema.knowledgeSupertagFields.supertagId, supertagIds));
    const visibleStFieldRows = stFieldRows.filter((row) =>
      docsRelationVisible(`${row.supertagId}:${row.fieldId}`, supertagFieldVisibility),
    );
    const fieldIds = Array.from(
      new Set(visibleStFieldRows.map((r) => r.fieldId)),
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
    for (const v of valueRows) {
      if (
        docsRelationVisible(`${nodeId}:${v.fieldId}`, fieldValueVisibility)
      ) valueByField.set(v.fieldId, v);
    }
    const sortByField = new Map<string, number>();
    for (const sf of visibleStFieldRows) {
      if (typeof sf.sortOrder === "number") {
        sortByField.set(sf.fieldId, sf.sortOrder);
      }
    }

    return fieldRows
      .filter((row) => docsRowVisible(row, visibility))
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
    const visibility = await getDocsVisibility("knowledge_supertags");
    const rows = await db.select().from(schema.knowledgeSupertags);
    return rows
      .filter((row) => docsRowVisible(row, visibility))
      .map(toSupertag)
      .sort((a, b) =>
        String(a.name ?? "").localeCompare(String(b.name ?? "")),
      );
  },

  /** バックリンク（target=nodeId のエッジ元ノード）。 */
  async getBacklinks(nodeId: string): Promise<DocsNode[]> {
    const nodeRow = await getNodeRow(nodeId);
    if (!nodeRow) return [];
    const nodeScopeKeys = await getDocsNodeScopeKeys(nodeId, nodeRow);
    const nodeVisibility = await getDocsVisibility("knowledge_nodes", nodeScopeKeys);
    if (!docsRowVisible(nodeRow, nodeVisibility)) return [];
    const db = getDb();
    const visibility = await getDocsNodeVisibilityCondition();
    const rowVisibility = await getDocsVisibility("knowledge_nodes", nodeScopeKeys);
    const edgeVisibility = await getDocsVisibility("knowledge_edges", nodeScopeKeys);
    const rows = await db
      .select({
        node: schema.knowledgeNodes,
        edgeId: schema.knowledgeEdges.id,
      })
      .from(schema.knowledgeEdges)
      .innerJoin(
        schema.knowledgeNodes,
        eq(schema.knowledgeEdges.sourceNodeId, schema.knowledgeNodes.id),
      )
      .where(and(eq(schema.knowledgeEdges.targetNodeId, nodeId), visibility as never));
    return rows
      .filter((row) =>
        docsRelationVisible(row.edgeId, edgeVisibility),
      )
      .filter((row) => docsRowVisible(row.node, rowVisibility))
      .map((r) => toNode(r.node));
  },

  /** 参照先（source=nodeId のエッジ target ノード）。 */
  async getOutgoingReferences(nodeId: string): Promise<DocsNode[]> {
    const nodeRow = await getNodeRow(nodeId);
    if (!nodeRow) return [];
    const nodeScopeKeys = await getDocsNodeScopeKeys(nodeId, nodeRow);
    const nodeVisibility = await getDocsVisibility("knowledge_nodes", nodeScopeKeys);
    if (!docsRowVisible(nodeRow, nodeVisibility)) return [];
    const db = getDb();
    const visibility = await getDocsNodeVisibilityCondition();
    const rowVisibility = await getDocsVisibility("knowledge_nodes", nodeScopeKeys);
    const edgeVisibility = await getDocsVisibility("knowledge_edges", nodeScopeKeys);
    const rows = await db
      .select({
        node: schema.knowledgeNodes,
        edgeId: schema.knowledgeEdges.id,
      })
      .from(schema.knowledgeEdges)
      .innerJoin(
        schema.knowledgeNodes,
        eq(schema.knowledgeEdges.targetNodeId, schema.knowledgeNodes.id),
      )
      .where(and(eq(schema.knowledgeEdges.sourceNodeId, nodeId), visibility as never));
    return rows
      .filter((row) => docsRelationVisible(row.edgeId, edgeVisibility))
      .filter((row) => docsRowVisible(row.node, rowVisibility))
      .map((row) => toNode(row.node));
  },

  /** オフライン検索（title / description の部分一致）。 */
  async searchLocal(q: string): Promise<DocsNode[]> {
    const term = q.trim();
    if (!term) return [];
    const db = getDb();
    const pattern = `%${term}%`;
    const visibility = await getDocsNodeVisibilityCondition();
    const rowVisibility = await getDocsVisibility("knowledge_nodes");
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
          visibility as never,
        ),
      );
    return rows
      .filter((row) => docsRowVisible(row, rowVisibility))
      .map(toNode)
      .sort(sortBySortThenTitle);
  },

  // ===== 書き込み（ローカル反映 + outbox） =====

  createClipIngestTree,

  async createNode(input: {
    parentId?: string | null;
    projectId?: string | null;
    title: string;
    description?: string;
    nodeType?: string;
    dayDate?: string | null;
    sortOrder?: number;
    bodyJson?: Record<string, unknown>;
    bodyText?: string | null;
    sourceRefs?: readonly unknown[];
  }): Promise<DocsNode> {
    const db = getDb();
    const id = randomId();
    const now = new Date().toISOString();
    const parentId = input.parentId ?? null;
    const parent = parentId ? await getNodeRow(parentId) : null;
    if (parentId) assertDocsWritable(parent);
    const sortOrder =
      typeof input.sortOrder === "number"
        ? input.sortOrder
        : await nextSortOrder(parentId);
    const projectId = input.projectId !== undefined
      ? input.projectId ?? null
      : parent?.projectId ?? null;
    // root_page_id はサーバ権威。ローカルは親から推定し、pull で正規化される。
    let rootPageId: string | null = null;
    if (parentId) {
      rootPageId = parent?.rootPageId ?? parent?.id ?? null;
    }
    const nodeType = input.nodeType ?? "node";
    const sourceRefs = sanitizeClipIngestSourceRefs(input.sourceRefs);
    const node: DocsNode = {
      id,
      workspace_id: parent?.workspaceId ?? null,
      parent_id: parentId,
      root_page_id: rootPageId,
      project_id: projectId,
      source: parent?.source ?? (projectId ? "project" : "personal"),
      access: parent?.access ?? "write",
      read_only: parent?.readOnly ?? false,
      system_key: null,
      title: input.title ?? "",
      aliases: [],
      description: input.description ?? null,
      body_json: input.bodyJson ?? null,
      body_text: input.bodyText ?? null,
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
          workspace_id: node.workspace_id,
          parent_id: parentId,
          project_id: projectId,
          title: node.title,
          ...(node.body_text !== null ? { body_text: node.body_text } : {}),
          description: node.description,
          node_type: nodeType,
          day_date: node.day_date,
          sort_order: sortOrder,
          ...(input.bodyJson ? { body_json: input.bodyJson } : {}),
          ...(sourceRefs.length ? { source_refs: sourceRefs } : {}),
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
      bodyText?: string | null;
      sourceRefs?: readonly unknown[];
      sortOrder?: number;
      projectId?: string | null;
    },
  ): Promise<DocsNode> {
    const db = getDb();
    const before = await getNodeRow(id);
    assertDocsWritable(before);
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
    if ("bodyText" in patch && patch.bodyText !== undefined) {
      localSet.bodyText = patch.bodyText;
      payload.body_text = patch.bodyText;
    }
    if ("sourceRefs" in patch && patch.sourceRefs !== undefined) {
      const sourceRefs = sanitizeClipIngestSourceRefs(patch.sourceRefs);
      // Do not enqueue an empty/invalid provenance-only update.  Sending the
      // rejected input would make sync retry a known 400 forever; valid refs
      // are still forwarded as the optional top-level field.
      if (sourceRefs.length) payload.source_refs = sourceRefs;
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
        docsScopeKey: docsScopeKeyForRow(before),
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
    assertDocsWritable(before);
    assertDocsWritable(newParent);
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
        docsScopeKey: docsScopeKeyForRow(before),
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
    assertDocsWritable(node);
    assertDocsWritable(parent);
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
          docsScopeKey: docsScopeKeyForRow(before),
        });
      }
      return;
    }
    await this.moveNode(id, parent.parentId);
  },

  async archiveNode(id: string): Promise<void> {
    const db = getDb();
    const before = await getNodeRow(id);
    assertDocsWritable(before);
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
        docsScopeKey: docsScopeKeyForRow(before),
      });
    }
  },

  async addTag(
    nodeId: string,
    opts: { supertagId?: string; name?: string },
  ): Promise<void> {
    const db = getDb();
    const node = await getNodeRow(nodeId);
    assertDocsWritable(node);
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
        docsScopeKey: docsScopeKeyForRow(node),
      });
    }
  },

  async removeTag(nodeId: string, supertagId: string): Promise<void> {
    const db = getDb();
    const node = await getNodeRow(nodeId);
    assertDocsWritable(node);
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
        docsScopeKey: docsScopeKeyForRow(node),
      });
    }
  },

  async setField(
    nodeId: string,
    fieldId: string,
    value: unknown,
  ): Promise<void> {
    const db = getDb();
    const node = await getNodeRow(nodeId);
    assertDocsWritable(node);
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
        docsScopeKey: docsScopeKeyForRow(node),
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
