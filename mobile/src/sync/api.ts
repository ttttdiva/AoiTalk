import { fetchApi } from '../lib/api-client';

export type SyncAction = 'create' | 'update' | 'delete';
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
  | 'scenarios'
  | 'scenario_characters'
  | 'scenario_scenes'
  | 'scenario_episodes';

export interface SyncTablePayload<T = Record<string, unknown>> {
  changes: T[];
  tombstones: Array<{ id: string; deleted_at?: string | null }>;
  cursor?: string | null;
  authoritative_ids?: string[];
}

export interface SyncPullResponse {
  tables: Partial<Record<SyncTable, SyncTablePayload>>;
  server_time: string;
  has_more: boolean;
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
}): Promise<SyncPullResponse> {
  const search = new URLSearchParams();
  if (params.since) search.set('since', params.since);
  if (params.tables?.length) search.set('tables', params.tables.join(','));
  const suffix = search.toString() ? `?${search.toString()}` : '';
  return fetchApi<SyncPullResponse>(`/api/sync/pull${suffix}`);
}

export async function pushSync(operations: SyncPushOperation[]): Promise<{
  results: SyncPushResult[];
}> {
  return fetchApi<{ results: SyncPushResult[] }>('/api/sync/push', {
    method: 'POST',
    body: JSON.stringify({ operations }),
  });
}
