import type { DocsSqliteAsyncTransaction } from "../db/docs-sync-async";
import { compareDocsVersions } from "./docs-create-ack";

export const DOCS_PROMOTION_SNAPSHOT_PAGE_SIZE = 256;
/** Bound pure-JS work without paying one timer turn for every SQLite page. */
export const DOCS_PROMOTION_CPU_YIELD_INTERVAL = 4_096;

type DocsAuthoritativeLike = Record<string, { ids?: string[] }>;

export type DocsPromotionDirtyRows = {
  nodes: Set<string>;
  supertags: Set<string>;
  nodeSupertags: Set<string>;
  fieldValues: Set<string>;
};

export type DocsPromotionMembershipSnapshot = {
  refs: Map<string, number>;
  scoped: Map<string, Set<string>>;
  readonlyNodes: Set<string>;
  /** Existing scoped rows whose stored ACL projection needs refreshing. */
  needsUpsert: Map<string, Set<string>>;
};

export type DocsPromotionQuarantineMembershipSnapshot = {
  refs: Map<string, number>;
  scopedKeys: Set<string>;
  otherWritableKeys: Set<string>;
};

export type DocsPromotionScopedOutboxRow = {
  op_id: string;
  table_name: string;
  entity_id: string;
  docs_scope_key: string | null;
};

export type DocsPromotionQuarantineOutboxSnapshot = {
  rows: DocsPromotionScopedOutboxRow[];
  entityKeys: Set<string>;
};

type MembershipScanRow = {
  scope_key: string;
  scope_id: string | null;
  project_id: string | null;
  table_name: string;
  entity_key: string;
  state: string;
  access: string | null;
  read_only: unknown;
};

type DocsPromotionTargetScope = {
  workspace_id?: string;
  project_id?: string | null;
};

function sqliteBoolean(value: unknown): boolean {
  return value === true || value === 1 || value === "1";
}

async function yieldDocsPromotionCpu(): Promise<void> {
  await new Promise<void>((resolve) => setTimeout(resolve, 0));
}

function isPromiseLike(value: unknown): value is PromiseLike<void> {
  return Boolean(
    value &&
      (typeof value === "object" || typeof value === "function") &&
      typeof (value as PromiseLike<void>).then === "function",
  );
}

/**
 * Iterate a potentially large in-memory set without monopolising the JS event
 * loop.  Database-backed handlers still await their native call normally;
 * pure-JS handlers get an explicit macrotask yield every bounded chunk.
 */
export async function forEachDocsPromotionChunk<T>(
  items: Iterable<T>,
  handler: (item: T) => void | Promise<void>,
  assertActive: () => void = () => undefined,
): Promise<void> {
  let processed = 0;
  assertActive();
  for (const item of items) {
    const pending = handler(item);
    if (isPromiseLike(pending)) await pending;
    processed += 1;
    if (processed % DOCS_PROMOTION_CPU_YIELD_INTERVAL === 0) {
      await yieldDocsPromotionCpu();
      assertActive();
    }
  }
}

/**
 * Build authoritative id sets exactly once, before the SQLite promotion
 * transaction starts.  The old promotion rebuilt `new Set(ids)` from inside
 * stale-row filters, turning a large snapshot into O(N*M) JS work while the
 * exclusive transaction was open.
 */
export async function buildDocsAuthoritativeSets(
  authoritative: DocsAuthoritativeLike,
  tableNames: readonly string[],
  assertActive: () => void = () => undefined,
): Promise<Map<string, Set<string>>> {
  const result = new Map<string, Set<string>>();
  for (const table of tableNames) {
    assertActive();
    const ids = authoritative[table]?.ids;
    if (ids == null) continue;
    const set = new Set<string>();
    for (let index = 0; index < ids.length; index += 1) {
      set.add(ids[index]);
      if (
        (index + 1) % DOCS_PROMOTION_CPU_YIELD_INTERVAL === 0 &&
        index + 1 < ids.length
      ) {
        await yieldDocsPromotionCpu();
        assertActive();
      }
    }
    result.set(table, set);
  }
  return result;
}

async function forEachSnapshotPage<T>(
  loadPage: (cursor: T | null) => Promise<T[]>,
  cursorKey: (row: T) => string,
  visit: (row: T) => void,
): Promise<void> {
  let cursor: T | null = null;
  let previousCursorKey: string | null = null;
  while (true) {
    const rows = await loadPage(cursor);
    if (!rows.length) return;
    for (const row of rows) visit(row);
    const last = rows[rows.length - 1];
    const nextCursorKey = cursorKey(last);
    if (!nextCursorKey || nextCursorKey === previousCursorKey) return;
    cursor = last;
    previousCursorKey = nextCursorKey;
    if (rows.length < DOCS_PROMOTION_SNAPSHOT_PAGE_SIZE) return;
  }
}

async function scanMembershipRowsTx(
  tx: DocsSqliteAsyncTransaction,
  authScope: string,
  visit: (row: MembershipScanRow) => void,
): Promise<void> {
  await forEachSnapshotPage<MembershipScanRow>(
    (cursor) =>
      cursor == null
        ? tx.getAllAsync<MembershipScanRow>(
            `SELECT scope_key, scope_id, project_id, table_name, entity_key, state, access, read_only
               FROM docs_scope_membership
              WHERE auth_scope = ?
              ORDER BY scope_key ASC, table_name ASC, entity_key ASC
              LIMIT ${DOCS_PROMOTION_SNAPSHOT_PAGE_SIZE}`,
            authScope,
          )
        : tx.getAllAsync<MembershipScanRow>(
            `SELECT scope_key, scope_id, project_id, table_name, entity_key, state, access, read_only
               FROM docs_scope_membership
              WHERE auth_scope = ?
                AND (scope_key, table_name, entity_key) > (?, ?, ?)
              ORDER BY scope_key ASC, table_name ASC, entity_key ASC
              LIMIT ${DOCS_PROMOTION_SNAPSHOT_PAGE_SIZE}`,
            authScope,
            cursor.scope_key,
            cursor.table_name,
            cursor.entity_key,
          ),
    (row) => `${row.scope_key}\u0000${row.table_name}\u0000${row.entity_key}`,
    visit,
  );
}

/** One paged account scan replaces the old target-scope + full-account reads. */
export async function loadDocsMembershipSnapshotTx(
  tx: DocsSqliteAsyncTransaction,
  authScope: string,
  scopeKey: string,
  scopeMeta?: {
    workspace_id?: string;
    project_id?: string | null;
    access?: string;
    read_only?: boolean;
  },
  assertActive: () => void = () => undefined,
  targetScope?: DocsPromotionTargetScope,
): Promise<DocsPromotionMembershipSnapshot> {
  const refs = new Map<string, number>();
  const scoped = new Map<string, Set<string>>();
  const readonlyNodes = new Set<string>();
  const needsUpsert = new Map<string, Set<string>>();
  const markNeedsUpsert = (table: string, key: string): void => {
    const rows = needsUpsert.get(table) ?? new Set<string>();
    rows.add(key);
    needsUpsert.set(table, rows);
  };
  const membershipNeedsRefresh = (
    row: MembershipScanRow,
    desiredState: string,
  ): boolean => {
    // The membership identity is scoped by scope_key, but the UPSERT also
    // refreshes the target scope metadata.  Prefer the target identity passed
    // independently from the ACL projection; the scopeMeta fallback keeps
    // older compatibility callers working when they provide both together.
    const targetScopeId = targetScope
      ? targetScope.workspace_id
      : scopeMeta?.workspace_id;
    const targetProjectId = targetScope
      ? targetScope.project_id ?? null
      : scopeMeta?.project_id ?? null;
    const metadataNeedsRefresh = Boolean(targetScopeId)
      && (row.scope_id !== targetScopeId
        || (row.project_id ?? null) !== targetProjectId);
    const desiredAccess = desiredState === "readonly" ? "read" : scopeMeta?.access ?? null;
    const desiredReadOnly = desiredState === "readonly"
      ? 1
      : scopeMeta?.read_only == null ? null : scopeMeta.read_only ? 1 : 0;
    const currentReadOnly = row.read_only == null ? null : sqliteBoolean(row.read_only) ? 1 : 0;
    return metadataNeedsRefresh
      || row.state !== desiredState
      || row.access !== desiredAccess
      || currentReadOnly !== desiredReadOnly;
  };
  assertActive();
  await scanMembershipRowsTx(tx, authScope, (row) => {
    if (row.state === "deleted") return;
    const refKey = `${row.table_name}:${row.entity_key}`;
    refs.set(refKey, (refs.get(refKey) ?? 0) + 1);
    // Shared live-row deletion needs account-wide refs; ACL projection is
    // owned by the requested scope and must not inherit a sibling's state.
    if (row.scope_key !== scopeKey) return;
    if (row.table_name === "knowledge_nodes" && (
      row.state !== "active" || sqliteBoolean(row.read_only) || row.access === "read"
    )) {
      readonlyNodes.add(row.entity_key);
    }
    const desiredState = row.table_name === "knowledge_nodes" && readonlyNodes.has(row.entity_key)
      ? "readonly"
      : "active";
    if (membershipNeedsRefresh(row, desiredState)) {
      markNeedsUpsert(row.table_name, row.entity_key);
    }
    const bucket = scoped.get(row.table_name) ?? new Set<string>();
    bucket.add(row.entity_key);
    scoped.set(row.table_name, bucket);
  });
  return { refs, scoped, readonlyNodes, needsUpsert };
}

export async function loadDocsQuarantineMembershipSnapshotTx(
  tx: DocsSqliteAsyncTransaction,
  authScope: string,
  scopeKey: string,
  mode: "revoke" | "downgrade",
): Promise<DocsPromotionQuarantineMembershipSnapshot> {
  const refs = new Map<string, number>();
  const scopedKeys = new Set<string>();
  const otherWritableKeys = new Set<string>();
  await scanMembershipRowsTx(tx, authScope, (row) => {
    const compound = `${row.table_name}:${row.entity_key}`;
    if (row.state !== "deleted") {
      refs.set(compound, (refs.get(compound) ?? 0) + 1);
    }
    if (
      row.scope_key === scopeKey &&
      (mode === "revoke" ? row.state !== "deleted" : row.state === "active")
    ) {
      scopedKeys.add(compound);
    }
    if (
      row.scope_key !== scopeKey &&
      row.state === "active" &&
      !sqliteBoolean(row.read_only) &&
      row.access !== "read"
    ) {
      otherWritableKeys.add(compound);
    }
  });
  return { refs, scopedKeys, otherWritableKeys };
}

export async function loadDocsScopedOutboxProtectionTx(
  tx: DocsSqliteAsyncTransaction,
  authScope: string,
  scopeKey: string,
  tableNames: readonly string[],
): Promise<Map<string, Set<string>>> {
  const allowed = new Set(tableNames);
  const protection = new Map<string, Set<string>>();
  type Row = { op_id: string; table_name: string; entity_id: string };
  await forEachSnapshotPage<Row>(
    (cursor) =>
      cursor == null
        ? tx.getAllAsync<Row>(
            `SELECT op_id, table_name, entity_id
               FROM outbox
              WHERE (auth_scope IS NULL OR auth_scope = ?)
                AND docs_scope_key = ?
              ORDER BY op_id ASC
              LIMIT ${DOCS_PROMOTION_SNAPSHOT_PAGE_SIZE}`,
            authScope,
            scopeKey,
          )
        : tx.getAllAsync<Row>(
            `SELECT op_id, table_name, entity_id
               FROM outbox
              WHERE (auth_scope IS NULL OR auth_scope = ?)
                AND docs_scope_key = ?
                AND op_id > ?
              ORDER BY op_id ASC
              LIMIT ${DOCS_PROMOTION_SNAPSHOT_PAGE_SIZE}`,
            authScope,
            scopeKey,
            cursor.op_id,
          ),
    (row) => row.op_id,
    (row) => {
      if (!allowed.has(row.table_name)) return;
      const bucket = protection.get(row.table_name) ?? new Set<string>();
      bucket.add(row.entity_id);
      protection.set(row.table_name, bucket);
    },
  );
  return protection;
}

/** An authoritative pull cannot tombstone an unsent create in this writable scope. */
export async function loadDocsPendingNodeCreatesTx(
  tx: DocsSqliteAsyncTransaction,
  authScope: string,
  scopeKey: string,
  workspaceId: string,
  projectId: string | null,
  snapshotServerTime?: string | null,
): Promise<Set<string>> {
  const ids = new Set<string>();
  type Row = { op_id: string; entity_id: string };
  await forEachSnapshotPage<Row>(
    (cursor) => tx.getAllAsync<Row>(
      `SELECT o.op_id, o.entity_id FROM outbox o
         JOIN knowledge_nodes n ON n.id = o.entity_id
         JOIN docs_scope_membership m ON m.auth_scope = o.auth_scope
          AND m.scope_key = o.docs_scope_key AND m.table_name = o.table_name
          AND m.entity_key = o.entity_id
        WHERE o.auth_scope = ? AND o.docs_scope_key = ?
          AND o.table_name = 'knowledge_nodes' AND o.action = 'create'
          AND n.workspace_id = ? AND n.project_id IS ?
          AND n.read_only = 0 AND n.access IN ('owner', 'write')
          AND m.state = 'active' AND m.read_only = 0 AND m.access IN ('owner', 'write')
          ${cursor ? "AND o.op_id > ?" : ""}
        ORDER BY o.op_id ASC LIMIT ${DOCS_PROMOTION_SNAPSHOT_PAGE_SIZE}`,
      authScope, scopeKey, workspaceId, projectId,
      ...(cursor ? [cursor.op_id] : []),
    ),
    (row) => row.op_id,
    (row) => { ids.add(row.entity_id); },
  );
  if (Number.isFinite(Date.parse(snapshotServerTime ?? ""))) {
    await forEachSnapshotPage<{ id: string; created_at: string }>(
      (cursor) => tx.getAllAsync<{ id: string; created_at: string }>(
        `SELECT n.id, n.created_at FROM knowledge_nodes n JOIN docs_scope_membership m ON m.entity_key = n.id
          WHERE m.auth_scope = ? AND m.scope_key = ? AND m.table_name = 'knowledge_nodes'
            AND m.state = 'active' AND m.read_only = 0 AND m.access IN ('owner', 'write')
            AND n.workspace_id = ? AND n.project_id IS ?
            AND n.created_by = ? AND n.server_updated_at IS NOT NULL
            AND julianday(n.created_at) >= julianday(?)
            AND n.read_only = 0 AND n.access IN ('owner', 'write')
            ${cursor ? "AND n.id > ?" : ""}
          ORDER BY n.id ASC LIMIT ${DOCS_PROMOTION_SNAPSHOT_PAGE_SIZE}`,
        authScope, scopeKey, workspaceId, projectId, authScope.slice("auth:".length), snapshotServerTime!,
        ...(cursor ? [cursor.id] : []),
      ),
      (row) => row.id,
      (row) => { if (compareDocsVersions(row.created_at, snapshotServerTime!) === 1) ids.add(row.id); },
    );
  }
  return ids;
}

export async function loadDocsQuarantineOutboxSnapshotTx(
  tx: DocsSqliteAsyncTransaction,
  authScope: string,
  scopeKey: string,
  tableNames: readonly string[],
): Promise<DocsPromotionQuarantineOutboxSnapshot> {
  const allowed = new Set(tableNames);
  const rows: DocsPromotionScopedOutboxRow[] = [];
  const entityKeys = new Set<string>();
  await forEachSnapshotPage<DocsPromotionScopedOutboxRow>(
    (cursor) =>
      cursor == null
        ? tx.getAllAsync<DocsPromotionScopedOutboxRow>(
            `SELECT op_id, table_name, entity_id, docs_scope_key
               FROM outbox
              WHERE auth_scope = ? AND docs_scope_key = ?
              ORDER BY op_id ASC
              LIMIT ${DOCS_PROMOTION_SNAPSHOT_PAGE_SIZE}`,
            authScope,
            scopeKey,
          )
        : tx.getAllAsync<DocsPromotionScopedOutboxRow>(
            `SELECT op_id, table_name, entity_id, docs_scope_key
               FROM outbox
              WHERE auth_scope = ? AND docs_scope_key = ? AND op_id > ?
              ORDER BY op_id ASC
              LIMIT ${DOCS_PROMOTION_SNAPSHOT_PAGE_SIZE}`,
            authScope,
            scopeKey,
            cursor.op_id,
          ),
    (row) => row.op_id,
    (row) => {
      if (!allowed.has(row.table_name)) return;
      rows.push(row);
      entityKeys.add(`${row.table_name}:${row.entity_id}`);
    },
  );
  return { rows, entityKeys };
}

export async function loadDocsDirtyRowsPagedTx(
  tx: DocsSqliteAsyncTransaction,
): Promise<DocsPromotionDirtyRows> {
  const nodes = new Set<string>();
  const supertags = new Set<string>();
  const nodeSupertags = new Set<string>();
  const fieldValues = new Set<string>();

  type IdRow = { id: string };
  await forEachSnapshotPage<IdRow>(
    (cursor) =>
      cursor == null
        ? tx.getAllAsync<IdRow>(
            `SELECT id FROM knowledge_nodes WHERE dirty = 1
              ORDER BY id ASC LIMIT ${DOCS_PROMOTION_SNAPSHOT_PAGE_SIZE}`,
          )
        : tx.getAllAsync<IdRow>(
            `SELECT id FROM knowledge_nodes WHERE dirty = 1 AND id > ?
              ORDER BY id ASC LIMIT ${DOCS_PROMOTION_SNAPSHOT_PAGE_SIZE}`,
            cursor.id,
          ),
    (row) => row.id,
    (row) => nodes.add(row.id),
  );

  await forEachSnapshotPage<IdRow>(
    (cursor) =>
      cursor == null
        ? tx.getAllAsync<IdRow>(
            `SELECT id FROM knowledge_supertags WHERE dirty = 1
              ORDER BY id ASC LIMIT ${DOCS_PROMOTION_SNAPSHOT_PAGE_SIZE}`,
          )
        : tx.getAllAsync<IdRow>(
            `SELECT id FROM knowledge_supertags WHERE dirty = 1 AND id > ?
              ORDER BY id ASC LIMIT ${DOCS_PROMOTION_SNAPSHOT_PAGE_SIZE}`,
            cursor.id,
          ),
    (row) => row.id,
    (row) => supertags.add(row.id),
  );

  type NodeSupertagRow = { node_id: string; supertag_id: string };
  await forEachSnapshotPage<NodeSupertagRow>(
    (cursor) =>
      cursor == null
        ? tx.getAllAsync<NodeSupertagRow>(
            `SELECT node_id, supertag_id FROM knowledge_node_supertags
              WHERE dirty = 1
              ORDER BY node_id ASC, supertag_id ASC
              LIMIT ${DOCS_PROMOTION_SNAPSHOT_PAGE_SIZE}`,
          )
        : tx.getAllAsync<NodeSupertagRow>(
            `SELECT node_id, supertag_id FROM knowledge_node_supertags
              WHERE dirty = 1 AND (node_id, supertag_id) > (?, ?)
              ORDER BY node_id ASC, supertag_id ASC
              LIMIT ${DOCS_PROMOTION_SNAPSHOT_PAGE_SIZE}`,
            cursor.node_id,
            cursor.supertag_id,
          ),
    (row) => `${row.node_id}\u0000${row.supertag_id}`,
    (row) => nodeSupertags.add(`${row.node_id}:${row.supertag_id}`),
  );

  type FieldValueRow = { node_id: string; field_id: string };
  await forEachSnapshotPage<FieldValueRow>(
    (cursor) =>
      cursor == null
        ? tx.getAllAsync<FieldValueRow>(
            `SELECT node_id, field_id FROM knowledge_field_values
              WHERE dirty = 1
              ORDER BY node_id ASC, field_id ASC
              LIMIT ${DOCS_PROMOTION_SNAPSHOT_PAGE_SIZE}`,
          )
        : tx.getAllAsync<FieldValueRow>(
            `SELECT node_id, field_id FROM knowledge_field_values
              WHERE dirty = 1 AND (node_id, field_id) > (?, ?)
              ORDER BY node_id ASC, field_id ASC
              LIMIT ${DOCS_PROMOTION_SNAPSHOT_PAGE_SIZE}`,
            cursor.node_id,
            cursor.field_id,
          ),
    (row) => `${row.node_id}\u0000${row.field_id}`,
    (row) => fieldValues.add(`${row.node_id}:${row.field_id}`),
  );

  return { nodes, supertags, nodeSupertags, fieldValues };
}
