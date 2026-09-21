export type AutomationExecutionMode =
  | "research_only"
  | "draft"
  | "review_before_generate"
  | "auto_generate";

export type AutomationRunState =
  | "scheduled"
  | "theme_discovery"
  | "research"
  | "brief"
  | "concept_planning"
  | "prompt_planning"
  | "waiting_review"
  | "generation_submitting"
  | "generation_running"
  | "complete"
  | "failed"
  | "uncertain"
  | string;

export type AutomationRevision = {
  id: string;
  program_id: string;
  version: number;
  execution_mode: AutomationExecutionMode;
  trigger: Record<string, unknown>;
  discovery: Record<string, unknown>;
  research_binding: Record<string, unknown>;
  planning_policy: Record<string, unknown>;
  generation_action: Record<string, unknown>;
  fallback: Record<string, unknown>;
  content_hash: string;
  created_at?: string | null;
};

export type AutomationCandidate = {
  candidate_id: string;
  status: string;
  payload: Record<string, unknown>;
};

export type AutomationRun = {
  id: string;
  program_id: string;
  program_revision_id: string;
  project_id?: string | null;
  trigger_kind: string;
  state: AutomationRunState;
  observation: Record<string, unknown>;
  research_run_id?: string | null;
  research_brief: Record<string, unknown>;
  candidates: AutomationCandidate[];
  selected_candidate_id?: string | null;
  novelty_snapshot: Record<string, unknown>;
  external_run_id?: string | null;
  preset_id?: string | null;
  preset_revision_id?: string | null;
  preset_revision_number?: number | null;
  preset_checksum?: string | null;
  result_deep_link?: string | null;
  generation_result?: Record<string, unknown>;
  error_code?: string | null;
  error_message?: string | null;
  correlation_id?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
};

export type AutomationProgram = {
  id: string;
  project_id?: string | null;
  name: string;
  enabled: boolean;
  current_revision: AutomationRevision;
  revisions: AutomationRevision[];
  latest_run?: AutomationRun | null;
  updated_at?: string | null;
};

export type PresetCatalogItem = {
  preset_id: string;
  name: string;
  description?: string;
  current_revision_id: string;
  current_revision: number;
  checksum: string;
  model_family?: string | null;
  updated_at?: string | null;
};

export type AutomationRevisionInput = {
  execution_mode: AutomationExecutionMode;
  trigger: Record<string, unknown>;
  discovery: Record<string, unknown>;
  research_binding: Record<string, unknown>;
  planning_policy: Record<string, unknown>;
  generation_action: Record<string, unknown>;
  fallback: Record<string, unknown>;
};

export type AutomationProgramCreateInput = AutomationRevisionInput & {
  project_id?: string | null;
  name: string;
  enabled?: boolean;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

async function request<T>(path: string, init: RequestInit = {}, idempotencyKey?: string): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body && !headers.has("content-type")) headers.set("content-type", "application/json");
  if (idempotencyKey) headers.set("idempotency-key", idempotencyKey);
  const response = await fetch(`/api/python-proxy${path}`, { ...init, headers, credentials: "include", cache: "no-store" });
  let body: unknown;
  const contentType = response.headers.get("content-type") ?? "";
  if (contentType.includes("json")) body = await response.json().catch(() => undefined);
  else body = await response.text().catch(() => "");
  if (!response.ok) {
    const record = isRecord(body) ? body : {};
    const detail = record.detail ?? record.message ?? record.error;
    throw new Error(typeof detail === "string" ? detail : response.statusText || `Automation request failed (${response.status})`);
  }
  return body as T;
}

const jsonBody = (value: unknown): RequestInit => ({ body: JSON.stringify(value), headers: { "content-type": "application/json" } });
const id = (value: string) => encodeURIComponent(value);

export const mediaAutomationApi = {
  listPrograms(projectId?: string | null): Promise<AutomationProgram[]> {
    const query = projectId ? `?project_id=${encodeURIComponent(projectId)}` : "";
    return request(`/operations/media/automations${query}`);
  },
  createProgram(input: AutomationProgramCreateInput, key: string): Promise<AutomationProgram> {
    return request("/operations/media/automations", { method: "POST", ...jsonBody(input) }, key);
  },
  getProgram(programId: string): Promise<AutomationProgram> {
    return request(`/operations/media/automations/${id(programId)}`);
  },
  appendRevision(programId: string, input: AutomationRevisionInput & { expected_version: number }, key: string): Promise<AutomationRevision> {
    return request(`/operations/media/automations/${id(programId)}/revisions`, { method: "POST", ...jsonBody(input) }, key);
  },
  setEnabled(programId: string, enabled: boolean): Promise<AutomationProgram> {
    return request(`/operations/media/automations/${id(programId)}/enabled`, { method: "PATCH", ...jsonBody({ enabled }) });
  },
  duplicate(programId: string, name: string | null, key: string): Promise<AutomationProgram> {
    return request(`/operations/media/automations/${id(programId)}/duplicate`, { method: "POST", ...jsonBody({ name }) }, key);
  },
  presetCatalog(workspaceId: string): Promise<PresetCatalogItem[]> {
    return request(`/operations/media/automations/generation-studio/${id(workspaceId)}/presets`);
  },
  trigger(programId: string, triggerKey: string, triggerKind = "manual"): Promise<AutomationRun> {
    return request(`/operations/media/automations/${id(programId)}/runs`, { method: "POST", ...jsonBody({ trigger_key: triggerKey, trigger_kind: triggerKind, execute: true }) });
  },
  listRuns(programId?: string | null): Promise<AutomationRun[]> {
    const query = programId ? `?program_id=${encodeURIComponent(programId)}` : "";
    return request(`/operations/media/automations/runs/list${query}`);
  },
  getRun(runId: string): Promise<AutomationRun> {
    return request(`/operations/media/automations/runs/${id(runId)}`);
  },
  resume(runId: string): Promise<AutomationRun> {
    return request(`/operations/media/automations/runs/${id(runId)}/resume`, { method: "POST" });
  },
  editCandidate(runId: string, candidateId: string, payload: Record<string, unknown>): Promise<AutomationRun> {
    return request(`/operations/media/automations/runs/${id(runId)}/candidates/${id(candidateId)}`, { method: "PATCH", ...jsonBody({ payload }) });
  },
  regenerate(runId: string, key: string): Promise<AutomationRun> {
    return request(`/operations/media/automations/runs/${id(runId)}/regenerate`, { method: "POST" }, key);
  },
  approve(runId: string, candidateId: string): Promise<AutomationRun> {
    return request(`/operations/media/automations/runs/${id(runId)}/candidates/${id(candidateId)}/approve`, { method: "POST" });
  },
  retryUncertain(runId: string): Promise<AutomationRun> {
    return request(`/operations/media/automations/runs/${id(runId)}/retry-uncertain`, { method: "POST" });
  },
  reconcile(runId: string): Promise<AutomationRun> {
    return request(`/operations/media/automations/runs/${id(runId)}/reconcile`, { method: "POST" });
  },
};
