/** Human review boundary for MediaOps Learning proposals. */

export type LearningProposal = {
  id: string;
  subject_type: string;
  subject_ref: string;
  proposal_type: string;
  title: string;
  summary: string;
  recommendation: string;
  evidence_refs: Array<Record<string, unknown>>;
  human_decision_refs: string[];
  target_fields: string[];
  proposed_before: Record<string, unknown>;
  proposed_after: Record<string, unknown>;
  expected_persona_revision_id?: string | null;
  expected_persona_revision_version?: number | null;
  expected_persona_revision_hash?: string | null;
  review_history: Array<Record<string, unknown>>;
  applied_persona_revision_id?: string | null;
  applied_persona_revision_version?: number | null;
  applied_persona_revision_hash?: string | null;
  window_start: string;
  window_end: string;
  confidence: number;
  uncertainty: number;
  status: "pending_review" | "accepted" | "rejected" | "stale" | string;
  proposal_hash: string;
};

export type LearningProposalEditInput = Partial<Pick<LearningProposal, "title" | "summary" | "recommendation" | "evidence_refs" | "human_decision_refs" | "target_fields" | "proposed_before" | "proposed_after" | "window_start" | "window_end" | "confidence" | "uncertainty">> & {
  reason: string;
};

export type LearningProposalDecisionInput = {
  reason: string;
  expected_persona_revision_id?: string | null;
  expected_persona_revision_version?: number | null;
  expected_persona_revision_hash?: string | null;
};

export type LearningProposalApplyInput = Omit<LearningProposalDecisionInput, "reason"> & {
  reason?: string | null;
};

export class MediaLearningApiError extends Error {
  readonly status: number;
  readonly detail?: unknown;
  readonly body?: unknown;

  constructor(message: string, options: { status: number; detail?: unknown; body?: unknown }) {
    super(message);
    this.name = "MediaLearningApiError";
    this.status = options.status;
    this.detail = options.detail;
    this.body = options.body;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

async function parseResponse(response: Response): Promise<unknown> {
  if (response.status === 204) return undefined;
  const contentType = response.headers.get("content-type") ?? "";
  if (contentType.includes("json")) return response.json().catch(() => undefined);
  const text = await response.text().catch(() => "");
  if (!text) return undefined;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return text;
  }
}

async function request<T>(path: string, init: RequestInit = {}, idempotencyKey?: string): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body && !(init.body instanceof FormData) && !headers.has("content-type")) headers.set("content-type", "application/json");
  if (idempotencyKey?.trim()) headers.set("idempotency-key", idempotencyKey.trim());
  const response = await fetch(`/api/python-proxy${path}`, { ...init, cache: "no-store", credentials: "include", headers });
  const body = await parseResponse(response);
  if (!response.ok) {
    const record = isRecord(body) ? body : {};
    const detail = record.detail ?? record.message ?? record.error;
    const message = typeof detail === "string" ? detail : response.statusText || `Learning request failed (${response.status})`;
    throw new MediaLearningApiError(message, { status: response.status, detail, body });
  }
  return body as T;
}

function jsonBody(value: unknown): RequestInit {
  return { body: JSON.stringify(value), headers: { "content-type": "application/json" } };
}

function encodeId(id: string): string {
  return encodeURIComponent(id);
}

export const mediaLearningApi = {
  list(projectId?: string | null): Promise<LearningProposal[]> {
    const query = projectId ? `?project_id=${encodeURIComponent(projectId)}` : "";
    return request<LearningProposal[]>(`/operations/media/learning-proposals${query}`);
  },
  get(id: string): Promise<LearningProposal> {
    // Read/list are hosted on the legacy metrics router for compatibility.
    return request<LearningProposal>(`/operations/media/learning-proposals/${encodeId(id)}`);
  },
  edit(id: string, input: LearningProposalEditInput, idempotencyKey: string): Promise<LearningProposal> {
    return request<LearningProposal>(`/operations/media/learning-proposals/${encodeId(id)}`, { method: "PATCH", ...jsonBody(input) }, idempotencyKey);
  },
  approve(id: string, input: LearningProposalDecisionInput, idempotencyKey: string): Promise<LearningProposal> {
    return request<LearningProposal>(`/operations/media/learning-proposals/${encodeId(id)}/approve`, { method: "POST", ...jsonBody(input) }, idempotencyKey);
  },
  reject(id: string, input: LearningProposalDecisionInput, idempotencyKey: string): Promise<LearningProposal> {
    return request<LearningProposal>(`/operations/media/learning-proposals/${encodeId(id)}/reject`, { method: "POST", ...jsonBody(input) }, idempotencyKey);
  },
  apply(id: string, input: LearningProposalApplyInput, idempotencyKey: string): Promise<LearningProposal> {
    return request<LearningProposal>(`/operations/media/learning-proposals/${encodeId(id)}/apply`, { method: "POST", ...jsonBody(input) }, idempotencyKey);
  },
};
