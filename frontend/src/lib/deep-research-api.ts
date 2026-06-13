export type DeepResearchStatus =
  | "queued"
  | "running"
  | "completed"
  | "failed"
  | "cancelled";

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
  query: string;
  status: DeepResearchStatus;
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
  available: boolean;
};

export type StartDeepResearchRequest = {
  query: string;
  mode: "quick" | "detailed" | "report";
  max_iterations: number;
  questions_per_iteration: number;
  max_results_per_query: number;
  engines: string[];
  include_local_knowledge: boolean;
  project_id?: string | null;
};

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
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(detail.detail || res.statusText);
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

  markdownUrl(jobId: string) {
    return `/api/python-proxy/api/deep-research/jobs/${encodeURIComponent(
      jobId,
    )}/markdown`;
  },
};
