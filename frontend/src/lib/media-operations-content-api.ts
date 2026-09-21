import { MediaResearchApiError } from "@/lib/media-operations-research-api";

/**
 * Platform variants deliberately use a closed discriminated union.  The
 * browser never accepts a raw JSON blob for a variant; each platform has its
 * own small, reviewable payload shape.
 */
export type MediaContentPlatform =
  | "x"
  | "pixiv"
  | "dlsite"
  | "patreon"
  | "youtube"
  | "instagram";

export type XContentPayload = {
  type: "x_post";
  text: string;
  media?: string[];
  alt_text?: string;
  links?: string[];
  hashtags?: string[];
  sensitive_content?: boolean;
  reply_to?: string | null;
  scheduled_at?: string | null;
};

export type XThreadContentPayload = {
  type: "x_thread";
  posts: Array<{
    text: string;
    media?: string[];
    alt_text?: string;
  }>;
  scheduled_at?: string | null;
  reply_to?: string | null;
};

export type PixivContentPayload = {
  type: "pixiv_work";
  title: string;
  caption: string;
  tags: string[];
  media: string[];
  ai_generated?: boolean;
  rating?: string;
  r18?: boolean;
  r18g?: boolean;
  series_id?: string | null;
};

export type DlsiteContentPayload = {
  type: "dlsite_release";
  title: string;
  description: string;
  category: string;
  age_rating: string;
  price: number;
  sales: {
    currency: string;
    tax_included: boolean;
    distribution: string;
  };
  preview_assets: string[];
  deliverable_package_ref: string;
  rights_checklist: Array<{
    code: string;
    status: "passed" | "failed" | "not_run" | "review_required";
    note: string | null;
  }>;
  thumbnail_assets?: string[];
};

export type PatreonContentPayload = {
  type: "patreon_post";
  audience: "public" | "paid" | "tier";
  title: string;
  body: string;
  public_preview?: string;
  attachments?: string[];
  tier_refs?: string[];
  scheduled_at?: string | null;
};

export type YoutubeContentPayload = {
  type: "youtube_video" | "youtube_short";
  title: string;
  description: string;
  tags: string[];
  media_asset: string[];
  visibility: string;
  thumbnail?: string[];
  captions?: string[];
  scheduled_at?: string | null;
  audience?: string;
  disclosure?: string;
};

export type InstagramContentPayload = {
  type: "instagram_feed" | "instagram_carousel" | "instagram_reel";
  caption: string;
  media: string[];
  alt_text?: string;
  scheduled_at?: string | null;
  cover?: string[];
};

// Names mirror the backend OpenAPI component names for consumers that prefer
// platform-specific imports over the aggregate union.
export type XPostContentPayload = XContentPayload;
export type YouTubeContentPayload = YoutubeContentPayload;

export type PlatformContentPayload =
  | XContentPayload
  | XThreadContentPayload
  | PixivContentPayload
  | DlsiteContentPayload
  | PatreonContentPayload
  | YoutubeContentPayload
  | InstagramContentPayload;

export type ContentVariantStatus =
  | "draft"
  | "ready"
  | "blocked"
  | "publishable"
  | string;

export type AssessmentResult = "passed" | "failed" | "review_required" | string;

export type RightsAssessmentResult =
  | "cleared"
  | "blocked"
  | "review_required"
  | "unknown"
  | string;

export type ContentVariantRevision = {
  id: string;
  content_variant_id: string;
  version: number;
  content_item_id: string;
  content_item_hash: string;
  persona_revision_id: string;
  persona_revision_hash: string;
  platform_account_id: string;
  platform_account_revision_id?: string | null;
  platform_account_revision_hash?: string | null;
  platform: MediaContentPlatform;
  payload: PlatformContentPayload;
  generation_output_refs: Array<{
    generation_output_id: string;
    sha256?: string | null;
  }>;
  source_evidence: Array<{
    type: "url" | "artifact";
    url?: string;
    sha256?: string;
    mime_type?: string;
    label?: string | null;
    note?: string | null;
  }>;
  content_hash: string;
  created_at?: string | null;
  [key: string]: unknown;
};

export type QAAssessment = {
  id: string;
  content_variant_revision_id: string;
  policy_revision_id?: string | null;
  policy_revision_hash?: string | null;
  result: AssessmentResult;
  checks: AssessmentCheck[];
  findings: AssessmentFinding[];
  evidence: AssessmentEvidence[];
  created_at?: string | null;
  [key: string]: unknown;
};

export type RightsAssessment = {
  id: string;
  content_variant_revision_id: string;
  result: RightsAssessmentResult;
  checks: AssessmentCheck[];
  findings: AssessmentFinding[];
  evidence: AssessmentEvidence[];
  created_at?: string | null;
  [key: string]: unknown;
};

export type QaResult = QAAssessment;
export type RightsRecord = RightsAssessment;

export type ContentVariant = {
  id: string;
  content_item_id: string;
  content_item_hash?: string | null;
  persona_revision_id?: string | null;
  persona_revision_hash?: string | null;
  platform_account_id?: string | null;
  platform_account_revision_id?: string | null;
  platform_account_revision_hash?: string | null;
  platform: MediaContentPlatform;
  status: ContentVariantStatus;
  current_revision?: ContentVariantRevision | null;
  revisions?: ContentVariantRevision[];
  qa_assessments?: QAAssessment[];
  rights_assessments?: RightsAssessment[];
  created_at?: string | null;
  [key: string]: unknown;
};

export type ContentVariantCreateInput = {
  content_item_id: string;
  persona_revision_id: string;
  platform_account_id?: string | null;
  platform_account_revision_id?: string | null;
  platform: MediaContentPlatform;
  payload: PlatformContentPayload;
  generation_output_refs?: Array<{
    generation_output_id: string;
    sha256?: string | null;
  }>;
  source_evidence?: Array<{
    type: "url" | "artifact";
    url?: string;
    sha256?: string;
    mime_type?: string;
    label?: string | null;
    note?: string | null;
  }>;
};

export type ContentVariantRevisionInput = Omit<
  ContentVariantCreateInput,
  "content_item_id" | "platform"
> & {
  expected_version: number;
};

export type AssessmentCheck = {
  code: string;
  status: "passed" | "failed" | "not_run";
  mandatory: boolean;
  message: string | null;
};

export type AssessmentFinding = {
  code: string;
  severity: "info" | "warning" | "error";
  message: string;
};

export type AssessmentEvidence = {
  type: "url" | "artifact";
  url?: string;
  sha256?: string;
  mime_type?: string;
  label?: string | null;
  note?: string | null;
};

export type QAAssessmentInput = {
  variant_revision_id: string;
  policy_revision_id?: string | null;
  policy_revision_hash?: string | null;
  result: Exclude<AssessmentResult, string> | AssessmentResult;
  checks: AssessmentCheck[];
  findings: AssessmentFinding[];
  evidence: AssessmentEvidence[];
};

export type RightsAssessmentInput = {
  variant_revision_id: string;
  result: RightsAssessmentResult;
  policy_revision_id?: string | null;
  policy_revision_hash?: string | null;
  checks: AssessmentCheck[];
  findings: AssessmentFinding[];
  evidence: AssessmentEvidence[];
};

export type VariantReadiness = {
  content_variant_id: string;
  revision_id?: string | null;
  revision_hash?: string | null;
  ready: boolean;
  status: ContentVariantStatus;
  publication_allowed?: boolean;
  qa_result?: AssessmentResult | null;
  rights_result?: RightsAssessmentResult | null;
  blockers?: string[];
  blocking_reasons: string[];
  qa?: QAAssessment | null;
  rights?: RightsAssessment | null;
  [key: string]: unknown;
};

export class MediaContentApiError extends MediaResearchApiError {
  override readonly name = "MediaContentApiError";
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
          `Media content request failed (${response.status})`;
    throw new MediaContentApiError(message, {
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

function querySuffix(
  params: Record<string, string | null | undefined>,
): string {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value) query.set(key, value);
  }
  const encoded = query.toString();
  return encoded ? `?${encoded}` : "";
}

/** Credential-free client for the immutable ContentVariant / QA boundary. */
export const mediaContentApi = {
  async listVariants(options: {
    contentItemId?: string | null;
    platform?: MediaContentPlatform | null;
    projectId?: string | null;
  } = {}): Promise<ContentVariant[]> {
    return request<ContentVariant[]>(
      `/operations/media/content-variants${querySuffix({
        content_item_id: options.contentItemId,
        platform: options.platform,
        project_id: options.projectId,
      })}`,
    );
  },

  async createVariant(
    input: ContentVariantCreateInput,
    idempotencyKey: string,
  ): Promise<ContentVariant> {
    return request<ContentVariant>(
      "/operations/media/content-variants",
      { method: "POST", ...jsonBody(input) },
      idempotencyKey,
    );
  },

  /** Nested alias used by ContentItem detail surfaces. */
  async createVariantForContentItem(
    contentItemId: string,
    input: Omit<ContentVariantCreateInput, "content_item_id">,
    idempotencyKey: string,
  ): Promise<ContentVariant> {
    return request<ContentVariant>(
      `/operations/media/content-items/${encodeId(contentItemId)}/variants`,
      { method: "POST", ...jsonBody(input) },
      idempotencyKey,
    );
  },

  async getVariant(id: string): Promise<ContentVariant> {
    return request<ContentVariant>(
      `/operations/media/content-variants/${encodeId(id)}`,
    );
  },

  async listRevisions(variantId: string): Promise<ContentVariantRevision[]> {
    return request<ContentVariantRevision[]>(
      `/operations/media/content-variants/${encodeId(variantId)}/revisions`,
    );
  },

  async getRevision(revisionId: string): Promise<ContentVariantRevision> {
    return request<ContentVariantRevision>(
      `/operations/media/content-variant-revisions/${encodeId(revisionId)}`,
    );
  },

  async appendRevision(
    variantId: string,
    input: ContentVariantRevisionInput,
    idempotencyKey: string,
  ): Promise<ContentVariantRevision> {
    return request<ContentVariantRevision>(
      `/operations/media/content-variants/${encodeId(variantId)}/revisions`,
      { method: "POST", ...jsonBody(input) },
      idempotencyKey,
    );
  },

  async recordQa(
    input: QAAssessmentInput,
    idempotencyKey: string,
  ): Promise<QAAssessment> {
    return request<QAAssessment>(
      `/operations/media/content-variant-revisions/${encodeId(input.variant_revision_id)}/qa`,
      {
        method: "POST",
        ...jsonBody({
          policy_revision_id: input.policy_revision_id ?? null,
          policy_revision_hash: input.policy_revision_hash ?? null,
          result: input.result,
          checks: input.checks,
          findings: input.findings,
          evidence: input.evidence,
        }),
      },
      idempotencyKey,
    );
  },

  async recordRights(
    input: RightsAssessmentInput,
    idempotencyKey: string,
  ): Promise<RightsAssessment> {
    return request<RightsAssessment>(
      `/operations/media/content-variant-revisions/${encodeId(input.variant_revision_id)}/rights`,
      {
        method: "POST",
        ...jsonBody({
          result: input.result,
          policy_revision_id: input.policy_revision_id ?? null,
          policy_revision_hash: input.policy_revision_hash ?? null,
          checks: input.checks,
          findings: input.findings,
          evidence: input.evidence,
        }),
      },
      idempotencyKey,
    );
  },

  async getReadiness(variantId: string): Promise<VariantReadiness> {
    return request<VariantReadiness>(
      `/operations/media/content-variants/${encodeId(variantId)}/readiness`,
    );
  },
};

// Keep a descriptive alias alongside the shorter name used by existing
// MediaOps clients.  This makes the boundary easy to discover without adding
// a second implementation.
export const mediaOperationsContentApi = mediaContentApi;
