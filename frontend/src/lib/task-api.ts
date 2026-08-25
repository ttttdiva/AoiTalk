// ブラウザから直接呼ぶ用（"use client"コンポーネントから使用）
// 全て /api/ 経由（Next.js Route Handler）

import {
  normalizeRecurrenceRule,
  normalizeTask,
  normalizeTaskStatus,
} from "@/lib/task-status";
import { normalizeSkipMode } from "@/lib/recurrence-preview";
import {
  requestTaskCompletionConfirmation,
  TaskCompletionCancelledError,
  type IncompleteSubtaskSummary,
} from "@/lib/task-completion-confirmation";
import { toast } from "sonner";

export type Tag = {
  id: string;
  space_id: string;
  name: string;
  color?: string | null;
  created_by?: string | null;
  created_at?: string | null;
};

export type TaskAssignee = {
  id: string;
  task_id: string;
  user_id: string;
  is_primary: boolean;
  display_name?: string | null;
  username?: string | null;
};

export type TaskAssigneeCandidate = {
  user_id: string;
  username?: string | null;
  display_name?: string | null;
};

export type TaskComment = {
  id: string;
  task_id: string;
  user_id?: string | null;
  content: string;
  created_at?: string | null;
  updated_at?: string | null;
  display_name?: string | null;
  username?: string | null;
};

export type Task = {
  id: string;
  project_id: string;
  knowledge_node_id?: string | null;
  project_name?: string | null;
  project_color?: string | null;
  title: string;
  description?: string | null;
  status: string;
  priority: string;
  start_at?: string | null;
  end_at?: string | null;
  all_day: boolean;
  /** 期日 (end_at) 到達時に自動で完了する設定。旧レスポンス互換で未指定は false。 */
  auto_close_on_due?: boolean;
  reminder_offsets: number[];
  notifications_enabled: boolean;
  source: string;
  created_by?: string | null;
  completed_at?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  metadata: Record<string, unknown>;
  assignees: TaskAssignee[];
  tags: Tag[];
  active_time_entry?: TimeEntry | null;
  estimated_hours?: number | null;
  sort_order?: number;
  total_time_seconds?: number;
  parent_task_id?: string | null;
  subtasks?: Task[];
  comments?: TaskComment[];
  activities?: { id: string; duration_seconds?: number | null }[];
  has_recurrence?: boolean;
  effective_start_at?: string | null;
  effective_end_at?: string | null;
  effective_all_day?: boolean | null;
  effective_occurrence_id?: string | null;
  effective_occurrence_start_at?: string | null;
  effective_occurrence_end_at?: string | null;
  effective_occurrence_original_start_at?: string | null;
  effective_occurrence_source_kind?: string | null;
  effective_occurrence_status?: string | null;
  google_calendar_sync?: GoogleCalendarSyncResult;
  remote_server_id?: string;
  remote_server_name?: string;
  remote_server_color?: string | null;
  remote_server_base_url?: string;
  resource_id?: string;
};

export type TaskAttachment = {
  id: string;
  task_id: string;
  project_id: string;
  file_path: string;
  display_name: string;
  mime_type?: string | null;
  size_bytes?: number | null;
  kind: "image" | "file" | string;
  created_by?: string | null;
  created_at?: string | null;
  metadata?: Record<string, unknown>;
  url?: string;
};

export type TaskAgentTriageResult = {
  task_id: string;
  status:
    | "pending"
    | "in_progress"
    | "needs_user"
    | "ready"
    | "done"
    | "failed"
    | string;
  summary: string;
  questions: string[];
  metadata: Record<string, unknown>;
  task?: unknown;
};

export type GoogleCalendarSyncResult = {
  status: "created" | "updated" | "deleted" | "skipped" | "warning";
  reason?: string;
  message?: string;
  event_id?: string | null;
  html_link?: string | null;
  calendar_id?: string | null;
};

export type RecurrenceRule = {
  id: string;
  task_id: string;
  rrule: string;
  timezone: string;
  horizon_days: number;
  trigger_status: string;
  create_new: boolean;
  recur_forever: boolean;
  reset_status_to: string;
  end_count?: number | null;
  end_date?: string | null;
  skip_weekend?: boolean;
  skip_holiday?: boolean;
  skip_mode?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
};

export type TaskOccurrence = {
  id: string;
  task_id: string;
  project_id?: string | null;
  project_name?: string | null;
  project_color?: string | null;
  status: string;
  start_at?: string | null;
  end_at?: string | null;
  all_day: boolean;
  title?: string | null;
  source_kind: string;
  is_generated?: boolean;
  original_start_at?: string | null;
  tags?: Tag[];
  source?: "local" | "remote" | string;
  remote_server_id?: string;
  resource_id?: string;
};

export type RecurringOccurrenceContext = {
  occurrence_id?: string | null;
  start_at: string;
  end_at?: string | null;
  original_start_at?: string | null;
  source_kind: string;
  status?: string | null;
};

export type TimeEntry = {
  id: string;
  task_id: string;
  user_id: string;
  started_at?: string | null;
  ended_at?: string | null;
  duration_seconds?: number | null;
  source: string;
  note?: string | null;
  task_title?: string | null;
  project_id?: string | null;
  project_name?: string | null;
  project_color?: string | null;
  space_id?: string | null;
  space_name?: string | null;
  original_started_at?: string | null;
  original_ended_at?: string | null;
  remote_server_id?: string;
  remote_server_name?: string;
  remote_server_color?: string | null;
  remote_server_base_url?: string;
  resource_id?: string;
};

export type TimeReportBucket = {
  key: string;
  label: string;
  seconds: number;
  entries: number;
  project_id?: string | null;
  project_name?: string | null;
  source?: "local" | "remote" | string;
  remote_server_id?: string;
  resource_id?: string;
};

export type TimeReport = {
  summary: {
    total_seconds: number;
    entry_count: number;
    active_entries: number;
  };
  by_project: TimeReportBucket[];
  by_day: TimeReportBucket[];
  by_user: TimeReportBucket[];
  by_task: TimeReportBucket[];
};

export type Space = {
  id: string;
  name: string;
  slug: string;
  description?: string | null;
  color?: string | null;
  owner_id?: string;
  sort_order?: number;
  created_at?: string | null;
  updated_at?: string | null;
  /** Explicit server-side capability used by Tasks/Projects color editing. */
  can_write?: boolean;
  source?: "local" | "remote" | string;
  remote_server_id?: string;
  remote_server_name?: string;
  remote_server_color?: string | null;
  remote_server_base_url?: string;
  resource_id?: string;
};

export type Project = {
  id: string;
  name: string;
  description?: string | null;
  slug: string;
  aliases?: string[];
  space_id?: string | null;
  knowledge_node_id?: string | null;
  is_completed?: boolean;
  can_write?: boolean;
  /** Explicit server-side capability required by the Project PATCH route. */
  can_manage_settings?: boolean;
  /** Accessible via authorization; participating only when owner/member read. */
  is_participating?: boolean;
  membership?: { role: string | null; permissions?: Record<string, unknown> | null } | null;
  color?: string | null;
  metadata?: Record<string, unknown> & {
    workspace_tools_enabled?: boolean;
  };
  source?: "local" | "remote" | string;
  remote_server_id?: string;
  remote_server_name?: string;
  remote_server_color?: string | null;
  remote_server_base_url?: string;
  resource_id?: string;
};

export type TaskReference = {
  id: string;
  reference_type: string;
  relation_type: "source" | "related" | string;
  display_name: string;
  subtitle?: string | null;
  target_id?: string | null;
  target_path?: string | null;
  target_url?: string | null;
  metadata?: Record<string, unknown>;
  created_by?: string | null;
  created_at?: string | null;
  can_remove: boolean;
  exists: boolean;
  open: { id?: string | null; path?: string | null; url?: string | null };
  attachment?: TaskAttachment;
};

export type TaskAppLink = {
  id: string;
  task_id: string;
  app_id: string;
  target_id?: string | null;
  relation_type: "develops" | "fixes" | "tests" | "releases" | "uses" | "related" | string;
  created_by?: string | null;
  created_at?: string | null;
  app?: {
    id: string;
    name: string;
    related_project_ids?: string[];
  };
  target?: {
    id: string;
    target_key: string;
    display_name: string;
  } | null;
};

export type TaskDocsNode = {
  id: string;
  title: string;
  project_id?: string | null;
};

export type TaskDocsNodeResult = {
  node: TaskDocsNode;
  created: boolean;
};

export class TaskApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly responseBody: unknown = null,
  ) {
    super(message);
    this.name = "TaskApiError";
  }
}

async function request<T>(
  path: string,
  init?: RequestInit,
  timeoutMs = 5000,
): Promise<T> {
  const res = await fetch(path, {
    credentials: "include",
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
    signal: init?.signal
      ? AbortSignal.any([init.signal, AbortSignal.timeout(timeoutMs)])
      : AbortSignal.timeout(timeoutMs),
  });
  if (res.status === 401) {
    const shouldRedirectToLogin = ![
      "/api/time-entries/active",
      "/api/spaces",
      "/api/projects",
    ].includes(path);
    if (shouldRedirectToLogin && typeof window !== "undefined") {
      window.location.href = "/login";
    }
    throw new TaskApiError("認証が必要です", 401, null);
  }
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    const message =
      typeof detail?.detail === "string" ? detail.detail : res.statusText;
    throw new TaskApiError(message || "API request failed", res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}

function isRetryableListError(error: unknown): boolean {
  if (error instanceof TaskApiError) {
    return error.status === 408 || error.status === 429 || error.status >= 500;
  }
  return (
    error instanceof TypeError ||
    (error instanceof DOMException && error.name === "TimeoutError")
  );
}

async function requestList<T>(path: string, init?: RequestInit): Promise<T> {
  const attempts = 2;
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    try {
      return await request<T>(path, init, 30000);
    } catch (error) {
      if (attempt === attempts - 1 || !isRetryableListError(error)) throw error;
      await new Promise((resolve) => setTimeout(resolve, 300));
    }
  }
  throw new Error("一覧を取得できませんでした");
}

async function uploadRequest<T>(path: string, form: FormData): Promise<T> {
  const res = await fetch(path, {
    method: "POST",
    credentials: "include",
    body: form,
    signal: AbortSignal.timeout(30000),
  });
  if (res.status === 401) {
    if (typeof window !== "undefined") {
      window.location.href = "/login";
    }
    throw new TaskApiError("Authentication required", 401);
  }
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    const message =
      typeof detail?.detail === "string" && detail.detail.trim()
        ? detail.detail
        : res.statusText || `HTTP ${res.status}`;
    throw new TaskApiError(message, res.status);
  }
  return res.json();
}

function normalizeTaskResponse(task: Task): Task {
  if (
    typeof window !== "undefined" &&
    task.google_calendar_sync?.status === "warning"
  ) {
    toast.warning(
      `Google Calendar sync failed: ${
        task.google_calendar_sync.message || "unknown error"
      }`,
    );
  }
  const normalized = normalizeTask(task);
  return {
    ...normalized,
    auto_close_on_due: task.auto_close_on_due === true,
    subtasks: task.subtasks?.map(normalizeTaskResponse),
  };
}

function normalizeOccurrenceResponse(
  occurrence: TaskOccurrence,
): TaskOccurrence {
  return {
    ...occurrence,
    status: normalizeTaskStatus(occurrence.status),
  };
}

function normalizeTaskPayload(
  data: Record<string, unknown>,
): Record<string, unknown> {
  if (!("status" in data)) return data;
  return {
    ...data,
    status: normalizeTaskStatus(data.status),
  };
}

function getIncompleteSubtasksConfirmation(
  error: unknown,
): IncompleteSubtaskSummary[] | null {
  if (!(error instanceof TaskApiError) || error.status !== 409) return null;
  const body = error.responseBody;
  if (typeof body !== "object" || body === null) return null;
  const payload = body as {
    code?: unknown;
    incomplete_subtasks?: unknown;
  };
  if (
    payload.code !== "incomplete_subtasks_confirmation_required" ||
    !Array.isArray(payload.incomplete_subtasks)
  ) {
    return null;
  }
  return payload.incomplete_subtasks.filter(
    (item): item is IncompleteSubtaskSummary =>
      typeof item === "object" &&
      item !== null &&
      typeof (item as IncompleteSubtaskSummary).id === "string" &&
      typeof (item as IncompleteSubtaskSummary).title === "string" &&
      typeof (item as IncompleteSubtaskSummary).status === "string",
  );
}

async function updateTaskWithSubtaskConfirmation(
  taskId: string,
  data: Record<string, unknown>,
): Promise<Task> {
  const normalized = normalizeTaskPayload(data);
  try {
    return await request<Task>(`/api/tasks/${taskId}`, {
      method: "PATCH",
      body: JSON.stringify(normalized),
    }).then(normalizeTaskResponse);
  } catch (error) {
    const incompleteSubtasks = getIncompleteSubtasksConfirmation(error);
    if (!incompleteSubtasks) throw error;
    const confirmed = await requestTaskCompletionConfirmation({
      taskId,
      incompleteSubtasks,
    });
    if (!confirmed) throw new TaskCompletionCancelledError();
    return request<Task>(`/api/tasks/${taskId}`, {
      method: "PATCH",
      body: JSON.stringify({
        ...normalized,
        close_incomplete_subtasks: true,
      }),
    }).then(normalizeTaskResponse);
  }
}

function normalizeRecurrencePayload(
  data: Record<string, unknown>,
): Record<string, unknown> {
  const next = { ...data };
  if ("trigger_status" in next) {
    next.trigger_status = normalizeTaskStatus(next.trigger_status);
  }
  if ("reset_status_to" in next) {
    next.reset_status_to = normalizeTaskStatus(next.reset_status_to);
  }
  if ("skip_mode" in next) {
    next.skip_mode = normalizeSkipMode(
      typeof next.skip_mode === "string" ? next.skip_mode : null,
    );
  }
  return next;
}

function normalizeRecurrenceResponse(rule: RecurrenceRule): RecurrenceRule {
  return {
    ...normalizeRecurrenceRule(rule),
    skip_mode: normalizeSkipMode(rule.skip_mode),
  };
}

export type Scope = { project_id?: string; space_id?: string };

function buildScopeQuery(scope: Scope | string | undefined): URLSearchParams {
  const params = new URLSearchParams();
  if (typeof scope === "string") {
    params.set("project_id", scope);
  } else if (scope) {
    if (scope.project_id) params.set("project_id", scope.project_id);
    if (scope.space_id) params.set("space_id", scope.space_id);
  }
  return params;
}

export const taskApi = {
  // Tasks
  listTasks: (scope?: Scope | string, init?: RequestInit) => {
    const params = buildScopeQuery(scope);
    const qs = params.toString();
    const path = `/api/tasks${qs ? `?${qs}` : ""}`;
    return requestList<Task[]>(path, init).then((tasks) =>
      tasks.map(normalizeTaskResponse),
    );
  },
  getTask: (taskId: string) =>
    request<Task>(`/api/tasks/${taskId}`).then(normalizeTaskResponse),
  createTask: (data: Record<string, unknown>) =>
    request<Task>("/api/tasks", {
      method: "POST",
      body: JSON.stringify(normalizeTaskPayload(data)),
    }).then(normalizeTaskResponse),
  updateTask: updateTaskWithSubtaskConfirmation,
  moveTask: (taskId: string, data: Record<string, unknown>) =>
    request<Task>(`/api/tasks/${taskId}`, {
      method: "PATCH",
      body: JSON.stringify(
        normalizeTaskPayload({ ...data, project_move_intent: true }),
      ),
    }).then(normalizeTaskResponse),
  ensureDocsNode: (taskId: string) =>
    request<TaskDocsNodeResult>(`/api/tasks/${taskId}/docs-node`, {
      method: "POST",
    }),
  deleteTask: (taskId: string) =>
    request<void>(`/api/tasks/${taskId}`, { method: "DELETE" }),
  reorderTasks: (
    projectId: string,
    taskIds: string[],
    parentTaskId?: string | null,
  ) =>
    request<void>(`/api/projects/${projectId}/tasks/reorder`, {
      method: "POST",
      body: JSON.stringify({
        task_ids: taskIds,
        ...(parentTaskId !== undefined
          ? { parent_task_id: parentTaskId }
          : {}),
      }),
    }),
  reorderAllTasks: (taskIds: string[]) =>
    request<void>("/api/tasks/reorder", {
      method: "POST",
      body: JSON.stringify({ task_ids: taskIds }),
    }),

  // Recurrence
  getRecurrence: (taskId: string) =>
    request<RecurrenceRule | null>(`/api/tasks/${taskId}/recurrence`).then(
      (rule) => (rule ? normalizeRecurrenceResponse(rule) : null),
    ),
  saveRecurrence: (
    taskId: string,
    data: {
      rrule: string;
      timezone?: string;
      horizon_days?: number;
      trigger_status?: string;
      create_new?: boolean;
      recur_forever?: boolean;
      reset_status_to?: string;
      end_count?: number | null;
      end_date?: string | null;
      skip_weekend?: boolean;
      skip_holiday?: boolean;
      skip_mode?: string;
    },
  ) =>
    request<RecurrenceRule>(`/api/tasks/${taskId}/recurrence`, {
      method: "PUT",
      body: JSON.stringify(normalizeRecurrencePayload(data)),
    }).then(normalizeRecurrenceResponse),
  deleteRecurrence: (taskId: string) =>
    request<void>(`/api/tasks/${taskId}/recurrence`, { method: "DELETE" }),

  // Tags
  listTags: (projectId: string) =>
    request<Tag[]>(`/api/projects/${projectId}/tags`),
  createTag: (projectId: string, data: { name: string; color?: string }) =>
    request<Tag>(`/api/projects/${projectId}/tags`, {
      method: "POST",
      body: JSON.stringify(data),
    }),
  copyTagToSpace: (tagId: string, spaceId: string) =>
    request<Tag>(`/api/tags/${tagId}/copy`, {
      method: "POST",
      body: JSON.stringify({ space_id: spaceId }),
    }),
  updateTag: (tagId: string, data: { name?: string; color?: string }) =>
    request<Tag>(`/api/tags/${tagId}`, {
      method: "PATCH",
      body: JSON.stringify(data),
    }),
  deleteTag: (tagId: string) =>
    request<void>(`/api/tags/${tagId}`, { method: "DELETE" }),

  // Time entries
  listTimeEntries: (
    scope: Scope | string,
    dateFrom?: string,
    dateTo?: string,
  ) => {
    const params = buildScopeQuery(scope);
    if (dateFrom) params.set("date_from", dateFrom);
    if (dateTo) params.set("date_to", dateTo);
    return request<TimeEntry[]>(`/api/time-entries?${params}`);
  },
  updateTimeEntry: (
    id: string,
    data: { started_at?: string; ended_at?: string; note?: string },
  ) =>
    request<TimeEntry>(`/api/time-entries/${id}`, {
      method: "PATCH",
      body: JSON.stringify(data),
    }),
  deleteTimeEntry: (id: string) =>
    request<{ success: boolean }>(`/api/time-entries/${id}`, {
      method: "DELETE",
    }),
  startTimer: (taskId: string, occurrenceId?: string) =>
    request<TimeEntry>("/api/time-entries/start", {
      method: "POST",
      body: JSON.stringify({ task_id: taskId, occurrence_id: occurrenceId }),
    }),
  stopTimer: (timeEntryId?: string) =>
    request<TimeEntry>("/api/time-entries/stop", {
      method: "POST",
      body: JSON.stringify({ time_entry_id: timeEntryId }),
    }),
  createTimeEntry: (data: {
    task_id: string;
    started_at: string;
    ended_at: string;
    note?: string;
  }) =>
    request<TimeEntry>("/api/time-entries", {
      method: "POST",
      body: JSON.stringify(data),
    }),
  getActiveTimeEntry: () =>
    request<TimeEntry | null>("/api/time-entries/active"),

  // Comments
  addComment: (taskId: string, content: string) =>
    request<unknown>(`/api/tasks/${taskId}/comments`, {
      method: "POST",
      body: JSON.stringify({ content }),
    }),

  // Attachments
  listAttachments: (taskId: string) =>
    request<TaskAttachment[]>(`/api/tasks/${taskId}/attachments`),
  uploadAttachment: (taskId: string, file: File) => {
    const form = new FormData();
    form.append("file", file);
    return uploadRequest<TaskAttachment>(
      `/api/tasks/${taskId}/attachments`,
      form,
    );
  },
  deleteAttachment: (taskId: string, attachmentId: string) =>
    request<void>(`/api/tasks/${taskId}/attachments/${attachmentId}`, {
      method: "DELETE",
    }),
  listReferences: (taskId: string) =>
    request<TaskReference[]>(`/api/tasks/${taskId}/references`),
  addReference: (
    taskId: string,
    data: {
      reference_type: string;
      relation_type?: "source" | "related";
      target_id?: string;
      target_path?: string;
      target_url?: string;
      display_name?: string;
      metadata?: Record<string, unknown>;
    },
  ) =>
    request<TaskReference>(`/api/tasks/${taskId}/references`, {
      method: "POST",
      body: JSON.stringify(data),
    }),
  removeReference: (
    taskId: string,
    referenceId: string,
    confirmSource = false,
  ) =>
    request<void>(
      `/api/tasks/${taskId}/references/${encodeURIComponent(referenceId)}${confirmSource ? "?confirm_source=true" : ""}`,
      { method: "DELETE" },
    ),
  listAppLinks: (taskId: string) =>
    request<{ task_id: string; apps: TaskAppLink[] }>(`/api/python-proxy/tasks/${taskId}/apps`),
  linkApp: (
    taskId: string,
    data: { app_id: string; target_id?: string | null; relation_type?: TaskAppLink["relation_type"] },
  ) =>
    request<{ success: boolean; link: TaskAppLink }>(`/api/python-proxy/tasks/${taskId}/apps`, {
      method: "POST",
      body: JSON.stringify(data),
    }),
  unlinkApp: (
    taskId: string,
    appId: string,
    options?: { targetId?: string | null; relationType?: string | null },
  ) => {
    const params = new URLSearchParams();
    if (options?.targetId) params.set("target_id", options.targetId);
    if (options?.relationType) params.set("relation_type", options.relationType);
    const query = params.toString() ? `?${params.toString()}` : "";
    return request<{ success: boolean }>(`/api/python-proxy/tasks/${taskId}/apps/${appId}${query}`, { method: "DELETE" });
  },
  runAgentTriage: (taskId: string) =>
    request<TaskAgentTriageResult>(`/api/tasks/${taskId}/agent-triage`, {
      method: "POST",
    }),

  // Occurrences
  listOccurrences: (
    scope: Scope | string | undefined,
    startFrom: string,
    endTo: string,
    init?: RequestInit,
  ) => {
    const params = buildScopeQuery(scope);
    params.set("start_from", startFrom);
    params.set("end_to", endTo);
    return request<TaskOccurrence[]>(
      `/api/task-occurrences?${params}`,
      init,
    ).then((occurrences) => occurrences.map(normalizeOccurrenceResponse));
  },
  moveOccurrence: (
    taskId: string,
    data: {
      occurrence_id?: string | null;
      occurrence_start_at: string;
      occurrence_end_at?: string | null;
      original_start_at?: string | null;
      next_start_at: string;
      next_end_at?: string | null;
      status?: string | null;
      all_day?: boolean;
    },
  ) =>
    request<{
      success: boolean;
      occurrence?: {
        id: string;
        task_id: string;
        status?: string | null;
        start_at: string | null;
        end_at: string | null;
        source_kind: string;
        original_start_at: string | null;
      };
    }>(`/api/tasks/${taskId}/occurrence`, {
      method: "PATCH",
      body: JSON.stringify(data),
    }),
  updateOccurrenceStatus: (
    taskId: string,
    data: {
      occurrence_id?: string | null;
      occurrence_start_at: string;
      occurrence_end_at?: string | null;
      original_start_at?: string | null;
      status: string;
      all_day?: boolean;
    },
  ) =>
    request<{
      success: boolean;
      occurrence?: {
        id: string;
        task_id: string;
        status: string | null;
        start_at: string | null;
        end_at: string | null;
        source_kind: string;
        original_start_at: string | null;
      };
    }>(`/api/tasks/${taskId}/occurrence`, {
      method: "PATCH",
      body: JSON.stringify(data),
    }),
  deleteOccurrence: (
    taskId: string,
    data: {
      mode: "single" | "future";
      occurrence_id?: string | null;
      occurrence_start_at: string;
      occurrence_end_at?: string | null;
      original_start_at?: string | null;
    },
  ) =>
    request<{ success: boolean; deleted_task?: boolean }>(
      `/api/tasks/${taskId}/occurrence`,
      {
        method: "DELETE",
        body: JSON.stringify(data),
      },
    ),

  // Reports
  getTimeReport: (
    scope: Scope | string,
    dateFrom?: string,
    dateTo?: string,
  ) => {
    const params = buildScopeQuery(scope);
    if (dateFrom) params.set("date_from", dateFrom);
    if (dateTo) params.set("date_to", dateTo);
    return request<TimeReport>(`/api/reports/time?${params}`);
  },

  // Projects
  listProjects: () =>
    requestList<{ projects: Project[]; total: number }>("/api/projects"),
  listAssigneeCandidates: (projectId: string) =>
    request<{ members: TaskAssigneeCandidate[]; total: number }>(
      `/api/projects/${projectId}/assignee-candidates`,
    ).then((response) => response.members),
  updateProject: (id: string, data: Record<string, unknown>) =>
    request<Project>(`/api/projects/${id}`, {
      method: "PATCH",
      body: JSON.stringify(data),
    }),

  // Spaces
  listSpaces: () =>
    requestList<{ spaces: Space[]; total: number }>("/api/spaces"),
  createSpace: (data: {
    name: string;
    description?: string;
    color?: string;
    sort_order?: number;
  }) =>
    request<{ success: boolean; space: Space }>("/api/spaces", {
      method: "POST",
      body: JSON.stringify(data),
    }),
  updateSpace: (id: string, data: Record<string, unknown>) =>
    request<{ success: boolean; space: Space }>(`/api/spaces/${id}`, {
      method: "PATCH",
      body: JSON.stringify(data),
    }),
  deleteSpace: (id: string) =>
    request<{ success: boolean }>(`/api/spaces/${id}`, { method: "DELETE" }),
};
