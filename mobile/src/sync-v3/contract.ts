// Sync v3 共有契約（改訂3）。C1a/C2a/C3で同一内容。変更は統合タスクのみ。
export const SYNC_V3_CONTRACT_REVISION = 3;
export const SYNC_V3_PROTOCOL = 3;
export const MAX_OPS_PER_REQUEST = 50;
export const MAX_SEQ = '9223372036854775807';
export const SYNC_DISABLED_CODE = 'sync_disabled';
export const CURSOR_EXPIRED_CODE = 'cursor_expired';

export type SyncTableV3 =
  | 'spaces' | 'projects' | 'tasks' | 'task_occurrences' | 'time_entries'
  | 'conversation_sessions' | 'conversation_messages'
  | 'record_tables' | 'record_fields' | 'record_rows'
  | 'knowledge_nodes' | 'knowledge_supertags' | 'knowledge_node_supertags'
  | 'knowledge_supertag_fields' | 'knowledge_fields' | 'knowledge_field_values'
  | 'knowledge_node_placements' | 'knowledge_edges';
export type OpAction = 'create' | 'update' | 'delete' | 'restore' | 'reorder';
export type BaseUnknown = { $unknown: true };
export const BASE_UNKNOWN: BaseUnknown = { $unknown: true };

export type OpRequest = {
  op_id: string; table: SyncTableV3; action: OpAction; entity_id: string;
  scope_key: string | null; patch: Record<string, unknown>; base: Record<string, unknown>;
};
export type OpCode = 'field_conflict' | 'entity_deleted' | 'forbidden' | 'invalid' | 'unsupported' | 'server_error';
/** row_version は entity ごとのcommit順の版（10進文字列）。entity が無い場合は null。 */
export type OpResult = {
  op_id: string; status: 'applied' | 'applied_partial' | 'rejected' | 'retry';
  code: OpCode | null; entity: Record<string, unknown> | null; row_version: string | null;
  conflicts: Array<{ field: string; local: unknown; server: unknown; base: unknown }>;
  retry_after_ms: number | null;
};
export type OpsResponse = { results: OpResult[] };
export type ScopeKind = 'user' | 'space' | 'project' | 'docs';
export type ScopeV3 = {
  scope_key: string; kind: ScopeKind; id: string; project_id: string | null;
  read_only: boolean; access: string; tables: SyncTableV3[];
  /** 可視範囲（権限・共有）の指紋。変化したscopeは世代を上げて再取得する。 */
  fingerprint: string;
};
export type ScopesResponse = { scope_set_revision: string; scopes: ScopeV3[]; head_cursor: string };
/** delete=サーバーに存在しない。hide=存在するが本人に見えない。 */
export type ChangeV3 = {
  table: SyncTableV3; entity_id: string; op: 'upsert' | 'delete' | 'hide';
  row: Record<string, unknown> | null; row_version: string; scope_keys: string[];
};
/** cursor は "<commit_seq>:<seq>"。commit順で欠落なく進む。 */
export type ChangesResponse = {
  changes: ChangeV3[]; cursor: string; has_more: boolean; scope_set_revision: string;
  server_time: string; head_committed_at: string | null;
};
export type BootstrapResponse = {
  rows: Array<{ entity_id: string; row: Record<string, unknown>; row_version: string }>;
  next_after: string | null; total_count: number | null;
};
export type CapabilitiesResponse = { protocol: number; enabled: boolean; max_ops_per_request: number };

export type ConflictKind = 'field_conflict' | 'entity_deleted' | 'forbidden' | 'invalid'
  | 'scope_unknown' | 'access_revoked' | 'migrated_unresolved';
export type ConflictRecord = {
  id: string; auth_scope: string; table: SyncTableV3; entity_id: string; scope_key: string | null;
  kind: ConflictKind; local: Record<string, unknown>; base: Record<string, unknown> | null;
  server: Record<string, unknown> | null; fields: string[]; source_op_id: string | null;
  title_hint: string | null; created_at: number; state: 'open' | 'reapplied' | 'dismissed';
};
export interface ConflictStore {
  listOpen(authScope: string): Promise<ConflictRecord[]>;
  countOpen(authScope: string): Promise<number>;
  reapply(id: string): Promise<string | null>;
  dismiss(id: string): Promise<void>;
}

export type LaneState = {
  state: 'idle' | 'running' | 'backoff' | 'offline' | 'auth_required' | 'disabled';
  lastSuccessAt: number | null; nextAttemptAt: number | null; lastErrorCode: string | null;
};
export type LegacyMigrationStatus = {
  state: 'not_needed' | 'capturing' | 'awaiting_confirm' | 'migrating' | 'migrated' | 'failed';
  legacyCount: number | null; ownerUnknownCount: number | null;
  captureSha256: string | null; captureFile: string | null; errorCode: string | null;
};
export type SyncV3Overall = 'synced' | 'syncing' | 'pending' | 'bootstrapping' | 'offline'
  | 'auth_required' | 'disabled' | 'migration_pending' | 'degraded';
export type SyncV3Status = {
  authScope: string | null; push: LaneState; changes: LaneState;
  bootstrap: LaneState & { scopesPending: number; seen: number; total: number | null };
  queue: { queued: number; backoff: number; sent: number };
  openConflicts: number; legacy: LegacyMigrationStatus;
  disabledReason: string | null; overall: SyncV3Overall;
};
export type SyncTrigger = 'interval' | 'foreground' | 'mutation' | 'manual' | 'auth';
export interface SyncV3EngineControl {
  start(authScope: string): void;
  stop(): Promise<void>;
  requestSync(reason: SyncTrigger): void;
  requestRebootstrap(scopeKey: string): Promise<void>;
  /** 画面に表示した captureSha256 と一致した場合だけ、旧outboxの移行を実行する。 */
  confirmLegacyMigration(captureSha256: string): Promise<void>;
  getStatus(): SyncV3Status;
  subscribe(listener: (status: SyncV3Status) => void): () => void;
}

let engine: SyncV3EngineControl | null = null;
let conflictStore: ConflictStore | null = null;
export function registerSyncV3Engine(value: SyncV3EngineControl | null): void { engine = value; }
export function getSyncV3Engine(): SyncV3EngineControl | null { return engine; }
export function registerConflictStore(value: ConflictStore | null): void { conflictStore = value; }
export function getConflictStore(): ConflictStore | null { return conflictStore; }
export function notifySyncMutation(): void { engine?.requestSync('mutation'); }

export function compareCursor(a: string, b: string): number {
  const [at, as] = a.split(':').map(BigInt);
  const [bt, bs] = b.split(':').map(BigInt);
  if (at !== bt) return at < bt ? -1 : 1;
  if (as !== bs) return as < bs ? -1 : 1;
  return 0;
}
export function compareRowVersion(a: string, b: string): number {
  const x = BigInt(a); const y = BigInt(b);
  return x === y ? 0 : x < y ? -1 : 1;
}
export function docsScopeKey(libraryId: string, projectId: string | null): string {
  return `${libraryId}|project:${projectId ?? ''}`;
}
