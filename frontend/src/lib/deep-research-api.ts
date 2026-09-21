import type { StructuredApiError } from "@/lib/chat-api";

export type DeepResearchStatus =
  | "queued"
  | "running"
  | "completed"
  | "failed"
  | "cancelled"
  | "interrupted";

export type DeepResearchErrorCode =
  | "scope_missing"
  | "scope_revoked"
  | "privacy_protection_failed"
  | "queue_full"
  | "planning_timeout"
  | "engine_timeout"
  | "synthesis_timeout"
  | "deadline"
  | "process_restarted"
  | "cancelled"
  | "provider_failed"
  | "provider_invalid"
  | "egress_unreachable"
  | "credential_missing"
  | "internal_error"
  | (string & {});

export type DeepResearchEvent = {
  timestamp: string;
  message: string;
  progress: number;
  phase: string;
  metadata?: Record<string, unknown>;
};

export type DeepResearchSource = {
  id: number;
  title: string;
  url: string;
  snippet: string;
  engine: string;
  query: string;
  published_at?: string | null;
};

export type DeepResearchJob = {
  id: string;
  user_id: string;
  session_id?: string | null;
  actor_user_id?: string | null;
  query: string;
  status: DeepResearchStatus;
  interrupted?: boolean;
  error_code?: DeepResearchErrorCode | null;
  progress: number;
  mode: "quick" | "detailed" | "report" | string;
  created_at: string;
  updated_at: string;
  started_at?: string | null;
  completed_at?: string | null;
  error?: string | null;
  events: DeepResearchEvent[];
  questions_by_iteration: Record<string, string[]>;
  sources: DeepResearchSource[];
  report_markdown: string;
  metadata: Record<string, unknown>;
};

export type DeepResearchEngine = {
  id: string;
  label: string;
  /** Legacy alias for configured/readiness projections. */
  available?: boolean;
  configured?: boolean;
  reachability?: "ready" | "unreachable" | "unknown";
  reason?: string | null;
  checked_at?: string | null;
};

export type StartDeepResearchRequest = {
  query: string;
  mode: "quick" | "detailed" | "report";
  max_iterations: number;
  questions_per_iteration: number;
  max_results_per_query: number;
  engines: string[];
  include_local_knowledge: boolean;
  /** Conversation scope is validated by the server before execution. */
  session_id?: string | null;
  project_id?: string | null;
};

const RETRYABLE_STATUSES = new Set([408, 425, 429, 500, 502, 503, 504]);

function fallbackErrorMessage(status: number, statusText: string): string {
  if (status === 401) return "認証が必要です";
  if (status === 403) return "この調査を実行する権限がありません";
  if (status === 404) return "調査ジョブが見つかりません";
  if (status === 409) return "調査要求が競合したため完了できませんでした";
  if (status >= 500) return "Deep Researchサーバーで要求を処理できませんでした";
  return statusText || "Deep Research要求を処理できませんでした";
}

function parseErrorPayload(
  raw: string,
  status: number,
  statusText: string,
  responseRequestId: string | null,
): StructuredApiError {
  let payload: unknown;
  try {
    payload = raw ? JSON.parse(raw) : null;
  } catch {
    payload = null;
  }

  let candidate: unknown =
    payload && typeof payload === "object"
      ? (payload as { error?: unknown }).error
      : null;
  if (!candidate && payload && typeof payload === "object") {
    const detail = (payload as { detail?: unknown }).detail;
    candidate =
      detail && typeof detail === "object"
        ? (detail as { error?: unknown }).error ?? detail
        : null;
  }
  if (candidate && typeof candidate === "object") {
    const value = candidate as Record<string, unknown>;
    const code =
      typeof value.code === "string" && value.code.trim()
        ? value.code.trim()
        : "deep_research_error";
    const message =
      typeof value.message === "string" && value.message.trim()
        ? value.message.trim()
        : fallbackErrorMessage(status, statusText);
    const requestId =
      typeof value.request_id === "string" && value.request_id.trim()
        ? value.request_id.trim()
        : responseRequestId;
    return {
      code,
      message,
      retryable:
        typeof value.retryable === "boolean"
          ? value.retryable
          : RETRYABLE_STATUSES.has(status),
      request_id: requestId,
    };
  }

  const detail =
    payload && typeof payload === "object"
      ? (payload as { detail?: unknown }).detail
      : null;
  const message =
    typeof detail === "string" && detail.trim() && detail.length <= 512
      ? detail.trim()
      : fallbackErrorMessage(status, statusText);
  return {
    code: "deep_research_error",
    message,
    retryable: RETRYABLE_STATUSES.has(status),
    request_id: responseRequestId,
  };
}

export class DeepResearchApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly retryable: boolean;
  readonly requestId: string | null;
  readonly request_id: string | null;
  readonly error: StructuredApiError;

  constructor(details: StructuredApiError, status: number) {
    super(details.message);
    this.name = "DeepResearchApiError";
    this.status = status;
    this.code = details.code;
    this.retryable = details.retryable;
    this.requestId = details.request_id ?? null;
    this.request_id = this.requestId;
    this.error = details;
  }
}

async function deepResearchFetch<T>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const res = await fetch(`/api/python-proxy/api/deep-research${path}`, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...init?.headers },
    ...init,
  });
  if (!res.ok) {
    const raw = await res.text().catch(() => "");
    const details = parseErrorPayload(
      raw,
      res.status,
      res.statusText,
      res.headers.get("x-request-id"),
    );
    throw new DeepResearchApiError(details, res.status);
  }
  return res.json() as Promise<T>;
}

export const deepResearchApi = {
  async listEngines() {
    return deepResearchFetch<{
      engines: DeepResearchEngine[];
      default: string[];
    }>("/engines");
  },

  async listJobs(limit = 30) {
    return deepResearchFetch<{ jobs: DeepResearchJob[] }>(
      `/jobs?limit=${encodeURIComponent(String(limit))}`,
    );
  },

  async startJob(payload: StartDeepResearchRequest) {
    return deepResearchFetch<DeepResearchJob>("/jobs", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  async getJob(jobId: string) {
    return deepResearchFetch<DeepResearchJob>(
      `/jobs/${encodeURIComponent(jobId)}`,
    );
  },

  async cancelJob(jobId: string) {
    return deepResearchFetch<DeepResearchJob>(
      `/jobs/${encodeURIComponent(jobId)}/cancel`,
      { method: "POST" },
    );
  },

  markdownUrl(jobId: string) {
    return `/api/python-proxy/api/deep-research/jobs/${encodeURIComponent(
      jobId,
    )}/markdown`;
  },
};
