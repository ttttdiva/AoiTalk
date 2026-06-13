"use client";

import { useMemo } from "react";
import { Plus, X, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { formatLocalDate } from "@/lib/date-time";
import {
  getTaskDisplayEndAt,
  getTaskDisplayStartAt,
} from "@/lib/task-effective-date";
import type { Task, Tag, Project } from "@/lib/task-api";

export type FilterField =
  | "status"
  | "priority"
  | "tag"
  | "project"
  | "start_at"
  | "end_at"
  | "title"
  | "assignee";

export type FilterOperator =
  | "is"
  | "is_not"
  | "is_set"
  | "is_not_set"
  | "contains"
  | "not_contains"
  | "before"
  | "after"
  | "on";

export interface FilterRule {
  id: string;
  field: FilterField;
  op: FilterOperator;
  value: string;
}

export interface FilterConfig {
  logic: "and" | "or";
  rules: FilterRule[];
}

export const EMPTY_FILTER: FilterConfig = { logic: "and", rules: [] };

const FIELD_LABELS: Record<FilterField, string> = {
  status: "Status",
  priority: "Priority",
  tag: "Tag",
  project: "Project",
  start_at: "Start Date",
  end_at: "Due Date",
  title: "Title",
  assignee: "Assignee",
};

const STATUS_OPTIONS = [
  { v: "open", l: "未着手" },
  { v: "in_progress", l: "進行中" },
  { v: "on_hold", l: "保留" },
  { v: "review", l: "確認待ち" },
  { v: "closed", l: "完了" },
];

const PRIORITY_OPTIONS = [
  { v: "urgent", l: "Urgent" },
  { v: "high", l: "High" },
  { v: "medium", l: "Medium" },
  { v: "low", l: "Low" },
  { v: "none", l: "None" },
];

const FIELD_OPERATORS: Record<FilterField, FilterOperator[]> = {
  status: ["is", "is_not"],
  priority: ["is", "is_not"],
  tag: ["is", "is_not", "is_set", "is_not_set"],
  project: ["is", "is_not"],
  start_at: ["is_set", "is_not_set", "is", "before", "after", "on"],
  end_at: ["is_set", "is_not_set", "is", "before", "after", "on"],
  title: ["contains", "not_contains", "is_set", "is_not_set"],
  assignee: ["is", "is_not", "is_set", "is_not_set"],
};

const OPERATOR_LABELS: Record<FilterOperator, string> = {
  is: "is",
  is_not: "is not",
  is_set: "is set",
  is_not_set: "is not set",
  contains: "contains",
  not_contains: "not contains",
  before: "before",
  after: "after",
  on: "on",
};

function randomId(): string {
  return Math.random().toString(36).slice(2, 10);
}

function needsValueInput(op: FilterOperator): boolean {
  return op !== "is_set" && op !== "is_not_set";
}

function startOfDay(d: Date) {
  const x = new Date(d);
  x.setHours(0, 0, 0, 0);
  return x;
}

const DATE_RELATIVE_OPTIONS = [
  { value: "today", label: "Today" },
  { value: "yesterday", label: "Yesterday" },
  { value: "tomorrow", label: "Tomorrow" },
  { value: "today_or_earlier", label: "Today & Earlier" },
  { value: "today_or_later", label: "Today & Later" },
] as const;

type RelativeDateValue = (typeof DATE_RELATIVE_OPTIONS)[number]["value"];

function isRelativeDateValue(value: string): value is RelativeDateValue {
  return DATE_RELATIVE_OPTIONS.some((option) => option.value === value);
}

function matchesRelativeDate(raw: string, value: RelativeDateValue): boolean {
  const date = startOfDay(new Date(raw));
  const today = startOfDay(new Date());
  const diffDays = Math.round(
    (date.getTime() - today.getTime()) / (1000 * 60 * 60 * 24),
  );
  switch (value) {
    case "today":
      return diffDays === 0;
    case "yesterday":
      return diffDays === -1;
    case "tomorrow":
      return diffDays === 1;
    case "today_or_earlier":
      return diffDays <= 0;
    case "today_or_later":
      return diffDays >= 0;
  }
}

/** 1件のタスクに対してフィルタを評価 */
function evaluateRule(
  task: Task,
  rule: FilterRule,
  projectMap: Map<string, string>,
): boolean {
  const val = rule.value;
  switch (rule.field) {
    case "status": {
      if (rule.op === "is") return task.status === val;
      if (rule.op === "is_not") return task.status !== val;
      return true;
    }
    case "priority": {
      if (rule.op === "is") return task.priority === val;
      if (rule.op === "is_not") return task.priority !== val;
      return true;
    }
    case "tag": {
      const names = (task.tags || []).map((t) => t.name.toLowerCase());
      if (rule.op === "is_set") return names.length > 0;
      if (rule.op === "is_not_set") return names.length === 0;
      if (rule.op === "is") return names.includes(val.toLowerCase());
      if (rule.op === "is_not") return !names.includes(val.toLowerCase());
      return true;
    }
    case "project": {
      const name = projectMap.get(task.project_id) || "";
      if (rule.op === "is")
        return (
          name.toLowerCase() === val.toLowerCase() || task.project_id === val
        );
      if (rule.op === "is_not")
        return (
          name.toLowerCase() !== val.toLowerCase() && task.project_id !== val
        );
      return true;
    }
    case "start_at":
    case "end_at": {
      const raw =
        rule.field === "start_at"
          ? getTaskDisplayStartAt(task)
          : getTaskDisplayEndAt(task);
      if (rule.op === "is_set") return !!raw;
      if (rule.op === "is_not_set") return !raw;
      if (!raw || !val) return false;
      if (rule.op === "is" && isRelativeDateValue(val)) {
        return matchesRelativeDate(raw, val);
      }
      const d = startOfDay(new Date(raw));
      const ref = startOfDay(new Date(val));
      if (rule.op === "is") return d.getTime() === ref.getTime();
      if (rule.op === "before") return d < ref;
      if (rule.op === "after") return d > ref;
      if (rule.op === "on") return d.getTime() === ref.getTime();
      return true;
    }
    case "title": {
      const title = (task.title || "").toLowerCase();
      if (rule.op === "is_set") return title.length > 0;
      if (rule.op === "is_not_set") return title.length === 0;
      if (rule.op === "contains") return title.includes(val.toLowerCase());
      if (rule.op === "not_contains") return !title.includes(val.toLowerCase());
      return true;
    }
    case "assignee": {
      const names = (task.assignees || []).map((a) =>
        (a.display_name || a.username || a.user_id || "").toLowerCase(),
      );
      if (rule.op === "is_set") return names.length > 0;
      if (rule.op === "is_not_set") return names.length === 0;
      if (rule.op === "is") return names.includes(val.toLowerCase());
      if (rule.op === "is_not") return !names.includes(val.toLowerCase());
      return true;
    }
  }
  return true;
}

/** タスク配列に対してフィルタを適用 */
export function applyTaskFilter(
  tasks: Task[],
  config: FilterConfig,
  projectMap: Map<string, string>,
): Task[] {
  if (config.rules.length === 0) return tasks;
  return tasks.filter((t) => {
    if (config.logic === "and") {
      return config.rules.every((r) => evaluateRule(t, r, projectMap));
    }
    return config.rules.some((r) => evaluateRule(t, r, projectMap));
  });
}

interface TaskFilterBuilderProps {
  config: FilterConfig;
  onChange: (next: FilterConfig) => void;
  tags: Tag[];
  projects: Project[];
  onClose: () => void;
}

export function TaskFilterBuilder({
  config,
  onChange,
  tags,
  projects,
  onClose,
}: TaskFilterBuilderProps) {
  const addRule = () => {
    const rule: FilterRule = {
      id: randomId(),
      field: "status",
      op: "is",
      value: "open",
    };
    onChange({ ...config, rules: [...config.rules, rule] });
  };

  const updateRule = (id: string, patch: Partial<FilterRule>) => {
    onChange({
      ...config,
      rules: config.rules.map((r) => {
        if (r.id !== id) return r;
        const next = { ...r, ...patch };
        // field が変わったら op と value をリセット
        if (patch.field && patch.field !== r.field) {
          const ops = FIELD_OPERATORS[patch.field];
          next.op = ops[0];
          next.value = defaultValueFor(patch.field);
        }
        // op が is_set / is_not_set になったら value を空に
        if (patch.op && !needsValueInput(patch.op)) {
          next.value = "";
        }
        return next;
      }),
    });
  };

  const removeRule = (id: string) => {
    onChange({
      ...config,
      rules: config.rules.filter((r) => r.id !== id),
    });
  };

  const clearAll = () => {
    onChange(EMPTY_FILTER);
  };

  function defaultValueFor(field: FilterField): string {
    switch (field) {
      case "status":
        return "open";
      case "priority":
        return "medium";
      case "tag":
        return tags[0]?.name || "";
      case "project":
        return projects[0]?.name || "";
      case "start_at":
      case "end_at":
        return "today";
      case "title":
      case "assignee":
      default:
        return "";
    }
  }

  const tagNames = useMemo(() => tags.map((t) => t.name), [tags]);
  const projectNames = useMemo(() => projects.map((p) => p.name), [projects]);

  return (
    <div className="flex w-[520px] max-w-[90vw] flex-col gap-3 p-1">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium">フィルター条件</span>
          {config.rules.length > 1 && (
            <Select
              value={config.logic}
              onValueChange={(v: string | null) =>
                v && onChange({ ...config, logic: v as "and" | "or" })
              }
            >
              <SelectTrigger className="h-7 w-[72px] text-xs">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="and">AND</SelectItem>
                <SelectItem value="or">OR</SelectItem>
              </SelectContent>
            </Select>
          )}
        </div>
        <div className="flex items-center gap-1">
          {config.rules.length > 0 && (
            <Button
              variant="ghost"
              size="sm"
              className="h-7 px-2 text-xs"
              onClick={clearAll}
            >
              <Trash2 className="size-3" />
              全クリア
            </Button>
          )}
          <Button
            variant="ghost"
            size="icon"
            className="size-7"
            onClick={onClose}
          >
            <X className="size-3.5" />
          </Button>
        </div>
      </div>

      {config.rules.length === 0 ? (
        <div className="rounded-md border border-dashed p-4 text-center text-xs text-muted-foreground">
          条件がありません。「条件を追加」で絞り込みを始めてください。
        </div>
      ) : (
        <div className="flex flex-col gap-1.5">
          {config.rules.map((rule, idx) => (
            <div
              key={rule.id}
              className="flex items-center gap-1.5 rounded-md border bg-muted/30 px-2 py-1.5"
            >
              <span className="w-10 shrink-0 text-xs text-muted-foreground">
                {idx === 0 ? "Where" : config.logic.toUpperCase()}
              </span>
              <Select
                value={rule.field}
                onValueChange={(v: string | null) =>
                  v && updateRule(rule.id, { field: v as FilterField })
                }
              >
                <SelectTrigger className="h-7 w-[110px] text-xs">
                  <SelectValue>{FIELD_LABELS[rule.field]}</SelectValue>
                </SelectTrigger>
                <SelectContent>
                  {(Object.keys(FIELD_LABELS) as FilterField[]).map((f) => (
                    <SelectItem key={f} value={f}>
                      {FIELD_LABELS[f]}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <Select
                value={rule.op}
                onValueChange={(v: string | null) =>
                  v && updateRule(rule.id, { op: v as FilterOperator })
                }
              >
                <SelectTrigger className="h-7 w-[110px] text-xs">
                  <SelectValue>{OPERATOR_LABELS[rule.op]}</SelectValue>
                </SelectTrigger>
                <SelectContent>
                  {FIELD_OPERATORS[rule.field].map((o) => (
                    <SelectItem key={o} value={o}>
                      {OPERATOR_LABELS[o]}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              {needsValueInput(rule.op) && (
                <ValueInput
                  field={rule.field}
                  op={rule.op}
                  value={rule.value}
                  onChange={(v) => updateRule(rule.id, { value: v })}
                  tagNames={tagNames}
                  projectNames={projectNames}
                />
              )}
              <Button
                variant="ghost"
                size="icon"
                className="ml-auto size-6"
                onClick={() => removeRule(rule.id)}
                title="条件を削除"
              >
                <X className="size-3" />
              </Button>
            </div>
          ))}
        </div>
      )}

      <Button
        variant="outline"
        size="sm"
        onClick={addRule}
        className="h-7 justify-start text-xs"
      >
        <Plus className="size-3" />
        条件を追加
      </Button>
    </div>
  );
}

function ValueInput({
  field,
  op,
  value,
  onChange,
  tagNames,
  projectNames,
}: {
  field: FilterField;
  op: FilterOperator;
  value: string;
  onChange: (v: string) => void;
  tagNames: string[];
  projectNames: string[];
}) {
  if (field === "status") {
    return (
      <Select
        value={value}
        onValueChange={(v: string | null) => v && onChange(v)}
      >
        <SelectTrigger className="h-7 flex-1 text-xs">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {STATUS_OPTIONS.map((o) => (
            <SelectItem key={o.v} value={o.v}>
              {o.l}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    );
  }
  if (field === "priority") {
    return (
      <Select
        value={value}
        onValueChange={(v: string | null) => v && onChange(v)}
      >
        <SelectTrigger className="h-7 flex-1 text-xs">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {PRIORITY_OPTIONS.map((o) => (
            <SelectItem key={o.v} value={o.v}>
              {o.l}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    );
  }
  if (field === "tag" && tagNames.length > 0) {
    return (
      <Select
        value={value}
        onValueChange={(v: string | null) => v && onChange(v)}
      >
        <SelectTrigger className="h-7 flex-1 text-xs">
          <SelectValue placeholder="選択" />
        </SelectTrigger>
        <SelectContent>
          {tagNames.map((n) => (
            <SelectItem key={n} value={n}>
              {n}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    );
  }
  if (field === "project" && projectNames.length > 0) {
    return (
      <Select
        value={value}
        onValueChange={(v: string | null) => v && onChange(v)}
      >
        <SelectTrigger className="h-7 flex-1 text-xs">
          <SelectValue placeholder="選択" />
        </SelectTrigger>
        <SelectContent>
          {projectNames.map((n) => (
            <SelectItem key={n} value={n}>
              {n}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    );
  }
  if (field === "start_at" || field === "end_at") {
    if (op === "is") {
      return (
        <Select
          value={isRelativeDateValue(value) ? value : "today"}
          onValueChange={(v: string | null) => v && onChange(v)}
        >
          <SelectTrigger className="h-7 flex-1 text-xs">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {DATE_RELATIVE_OPTIONS.map((option) => (
              <SelectItem key={option.value} value={option.value}>
                {option.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      );
    }
    return (
      <Input
        type="date"
        value={
          isRelativeDateValue(value)
            ? formatLocalDate(new Date())
            : value
        }
        onChange={(e) => onChange(e.target.value)}
        className="h-7 flex-1 text-xs"
      />
    );
  }
  return (
    <Input
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder="値"
      className="h-7 flex-1 text-xs"
    />
  );
}
