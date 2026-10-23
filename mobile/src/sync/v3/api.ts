import { fetchApi, isApiHttpError } from "../../lib/api-client";
import { beginSyncV3Request } from "../../sync-v3/request-observer";
import {
  SYNC_V3_BOOTSTRAP_PAGE_LIMIT,
  SYNC_V3_CHANGES_PAGE_LIMIT,
  SYNC_V3_PROTOCOL,
  SyncV3CursorExpiredError,
  SyncV3DisabledError,
  type SyncV3BootstrapResponse,
  type SyncV3CapabilitiesResponse,
  type SyncV3ChangesResponse,
  type SyncV3OpsRequest,
  type SyncV3OpsResponse,
  type SyncV3ScopesResponse,
} from "./types";

const SYNC_V3_READ_TIMEOUT = 60_000;
const BASE = "/api/sync/v3";

function errorCode(error: unknown): string | null {
  if (!isApiHttpError(error)) return null;
  try {
    const parsed = JSON.parse(error.responseBody) as { code?: unknown };
    return typeof parsed?.code === "string" ? parsed.code : null;
  } catch {
    return null;
  }
}

/**
 * Translate the two protocol-level statuses into typed errors. Every other
 * HTTP error (400/404/413/5xx) and transport error is re-thrown unchanged.
 */
function translate(error: unknown): never {
  if (isApiHttpError(error)) {
    if (error.status === 503 && errorCode(error) === "sync_disabled") {
      throw new SyncV3DisabledError();
    }
    if (error.status === 410 && errorCode(error) === "cursor_expired") {
      throw new SyncV3CursorExpiredError();
    }
  }
  throw error;
}

export type SyncV3Mode = "v3" | "v2";
export type SyncV3RequestIdObserver = (requestId: string) => void;

async function tracked<T>(
  authScope: string,
  lane: "push" | "changes" | "bootstrap" | "scopes" | "capabilities",
  path: string,
  options: RequestInit,
  timeout?: number,
  onRequestId?: SyncV3RequestIdObserver,
  validate?: (value: T) => void,
): Promise<T> {
  const request = beginSyncV3Request(authScope, lane);
  onRequestId?.(request.requestId);
  try {
    const value = await fetchApi<T>(path, options, timeout, request.lifecycle);
    validate?.(value);
    await request.finish("ok");
    return value;
  } catch (error) {
    await request.fail(error);
    throw error;
  }
}

/**
 * Decide the protocol for one run. A server without v3 (404), a disabled
 * switch (enabled=false) and an unavailable v3 (503) select v2. Transport
 * failures and other errors are thrown to the caller.
 */
export async function getSyncV3Mode(signal?: AbortSignal, authScope = "auth:unknown"): Promise<SyncV3Mode> {
  let response: SyncV3CapabilitiesResponse;
  try {
    response = await tracked<SyncV3CapabilitiesResponse>(
      authScope,
      "capabilities",
      `${BASE}/capabilities`,
      { method: "GET", signal },
      SYNC_V3_READ_TIMEOUT,
      undefined,
      (value) => {
        if (!value || !Number.isInteger(value.protocol) || typeof value.enabled !== "boolean") {
          throw new TypeError("sync_v3_capabilities_invalid");
        }
      },
    );
  } catch (error) {
    if (isApiHttpError(error) && (error.status === 404 || error.status === 503)) return "v2";
    throw error;
  }
  return response && response.enabled === true && response.protocol === SYNC_V3_PROTOCOL
    ? "v3"
    : "v2";
}

export async function getSyncV3Scopes(
  signal?: AbortSignal,
  authScope = "auth:unknown",
  onRequestId?: SyncV3RequestIdObserver,
): Promise<SyncV3ScopesResponse> {
  try {
    const response = await tracked<SyncV3ScopesResponse>(
      authScope,
      "scopes",
      `${BASE}/scopes`,
      { method: "GET", signal },
      SYNC_V3_READ_TIMEOUT,
      onRequestId,
      (value) => {
        if (!value || !Array.isArray(value.scopes) || typeof value.scope_set_revision !== "string") {
          throw new TypeError("sync_v3_scopes_invalid");
        }
      },
    );
    return response;
  } catch (error) {
    return translate(error);
  }
}

export async function getSyncV3Changes(
  cursor: string,
  signal?: AbortSignal,
  limit: number = SYNC_V3_CHANGES_PAGE_LIMIT,
  authScope = "auth:unknown",
  onRequestId?: SyncV3RequestIdObserver,
): Promise<SyncV3ChangesResponse> {
  const search = new URLSearchParams({ cursor, limit: String(limit) });
  try {
    const response = await tracked<SyncV3ChangesResponse>(
      authScope,
      "changes",
      `${BASE}/changes?${search.toString()}`,
      { method: "GET", signal },
      SYNC_V3_READ_TIMEOUT,
      onRequestId,
      (value) => {
        if (!value || !Array.isArray(value.changes) || typeof value.cursor !== "string") {
          throw new TypeError("sync_v3_changes_invalid");
        }
      },
    );
    return response;
  } catch (error) {
    return translate(error);
  }
}

export async function getSyncV3Bootstrap(
  params: { scopeKey: string; table: string; after: string | null; limit?: number },
  signal?: AbortSignal,
  authScope = "auth:unknown",
  onRequestId?: SyncV3RequestIdObserver,
): Promise<SyncV3BootstrapResponse> {
  const search = new URLSearchParams({
    scope_key: params.scopeKey,
    table: params.table,
    after: params.after ?? "",
    limit: String(params.limit ?? SYNC_V3_BOOTSTRAP_PAGE_LIMIT),
  });
  try {
    const response = await tracked<SyncV3BootstrapResponse>(
      authScope,
      "bootstrap",
      `${BASE}/bootstrap?${search.toString()}`,
      { method: "GET", signal },
      SYNC_V3_READ_TIMEOUT,
      onRequestId,
      (value) => {
        if (!value || !Array.isArray(value.rows)) throw new TypeError("sync_v3_bootstrap_invalid");
      },
    );
    return response;
  } catch (error) {
    return translate(error);
  }
}

export async function postSyncV3Ops(
  body: SyncV3OpsRequest,
  signal?: AbortSignal,
  authScope = "auth:unknown",
  onRequestId?: SyncV3RequestIdObserver,
): Promise<SyncV3OpsResponse> {
  try {
    const response = await tracked<SyncV3OpsResponse>(authScope, "push", `${BASE}/ops`, {
        method: "POST",
        body: JSON.stringify(body),
        signal,
      }, undefined, onRequestId, (value) => {
        if (!value || !Array.isArray(value.results) || value.results.length !== body.ops.length
          || value.results.some((item) => !item || typeof item.op_id !== "string"
            || !["applied", "applied_partial", "rejected", "retry"].includes(item.status))) {
          throw new TypeError("sync_v3_ops_invalid");
        }
        const ids = new Set(value.results.map((item) => item.op_id));
        if (ids.size !== body.ops.length || body.ops.some((item) => !ids.has(item.op_id))) {
          throw new TypeError("sync_v3_ops_receipts_incomplete");
        }
      });
    return response;
  } catch (error) {
    // /ops answers 503 only for sync_disabled; treat any 503 as a v2 fallback.
    if (isApiHttpError(error) && error.status === 503) throw new SyncV3DisabledError();
    return translate(error);
  }
}
