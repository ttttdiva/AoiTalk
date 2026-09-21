/**
 * Client for the Engagement Operations API.
 *
 * The Operations surface talks to FastAPI through the authenticated same
 * origin proxy.  DTOs deliberately keep an index signature and the response
 * normalizers live in this module so the UI does not need to know whether a
 * backend response is wrapped in `{ data: ... }`, `{ opportunity: ... }`, or
 * returned directly.  This is useful while the API evolves and keeps all
 * field mapping in one place.
 */

import type { components } from "@/lib/api-types.gen";

export type OperationsJson = Record<string, unknown>;

export type OperationsConnection = {
  id: string;
  provider_key: string;
  display_name: string;
  remote_account_ref?: string | null;
  auth_status?: string | null;
  project_id?: string | null;
  version?: number;
  created_at?: string | null;
  updated_at?: string | null;
  [key: string]: unknown;
};

export type EngagementOpportunity = {
  id: string;
  connection_id?: string | null;
  project_id?: string | null;
  title?: string | null;
  source_url?: string | null;
  source_text?: string | null;
  source_hash?: string | null;
  source_snapshot_hash?: string | null;
  source_untrusted?: boolean;
  status?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  [key: string]: unknown;
};

export type OpportunityEvaluation = {
  id: string;
  opportunity_id?: string | null;
  version?: number;
  fit?: string | null;
  estimated_effort_hours?: number | null;
  estimated_cost?: number | null;
  estimated_revenue?: number | null;
  risks?: string[];
  missing_requirements?: string[];
  summary?: string | null;
  evidence_refs?: string[];
  created_at?: string | null;
  [key: string]: unknown;
};

export type ApplicationDraft = {
  id: string;
  opportunity_id?: string | null;
  version?: number;
  message?: string | null;
  offered_price?: number | null;
  currency?: string | null;
  delivery_estimate?: string | null;
  artifact_version_ids?: string[];
  created_at?: string | null;
  [key: string]: unknown;
};

export type OperationsArtifact = {
  id: string;
  filename?: string | null;
  sha256?: string | null;
  size_bytes?: number | null;
  mime_type?: string | null;
  created_at?: string | null;
  [key: string]: unknown;
};

export type OpportunityDetail = EngagementOpportunity & {
  evaluations: OpportunityEvaluation[];
  drafts: ApplicationDraft[];
  actions?: OperationsAction[];
};

export type OperationsAttempt = {
  id: string;
  action_id?: string | null;
  status?: string | null;
  outcome?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
  result_summary?: string | null;
  evidence_note?: string | null;
  remote_resource_id?: string | null;
  remote_url?: string | null;
  remote_status?: string | null;
  confirmation_level?: string | null;
  provider_attempt_ref?: string | null;
  evidence_artifact_ids?: string[];
  error_message?: string | null;
  version?: number;
  [key: string]: unknown;
};

export type OperationsTimelineEntry = {
  id?: string;
  event_type?: string | null;
  type?: string | null;
  status?: string | null;
  actor_id?: string | null;
  created_at?: string | null;
  detail?: string | null;
  entity_type?: string | null;
  entity_id?: string | null;
  actor_type?: string | null;
  payload?: OperationsJson | null;
  [key: string]: unknown;
};

export type OperationsAction = {
  id: string;
  connection_id?: string | null;
  opportunity_id?: string | null;
  application_draft_id?: string | null;
  status?: string | null;
  version?: number;
  action_version?: number;
  payload?: OperationsJson | null;
  payload_hash?: string | null;
  artifact_hashes?: string[];
  source_url?: string | null;
  source_hash?: string | null;
  source_snapshot_hash?: string | null;
  timeline?: OperationsTimelineEntry[];
  history?: {
    approvals_has_more?: boolean;
    attempts_has_more?: boolean;
    timeline_has_more?: boolean;
    approval_limit?: number;
    attempt_limit?: number;
    timeline_limit?: number;
  };
  approvals?: Array<OperationsJson & { decision?: string | null }>;
  attempts?: OperationsAttempt[];
  receipt?: OperationsJson | null;
  created_at?: string | null;
  updated_at?: string | null;
  [key: string]: unknown;
};

export type CreateConnectionInput = {
  provider_key: string;
  display_name: string;
  remote_account_ref?: string | null;
  project_id?: string | null;
};

/**
 * Connection updates mirror the strict FastAPI command DTO.  In particular,
 * project_id is intentionally absent because a connection's project scope is
 * immutable after creation.
 */
export type UpdateConnectionInput = components["schemas"]["ConnectionUpdateRequest"];

export type CreateOpportunityInput = {
  connection_id?: string | null;
  project_id?: string | null;
  source_url: string;
  source_text: string;
  title?: string | null;
};

export type CreateEvaluationInput = {
  estimated_effort_hours?: number | null;
  estimated_cost?: number | null;
  estimated_revenue?: number | null;
  fit?: string | null;
  risks?: string[];
  missing_requirements?: string[];
  summary?: string | null;
  evidence_refs?: string[];
};

export type CreateDraftInput = {
  message: string;
  offered_price?: number | null;
  currency?: string | null;
  delivery_estimate?: string | null;
  artifact_version_ids?: string[];
};

export type CreateActionInput = {
  connection_id: string;
  application_draft_id: string;
  idempotency_key?: string;
};

export type ReviseActionInput = {
  application_draft_id: string;
  expected_version: number;
};

export type ActionDecisionInput = {
  expected_version: number;
  reason?: string | null;
};

export type StartAttemptInput = {
  expected_version: number;
};

export type CompleteAttemptInput = {
  expected_version: number;
  outcome?: "succeeded" | "failed" | "uncertain" | string;
  status?: "succeeded" | "failed" | "uncertain" | string;
  result_summary?: string | null;
  remote_resource_id?: string | null;
  remote_url?: string | null;
  remote_status?: string | null;
  evidence_note?: string | null;
  confirmation_level?: string | null;
  evidence_artifact_ids?: string[];
  provider_receipt_ref?: string | null;
  error_message?: string | null;
};

export type ReconcileActionInput = {
  expected_version: number;
  resolution?: string;
  outcome?: string;
  evidence_note?: string;
  reason?: string;
  evidence_artifact_ids?: string[];
  provider_receipt_ref?: string | null;
  remote_resource_id?: string | null;
  remote_url?: string | null;
  remote_status?: string | null;
};

export type UploadArtifactInput = {
  file: File;
  /** Optional project ownership for artifacts attached to a Character. */
  project_id?: string | null;
  opportunity_id?: string | null;
  label?: string | null;
};

export class OperationsApiError extends Error {
  readonly status: number;
  readonly code?: string;
  readonly detail?: unknown;
  readonly body?: unknown;

  constructor(message: string, options: { status: number; code?: string; detail?: unknown; body?: unknown }) {
    super(message);
    this.name = "OperationsApiError";
    this.status = options.status;
    this.code = options.code;
    this.detail = options.detail;
    this.body = options.body;
  }
}

function isRecord(value: unknown): value is OperationsJson {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function asString(value: unknown): string | undefined {
  return typeof value === "string" ? value : undefined;
}

function asNumber(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function asStringArray(value: unknown): string[] | undefined {
  if (!Array.isArray(value)) return undefined;
  return value.filter((item): item is string => typeof item === "string");
}

function firstRecord(payload: unknown, keys: readonly string[]): OperationsJson {
  if (!isRecord(payload)) return {};
  for (const key of keys) {
    const candidate = payload[key];
    if (isRecord(candidate)) return candidate;
  }
  return payload;
}

function firstArray(payload: unknown, keys: readonly string[]): unknown[] {
  if (Array.isArray(payload)) return payload;
  if (!isRecord(payload)) return [];
  for (const key of keys) {
    const candidate = payload[key];
    if (Array.isArray(candidate)) return candidate;
  }
  const data = payload.data;
  if (Array.isArray(data)) return data;
  return [];
}

function normalizeConnection(value: unknown): OperationsConnection {
  const record = isRecord(value) ? value : {};
  return {
    ...record,
    id: String(record.id ?? record.connection_id ?? ""),
    provider_key: String(record.provider_key ?? record.provider ?? ""),
    display_name: String(record.display_name ?? record.name ?? record.provider_key ?? "Connection"),
    remote_account_ref: asString(record.remote_account_ref ?? record.remote_account_id) ?? null,
    auth_status: asString(record.auth_status ?? record.status) ?? null,
    project_id: asString(record.project_id) ?? null,
    version: asNumber(record.version),
  };
}

function normalizeEvaluation(value: unknown): OpportunityEvaluation {
  const record = isRecord(value) ? value : {};
  return {
    ...record,
    id: String(record.id ?? record.evaluation_id ?? ""),
    opportunity_id: asString(record.opportunity_id) ?? null,
    version: asNumber(record.version ?? record.evaluation_version),
    fit: asString(record.fit ?? record.fit_assessment) ?? null,
    estimated_effort_hours: asNumber(record.estimated_effort_hours ?? record.effort_hours) ?? null,
    estimated_cost: asNumber(record.estimated_cost) ?? null,
    estimated_revenue: asNumber(record.estimated_revenue) ?? null,
    risks: asStringArray(record.risks) ?? [],
    missing_requirements: asStringArray(record.missing_requirements) ?? [],
    summary: asString(record.summary) ?? null,
    evidence_refs: asStringArray(record.evidence_refs ?? record.evidence) ?? [],
  };
}

function normalizeDraft(value: unknown): ApplicationDraft {
  const record = isRecord(value) ? value : {};
  return {
    ...record,
    id: String(record.id ?? record.draft_id ?? ""),
    opportunity_id: asString(record.opportunity_id) ?? null,
    version: asNumber(record.version ?? record.draft_version),
    message: asString(record.message ?? record.body) ?? null,
    offered_price: asNumber(record.offered_price ?? record.price) ?? null,
    currency: asString(record.currency) ?? null,
    delivery_estimate: asString(record.delivery_estimate ?? record.delivery) ?? null,
    artifact_version_ids: asStringArray(record.artifact_version_ids ?? record.artifact_ids) ?? [],
  };
}

function normalizeAttempt(value: unknown): OperationsAttempt {
  const record = isRecord(value) ? value : {};
  return {
    ...record,
    id: String(record.id ?? record.attempt_id ?? ""),
    action_id: asString(record.action_id) ?? null,
    status: asString(record.status) ?? null,
    outcome: asString(record.outcome) ?? null,
    started_at: asString(record.started_at) ?? null,
    completed_at: asString(record.completed_at ?? record.finished_at) ?? null,
    result_summary: asString(record.result_summary) ?? null,
    evidence_note: asString(record.evidence_note) ?? null,
    remote_resource_id: asString(record.remote_resource_id ?? record.provider_attempt_ref) ?? null,
    remote_url: asString(record.remote_url) ?? null,
    remote_status: asString(record.remote_status) ?? null,
    confirmation_level: asString(record.confirmation_level) ?? null,
    provider_attempt_ref: asString(record.provider_attempt_ref ?? record.remote_resource_id) ?? null,
    evidence_artifact_ids: asStringArray(record.evidence_artifact_ids) ?? [],
    error_message: asString(record.error_message) ?? null,
    version: asNumber(record.version),
  };
}

function normalizeAction(value: unknown): OperationsAction {
  const record = isRecord(value) ? value : {};
  const timeline = firstArray(record.timeline ?? record.events, ["timeline", "events"]).filter(isRecord).map((entry) => ({ ...entry }));
  const attempts = firstArray(record.attempts, ["attempts"]).map(normalizeAttempt);
  const approvals = firstArray(record.approvals, ["approvals"]).filter(isRecord).map((approval) => ({ ...approval }));
  return {
    ...record,
    id: String(record.id ?? record.action_id ?? ""),
    connection_id: asString(record.connection_id) ?? null,
    opportunity_id: asString(record.opportunity_id) ?? null,
    application_draft_id: asString(record.application_draft_id ?? record.draft_id) ?? null,
    status: asString(record.status) ?? null,
    version: asNumber(record.version),
    action_version: asNumber(record.action_version ?? record.version),
    payload: isRecord(record.payload ?? record.canonical_payload) ? (record.payload ?? record.canonical_payload) as OperationsJson : null,
    payload_hash: asString(record.payload_hash ?? record.canonical_payload_hash) ?? null,
    artifact_hashes: asStringArray(record.artifact_hashes) ?? [],
    source_url: asString(record.source_url) ?? null,
    source_hash: asString(record.source_hash ?? record.source_snapshot_hash) ?? null,
    source_snapshot_hash: asString(record.source_snapshot_hash ?? record.source_hash) ?? null,
    timeline,
    approvals,
    attempts,
    receipt: isRecord(record.receipt) ? record.receipt : null,
  };
}

function normalizeOpportunity(value: unknown): EngagementOpportunity {
  const record = isRecord(value) ? value : {};
  return {
    ...record,
    id: String(record.id ?? record.opportunity_id ?? ""),
    connection_id: asString(record.connection_id) ?? null,
    project_id: asString(record.project_id) ?? null,
    title: asString(record.title ?? record.name) ?? null,
    source_url: asString(record.source_url ?? record.url) ?? null,
    source_text: asString(record.source_text ?? record.description ?? record.body) ?? null,
    source_hash: asString(record.source_hash ?? record.source_snapshot_hash) ?? null,
    source_snapshot_hash: asString(record.source_snapshot_hash ?? record.source_hash) ?? null,
    source_untrusted: record.source_untrusted === true,
    status: asString(record.status) ?? null,
  };
}

function normalizeOpportunityDetail(payload: unknown): OpportunityDetail {
  const outer = firstRecord(payload, ["opportunity", "detail", "data"]);
  const record = isRecord(outer.opportunity)
    ? outer.opportunity
    : isRecord(outer.detail)
      ? outer.detail
      : outer;
  const opportunity = normalizeOpportunity(record);
  const relatedSource = isRecord(outer) ? outer : payload;
  const evaluations = firstArray(relatedSource, ["evaluations", "evaluation_versions"])
    .map(normalizeEvaluation);
  const drafts = firstArray(relatedSource, ["drafts", "application_drafts", "draft_versions"])
    .map(normalizeDraft);
  const actions = firstArray(relatedSource, ["actions"]).map(normalizeAction);
  // Some backends put the related arrays on the nested opportunity object.
  const nestedEvaluations = evaluations.length ? evaluations : firstArray(record, ["evaluations", "evaluation_versions"]).map(normalizeEvaluation);
  const nestedDrafts = drafts.length ? drafts : firstArray(record, ["drafts", "application_drafts", "draft_versions"]).map(normalizeDraft);
  const nestedActions = actions.length ? actions : firstArray(record, ["actions"]).map(normalizeAction);
  return { ...opportunity, evaluations: nestedEvaluations, drafts: nestedDrafts, actions: nestedActions };
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
  if (init.body && !(init.body instanceof FormData) && !headers.has("content-type")) {
    headers.set("content-type", "application/json");
  }
  if (idempotencyKey?.trim()) headers.set("idempotency-key", idempotencyKey.trim());
  const response = await fetch(`/api/python-proxy${path}`, {
    ...init,
    cache: "no-store",
    credentials: "include",
    headers,
  });
  const body = await parseResponse(response);
  if (!response.ok) {
    const record = isRecord(body) ? body : {};
    const detail = record.detail ?? record.message ?? record.error;
    const message = typeof detail === "string" ? detail : response.statusText || `Operations API request failed (${response.status})`;
    throw new OperationsApiError(message, {
      status: response.status,
      code: asString(record.code),
      detail,
      body,
    });
  }
  return body as T;
}

function jsonBody(value: unknown): RequestInit {
  return { body: JSON.stringify(value), headers: { "content-type": "application/json" } };
}

/**
 * Keep the wire payload bounded to the server's strict update DTO even when a
 * caller receives a wider object at runtime (for example, a read projection
 * that still contains project_id or stale metadata).
 */
function serializeConnectionUpdate(input: UpdateConnectionInput): UpdateConnectionInput {
  const body = {
    expected_version: input.expected_version,
    ...(input.provider_key !== undefined ? { provider_key: input.provider_key } : {}),
    ...(input.display_name !== undefined ? { display_name: input.display_name } : {}),
    ...(input.remote_account_ref !== undefined ? { remote_account_ref: input.remote_account_ref } : {}),
    ...(input.auth_status !== undefined ? { auth_status: input.auth_status } : {}),
    ...(input.metadata !== undefined ? { metadata: input.metadata } : {}),
  };
  return body;
}

function encodeId(id: string): string {
  return encodeURIComponent(id);
}

function actionRecord(payload: unknown): OperationsAction {
  const outer = firstRecord(payload, ["action", "data"]);
  return normalizeAction(isRecord(outer.action) ? outer.action : outer);
}

export const operationsApi = {
  async listConnections(): Promise<OperationsConnection[]> {
    const payload = await request<unknown>("/operations/connections");
    return firstArray(payload, ["connections", "items"]).map(normalizeConnection);
  },

  async createConnection(input: CreateConnectionInput, idempotencyKey?: string): Promise<OperationsConnection> {
    const payload = await request<unknown>("/operations/connections", { method: "POST", ...jsonBody(input) }, idempotencyKey);
    return normalizeConnection(firstRecord(payload, ["connection", "data"]));
  },

  async updateConnection(id: string, input: UpdateConnectionInput, idempotencyKey?: string): Promise<OperationsConnection> {
    const payload = await request<unknown>(`/operations/connections/${encodeId(id)}`, { method: "PATCH", ...jsonBody(serializeConnectionUpdate(input)) }, idempotencyKey);
    return normalizeConnection(firstRecord(payload, ["connection", "data"]));
  },

  async listOpportunities(params?: { connection_id?: string; project_id?: string }): Promise<EngagementOpportunity[]> {
    const query = new URLSearchParams();
    if (params?.connection_id) query.set("connection_id", params.connection_id);
    if (params?.project_id) query.set("project_id", params.project_id);
    const suffix = query.toString() ? `?${query.toString()}` : "";
    const payload = await request<unknown>(`/operations/opportunities${suffix}`);
    return firstArray(payload, ["opportunities", "items"]).map(normalizeOpportunity);
  },

  async createOpportunity(input: CreateOpportunityInput, idempotencyKey?: string): Promise<EngagementOpportunity> {
    const payload = await request<unknown>("/operations/opportunities", { method: "POST", ...jsonBody(input) }, idempotencyKey);
    return normalizeOpportunity(firstRecord(payload, ["opportunity", "data"]));
  },

  async getOpportunity(id: string): Promise<OpportunityDetail> {
    const payload = await request<unknown>(`/operations/opportunities/${encodeId(id)}`);
    return normalizeOpportunityDetail(payload);
  },

  async createEvaluation(opportunityId: string, input: CreateEvaluationInput, idempotencyKey?: string): Promise<OpportunityEvaluation> {
    const payload = await request<unknown>(`/operations/opportunities/${encodeId(opportunityId)}/evaluations`, { method: "POST", ...jsonBody(input) }, idempotencyKey);
    return normalizeEvaluation(firstRecord(payload, ["evaluation", "data"]));
  },

  async createDraft(opportunityId: string, input: CreateDraftInput, idempotencyKey?: string): Promise<ApplicationDraft> {
    const payload = await request<unknown>(`/operations/opportunities/${encodeId(opportunityId)}/drafts`, { method: "POST", ...jsonBody(input) }, idempotencyKey);
    return normalizeDraft(firstRecord(payload, ["draft", "application_draft", "data"]));
  },

  async uploadArtifact(input: UploadArtifactInput, idempotencyKey?: string): Promise<OperationsArtifact> {
    const form = new FormData();
    form.append("file", input.file);
    if (input.project_id) form.append("project_id", input.project_id);
    if (input.opportunity_id) form.append("opportunity_id", input.opportunity_id);
    if (input.label) form.append("label", input.label);
    return request<OperationsArtifact>("/operations/artifacts", { method: "POST", body: form }, idempotencyKey);
  },

  async listActions(params?: { status?: string; project_id?: string }): Promise<OperationsAction[]> {
    const query = new URLSearchParams();
    if (params?.status) query.set("status", params.status);
    if (params?.project_id) query.set("project_id", params.project_id);
    const suffix = query.toString() ? `?${query.toString()}` : "";
    const payload = await request<unknown>(`/operations/actions${suffix}`);
    return firstArray(payload, ["actions", "items"]).map(normalizeAction);
  },

  async createAction(input: CreateActionInput, idempotencyKey?: string): Promise<OperationsAction> {
    const payload = await request<unknown>("/operations/actions", { method: "POST", ...jsonBody(input) }, idempotencyKey ?? input.idempotency_key);
    return actionRecord(payload);
  },

  async getAction(id: string): Promise<OperationsAction> {
    const payload = await request<unknown>(`/operations/actions/${encodeId(id)}`);
    return actionRecord(payload);
  },

  async reviseAction(id: string, input: ReviseActionInput, idempotencyKey?: string): Promise<OperationsAction> {
    const payload = await request<unknown>(`/operations/actions/${encodeId(id)}/revise`, { method: "POST", ...jsonBody(input) }, idempotencyKey);
    return actionRecord(payload);
  },

  async approveAction(id: string, input: ActionDecisionInput, idempotencyKey?: string): Promise<OperationsAction> {
    const payload = await request<unknown>(`/operations/actions/${encodeId(id)}/approve`, { method: "POST", ...jsonBody(input) }, idempotencyKey);
    return actionRecord(payload);
  },

  async rejectAction(id: string, input: ActionDecisionInput, idempotencyKey?: string): Promise<OperationsAction> {
    const payload = await request<unknown>(`/operations/actions/${encodeId(id)}/reject`, { method: "POST", ...jsonBody(input) }, idempotencyKey);
    return actionRecord(payload);
  },

  async startAttempt(actionId: string, input: StartAttemptInput, idempotencyKey?: string): Promise<OperationsAttempt> {
    const payload = await request<unknown>(`/operations/actions/${encodeId(actionId)}/attempts`, { method: "POST", ...jsonBody(input) }, idempotencyKey);
    return normalizeAttempt(firstRecord(payload, ["attempt", "data"]));
  },

  async completeAttempt(actionId: string, attemptId: string, input: CompleteAttemptInput, idempotencyKey?: string): Promise<OperationsAction> {
    // The canonical FastAPI DTO calls the outcome `status` and requires
    // evidence artifact IDs.  Keep the friendlier UI aliases accepted at the
    // edge while sending the strict wire shape to the backend.
    const body = {
      status: input.status ?? input.outcome,
      expected_version: input.expected_version,
      evidence_artifact_ids: input.evidence_artifact_ids ?? [],
      provider_receipt_ref: input.provider_receipt_ref ?? input.remote_resource_id ?? null,
      error_message: input.error_message ?? null,
      // Keep optional provider/evidence fields on the wire as well.  Newer
      // routers may persist these fields explicitly; older routers safely
      // ignore unknown JSON keys while still accepting the canonical subset.
      result_summary: input.result_summary ?? null,
      remote_resource_id: input.remote_resource_id ?? null,
      remote_url: input.remote_url ?? null,
      remote_status: input.remote_status ?? null,
      evidence_note: input.evidence_note ?? null,
      confirmation_level: input.confirmation_level ?? null,
    };
    const payload = await request<unknown>(`/operations/actions/${encodeId(actionId)}/attempts/${encodeId(attemptId)}/complete`, { method: "POST", ...jsonBody(body) }, idempotencyKey);
    return actionRecord(payload);
  },

  async reconcileAction(actionId: string, input: ReconcileActionInput, idempotencyKey?: string): Promise<OperationsAction> {
    const body = {
      outcome: input.outcome ?? input.resolution,
      expected_version: input.expected_version,
      evidence_artifact_ids: input.evidence_artifact_ids ?? [],
      provider_receipt_ref: input.provider_receipt_ref ?? input.remote_resource_id ?? null,
      reason: input.reason ?? input.evidence_note ?? null,
      evidence_note: input.evidence_note ?? null,
      remote_resource_id: input.remote_resource_id ?? null,
      remote_url: input.remote_url ?? null,
      remote_status: input.remote_status ?? null,
    };
    const payload = await request<unknown>(`/operations/actions/${encodeId(actionId)}/reconcile`, { method: "POST", ...jsonBody(body) }, idempotencyKey);
    return actionRecord(payload);
  },
};

export const __operationsTestUtils = {
  normalizeAction,
  normalizeOpportunityDetail,
  normalizeConnection,
  normalizeEvaluation,
  normalizeDraft,
};
