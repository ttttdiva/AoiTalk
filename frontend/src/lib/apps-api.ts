export type AppPermission = "viewer" | "runner" | "developer" | "maintainer" | "admin";

export type AppArchiveExclusions = {
  git?: boolean;
  dependencies?: boolean;
  runtime?: boolean;
  credentials?: boolean;
};
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

export interface AppGrant {
  id: string;
  app_id: string;
  user_id?: string | null;
  project_id?: string | null;
  permission: AppPermission | string;
  created_by?: string | null;
  created_at?: string | null;
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
  latest_agent_run?: { id: string; status?: string; created_at?: string | null } | null;
  incomplete_task_count?: number;
  /**
   * development binding の git 状態。App 1件ごとに git プロセスが起動するため、
   * `GET /projects/{project_id}/apps` では `?with_git=1` を付けたときだけ返る。
   * 固定Release binding の合成statusは常時返る。
   * undefined は「変更なし」ではなく「未取得」を意味するので、表示側で区別すること。
   */
  git_status?: AppGitStatus;
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

export interface AppOverviewAnalysis {
  purpose?: string;
  audience?: string;
  input?: { label?: string; detail?: string };
  process?: { label?: string; detail?: string };
  output?: { label?: string; detail?: string };
  steps?: string[];
  capabilities?: string[];
  limitations?: string[];
  evidence_files?: string[];
  method?: "llm" | "heuristic" | "starter" | string;
  confidence?: number;
  analyzed_at?: string;
  analysis_version?: number;
  targets?: Record<string, {
    purpose?: string;
    input?: { label?: string; detail?: string };
    output?: { label?: string; detail?: string };
    steps?: string[];
    constraints?: string[];
    evidence_files?: string[];
    confidence?: number;
  }>;
}

export interface AppGitStatus {
  branch?: string | null;
  revision?: string | null;
  clean?: boolean;
  files?: Array<{ path?: string; code?: string; status?: string; [key: string]: unknown }>;
  [key: string]: unknown;
}

export interface AppGitHistoryEntry {
  revision?: string;
  message?: string;
  author?: string;
  date?: string;
  [key: string]: unknown;
}

export interface AppFileContent {
  path: string;
  content: string;
  sha256: string;
}

export interface AppSourceImportFile {
  file: File;
  relativePath: string;
}

export interface AppSourceImportFileChange {
  path: string;
  size_bytes?: number | null;
  previous_size_bytes?: number | null;
  sha256?: string | null;
  previous_sha256?: string | null;
  hash?: string | null;
  reason?: string | null;
  rejection_reason?: string | null;
  [key: string]: unknown;
}

export interface AppSourceImportWarning {
  category?: string;
  message?: string;
  path?: string | null;
  [key: string]: unknown;
}

export interface AppSourceImportPreview {
  import_id: string;
  base_revision?: string | null;
  expires_at?: string | null;
  expected_revision?: string | null;
  current_revision?: string | null;
  revision?: string | null;
  added?: AppSourceImportFileChange[];
  modified?: AppSourceImportFileChange[];
  deleted?: AppSourceImportFileChange[];
  unchanged?: AppSourceImportFileChange[];
  rejected?: AppSourceImportFileChange[];
  changes?: Partial<Record<"added" | "modified" | "deleted" | "unchanged" | "rejected", AppSourceImportFileChange[]>>;
  summary?: Partial<Record<"added" | "modified" | "deleted" | "unchanged" | "rejected", number>>;
  warnings?: AppSourceImportWarning[] | Record<string, unknown>;
  current_files?: string[];
  [key: string]: unknown;
}

export interface AppSourceImportApplyInput {
  expected_revision: string;
  delete_paths?: string[];
}

export interface AppSourceImportApplyResult {
  success: boolean;
  import_id?: string;
  revision?: string | null;
  [key: string]: unknown;
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

const API_PREFIX = "/api/python-proxy";

export class AppsApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
    public readonly detail?: unknown,
  ) {
    super(message);
    this.name = "AppsApiError";
  }
}

async function appsApiError(response: Response): Promise<AppsApiError> {
  const detail = await response.json().catch(() => ({ detail: response.statusText }));
  const message = typeof detail?.detail === "string"
    ? detail.detail
    : typeof detail?.message === "string"
      ? detail.message
      : response.statusText;
  return new AppsApiError(message || `API Error: ${response.status}`, response.status, detail);
}

export async function appsApiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_PREFIX}${path}`, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...init?.headers },
    ...init,
  });
  if (!response.ok) {
    throw await appsApiError(response);
  }
  return response.json() as Promise<T>;
}

export async function appsApiFetchFormData<T>(path: string, body: FormData): Promise<T> {
  const response = await fetch(`${API_PREFIX}${path}`, {
    credentials: "include",
    method: "POST",
    body,
  });
  if (!response.ok) {
    throw await appsApiError(response);
  }
  return response.json() as Promise<T>;
}

const json = (body: unknown): RequestInit => ({
  method: "POST",
  body: JSON.stringify(body),
});

export const appsApi = {
  list: (projectId?: string) =>
    appsApiFetch<{ apps: AppSummary[] }>(
      `/apps${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ""}`,
    ),
  get: (appId: string, projectId?: string) => appsApiFetch<{ app: AppSummary }>(`/apps/${appId}${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ""}`),
  create: (input: CreateAppInput) => appsApiFetch<{ app: AppSummary }>("/apps", json(input)),
  importProjectSource: (input: CreateAppInput & { project_id: string; source_path: string }) =>
    appsApiFetch<{ app: AppSummary; imported_files: string[] }>("/apps/import-project-source", json(input)),
  update: (appId: string, input: Partial<CreateAppInput> & { default_target_key?: string | null }, projectId?: string) =>
    appsApiFetch<{ app: AppSummary }>(`/apps/${appId}${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ""}`, { ...json(input), method: "PATCH" }),
  getContext: (appId: string, projectId?: string) => appsApiFetch<AppContext>(`/apps/${appId}/context${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ""}`),
  analyze: (appId: string, input: { expected_manifest_sha256?: string } = {}, projectId?: string) =>
    appsApiFetch<{
      success: boolean;
      analysis: AppOverviewAnalysis;
      manifest: Record<string, unknown>;
      manifest_hash: string;
      readme: string;
      revision?: string | null;
    }>(
      `/apps/${appId}/analysis${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ""}`,
      json(input),
    ),
  getTargets: (appId: string, projectId?: string) => appsApiFetch<{ targets: AppTarget[] }>(`/apps/${appId}/targets${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ""}`),
  getFiles: (appId: string, projectId?: string) => appsApiFetch<{ files: AppFile[] }>(`/apps/${appId}/files${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ""}`),
  previewSourceImport: (appId: string, input: { files: AppSourceImportFile[]; expected_revision?: string | null; root_mode?: "strip_common" | "preserve" }, projectId?: string) => {
    const formData = new FormData();
    input.files.forEach(({ file, relativePath }) => {
      formData.append("files", file, file.name);
      formData.append("relative_paths", relativePath);
    });
    formData.append("expected_revision", input.expected_revision || "");
    formData.append("root_mode", input.root_mode || "strip_common");
    return appsApiFetchFormData<AppSourceImportPreview>(
      `/apps/${appId}/source-imports/preview?project_id=${encodeURIComponent(projectId || "")}`,
      formData,
    );
  },
  applySourceImport: (appId: string, importId: string, input: AppSourceImportApplyInput, projectId?: string) =>
    appsApiFetch<AppSourceImportApplyResult>(
      `/apps/${appId}/source-imports/${encodeURIComponent(importId)}/apply?project_id=${encodeURIComponent(projectId || "")}`,
      { method: "POST", body: JSON.stringify(input) },
    ),
  getFile: (appId: string, path: string, projectId?: string) =>
    appsApiFetch<AppFileContent>(`/apps/${appId}/files/content?path=${encodeURIComponent(path)}${projectId ? `&project_id=${encodeURIComponent(projectId)}` : ""}`),
  downloadFile: (appId: string, path: string, projectId?: string) =>
    `${API_PREFIX}/apps/${encodeURIComponent(appId)}/files/download?path=${encodeURIComponent(path)}${projectId ? `&project_id=${encodeURIComponent(projectId)}` : ""}`,
  /**
   * App一式のzip URL。既定は全階層・除外なしで、GUIで選択された範囲とカテゴリだけをqueryへ載せる。
   * 一括取得なのでバックエンドは runner 以上を要求する。
   */
  downloadArchive: (appId: string, projectId?: string, exclusions: AppArchiveExclusions = {}, includePaths?: string[] | null) => {
    const query = new URLSearchParams();
    if (projectId) query.set("project_id", projectId);
    if (includePaths !== undefined && includePaths !== null) query.set("include_paths", JSON.stringify(includePaths));
    if (exclusions.git) query.set("exclude_git", "true");
    if (exclusions.dependencies) query.set("exclude_dependencies", "true");
    if (exclusions.runtime) query.set("exclude_runtime", "true");
    if (exclusions.credentials) query.set("exclude_credentials", "true");
    const suffix = query.toString();
    return `${API_PREFIX}/apps/${encodeURIComponent(appId)}/files/archive${suffix ? `?${suffix}` : ""}`;
  },
  writeFile: (appId: string, input: { path: string; content: string; expected_sha256?: string }, projectId?: string) =>
    appsApiFetch<{ success: boolean; path: string; sha256: string; revision?: string | null }>(
      `/apps/${appId}/files/content${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ""}`,
      { method: "PUT", body: JSON.stringify(input) },
    ),
  deleteFile: (appId: string, path: string, projectId?: string) =>
    appsApiFetch<{ success: boolean; path: string }>(
      `/apps/${appId}/files/content?path=${encodeURIComponent(path)}${projectId ? `&project_id=${encodeURIComponent(projectId)}` : ""}`,
      { method: "DELETE" },
    ),
  updateReadme: (appId: string, content: string, expected_sha256?: string, projectId?: string) =>
    appsApiFetch<{ success: boolean; sha256: string; node_id: string }>(
      `/apps/${appId}/readme${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ""}`,
      { method: "PUT", body: JSON.stringify({ content, expected_sha256 }) },
    ),
  validateManifest: (appId: string, content?: string, projectId?: string) =>
    appsApiFetch<{ valid: boolean; manifest?: Record<string, unknown>; errors?: string[]; warnings?: string[] }>(
      `/apps/${appId}/manifest/validate${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ""}`,
      json(content === undefined ? {} : { content }),
    ),
  updateManifest: (appId: string, content: string, expected_sha256?: string, projectId?: string) =>
    appsApiFetch<{ success: boolean; manifest_hash: string }>(
      `/apps/${appId}/manifest${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ""}`,
      { method: "PUT", body: JSON.stringify({ content, expected_sha256 }) },
    ),
  getGitStatus: (appId: string, projectId?: string) => appsApiFetch<AppGitStatus>(`/apps/${appId}/git/status${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ""}`),
  getGitHistory: (appId: string, projectId?: string) =>
    appsApiFetch<{ history: AppGitHistoryEntry[] }>(`/apps/${appId}/git/history${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ""}`),
  restoreGitFile: (appId: string, path: string, revision: string, projectId?: string) =>
    appsApiFetch<{ success: boolean; path: string; restored_from_revision: string; revision?: string | null }>(
      `/apps/${appId}/git/restore${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ""}`,
      { method: "POST", body: JSON.stringify({ path, revision }) },
    ),
  restoreGitRevision: (appId: string, revision: string, projectId?: string) =>
    appsApiFetch<{ success: boolean; path: string; restored_from_revision: string; revision?: string | null }>(
      `/apps/${appId}/git/restore${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ""}`,
      { method: "POST", body: JSON.stringify({ revision }) },
    ),
  getJobs: (appId: string, projectId?: string) => appsApiFetch<{ jobs: AppJob[] }>(`/apps/${appId}/jobs${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ""}`),
  createJob: (appId: string, input: { target_key: string; job_type: AppJobType; project_id?: string | null; input_json?: Record<string, unknown> }) =>
    appsApiFetch<{ job: AppJob }>(`/apps/${appId}/jobs`, json(input)),
  stopJob: (appId: string, jobId: string, projectId?: string) =>
    appsApiFetch<{ job: AppJob }>(`/apps/${appId}/jobs/${jobId}/stop${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ""}`, json({})),
  getJobLogs: (appId: string, jobId: string, projectId?: string) =>
    appsApiFetch<{ job_id: string; logs: string }>(`/apps/${appId}/jobs/${jobId}/logs${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ""}`),
  getReleases: (appId: string, projectId?: string) => appsApiFetch<{ releases: AppRelease[] }>(`/apps/${appId}/releases${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ""}`),
  createRelease: (appId: string, input: { version: string; changelog?: string }, projectId?: string) =>
    appsApiFetch<{ release: AppRelease }>(`/apps/${appId}/releases${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ""}`, json(input)),
  /**
   * 成果物ダウンロードURL。バックエンドは runner 以上を要求するため、
   * viewer にはリンク自体を出さないこと（`permissionAtLeast(permission, "runner")`）。
   */
  downloadArtifact: (appId: string, artifactId: string, projectId?: string) =>
    `${API_PREFIX}/apps/${encodeURIComponent(appId)}/artifacts/${encodeURIComponent(artifactId)}/download${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ""}`,
  getGrants: (appId: string) => appsApiFetch<{ grants: AppGrant[] }>(`/apps/${appId}/grants`),
  createGrant: (appId: string, input: { user_id?: string; project_id?: string; permission: AppPermission }) =>
    appsApiFetch<{ grant: AppGrant }>(`/apps/${appId}/grants`, json(input)),
  deleteGrant: (appId: string, grantId: string) =>
    appsApiFetch<{ success: boolean }>(`/apps/${appId}/grants/${grantId}`, { method: "DELETE" }),
  getProjectApps: (projectId: string) =>
    appsApiFetch<{ project_id: string; apps: ProjectAppBinding[] }>(`/projects/${projectId}/apps`),
  linkProjectApp: (projectId: string, input: ProjectAppInput) =>
    appsApiFetch<{ binding: { project_id: string; app_id: string; binding_mode: AppBindingMode } }>(
      `/projects/${projectId}/apps`,
      json(input),
    ),
  updateProjectApp: (projectId: string, appId: string, input: Partial<ProjectAppInput>) =>
    appsApiFetch<{ binding: ProjectAppBinding }>(`/projects/${projectId}/apps/${appId}`, {
      method: "PATCH",
      body: JSON.stringify(input),
    }),
  unlinkProjectApp: (projectId: string, appId: string) =>
    appsApiFetch<{ success: boolean }>(`/projects/${projectId}/apps/${appId}`, { method: "DELETE" }),
  listTaskApps: (taskId: string) =>
    appsApiFetch<{ task_id: string; apps: TaskAppLink[] }>(`/tasks/${taskId}/apps`),
  listAppTasks: (appId: string, projectId: string, relationType?: string) =>
    appsApiFetch<{ app_id: string; project_id: string; tasks: Array<TaskAppLink & { task?: Record<string, unknown> }> }>(`/apps/${appId}/tasks?project_id=${encodeURIComponent(projectId)}${relationType ? `&relation_type=${encodeURIComponent(relationType)}` : ""}`),
  linkTaskApp: (taskId: string, input: { app_id: string; target_id?: string | null; relation_type?: string }) =>
    appsApiFetch<{ success: boolean; link: TaskAppLink }>(`/tasks/${taskId}/apps`, json(input)),
  unlinkTaskApp: (taskId: string, appId: string, query = "") =>
    appsApiFetch<{ success: boolean }>(`/tasks/${taskId}/apps/${appId}${query}`, { method: "DELETE" }),
};

export function permissionAtLeast(permission: string | undefined, required: AppPermission): boolean {
  const rank: Record<AppPermission, number> = {
    viewer: 10,
    runner: 20,
    developer: 30,
    maintainer: 40,
    admin: 50,
  };
  return (rank[permission as AppPermission] || 0) >= rank[required];
}
