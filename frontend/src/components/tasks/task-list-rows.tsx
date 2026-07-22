"use client";

import { useCallback, useRef, useState } from "react";
import type React from "react";

import { CornerDownRight, Plus, Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { TaskRowDatePicker } from "@/components/tasks/task-row-date-picker";
import { taskApi, type Task } from "@/lib/task-api";
import { toLocalDateTimeInputValue } from "@/lib/date-time";
import {
  getTaskDisplayAllDay,
  getTaskDisplayEndAt,
  getTaskDisplayStartAt,
} from "@/lib/task-effective-date";
import { cn } from "@/lib/utils";
import {
  dateButtonColor,
  STATUS_DOT_COLORS,
  STATUS_LABELS,
} from "@/lib/tasks-page-utils";
import type { DropMode } from "@/lib/task-reorder";
import type { FetchDataOptions } from "@/components/tasks/hooks/use-tasks-data";
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
  fetchData,
  handleTaskDateChange,
  applyTaskPatchLocally,
  requestRecurringDelete,
  readOnly = false,
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
  fetchData: (options?: FetchDataOptions) => Promise<void>;
  handleTaskDateChange: (
    task: Task,
    changes: { start_at?: string | null; end_at?: string | null },
  ) => Promise<void>;
  applyTaskPatchLocally: (taskId: string, patch: Partial<Task>) => void;
  requestRecurringDelete?: (task: Task) => boolean;
  readOnly?: boolean;
}) {
  return (
    <tr
      key={sub.id}
      data-testid={`task-row-${sub.id}`}
      draggable={!readOnly}
      onDragStart={(e) => onDragStart(e, sub.id)}
      onDragOver={(e) => onDragOver(e, sub.id)}
      onDragLeave={onDragLeave}
      onDrop={(e) => onDrop(e, sub.id)}
      onDragEnd={onDragEnd}
      onMouseEnter={() => setHoveredGroupId(parentTask.id)}
      onClick={() => {
        openTask(sub);
      }}
      onContextMenu={(e) => {
        if (!readOnly) onContextMenu(e, sub);
      }}
      className={cn(
        "group relative border-b border-border/30 cursor-pointer transition-colors hover:bg-accent/60 hover:shadow-sm bg-muted/10",
        draggingIds.includes(sub.id) && "opacity-40",
      )}
    >
      <td className="py-1.5 pl-2 w-8">
        {dropTargetId === sub.id && !draggingIds.includes(sub.id) && (
          <div
            className={cn(
              "pointer-events-none absolute inset-x-0 z-20 h-[3px] rounded-full bg-blue-500 shadow-[0_0_6px_rgba(59,130,246,0.6)]",
              (dropMode === "reorder-before" ||
                dropMode === "subtask-before") &&
                "-top-[2px]",
              (dropMode === "reorder-after" || dropMode === "subtask-after") &&
                "-bottom-[2px]",
            )}
          />
        )}
      </td>
      <td className="py-1.5 pl-0">
        <div className="flex items-center gap-0.5 pl-4">
          <CornerDownRight className="size-3 text-muted-foreground shrink-0" />
          <Checkbox
            disabled={readOnly}
            checked={sub.status === "closed"}
            onCheckedChange={async (checked) => {
              try {
                pushUndo({
                  type: "update",
                  taskId: sub.id,
                  previous: { status: sub.status },
                });
                await taskApi.updateTask(sub.id, {
                  status: checked ? "closed" : "open",
                });
                await fetchData();
              } catch (err) {
                console.error("ステータス更新失敗:", err);
              }
            }}
            onClick={(e) => e.stopPropagation()}
            className={cn(
              "size-3.5",
              sub.status === "in_progress" && STATUS_DOT_COLORS.in_progress,
            )}
            title={STATUS_LABELS[sub.status]}
          />
        </div>
      </td>
      <td className="py-1.5 pl-2" colSpan={projectTab === "all" ? 2 : 1}>
        <span
          className={cn(
            "text-sm",
            sub.status === "closed" && "line-through text-muted-foreground",
          )}
        >
          {sub.title}
        </span>
      </td>
      {/* サブタスク日程 — 開始日と期限を同じポップオーバーで編集 */}
      <td
        className="py-1.5 px-2 text-xs whitespace-nowrap"
        colSpan={2}
        onClick={(e) => e.stopPropagation()}
      >
        {readOnly ? (
          <span className="text-muted-foreground">
            {getTaskDisplayStartAt(sub) || getTaskDisplayEndAt(sub)
              ? `${getTaskDisplayStartAt(sub) ?? "-"} / ${getTaskDisplayEndAt(sub) ?? "-"}`
              : "-"}
          </span>
        ) : (
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
              applyTaskPatchLocally(sub.id, {
                has_recurrence: hasRecurrence,
              })
            }
            allDay={getTaskDisplayAllDay(sub)}
            startPlaceholder="Start Date"
            endPlaceholder="Due Date"
            startButtonClassName={dateButtonColor(
              getTaskDisplayStartAt(sub),
              sub,
              "start",
            )}
            endButtonClassName={dateButtonColor(
              getTaskDisplayEndAt(sub),
              sub,
              "end",
            )}
          />
        )}
      </td>
      {/* 記録時間（サブタスクでは空） */}
      <td className="py-1.5 px-2"></td>
      <td className="py-1.5 pr-2">
        <Button
          variant="ghost"
          size="icon-xs"
          onClick={(e) => {
            e.stopPropagation();
            if (readOnly) return;
            if (requestRecurringDelete?.(sub)) return;
            pushUndo({
              type: "recreate",
              tasks: [sub],
            });
            taskApi.deleteTask(sub.id).then(() => fetchData());
          }}
          className="shrink-0 text-muted-foreground hover:text-red-500"
          disabled={readOnly}
        >
          <Trash2 className="size-3" />
        </Button>
      </td>
    </tr>
  );
}

/**
 * サブタスク追加行 — 既にサブタスクがあり、かつグループに hover 中 or 入力中のみ表示。
 */
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
  fetchData,
}: {
  colSpan: number;
  projectTab: string;
  selectedProjectId: string | null;
  upsertTaskLocally: (task: Task) => void;
  fetchData: (options?: FetchDataOptions) => Promise<void>;
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
      void fetchData();
    } catch (err) {
      console.error("タスク作成失敗:", err);
    } finally {
      setQuickAddCreating(false);
    }
  }, [
    fetchData,
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
