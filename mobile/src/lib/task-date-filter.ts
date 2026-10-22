import type { Task } from "../types/api";
import { COMPLETED_TASK_STATUSES } from "./task-visibility";

/**
 * Web の task-filter-builder（開始日/期限の条件）と「期限超過のみ」に相当する
 * Mobile 用の純粋なクライアント側フィルター。
 */
export type TaskDateField = "start_at" | "end_at";
export type TaskDateOp =
  | "on_or_before"
  | "on_or_after"
  | "before"
  | "after"
  | "on"
  | "is_set"
  | "is_not_set";
export type TaskRelativeDate = "yesterday" | "today" | "tomorrow";

export interface TaskDateRule {
  id: string;
  field: TaskDateField;
  op: TaskDateOp;
  /** YYYY-MM-DD または相対指定。is_set / is_not_set では空。 */
  value: string;
}

export interface TaskDateFilter {
  overdueOnly: boolean;
  logic: "and" | "or";
  rules: TaskDateRule[];
}

export const EMPTY_TASK_DATE_FILTER: TaskDateFilter = {
  overdueOnly: false,
  logic: "and",
  rules: [],
};

export const TASK_DATE_FIELD_LABELS: Record<TaskDateField, string> = {
  start_at: "開始日",
  end_at: "期限",
};

export const TASK_DATE_OP_LABELS: Record<TaskDateOp, string> = {
  on_or_before: "以前",
  on_or_after: "以降",
  before: "より前",
  after: "より後",
  on: "当日",
  is_set: "設定あり",
  is_not_set: "未設定",
};

export const TASK_DATE_OPS = Object.keys(TASK_DATE_OP_LABELS) as TaskDateOp[];

export const TASK_RELATIVE_DATE_LABELS: Record<TaskRelativeDate, string> = {
  yesterday: "昨日",
  today: "今日",
  tomorrow: "明日",
};

const ISO_DAY = /^\d{4}-\d{2}-\d{2}$/;

export function taskDateOpNeedsValue(op: TaskDateOp): boolean {
  return op !== "is_set" && op !== "is_not_set";
}

function startOfDay(date: Date): Date {
  const next = new Date(date);
  next.setHours(0, 0, 0, 0);
  return next;
}

/** 相対指定または YYYY-MM-DD を当日0時のローカル日付へ。解釈できなければ null。 */
export function resolveTaskDateValue(value: string, now = new Date()): Date | null {
  const today = startOfDay(now);
  if (value === "today") return today;
  if (value === "yesterday") {
    today.setDate(today.getDate() - 1);
    return today;
  }
  if (value === "tomorrow") {
    today.setDate(today.getDate() + 1);
    return today;
  }
  if (!ISO_DAY.test(value)) return null;
  const [y, m, d] = value.split("-").map(Number);
  const parsed = new Date(y, m - 1, d);
  if (
    parsed.getFullYear() !== y ||
    parsed.getMonth() !== m - 1 ||
    parsed.getDate() !== d
  ) {
    return null;
  }
  return parsed;
}

/** 値の入力が完了していて評価できるルールか。 */
export function isTaskDateRuleComplete(rule: TaskDateRule): boolean {
  return !taskDateOpNeedsValue(rule.op) || resolveTaskDateValue(rule.value) !== null;
}

export function isTaskOverdue(
  task: Pick<Task, "end_at" | "status" | "all_day">,
  now = new Date(),
): boolean {
  if (!task.end_at || COMPLETED_TASK_STATUSES.has(task.status)) return false;
  const due = new Date(task.end_at);
  if (Number.isNaN(due.getTime())) return false;
  if (task.all_day) return startOfDay(due) < startOfDay(now);
  return due < now;
}

function matchesRule(task: Task, rule: TaskDateRule, now: Date): boolean {
  const raw = rule.field === "start_at" ? task.start_at : task.end_at;
  if (rule.op === "is_set") return !!raw;
  if (rule.op === "is_not_set") return !raw;
  if (!raw) return false;
  const ref = resolveTaskDateValue(rule.value, now);
  const date = new Date(raw);
  if (!ref || Number.isNaN(date.getTime())) return false;
  const day = startOfDay(date).getTime();
  const refTime = ref.getTime();
  switch (rule.op) {
    case "on_or_before":
      return day <= refTime;
    case "on_or_after":
      return day >= refTime;
    case "before":
      return day < refTime;
    case "after":
      return day > refTime;
    case "on":
      return day === refTime;
    default:
      return true;
  }
}

/** 有効な条件の件数（ボタンのバッジ用）。入力途中のルールは数えない。 */
export function countActiveTaskDateFilters(filter: TaskDateFilter): number {
  return (
    (filter.overdueOnly ? 1 : 0) +
    filter.rules.filter(isTaskDateRuleComplete).length
  );
}

export function applyTaskDateFilter(
  tasks: Task[],
  filter: TaskDateFilter,
  now = new Date(),
): Task[] {
  let result = tasks;
  if (filter.overdueOnly) {
    result = result.filter((task) => isTaskOverdue(task, now));
  }
  const rules = filter.rules.filter(isTaskDateRuleComplete);
  if (rules.length === 0) return result;
  return result.filter((task) =>
    filter.logic === "and"
      ? rules.every((rule) => matchesRule(task, rule, now))
      : rules.some((rule) => matchesRule(task, rule, now)),
  );
}
