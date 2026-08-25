import { fetchApi } from '../lib/api-client';

const SYNC_PULL_TIMEOUT = 60_000;

export type SyncAction = 'create' | 'update' | 'delete' | 'restore';
export type SyncTable =
  | 'projects'
  | 'tasks'
  | 'task_occurrences'
  | 'time_entries'
  | 'conversation_sessions'
  | 'conversation_messages'
  | 'record_tables'
  | 'record_fields'
  | 'record_rows'
  | 'knowledge_nodes'
  | 'knowledge_supertags'
  | 'knowledge_node_supertags'
  | 'knowledge_supertag_fields'
  | 'knowledge_fields'
  | 'knowledge_field_values'
  | 'knowledge_node_placements'
  | 'knowledge_edges';

export interface SyncTablePayload<T = Record<string, unknown>> {
  changes: T[];
  tombstones: Array<{ id: string; deleted_at?: string | null }>;
  cursor?: string | null;
  /** サーバー正本の総行数。分割再同期の進捗表示に使う。 */
  authoritative_count?: number;
  authoritative_ids?: string[];
  authoritative_scope_id?: string;
  // Docs 8テーブルは最終pageで権威digestを返す。クライアントは保存・エコーする。
  authoritative_digest?: string;
  /** Opaque server snapshot token echoed on every Docs page. */
  docs_snapshot_token?: string;
  /** Per-table immutable revision/digest for the current Docs snapshot. */
  docs_snapshot_revision?: string;
  /** Opaque ACL/scope revision echoed on every Docs page. */
  docs_scope_revision?: string;
}

export interface SyncPullResponse {
  tables: Partial<Record<SyncTable, SyncTablePayload>>;
  server_time: string;
  has_more: boolean;
  docs_pagination_version?: number;
  docs_scope_digest?: string;
  /** Opaque server snapshot token, stable across all pages in one run. */
  docs_snapshot_token?: string;
  /** Opaque ACL revision, stable across all pages in one run. */
  docs_scope_revision?: string;
  /** All Docs workspaces currently visible to this actor. */
  docs_scopes?: DocsSyncScope[];
}

export interface DocsSyncScope {
  workspace_id: string;
  source: "personal" | "shared" | "project" | string;
  access: "owner" | "read" | "write" | string;
  read_only: boolean;
  project_id?: string | null;
}

export interface SyncPushOperation {
  op_id: string;
  table: string;
  action: SyncAction;
  entity_id: string;
  payload: Record<string, unknown>;
  base_updated_at?: string | null;
}

export interface SyncPushResult {
  op_id: string;
  status: 'ok' | 'conflict' | 'error';
  entity?: Record<string, unknown>;
  server_updated_at?: string | null;
  reason?: string;
}

export async function pullSync(params: {
  since?: string | null;
  tables?: SyncTable[];
  docs_digests?: Record<string, string>;
  docs_cursors?: Record<string, string>;
  docs_pagination?: boolean;
  docs_scope_digest?: string;
  docs_scope_id?: string;
  /** Project discriminator for shared Docs libraries. */
  project_id?: string;
  docs_reconcile?: boolean;
  docs_snapshot_token?: string;
  docs_scope_revision?: string;
}): Promise<SyncPullResponse> {
  const search = new URLSearchParams();
  if (params.since) search.set('since', params.since);
  if (params.tables?.length) search.set('tables', params.tables.join(','));
  // Docs digest はサーバ現在値と一致すれば全量再送を省ける。保存済み digest だけ
  // クエリパラメータ（URLエンコードJSON）で送る。RN の fetch は GET に body を
  // 付けられず、/pull は GET のみ受け付けるため、転送はクエリ経由に統一する。
  const digests = params.docs_digests;
  if (digests && Object.keys(digests).length > 0) {
    search.set('docs_digests', JSON.stringify(digests));
  }
  const cursors = params.docs_cursors;
  if (cursors && Object.keys(cursors).length > 0) {
    search.set('docs_cursors', JSON.stringify(cursors));
  }
  if (params.docs_pagination) search.set('docs_pagination', '1');
  if (params.docs_reconcile === false) search.set('docs_reconcile', '0');
  if (params.docs_scope_digest) {
    search.set('docs_scope_digest', params.docs_scope_digest);
  }
  if (params.docs_scope_id) search.set('docs_scope_id', params.docs_scope_id);
  if (params.project_id) search.set('project_id', params.project_id);
  if (params.docs_snapshot_token) {
    search.set('docs_snapshot_token', params.docs_snapshot_token);
  }
  if (params.docs_scope_revision) {
    search.set('docs_scope_revision', params.docs_scope_revision);
  }
  const suffix = search.toString() ? `?${search.toString()}` : '';
  return fetchApi<SyncPullResponse>(
    `/api/sync/pull${suffix}`,
    {},
    SYNC_PULL_TIMEOUT,
  );
}

export async function pushSync(operations: SyncPushOperation[]): Promise<{
  results: SyncPushResult[];
}> {
  return fetchApi<{ results: SyncPushResult[] }>('/api/sync/push', {
    method: 'POST',
    body: JSON.stringify({ operations }),
  });
}
