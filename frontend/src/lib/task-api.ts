// ブラウザから直接呼ぶ用（"use client"コンポーネントから使用）
// 全て /api/ 経由（Next.js Route Handler）

import {
  normalizeRecurrenceRule,
  normalizeTask,
  normalizeTaskStatus,
} from "@/lib/task-status";
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
  project_name?: string | null;
  project_color?: string | null;
  title: string;
  description?: string | null;
  status: string;
  priority: string;
  start_at?: string | null;
  end_at?: string | null;
  all_day: boolean;
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
};

export type TimeReportBucket = {
  key: string;
  label: string;
  seconds: number;
  entries: number;
  project_id?: string | null;
  project_name?: string | null;
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
};

export type Project = {
  id: string;
  name: string;
  description?: string | null;
  slug: string;
  aliases?: string[];
  space_id?: string | null;
  is_completed?: boolean;
  color?: string | null;
  metadata?: Record<string, unknown>;
};

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    credentials: "include",
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
    signal: init?.signal ?? AbortSignal.timeout(5000),
  });
  if (res.status === 401) {
    if (typeof window !== "undefined") {
      window.location.href = "/login";
    }
    throw new Error("認証が必要です");
  }
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(detail.detail || res.statusText);
  }
  if (res.status === 204) return undefined as T;
  return res.json();
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
    throw new Error("Authentication required");
  }
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(detail.detail || res.statusText);
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
  return {
    ...normalizeTask(task),
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
  return next;
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
    return request<Task[]>(`/api/tasks${qs ? `?${qs}` : ""}`, init).then(
      (tasks) => tasks.map(normalizeTaskResponse),
    );
  },
  getTask: (taskId: string) =>
    request<Task>(`/api/tasks/${taskId}`).then(normalizeTaskResponse),
  createTask: (data: Record<string, unknown>) =>
    request<Task>("/api/tasks", {
      method: "POST",
      body: JSON.stringify(normalizeTaskPayload(data)),
    }).then(normalizeTaskResponse),
  updateTask: (taskId: string, data: Record<string, unknown>) =>
    request<Task>(`/api/tasks/${taskId}`, {
      method: "PATCH",
      body: JSON.stringify(normalizeTaskPayload(data)),
    }).then(normalizeTaskResponse),
  moveTask: (taskId: string, data: Record<string, unknown>) =>
    request<Task>(`/api/tasks/${taskId}`, {
      method: "PATCH",
      body: JSON.stringify(
        normalizeTaskPayload({ ...data, project_move_intent: true }),
      ),
    }).then(normalizeTaskResponse),
  deleteTask: (taskId: string) =>
    request<void>(`/api/tasks/${taskId}`, { method: "DELETE" }),
  reorderTasks: (projectId: string, taskIds: string[]) =>
    request<void>(`/api/projects/${projectId}/tasks/reorder`, {
      method: "POST",
      body: JSON.stringify({ task_ids: taskIds }),
    }),
  reorderAllTasks: (taskIds: string[]) =>
    request<void>("/api/tasks/reorder", {
      method: "POST",
      body: JSON.stringify({ task_ids: taskIds }),
    }),

  // Recurrence
  getRecurrence: (taskId: string) =>
    request<RecurrenceRule | null>(`/api/tasks/${taskId}/recurrence`).then(
      (rule) => (rule ? normalizeRecurrenceRule(rule) : null),
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
    },
  ) =>
    request<RecurrenceRule>(`/api/tasks/${taskId}/recurrence`, {
      method: "PUT",
      body: JSON.stringify(normalizeRecurrencePayload(data)),
    }).then(normalizeRecurrenceRule),
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
    request<{ projects: Project[]; total: number }>("/api/projects"),
  updateProject: (id: string, data: Record<string, unknown>) =>
    request<Project>(`/api/projects/${id}`, {
      method: "PATCH",
      body: JSON.stringify(data),
    }),

  // Spaces
  listSpaces: () => request<{ spaces: Space[]; total: number }>("/api/spaces"),
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
