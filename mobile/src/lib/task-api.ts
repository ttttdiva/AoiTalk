/**
 * Task-related mobile API client.
 */

import { Alert } from "react-native";

import { fetchApi, getBaseUrl, isApiHttpError } from "./api-client";
import { sendWithTransportAuth } from "./transport-auth";
import { nativeMultipartUpload, type NativeMultipartFile } from "./native-multipart";
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

type TaskAssigneeResponse = {
  id?: unknown;
  task_id?: unknown;
  user_id?: unknown;
  is_primary?: unknown;
  display_name?: unknown;
  username?: unknown;
};

type TaskTagResponse = {
  id?: unknown;
  project_id?: unknown;
  name?: unknown;
  color?: unknown;
  created_by?: unknown;
  created_at?: unknown;
};

type LegacyTaskAssigneeFields = {
  assignees?: unknown;
  assignee_ids?: unknown;
  primary_assignee_user_id?: unknown;
  primary_assignee_display_name?: unknown;
  primary_assignee_username?: unknown;
  primary_assignee_name?: unknown;
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

function taskAssigneeFromId(
  taskId: string,
  userId: string,
  index: number,
): Task["assignees"][number] {
  return {
    id: `legacy-${taskId}-${userId}`,
    task_id: taskId,
    user_id: userId,
    is_primary: index === 0,
  };
}

function normalizeAssignees(
  task: Record<string, unknown>,
  taskId: string,
): {
  assignees: Task["assignees"];
  complete: boolean;
} {
  const rawAssignees = task.assignees;
  if (Array.isArray(rawAssignees)) {
    const normalized: Task["assignees"] = [];
    for (const [index, item] of rawAssignees.entries()) {
      if (!item || typeof item !== "object" || Array.isArray(item)) {
        return { assignees: [], complete: false };
      }
      const candidate = item as TaskAssigneeResponse;
      if (typeof candidate.user_id !== "string" || !candidate.user_id.trim()) {
        return { assignees: [], complete: false };
      }
      normalized.push({
        id:
          typeof candidate.id === "string" && candidate.id.trim()
            ? candidate.id
            : `legacy-${taskId}-${candidate.user_id}`,
        task_id:
          typeof candidate.task_id === "string" && candidate.task_id.trim()
            ? candidate.task_id
            : taskId,
        user_id: candidate.user_id,
        is_primary:
          typeof candidate.is_primary === "boolean"
            ? candidate.is_primary
            : index === 0,
        ...(typeof candidate.display_name === "string"
          ? { display_name: candidate.display_name }
          : {}),
        ...(typeof candidate.username === "string"
          ? { username: candidate.username }
          : {}),
      });
    }
    // An explicitly returned collection, including [], is authoritative.
    return { assignees: normalized, complete: true };
  }

  const rawAssigneeIds = task.assignee_ids;
  if (Array.isArray(rawAssigneeIds)) {
    const ids = rawAssigneeIds.filter(
      (value): value is string =>
        typeof value === "string" && Boolean(value.trim()),
    );
    if (ids.length !== rawAssigneeIds.length) {
      return { assignees: [], complete: false };
    }
    return {
      assignees: ids.map((userId, index) =>
        taskAssigneeFromId(taskId, userId, index),
      ),
      complete: true,
    };
  }

  if (Object.prototype.hasOwnProperty.call(task, "primary_assignee_user_id")) {
    const primaryId = task.primary_assignee_user_id;
    if (typeof primaryId === "string" && primaryId.trim()) {
      const primary = taskAssigneeFromId(taskId, primaryId, 0);
      const displayName =
        typeof task.primary_assignee_display_name === "string"
          ? task.primary_assignee_display_name
          : typeof task.primary_assignee_name === "string"
            ? task.primary_assignee_name
            : undefined;
      const username =
        typeof task.primary_assignee_username === "string"
          ? task.primary_assignee_username
          : undefined;
      return {
        assignees: [
          {
            ...primary,
            ...(displayName ? { display_name: displayName } : {}),
            ...(username ? { username } : {}),
          },
        ],
        complete: true,
      };
    }
    // Legacy primary-only responses used null to mean that the task has no
    // primary assignment.  Unlike an omitted field, that is authoritative.
    return { assignees: [], complete: true };
  }

  // `null` and omission are both an incomplete aggregate on old/detail
  // projections.  Preserve the distinction from an explicit [] so callers
  // can omit assignee_ids from unrelated updates.
  return { assignees: [], complete: false };
}

function normalizeTags(
  task: Record<string, unknown>,
): {
  tags: Task["tags"];
  complete: boolean;
} {
  if (!Array.isArray(task.tags)) {
    // Older/compact task responses omit tags (or return null).  The
    // repository must retain its cached aggregate until a full response
    // arrives instead of interpreting this as an explicit clear.
    return { tags: [], complete: false };
  }
  const normalized: Task["tags"] = [];
  for (const item of task.tags) {
    if (!item || typeof item !== "object" || Array.isArray(item)) {
      return { tags: [], complete: false };
    }
    const candidate = item as TaskTagResponse;
    if (
      typeof candidate.id !== "string" ||
      !candidate.id.trim() ||
      typeof candidate.name !== "string"
    ) {
      return { tags: [], complete: false };
    }
    normalized.push({
      id: candidate.id,
      project_id:
        typeof candidate.project_id === "string" ? candidate.project_id : "",
      name: candidate.name,
      ...(typeof candidate.color === "string"
        ? { color: candidate.color }
        : {}),
      ...(typeof candidate.created_by === "string"
        ? { created_by: candidate.created_by }
        : {}),
      ...(typeof candidate.created_at === "string"
        ? { created_at: candidate.created_at }
        : {}),
    });
  }
  return { tags: normalized, complete: true };
}

export function normalizeTask<T extends { status: string }>(
  task: T,
): T &
  Pick<Task, "assignees" | "assignees_complete" | "tags" | "tags_complete"> {
  const raw = task as T & LegacyTaskAssigneeFields & Record<string, unknown>;
  const normalizedAssignees = normalizeAssignees(raw, String(raw.id ?? ""));
  const normalizedTags = normalizeTags(raw);
  return {
    ...task,
    status: normalizeTaskStatus(task.status),
    auto_close_on_due:
      (task as unknown as Task).auto_close_on_due === true,
    assignees: normalizedAssignees.assignees,
    assignees_complete: normalizedAssignees.complete,
    tags: normalizedTags.tags,
    tags_complete: normalizedTags.complete,
  } as T &
    Pick<Task, "assignees" | "assignees_complete" | "tags" | "tags_complete">;
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

async function uploadFile<T>(path: string, file: NativeMultipartFile): Promise<T> {
  const baseUrl = await getBaseUrl();
  const url = `${baseUrl}${path}`;
  const res = await sendWithTransportAuth(url, { targetBaseUrl: baseUrl }, (headers) =>
    nativeMultipartUpload({
      url,
      headers,
      file,
    }),
  );
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
    return uploadFile<TaskAttachment>(`/api/tasks/${taskId}/attachments`, {
      uri: file.uri,
      name: file.name,
      mimeType: file.mimeType || "application/octet-stream",
    });
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
