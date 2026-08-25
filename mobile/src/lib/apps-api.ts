/**
 * Apps のモバイル API クライアント。
 *
 * モバイルは App の概要・関連・実行状況を扱う観察/操作 surface に限定する。
 * source import、ファイル書き込み、README/manifest 編集、Git、Release 作成、
 * Grant 管理、embedded runtime はここへ追加しない。
 */

import { fetchApi } from "./api-client";

export type AppPermission = "viewer" | "runner" | "developer" | "maintainer" | "admin";
export type AppBindingMode = "development" | "installed";
export type AppJobType = "build" | "test" | "run" | "package";

export interface AppTarget {
  id: string;
  app_id: string;
  target_key: string;
  display_name: string;
  surface: string;
  runtime: string;
  execution_host: string;
  entrypoint: string;
  manifest_snapshot?: Record<string, unknown>;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface AppArtifact {
  id: string;
  release_id: string;
  target_id: string;
  artifact_type: string;
  filename: string;
  sha256: string;
  size_bytes: number;
  created_at?: string | null;
}

export interface AppRelease {
  id: string;
  app_id: string;
  version: string;
  git_revision: string;
  manifest_hash: string;
  readme_hash: string;
  changelog?: string | null;
  status: "published" | "deprecated" | string;
  created_at?: string | null;
  artifacts?: AppArtifact[];
}

export interface AppSummary {
  id: string;
  owner_user_id?: string | null;
  origin_project_id?: string | null;
  name: string;
  slug: string;
  description?: string | null;
  visibility: string;
  default_target_key?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  archived_at?: string | null;
  permission?: AppPermission | string;
  targets?: AppTarget[];
  releases?: AppRelease[];
  related_project_ids?: string[];
}

export interface AppFile {
  path: string;
  filename?: string;
  name?: string;
  size_bytes?: number;
  size?: number;
  is_dir?: boolean;
  modified_at?: string | null;
  extension?: string;
  [key: string]: unknown;
}

export interface AppFileContent {
  path: string;
  content: string;
  sha256?: string;
}

export interface AppJob {
  id: string;
  app_id: string;
  target_id?: string | null;
  project_id?: string | null;
  release_id?: string | null;
  job_type: AppJobType | string;
  status: string;
  result_json?: Record<string, unknown>;
  exit_code?: number | null;
  started_at?: string | null;
  ended_at?: string | null;
}

export interface TaskAppLink {
  id: string;
  task_id: string;
  app_id: string;
  target_id?: string | null;
  relation_type: string;
  app?: AppSummary;
  target?: AppTarget | null;
}

export interface ProjectAppBinding {
  project_id: string;
  app_id: string;
  binding_mode: AppBindingMode;
  installed_release_id?: string | null;
  enabled: boolean;
  pinned: boolean;
  display_alias?: string | null;
  config_json?: Record<string, unknown>;
  capability_grants_json?: Record<string, unknown>;
  created_at?: string | null;
  updated_at?: string | null;
  permission?: AppPermission | string;
  app: AppSummary;
  targets?: AppTarget[];
  latest_release?: AppRelease | null;
  latest_job?: AppJob | null;
  latest_agent_run?: {
    id: string;
    status?: string;
    created_at?: string | null;
  } | null;
  incomplete_task_count?: number;
}

export interface AppContext {
  app: AppSummary;
  permission: AppPermission | string;
  target_key?: string | null;
  targets?: AppTarget[];
  releases?: AppRelease[];
  manifest?: Record<string, unknown>;
  manifest_hash?: string | null;
  readme: string;
  binding_mode?: AppBindingMode;
  selected_release?: AppRelease | null;
}

export interface CreateAppInput {
  name: string;
  slug?: string;
  description?: string;
  origin_project_id?: string | null;
  visibility?: "private" | "shared" | "public";
}

export interface ProjectAppInput {
  app_id: string;
  binding_mode: AppBindingMode;
  installed_release_id?: string | null;
  enabled?: boolean;
  pinned?: boolean;
  display_alias?: string | null;
}

export interface TaskAppInput {
  app_id: string;
  target_id?: string | null;
  relation_type?: string;
}

export class AppsOfflineMutationError extends Error {
  constructor() {
    super("オフラインでは Apps の変更を実行できません");
    this.name = "AppsOfflineMutationError";
  }
}

export class AppsPermissionError extends Error {
  constructor(message = "この Apps 操作には runner 権限が必要です") {
    super(message);
    this.name = "AppsPermissionError";
  }
}

const PERMISSION_RANK: Record<AppPermission, number> = {
  viewer: 10,
  runner: 20,
  developer: 30,
  maintainer: 40,
  admin: 50,
};

export function permissionAtLeast(
  permission: string | null | undefined,
  required: AppPermission,
): boolean {
  return (
    PERMISSION_RANK[permission as AppPermission] ?? 0
  ) >= PERMISSION_RANK[required];
}

export function assertRunnerPermission(permission: string | null | undefined): void {
  if (!permissionAtLeast(permission, "runner")) {
    throw new AppsPermissionError();
  }
}

function query(params: Record<string, string | null | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== null && value !== undefined && value !== "") {
      search.set(key, value);
    }
  }
  const encoded = search.toString();
  return encoded ? `?${encoded}` : "";
}

function arrayPayload<T>(payload: T[] | { [key: string]: unknown }, key: string): T[] {
  if (Array.isArray(payload)) return payload;
  const value = payload[key];
  return Array.isArray(value) ? (value as T[]) : [];
}

export const appsApi = {
  async list(projectId?: string | null): Promise<AppSummary[]> {
    const response = await fetchApi<{ apps?: AppSummary[] } | AppSummary[]>(
      `/api/apps${query({ project_id: projectId })}`,
    );
    return arrayPayload(response, "apps");
  },

  async get(appId: string, projectId?: string | null): Promise<AppSummary> {
    const response = await fetchApi<{ app: AppSummary } | AppSummary>(
      `/api/apps/${encodeURIComponent(appId)}${query({ project_id: projectId })}`,
    );
    if ("app" in response && response.app) return response.app;
    return response as AppSummary;
  },

  async create(input: CreateAppInput): Promise<AppSummary> {
    const response = await fetchApi<{ app: AppSummary }>("/api/apps", {
      method: "POST",
      body: JSON.stringify(input),
    });
    return response.app;
  },

  async update(
    appId: string,
    input: Partial<CreateAppInput> & { default_target_key?: string | null },
    projectId?: string | null,
  ): Promise<AppSummary> {
    const response = await fetchApi<{ app: AppSummary }>(
      `/api/apps/${encodeURIComponent(appId)}${query({ project_id: projectId })}`,
      { method: "PATCH", body: JSON.stringify(input) },
    );
    return response.app;
  },

  async archive(appId: string, projectId?: string | null): Promise<AppSummary | null> {
    const response = await fetchApi<{ app?: AppSummary; archived?: boolean }>(
      `/api/apps/${encodeURIComponent(appId)}${query({ project_id: projectId })}`,
      { method: "DELETE" },
    );
    return response.app ?? null;
  },

  async getContext(appId: string, projectId?: string | null): Promise<AppContext> {
    return fetchApi<AppContext>(
      `/api/apps/${encodeURIComponent(appId)}/context${query({ project_id: projectId })}`,
    );
  },

  async getTargets(appId: string, projectId?: string | null): Promise<AppTarget[]> {
    const response = await fetchApi<{ targets?: AppTarget[] } | AppTarget[]>(
      `/api/apps/${encodeURIComponent(appId)}/targets${query({ project_id: projectId })}`,
    );
    return arrayPayload(response, "targets");
  },

  async getFiles(appId: string, projectId?: string | null): Promise<AppFile[]> {
    const response = await fetchApi<{ files?: AppFile[] } | AppFile[]>(
      `/api/apps/${encodeURIComponent(appId)}/files${query({ project_id: projectId })}`,
    );
    return arrayPayload(response, "files");
  },

  async getFile(
    appId: string,
    path: string,
    projectId?: string | null,
  ): Promise<AppFileContent> {
    return fetchApi<AppFileContent>(
      `/api/apps/${encodeURIComponent(appId)}/files/content${query({
        path,
        project_id: projectId,
      })}`,
    );
  },

  async getReleases(appId: string, projectId?: string | null): Promise<AppRelease[]> {
    const response = await fetchApi<{ releases?: AppRelease[] } | AppRelease[]>(
      `/api/apps/${encodeURIComponent(appId)}/releases${query({ project_id: projectId })}`,
    );
    return arrayPayload(response, "releases");
  },

  async getJobs(appId: string, projectId?: string | null): Promise<AppJob[]> {
    const response = await fetchApi<{ jobs?: AppJob[] } | AppJob[]>(
      `/api/apps/${encodeURIComponent(appId)}/jobs${query({ project_id: projectId })}`,
    );
    return arrayPayload(response, "jobs");
  },

  async startJob(
    appId: string,
    input: {
      target_key: string;
      job_type: AppJobType;
      project_id?: string | null;
      input_json?: Record<string, unknown>;
    },
  ): Promise<AppJob> {
    const response = await fetchApi<{ job: AppJob }>(
      `/api/apps/${encodeURIComponent(appId)}/jobs`,
      { method: "POST", body: JSON.stringify(input) },
    );
    return response.job;
  },

  async stopJob(
    appId: string,
    jobId: string,
    projectId?: string | null,
  ): Promise<AppJob> {
    const response = await fetchApi<{ job: AppJob }>(
      `/api/apps/${encodeURIComponent(appId)}/jobs/${encodeURIComponent(jobId)}/stop${query({
        project_id: projectId,
      })}`,
      { method: "POST", body: JSON.stringify({}) },
    );
    return response.job;
  },

  async getJobLogs(
    appId: string,
    jobId: string,
    projectId?: string | null,
  ): Promise<{ job_id: string; logs: string }> {
    return fetchApi<{ job_id: string; logs: string }>(
      `/api/apps/${encodeURIComponent(appId)}/jobs/${encodeURIComponent(jobId)}/logs${query({
        project_id: projectId,
      })}`,
    );
  },

  async getProjectApps(projectId: string): Promise<ProjectAppBinding[]> {
    const response = await fetchApi<
      { project_id?: string; apps?: ProjectAppBinding[] } | ProjectAppBinding[]
    >(`/api/projects/${encodeURIComponent(projectId)}/apps`);
    return arrayPayload(response, "apps");
  },

  async linkProjectApp(
    projectId: string,
    input: ProjectAppInput,
  ): Promise<ProjectAppBinding> {
    const response = await fetchApi<{ binding: ProjectAppBinding }>(
      `/api/projects/${encodeURIComponent(projectId)}/apps`,
      { method: "POST", body: JSON.stringify(input) },
    );
    return response.binding;
  },

  async updateProjectApp(
    projectId: string,
    appId: string,
    input: Partial<ProjectAppInput>,
  ): Promise<ProjectAppBinding> {
    const response = await fetchApi<{ binding: ProjectAppBinding }>(
      `/api/projects/${encodeURIComponent(projectId)}/apps/${encodeURIComponent(appId)}`,
      { method: "PATCH", body: JSON.stringify(input) },
    );
    return response.binding;
  },

  async unlinkProjectApp(projectId: string, appId: string): Promise<void> {
    await fetchApi(
      `/api/projects/${encodeURIComponent(projectId)}/apps/${encodeURIComponent(appId)}`,
      { method: "DELETE" },
    );
  },

  async listTaskApps(taskId: string): Promise<TaskAppLink[]> {
    const response = await fetchApi<{ task_id?: string; apps?: TaskAppLink[] } | TaskAppLink[]>(
      `/api/tasks/${encodeURIComponent(taskId)}/apps`,
    );
    return arrayPayload(response, "apps");
  },

  async listAppTasks(
    appId: string,
    projectId: string,
    relationType?: string,
  ): Promise<Array<TaskAppLink & { task?: Record<string, unknown> }>> {
    const response = await fetchApi<{
      app_id?: string;
      project_id?: string;
      tasks?: Array<TaskAppLink & { task?: Record<string, unknown> }>;
    }>(
      `/api/apps/${encodeURIComponent(appId)}/tasks${query({
        project_id: projectId,
        relation_type: relationType,
      })}`,
    );
    return response.tasks ?? [];
  },

  async linkTaskApp(taskId: string, input: TaskAppInput): Promise<TaskAppLink> {
    const response = await fetchApi<{ link: TaskAppLink }>(
      `/api/tasks/${encodeURIComponent(taskId)}/apps`,
      { method: "POST", body: JSON.stringify(input) },
    );
    return response.link;
  },

  async unlinkTaskApp(
    taskId: string,
    appId: string,
    options?: { targetId?: string | null; relationType?: string | null },
  ): Promise<void> {
    await fetchApi(
      `/api/tasks/${encodeURIComponent(taskId)}/apps/${encodeURIComponent(appId)}${query({
        target_id: options?.targetId,
        relation_type: options?.relationType,
      })}`,
      { method: "DELETE" },
    );
  },
};
