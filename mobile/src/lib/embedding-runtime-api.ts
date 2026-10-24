import { ApiHttpError, fetchApi } from "./api-client";
import type { components } from "../types/api-types.gen";
import {
  EMBEDDING_ENABLED_SETTING_KEY,
  isSavedEmbeddingEnabled,
  readEmbeddingEnabled,
} from "./embedding-master-setting";

export type EmbeddingRuntimeStatus = components["schemas"]["EmbeddingRuntimeStatus"];
export type EmbeddingRuntimeView = components["schemas"]["EmbeddingRuntimeView"];
export type EmbeddingRuntimeReleaseResult = components["schemas"]["EmbeddingRuntimeReleaseResult"];

/**
 * Embedding マスタースイッチ（settings.search.embedding_enabled）の取得・保存。
 * 応答が不正・未確認の場合は成功を偽装せず Error を投げる。HTTP/通信/中止のエラーは fetchApi のものをそのまま伝える。
 */
export const embeddingRuntimeApi = {
  /** 認証済み GET /api/settings。キー欠落のみ true、不正な値・構造は Error。 */
  async getEnabled(signal?: AbortSignal): Promise<boolean> {
    const body = await fetchApi<unknown>("/api/settings", { signal }, 5000);
    const read = readEmbeddingEnabled(body);
    if (!read.ok) throw new Error("Embedding の有効設定を確認できません（応答が不正です）");
    return read.enabled;
  },
  /** PATCH /api/settings を persist:true で送り、永続化が確認できた値だけを返す。不一致・不正応答は Error。 */
  async setEnabled(enabled: boolean, signal?: AbortSignal): Promise<boolean> {
    const body = await fetchApi<unknown>("/api/settings", {
      method: "PATCH",
      body: JSON.stringify({ key: EMBEDDING_ENABLED_SETTING_KEY, value: enabled, persist: true }),
      signal,
    }, 10000);
    if (!isSavedEmbeddingEnabled(body, enabled)) {
      throw new Error("Embedding の有効設定の保存を確認できません（応答が要求と一致しません）");
    }
    return enabled;
  },
  status(signal?: AbortSignal) {
    return fetchApi<EmbeddingRuntimeView>("/api/runtime/embedding", { signal }, 5000);
  },
  async release(signal?: AbortSignal): Promise<EmbeddingRuntimeReleaseResult> {
    try {
      return await fetchApi<EmbeddingRuntimeReleaseResult>("/api/runtime/embedding/release", {
        method: "POST", signal,
      }, 30000);
    } catch (cause) {
      if (cause instanceof ApiHttpError && [409, 503].includes(cause.status)) {
        try {
          const result = JSON.parse(cause.responseBody) as EmbeddingRuntimeReleaseResult;
          if (result.accepted === false && ["busy", "closed", "error"].includes(result.reason)) return result;
        } catch { /* Preserve the original HTTP failure for malformed responses. */ }
      }
      throw cause;
    }
  },
};
