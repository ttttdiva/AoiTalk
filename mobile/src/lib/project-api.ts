import { fetchApi, getBaseUrl } from './api-client';
import { getToken } from './auth';
import type {
  Project,
  ProjectMember,
  ProjectStorageUsage,
  Tag,
  Space,
  TaskAssigneeCandidate,
} from '../types/api';

type ProjectPayload = {
  name: string;
  description?: string | null;
  aliases?: string[];
  allow_join_requests?: boolean;
  storage_quota_mb?: number;
  space_id?: string | null;
  is_completed?: boolean;
  project_metadata?: Record<string, unknown> | null;
};

type ProjectMemberResponse = ProjectMember & {
  user?: {
    username?: string | null;
    display_name?: string | null;
  } | null;
};

/** A pending membership request returned by the canonical project API. */
export interface ProjectJoinRequest {
  id: string;
  project_id: string;
  user_id: string;
  message?: string | null;
  status?: 'pending' | 'approved' | 'rejected' | string;
  processed_by?: string | null;
  processed_at?: string | null;
  rejection_reason?: string | null;
  created_at?: string | null;
  user?: {
    id?: string;
    username?: string | null;
    display_name?: string | null;
  } | null;
}

export type ProjectMemberRole = 'owner' | 'admin' | 'member' | 'viewer';
export type ProjectPermission =
  | 'read'
  | 'write'
  | 'delete'
  | 'manage_members'
  | 'manage_settings';

export interface ProjectCapabilities {
  role: ProjectMemberRole | string | null;
  isOwner: boolean;
  isGlobalAdmin: boolean;
  canRead: boolean;
  canWrite: boolean;
  canDelete: boolean;
  canManageMembers: boolean;
  canManageSettings: boolean;
}

export interface ProjectFileEntry {
  path: string;
  name?: string;
  filename?: string;
  is_dir?: boolean;
  size?: number;
  size_bytes?: number;
  modified_at?: string | null;
  [key: string]: unknown;
}

export interface ProjectFileListing {
  success?: boolean;
  path?: string;
  root_path?: string;
  directories?: ProjectFileEntry[];
  files?: ProjectFileEntry[];
  [key: string]: unknown;
}

export interface ProjectStoragePath {
  path?: string;
  project_storage_path?: string;
  workspace_root?: string | null;
  wbs_file?: string | null;
  issue_file?: string | null;
  risk_file?: string | null;
  permissions?: Record<string, boolean>;
  [key: string]: unknown;
}

type ProjectUser = { user_id?: string | null; role?: string | null } | null | undefined;

/**
 * Resolve the same explicit ACL used by the backend before exposing project
 * actions in a mobile surface.  A role name alone is never treated as a
 * grant: the API's permissions object is the canonical allow-list.  Owner
 * and global-admin bypasses mirror ``has_effective_project_permission``.
 */
export function getProjectCapabilities(
  project: Project,
  user?: ProjectUser,
): ProjectCapabilities {
  const isGlobalAdmin = user?.role === 'admin';
  const isOwner = Boolean(
    user?.user_id && project.owner_id && user.user_id === project.owner_id,
  );
  const localOnly =
    project.owner_id == null &&
    Boolean(project.metadata && project.metadata.local_only === true);
  const permissions = project.membership?.permissions;
  const grant = (permission: ProjectPermission): boolean =>
    isGlobalAdmin || isOwner || localOnly || permissions?.[permission] === true;

  return {
    role: isOwner
      ? 'owner'
      : isGlobalAdmin
        ? 'admin'
        : project.membership?.role ?? null,
    isOwner,
    isGlobalAdmin,
    canRead: grant('read'),
    canWrite: grant('write'),
    canDelete: grant('delete'),
    canManageMembers: grant('manage_members'),
    canManageSettings: grant('manage_settings'),
  };
}

function projectPath(projectId: string, suffix = ''): string {
  return `/api/projects/${encodeURIComponent(projectId)}${suffix}`;
}

function withQuery(path: string, params: Record<string, string | number | undefined>): string {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== '') query.set(key, String(value));
  }
  const encoded = query.toString();
  return encoded ? `${path}?${encoded}` : path;
}

async function uploadProjectForm<T>(path: string, form: FormData): Promise<T> {
  const token = await getToken();
  const response = await fetch(`${await getBaseUrl()}${path}`, {
    method: 'POST',
    headers: token ? { Authorization: `Bearer ${token}` } : undefined,
    body: form,
  });
  if (!response.ok) {
    const body = await response.text().catch(() => '');
    throw new Error(`API Error ${response.status}: ${body.slice(0, 500)}`);
  }
  return response.json() as Promise<T>;
}

type AssigneeCandidateResponse = TaskAssigneeCandidate & {
  user?: {
    username?: string | null;
    display_name?: string | null;
  } | null;
};

function normalizeMembers(
  data: ProjectMemberResponse[] | { members: ProjectMemberResponse[] },
): ProjectMember[] {
  const members = Array.isArray(data) ? data : data.members;
  return members.map(({ user, ...member }) => ({
    ...member,
    username: member.username ?? user?.username ?? null,
    display_name: member.display_name ?? user?.display_name ?? null,
  }));
}

export const projectApi = {
  async list(): Promise<Project[]> {
    const data = await fetchApi<{ projects: Project[]; total: number }>('/api/projects');
    return data.projects;
  },

  async listSpaces(): Promise<Space[]> {
    const data = await fetchApi<{ spaces: Space[]; total?: number } | Space[]>('/api/spaces');
    return Array.isArray(data) ? data : data.spaces;
  },

  async getSpace(spaceId: string): Promise<Space> {
    return fetchApi<Space>(`/api/spaces/${encodeURIComponent(spaceId)}`);
  },

  async createSpace(payload: {
    name: string;
    description?: string | null;
    color?: string | null;
    sort_order?: number;
  }): Promise<Space> {
    const data = await fetchApi<{ success?: boolean; space: Space }>('/api/spaces', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    return data.space;
  },

  async updateSpace(
    spaceId: string,
    payload: Partial<{
      name: string;
      description: string | null;
      color: string | null;
      sort_order: number;
    }>,
  ): Promise<Space> {
    const data = await fetchApi<{ success?: boolean; space: Space }>(
      `/api/spaces/${encodeURIComponent(spaceId)}`,
      { method: 'PATCH', body: JSON.stringify(payload) },
    );
    return data.space;
  },

  async deleteSpace(spaceId: string): Promise<void> {
    await fetchApi(`/api/spaces/${encodeURIComponent(spaceId)}`, { method: 'DELETE' });
  },

  async create(payload: ProjectPayload): Promise<Project> {
    const data = await fetchApi<{ success: boolean; project: Project }>('/api/projects', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    return data.project;
  },

  async update(projectId: string, payload: Partial<ProjectPayload>): Promise<Project> {
    const data = await fetchApi<{ success: boolean; project: Project }>(projectPath(projectId), {
      method: 'PATCH',
      body: JSON.stringify(payload),
    });
    return data.project;
  },

  async delete(projectId: string): Promise<void> {
    await fetchApi(projectPath(projectId), { method: 'DELETE' });
  },

  async get(projectId: string): Promise<Project> {
    const data = await fetchApi<Project | { project: Project }>(projectPath(projectId));
    return 'project' in data ? data.project : data;
  },

  async listMembers(projectId: string): Promise<ProjectMember[]> {
    const data = await fetchApi<
      ProjectMemberResponse[] | { members: ProjectMemberResponse[] }
    >(
      projectPath(projectId, '/members'),
    );
    return normalizeMembers(data);
  },

  async addMember(
    projectId: string,
    payload: { user_id?: string; username?: string; role?: Exclude<ProjectMemberRole, 'owner'> },
  ): Promise<ProjectMember> {
    const data = await fetchApi<{ success?: boolean; member: ProjectMemberResponse }>(
      projectPath(projectId, '/members'),
      { method: 'POST', body: JSON.stringify({ role: 'member', ...payload }) },
    );
    return normalizeMembers([data.member])[0];
  },

  async updateMember(
    projectId: string,
    userId: string,
    payload: { role?: Exclude<ProjectMemberRole, 'owner'>; permissions?: Record<string, boolean> },
  ): Promise<ProjectMember> {
    const data = await fetchApi<{ success?: boolean; member: ProjectMemberResponse }>(
      projectPath(projectId, `/members/${encodeURIComponent(userId)}`),
      { method: 'PATCH', body: JSON.stringify(payload) },
    );
    return normalizeMembers([data.member])[0];
  },

  async removeMember(projectId: string, userId: string): Promise<void> {
    await fetchApi(
      projectPath(projectId, `/members/${encodeURIComponent(userId)}`),
      { method: 'DELETE' },
    );
  },

  async listJoinRequests(projectId: string): Promise<ProjectJoinRequest[]> {
    const data = await fetchApi<
      ProjectJoinRequest[] | { requests?: ProjectJoinRequest[] }
    >(projectPath(projectId, '/join-requests'));
    return Array.isArray(data) ? data : data.requests ?? [];
  },

  async submitJoinRequest(
    projectId: string,
    message?: string | null,
  ): Promise<ProjectJoinRequest> {
    const data = await fetchApi<{ success?: boolean; request: ProjectJoinRequest }>(
      projectPath(projectId, '/join-requests'),
      { method: 'POST', body: JSON.stringify({ message: message ?? null }) },
    );
    return data.request;
  },

  async approveJoinRequest(
    projectId: string,
    requestId: string,
    role: Exclude<ProjectMemberRole, 'owner'> = 'member',
  ): Promise<ProjectMember> {
    const data = await fetchApi<{ success?: boolean; member: ProjectMemberResponse }>(
      projectPath(projectId, `/join-requests/${encodeURIComponent(requestId)}/approve`),
      { method: 'POST', body: JSON.stringify({ role }) },
    );
    return normalizeMembers([data.member])[0];
  },

  async rejectJoinRequest(
    projectId: string,
    requestId: string,
    reason?: string | null,
  ): Promise<void> {
    await fetchApi(
      projectPath(projectId, `/join-requests/${encodeURIComponent(requestId)}/reject`),
      { method: 'POST', body: JSON.stringify({ reason: reason ?? null }) },
    );
  },

  async listAssigneeCandidates(
    projectId: string,
  ): Promise<TaskAssigneeCandidate[]> {
    const data = await fetchApi<
      AssigneeCandidateResponse[] | { members: AssigneeCandidateResponse[] }
    >(projectPath(projectId, '/assignee-candidates'));
    const members = Array.isArray(data) ? data : data.members;
    return members.map(({ user, ...member }) => ({
      ...member,
      username: member.username ?? user?.username ?? null,
      display_name: member.display_name ?? user?.display_name ?? null,
    }));
  },

  async getStorageUsage(projectId: string): Promise<ProjectStorageUsage> {
    return fetchApi<ProjectStorageUsage>(projectPath(projectId, '/storage-usage'));
  },

  async getStoragePath(projectId: string): Promise<ProjectStoragePath> {
    return fetchApi<ProjectStoragePath>(projectPath(projectId, '/storage-path'));
  },

  async listFiles(projectId: string, path = ''): Promise<ProjectFileListing> {
    return fetchApi<ProjectFileListing>(
      withQuery(projectPath(projectId, '/files'), { path }),
    );
  },

  async getFileInfo(projectId: string, path: string): Promise<ProjectFileEntry> {
    return fetchApi<ProjectFileEntry>(
      withQuery(projectPath(projectId, '/files/info'), { path }),
    );
  },

  async previewFile(projectId: string, path: string): Promise<Record<string, unknown>> {
    return fetchApi<Record<string, unknown>>(
      withQuery(projectPath(projectId, '/files/preview'), { path }),
    );
  },

  async getFileContent(projectId: string, path: string): Promise<{
    path?: string;
    content?: string;
    [key: string]: unknown;
  }> {
    return fetchApi(withQuery(projectPath(projectId, '/files/content'), { path }));
  },

  async searchFiles(
    projectId: string,
    query: string,
    options?: { path?: string; limit?: number },
  ): Promise<{ results?: ProjectFileEntry[]; [key: string]: unknown }> {
    return fetchApi(
      withQuery(projectPath(projectId, '/files/search'), {
        q: query,
        path: options?.path,
        limit: options?.limit,
      }),
    );
  },

  async createFolder(
    projectId: string,
    payload: { path?: string; name: string },
  ): Promise<ProjectFileListing> {
    return fetchApi(projectPath(projectId, '/files/folders'), {
      method: 'POST',
      body: JSON.stringify({ path: '', ...payload }),
    });
  },

  async renameFile(projectId: string, path: string, newName: string): Promise<ProjectFileListing> {
    return fetchApi(projectPath(projectId, '/files/rename'), {
      method: 'POST',
      body: JSON.stringify({ path, new_name: newName }),
    });
  },

  async moveFile(projectId: string, src: string, dest: string): Promise<ProjectFileListing> {
    return fetchApi(projectPath(projectId, '/files/move'), {
      method: 'POST',
      body: JSON.stringify({ src, dest }),
    });
  },

  async copyFile(projectId: string, src: string, dest: string): Promise<ProjectFileListing> {
    return fetchApi(projectPath(projectId, '/files/copy'), {
      method: 'POST',
      body: JSON.stringify({ src, dest }),
    });
  },

  async archiveFiles(
    projectId: string,
    paths: string[],
    dest = '',
  ): Promise<ProjectFileListing> {
    return fetchApi(projectPath(projectId, '/files/archive'), {
      method: 'POST',
      body: JSON.stringify({ paths, dest }),
    });
  },

  async extractFiles(
    projectId: string,
    paths: string[],
    dest = '',
  ): Promise<ProjectFileListing> {
    return fetchApi(projectPath(projectId, '/files/extract'), {
      method: 'POST',
      body: JSON.stringify({ paths, dest }),
    });
  },

  async restoreFile(projectId: string, token: string): Promise<ProjectFileListing> {
    return fetchApi(projectPath(projectId, '/files/restore'), {
      method: 'POST',
      body: JSON.stringify({ token }),
    });
  },

  async deleteFile(projectId: string, path: string): Promise<ProjectFileListing> {
    return fetchApi(withQuery(projectPath(projectId, '/files'), { path }), {
      method: 'DELETE',
    });
  },

  async uploadFile(
    projectId: string,
    file: { uri: string; name: string; mimeType?: string | null },
    path = '',
  ): Promise<ProjectFileListing> {
    const form = new FormData();
    form.append('file', {
      uri: file.uri,
      name: file.name,
      type: file.mimeType || 'application/octet-stream',
    } as unknown as Blob);
    return uploadProjectForm(
      withQuery(projectPath(projectId, '/files/upload'), { path }),
      form,
    );
  },

  async organizeInformation(
    projectId: string,
    payload: {
      path?: string;
      apply?: boolean;
      use_llm?: boolean;
      max_files?: number;
      draft?: Record<string, unknown> | null;
    },
  ): Promise<Record<string, unknown>> {
    return fetchApi(projectPath(projectId, '/information/organize-folder'), {
      method: 'POST',
      body: JSON.stringify({
        path: '',
        apply: false,
        use_llm: true,
        max_files: 80,
        ...payload,
      }),
    });
  },

  async dailyIntake(
    projectId: string,
    payload: {
      raw_input: string;
      intake_date?: string;
      clarification_answers?: string;
      apply?: boolean;
      use_llm?: boolean;
      draft?: Record<string, unknown> | null;
    },
  ): Promise<Record<string, unknown>> {
    return fetchApi(projectPath(projectId, '/information/daily-intake'), {
      method: 'POST',
      body: JSON.stringify({
        intake_date: '',
        clarification_answers: '',
        apply: false,
        use_llm: true,
        ...payload,
      }),
    });
  },

  async listTags(projectId: string): Promise<Tag[]> {
    const data = await fetchApi<Tag[] | { tags?: Tag[] }>(projectPath(projectId, '/tags'));
    return Array.isArray(data) ? data : data.tags ?? [];
  },

  async createTag(projectId: string, payload: { name: string; color?: string | null }): Promise<Tag> {
    return fetchApi<Tag>(projectPath(projectId, '/tags'), {
      method: 'POST',
      body: JSON.stringify(payload),
    });
  },

  async updateTag(
    tagId: string,
    payload: { name?: string; color?: string | null },
  ): Promise<Tag> {
    return fetchApi<Tag>(`/api/tags/${encodeURIComponent(tagId)}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    });
  },

  async deleteTag(tagId: string): Promise<void> {
    await fetchApi(`/api/tags/${encodeURIComponent(tagId)}`, { method: 'DELETE' });
  },

  async listSpaceTags(spaceId: string): Promise<Tag[]> {
    const data = await fetchApi<Tag[] | { tags?: Tag[] }>(
      `/api/spaces/${encodeURIComponent(spaceId)}/tags`,
    );
    return Array.isArray(data) ? data : data.tags ?? [];
  },

  async createSpaceTag(
    spaceId: string,
    payload: { name: string; color?: string | null },
  ): Promise<Tag> {
    return fetchApi<Tag>(`/api/spaces/${encodeURIComponent(spaceId)}/tags`, {
      method: 'POST',
      body: JSON.stringify(payload),
    });
  },
};
