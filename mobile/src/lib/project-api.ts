import { fetchApi } from './api-client';
import type { Project, ProjectMember, ProjectStorageUsage } from '../types/api';

type ProjectPayload = {
  name: string;
  description?: string | null;
  aliases?: string[];
  allow_join_requests?: boolean;
  storage_quota_mb?: number;
  project_metadata?: Record<string, unknown> | null;
};

export const projectApi = {
  async list(): Promise<Project[]> {
    const data = await fetchApi<{ projects: Project[]; total: number }>('/api/projects');
    return data.projects;
  },

  async create(payload: ProjectPayload): Promise<Project> {
    const data = await fetchApi<{ success: boolean; project: Project }>('/api/projects', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    return data.project;
  },

  async update(projectId: string, payload: Partial<ProjectPayload>): Promise<Project> {
    const data = await fetchApi<{ success: boolean; project: Project }>(`/api/projects/${projectId}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    });
    return data.project;
  },

  async delete(projectId: string): Promise<void> {
    await fetchApi(`/api/projects/${projectId}`, { method: 'DELETE' });
  },

  async get(projectId: string): Promise<Project> {
    return fetchApi<Project>(`/api/projects/${projectId}`);
  },

  async listMembers(projectId: string): Promise<ProjectMember[]> {
    const data = await fetchApi<{ members: ProjectMember[] }>(`/api/projects/${projectId}/members`);
    return data.members;
  },

  async getStorageUsage(projectId: string): Promise<ProjectStorageUsage> {
    return fetchApi<ProjectStorageUsage>(`/api/projects/${projectId}/storage-usage`);
  },
};
