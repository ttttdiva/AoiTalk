import type { components } from "@/lib/api-types.gen";

type Schemas = components["schemas"];

/** These request properties have server-owned defaults in FastAPI. */
type OptionalDefaults<T, K extends keyof T> = Omit<T, K> &
  Partial<Pick<T, K>>;

export type ResearchFindingKind =
  Schemas["ResearchFindingKind"];

export type ResearchRoutine =
  Schemas["ResearchRoutineSummaryResponse"];
export type ResearchRoutineDetail =
  Schemas["ResearchRoutineDetailResponse"];
export type ResearchRoutineCreateInput =
  Schemas["ResearchRoutineCreateRequest"];

export type ResearchRun =
  Schemas["ResearchRunSummaryResponse"];
export type ResearchRunDetail =
  Schemas["ResearchRunDetailResponse"];
export type ResearchRunCreateInput = OptionalDefaults<
  Schemas["ResearchRunCreateRequest"],
  "status"
>;

export type ResearchFinding =
  Schemas["ResearchFindingResponse"];
export type ResearchFindingCreateInput =
  Schemas["ResearchFindingCreateRequest"];

export type EditorialProgram =
  Schemas["EditorialProgramSummaryResponse"];
export type EditorialProgramDetail =
  Schemas["EditorialProgramDetailResponse"];
export type EditorialProgramCreateInput =
  Schemas["EditorialProgramCreateRequest"];

export type ContentItem =
  Schemas["ContentItemSummaryResponse"];
export type ContentItemDetail =
  Schemas["ContentItemDetailResponse"];
export type ContentItemCreateInput = OptionalDefaults<
  Schemas["ContentItemCreateRequest"],
  "content_type"
>;

/**
 * A research candidate is source-backed, untrusted material.  Keep the
 * candidate API separate from ContentItem so the UI cannot accidentally treat
 * discovery as publication-ready content.
 */
export type ResearchCandidate = Schemas["ResearchCandidateResponse"];

export type ResearchCandidateStatus =
  | "discovered"
  | "triaged"
  | "accepted"
  | "rejected"
  | "expired"
  | "promoted";

/** Safe immutable decision projection returned by the decision-ledger route. */
export type ResearchCandidateDecision = {
  id: string;
  candidate_id: string;
  sequence: number;
  event_type: string;
  from_status: ResearchCandidateStatus | null;
  to_status: ResearchCandidateStatus;
  reason: string | null;
  candidate_hash: string;
  candidate_snapshot_hash: string;
  request_hash: string;
  actor_id: string | null;
  actor_type: string;
  content_item_id: string | null;
  decision_hash: string;
  prev_decision_hash: string | null;
  prev_event_hash: string | null;
  event_hash: string;
  decided_at: string;
  created_at: string;
};

export type ResearchCandidateDecisionPage = {
  candidate_id: string;
  current_status: ResearchCandidateStatus;
  current_decision_version: number;
  candidate_hash: string;
  items: ResearchCandidateDecision[];
  total: number;
  limit: number;
  offset: number;
  has_more: boolean;
};

export type ResearchCandidateTriageInput = {
  status: Extract<ResearchCandidateStatus, "triaged" | "accepted" | "rejected">;
  reason?: string | null;
  expected_status: Extract<ResearchCandidateStatus, "discovered" | "triaged" | "accepted">;
  expected_decision_version: number;
  expected_candidate_hash: string;
};

export type ResearchCandidatePromoteInput = {
  accepted_decision_id: string;
  expected_decision_version?: number | null;
  expected_decision_hash?: string | null;
  title?: string | null;
  brief?: string | null;
};


export class MediaResearchApiError extends Error {
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
    this.name = "MediaResearchApiError";
    this.status = options.status;
    this.detail = options.detail;
    this.body = options.body;
  }
}

function isRecord(
  value: unknown,
): value is Record<string, unknown> {
  return typeof value === "object" &&
    value !== null &&
    !Array.isArray(value);
}

async function parseResponse(
  response: Response,
): Promise<unknown> {
  if (response.status === 204) return undefined;

  const contentType =
    response.headers.get("content-type") ?? "";

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

  const response = await fetch(
    `/api/python-proxy${path}`,
    {
      ...init,
      cache: "no-store",
      credentials: "include",
      headers,
    },
  );

  const body = await parseResponse(response);

  if (!response.ok) {
    const record = isRecord(body) ? body : {};
    const detail =
      record.detail ??
      record.message ??
      record.error;

    const message =
      typeof detail === "string"
        ? detail
        : response.statusText ||
          `Media research request failed (${response.status})`;

    throw new MediaResearchApiError(message, {
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


export const mediaResearchApi = {
  async listRoutines(
    projectId?: string | null,
  ): Promise<ResearchRoutine[]> {
    const query = new URLSearchParams();
    if (projectId) query.set("project_id", projectId);
    const suffix = query.toString()
      ? `?${query.toString()}`
      : "";

    return request<ResearchRoutine[]>(
      `/operations/media/research-routines${suffix}`,
    );
  },

  async createRoutine(
    input: ResearchRoutineCreateInput,
    idempotencyKey: string,
  ): Promise<ResearchRoutineDetail> {
    return request<ResearchRoutineDetail>(
      "/operations/media/research-routines",
      {
        method: "POST",
        ...jsonBody(input),
      },
      idempotencyKey,
    );
  },

  async getRoutine(
    id: string,
  ): Promise<ResearchRoutineDetail> {
    return request<ResearchRoutineDetail>(
      `/operations/media/research-routines/${encodeId(id)}`,
    );
  },

  async listRuns(
    routineId?: string | null,
  ): Promise<ResearchRun[]> {
    const query = new URLSearchParams();
    if (routineId) {
      query.set(
        "research_routine_id",
        routineId,
      );
    }
    const suffix = query.toString()
      ? `?${query.toString()}`
      : "";

    return request<ResearchRun[]>(
      `/operations/media/research-runs${suffix}`,
    );
  },

  async startRun(
    input: ResearchRunCreateInput,
    idempotencyKey: string,
  ): Promise<ResearchRunDetail> {
    return request<ResearchRunDetail>(
      "/operations/media/research-runs",
      {
        method: "POST",
        ...jsonBody(input),
      },
      idempotencyKey,
    );
  },

  async getRun(
    id: string,
  ): Promise<ResearchRunDetail> {
    return request<ResearchRunDetail>(
      `/operations/media/research-runs/${encodeId(id)}`,
    );
  },

  async listFindings(
    runId: string,
  ): Promise<ResearchFinding[]> {
    return request<ResearchFinding[]>(
      `/operations/media/research-runs/${encodeId(runId)}/findings`,
    );
  },

  async appendFinding(
    runId: string,
    input: ResearchFindingCreateInput,
    idempotencyKey: string,
  ): Promise<ResearchFinding> {
    return request<ResearchFinding>(
      `/operations/media/research-runs/${encodeId(runId)}/findings`,
      {
        method: "POST",
        ...jsonBody(input),
      },
      idempotencyKey,
    );
  },

  async listPrograms(): Promise<EditorialProgram[]> {
    return request<EditorialProgram[]>(
      "/operations/media/editorial-programs",
    );
  },

  async createProgram(
    input: EditorialProgramCreateInput,
    idempotencyKey: string,
  ): Promise<EditorialProgramDetail> {
    return request<EditorialProgramDetail>(
      "/operations/media/editorial-programs",
      {
        method: "POST",
        ...jsonBody(input),
      },
      idempotencyKey,
    );
  },

  async listContentItems(
    programId?: string | null,
  ): Promise<ContentItem[]> {
    const query = new URLSearchParams();
    if (programId) {
      query.set(
        "editorial_program_id",
        programId,
      );
    }
    const suffix = query.toString()
      ? `?${query.toString()}`
      : "";

    return request<ContentItem[]>(
      `/operations/media/content-items${suffix}`,
    );
  },

  async createContentItem(
    input: ContentItemCreateInput,
    idempotencyKey: string,
  ): Promise<ContentItemDetail> {
    return request<ContentItemDetail>(
      "/operations/media/content-items",
      {
        method: "POST",
        ...jsonBody(input),
      },
      idempotencyKey,
    );
  },

  async getContentItem(
    id: string,
  ): Promise<ContentItemDetail> {
    return request<ContentItemDetail>(
      `/operations/media/content-items/${encodeId(id)}`,
    );
  },

  /**
   * List source-backed candidates in the authenticated scope.  Filters are
   * explicit so a caller never needs to infer project ownership from an
   * opaque candidate id.
   */
  async listCandidates(params?: {
    project_id?: string | null;
    research_run_id?: string | null;
    research_routine_id?: string | null;
    status?: ResearchCandidateStatus | null;
    limit?: number;
    offset?: number;
  }): Promise<ResearchCandidate[]> {
    const query = new URLSearchParams();
    if (params?.project_id) query.set("project_id", params.project_id);
    if (params?.research_run_id) {
      query.set("research_run_id", params.research_run_id);
    }
    if (params?.research_routine_id) {
      query.set("research_routine_id", params.research_routine_id);
    }
    if (params?.status) query.set("status", params.status);
    if (params?.limit !== undefined) query.set("limit", String(params.limit));
    if (params?.offset !== undefined) query.set("offset", String(params.offset));
    const suffix = query.toString() ? `?${query.toString()}` : "";

    return request<ResearchCandidate[]>(
      `/operations/media/research-candidates${suffix}`,
    );
  },

  async getCandidate(id: string): Promise<ResearchCandidate> {
    return request<ResearchCandidate>(
      `/operations/media/research-candidates/${encodeId(id)}`,
    );
  },

  /** Read-only immutable decision history for one candidate. */
  async listCandidateDecisions(
    candidateId: string,
    params?: { limit?: number; offset?: number },
  ): Promise<ResearchCandidateDecisionPage> {
    const query = new URLSearchParams();
    if (params?.limit !== undefined) query.set("limit", String(params.limit));
    if (params?.offset !== undefined) query.set("offset", String(params.offset));
    const suffix = query.toString() ? `?${query.toString()}` : "";

    return request<ResearchCandidateDecisionPage>(
      `/operations/media/research-candidates/${encodeId(candidateId)}/decisions${suffix}`,
    );
  },

  async triageCandidate(
    candidateId: string,
    input: ResearchCandidateTriageInput,
    idempotencyKey: string,
  ): Promise<ResearchCandidate> {
    return request<ResearchCandidate>(
      `/operations/media/research-candidates/${encodeId(candidateId)}/triage`,
      {
        method: "POST",
        ...jsonBody(input),
      },
      idempotencyKey,
    );
  },

  async promoteCandidate(
    candidateId: string,
    input: ResearchCandidatePromoteInput,
    idempotencyKey: string,
  ): Promise<ContentItemDetail> {
    return request<ContentItemDetail>(
      `/operations/media/research-candidates/${encodeId(candidateId)}/promote`,
      {
        method: "POST",
        ...jsonBody(input),
      },
      idempotencyKey,
    );
  },
};
