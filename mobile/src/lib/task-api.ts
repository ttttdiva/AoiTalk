/**
 * Task-related mobile API client.
 */

import { fetchApi, getBaseUrl } from "./api-client";
import { getToken } from "./auth";
import { normalizeTaskStatus } from "./task-status";
import type {
  ProjectNotificationSetting,
  Project,
  Space,
  Tag,
  Task,
  TaskAttachment,
  TaskOccurrence,
  TimeEntry,
  TimeReport,
  UserSettings,
  UserSettingsResponse,
  UserNotificationPreferences,
} from "../types/api";

export type Scope = { project_id?: string; space_id?: string };

function buildScopeQuery(scope: Scope | string): URLSearchParams {
  const params = new URLSearchParams();
  if (typeof scope === "string") {
    params.set("project_id", scope);
  } else {
    if (scope.project_id) params.set("project_id", scope.project_id);
    if (scope.space_id) params.set("space_id", scope.space_id);
  }
  return params;
}

function normalizeTask<T extends { status: string }>(task: T): T {
  return {
    ...task,
    status: normalizeTaskStatus(task.status),
  };
}

function normalizeOccurrence<T extends { status: string }>(occurrence: T): T {
  return {
    ...occurrence,
    status: normalizeTaskStatus(occurrence.status),
  };
}

function normalizePayload(
  data: Record<string, unknown>,
): Record<string, unknown> {
  if (!("status" in data)) return data;
  return {
    ...data,
    status: normalizeTaskStatus(data.status),
  };
}

async function uploadForm<T>(path: string, form: FormData): Promise<T> {
  const token = await getToken();
  const baseUrl = await getBaseUrl();
  const res = await fetch(`${baseUrl}${path}`, {
    method: "POST",
    headers: {
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: form,
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`API Error ${res.status}: ${text.slice(0, 500)}`);
  }
  return res.json();
}

export const taskApi = {
  async listAllTasks(): Promise<Task[]> {
    return fetchApi<Task[]>("/api/tasks").then((tasks) =>
      tasks.map(normalizeTask),
    );
  },

  async listTasksByScope(scope: Scope = {}): Promise<Task[]> {
    const params = buildScopeQuery(scope);
    const suffix = params.toString() ? `?${params.toString()}` : "";
    return fetchApi<Task[]>(`/api/tasks${suffix}`).then((tasks) =>
      tasks.map(normalizeTask),
    );
  },

  async listTasks(projectId: string): Promise<Task[]> {
    const params = new URLSearchParams({ project_id: projectId });
    return fetchApi<Task[]>(`/api/tasks?${params.toString()}`).then((tasks) =>
      tasks.map(normalizeTask),
    );
  },

  async getTask(taskId: string): Promise<Task> {
    return fetchApi<Task>(`/api/tasks/${taskId}`).then(normalizeTask);
  },

  async createTask(data: Record<string, unknown>): Promise<Task> {
    return fetchApi<Task>("/api/tasks", {
      method: "POST",
      body: JSON.stringify(normalizePayload(data)),
    }).then(normalizeTask);
  },

  async updateTask(
    taskId: string,
    data: Record<string, unknown>,
  ): Promise<Task> {
    return fetchApi<Task>(`/api/tasks/${taskId}`, {
      method: "PATCH",
      body: JSON.stringify(normalizePayload(data)),
    }).then(normalizeTask);
  },

  async deleteTask(taskId: string): Promise<void> {
    await fetchApi(`/api/tasks/${taskId}`, { method: "DELETE" });
  },

  async reorderTasks(projectId: string, taskIds: string[]): Promise<void> {
    await fetchApi(`/api/projects/${projectId}/tasks/reorder`, {
      method: "POST",
      body: JSON.stringify({ task_ids: taskIds }),
    });
  },

  async reorderAllTasks(taskIds: string[]): Promise<void> {
    await fetchApi("/api/tasks/reorder", {
      method: "POST",
      body: JSON.stringify({ task_ids: taskIds }),
    });
  },

  async listProjects(): Promise<Project[]> {
    const data = await fetchApi<{ projects: Project[]; total: number }>(
      "/api/projects",
    );
    return data.projects;
  },

  async listSpaces(): Promise<Space[]> {
    const data = await fetchApi<{ spaces: Space[]; total: number }>(
      "/api/spaces",
    );
    return data.spaces;
  },

  async listTags(projectId: string): Promise<Tag[]> {
    return fetchApi<Tag[]>(`/api/projects/${projectId}/tags`);
  },

  async createTag(
    projectId: string,
    data: { name: string; color?: string },
  ): Promise<Tag> {
    return fetchApi<Tag>(`/api/projects/${projectId}/tags`, {
      method: "POST",
      body: JSON.stringify(data),
    });
  },

  async addComment(taskId: string, content: string): Promise<unknown> {
    return fetchApi<unknown>(`/api/tasks/${taskId}/comments`, {
      method: "POST",
      body: JSON.stringify({ content }),
    });
  },

  async listAttachments(taskId: string): Promise<TaskAttachment[]> {
    return fetchApi<TaskAttachment[]>(`/api/tasks/${taskId}/attachments`);
  },

  async uploadAttachment(
    taskId: string,
    file: { uri: string; name: string; mimeType?: string | null },
  ): Promise<TaskAttachment> {
    const form = new FormData();
    form.append("file", {
      uri: file.uri,
      name: file.name,
      type: file.mimeType || "application/octet-stream",
    } as unknown as Blob);
    return uploadForm<TaskAttachment>(`/api/tasks/${taskId}/attachments`, form);
  },

  async deleteAttachment(taskId: string, attachmentId: string): Promise<void> {
    await fetchApi(`/api/tasks/${taskId}/attachments/${attachmentId}`, {
      method: "DELETE",
    });
  },

  async runAgentTriage(taskId: string): Promise<{
    task_id: string;
    status: string;
    summary: string;
    questions: string[];
    metadata: Record<string, unknown>;
  }> {
    return fetchApi(`/api/tasks/${taskId}/agent-triage`, {
      method: "POST",
    });
  },

  async listOccurrences(
    scopeOrProjectId?: Scope | string,
    startFrom?: string,
    endTo?: string,
  ): Promise<TaskOccurrence[]> {
    const params = buildScopeQuery(scopeOrProjectId ?? {});
    if (startFrom) params.set("start_from", startFrom);
    if (endTo) params.set("end_to", endTo);
    const suffix = params.toString() ? `?${params.toString()}` : "";
    return fetchApi<TaskOccurrence[]>(`/api/task-occurrences${suffix}`).then(
      (occurrences) => occurrences.map(normalizeOccurrence),
    );
  },

  async startTimer(
    taskId: string,
    occurrenceId?: string | null,
    note?: string | null,
  ): Promise<TimeEntry> {
    return fetchApi<TimeEntry>("/api/time-entries/start", {
      method: "POST",
      body: JSON.stringify({
        task_id: taskId,
        occurrence_id: occurrenceId ?? null,
        note: note ?? null,
        source: "mobile",
      }),
    });
  },

  async stopTimer(entryId?: string): Promise<TimeEntry> {
    return fetchApi<TimeEntry>("/api/time-entries/stop", {
      method: "POST",
      body: JSON.stringify({
        time_entry_id: entryId ?? null,
      }),
    });
  },

  async getActiveTimer(): Promise<TimeEntry | null> {
    try {
      return await fetchApi<TimeEntry>("/api/time-entries/active");
    } catch {
      return null;
    }
  },

  async listTimeEntries(
    scope: Scope | string,
    dateFrom?: string,
    dateTo?: string,
  ): Promise<TimeEntry[]> {
    const params = buildScopeQuery(scope);
    if (dateFrom) params.set("date_from", dateFrom);
    if (dateTo) params.set("date_to", dateTo);
    return fetchApi<TimeEntry[]>(`/api/time-entries?${params.toString()}`);
  },

  async getTimeReport(
    scope: Scope | string,
    dateFrom?: string,
    dateTo?: string,
  ): Promise<TimeReport> {
    const params = buildScopeQuery(scope);
    if (dateFrom) params.set("date_from", dateFrom);
    if (dateTo) params.set("date_to", dateTo);
    return fetchApi<TimeReport>(`/api/reports/time?${params.toString()}`);
  },

  async updateTimeEntry(
    entryId: string,
    data: Record<string, unknown>,
  ): Promise<TimeEntry> {
    return fetchApi<TimeEntry>(`/api/time-entries/${entryId}`, {
      method: "PATCH",
      body: JSON.stringify(data),
    });
  },

  async deleteTimeEntry(entryId: string): Promise<void> {
    await fetchApi(`/api/time-entries/${entryId}`, { method: "DELETE" });
  },

  async listNotifications(
    unreadOnly = false,
  ): Promise<Record<string, unknown>[]> {
    return fetchApi<Record<string, unknown>[]>(
      `/api/notifications?unread_only=${unreadOnly ? "true" : "false"}`,
    );
  },

  async markNotificationRead(notificationId: string): Promise<void> {
    await fetchApi(`/api/notifications/${notificationId}/read`, {
      method: "POST",
    });
  },

  async getNotificationSettings(
    projectId: string,
  ): Promise<ProjectNotificationSetting> {
    return fetchApi<ProjectNotificationSetting>(
      `/api/projects/${projectId}/notification-settings`,
    );
  },

  async updateNotificationSettings(
    projectId: string,
    data: Partial<ProjectNotificationSetting>,
  ): Promise<ProjectNotificationSetting> {
    return fetchApi<ProjectNotificationSetting>(
      `/api/projects/${projectId}/notification-settings`,
      {
        method: "PATCH",
        body: JSON.stringify(data),
      },
    );
  },

  async getUserNotificationPreferences(): Promise<UserNotificationPreferences> {
    return fetchApi<UserNotificationPreferences>(
      "/api/users/me/notification-preferences",
    );
  },

  async updateUserNotificationPreferences(
    data: UserNotificationPreferences,
  ): Promise<UserNotificationPreferences> {
    return fetchApi<UserNotificationPreferences>(
      "/api/users/me/notification-preferences",
      {
        method: "PATCH",
        body: JSON.stringify(data),
      },
    );
  },

  async getUserSettings(): Promise<UserSettings> {
    const res = await fetchApi<UserSettingsResponse>("/api/users/me/settings");
    return res.settings ?? {};
  },

  async updateUserSettings(data: UserSettings): Promise<UserSettings> {
    const res = await fetchApi<UserSettingsResponse>("/api/users/me/settings", {
      method: "PATCH",
      body: JSON.stringify(data),
    });
    return res.settings ?? {};
  },
};
