import type React from "react";

import {
  taskApi,
  type RecurringOccurrenceContext,
  type Task,
} from "@/lib/task-api";
import {
  parseLocalDateTime,
  toLocalDateTimeInputValue,
  toTaskDatePayloadValue,
} from "@/lib/date-time";
import {
  getTaskDisplayAllDay,
  getTaskDisplayEndAt,
  getTaskDisplayStartAt,
} from "@/lib/task-effective-date";
import { formatTimerClock, getElapsedTimerSeconds } from "@/lib/task-time";

export const STATUS_LABELS: Record<string, string> = {
  open: "未着手",
  in_progress: "進行中",
  on_hold: "保留",
  review: "確認待ち",
  closed: "完了",
};

export const STATUS_DOT_COLORS: Record<string, string> = {
  open: "border-gray-400 dark:border-gray-500",
  in_progress:
    "border-red-400 bg-red-400/30 dark:border-red-500 dark:bg-red-500/30",
  on_hold:
    "border-pink-400 bg-pink-400/30 dark:border-pink-500 dark:bg-pink-500/30",
  review: "border-sky-400 bg-sky-400/30 dark:border-sky-500 dark:bg-sky-500/30",
  closed:
    "border-green-500 bg-green-500 dark:border-green-400 dark:bg-green-400",
};

export const STATUS_SHORTCUT_KEYS: Record<string, string> = {
  c: "closed",
  s: "in_progress",
  r: "review",
  h: "on_hold",
  o: "open",
  x: "open",
};

export const STATUS_KEY_HINTS: Record<string, string> = {
  closed: "C",
  in_progress: "S",
  review: "R",
  on_hold: "H",
  open: "O",
};

export function getStatusShortcutTarget(key: string): string | undefined {
  return STATUS_SHORTCUT_KEYS[key.toLowerCase()];
}

export function handleStatusShortcutCapture(
  e: React.KeyboardEvent,
  onShortcut: (status: string) => void,
) {
  const target = getStatusShortcutTarget(e.key);
  if (!target) return;
  e.preventDefault();
  e.stopPropagation();
  onShortcut(target);
}

export const TASK_LIST_COMMAND_INITIAL_VALUE = "/";
export const TASK_DND_MIME = "application/x-aoitalk-task-dnd";

export const PRIORITY_COLORS: Record<string, string> = {
  urgent: "bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-400",
  high: "bg-orange-100 text-orange-800 dark:bg-orange-900/30 dark:text-orange-400",
  medium:
    "bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-400",
  low: "bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-400",
  none: "bg-gray-100 text-gray-600 dark:bg-gray-900/30 dark:text-gray-400",
};

export const PRIORITY_LABELS: Record<string, string> = {
  urgent: "Urgent",
  high: "High",
  medium: "Medium",
  low: "Low",
  none: "-",
};

export type FilterTab = "all" | "overdue";
export type ClipboardMode = "copy" | "cut";

export type TaskClipboard = {
  tasks: Task[];
  mode: ClipboardMode;
};

export const TASK_SIDEBAR_VIEW_STATE_KEY = "tasks-sidebar-view-state";
export const TASK_PROJECT_TAB_ALL = "all";
export const TASK_PROJECT_TAB_STATE_VERSION = 2;

export type TaskSidebarViewState = {
  projectTab?: string;
  projectTabsBySpace?: Record<string, string>;
  projectTabsCollapsed?: boolean;
  projectTabStateVersion?: number;
};

export function readTaskSidebarViewState(): TaskSidebarViewState {
  if (typeof window === "undefined") return {};
  try {
    const raw = window.localStorage.getItem(TASK_SIDEBAR_VIEW_STATE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as unknown;
    return parsed && typeof parsed === "object"
      ? (parsed as TaskSidebarViewState)
      : {};
  } catch {
    return {};
  }
}

export function getSavedProjectTab(spaceId: string | null): string {
  const saved = readTaskSidebarViewState();
  if (saved.projectTabStateVersion !== TASK_PROJECT_TAB_STATE_VERSION) {
    return TASK_PROJECT_TAB_ALL;
  }
  if (saved.projectTab === TASK_PROJECT_TAB_ALL) {
    return TASK_PROJECT_TAB_ALL;
  }
  if (spaceId) {
    const spaceTab = saved.projectTabsBySpace?.[spaceId];
    if (spaceTab) return spaceTab;
  }
  return saved.projectTab ?? TASK_PROJECT_TAB_ALL;
}

export function persistProjectTabSelection(
  projectTab: string,
  spaceId: string | null,
) {
  if (typeof window === "undefined") return;
  try {
    const saved = readTaskSidebarViewState();
    const projectTabsBySpace = { ...(saved.projectTabsBySpace ?? {}) };
    if (spaceId) {
      projectTabsBySpace[spaceId] = projectTab;
    }

    window.localStorage.setItem(
      TASK_SIDEBAR_VIEW_STATE_KEY,
      JSON.stringify({
        ...saved,
        projectTab,
        projectTabStateVersion: TASK_PROJECT_TAB_STATE_VERSION,
        projectTabsBySpace,
      }),
    );
  } catch {
    // ignore
  }
}

export function getSavedProjectTabsCollapsed(): boolean {
  return readTaskSidebarViewState().projectTabsCollapsed === true;
}

export function persistProjectTabsCollapsed(collapsed: boolean) {
  if (typeof window === "undefined") return;
  try {
    const saved = readTaskSidebarViewState();
    window.localStorage.setItem(
      TASK_SIDEBAR_VIEW_STATE_KEY,
      JSON.stringify({
        ...saved,
        projectTabsCollapsed: collapsed,
      }),
    );
  } catch {
    // ignore
  }
}

export function parseTaskDateValue(
  value: string | null | undefined,
): Date | null {
  if (!value) return null;
  const date = parseLocalDateTime(value) ?? new Date(value);
  return isNaN(date.getTime()) ? null : date;
}

export function getTaskDateView(
  task: Task,
): Pick<Task, "start_at" | "end_at" | "all_day"> {
  return {
    start_at: getTaskDisplayStartAt(task),
    end_at: getTaskDisplayEndAt(task),
    all_day: getTaskDisplayAllDay(task),
  };
}

export function getTaskOccurrenceContext(
  task: Task,
): RecurringOccurrenceContext | null {
  if (!task.has_recurrence || !task.effective_occurrence_start_at) return null;
  return {
    occurrence_id: task.effective_occurrence_id ?? null,
    start_at: task.effective_occurrence_start_at,
    end_at: task.effective_occurrence_end_at ?? null,
    original_start_at:
      task.effective_occurrence_original_start_at ??
      task.effective_occurrence_start_at,
    source_kind: task.effective_occurrence_source_kind ?? "task_schedule",
    status: task.effective_occurrence_status ?? task.status ?? null,
  };
}

export function isEditableTarget(element: HTMLElement | null): boolean {
  if (!element) return false;
  return (
    element.tagName === "INPUT" ||
    element.tagName === "TEXTAREA" ||
    element.tagName === "SELECT" ||
    element.isContentEditable
  );
}

/** start_at が明日以降かどうか（今日中は false） */
export function isFutureTask(task: Task): boolean {
  const startAt = getTaskDisplayStartAt(task);
  if (!startAt) return false;
  const d = parseTaskDateValue(startAt);
  if (!d) return false;
  const tomorrow = new Date();
  tomorrow.setHours(0, 0, 0, 0);
  tomorrow.setDate(tomorrow.getDate() + 1);
  // start_at の日付部分が明日以降
  const taskDay = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  return taskDay >= tomorrow;
}

export function isToday(dateStr: string): boolean {
  const d = parseTaskDateValue(dateStr);
  if (!d) return false;
  const now = new Date();
  return (
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate()
  );
}

export function isOverdue(task: Task): boolean {
  const endAt = getTaskDisplayEndAt(task);
  if (!endAt || task.status === "closed") return false;
  const due = parseTaskDateValue(endAt);
  if (!due) return false;
  if (
    getTaskDisplayAllDay(task) ||
    (due.getHours() === 0 && due.getMinutes() === 0)
  ) {
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const dueDay = new Date(due);
    dueDay.setHours(0, 0, 0, 0);
    return dueDay < today;
  }
  return due < new Date();
}

export function formatElapsed(startedAt: string, nowMs: number): string {
  return formatTimerClock(getElapsedTimerSeconds(startedAt, nowMs));
}

export function formatDuration(seconds: number): string {
  if (seconds <= 0) return "";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (h > 0 && m > 0) return `${h}h ${m}m`;
  if (h > 0) return `${h}h`;
  return `${m}m`;
}

/**
 * 日付が「過去」かを判定する。
 * - 終日 or 00:00 の場合は日単位で比較
 * - それ以外は時分単位で比較
 */
export function isDatePast(
  dateStr: string,
  task: Pick<Task, "all_day" | "effective_all_day">,
): boolean {
  const d = parseTaskDateValue(dateStr);
  if (!d) return false;
  const allDay = task.effective_all_day ?? task.all_day;
  if (allDay || (d.getHours() === 0 && d.getMinutes() === 0)) {
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const day = new Date(d);
    day.setHours(0, 0, 0, 0);
    return day < today;
  }
  return d < new Date();
}

/**
 * 日付の色付け。
 * - 'end' (Due Date): 過去=赤、今日=オレンジ、未来=通常
 * - 'start' (Start Date): Due Date を基準に判定する。
 *     - Start が未来 → 通常 (今日ならオレンジ)
 *     - Start が過去 + Due も過去 → 赤
 *     - Start が過去 + Due が未来/未設定 → 作業期間中 (オレンジ)
 */
export function dateColor(
  dateStr: string | null | undefined,
  task: Task,
  kind: "start" | "end" = "end",
): string {
  if (!dateStr || task.status === "closed") return "";
  const yellow =
    "font-semibold border-yellow-300/70 bg-yellow-50/80 text-yellow-800 hover:bg-yellow-100/80 dark:border-yellow-400/40 dark:bg-yellow-500/10 dark:text-yellow-200 dark:hover:bg-yellow-500/15";
  const red = "text-red-500 dark:text-red-400";

  if (kind === "start") {
    const startPast = isDatePast(dateStr, task);
    if (!startPast) {
      return isToday(dateStr) ? yellow : "";
    }
    // Start が過去
    const endAt = getTaskDisplayEndAt(task);
    if (endAt) {
      const endPast = isDatePast(endAt, task);
      return endPast ? red : yellow;
    }
    // Due 未設定: 作業期間中とみなす
    return yellow;
  }

  // kind === "end"
  if (isDatePast(dateStr, task)) return red;
  if (isToday(dateStr)) return yellow;
  return "";
}

export function dateButtonColor(
  dateStr: string | null | undefined,
  task: Task,
  kind: "start" | "end" = "end",
): string {
  return (
    dateColor(dateStr, task, kind) || (dateStr ? "text-muted-foreground" : "")
  );
}

export function hasNonMidnightTime(value: string | null | undefined): boolean {
  if (!value) return false;
  const d = parseTaskDateValue(value);
  if (!d) return false;
  return d.getHours() !== 0 || d.getMinutes() !== 0;
}

export type TaskDateUpdate = {
  start_at?: string | null;
  end_at?: string | null;
  all_day: boolean;
};

export function buildTaskDateUpdate(
  task: Pick<Task, "start_at" | "end_at" | "all_day">,
  changes: { start_at?: string | null; end_at?: string | null },
): TaskDateUpdate {
  const hasStart = Object.prototype.hasOwnProperty.call(changes, "start_at");
  const hasEnd = Object.prototype.hasOwnProperty.call(changes, "end_at");
  const nextStart = hasStart
    ? (changes.start_at ?? null)
    : toLocalDateTimeInputValue(task.start_at, { allDay: task.all_day });
  const nextEnd = hasEnd
    ? (changes.end_at ?? null)
    : toLocalDateTimeInputValue(task.end_at, { allDay: task.all_day });
  const hasAnyDate = !!nextStart || !!nextEnd;

  const updates: TaskDateUpdate = {
    all_day:
      hasAnyDate &&
      !hasNonMidnightTime(nextStart) &&
      !hasNonMidnightTime(nextEnd),
  };

  if (hasStart) {
    updates.start_at = toTaskDatePayloadValue(changes.start_at, {
      allDay: updates.all_day,
    });
  }
  if (hasEnd) {
    updates.end_at = toTaskDatePayloadValue(changes.end_at, {
      allDay: updates.all_day,
    });
  }

  return updates;
}

/**
 * project_id の変更を伴う場合は moveTask、それ以外は updateTask で保存する。
 */
export function saveTaskUpdate(
  taskId: string,
  data: Record<string, unknown>,
  currentProjectId?: string | null,
) {
  const nextProjectId =
    typeof data.project_id === "string" ? data.project_id : null;
  return nextProjectId && nextProjectId !== currentProjectId
    ? taskApi.moveTask(taskId, data)
    : taskApi.updateTask(taskId, data);
}
