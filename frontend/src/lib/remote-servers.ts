/**
 * 外部AoiTalkサーバー接続プロファイルのクライアント。
 * Python API へ /api/python-proxy 経由でアクセスする（cookie認証）。
 */

import type {
  Project,
  Space,
  Task,
  TaskOccurrence,
  TimeEntry,
  TimeReport,
} from "@/lib/task-api";

export type RemoteServerProfile = {
  id: string;
  user_id: string;
  name: string;
  base_url: string;
  display_color?: string | null;
  enabled: boolean;
  has_token: boolean;
  last_status?: string | null;
  last_checked_at?: string | null;
  last_capabilities?: RemoteCapabilities | null;
  created_at?: string | null;
  updated_at?: string | null;
};

export type RemoteCapabilities = {
  version?: string;
  profile?: string;
  features?: Record<string, boolean>;
  resources?: Record<string, { read?: boolean; write?: boolean }>;
  server_time?: string;
  user?: { id?: string; username?: string; role?: string } | null;
};

export type CreateRemoteServerInput = {
  name: string;
  base_url: string;
  auth_token?: string | null;
  display_color?: string | null;
  enabled?: boolean;
};

export type UpdateRemoteServerInput = Partial<CreateRemoteServerInput>;

export type ConnectionTestResult = {
  success: boolean;
  status: string;
  capabilities?: RemoteCapabilities;
  error?: string;
};

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api/python-proxy${path}`, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...init?.headers },
    ...init,
  });
  if (!res.ok) {
    const detail = await res
      .json()
      .catch(() => ({ detail: res.statusText }));
    throw new Error(detail.detail || res.statusText);
  }
  return res.json();
}

function buildQuery(params?: Record<string, string | number | undefined>): string {
  if (!params) return "";
  const query = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== "") query.set(key, String(value));
  });
  const text = query.toString();
  return text ? `?${text}` : "";
}

async function proxyData<T>(path: string): Promise<T> {
  const response = await request<{ data: T }>(path);
  return response.data;
}

export async function fetchRemoteCapabilities(
  profileId: string,
): Promise<RemoteCapabilities> {
  return proxyData<RemoteCapabilities>(`/remote-servers/${profileId}/capabilities`);
}

export async function listRemoteServers(): Promise<RemoteServerProfile[]> {
  const data = await request<{ profiles: RemoteServerProfile[] }>(
    "/remote-servers",
  );
  return data.profiles ?? [];
}

export async function createRemoteServer(
  input: CreateRemoteServerInput,
): Promise<RemoteServerProfile> {
  const data = await request<{ profile: RemoteServerProfile }>(
    "/remote-servers",
    { method: "POST", body: JSON.stringify(input) },
  );
  return data.profile;
}

export async function updateRemoteServer(
  id: string,
  input: UpdateRemoteServerInput,
): Promise<RemoteServerProfile> {
  const data = await request<{ profile: RemoteServerProfile }>(
    `/remote-servers/${id}`,
    { method: "PATCH", body: JSON.stringify(input) },
  );
  return data.profile;
}

export async function deleteRemoteServer(id: string): Promise<void> {
  await request<{ success: boolean }>(`/remote-servers/${id}`, {
    method: "DELETE",
  });
}

export async function testRemoteServer(
  id: string,
): Promise<ConnectionTestResult> {
  // 失敗時もボディに status/error が入るため、ここでは例外化せず返す。
  const res = await fetch(`/api/python-proxy/remote-servers/${id}/test`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
  });
  const data = (await res.json().catch(() => ({}))) as ConnectionTestResult;
  if (data && typeof data.success === "boolean") return data;
  return { success: false, status: "error", error: res.statusText };
}

export async function listRemoteSpaces(profileId: string): Promise<Space[]> {
  const payload = await proxyData<Space[] | { spaces?: Space[] }>(
    `/remote-servers/${profileId}/spaces`,
  );
  return Array.isArray(payload) ? payload : payload.spaces ?? [];
}

export async function listRemoteProjects(profileId: string): Promise<Project[]> {
  const payload = await proxyData<Project[] | { projects?: Project[] }>(
    `/remote-servers/${profileId}/projects`,
  );
  return Array.isArray(payload) ? payload : payload.projects ?? [];
}

export async function listRemoteTaskOccurrences(
  profileId: string,
  params: {
    project_id?: string;
    space_id?: string;
    start_from: string;
    end_to: string;
  },
): Promise<TaskOccurrence[]> {
  const payload = await proxyData<TaskOccurrence[] | { occurrences?: TaskOccurrence[] }>(
    `/remote-servers/${profileId}/task-occurrences${buildQuery(params)}`,
  );
  return Array.isArray(payload) ? payload : payload.occurrences ?? [];
}

export async function getRemoteTimeReport(
  profileId: string,
  params: { project_id?: string; space_id?: string; date_from?: string; date_to?: string },
): Promise<TimeReport> {
  return proxyData<TimeReport>(
    `/remote-servers/${profileId}/reports/time${buildQuery(params)}`,
  );
}

export async function listRemoteTimeEntries(
  profileId: string,
  params: { project_id?: string; space_id?: string; date_from?: string; date_to?: string },
): Promise<TimeEntry[]> {
  const payload = await proxyData<TimeEntry[] | { entries?: TimeEntry[] }>(
    `/remote-servers/${profileId}/time-entries${buildQuery(params)}`,
  );
  return Array.isArray(payload) ? payload : payload.entries ?? [];
}

export type RemoteWorkspaceListing = {
  success?: boolean;
  current_path?: string;
  parent_path?: string | null;
  can_go_up?: boolean;
  directories?: Array<Record<string, unknown>>;
  files?: Array<Record<string, unknown>>;
  total_items?: number;
};

export async function listRemoteWorkspaceFiles(
  profileId: string,
  projectId: string,
  path = "",
): Promise<RemoteWorkspaceListing> {
  return proxyData<RemoteWorkspaceListing>(
    `/remote-servers/${profileId}/workspace/files${buildQuery({ project_id: projectId, path })}`,
  );
}

export async function getRemoteWorkspaceInfo(
  profileId: string,
  projectId: string,
  path: string,
): Promise<Record<string, unknown>> {
  return proxyData<Record<string, unknown>>(
    `/remote-servers/${profileId}/workspace/info${buildQuery({ project_id: projectId, path })}`,
  );
}

export async function getRemoteWorkspacePreview(
  profileId: string,
  projectId: string,
  path: string,
): Promise<Record<string, unknown>> {
  return proxyData<Record<string, unknown>>(
    `/remote-servers/${profileId}/workspace/preview${buildQuery({ project_id: projectId, path })}`,
  );
}

export async function getRemoteWorkspaceContent(
  profileId: string,
  projectId: string,
  path: string,
): Promise<Record<string, unknown>> {
  return proxyData<Record<string, unknown>>(
    `/remote-servers/${profileId}/workspace/content${buildQuery({ project_id: projectId, path })}`,
  );
}

export async function searchRemoteWorkspace(
  profileId: string,
  projectId: string,
  query: string,
  path = "",
  limit = 50,
): Promise<Record<string, unknown>[]> {
  const payload = await proxyData<{ results?: Record<string, unknown>[] }>(
    `/remote-servers/${profileId}/workspace/search${buildQuery({ project_id: projectId, q: query, path, limit })}`,
  );
  return payload.results ?? [];
}

export function remoteWorkspaceDownloadUrl(
  profileId: string,
  projectId: string,
  path: string,
): string {
  return `/api/python-proxy/remote-servers/${profileId}/workspace/download${buildQuery({ project_id: projectId, path })}`;
}

export type RemoteDocsTree = Record<string, unknown> & {
  nodes?: Array<Record<string, unknown>>;
  edges?: Array<Record<string, unknown>>;
  fields?: Array<Record<string, unknown>>;
  field_values?: Array<Record<string, unknown>>;
  supertags?: Array<Record<string, unknown>>;
  node_supertags?: Array<Record<string, unknown>>;
  placements?: Array<Record<string, unknown>>;
};

export async function getRemoteDocsTree(profileId: string): Promise<RemoteDocsTree> {
  return proxyData<RemoteDocsTree>(`/remote-servers/${profileId}/docs/tree`);
}

export async function searchRemoteDocs(
  profileId: string,
  query: string,
  projectId?: string,
): Promise<Record<string, unknown>[]> {
  const payload = await proxyData<{ results?: Record<string, unknown>[] }>(
    `/remote-servers/${profileId}/docs/search${buildQuery({ q: query, project: projectId })}`,
  );
  return payload.results ?? [];
}

export async function getRemoteDocNode(
  profileId: string,
  nodeId: string,
): Promise<Record<string, unknown>> {
  return proxyData<Record<string, unknown>>(
    `/remote-servers/${profileId}/docs/nodes/${nodeId}`,
  );
}
