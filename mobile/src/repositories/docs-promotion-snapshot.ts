import type { DocsSqliteAsyncTransaction } from "../db/docs-sync-async";

export const DOCS_PROMOTION_SNAPSHOT_PAGE_SIZE = 256;

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
  table_name: string;
  entity_key: string;
  state: string;
  access: string | null;
  read_only: unknown;
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
): Promise<void> {
  let processed = 0;
  for (const item of items) {
    const pending = handler(item);
    if (isPromiseLike(pending)) await pending;
    processed += 1;
    if (processed % DOCS_PROMOTION_SNAPSHOT_PAGE_SIZE === 0) {
      await yieldDocsPromotionCpu();
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
): Promise<Map<string, Set<string>>> {
  const result = new Map<string, Set<string>>();
  for (const table of tableNames) {
    const ids = authoritative[table]?.ids;
    if (ids == null) continue;
    const set = new Set<string>();
    for (let index = 0; index < ids.length; index += 1) {
      set.add(ids[index]);
      if (
        (index + 1) % DOCS_PROMOTION_SNAPSHOT_PAGE_SIZE === 0 &&
        index + 1 < ids.length
      ) {
        await yieldDocsPromotionCpu();
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
            `SELECT scope_key, table_name, entity_key, state, access, read_only
               FROM docs_scope_membership
              WHERE auth_scope = ?
              ORDER BY scope_key ASC, table_name ASC, entity_key ASC
              LIMIT ${DOCS_PROMOTION_SNAPSHOT_PAGE_SIZE}`,
            authScope,
          )
        : tx.getAllAsync<MembershipScanRow>(
            `SELECT scope_key, table_name, entity_key, state, access, read_only
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
): Promise<DocsPromotionMembershipSnapshot> {
  const refs = new Map<string, number>();
  const scoped = new Map<string, Set<string>>();
  await scanMembershipRowsTx(tx, authScope, (row) => {
    if (row.state === "deleted") return;
    const refKey = `${row.table_name}:${row.entity_key}`;
    refs.set(refKey, (refs.get(refKey) ?? 0) + 1);
    if (row.scope_key !== scopeKey) return;
    const bucket = scoped.get(row.table_name) ?? new Set<string>();
    bucket.add(row.entity_key);
    scoped.set(row.table_name, bucket);
  });
  return { refs, scoped };
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
