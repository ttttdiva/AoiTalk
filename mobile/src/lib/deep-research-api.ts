import { fetchApi } from "./api-client";
import { CHAT_TIMEOUT } from "../constants/config";

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
  sources: Array<{
    id: number;
    title: string;
    url: string;
    snippet: string;
    engine: string;
    query: string;
    published_at?: string | null;
  }>;
  report_markdown: string;
  metadata: Record<string, unknown>;
};

export type StartDeepResearchRequest = {
  query: string;
  mode: "quick" | "detailed" | "report";
  max_iterations: number;
  questions_per_iteration: number;
  max_results_per_query: number;
  engines: string[];
  include_local_rag: boolean;
  project_id?: string | null;
};

export const deepResearchApi = {
  listJobs(limit = 20): Promise<{ jobs: DeepResearchJob[] }> {
    return fetchApi(
      `/api/deep-research/jobs?limit=${encodeURIComponent(String(limit))}`,
      {},
      CHAT_TIMEOUT,
    );
  },

  startJob(payload: StartDeepResearchRequest): Promise<DeepResearchJob> {
    return fetchApi(
      "/api/deep-research/jobs",
      { method: "POST", body: JSON.stringify(payload) },
      CHAT_TIMEOUT,
    );
  },

  getJob(jobId: string): Promise<DeepResearchJob> {
    return fetchApi(
      `/api/deep-research/jobs/${encodeURIComponent(jobId)}`,
      {},
      CHAT_TIMEOUT,
    );
  },
};
