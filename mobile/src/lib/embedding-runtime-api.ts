import { ApiHttpError, fetchApi } from "./api-client";
import type { components } from "../types/api-types.gen";

export type EmbeddingRuntimeStatus = components["schemas"]["EmbeddingRuntimeStatus"];
export type EmbeddingRuntimeView = components["schemas"]["EmbeddingRuntimeView"];
export type EmbeddingRuntimeReleaseResult = components["schemas"]["EmbeddingRuntimeReleaseResult"];

export const embeddingRuntimeApi = {
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
