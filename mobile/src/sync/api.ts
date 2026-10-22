import {
  createApiRequestId,
  fetchApi,
  isApiConnectionError,
  isApiHttpError,
  isApiTimeoutError,
  type ApiRequestLifecycle,
} from '../lib/api-client';
import type { components } from '../types/api-types.gen';

const SYNC_PULL_TIMEOUT = 60_000;
const DOCS_DIAGNOSTIC_TIMEOUT = 15_000;
type DocsSyncDiagnosticWireResponse = components['schemas']['SyncDocsDiagnosticResponse'];

export type DocsSyncDiagnosticOutcome =
  | "running"
  | "success"
  | "http_error"
  | "timeout"
  | "connection_error"
  | "invalid_response"
  | "cancelled"
  | "request_error";

export type DocsSyncDiagnosticStage = "requesting" | "response" | "complete" | "error";

export type DocsSyncDiagnosticResponse = {
  protocolVersion: DocsSyncDiagnosticWireResponse['protocol_version'];
  docsSyncProtocolVersion: DocsSyncDiagnosticWireResponse['docs_sync_protocol_version'];
  readOnly: DocsSyncDiagnosticWireResponse['read_only'];
  serverPhase: DocsSyncDiagnosticWireResponse['server_phase'];
  personalLibraryExists: NonNullable<DocsSyncDiagnosticWireResponse['personal_library_exists']>;
  needsNormalization: NonNullable<DocsSyncDiagnosticWireResponse['needs_normalization']>;
  accessibleProjectCount: NonNullable<DocsSyncDiagnosticWireResponse['accessible_project_count']>;
  accessibleScopeCount: NonNullable<DocsSyncDiagnosticWireResponse['accessible_scope_count']>;
};

export type DocsSyncDiagnosticServerError = {
  protocolVersion: DocsSyncDiagnosticWireResponse['protocol_version'];
  docsSyncProtocolVersion: DocsSyncDiagnosticWireResponse['docs_sync_protocol_version'];
  readOnly: DocsSyncDiagnosticWireResponse['read_only'];
  serverPhase: DocsSyncDiagnosticWireResponse['server_phase'];
  errorCode: NonNullable<DocsSyncDiagnosticWireResponse['error_code']>;
  exceptionType: NonNullable<DocsSyncDiagnosticWireResponse['exception_type']>;
};

/** Safe, in-memory progress/result for the server-only Docs metadata probe. */
export type DocsSyncDiagnosticSnapshot = {
  requestId: string;
  attempts: number;
  httpStatus: number | null;
  stage: DocsSyncDiagnosticStage;
  outcome: DocsSyncDiagnosticOutcome;
  elapsedMs: number;
  response?: DocsSyncDiagnosticResponse;
  serverError?: DocsSyncDiagnosticServerError;
};

export type DocsSyncDiagnosticOptions = {
  signal?: AbortSignal;
  onProgress?: (snapshot: DocsSyncDiagnosticSnapshot) => void;
};

function parseDocsSyncDiagnosticResponse(value: unknown): DocsSyncDiagnosticResponse | null {
  if (typeof value !== "object" || value === null) return null;
  const payload = value as Record<string, unknown>;
  if (
    payload.protocol_version !== 1
    || payload.docs_sync_protocol_version !== 2
    || payload.read_only !== true
    || payload.server_phase !== "metadata_read"
    || payload.status !== "ok"
    || typeof payload.personal_library_exists !== "boolean"
    || typeof payload.needs_normalization !== "boolean"
    || !Number.isSafeInteger(payload.accessible_project_count)
    || (payload.accessible_project_count as number) < 0
    || !Number.isSafeInteger(payload.accessible_scope_count)
    || (payload.accessible_scope_count as number) < 0
  ) return null;
  return {
    protocolVersion: 1,
    docsSyncProtocolVersion: 2,
    readOnly: true,
    serverPhase: "metadata_read",
    personalLibraryExists: payload.personal_library_exists,
    needsNormalization: payload.needs_normalization,
    accessibleProjectCount: payload.accessible_project_count as number,
    accessibleScopeCount: payload.accessible_scope_count as number,
  };
}

function parseDocsSyncDiagnosticServerError(value: unknown): DocsSyncDiagnosticServerError | null {
  if (typeof value !== "string") return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(value);
  } catch {
    return null;
  }
  if (typeof parsed !== "object" || parsed === null) return null;
  const payload = parsed as Record<string, unknown>;
  if (
    payload.protocol_version !== 1
    || payload.docs_sync_protocol_version !== 2
    || payload.read_only !== true
    || payload.server_phase !== "metadata_read"
    || payload.status !== "error"
    || payload.error_code !== "diagnostic_query_failed"
    || typeof payload.exception_type !== "string"
    || payload.exception_type.length > 128
    || !/^[A-Za-z_][A-Za-z0-9_.]{0,127}(?![\s\S])/.test(payload.exception_type)
  ) return null;
  return {
    protocolVersion: 1,
    docsSyncProtocolVersion: 2,
    readOnly: true,
    serverPhase: "metadata_read",
    errorCode: "diagnostic_query_failed",
    exceptionType: payload.exception_type,
  };
}

/**
 * Read only bounded Docs sync metadata. Raw error bodies and messages are
 * discarded; only a fixed server code and validated exception type may pass.
 */
export async function runDocsSyncDiagnostic(
  options: DocsSyncDiagnosticOptions = {},
): Promise<DocsSyncDiagnosticSnapshot> {
  const requestId = createApiRequestId("docs-diagnostic");
  const startedAt = Date.now();
  let attempts = 0;
  let httpStatus: number | null = null;
  const elapsedMs = () => Math.max(0, Date.now() - startedAt);
  const publish = (
    stage: DocsSyncDiagnosticStage,
    outcome: DocsSyncDiagnosticOutcome,
    response?: DocsSyncDiagnosticResponse,
    serverError?: DocsSyncDiagnosticServerError,
  ): DocsSyncDiagnosticSnapshot => {
    const snapshot: DocsSyncDiagnosticSnapshot = {
      requestId,
      attempts,
      httpStatus,
      stage,
      outcome,
      elapsedMs: elapsedMs(),
      ...(response ? { response } : {}),
      ...(serverError ? { serverError } : {}),
    };
    try {
      options.onProgress?.(snapshot);
    } catch {
      // A diagnostics observer must not change request behavior.
    }
    return snapshot;
  };

  publish("requesting", "running");
  const lifecycle: ApiRequestLifecycle = {
    requestId,
    onAttempt: () => {
      attempts += 1;
      httpStatus = null;
      publish("requesting", "running");
    },
    onResponse: (event) => {
      httpStatus = Number.isInteger(event.status) && event.status >= 100 && event.status <= 599
        ? event.status
        : null;
      publish("response", "running");
    },
  };

  try {
    const value = await fetchApi<unknown>(
      "/api/sync/docs-diagnostic",
      { method: "GET", signal: options.signal },
      DOCS_DIAGNOSTIC_TIMEOUT,
      lifecycle,
    );
    const response = parseDocsSyncDiagnosticResponse(value);
    if (!response) return publish("error", "invalid_response");
    return publish("complete", "success", response);
  } catch (error) {
    if (isApiTimeoutError(error)) {
      return publish("error", "timeout");
    }
    if (options.signal?.aborted) {
      return publish("error", "cancelled");
    }
    if (isApiHttpError(error)) {
      const status = error.status;
      if (typeof status === "number" && Number.isInteger(status) && status >= 100 && status <= 599) {
        httpStatus = status;
      }
      const serverError = httpStatus === 503
        ? parseDocsSyncDiagnosticServerError(error.responseBody)
        : null;
      return publish("error", "http_error", undefined, serverError ?? undefined);
    }
    if (isApiConnectionError(error)) {
      return publish("error", "connection_error");
    }
    return publish("error", "request_error");
  }
}

export type SyncPullRequestOutcome =
  | "success"
  | "http_error"
  | "transport_error"
  | "cancelled"
  | "parse_error";

export type SyncPullLifecycle = {
  onAttempt?: (event: { requestId: string }) => void;
  onResponse?: (event: { requestId: string; status: number }) => void;
  onFinish?: (event: {
    requestId: string;
    startedAt: number;
    finishedAt: number;
    status: number | null;
    outcome: SyncPullRequestOutcome;
  }) => void;
};

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
  /** Receipt of all delivered pages, valid only at the terminal general page. */
  general_digest?: string;
  general_not_modified?: boolean;
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
  /** Additive bounded pagination for non-Docs tables. */
  general_pagination_version?: number;
  general_snapshot_token?: string;
  /** Durable incremental watermark. General v1 currently returns null until
   * the server has a commit-ordered change boundary. Content receipts may
   * elide an unchanged full projection; they never advance this timestamp. */
  general_watermark?: string | null;
  general_has_more?: boolean;
  general_digest_version?: number;
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
  /** Structured server classification. Unknown/omitted values fail closed. */
  reason_code?: string;
}

export async function pullSync(params: {
  since?: string | null;
  tables?: SyncTable[];
  general_pagination?: boolean;
  general_digest_version?: number;
  general_digests?: Record<string, string>;
  general_cursors?: Record<string, string>;
  general_snapshot_token?: string;
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
  /** Caller-owned lifecycle signal; aborting it must not mark the API offline. */
  signal?: AbortSignal;
  /** Logical request ID reused across auth retries for access-log correlation. */
  requestId?: string;
  /** Non-secret observer for bounded app diagnostics. */
  lifecycle?: SyncPullLifecycle;
}): Promise<SyncPullResponse> {
  const requestId = params.requestId ?? createApiRequestId("sync-pull");
  const startedAt = Date.now();
  let status: number | null = null;
  const finish = (outcome: SyncPullRequestOutcome, error?: unknown): void => {
    try {
      params.lifecycle?.onFinish?.({
        requestId,
        startedAt,
        finishedAt: Date.now(),
        status: status ?? (isApiHttpError(error) ? error.status : null),
        outcome,
      });
    } catch {
      // Diagnostics are observers and must not alter request behavior.
    }
  };
  const lifecycle: ApiRequestLifecycle = {
    requestId,
    onAttempt: (event) => {
      status = null;
      try {
        params.lifecycle?.onAttempt?.(event);
      } catch {
        // Diagnostics are observers and must not alter request behavior.
      }
    },
    onResponse: (event) => {
      status = event.status;
      try {
        params.lifecycle?.onResponse?.(event);
      } catch {
        // Diagnostics are observers and must not alter request behavior.
      }
    },
  };
  const search = new URLSearchParams();
  if (params.since) search.set('since', params.since);
  if (params.tables?.length) search.set('tables', params.tables.join(','));
  if (params.general_pagination) search.set('general_pagination', '1');
  if (params.general_digest_version) search.set('general_digest_version', String(params.general_digest_version));
  if (params.general_digests && Object.keys(params.general_digests).length) {
    search.set('general_digests', JSON.stringify(params.general_digests));
  }
  const generalCursors = params.general_cursors;
  if (generalCursors && Object.keys(generalCursors).length > 0) {
    search.set('general_cursors', JSON.stringify(generalCursors));
  }
  if (params.general_snapshot_token) {
    search.set('general_snapshot_token', params.general_snapshot_token);
  }
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
  try {
    const response = await fetchApi<SyncPullResponse>(
      `/api/sync/pull${suffix}`,
      { signal: params.signal },
      SYNC_PULL_TIMEOUT,
      lifecycle,
    );
    finish("success");
    return response;
  } catch (error) {
    const outcome: SyncPullRequestOutcome = isApiHttpError(error)
      ? "http_error"
      : error instanceof Error && error.name === "AbortError"
        ? "cancelled"
        : isApiConnectionError(error) || isApiTimeoutError(error)
          ? "transport_error"
          : "parse_error";
    finish(outcome, error);
    throw error;
  }
}

export async function pushSync(
  operations: SyncPushOperation[],
  signal?: AbortSignal,
): Promise<{
  results: SyncPushResult[];
}> {
  return fetchApi<{ results: SyncPushResult[] }>('/api/sync/push', {
    method: 'POST',
    body: JSON.stringify({ operations }),
    signal,
  });
}
