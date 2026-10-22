import type { Task, TaskOccurrence } from "@/lib/task-api";
import {
  getTaskDisplayAllDay,
  getTaskDisplayEndAt,
  getTaskDisplayStartAt,
} from "@/lib/task-effective-date";
import { isClosedTaskStatus } from "@/lib/task-status";

export type TodayEntryKind =
  "scheduled" | "active" | "workflow" | "overdue" | "unscheduled";

export type TodayEntry = {
  key: string;
  task: Task;
  occurrence?: TaskOccurrence;
  kind: TodayEntryKind;
  title: string;
  projectName: string | null;
  status: string;
  priority: string;
  startAt: string | null;
  endAt: string | null;
  allDay: boolean;
  reason: string;
  score: number;
};

export type HomeTodayData = {
  tasks: Task[];
  occurrences: TaskOccurrence[];
};

export type HomeTodayView = {
  entries: TodayEntry[];
  todayEntries: TodayEntry[];
  agentEntries: TodayEntry[];
  openTaskCount: number;
  activeCount: number;
  overdueCount: number;
  readyCount: number;
};

const PRIORITY_SCORES: Record<string, number> = {
  urgent: 40,
  high: 28,
  medium: 14,
  low: 4,
};

const AGENT_CANDIDATE_STATUSES = new Set([
  "pending",
  "needs_user",
  "ready",
  "failed",
]);

function parseDate(value: string | null | undefined): Date | null {
  if (!value) return null;
  const dateOnly = value.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (dateOnly) {
    return new Date(
      Number(dateOnly[1]),
      Number(dateOnly[2]) - 1,
      Number(dateOnly[3]),
    );
  }
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

function startOfDay(now: Date): Date {
  return new Date(now.getFullYear(), now.getMonth(), now.getDate());
}

function endOfDay(now: Date): Date {
  const end = startOfDay(now);
  end.setDate(end.getDate() + 1);
  end.setMilliseconds(-1);
  return end;
}

function isSameLocalDay(value: string | null | undefined, now: Date): boolean {
  const date = parseDate(value);
  if (!date) return false;
  return (
    date.getFullYear() === now.getFullYear() &&
    date.getMonth() === now.getMonth() &&
    date.getDate() === now.getDate()
  );
}

function overlapsDay(
  startValue: string | null | undefined,
  endValue: string | null | undefined,
  now: Date,
): boolean {
  const start = parseDate(startValue);
  const end = parseDate(endValue) ?? start;
  if (!start || !end) return false;
  return start <= endOfDay(now) && end >= startOfDay(now);
}

function isEligibleTask(task: Task): boolean {
  return !(
    task.deleted_at ||
    task.archived_at ||
    task.project_deleted_at ||
    task.project_is_completed ||
    isClosedTaskStatus(task.status)
  );
}

function hasLiveTimer(task: Task): boolean {
  const timer = task.active_time_entry;
  return Boolean(
    timer && timer.task_id === task.id && !timer.ended_at && !timer.deleted_at,
  );
}

function isOverdueTask(task: Task, now: Date): boolean {
  const due = parseDate(getTaskDisplayEndAt(task));
  if (!due) return false;
  if (getTaskDisplayAllDay(task)) return due < startOfDay(now);
  return due < now;
}

function priorityScore(priority: string): number {
  return PRIORITY_SCORES[priority] ?? 8;
}

function taskScore(task: Task, kind: TodayEntryKind): number {
  let score = priorityScore(task.priority);
  if (kind === "overdue") score += 70;
  if (kind === "active") score += 58;
  if (kind === "workflow") score += 50;
  if (kind === "scheduled") score += 34;
  if (hasLiveTimer(task)) score += 12;
  if (task.status === "review") score += 10;
  const triageStatus = task.metadata?.agent_triage_status;
  if (triageStatus === "ready" || triageStatus === "needs_user") score += 8;
  return score;
}

function getTaskKind(task: Task, now: Date): TodayEntryKind | null {
  if (hasLiveTimer(task)) return "active";
  if (isOverdueTask(task, now)) return "overdue";
  const startAt = getTaskDisplayStartAt(task);
  const endAt = getTaskDisplayEndAt(task);
  if (
    overlapsDay(startAt, endAt, now) ||
    isSameLocalDay(startAt, now) ||
    isSameLocalDay(endAt, now)
  ) {
    return "scheduled";
  }
  if (task.status === "in_progress") return "workflow";
  if (task.priority === "urgent" || task.priority === "high") {
    return "unscheduled";
  }
  return null;
}

function reasonForEntry(kind: TodayEntryKind): string {
  switch (kind) {
    case "active":
      return "タイマー計測中";
    case "workflow":
      return "進行中（状態）";
    case "overdue":
      return "期限超過";
    case "scheduled":
      return "今日";
    case "unscheduled":
      return "高優先度";
  }
}

function taskWithOccurrence(task: Task, occurrence: TaskOccurrence): Task {
  return {
    ...task,
    title: occurrence.title || task.title,
    project_name: occurrence.project_name ?? task.project_name,
    project_color: occurrence.project_color ?? task.project_color,
    status: occurrence.status || task.status,
    all_day: occurrence.all_day,
    effective_start_at: occurrence.start_at ?? null,
    effective_end_at: occurrence.end_at ?? null,
    effective_all_day: occurrence.all_day,
    effective_occurrence_id: occurrence.id,
    effective_occurrence_start_at: occurrence.start_at ?? null,
    effective_occurrence_end_at: occurrence.end_at ?? null,
    effective_occurrence_original_start_at:
      occurrence.original_start_at ?? occurrence.start_at ?? null,
    effective_occurrence_source_kind: occurrence.source_kind,
    effective_occurrence_status: occurrence.status,
    tags: occurrence.tags ?? task.tags,
  };
}

function isAgentCandidate(task: Task): boolean {
  const status = task.metadata?.agent_triage_status;
  return typeof status === "string" && AGENT_CANDIDATE_STATUSES.has(status);
}

export function buildTodayEntries(
  data: HomeTodayData,
  now = new Date(),
): TodayEntry[] {
  const eligibleTasks = data.tasks.filter(isEligibleTask);
  const taskById = new Map(eligibleTasks.map((task) => [task.id, task]));
  const occurrenceTaskIds = new Set<string>();
  const entries: TodayEntry[] = [];

  for (const occurrence of data.occurrences) {
    if (occurrence.deleted_at || isClosedTaskStatus(occurrence.status))
      continue;
    const baseTask = taskById.get(occurrence.task_id);
    if (!baseTask || baseTask.parent_task_id) continue;
    occurrenceTaskIds.add(baseTask.id);
    const task = taskWithOccurrence(baseTask, occurrence);
    const kind = hasLiveTimer(task)
      ? "active"
      : task.status === "in_progress"
        ? "workflow"
        : "scheduled";
    entries.push({
      key: `occurrence:${occurrence.id}`,
      task,
      occurrence,
      kind,
      title: occurrence.title || baseTask.title,
      projectName: occurrence.project_name ?? baseTask.project_name ?? null,
      status: occurrence.status,
      priority: baseTask.priority,
      startAt: occurrence.start_at ?? null,
      endAt: occurrence.end_at ?? null,
      allDay: occurrence.all_day,
      reason: reasonForEntry(kind),
      score: taskScore(task, kind),
    });
  }

  for (const task of eligibleTasks) {
    if (task.parent_task_id || occurrenceTaskIds.has(task.id)) continue;
    const kind = getTaskKind(task, now);
    if (!kind) continue;
    const startAt = getTaskDisplayStartAt(task) ?? null;
    const endAt = getTaskDisplayEndAt(task) ?? null;
    entries.push({
      key: `task:${task.id}`,
      task,
      kind,
      title: task.title,
      projectName: task.project_name ?? null,
      status: task.status,
      priority: task.priority,
      startAt,
      endAt,
      allDay: getTaskDisplayAllDay(task),
      reason: reasonForEntry(kind),
      score: taskScore(task, kind),
    });
  }

  return entries.sort((a, b) => {
    if (b.score !== a.score) return b.score - a.score;
    const aTime =
      parseDate(a.startAt ?? a.endAt)?.getTime() ?? Number.MAX_SAFE_INTEGER;
    const bTime =
      parseDate(b.startAt ?? b.endAt)?.getTime() ?? Number.MAX_SAFE_INTEGER;
    return aTime - bTime;
  });
}

export function buildHomeTodayView(
  data: HomeTodayData,
  now = new Date(),
): HomeTodayView {
  const eligibleTasks = data.tasks.filter(isEligibleTask);
  const entries = buildTodayEntries(data, now);
  const todayEntries = entries.filter((entry) => entry.kind !== "unscheduled");
  const seenAgentTaskIds = new Set<string>();
  const agentEntries = entries
    .filter((entry) => {
      if (
        seenAgentTaskIds.has(entry.task.id) ||
        !isAgentCandidate(entry.task)
      ) {
        return false;
      }
      seenAgentTaskIds.add(entry.task.id);
      return true;
    })
    .slice(0, 6);
  const activeTaskIds = new Set(
    eligibleTasks.filter(hasLiveTimer).map((task) => task.id),
  );

  return {
    entries,
    todayEntries,
    agentEntries,
    openTaskCount: eligibleTasks.filter((task) => !task.parent_task_id).length,
    activeCount: activeTaskIds.size,
    overdueCount: entries.filter((entry) => entry.kind === "overdue").length,
    readyCount: eligibleTasks.filter((task) => {
      if (task.parent_task_id) return false;
      const status = task.metadata?.agent_triage_status;
      return status === "ready" || status === "needs_user";
    }).length,
  };
}
