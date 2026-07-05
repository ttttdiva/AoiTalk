"use client";

import React, { useCallback, useEffect, useState } from "react";
import { createPortal } from "react-dom";
import {
  ChevronRight,
  Copy,
  Flag,
  Hash,
  Play,
  RefreshCw,
  Square,
  Trash2,
} from "lucide-react";
import { toast } from "sonner";
import {
  taskApi,
  type RecurringOccurrenceContext,
  type Task,
} from "@/lib/task-api";
import {
  createTaskCompletionUndoEntry,
  dispatchTaskCompletionUndoBatch,
  isTaskCompletionTransition,
} from "@/lib/task-completion-undo";
import {
  TASK_STATUS_DOT_COLORS as STATUS_DOT,
  TASK_STATUS_KEY_HINTS as STATUS_KEY_HINT,
  TASK_STATUS_LABELS as STATUS_LABEL,
  TASK_STATUS_OPTIONS as STATUS_ORDER,
  TASK_STATUS_SHORTCUT_KEYS as STATUS_SHORTCUT,
  type TaskStatusOption as Status,
} from "@/lib/task-status";
import { cn } from "@/lib/utils";
import { useContextMenuPosition } from "@/hooks/use-context-menu-position";

type Priority = "urgent" | "high" | "medium" | "low" | "none";

const PRIORITY_ORDER: Priority[] = ["urgent", "high", "medium", "low", "none"];

const PRIORITY_LABEL: Record<Priority, string> = {
  urgent: "Urgent",
  high: "High",
  medium: "Medium",
  low: "Low",
  none: "None",
};

const PRIORITY_DOT: Record<Priority, string> = {
  urgent: "bg-red-500",
  high: "bg-orange-500",
  medium: "bg-yellow-500",
  low: "bg-blue-500",
  none: "bg-gray-400",
};

async function copyTextToClipboard(text: string) {
  try {
    await navigator.clipboard.writeText(text);
    return;
  } catch (err) {
    if (typeof document === "undefined") throw err;
    const textarea = document.createElement("textarea");
    textarea.value = text;
    textarea.setAttribute("readonly", "");
    textarea.style.position = "fixed";
    textarea.style.left = "-9999px";
    document.body.appendChild(textarea);
    textarea.select();
    try {
      if (!document.execCommand("copy")) throw err;
    } finally {
      document.body.removeChild(textarea);
    }
  }
}

export interface TaskContextMenuState {
  x: number;
  y: number;
  task: Task;
  occurrenceContext?: RecurringOccurrenceContext | null;
}

export function useTaskContextMenu() {
  const [menu, setMenu] = useState<TaskContextMenuState | null>(null);

  const open = useCallback(
    (
      e: React.MouseEvent,
      task: Task,
      occurrenceContext?: RecurringOccurrenceContext | null,
    ) => {
      e.preventDefault();
      e.stopPropagation();
      setMenu({ x: e.clientX, y: e.clientY, task, occurrenceContext });
    },
    [],
  );

  const close = useCallback(() => setMenu(null), []);

  return { menu, open, close };
}

interface Props {
  menu: TaskContextMenuState | null;
  onClose: () => void;
  onRefresh: () => void;
}

export function TaskContextMenu({ menu, onClose, onRefresh }: Props) {
  const { ref, style, submenuSide } = useContextMenuPosition(
    menu ? { x: menu.x, y: menu.y } : null,
    { fallbackWidth: 200, fallbackHeight: 300, submenuWidth: 160 },
  );
  const [statusOpen, setStatusOpen] = useState(false);
  const [priorityOpen, setPriorityOpen] = useState(false);

  useEffect(() => {
    if (!menu) return;
    const handleClick = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose();
    };
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("mousedown", handleClick);
    document.addEventListener("keydown", handleKey);
    return () => {
      document.removeEventListener("mousedown", handleClick);
      document.removeEventListener("keydown", handleKey);
    };
  }, [menu, onClose, ref]);

  if (!menu) return null;
  const { task, occurrenceContext } = menu;
  if (typeof document === "undefined") return null;

  const changeStatus = async (status: Status) => {
    onClose();
    try {
      if (task.has_recurrence && occurrenceContext?.start_at) {
        await taskApi.updateOccurrenceStatus(task.id, {
          occurrence_id: occurrenceContext.occurrence_id ?? null,
          occurrence_start_at: occurrenceContext.start_at,
          occurrence_end_at: occurrenceContext.end_at ?? null,
          original_start_at: occurrenceContext.original_start_at ?? null,
          status,
        });
        onRefresh();
        return;
      }

      await taskApi.updateTask(task.id, { status });
      if (isTaskCompletionTransition(task.status, status)) {
        dispatchTaskCompletionUndoBatch({
          entries: [createTaskCompletionUndoEntry(task)],
        });
      }
      onRefresh();
    } catch (err) {
      console.error("ステータス更新失敗:", err);
    }
  };

  const changePriority = async (priority: Priority) => {
    onClose();
    try {
      await taskApi.updateTask(task.id, { priority });
      onRefresh();
    } catch (err) {
      console.error("優先度更新失敗:", err);
    }
  };

  const toggleTimer = async () => {
    onClose();
    try {
      if (task.active_time_entry) {
        await taskApi.stopTimer(task.active_time_entry.id);
        window.dispatchEvent(
          new CustomEvent("timer-changed", {
            detail: { activeEntry: null },
          }),
        );
      } else {
        const started = await taskApi.startTimer(task.id);
        window.dispatchEvent(
          new CustomEvent("timer-changed", {
            detail: { activeEntry: started },
          }),
        );
      }
      onRefresh();
    } catch (err) {
      console.error("タイマー操作失敗:", err);
    }
  };

  const duplicate = async () => {
    onClose();
    try {
      await taskApi.createTask({
        project_id: task.project_id,
        title: `コピー: ${task.title}`,
        description: task.description || "",
        status: task.status,
        priority: task.priority,
        notifications_enabled: task.notifications_enabled,
        tag_ids: (task.tags || []).map((t) => t.id),
      });
      onRefresh();
    } catch (err) {
      console.error("タスク複製失敗:", err);
    }
  };

  const copyTaskId = async () => {
    onClose();
    try {
      await copyTextToClipboard(task.id);
      toast.success("タスクIDをコピーしました", {
        description: `@${task.id} でタスク候補を検索できます`,
      });
    } catch (err) {
      console.error("タスクIDコピー失敗:", err);
      toast.error("タスクIDのコピーに失敗しました");
    }
  };

  const remove = async () => {
    onClose();
    try {
      if (task.has_recurrence && occurrenceContext?.start_at) {
        const deleteFuture = window.confirm(
          "この回以降の繰り返しを削除しますか？\nOK: 今回以降を削除\nCancel: 選択を続ける",
        );
        if (deleteFuture) {
          await taskApi.deleteOccurrence(task.id, {
            mode: "future",
            occurrence_id: occurrenceContext.occurrence_id ?? null,
            occurrence_start_at: occurrenceContext.start_at,
            occurrence_end_at: occurrenceContext.end_at ?? null,
            original_start_at: occurrenceContext.original_start_at ?? null,
          });
          onRefresh();
          return;
        }

        const deleteSingle = window.confirm(
          "今回の発生分だけ削除しますか？\nOK: 今回だけ削除\nCancel: 中止",
        );
        if (!deleteSingle) return;

        await taskApi.deleteOccurrence(task.id, {
          mode: "single",
          occurrence_id: occurrenceContext.occurrence_id ?? null,
          occurrence_start_at: occurrenceContext.start_at,
          occurrence_end_at: occurrenceContext.end_at ?? null,
          original_start_at: occurrenceContext.original_start_at ?? null,
        });
        onRefresh();
        return;
      }

      await taskApi.deleteTask(task.id);
      onRefresh();
    } catch (err) {
      console.error("タスク削除失敗:", err);
    }
  };

  const submenuClassName = cn(
    "absolute top-0 min-w-36 rounded-md border bg-popover p-1 text-popover-foreground shadow-md",
    submenuSide === "left" ? "right-full mr-1" : "left-full ml-1",
  );

  return createPortal(
    <div
      ref={ref}
      className="fixed z-50 min-w-48 rounded-md border bg-popover p-1 text-popover-foreground shadow-md"
      style={style}
      onContextMenu={(e) => e.preventDefault()}
    >
      <div
        className="relative"
        onMouseEnter={() => setStatusOpen(true)}
        onMouseLeave={() => setStatusOpen(false)}
      >
        <button className="flex w-full items-center justify-between rounded-sm px-2 py-1.5 text-sm hover:bg-accent hover:text-accent-foreground cursor-default">
          <span className="flex items-center gap-2">
            <RefreshCw className="size-4" />
            ステータス変更
          </span>
          <ChevronRight className="size-4" />
        </button>
        {statusOpen && (
          <div
            className={submenuClassName}
            onKeyDown={(e) => {
              const target = STATUS_SHORTCUT[e.key.toLowerCase()];
              if (target) {
                e.preventDefault();
                changeStatus(target);
              }
            }}
          >
            {STATUS_ORDER.map((status) => (
              <button
                key={status}
                className={cn(
                  "flex w-full items-center justify-between gap-2 rounded-sm px-2 py-1.5 text-sm hover:bg-accent hover:text-accent-foreground cursor-default",
                  task.status === status && "font-bold",
                )}
                onClick={() => changeStatus(status)}
              >
                <span className="flex items-center gap-2">
                  <span
                    className={cn(
                      "inline-block size-2 rounded-full border-2",
                      STATUS_DOT[status],
                    )}
                  />
                  {STATUS_LABEL[status]}
                </span>
                <kbd className="text-[10px] text-muted-foreground opacity-60">
                  {STATUS_KEY_HINT[status]}
                </kbd>
              </button>
            ))}
          </div>
        )}
      </div>

      <div
        className="relative"
        onMouseEnter={() => setPriorityOpen(true)}
        onMouseLeave={() => setPriorityOpen(false)}
      >
        <button className="flex w-full items-center justify-between rounded-sm px-2 py-1.5 text-sm hover:bg-accent hover:text-accent-foreground cursor-default">
          <span className="flex items-center gap-2">
            <Flag className="size-4" />
            優先度変更
          </span>
          <ChevronRight className="size-4" />
        </button>
        {priorityOpen && (
          <div className={submenuClassName}>
            {PRIORITY_ORDER.map((priority) => (
              <button
                key={priority}
                className={cn(
                  "flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-sm hover:bg-accent hover:text-accent-foreground cursor-default",
                  task.priority === priority && "font-bold",
                )}
                onClick={() => changePriority(priority)}
              >
                <span
                  className={cn(
                    "inline-block size-2 rounded-full",
                    PRIORITY_DOT[priority],
                  )}
                />
                {PRIORITY_LABEL[priority]}
              </button>
            ))}
          </div>
        )}
      </div>

      <div className="my-1 h-px bg-border" />

      <button
        className="flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-sm hover:bg-accent hover:text-accent-foreground cursor-default"
        onClick={toggleTimer}
      >
        {task.active_time_entry ? (
          <>
            <Square className="size-4" />
            タイマー停止
          </>
        ) : (
          <>
            <Play className="size-4" />
            タイマー開始
          </>
        )}
      </button>

      <button
        className="flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-sm hover:bg-accent hover:text-accent-foreground cursor-default"
        onClick={duplicate}
      >
        <Copy className="size-4" />
        複製
      </button>

      <button
        className="flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-sm hover:bg-accent hover:text-accent-foreground cursor-default"
        onClick={copyTaskId}
      >
        <Hash className="size-4" />
        タスクIDをコピー
      </button>

      <div className="my-1 h-px bg-border" />

      <button
        className="flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-sm text-red-600 hover:bg-red-100 dark:text-red-400 dark:hover:bg-red-900/30 cursor-default"
        onClick={remove}
      >
        <Trash2 className="size-4" />
        削除
      </button>
    </div>,
    document.body,
  );
}
