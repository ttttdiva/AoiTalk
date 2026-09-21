import { MediaResearchApiError } from "@/lib/media-operations-research-api";

/** Opaque identifiers are intentionally kept as strings at the UI boundary. */
export type WorkspaceOpaqueId = string;
export type ProjectOpaqueId = string;
export type RunOpaqueId = string;
export type AssetOpaqueId = string;
export type OutputVersionOpaqueId = string;

export type GenerationWorkspaceStatus =
  | "configured"
  | "unavailable"
  | "verified"
  | string;
export type CreativeRecipeType =
  | "image"
  | "image_set"
  | "comic"
  | "video"
  | "thumbnail"
  | string;
export type GenerationPlanStatus =
  | "draft"
  | "submitted"
  | "unavailable"
  | "uncertain"
  | "succeeded"
  | "failed"
  | string;
export type GenerationRunStatus =
  | "draft"
  | "queued"
  | "claimed"
  | "running"
  | "output_pending"
  | "succeeded"
  | "failed"
  | "cancelled"
  | "quarantined"
  | "uncertain"
  | "unavailable"
  | string;

/** Semantic media kind owned by AoiTalk; provider/workflow details stay in 73. */
export type GenerationKind = "image" | "video";

export type GenerationCatalogStatus =
  | "available"
  | "configured"
  | "verified"
  | "unavailable"
  | "unsupported"
  | string;

export type GenerationWorkspace = {
  id: string;
  owner_user_id?: string | null;
  project_id?: string | null;
  provider: string;
  external_workspace_id: WorkspaceOpaqueId;
  external_project_id?: ProjectOpaqueId | null;
  base_url?: string | null;
  status: GenerationWorkspaceStatus;
  config_hash?: string | null;
  created_at?: string | null;
  [key: string]: unknown;
};

export type CreativeRecipeRevision = {
  id: string;
  creative_recipe_id?: string;
  persona_id?: string;
  persona_revision_id?: string | null;
  version: number;
  recipe_type: CreativeRecipeType;
  workflow_id?: string | null;
  image_model_selection_id?: string | null;
  model_selection_id?: string | null;
  identity_pack_id?: string | null;
  prompt_schema_id?: string | null;
  prompt_template: string;
  negative_requirements?: string | null;
  reference_asset_ids?: string[];
  aspect_ratio?: string | null;
  width?: number | null;
  height?: number | null;
  duration_seconds?: number | null;
  frames?: number | null;
  frame_count?: number | null;
  storyboard?: string[];
  candidate_count?: number;
  cost_policy?: unknown;
  provenance_retention_policy?: unknown;
  human_review_policy?: unknown;
  content_hash?: string;
  created_at?: string | null;
  [key: string]: unknown;
};

export type CreativeRecipe = {
  id: string;
  owner_user_id?: string | null;
  project_id?: string | null;
  persona_id: string;
  current_revision?: CreativeRecipeRevision | null;
  revisions?: CreativeRecipeRevision[];
  created_at?: string | null;
  [key: string]: unknown;
};

export type CreativeRecipeRevisionCreateInput = {
  expected_version?: number | null;
  persona_revision_id?: string | null;
  recipe_type: CreativeRecipeType;
  workflow_id?: string | null;
  image_model_selection_id?: string | null;
  model_selection_id?: string | null;
  identity_pack_id?: string | null;
  prompt_schema_id?: string | null;
  prompt_template: string;
  negative_requirements?: string | null;
  reference_asset_ids?: string[];
  aspect_ratio?: string | null;
  width?: number | null;
  height?: number | null;
  duration_seconds?: number | null;
  frames?: number | null;
  frame_count?: number | null;
  storyboard?: string[];
  candidate_count?: number;
  cost_policy?: unknown;
  provenance_retention_policy?: unknown;
  human_review_policy?: unknown;
};

export type CreativeRecipeCreateInput = CreativeRecipeRevisionCreateInput & {
  persona_id: string;
  name: string;
  project_id?: string | null;
};

export type GenerationPlan = {
  id: string;
  owner_user_id?: string | null;
  project_id?: string | null;
  persona_revision_id: string;
  persona_revision_hash?: string | null;
  content_item_id?: string | null;
  content_variant_id?: string | null;
  creative_recipe_revision_id: string;
  workspace_id: string;
  requested_outputs: number;
  request_spec: Record<string, unknown>;
  /** Server-derived from the immutable CreativeRecipe revision. */
  generation_kind?: GenerationKind | string | null;
  plan_hash?: string | null;
  status: GenerationPlanStatus;
  version?: number | null;
  created_at?: string | null;
  [key: string]: unknown;
};

export type GenerationRequestSpec = {
  prompt: string;
  negative_prompt?: string;
  seed?: number | null;
  /** Server-owned semantic model selection; provider/model internals stay server-side. */
  model_selection_id?: string;
  /** Image alias retained for the existing image contract. */
  image_model_selection_id?: string;
  generation_settings?: Record<string, string | number | boolean | null>;
  size_preset_id?: "normal_square" | "normal_landscape" | "normal_portrait" | "custom";
  width?: number | null;
  height?: number | null;
  duration_seconds?: number | null;
  frame_count?: number | null;
  storyboard?: string[];
  accept_metered_generation?: boolean;
  aspect_ratio?: string | null;
};

export type GenerationPlanCreateInput = {
  persona_revision_id: string;
  creative_recipe_revision_id: string;
  workspace_id: string;
  content_item_id?: string | null;
  content_variant_id?: string | null;
  requested_outputs: number;
  request_spec: GenerationRequestSpec;
};

export type GenerationRun = {
  id: string;
  plan_id: string;
  external_run_id?: RunOpaqueId | null;
  provider?: string | null;
  external_workspace_id?: WorkspaceOpaqueId | null;
  external_project_id?: ProjectOpaqueId | null;
  status: GenerationRunStatus;
  generation_kind?: GenerationKind | string | null;
  adapter_release?: string | null;
  cost_summary?: Record<string, unknown> | null;
  error_code?: string | null;
  result_deep_link?: string | null;
  outputs?: GenerationOutput[];
  started_at?: string | null;
  finished_at?: string | null;
  created_at?: string | null;
  [key: string]: unknown;
};

/**
 * Submit is intentionally an intent receipt rather than a run response.
 * An unavailable adapter returns `run: null`; callers must not manufacture a
 * run id or claim that generation succeeded in that case.
 */
export type GenerationRunIntent = {
  id: string;
  plan_id: string;
  owner_user_id?: string | null;
  project_id?: string | null;
  external_idempotency_key?: string | null;
  request_hash?: string | null;
  status?: string;
  external_run_id?: RunOpaqueId | null;
  adapter_release?: string | null;
  error_code?: string | null;
  [key: string]: unknown;
};

export type GenerationSubmitStatus =
  | "pending"
  | "submitted"
  | "unavailable"
  | "uncertain"
  | "failed"
  | string;

export type GenerationSubmitResponse = {
  plan: GenerationPlan;
  intent: GenerationRunIntent;
  run: GenerationRun | null;
  outputs: GenerationOutput[];
  status: GenerationSubmitStatus;
  external_idempotency_key: string;
};

export type GenerationOutput = {
  id: string;
  run_id?: string;
  generation_run_id?: string;
  external_asset_id: AssetOpaqueId;
  external_output_version: OutputVersionOpaqueId;
  sha256: string;
  mime_type: string;
  generation_kind?: GenerationKind | string | null;
  width?: number | null;
  height?: number | null;
  deep_link?: string | null;
  /** Optional server-proxied preview; never use a provider URL directly. */
  preview_url?: string | null;
  preview_mime_type?: string | null;
  provenance_hash?: string | null;
  [key: string]: unknown;
};

export type GenerationCatalogItem = {
  kind: GenerationKind | string;
  model_selection_id: string;
  /** Compatibility alias for the image catalog response. */
  image_model_selection_id?: string | null;
  display_name?: string | null;
  status?: GenerationCatalogStatus | null;
};

export type ImageCatalogItem = GenerationCatalogItem & {
  kind: "image" | string;
  image_model_selection_id: string;
};

export type VideoCatalogItem = GenerationCatalogItem & {
  kind: "video" | string;
};

export type GenerationSubmitInput = {
  expected_plan_hash?: string | null;
  /** Human-only acknowledgement for a metered/paid external action. */
  acknowledge_metered_generation?: boolean;
};

export type GenerationReconcileInput = {
  expected_plan_hash: string;
  expected_intent_request_hash: string;
  /** A reconcile never authorizes paid work implicitly. */
  acknowledge_metered_generation?: boolean;
};

export type GenerationOutputSelection = {
  id?: string;
  run_id?: string;
  output_id?: string;
  generation_run_id?: string;
  generation_output_id?: string;
  owner_user_id?: string | null;
  project_id?: string | null;
  selection_hash?: string | null;
  external_asset_id?: AssetOpaqueId;
  external_output_version?: OutputVersionOpaqueId;
  selected_at?: string | null;
  [key: string]: unknown;
};

export class MediaGenerationApiError extends MediaResearchApiError {
  override readonly name = "MediaGenerationApiError";
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

async function parseResponse(response: Response): Promise<unknown> {
  if (response.status === 204) return undefined;
  const contentType = response.headers.get("content-type") ?? "";
  if (contentType.includes("json")) {
    return response.json().catch(() => undefined);
  }
  const text = await response.text().catch(() => "");
  if (!text) return undefined;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return text;
  }
}

async function request<T>(
  path: string,
  init: RequestInit = {},
  idempotencyKey?: string,
): Promise<T> {
  const headers = new Headers(init.headers);
  if (
    init.body &&
    !(init.body instanceof FormData) &&
    !headers.has("content-type")
  ) {
    headers.set("content-type", "application/json");
  }
  if (idempotencyKey?.trim()) {
    headers.set("idempotency-key", idempotencyKey.trim());
  }

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
    const message =
      typeof detail === "string"
        ? detail
        : response.statusText ||
          `Media generation request failed (${response.status})`;
    throw new MediaGenerationApiError(message, {
      status: response.status,
      detail,
      body,
    });
  }
  return body as T;
}

function jsonBody(value: unknown): RequestInit {
  return {
    body: JSON.stringify(value),
    headers: { "content-type": "application/json" },
  };
}

function encodeId(id: string): string {
  return encodeURIComponent(id);
}

function querySuffix(params: Record<string, string | null | undefined>): string {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value) query.set(key, value);
  }
  const encoded = query.toString();
  return encoded ? `?${encoded}` : "";
}

/**
 * Thin, credential-free client for the AoiTalk Generation Studio boundary.
 * It only talks to the server-side Python proxy; the browser never contacts
 * the Generation Studio port directly and never receives provider secrets.
 */
export const mediaGenerationApi = {
  async listWorkspaces(projectId?: string | null): Promise<GenerationWorkspace[]> {
    return request<GenerationWorkspace[]>(
      `/operations/media/generation-workspaces${querySuffix({ project_id: projectId })}`,
    );
  },

  async createWorkspace(
    input: {
      project_id?: string | null;
      provider?: "comfyui_workbench";
      external_workspace_id: WorkspaceOpaqueId;
      external_project_id?: ProjectOpaqueId | null;
      base_url: string;
    },
    idempotencyKey: string,
  ): Promise<GenerationWorkspace> {
    return request<GenerationWorkspace>(
      "/operations/media/generation-workspaces",
      { method: "POST", ...jsonBody(input) },
      idempotencyKey,
    );
  },

  async getWorkspace(id: string): Promise<GenerationWorkspace> {
    return request<GenerationWorkspace>(
      `/operations/media/generation-workspaces/${encodeId(id)}`,
    );
  },

  async listRecipes(personaId?: string | null): Promise<CreativeRecipe[]> {
    return request<CreativeRecipe[]>(
      `/operations/media/creative-recipes${querySuffix({ persona_id: personaId })}`,
    );
  },

  async createRecipe(
    input: CreativeRecipeCreateInput,
    idempotencyKey: string,
  ): Promise<CreativeRecipe> {
    return request<CreativeRecipe>(
      "/operations/media/creative-recipes",
      { method: "POST", ...jsonBody(input) },
      idempotencyKey,
    );
  },

  async getRecipe(id: string): Promise<CreativeRecipe> {
    return request<CreativeRecipe>(
      `/operations/media/creative-recipes/${encodeId(id)}`,
    );
  },

  async appendRecipeRevision(
    recipeId: string,
    input: CreativeRecipeRevisionCreateInput,
    idempotencyKey: string,
  ): Promise<CreativeRecipeRevision> {
    return request<CreativeRecipeRevision>(
      `/operations/media/creative-recipes/${encodeId(recipeId)}/revisions`,
      { method: "POST", ...jsonBody(input) },
      idempotencyKey,
    );
  },

  async listPlans(
    options: { workspaceId?: string | null; personaRevisionId?: string | null } = {},
  ): Promise<GenerationPlan[]> {
    return request<GenerationPlan[]>(
      `/operations/media/generation-plans${querySuffix({
        workspace_id: options.workspaceId,
        persona_revision_id: options.personaRevisionId,
      })}`,
    );
  },

  async createPlan(
    input: GenerationPlanCreateInput,
    idempotencyKey: string,
  ): Promise<GenerationPlan> {
    return request<GenerationPlan>(
      "/operations/media/generation-plans",
      { method: "POST", ...jsonBody(input) },
      idempotencyKey,
    );
  },

  async getPlan(id: string): Promise<GenerationPlan> {
    return request<GenerationPlan>(
      `/operations/media/generation-plans/${encodeId(id)}`,
    );
  },

  async submitPlan(
    id: string,
    input: GenerationSubmitInput,
    idempotencyKey: string,
  ): Promise<GenerationSubmitResponse> {
    return request<GenerationSubmitResponse>(
      `/operations/media/generation-plans/${encodeId(id)}/submit`,
      { method: "POST", ...jsonBody(input) },
      idempotencyKey,
    );
  },

  /**
   * Reconcile a previously submitted/uncertain intent. This is deliberately
   * a separate action from submit: the UI must never retry an unknown write.
   */
  async reconcilePlan(
    id: string,
    input: GenerationReconcileInput,
    idempotencyKey: string,
  ): Promise<GenerationSubmitResponse> {
    return request<GenerationSubmitResponse>(
      `/operations/media/generation-plans/${encodeId(id)}/reconcile`,
      { method: "POST", ...jsonBody(input) },
      idempotencyKey,
    );
  },

  async listRuns(planId?: string | null): Promise<GenerationRun[]> {
    return request<GenerationRun[]>(
      `/operations/media/generation-runs${querySuffix({ plan_id: planId })}`,
    );
  },

  async getRun(id: string): Promise<GenerationRun> {
    return request<GenerationRun>(
      `/operations/media/generation-runs/${encodeId(id)}`,
    );
  },

  async refreshRun(id: string, idempotencyKey?: string): Promise<GenerationRun> {
    return request<GenerationRun>(
      `/operations/media/generation-runs/${encodeId(id)}/refresh`,
      { method: "POST" },
      idempotencyKey,
    );
  },

  async selectOutput(
    runId: string,
    outputId: string,
    idempotencyKey: string,
  ): Promise<GenerationOutputSelection> {
    return request<GenerationOutputSelection>(
      `/operations/media/generation-runs/${encodeId(runId)}/outputs/select`,
      { method: "POST", ...jsonBody({ output_id: outputId }) },
      idempotencyKey,
    );
  },

  /** Read-only model-selection catalog; it never contains generated assets. */
  async listGenerationCatalog(
    workspaceId: string,
    kind: GenerationKind,
  ): Promise<GenerationCatalogItem[]> {
    return request<GenerationCatalogItem[]>(
      `/operations/media/generation-workspaces/${encodeId(workspaceId)}/catalog${querySuffix({ kind })}`,
    );
  },

  /** Short alias used by newer callers; kept equivalent to listGenerationCatalog. */
  async listCatalog(
    workspaceId: string,
    kind: GenerationKind,
  ): Promise<GenerationCatalogItem[]> {
    return request<GenerationCatalogItem[]>(
      `/operations/media/generation-workspaces/${encodeId(workspaceId)}/catalog${querySuffix({ kind })}`,
    );
  },

  /** Compatibility helper for existing image-only callers. */
  async listImageCatalog(workspaceId: string): Promise<ImageCatalogItem[]> {
    return (await this.listGenerationCatalog(workspaceId, "image")) as ImageCatalogItem[];
  },

  async listVideoCatalog(workspaceId: string): Promise<VideoCatalogItem[]> {
    return (await this.listGenerationCatalog(workspaceId, "video")) as VideoCatalogItem[];
  },
};

export function isOpaqueExternalId(
  value: string | null | undefined,
  prefix: "wsp_" | "prj_" | "run_" | "ast_" | "outv_",
): boolean {
  return (
    typeof value === "string" &&
    new RegExp(`^${prefix}[A-Za-z0-9_-]{8,160}$`, "u").test(value)
  );
}
