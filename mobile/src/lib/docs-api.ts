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
  applyRemoteDocsSupertags,
} from "../repositories/docs";
import type {
  DocsNode,
  DocsNodeSupertag,
  DocsSearchHit,
  DocsSupertag,
} from "../types/api";

export const docsApi = {
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
};
