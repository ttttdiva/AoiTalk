/**
 * Shared contracts for the WS4 Apps/Chat mobile cache.
 *
 * The cache is intentionally keyed by the token-derived auth scope.  A
 * project-scoped entry always carries a concrete `projectKey`; the
 * `__global__` sentinel is used for App data that is not attached to a
 * project.  Keeping this normalization in one place prevents `NULL` (or an
 * empty string) from becoming an accidental cross-project cache key.
 */

export const GLOBAL_APP_PROJECT_KEY = "__global__" as const;

export type AppProjectKey = string;

/** Return the canonical non-null key used by project-scoped App caches. */
export function normalizeAppProjectKey(
  projectId?: string | null,
): AppProjectKey {
  const normalized = typeof projectId === "string" ? projectId.trim() : "";
  return normalized || GLOBAL_APP_PROJECT_KEY;
}

export type AppContextCacheSnapshot = {
  authScope: string;
  appId: string;
  projectKey: AppProjectKey;
  targetKey?: string | null;
  context: Record<string, unknown> | null;
  etag?: string | null;
  cachedAt?: string | null;
};

export type ConversationContextSnapshot = {
  authScope: string;
  sessionId: string;
  projectId?: string | null;
  appId?: string | null;
  appTargetId?: string | null;
  status: "available" | "unavailable";
  snapshot: Record<string, unknown> | null;
  messageId?: string | null;
  cachedAt?: string | null;
};

