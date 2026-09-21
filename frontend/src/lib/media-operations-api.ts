import type { components } from "@/lib/api-types.gen";

type Schemas = components["schemas"];

/** OpenAPI emits server-defaulted fields as required.  Keep those fields
 * optional at the client boundary because the backend owns their defaults. */
type OptionalDefaults<T, K extends keyof T> = Omit<T, K> &
  Partial<Pick<T, K>>;

export type MediaPlatform = Schemas["MediaPlatform"];
export type MediaPersonaRevision = Schemas["PersonaRevisionResponse"];
export type MediaPersona = Schemas["PersonaSummaryResponse"];
export type MediaPersonaDetail = Schemas["PersonaDetailResponse"];
export type MediaPersonaIntake = Schemas["PersonaIntakeResponse"];
export type StoredArtifactPersonaResourceProvenance = {
  type: "stored_artifact";
  artifact_id: string;
};
export type MediaPersonaResource = Omit<
  Schemas["PersonaResourceResponse"],
  "provenance"
> & {
  provenance:
    | Schemas["PersonaResourceResponse"]["provenance"]
    | StoredArtifactPersonaResourceProvenance;
};

/**
 * Character Kernel uses the existing Media Persona identity as its durable
 * character id.  Keep the Persona aliases above for callers of the legacy
 * setup surface while exposing character-oriented names to the primary UI.
 */
export type MediaCharacter = MediaPersona;
export type MediaCharacterDetail = MediaPersonaDetail;
export type MediaCharacterRevision = MediaPersonaRevision;

export type PersonaCreateInput = OptionalDefaults<
  Schemas["PersonaCreateRequest"],
  "state"
>;
export type PersonaRevisionCreateInput =
  Schemas["PersonaRevisionCreateRequest"];
export type PersonaResourceCreateInput = Omit<
  Schemas["PersonaResourceCreateRequest"],
  "provenance"
> & {
  provenance:
    | Schemas["PersonaResourceCreateRequest"]["provenance"]
    | StoredArtifactPersonaResourceProvenance;
};

/** Character create payloads are revision fields only; there is no intake slot. */
export type CharacterCreateInput = PersonaRevisionCreateInput;
/** Legacy PUT now uses the same optimistic concurrency gate as PATCH. */
export type CharacterUpdateInput = Schemas["CharacterUpdateRequest"];

/**
 * Optimistic-concurrency PATCH payload for Character revisions.
 *
 * The three revision tokens are always sent.  Revision fields are optional so
 * callers can safely send only the keys changed by the user (including an
 * explicit `null`, `[]`, or `{}` clear).
 */
export type CharacterPatchInput = Schemas["MediaCharacterPatchRequest"];

export type ListCharactersParams = {
  project_id?: string | null;
  search?: string | null;
  limit?: number;
  offset?: number;
};

export type CharacterDashboardConnectedAccount = {
  id: string;
  platform: string;
  account_ref: string | null;
  status: string;
  /** Optional safe capability projection supplied by newer dashboard APIs. */
  capability_status?: string | null;
  /** Optional connection state alias; never contains credential material. */
  connection_status?: string | null;
  /** Per-operation status map; keys/values are already allow-listed by server. */
  capabilities?: Record<string, string | null> | null;
  /** Latest provider-observed operation statuses, kept separate from policy. */
  observed_capabilities?: Record<string, string | null> | null;
  /** Whether the server-owned adapter has been configured (not a secret). */
  adapter_ready?: boolean | null;
};

export type CharacterDashboardResearchCandidate = {
  id: string;
  title: string;
  summary: string | null;
  status: string;
  review_state?: string;
  reason?: string | null;
  candidate_hash?: string;
  decision_version?: number;
  content_item_id?: string | null;
  latest_decision?: Record<string, unknown> | null;
  discovered_at: string | null;
  expires_at: string | null;
};

export type CharacterDashboardRecipe = {
  id: string;
  name: string;
  created_at: string | null;
};

export type CharacterDashboardRun = {
  id: string;
  status: string;
  started_at: string | null;
  finished_at: string | null;
};

export type CharacterDashboardCalendarEntry = {
  id: string;
  kind: string;
  title: string;
  starts_at: string | null;
  status: string;
};

export type CharacterDashboardLearningItem = {
  id: string;
  title: string;
  proposal_type: string;
  status: string;
  created_at: string | null;
};

/**
 * Dashboard child collections use a closed page envelope while item payloads
 * remain a deliberately small, source-free projection.  Keep this type local
 * to the client instead of widening generated OpenAPI output by hand.
 */
export type CharacterDashboardPage<T> = {
  items?: T[];
  count: number;
  limit: number;
  offset: number;
  has_more: boolean;
};

export type CharacterDashboardMetricItem = {
  id: string;
  persona_ref?: string | null;
  platform?: string | null;
  platform_account_ref?: string | null;
  content_variant_ref?: string | null;
  publication_ref?: string | null;
  observed_at?: string | null;
  metrics?: Record<string, number>;
};

export type CharacterDashboardRevenueItem = {
  id: string;
  persona_ref?: string | null;
  platform_account_ref?: string | null;
  content_ref?: string | null;
  publication_ref?: string | null;
  platform?: string | null;
  currency: string;
  gross_amount: number;
  net_amount: number;
  event_at?: string | null;
};

export type CharacterDashboardExperimentItem = {
  id: string;
  name: string;
  status: string;
  persona_refs?: string[];
  account_refs?: string[];
  results?: Array<{
    id: string;
    status: string;
    sample_size: number;
    winner_variant_ref?: string | null;
    created_at?: string | null;
  }>;
  created_at?: string | null;
};

export type MediaCharacterDashboard = {
  character: MediaCharacter | MediaCharacterDetail;
  connected_accounts: CharacterDashboardConnectedAccount[];
  research_candidates: CharacterDashboardResearchCandidate[];
  generation: {
    recipes: CharacterDashboardRecipe[];
    runs: CharacterDashboardRun[];
  };
  calendar: CharacterDashboardCalendarEntry[];
  results: {
    snapshot_count: number;
    metrics: Record<string, unknown>;
    last_observed_at: string | null;
  };
  /** Character-scoped child collections (all ACL-filtered by the server). */
  metrics?: CharacterDashboardPage<CharacterDashboardMetricItem> | null;
  revenue?: CharacterDashboardPage<CharacterDashboardRevenueItem> | null;
  experiments?: CharacterDashboardPage<CharacterDashboardExperimentItem> | null;
  /** Legacy aliases emitted by the server alongside the top-level pages. */
  metrics_page?: CharacterDashboardPage<CharacterDashboardMetricItem> | null;
  revenue_page?: CharacterDashboardPage<CharacterDashboardRevenueItem> | null;
  experiments_page?: CharacterDashboardPage<CharacterDashboardExperimentItem> | null;
  learning: {
    count: number;
    pending_review_count: number;
    items: CharacterDashboardLearningItem[];
    page?: CharacterDashboardPage<CharacterDashboardLearningItem> | null;
  };
  learning_page?: CharacterDashboardPage<CharacterDashboardLearningItem> | null;
};

export class MediaOperationsApiError extends Error {
  readonly status: number;
  readonly detail?: unknown;
  readonly body?: unknown;

  constructor(
    message: string,
    options: {
      status: number;
      detail?: unknown;
      body?: unknown;
    },
  ) {
    super(message);
    this.name = "MediaOperationsApiError";
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
          `Media Operations API request failed (${response.status})`;

    throw new MediaOperationsApiError(message, {
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
    headers: {
      "content-type": "application/json",
    },
  };
}

function encodeId(id: string): string {
  return encodeURIComponent(id);
}

export const mediaOperationsApi = {
  async getPersonaIntake(
    projectId?: string | null,
  ): Promise<MediaPersonaIntake> {
    const query = new URLSearchParams();
    if (projectId) query.set("project_id", projectId);
    const suffix = query.toString() ? `?${query.toString()}` : "";

    return request<MediaPersonaIntake>(
      `/operations/media/persona-intake${suffix}`,
    );
  },

  async listPersonas(projectId?: string | null): Promise<MediaPersona[]> {
    const query = new URLSearchParams();
    if (projectId) query.set("project_id", projectId);
    const suffix = query.toString() ? `?${query.toString()}` : "";

    return request<MediaPersona[]>(`/operations/media/personas${suffix}`);
  },

  async listCharacters(
    params?: string | null | ListCharactersParams,
  ): Promise<MediaCharacter[]> {
    const query = new URLSearchParams();
    // Keep the old `listCharacters(projectId)` call shape working while the
    // primary Character panel uses typed pagination/search params.
    const options =
      typeof params === "string" || params === null || params === undefined
        ? { project_id: params }
        : params;
    if (options.project_id) query.set("project_id", options.project_id);
    if (options.search?.trim()) query.set("search", options.search.trim());
    if (options.limit !== undefined) query.set("limit", String(options.limit));
    if (options.offset !== undefined) query.set("offset", String(options.offset));
    const suffix = query.toString() ? `?${query.toString()}` : "";

    return request<MediaCharacter[]>(
      `/operations/media/characters${suffix}`,
    );
  },

  async createPersona(
    input: PersonaCreateInput,
    idempotencyKey: string,
  ): Promise<MediaPersonaDetail> {
    return request<MediaPersonaDetail>(
      "/operations/media/personas",
      {
        method: "POST",
        ...jsonBody(input),
      },
      idempotencyKey,
    );
  },

  async createCharacter(
    input: CharacterCreateInput,
    idempotencyKey: string,
  ): Promise<MediaCharacterDetail> {
    return request<MediaCharacterDetail>(
      "/operations/media/characters",
      {
        method: "POST",
        ...jsonBody(input),
      },
      idempotencyKey,
    );
  },

  async getPersona(id: string): Promise<MediaPersonaDetail> {
    return request<MediaPersonaDetail>(
      `/operations/media/personas/${encodeId(id)}`,
    );
  },

  async getCharacter(id: string): Promise<MediaCharacterDetail> {
    return request<MediaCharacterDetail>(
      `/operations/media/characters/${encodeId(id)}`,
    );
  },

  async updateCharacter(
    id: string,
    input: CharacterUpdateInput,
    idempotencyKey?: string,
  ): Promise<MediaCharacterDetail> {
    return request<MediaCharacterDetail>(
      `/operations/media/characters/${encodeId(id)}`,
      {
        method: "PUT",
        ...jsonBody(input),
      },
      idempotencyKey,
    );
  },

  /**
   * Apply a sparse Character revision patch.  `input` must include all three
   * expected revision tokens; the backend uses them to reject stale writes.
   */
  async patchCharacter(
    id: string,
    input: CharacterPatchInput,
    idempotencyKey: string,
  ): Promise<MediaCharacterDetail> {
    return request<MediaCharacterDetail>(
      `/operations/media/characters/${encodeId(id)}`,
      {
        method: "PATCH",
        ...jsonBody(input),
      },
      idempotencyKey,
    );
  },

  async getCharacterDashboard(
    id: string,
  ): Promise<MediaCharacterDashboard> {
    return request<MediaCharacterDashboard>(
      `/operations/media/characters/${encodeId(id)}/dashboard`,
    );
  },

  async appendPersonaRevision(
    id: string,
    input: PersonaRevisionCreateInput,
    idempotencyKey: string,
  ): Promise<MediaPersonaRevision> {
    return request<MediaPersonaRevision>(
      `/operations/media/personas/${encodeId(id)}/revisions`,
      {
        method: "POST",
        ...jsonBody(input),
      },
      idempotencyKey,
    );
  },

  async listPersonaResources(
    id: string,
    params?: {
      limit?: number;
      offset?: number;
    },
  ): Promise<MediaPersonaResource[]> {
    const query = new URLSearchParams();

    if (params?.limit !== undefined) {
      query.set("limit", String(params.limit));
    }
    if (params?.offset !== undefined) {
      query.set("offset", String(params.offset));
    }

    const suffix = query.toString() ? `?${query.toString()}` : "";

    return request<MediaPersonaResource[]>(
      `/operations/media/personas/${encodeId(id)}/resources${suffix}`,
    );
  },

  async attachPersonaResource(
    id: string,
    input: PersonaResourceCreateInput,
    idempotencyKey: string,
  ): Promise<MediaPersonaResource> {
    return request<MediaPersonaResource>(
      `/operations/media/personas/${encodeId(id)}/resources`,
      {
        method: "POST",
        ...jsonBody(input),
      },
      idempotencyKey,
    );
  },
};
