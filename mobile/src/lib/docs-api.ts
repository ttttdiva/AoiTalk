/**
 * Docs online API クライアント（詳細設計書 2.10）。
 *
 * online 限定操作のみ（search / today）。ノード・タグ・フィールドの変更は
 * すべて outbox → /api/sync/push を経由する（docsRepo 参照）。Bearer は
 * api-client が自動付与する。
 */

import {
  fetchApi,
  fetchApiAtServerFingerprint,
} from "./api-client";
import {
  applyRemoteDocsNodeSupertags,
  applyRemoteDocsNodes,
  applyRemoteDocsEdges,
  applyRemoteDocsFieldValues,
  applyRemoteDocsFields,
  applyRemoteDocsPlacements,
  applyRemoteDocsSupertags,
  applyRemoteDocsSupertagFields,
} from "../repositories/docs";
import type {
  DocsEdge,
  DocsField,
  DocsFieldValue,
  DocsNode,
  DocsNodePlacement,
  DocsNodeSupertag,
  DocsSearchHit,
  DocsSupertag,
  DocsSupertagField,
  TaskReference,
} from "../types/api";

/** Canonical task projection used by the Docs task-binding affordance. */
export interface DocsTaskBinding {
  id: string;
  project_id: string | null;
  knowledge_node_id: string | null;
  title: string;
  status: string;
}

export interface DocsTreeResponse {
  library?: { id?: string; name?: string; owner_user_id?: string | null } | null;
  docs_library_id?: string | null;
  workspace?: { id?: string; name?: string; owner_user_id?: string | null } | null;
  nodes: DocsNode[];
  supertags: DocsSupertag[];
  node_supertags: DocsNodeSupertag[];
  supertag_fields: DocsSupertagField[];
  fields: DocsField[];
  field_values: DocsFieldValue[];
  placements: DocsNodePlacement[];
  edges: DocsEdge[];
}

export interface DocsNodeResponse {
  node: DocsNode;
  nodes?: DocsNode[];
  supertags?: DocsSupertag[];
  node_supertags?: DocsNodeSupertag[];
  supertag_fields?: DocsSupertagField[];
  fields?: DocsField[];
  field_values?: DocsFieldValue[];
  placements?: DocsNodePlacement[];
  edges?: DocsEdge[];
}

function normalizeTaskBinding(value: unknown): DocsTaskBinding | null {
  if (!value || typeof value !== "object") return null;
  const row = value as Record<string, unknown>;
  if (typeof row.id !== "string" || typeof row.title !== "string") return null;
  return {
    id: row.id,
    project_id: typeof row.project_id === "string" ? row.project_id : null,
    knowledge_node_id:
      typeof row.knowledge_node_id === "string" ? row.knowledge_node_id : null,
    title: row.title,
    status: typeof row.status === "string" ? row.status : "todo",
  };
}

async function applyDocsSnapshot(response: DocsTreeResponse | DocsNodeResponse) {
  const singleNode = "node" in response ? response.node : undefined;
  const nodes = response.nodes?.length ? response.nodes : singleNode ? [singleNode] : [];
  if (nodes.length) await applyRemoteDocsNodes(nodes);
  if (response.supertags?.length) await applyRemoteDocsSupertags(response.supertags);
  if (response.node_supertags?.length) {
    await applyRemoteDocsNodeSupertags(response.node_supertags);
  }
  if (response.supertag_fields?.length) {
    await applyRemoteDocsSupertagFields(response.supertag_fields);
  }
  if (response.fields?.length) await applyRemoteDocsFields(response.fields);
  if (response.field_values?.length) {
    await applyRemoteDocsFieldValues(response.field_values);
  }
  if (response.placements?.length) {
    await applyRemoteDocsPlacements(response.placements);
  }
  if (response.edges?.length) await applyRemoteDocsEdges(response.edges);
}

/** `POST /api/docs/ingest` の実行結果。ローカル実行時も同じ形へ揃える。 */
export interface ClipIngestResult {
  target_id: string;
  target_label: string;
  action: "create" | "append" | "duplicate_skip";
  changed_node_id: string | null;
  changed_node_title: string | null;
  open_node_id: string;
  open_node_title: string;
  direct_urls: string[];
  supplemental_urls: string[];
  failed_urls: Array<{ url?: string; error?: string; acquisition_status?: string }>;
  used_urls: string[];
  unconfirmed: string[];
  /** 添付の保存結果。添付なしの旧server応答では省略される。 */
  attachments?: Array<Record<string, unknown>>;
}

/** Durable server-side ClipIngest request. */
export interface ClipIngestJobRequest {
  source: string;
  upload_ids?: string[];
  skip_image_recognition?: boolean;
  enable_external_research?: boolean;
  target_node_id?: string | null;
}

/** Scope context persisted with a mobile operation and replayed as headers. */
export interface ClipIngestRequestContext {
  session_id?: string | null;
  project_id?: string | null;
}

export type ClipIngestJobStatus = "queued" | "running" | "succeeded" | "failed";

export interface ClipIngestJobResponse {
  job_id: string;
  status: ClipIngestJobStatus;
  idempotency_key: string | null;
  retryable: boolean;
  result: ClipIngestResult | null;
  error: Record<string, unknown>;
  dismissed_at: string | null;
  raw: Record<string, unknown>;
}

export class ClipIngestMalformedAckError extends Error {
  readonly payload: unknown;

  constructor(payload: unknown) {
    super("ClipIngest job response is malformed");
    this.name = "ClipIngestMalformedAckError";
    this.payload = payload;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function stringArray(value: unknown): string[] | null {
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string")) {
    return null;
  }
  return value as string[];
}

/** Validate and normalize the result embedded in a durable job ACK. */
export function normalizeClipIngestResult(
  value: unknown,
): ClipIngestResult | null {
  if (!isRecord(value)) return null;
  if (
    typeof value.target_id !== "string"
    || typeof value.target_label !== "string"
    || typeof value.open_node_id !== "string"
    || typeof value.open_node_title !== "string"
    || !["create", "append", "duplicate_skip"].includes(String(value.action))
  ) {
    return null;
  }
  if (
    value.changed_node_id !== null
    && value.changed_node_id !== undefined
    && typeof value.changed_node_id !== "string"
  ) {
    return null;
  }
  if (
    value.changed_node_title !== null
    && value.changed_node_title !== undefined
    && typeof value.changed_node_title !== "string"
  ) {
    return null;
  }
  const directUrls = stringArray(value.direct_urls);
  const supplementalUrls = stringArray(value.supplemental_urls);
  const usedUrls = stringArray(value.used_urls);
  const unconfirmed = stringArray(value.unconfirmed);
  if (!directUrls || !supplementalUrls || !usedUrls || !unconfirmed) return null;
  if (!Array.isArray(value.failed_urls)) return null;
  const failedUrls = value.failed_urls.filter(isRecord) as Array<{
    url?: string;
    error?: string;
    acquisition_status?: string;
  }>;
  if (failedUrls.length !== value.failed_urls.length) return null;

  return {
    target_id: value.target_id,
    target_label: value.target_label,
    action: value.action as ClipIngestResult["action"],
    changed_node_id:
      typeof value.changed_node_id === "string" ? value.changed_node_id : null,
    changed_node_title:
      typeof value.changed_node_title === "string"
        ? value.changed_node_title
        : null,
    open_node_id: value.open_node_id,
    open_node_title: value.open_node_title,
    direct_urls: directUrls,
    supplemental_urls: supplementalUrls,
    failed_urls: failedUrls,
    used_urls: usedUrls,
    unconfirmed,
    ...(Array.isArray(value.attachments)
      ? {
          attachments: value.attachments.filter(isRecord) as Array<
            Record<string, unknown>
          >,
        }
      : {}),
  };
}

function normalizeClipIngestJob(value: unknown): ClipIngestJobResponse {
  if (!isRecord(value)) throw new ClipIngestMalformedAckError(value);
  const status = value.status;
  if (
    typeof value.job_id !== "string"
    || !value.job_id
    || typeof status !== "string"
    || !["queued", "running", "succeeded", "failed"].includes(status)
  ) {
    throw new ClipIngestMalformedAckError(value);
  }
  const rawResult = value.result ?? value.result_json;
  const result = normalizeClipIngestResult(rawResult);
  if (status === "succeeded" && result === null) {
    throw new ClipIngestMalformedAckError(value);
  }
  const rawError = value.error ?? value.error_json;
  return {
    job_id: value.job_id,
    status: status as ClipIngestJobStatus,
    idempotency_key:
      typeof value.idempotency_key === "string" ? value.idempotency_key : null,
    retryable: value.retryable !== false,
    result,
    error: isRecord(rawError) ? rawError : {},
    dismissed_at:
      typeof value.dismissed_at === "string" ? value.dismissed_at : null,
    raw: value,
  };
}

function clipIngestContextHeaders(
  context?: ClipIngestRequestContext,
): Record<string, string> {
  const headers: Record<string, string> = {};
  if (context?.session_id) headers["X-Session-ID"] = context.session_id;
  if (context?.project_id) headers["X-Project-ID"] = context.project_id;
  return headers;
}

export const docsApi = {
  /**
   * GET /api/docs/tree — canonical online snapshot for one Docs library.
   *
   * The normal screen path uses staged sync for large snapshots.  This method
   * is a read-only escape hatch: callers may inspect the canonical response,
   * but must not apply it directly because doing so would bypass the sync
   * engine's bounded staging and atomic promotion contract.
   */
  async tree(opts?: {
    since?: string;
    includeArchived?: boolean;
    project?: string;
  }): Promise<DocsTreeResponse> {
    const params = new URLSearchParams();
    if (opts?.since) params.set("since", opts.since);
    if (opts?.includeArchived) params.set("include_archived", "1");
    if (opts?.project) params.set("project", opts.project);
    const suffix = params.toString() ? `?${params.toString()}` : "";
    const response = await fetchApi<DocsTreeResponse>(`/api/docs/tree${suffix}`);
    return response;
  },

  /** Durable ClipIngest enqueue. Results converge through the next Docs sync. */
  async enqueueIngestJob(
    request: ClipIngestJobRequest,
    operationKey: string,
    serverFingerprintOrContext?: string | ClipIngestRequestContext,
    contextArg?: ClipIngestRequestContext,
  ): Promise<ClipIngestJobResponse> {
    // The journal/replay path always supplies a fingerprint.  Keep the
    // historical `(request, key, context?)` shape for older callers while
    // they migrate; that compatibility path is never used for durable rows.
    const serverFingerprint =
      typeof serverFingerprintOrContext === "string"
        ? serverFingerprintOrContext
        : null;
    const context =
      typeof serverFingerprintOrContext === "string"
        ? contextArg
        : serverFingerprintOrContext;
    const options: RequestInit = {
      method: "POST",
      headers: {
        "Idempotency-Key": operationKey,
        ...clipIngestContextHeaders(context),
      },
      body: JSON.stringify(request),
    };
    const response = serverFingerprint
      ? await fetchApiAtServerFingerprint<unknown>(
          serverFingerprint,
          "/api/docs/ingest/jobs",
          options,
        )
      : await fetchApi<unknown>("/api/docs/ingest/jobs", options);
    return normalizeClipIngestJob(response);
  },

  /** Actor/ACL checked exact lookup. A 404 is intentionally propagated. */
  async findIngestJobByIdempotencyKey(
    operationKey: string,
    serverFingerprint?: string,
  ): Promise<ClipIngestJobResponse> {
    const path =
      `/api/docs/ingest/jobs/by-idempotency-key/${encodeURIComponent(operationKey)}`;
    const response = serverFingerprint
      ? await fetchApiAtServerFingerprint<unknown>(serverFingerprint, path)
      : await fetchApi<unknown>(path);
    return normalizeClipIngestJob(response);
  },

  async getIngestJob(
    jobId: string,
    serverFingerprint?: string,
  ): Promise<ClipIngestJobResponse> {
    const path = `/api/docs/ingest/jobs/${encodeURIComponent(jobId)}`;
    const response = serverFingerprint
      ? await fetchApiAtServerFingerprint<unknown>(serverFingerprint, path)
      : await fetchApi<unknown>(path);
    return normalizeClipIngestJob(response);
  },

  /** GET /api/docs/nodes/{node_id} — ACL checked canonical node detail. */
  async getNode(nodeId: string): Promise<DocsNodeResponse> {
    const response = await fetchApi<DocsNodeResponse>(
      `/api/docs/nodes/${encodeURIComponent(nodeId)}`,
    );
    await applyDocsSnapshot(response);
    return response;
  },

  /** GET /api/docs/search — online 全文検索。offline は docsRepo.searchLocal を UI で使う。 */
  async search(
    q: string,
    opts?: { tag?: string; project?: string; limit?: number },
  ): Promise<DocsSearchHit[]> {
    const params = new URLSearchParams();
    if (q) params.set("q", q);
    if (opts?.tag) params.set("tag", opts.tag);
    if (opts?.project) params.set("project", opts.project);
    if (typeof opts?.limit === "number") {
      params.set("limit", String(opts.limit));
    }
    const suffix = params.toString() ? `?${params.toString()}` : "";
    const res = await fetchApi<{ results: DocsSearchHit[] }>(
      `/api/docs/search${suffix}`,
    );
    return res.results ?? [];
  },

  /**
   * GET /api/docs/today — サーバで Day ノードを ensure し、レスポンスを
   * ローカルへ反映してから返す。
   */
  async today(
    date?: string,
  ): Promise<{
    node: DocsNode;
    supertag: DocsSupertag;
    nodeSupertags: DocsNodeSupertag[];
  }> {
    const params = new URLSearchParams();
    if (date) params.set("date", date);
    const suffix = params.toString() ? `?${params.toString()}` : "";
    const res = await fetchApi<{
      node: DocsNode;
      supertag: DocsSupertag;
      node_supertags: DocsNodeSupertag[];
    }>(`/api/docs/today${suffix}`);
    await applyRemoteDocsNodes([res.node]);
    if (res.supertag) await applyRemoteDocsSupertags([res.supertag]);
    const nodeSupertags = res.node_supertags ?? [];
    if (nodeSupertags.length) {
      await applyRemoteDocsNodeSupertags(nodeSupertags);
    }
    return { node: res.node, supertag: res.supertag, nodeSupertags };
  },

  /**
   * POST /api/docs/ingest — サーバ側でURL取得・保存先判定・保存を一括実行する。
   * 長いURL取得と複数回のLLM判定を含むため専用timeoutを使う。
   * @deprecated Durable Clip UI uses enqueueIngestJob + exact lookup.
   */
  async ingest(source: string): Promise<{
    result: ClipIngestResult;
    node: DocsNode;
    nodes: DocsNode[];
    local_sync_warning?: string;
  }> {
    const response = await fetchApi<{
      result: ClipIngestResult;
      node: DocsNode;
      nodes: DocsNode[];
    }>(
      "/api/docs/ingest",
      {
        method: "POST",
        body: JSON.stringify({ source }),
      },
      // URL取得(最大25秒/件) + 最大8回の補足検索とその根拠判定 + ルーティング/統合の
      // LLM呼び出しが直列に走るため、180秒では取りこぼす。途中で打ち切ってもサーバー側の
      // 処理は続き、再実行は409（実行中）になるだけなので、待つ側を長めに取る。
      300_000,
    );
    try {
      await applyRemoteDocsNodes(
        response.nodes?.length ? response.nodes : [response.node],
      );
      return response;
    } catch {
      return {
        ...response,
        local_sync_warning:
          "サーバへの保存は完了しましたが、端末への反映に失敗しました。同期後に保存ノードを確認してください。",
      };
    }
  },

  /**
   * GET /api/tasks — server ACL-filtered task projections used by Docs task
   * binding.  The backend has no Docs-specific list endpoint; filter the
   * canonical `knowledge_node_id` projection locally after the authorized
   * response is received.  This remains online-only by contract.
   */
  async listTasksForBinding(query?: string): Promise<DocsTaskBinding[]> {
    const params = new URLSearchParams();
    if (query?.trim()) params.set("search", query.trim());
    const suffix = params.toString() ? `?${params.toString()}` : "";
    const response = await fetchApi<unknown>(`/api/tasks${suffix}`);
    const rows = Array.isArray(response)
      ? response
      : response && typeof response === "object" && Array.isArray((response as { tasks?: unknown[] }).tasks)
        ? (response as { tasks: unknown[] }).tasks
        : [];
    return rows
      .map(normalizeTaskBinding)
      .filter((task): task is DocsTaskBinding => task !== null);
  },

  /** PATCH /api/tasks/{task_id} with the canonical Docs binding pointer. */
  async bindTask(taskId: string, nodeId: string): Promise<DocsTaskBinding> {
    const response = await fetchApi<unknown>(
      `/api/tasks/${encodeURIComponent(taskId)}`,
      {
        method: "PATCH",
        body: JSON.stringify({ knowledge_node_id: nodeId }),
      },
    );
    const task = normalizeTaskBinding(response);
    if (!task) throw new Error("タスク連携の応答が不正です");
    return task;
  },

  /** PATCH /api/tasks/{task_id} to clear the canonical Docs binding pointer. */
  async unbindTask(taskId: string): Promise<void> {
    await fetchApi(`/api/tasks/${encodeURIComponent(taskId)}`, {
      method: "PATCH",
      body: JSON.stringify({ knowledge_node_id: null }),
    });
  },

  /** Read task references through the canonical FastAPI Task API. */
  async listTaskReferences(taskId: string): Promise<TaskReference[]> {
    return fetchApi<TaskReference[]>(
      `/api/tasks/${encodeURIComponent(taskId)}/references`,
    );
  },
};
