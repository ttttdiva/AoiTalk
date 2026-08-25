/**
 * Task-related mobile API client.
 */

import { Alert } from "react-native";

import { fetchApi, getBaseUrl, isApiHttpError } from "./api-client";
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
  TaskRecurrence,
  TaskRecurrencePayload,
  TaskReference,
  TaskReferencePayload,
  TaskRestoreResponse,
  TimeEntry,
  TimeReport,
  UserSettings,
  UserSettingsResponse,
  UserNotificationPreferences,
} from "../types/api";

export type Scope = { project_id?: string; space_id?: string };

type IncompleteSubtaskSummary = {
  id: string;
  title: string;
  status: string;
};

export class TaskCompletionCancelledError extends Error {
  constructor() {
    super("タスクの完了をキャンセルしました");
    this.name = "TaskCompletionCancelledError";
  }
}

const pendingCompletionConfirmationByTaskId = new Map<
  string,
  Promise<boolean>
>();
let completionConfirmationQueue = Promise.resolve();

function parseIncompleteSubtasks(
  error: unknown,
): IncompleteSubtaskSummary[] | null {
  if (!isApiHttpError(error) || error.status !== 409) return null;
  let body: unknown;
  try {
    body = JSON.parse(error.responseBody);
  } catch {
    return null;
  }
  if (typeof body !== "object" || body === null) return null;
  const outer = body as { detail?: unknown };
  const payload =
    typeof outer.detail === "object" && outer.detail !== null
      ? outer.detail
      : body;
  const typed = payload as {
    code?: unknown;
    incomplete_subtasks?: unknown;
  };
  if (
    typed.code !== "incomplete_subtasks_confirmation_required" ||
    !Array.isArray(typed.incomplete_subtasks)
  ) {
    return null;
  }
  return typed.incomplete_subtasks.filter(
    (item): item is IncompleteSubtaskSummary =>
      typeof item === "object" &&
      item !== null &&
      typeof (item as IncompleteSubtaskSummary).id === "string" &&
      typeof (item as IncompleteSubtaskSummary).title === "string" &&
      typeof (item as IncompleteSubtaskSummary).status === "string",
  );
}

function showCompletionConfirmation(
  incompleteSubtasks: IncompleteSubtaskSummary[],
): Promise<boolean> {
  const titles = incompleteSubtasks
    .slice(0, 3)
    .map((subtask) => `「${subtask.title}」`)
    .join("、");
  const remaining = incompleteSubtasks.length - 3;
  const summary = remaining > 0 ? `${titles} ほか${remaining}件` : titles;
  return new Promise((resolve) => {
    Alert.alert(
      "サブタスクも完了しますか？",
      `未完了の直下サブタスク${incompleteSubtasks.length}件も同時に完了します。${summary}`,
      [
        { text: "キャンセル", style: "cancel", onPress: () => resolve(false) },
        { text: "すべて完了", onPress: () => resolve(true) },
      ],
      { cancelable: true, onDismiss: () => resolve(false) },
    );
  });
}

function requestCompletionConfirmation(
  taskId: string,
  incompleteSubtasks: IncompleteSubtaskSummary[],
): Promise<boolean> {
  const existing = pendingCompletionConfirmationByTaskId.get(taskId);
  if (existing) return existing;
  let pending: Promise<boolean>;
  pending = completionConfirmationQueue
    .then(() => showCompletionConfirmation(incompleteSubtasks))
    .finally(() => {
      if (pendingCompletionConfirmationByTaskId.get(taskId) === pending) {
        pendingCompletionConfirmationByTaskId.delete(taskId);
      }
    });
  pendingCompletionConfirmationByTaskId.set(taskId, pending);
  completionConfirmationQueue = pending.then(
    () => undefined,
    () => undefined,
  );
  return pending;
}

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
    auto_close_on_due:
      (task as unknown as Task).auto_close_on_due === true,
  } as T;
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
    const normalized = normalizePayload(data);
    try {
      return await fetchApi<Task>(`/api/tasks/${taskId}`, {
        method: "PATCH",
        body: JSON.stringify(normalized),
      }).then(normalizeTask);
    } catch (error) {
      const incompleteSubtasks = parseIncompleteSubtasks(error);
      if (!incompleteSubtasks) throw error;
      const confirmed = await requestCompletionConfirmation(
        taskId,
        incompleteSubtasks,
      );
      if (!confirmed) throw new TaskCompletionCancelledError();
      return fetchApi<Task>(`/api/tasks/${taskId}`, {
        method: "PATCH",
        body: JSON.stringify({
          ...normalized,
          close_incomplete_subtasks: true,
        }),
      }).then(normalizeTask);
    }
  },

  async deleteTask(taskId: string): Promise<void> {
    await fetchApi(`/api/tasks/${taskId}`, { method: "DELETE" });
  },

  /** Restore the current deletion batch within the server retention window. */
  async restoreTask(
    taskId: string,
    deletionBatchId?: string | null,
  ): Promise<TaskRestoreResponse> {
    const payload = deletionBatchId
      ? { deletion_batch_id: deletionBatchId }
      : {};
    return fetchApi<TaskRestoreResponse>(`/api/tasks/${taskId}/restore`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
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

  /** Update a canonical project/space tag. */
  async updateTag(
    tagId: string,
    data: { name?: string; color?: string | null },
  ): Promise<Tag> {
    return fetchApi<Tag>(`/api/tags/${tagId}`, {
      method: "PATCH",
      body: JSON.stringify(data),
    });
  },

  /** Copy a tag into another Space, preserving the server-side ACL check. */
  async copyTagToSpace(tagId: string, spaceId: string): Promise<Tag> {
    return fetchApi<Tag>(`/api/tags/${tagId}/copy`, {
      method: "POST",
      body: JSON.stringify({ space_id: spaceId }),
    });
  },

  async deleteTag(tagId: string): Promise<void> {
    await fetchApi(`/api/tags/${tagId}`, { method: "DELETE" });
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

  async getTaskRecurrence(taskId: string): Promise<TaskRecurrence | null> {
    return fetchApi<TaskRecurrence | null>(
      `/api/tasks/${taskId}/recurrence`,
    );
  },

  async upsertTaskRecurrence(
    taskId: string,
    data: TaskRecurrencePayload,
  ): Promise<TaskRecurrence> {
    return fetchApi<TaskRecurrence>(`/api/tasks/${taskId}/recurrence`, {
      method: "PUT",
      body: JSON.stringify(data),
    });
  },

  async deleteTaskRecurrence(taskId: string): Promise<void> {
    await fetchApi(`/api/tasks/${taskId}/recurrence`, { method: "DELETE" });
  },

  async listTaskReferences(taskId: string): Promise<TaskReference[]> {
    return fetchApi<TaskReference[]>(`/api/tasks/${taskId}/references`);
  },

  async createTaskReference(
    taskId: string,
    data: TaskReferencePayload,
  ): Promise<TaskReference> {
    return fetchApi<TaskReference>(`/api/tasks/${taskId}/references`, {
      method: "POST",
      body: JSON.stringify(data),
    });
  },

  async deleteTaskReference(
    taskId: string,
    referenceId: string,
    options?: { confirmSource?: boolean },
  ): Promise<void> {
    const query =
      options?.confirmSource === true ? "?confirm_source=true" : "";
    await fetchApi(
      `/api/tasks/${taskId}/references/${encodeURIComponent(referenceId)}${query}`,
      { method: "DELETE" },
    );
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

  async updateOccurrence(
    occurrenceId: string,
    data: {
      status?: string;
      start_at?: string | null;
      end_at?: string | null;
      reminder_offsets?: number[];
    },
  ): Promise<TaskOccurrence> {
    return fetchApi<TaskOccurrence>(
      `/api/task-occurrences/${occurrenceId}`,
      {
        method: "PATCH",
        body: JSON.stringify(normalizePayload(data)),
      },
    ).then(normalizeOccurrence);
  },

  /**
   * There is no destructive occurrence endpoint in the canonical API.  The
   * supported delete semantic is a cancelled occurrence, which keeps the
   * row/tombstone available for sync and undo-safe local caches.
   */
  async deleteOccurrence(occurrenceId: string): Promise<TaskOccurrence> {
    return this.updateOccurrence(occurrenceId, { status: "cancelled" });
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

  async logTime(data: {
    task_id: string;
    occurrence_id?: string | null;
    started_at: string;
    ended_at: string;
    note?: string | null;
    source?: string;
  }): Promise<TimeEntry> {
    return fetchApi<TimeEntry>("/api/time-entries/log", {
      method: "POST",
      body: JSON.stringify({ source: "manual", ...data }),
    });
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
