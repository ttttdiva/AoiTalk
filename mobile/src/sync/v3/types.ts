/**
 * Sync v3 wire contract (server: src/sync_v3/contract.py, src/api/sync_v3_routes.py,
 * src/api/sync_v3_ops_routes.py, src/sync_v3/ops.py) and the local state shape.
 */

export const SYNC_V3_PROTOCOL = 3;
export const SYNC_V3_MAX_OPS_PER_REQUEST = 50;
export const SYNC_V3_MAX_OPS_BODY_BYTES = 512 * 1024;
export const SYNC_V3_CHANGES_PAGE_LIMIT = 500;
export const SYNC_V3_BOOTSTRAP_PAGE_LIMIT = 500;

export type SyncV3Table =
  | "spaces"
  | "projects"
  | "tasks"
  | "task_occurrences"
  | "time_entries"
  | "conversation_sessions"
  | "conversation_messages"
  | "record_tables"
  | "record_fields"
  | "record_rows"
  | "knowledge_nodes"
  | "knowledge_supertags"
  | "knowledge_node_supertags"
  | "knowledge_supertag_fields"
  | "knowledge_fields"
  | "knowledge_field_values"
  | "knowledge_node_placements"
  | "knowledge_edges";

/** Local apply order: spaces first, then the v2 applyPullResponse order. */
export const SYNC_V3_APPLY_ORDER: readonly SyncV3Table[] = [
  "spaces",
  "projects",
  "tasks",
  "task_occurrences",
  "time_entries",
  "conversation_sessions",
  "conversation_messages",
  "record_tables",
  "record_fields",
  "record_rows",
  "knowledge_nodes",
  "knowledge_supertags",
  "knowledge_fields",
  "knowledge_field_values",
  "knowledge_node_supertags",
  "knowledge_supertag_fields",
  "knowledge_node_placements",
  "knowledge_edges",
];

export const SYNC_V3_DOCS_TABLES: readonly SyncV3Table[] = [
  "knowledge_nodes",
  "knowledge_supertags",
  "knowledge_node_supertags",
  "knowledge_supertag_fields",
  "knowledge_fields",
  "knowledge_field_values",
  "knowledge_node_placements",
  "knowledge_edges",
];

export const SYNC_V3_RECORD_TABLES: readonly SyncV3Table[] = [
  "record_tables", "record_fields", "record_rows",
];

export function isSyncV3Table(value: unknown): value is SyncV3Table {
  return typeof value === "string" && (SYNC_V3_APPLY_ORDER as readonly string[]).includes(value);
}

export type SyncV3ScopeKind = "user" | "space" | "project" | "docs";

export type SyncV3CapabilitiesResponse = {
  protocol: number;
  enabled: boolean;
  max_ops_per_request: number;
};

export type SyncV3Scope = {
  scope_key: string;
  kind: SyncV3ScopeKind;
  id: string;
  project_id: string | null;
  read_only: boolean;
  access: string;
  tables: SyncV3Table[];
  fingerprint: string;
};

export type SyncV3ScopesResponse = {
  scope_set_revision: string;
  scopes: SyncV3Scope[];
  head_cursor: string;
};

export type SyncV3ChangeOp = "upsert" | "delete" | "hide";

export type SyncV3Change = {
  table: SyncV3Table;
  entity_id: string;
  op: SyncV3ChangeOp;
  row: Record<string, unknown> | null;
  row_version: string;
  scope_keys: string[];
};

export type SyncV3ChangesResponse = {
  changes: SyncV3Change[];
  cursor: string;
  has_more: boolean;
  scope_set_revision: string;
  server_time: string | null;
  head_committed_at: string | null;
};

export type SyncV3BootstrapRow = {
  entity_id: string;
  row: Record<string, unknown>;
  row_version: string;
};

export type SyncV3BootstrapResponse = {
  rows: SyncV3BootstrapRow[];
  next_after: string | null;
  total_count: number | null;
};

export type SyncV3OpAction = "create" | "update" | "delete" | "restore" | "reorder";

export type SyncV3Op = {
  op_id: string;
  op_seq: number;
  table: string;
  action: SyncV3OpAction;
  entity_id: string;
  scope_key: string | null;
  patch: Record<string, unknown>;
  base: { updated_at: string | null };
};

export type SyncV3OpsRequest = {
  device_id: string;
  retirement_floor: number;
  ops: SyncV3Op[];
};

export type SyncV3OpStatus = "applied" | "applied_partial" | "rejected" | "retry";

export type SyncV3OpResult = {
  op_id: string;
  status: SyncV3OpStatus;
  code: string | null;
  /** Same meaning as the v2 push reason_code (server U2). */
  reason_code?: string | null;
  entity: Record<string, unknown> | null;
  row_version: string | null;
  conflicts: unknown[];
  retry_after_ms: number | null;
};

export type SyncV3OpsResponse = { results: SyncV3OpResult[] };

/** The server reports v3 as disabled (503 sync_disabled). The run falls back to v2. */
export class SyncV3DisabledError extends Error {
  constructor() {
    super("Sync v3 is disabled on the server");
    this.name = "SyncV3DisabledError";
  }
}

/** The saved changes cursor is older than the server retention (410 cursor_expired). */
export class SyncV3CursorExpiredError extends Error {
  constructor() {
    super("Sync v3 cursor expired");
    this.name = "SyncV3CursorExpiredError";
  }
}

// ---------- local state (sync_state.cursor JSON) ----------

export type SyncV3BootstrapPosition = {
  tableIndex: number;
  after: string | null;
  seen?: number;
  total?: number | null;
  /** Server fingerprint this bootstrap collects; saved as the scope fingerprint on completion. */
  fingerprint?: string;
};

export type SyncV3ScopeState = {
  evictionPending?: boolean;
  fingerprint: string;
  generation: number;
  tables: SyncV3Table[];
  bootstrap: SyncV3BootstrapPosition | null;
  /** Scope metadata kept for Docs membership projection. */
  kind?: SyncV3ScopeKind;
  id?: string;
  projectId?: string | null;
  access?: string | null;
  readOnly?: boolean | null;
  lastBootstrap?: { seen: number; total: number | null };
};

export type SyncV3State = {
  v: 1;
  cursor: string | null;
  scopeSetRevision: string | null;
  fullEvictPending: boolean;
  membershipResetPending?: boolean;
  lastSuccessAt?: number | null;
  scopes: Record<string, SyncV3ScopeState>;
};

export type SyncV3DeviceState = {
  device_id: string;
  next_op_seq: number;
};
