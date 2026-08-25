"use client";

export type ProjectMember = {
  id: string;
  project_id: string;
  user_id: string;
  role: string | null;
  joined_at: string | null;
  username: string;
  display_name: string | null;
};

export type ProjectMemberCandidate = {
  id: string;
  username: string;
  display_name: string | null;
};

export type ProjectMemberMutationResponse = {
  success?: boolean;
  detail?: string;
};

export type ManagementFileKind =
  | "wbs"
  | "issue"
  | "risk"
  | "request"
  | "attachment";

export type ManagementDocumentKind = Exclude<ManagementFileKind, "attachment">;

export type ManagementConfig = {
  wbsFile: string | null;
  issueFile: string | null;
  riskFile: string | null;
  requestFiles: string[];
};

export type ManagementConfigPatch = Partial<{
  wbs_file: string | null;
  issue_file: string | null;
  risk_file: string | null;
  request_files: string[];
}>;

export type WbsRowInfo = {
  title: string;
  wbsId: string | null;
  status: string;
  priority: string;
  plannedEnd: string | null;
  assignee: string | null;
  requestText: string | null;
  sheetName: string;
  rowNumber: number;
};

export type ManagementRequestItem = {
  title: string;
  target: string;
  reason: string;
  sourceType: string;
  sourcePath: string;
  sourceRef: string;
  dueAt: string | null;
  status: string;
};

export type WbsScanResponse = {
  config: ManagementConfig;
  file_path: string | null;
  upcoming: WbsRowInfo[];
  requests: ManagementRequestItem[];
  errors: string[];
  summary: {
    total: number;
    open: number;
    review: number;
    overdue: number;
    request_count: number;
  };
};

export type ProjectFilerDirectory = {
  name: string;
  path: string;
  modifiedAt?: string;
};

export type ProjectFilerFile = {
  name: string;
  path: string;
  size: number;
  modifiedAt: string;
  extension: string;
};

export type ProjectFilerListResponse = {
  currentPath: string;
  parentPath: string | null;
  directories: ProjectFilerDirectory[];
  files: ProjectFilerFile[];
};

export type ManagementFileUploadResponse = {
  success: boolean;
  kind: ManagementFileKind;
  name: string;
  path: string;
  size: number;
  registered: boolean;
  config: ManagementConfig;
};

/**
 * Workspace API failures keep the HTTP status so batch callers can surface a
 * useful per-target result without having to parse an Error message.
 */
export class ProjectsWorkspaceApiError extends Error {
  readonly status: number;
  readonly detail: string;

  constructor(detail: string, status: number) {
    super(detail);
    this.name = "ProjectsWorkspaceApiError";
    this.detail = detail;
    this.status = status;
  }
}

function responseErrorDetail(value: unknown, fallback: string): string {
  if (typeof value === "string" && value.trim()) return value;
  if (
    typeof value === "object" &&
    value !== null &&
    "detail" in value &&
    typeof value.detail === "string" &&
    value.detail.trim()
  ) {
    return value.detail;
  }
  return fallback;
}

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...init?.headers },
    ...init,
  });
  if (!response.ok) {
    const detail = await response
      .json()
      .catch(() => ({ detail: response.statusText }));
    throw new ProjectsWorkspaceApiError(
      responseErrorDetail(detail, response.statusText || `HTTP ${response.status}`),
      response.status,
    );
  }
  return response.json();
}

export function listProjectMembers(projectId: string, signal?: AbortSignal) {
  return apiFetch<ProjectMember[]>(`/api/projects/${projectId}/members`, {
    signal,
  });
}

export function listProjectMemberCandidates(signal?: AbortSignal) {
  return apiFetch<ProjectMemberCandidate[]>("/api/users/list", { signal });
}

export function addProjectMember(
  projectId: string,
  userId: string,
  role: string,
  signal?: AbortSignal,
) {
  return apiFetch<ProjectMemberMutationResponse>(
    `/api/projects/${projectId}/members`,
    {
    method: "POST",
    body: JSON.stringify({ user_id: userId, role }),
    signal,
    },
  );
}

export function removeProjectMember(
  projectId: string,
  memberId: string,
  signal?: AbortSignal,
) {
  return apiFetch<ProjectMemberMutationResponse>(
    `/api/projects/${projectId}/members`,
    {
      method: "DELETE",
      body: JSON.stringify({ member_id: memberId }),
      signal,
    },
  );
}

export function changeProjectMemberRole(
  projectId: string,
  memberId: string,
  role: string,
  signal?: AbortSignal,
) {
  return apiFetch<ProjectMemberMutationResponse>(
    `/api/projects/${projectId}/members`,
    {
      method: "PATCH",
      body: JSON.stringify({ member_id: memberId, role }),
      signal,
    },
  );
}

export function getProjectManagementConfig(
  projectId: string,
  signal?: AbortSignal,
) {
  return apiFetch<{ config: ManagementConfig }>(
    `/api/projects/${projectId}/management`,
    { signal },
  );
}

export function getProjectWbsScan(projectId: string, signal?: AbortSignal) {
  return apiFetch<WbsScanResponse>(`/api/projects/${projectId}/wbs`, {
    signal,
  });
}

export function updateProjectManagementConfig(
  projectId: string,
  patch: ManagementConfigPatch,
  signal?: AbortSignal,
) {
  return apiFetch<{ config: ManagementConfig }>(
    `/api/projects/${projectId}/management`,
    {
      method: "PATCH",
      body: JSON.stringify(patch),
      signal,
    },
  );
}

export function listProjectManagementFiles(
  projectId: string,
  path: string,
  signal?: AbortSignal,
) {
  return apiFetch<ProjectFilerListResponse>(
    `/api/projects/${projectId}/management/files?path=${encodeURIComponent(path)}`,
    { signal },
  );
}

export async function uploadProjectManagementFile(
  projectId: string,
  kind: ManagementFileKind,
  file: File,
  signal?: AbortSignal,
  idempotencyKey?: string,
) {
  const formData = new FormData();
  formData.append("file", file);
  formData.append("kind", kind);
  formData.append("directory", "management");
  const response = await fetch(`/api/projects/${projectId}/management/files`, {
    method: "POST",
    credentials: "include",
    body: formData,
    signal,
    ...(idempotencyKey
      ? { headers: { "X-Idempotency-Key": idempotencyKey } }
      : {}),
  });
  if (!response.ok) {
    const detail = await response
      .json()
      .catch(() => ({ detail: "アップロードに失敗しました" }));
    const message = responseErrorDetail(detail, "アップロードに失敗しました");
    throw new ProjectsWorkspaceApiError(
      `${file.name}: ${message} (HTTP ${response.status})`,
      response.status,
    );
  }
  return (await response.json()) as ManagementFileUploadResponse;
}
