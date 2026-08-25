/**
 * Docs online API クライアント（詳細設計書 2.10）。
 *
 * online 限定操作のみ（search / today）。ノード・タグ・フィールドの変更は
 * すべて outbox → /api/sync/push を経由する（docsRepo 参照）。Bearer は
 * api-client が自動付与する。
 */

import { fetchApi } from "./api-client";
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
