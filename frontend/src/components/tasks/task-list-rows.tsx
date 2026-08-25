"use client";

import { useCallback, useRef, useState } from "react";
import type React from "react";

import { CornerDownRight, Plus, Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { TaskRowDatePicker } from "@/components/tasks/task-row-date-picker";
import {
  TaskStatusMenuItems,
  type TaskStatusOption,
} from "@/components/tasks/task-status-menu-items";
import { taskApi, type Task } from "@/lib/task-api";
import { toLocalDateTimeInputValue } from "@/lib/date-time";
import { formatTaskDateLabel } from "@/lib/task-date-label";
import {
  getTaskDisplayAllDay,
  getTaskDisplayEndAt,
  getTaskDisplayStartAt,
} from "@/lib/task-effective-date";
import { cn } from "@/lib/utils";
import {
  dateButtonColor,
  formatDuration,
  getTaskDisplayStatus,
  STATUS_DOT_COLORS,
  STATUS_LABELS,
} from "@/lib/tasks-page-utils";
import type { DropMode } from "@/lib/task-reorder";
import type { UndoEntry } from "@/components/tasks/hooks/use-task-undo";

/**
 * サブタスク行。
 */
export function SubtaskRow({
  sub,
  parentTask,
  projectTab,
  draggingIds,
  dropTargetId,
  dropMode,
  onDragStart,
  onDragOver,
  onDragLeave,
  onDrop,
  onDragEnd,
  setHoveredGroupId,
  openTask,
  onContextMenu,
  pushUndo,
  handleTaskDateChange,
  applyTaskPatchLocally,
  removeTaskLocally,
  requestRecurringDelete,
  readOnly = false,
  showProjectColumn = projectTab === "all",
  showStartColumn = true,
  showDueColumn = true,
  showPriorityColumn = false,
  showAssigneeColumn = false,
  showTimeColumn = true,
  onStatusChange,
  rowRef,
  tabIndex = -1,
  onFocus,
  focusRow,
  focused = false,
}: {
  sub: Task;
  parentTask: Task;
  projectTab: string;
  draggingIds: string[];
  dropTargetId: string | null;
  dropMode: DropMode;
  onDragStart: (e: React.DragEvent, taskId: string) => void;
  onDragOver: (e: React.DragEvent, taskId: string) => void;
  onDragLeave: (e: React.DragEvent) => void;
  onDrop: (e: React.DragEvent, taskId: string) => void;
  onDragEnd: () => void;
  setHoveredGroupId: (taskId: string | null) => void;
  openTask: (task: Task) => void;
  onContextMenu: (e: React.MouseEvent, task: Task) => void;
  pushUndo: (entry: UndoEntry) => void;
  handleTaskDateChange: (
    task: Task,
    changes: { start_at?: string | null; end_at?: string | null },
  ) => Promise<void>;
  applyTaskPatchLocally: (taskId: string, patch: Partial<Task>) => void;
  removeTaskLocally: (taskId: string) => void;
  requestRecurringDelete?: (task: Task) => boolean;
  readOnly?: boolean;
  showProjectColumn?: boolean;
  showStartColumn?: boolean;
  showDueColumn?: boolean;
  showPriorityColumn?: boolean;
  showAssigneeColumn?: boolean;
  showTimeColumn?: boolean;
  onStatusChange?: (task: Task, status: TaskStatusOption) => Promise<void>;
  rowRef?: React.Ref<HTMLTableRowElement>;
  tabIndex?: number;
  onFocus?: () => void;
  focusRow?: () => void;
  focused?: boolean;
}) {
  const status = getTaskDisplayStatus(sub);

  return (
    <tr
      key={sub.id}
      ref={rowRef}
      data-testid={`task-row-${sub.id}`}
      tabIndex={tabIndex}
      draggable={!readOnly}
      onDragStart={(e) => onDragStart(e, sub.id)}
      onDragOver={(e) => onDragOver(e, sub.id)}
      onDragLeave={onDragLeave}
      onDrop={(e) => onDrop(e, sub.id)}
      onDragEnd={onDragEnd}
      onMouseEnter={() => setHoveredGroupId(parentTask.id)}
      onFocus={onFocus}
      onClick={() => {
        focusRow?.();
        openTask(sub);
      }}
      onContextMenu={(e) => {
        if (!readOnly) onContextMenu(e, sub);
      }}
      className={cn(
        "group relative h-11 cursor-pointer border-b border-border/40 bg-muted/10 transition-colors hover:bg-card/60",
        draggingIds.includes(sub.id) && "opacity-40",
        focused &&
          "is-focused bg-primary/10 outline outline-1 -outline-offset-1 outline-primary/60",
      )}
    >
      <td className="h-11 py-0 pl-6">
        {dropTargetId === sub.id && !draggingIds.includes(sub.id) && (
          <div
            className={cn(
              "pointer-events-none absolute inset-x-0 z-20 h-[3px] rounded-full bg-blue-500 shadow-[0_0_6px_rgba(59,130,246,0.6)]",
              (dropMode === "reorder-before" || dropMode === "subtask-before") &&
                "-top-[2px]",
              (dropMode === "reorder-after" || dropMode === "subtask-after") &&
                "-bottom-[2px]",
            )}
          />
        )}
      </td>
      <td className="h-11 py-0 text-center">
        <CornerDownRight className="mx-auto size-3 text-muted-foreground" />
      </td>
      <td className="h-11 py-0 text-center" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-center gap-1">
          {readOnly || !onStatusChange ? (
            <span
              className={cn(
                "size-4 shrink-0 rounded-full border-2",
                STATUS_DOT_COLORS[status] || STATUS_DOT_COLORS.open,
              )}
              title={STATUS_LABELS[status]}
            />
          ) : (
            <DropdownMenu>
              <DropdownMenuTrigger
                className={cn(
                  "size-4 shrink-0 rounded-full border-2 transition-colors hover:ring-2 hover:ring-primary/30",
                  STATUS_DOT_COLORS[status] || STATUS_DOT_COLORS.open,
                )}
                title={STATUS_LABELS[status]}
                aria-label={`${sub.title}のステータスを変更`}
              />
              <DropdownMenuContent align="start" className="min-w-36">
                <TaskStatusMenuItems
                  currentStatus={status}
                  onSelect={(nextStatus, event) => {
                    event.stopPropagation();
                    void onStatusChange(sub, nextStatus);
                  }}
                />
              </DropdownMenuContent>
            </DropdownMenu>
          )}
        </div>
      </td>
      <td className="h-11 overflow-hidden py-0 pl-0 pr-4">
        <div className="flex min-w-0 items-center gap-2 pl-4">
          <span
            className={cn(
              "truncate text-sm font-medium leading-5",
              status === "closed" && "text-muted-foreground line-through",
            )}
            title={sub.title}
          >
            {sub.title}
          </span>
          {sub.has_recurrence && (
            <span
              className="text-[10px] text-muted-foreground"
              aria-label="繰り返しタスク"
              title="繰り返しタスク"
            >
              ↻
            </span>
          )}
          {(sub.tags?.length ?? 0) > 0 && (
            <span className="flex min-w-0 max-w-[38%] items-center gap-1 overflow-hidden">
              {sub.tags.slice(0, 3).map((tag) => (
                <span
                  key={tag.id}
                  className="max-w-24 truncate rounded px-1.5 py-0.5 text-[10px] font-medium text-white"
                  style={{ backgroundColor: tag.color || "#6B7280" }}
                >
                  {tag.name}
                </span>
              ))}
              {sub.tags.length > 3 && (
                <span className="shrink-0 text-[10px] text-muted-foreground">
                  +{sub.tags.length - 3}
                </span>
              )}
            </span>
          )}
          {!readOnly && (
            <Button
              variant="ghost"
              size="icon-xs"
              onClick={(event) => {
                event.stopPropagation();
                if (requestRecurringDelete?.(sub)) return;
                pushUndo({
                  type: "recreate",
                  tasks: [sub],
                });
                void taskApi.deleteTask(sub.id).then(() => {
                  removeTaskLocally(sub.id);
                });
              }}
              className="ml-auto shrink-0 text-muted-foreground opacity-0 transition-opacity hover:text-red-500 group-hover:opacity-100"
              aria-label="サブタスクを削除"
            >
              <Trash2 className="size-3" />
            </Button>
          )}
        </div>
      </td>
      {showProjectColumn && (
        <td className="h-11 truncate px-2 py-0 text-[13px] leading-5 text-muted-foreground">
          {sub.project_name || "-"}
        </td>
      )}
      {showStartColumn && (
        <td
          className="h-11 whitespace-nowrap px-2 py-1 text-[13px] leading-5"
          onClick={(e) => e.stopPropagation()}
        >
          {readOnly ? (
            <span
              className="block min-w-0 flex-1 whitespace-nowrap text-[13px] leading-5 text-muted-foreground"
              title={
                formatTaskDateLabel(getTaskDisplayStartAt(sub), {
                  allDay: getTaskDisplayAllDay(sub),
                }) || "-"
              }
            >
              {formatTaskDateLabel(getTaskDisplayStartAt(sub), {
                allDay: getTaskDisplayAllDay(sub),
              }) || "-"}
            </span>
          ) : (
            <div className="min-w-0 flex-1">
              <TaskRowDatePicker
                taskId={sub.id}
                startAt={toLocalDateTimeInputValue(getTaskDisplayStartAt(sub), {
                  allDay: getTaskDisplayAllDay(sub),
                })}
                endAt={toLocalDateTimeInputValue(getTaskDisplayEndAt(sub), {
                  allDay: getTaskDisplayAllDay(sub),
                })}
                onRangeChange={({ startAt, endAt }) =>
                  handleTaskDateChange(sub, {
                    start_at: startAt,
                    end_at: endAt,
                  })
                }
                onRecurrenceChange={(hasRecurrence) =>
                  applyTaskPatchLocally(sub.id, { has_recurrence: hasRecurrence })
                }
                allDay={getTaskDisplayAllDay(sub)}
                startPlaceholder="Start Date"
                endPlaceholder="Due Date"
                startButtonClassName={cn(
                  "ao-task-date-button h-8 w-full min-w-0 px-1.5 text-[13px] leading-5",
                  dateButtonColor(getTaskDisplayStartAt(sub), sub, "start"),
                )}
                endButtonClassName={cn(
                  "ao-task-date-button h-8 w-full min-w-0 px-1.5 text-[13px] leading-5",
                  dateButtonColor(getTaskDisplayEndAt(sub), sub, "end"),
                )}
                showStartDate
                showEndDate={false}
              />
            </div>
          )}
        </td>
      )}
      {showDueColumn && (
        <td
          className="h-11 whitespace-nowrap px-2 py-1 text-[13px] leading-5"
          onClick={(e) => e.stopPropagation()}
        >
          <div className="flex min-w-0 items-center gap-1">
            {sub.has_recurrence && !showStartColumn && (
              <span
                className="text-[10px] text-muted-foreground"
                aria-label="繰り返しタスク"
                title="繰り返しタスク"
              >
                ↻
              </span>
            )}
            {readOnly ? (
              <span
                className="block min-w-0 flex-1 whitespace-nowrap text-[13px] leading-5 text-muted-foreground"
                title={
                  formatTaskDateLabel(getTaskDisplayEndAt(sub), {
                    allDay: getTaskDisplayAllDay(sub),
                  }) || "-"
                }
              >
                {formatTaskDateLabel(getTaskDisplayEndAt(sub), {
                  allDay: getTaskDisplayAllDay(sub),
                }) || "-"}
              </span>
            ) : (
              <div className="min-w-0 flex-1">
                <TaskRowDatePicker
                  taskId={sub.id}
                  startAt={toLocalDateTimeInputValue(getTaskDisplayStartAt(sub), {
                    allDay: getTaskDisplayAllDay(sub),
                  })}
                  endAt={toLocalDateTimeInputValue(getTaskDisplayEndAt(sub), {
                    allDay: getTaskDisplayAllDay(sub),
                  })}
                  onRangeChange={({ startAt, endAt }) =>
                    handleTaskDateChange(sub, {
                      start_at: startAt,
                      end_at: endAt,
                    })
                  }
                  onRecurrenceChange={(hasRecurrence) =>
                    applyTaskPatchLocally(sub.id, { has_recurrence: hasRecurrence })
                  }
                  allDay={getTaskDisplayAllDay(sub)}
                  startPlaceholder="Start Date"
                  endPlaceholder="Due Date"
                  startButtonClassName={cn(
                    "ao-task-date-button h-8 w-full min-w-0 px-1.5 text-[13px] leading-5",
                    dateButtonColor(getTaskDisplayStartAt(sub), sub, "start"),
                  )}
                  endButtonClassName={cn(
                    "ao-task-date-button h-8 w-full min-w-0 px-1.5 text-[13px] leading-5",
                    dateButtonColor(getTaskDisplayEndAt(sub), sub, "end"),
                  )}
                  showStartDate={false}
                  showEndDate
                />
              </div>
            )}
          </div>
        </td>
      )}
      {showPriorityColumn && (
        <td className="h-11 truncate px-2 py-0 text-[13px] leading-5 text-muted-foreground">
          {sub.priority !== "none" ? sub.priority : "-"}
        </td>
      )}
      {showAssigneeColumn && (
        <td className="h-11 truncate px-2 py-0 text-[13px] leading-5 text-muted-foreground">
          {sub.assignees?.length
            ? sub.assignees
                .map((assignee) => assignee.display_name || assignee.username || assignee.user_id)
                .join(", ")
            : "-"}
        </td>
      )}
      {showTimeColumn && (
        <td className="h-11 px-2 py-0 text-right font-mono text-[13px] leading-5 text-muted-foreground">
          {(sub.total_time_seconds ?? 0) > 0 ? formatDuration(sub.total_time_seconds ?? 0) : "--:--:--"}
        </td>
      )}
    </tr>
  );
}

export function SubtaskAddRow({
  task,
  colSpan,
  setHoveredGroupId,
  subtaskAddParentId,
  setSubtaskAddParentId,
  subtaskAddTitle,
  setSubtaskAddTitle,
  subtaskAddCreating,
  subtaskAddRef,
  onSubmit,
}: {
  task: Task;
  colSpan: number;
  setHoveredGroupId: (taskId: string | null) => void;
  subtaskAddParentId: string | null;
  setSubtaskAddParentId: (taskId: string | null) => void;
  subtaskAddTitle: string;
  setSubtaskAddTitle: (title: string) => void;
  subtaskAddCreating: boolean;
  subtaskAddRef: React.RefObject<HTMLInputElement | null>;
  onSubmit: (parentTask: Task) => void;
}) {
  return (
    <tr
      className="border-b border-border/20 bg-muted/10"
      onMouseEnter={() => setHoveredGroupId(task.id)}
    >
      <td colSpan={colSpan} className="py-1 px-2">
        {subtaskAddParentId === task.id ? (
          <div className="flex items-center gap-2 pl-8">
            <CornerDownRight className="size-3 text-muted-foreground shrink-0" />
            <input
              ref={subtaskAddRef}
              type="text"
              value={subtaskAddTitle}
              onChange={(e) => setSubtaskAddTitle(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") onSubmit(task);
                if (e.key === "Escape") {
                  setSubtaskAddParentId(null);
                  setSubtaskAddTitle("");
                }
              }}
              onBlur={() => {
                if (!subtaskAddTitle.trim()) {
                  setSubtaskAddParentId(null);
                }
              }}
              placeholder="サブタスク名を入力... (Enterで作成)"
              disabled={subtaskAddCreating}
              className="flex-1 bg-transparent text-sm outline-none placeholder:text-muted-foreground"
              autoFocus
            />
          </div>
        ) : (
          <button
            onClick={(e) => {
              e.stopPropagation();
              setSubtaskAddParentId(task.id);
              setSubtaskAddTitle("");
              setTimeout(() => subtaskAddRef.current?.focus(), 50);
            }}
            className="flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground transition-colors pl-8"
          >
            <Plus className="size-3" />
            サブタスクを追加
          </button>
        )}
      </td>
    </tr>
  );
}

/**
 * インラインQuickAdd行。
 */
export function QuickAddRow({
  colSpan,
  projectTab,
  selectedProjectId,
  upsertTaskLocally,
}: {
  colSpan: number;
  projectTab: string;
  selectedProjectId: string | null;
  upsertTaskLocally: (task: Task) => void;
}) {
  const [quickAddActive, setQuickAddActive] = useState(false);
  const [quickAddTitle, setQuickAddTitle] = useState("");
  const [quickAddCreating, setQuickAddCreating] = useState(false);
  const quickAddRef = useRef<HTMLInputElement>(null);

  const handleQuickAdd = useCallback(async () => {
    if (!quickAddTitle.trim() || !selectedProjectId) return;
    setQuickAddCreating(true);
    try {
      const created = await taskApi.createTask({
        project_id: projectTab !== "all" ? projectTab : selectedProjectId,
        title: quickAddTitle.trim(),
      });
      upsertTaskLocally(created);
      setQuickAddTitle("");
    } catch (err) {
      console.error("タスク作成失敗:", err);
    } finally {
      setQuickAddCreating(false);
    }
  }, [
    projectTab,
    quickAddTitle,
    selectedProjectId,
    upsertTaskLocally,
  ]);

  return (
    <tr className="border-b border-border/30">
      <td colSpan={colSpan} className="py-1 px-2">
        {quickAddActive ? (
          <div className="flex items-center gap-2">
            <input
              ref={quickAddRef}
              type="text"
              value={quickAddTitle}
              onChange={(e) => setQuickAddTitle(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") handleQuickAdd();
                if (e.key === "Escape") {
                  setQuickAddActive(false);
                  setQuickAddTitle("");
                }
              }}
              onBlur={() => {
                if (!quickAddTitle.trim()) {
                  setQuickAddActive(false);
                }
              }}
              placeholder="タスク名を入力... (Enterで作成)"
              disabled={quickAddCreating}
              className="flex-1 bg-transparent text-sm outline-none placeholder:text-muted-foreground"
              autoFocus
            />
            <Button
              size="sm"
              variant="ghost"
              onClick={handleQuickAdd}
              disabled={quickAddCreating || !quickAddTitle.trim()}
              className="h-7 text-xs"
            >
              追加
            </Button>
          </div>
        ) : (
          <button
            onClick={() => {
              setQuickAddActive(true);
              setTimeout(() => quickAddRef.current?.focus(), 50);
            }}
            className="flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground transition-colors w-full py-1"
          >
            <Plus className="size-3.5" />
            タスクを追加
          </button>
        )}
      </td>
    </tr>
  );
}
