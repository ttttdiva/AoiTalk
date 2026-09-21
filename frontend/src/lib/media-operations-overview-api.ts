export type MediaCalendarReference = {
  type: string;
  id: string;
};

export type MediaCalendarEvent = {
  id: string;
  source: string;
  kind: string;
  title: string;
  starts_at: string;
  ends_at?: string | null;
  status: string;
  platform?: string | null;
  persona_label?: string | null;
  requires_human_action: boolean;
  reference: MediaCalendarReference;
};

export type MediaCalendarResponse = {
  start: string;
  end: string;
  items: MediaCalendarEvent[];
  total: number;
  has_more: boolean;
};

export type MediaMetricPlatformSummary = {
  platform: string;
  snapshot_count: number;
  metrics: Record<string, number>;
  last_observed_at?: string | null;
};

export type MediaMetricPersonaSummary = {
  persona_ref: string;
  persona_label?: string | null;
  snapshot_count: number;
  metrics: Record<string, number>;
};

export type MediaMetricAccountSummary = {
  account_ref: string;
  account_label?: string | null;
  snapshot_count: number;
  metrics: Record<string, number>;
};

export type MediaMetricContentSummary = {
  content_ref: string;
  content_label?: string | null;
  snapshot_count: number;
  metrics: Record<string, number>;
};

export type MediaResultsResponse = {
  start: string;
  end: string;
  metric_snapshots: {
    count: number;
    by_platform: MediaMetricPlatformSummary[];
    by_persona: MediaMetricPersonaSummary[];
    by_account: MediaMetricAccountSummary[];
    by_content: MediaMetricContentSummary[];
  };
  revenue: {
    event_count: number;
    by_currency: Array<{
      currency: string;
      event_count: number;
      gross: number;
      net: number;
    }>;
  };
  experiments: {
    count: number;
    by_status: Record<string, number>;
    result_count: number;
  };
  learning: {
    count: number;
    by_status: Record<string, number>;
    pending_review_count: number;
  };
  evidence_count: number;
};

export class MediaOverviewApiError extends Error {
  readonly status: number;
  readonly detail?: unknown;
  readonly body?: unknown;

  constructor(message: string, options: { status: number; detail?: unknown; body?: unknown }) {
    super(message);
    this.name = "MediaOverviewApiError";
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

async function request<T>(path: string): Promise<T> {
  const response = await fetch(`/api/python-proxy${path}`, {
    cache: "no-store",
    credentials: "include",
  });
  const body = await parseResponse(response);
  if (!response.ok) {
    const record = isRecord(body) ? body : {};
    const detail = record.detail ?? record.message ?? record.error;
    const message = typeof detail === "string" ? detail : response.statusText || `Media overview request failed (${response.status})`;
    throw new MediaOverviewApiError(message, { status: response.status, detail, body });
  }
  return body as T;
}

function queryString(projectId?: string | null): string {
  const query = new URLSearchParams();
  if (projectId) query.set("project_id", projectId);
  const rendered = query.toString();
  return rendered ? `?${rendered}` : "";
}

export const mediaOverviewApi = {
  listCalendar(projectId?: string | null): Promise<MediaCalendarResponse> {
    return request<MediaCalendarResponse>(`/operations/media/calendar${queryString(projectId)}`);
  },
  getResults(projectId?: string | null): Promise<MediaResultsResponse> {
    return request<MediaResultsResponse>(`/operations/media/results${queryString(projectId)}`);
  },
};
