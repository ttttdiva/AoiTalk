import { fetchPythonApi, type InternalPythonUser } from "@/lib/server/python-api-proxy";
import type { components } from "@/lib/api-types.gen";

export const CONVERSATION_HISTORY_STATUS_PATH =
  "/api/conversations/history-status";
// The canonical batch request intentionally caps each request.  The Web
// history query has no page limit, so the bridge sends bounded chunks while
// still avoiding one API call per session.
export const CONVERSATION_HISTORY_STATUS_BATCH_SIZE = 200;

export const CONVERSATION_HISTORY_STATUS_VALUES = [
  "idle",
  "ready",
  "needs_attention",
  "working",
] as const;

export type ConversationHistoryStatus =
  components["schemas"]["ConversationHistoryStatusItem"]["history_status"];

export type ConversationHistoryStatusProjection =
  components["schemas"]["ConversationHistoryStatusItem"];

/**
 * Errors raised while the BFF is bridging the shared Python projection.
 *
 * A missing/invalid projection is a contract failure. Callers must surface it
 * as an error instead of manufacturing an `idle` result, because `idle` is a
 * real state and would hide a stale or unavailable server response.
 */
export class ConversationHistoryStatusBridgeError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = "ConversationHistoryStatusBridgeError";
  }
}

function isConversationHistoryStatus(
  value: unknown,
): value is ConversationHistoryStatus {
  return (
    typeof value === "string" &&
    (CONVERSATION_HISTORY_STATUS_VALUES as readonly string[]).includes(value)
  );
}

function normalizeSessionIds(sessionIds: readonly string[]): string[] {
  const normalized: string[] = [];
  const seen = new Set<string>();
  for (const sessionId of sessionIds) {
    if (typeof sessionId !== "string") continue;
    const value = sessionId.trim();
    if (!value || seen.has(value)) continue;
    seen.add(value);
    normalized.push(value);
  }
  return normalized;
}

function upstreamStatus(status: number): number {
  // Preserve an auth/ACL decision made by Python. Other upstream failures are
  // bridge failures from the Web route's perspective.
  if (status === 401 || status === 403) return status;
  return status >= 400 && status < 500 ? status : 502;
}

async function readDetail(response: Response): Promise<string | null> {
  const payload = (await response.json().catch(() => null)) as {
    detail?: unknown;
  } | null;
  if (typeof payload?.detail === "string" && payload.detail.trim()) {
    return payload.detail.trim();
  }
  return null;
}

/**
 * Fetch one bounded status projection for all already-visible Web sessions.
 *
 * The Web route owns its existing Drizzle visibility/Project/App checks. This
 * bridge receives only those IDs; Python applies its own read scope and the
 * response is therefore the intersection of both authorities.
 */
export async function fetchConversationHistoryStatuses(
  sessionIds: readonly string[],
  user: InternalPythonUser,
): Promise<Map<string, ConversationHistoryStatusProjection>> {
  const normalizedIds = normalizeSessionIds(sessionIds);
  if (normalizedIds.length === 0) return new Map();

  if (normalizedIds.length > CONVERSATION_HISTORY_STATUS_BATCH_SIZE) {
    const combined = new Map<string, ConversationHistoryStatusProjection>();
    for (
      let offset = 0;
      offset < normalizedIds.length;
      offset += CONVERSATION_HISTORY_STATUS_BATCH_SIZE
    ) {
      const chunk = normalizedIds.slice(
        offset,
        offset + CONVERSATION_HISTORY_STATUS_BATCH_SIZE,
      );
      const chunkProjection = await fetchConversationHistoryStatuses(chunk, user);
      for (const [sessionId, projection] of chunkProjection) {
        combined.set(sessionId, projection);
      }
    }
    return combined;
  }

  let response: Response;
  try {
    const requestBody: components["schemas"]["ConversationHistoryStatusRequest"] = {
      session_ids: normalizedIds,
    };
    response = await fetchPythonApi(CONVERSATION_HISTORY_STATUS_PATH, {
      method: "POST",
      user,
      headers: { "content-type": "application/json" },
      body: JSON.stringify(requestBody),
    });
  } catch (error) {
    throw new ConversationHistoryStatusBridgeError(
      502,
      "history_status_unavailable",
      error instanceof Error
        ? error.message
        : "会話履歴の状態サービスに接続できません",
    );
  }

  if (!response.ok) {
    const detail = await readDetail(response);
    throw new ConversationHistoryStatusBridgeError(
      upstreamStatus(response.status),
      "history_status_upstream_error",
      detail || "会話履歴の状態を取得できませんでした",
    );
  }

  const payload = (await response.json().catch(() => null)) as {
    statuses?: unknown;
  } | null;
  if (!payload || !Array.isArray(payload.statuses)) {
    throw new ConversationHistoryStatusBridgeError(
      502,
      "history_status_invalid_response",
      "会話履歴の状態応答が不正です",
    );
  }

  const requested = new Set(normalizedIds);
  const projection = new Map<string, ConversationHistoryStatusProjection>();
  for (const raw of payload.statuses) {
    if (!raw || typeof raw !== "object") {
      throw new ConversationHistoryStatusBridgeError(
        502,
        "history_status_invalid_projection",
        "会話履歴の状態投影が不正です",
      );
    }
    const item = raw as Partial<ConversationHistoryStatusProjection>;
    const sessionId =
      typeof item.session_id === "string" ? item.session_id.trim() : "";
    if (!requested.has(sessionId) || projection.has(sessionId)) {
      throw new ConversationHistoryStatusBridgeError(
        502,
        "history_status_invalid_projection",
        "会話履歴の状態投影に不正なセッションIDがあります",
      );
    }
    if (!isConversationHistoryStatus(item.history_status)) {
      throw new ConversationHistoryStatusBridgeError(
        502,
        "history_status_unknown_value",
        "会話履歴の状態値が不明です",
      );
    }
    if (
      item.history_completed_at !== null &&
      item.history_completed_at !== undefined &&
      typeof item.history_completed_at !== "string"
    ) {
      throw new ConversationHistoryStatusBridgeError(
        502,
        "history_status_invalid_projection",
        "会話履歴の完了時刻が不正です",
      );
    }
    projection.set(sessionId, {
      session_id: sessionId,
      history_status: item.history_status,
      history_completed_at:
        typeof item.history_completed_at === "string"
          ? item.history_completed_at
          : null,
    });
  }

  const missing = normalizedIds.filter((sessionId) => !projection.has(sessionId));
  if (missing.length > 0) {
    throw new ConversationHistoryStatusBridgeError(
      502,
      "history_status_missing_projection",
      "会話履歴の状態投影が一部欠落しています",
    );
  }
  return projection;
}

export function isConversationHistoryStatusBridgeError(
  error: unknown,
): error is ConversationHistoryStatusBridgeError {
  return error instanceof ConversationHistoryStatusBridgeError;
}
