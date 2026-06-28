"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { CornerDownRight, Plus, Trash2 } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { taskApi, type Task } from "@/lib/task-api";
import { cn } from "@/lib/utils";

// ─── サブタスクセクション ───

export function SubtaskSection({
  task,
  onEnsureTask,
  openInputSignal,
  onSubtaskAdded,
  onSubtaskUpdated,
  onSubtaskDeleted,
  onUpdated,
}: {
  task: Task;
  onEnsureTask?: () => Promise<Task | null | undefined>;
  openInputSignal?: number;
  onSubtaskAdded?: (parentTask: Task, subtask: Task) => void;
  onSubtaskUpdated?: (subtask: Task) => void;
  onSubtaskDeleted?: (subtaskId: string) => void;
  onUpdated: () => void;
}) {
  const [addTitle, setAddTitle] = useState("");
  const [adding, setAdding] = useState(false);
  const [showInput, setShowInput] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const keepInputOpenOnBlurRef = useRef(false);

  const subtasks = task.subtasks || [];

  useEffect(() => {
    if (!openInputSignal) return;
    setShowInput(true);
    setTimeout(() => inputRef.current?.focus(), 0);
  }, [openInputSignal]);

  const handleAdd = useCallback(async () => {
    const title = addTitle.trim();
    if (!title || adding) return;
    setAdding(true);
    try {
      const parentTask = (await onEnsureTask?.()) ?? task;
      if (!parentTask.id || parentTask.id === "draft") {
        toast.error("先に親タスク名を入力してください");
        return;
      }
      const subtask = await taskApi.createTask({
        project_id: parentTask.project_id,
        title,
        parent_task_id: parentTask.id,
      });
      setAddTitle("");
      keepInputOpenOnBlurRef.current = true;
      setShowInput(true);
      onSubtaskAdded?.(parentTask, subtask);
      if (!onSubtaskAdded) onUpdated();
      setTimeout(() => {
        inputRef.current?.focus();
      }, 0);
      setTimeout(() => {
        keepInputOpenOnBlurRef.current = false;
      }, 150);
    } catch (err) {
      console.error("サブタスク作成失敗:", err);
    } finally {
      setAdding(false);
    }
  }, [addTitle, adding, onEnsureTask, onSubtaskAdded, onUpdated, task]);

  return (
    <div className="space-y-2">
      <h2 className="text-sm font-medium flex items-center gap-2">
        サブタスク
        {subtasks.length > 0 && (
          <span className="text-xs text-muted-foreground">
            {subtasks.filter((s) => s.status === "closed").length}/
            {subtasks.length}
          </span>
        )}
      </h2>

      {subtasks.length > 0 && (
        <div className="space-y-1">
          {subtasks.map((sub) => (
            <div
              key={sub.id}
              className="flex items-center gap-2 rounded px-2 py-1 hover:bg-muted/50 group"
            >
              <CornerDownRight className="size-3 text-muted-foreground shrink-0" />
              <Checkbox
                checked={sub.status === "closed"}
                onCheckedChange={async (checked) => {
                  try {
                    const updated = await taskApi.updateTask(sub.id, {
                      status: checked ? "closed" : "open",
                    });
                    onSubtaskUpdated?.(updated);
                    onUpdated();
                  } catch (err) {
                    console.error("サブタスクステータス更新失敗:", err);
                  }
                }}
                className="size-3.5"
              />
              <span
                className={cn(
                  "text-sm flex-1",
                  sub.status === "closed" &&
                    "line-through text-muted-foreground",
                )}
              >
                {sub.title}
              </span>
              <Button
                variant="ghost"
                size="icon-xs"
                className="opacity-0 group-hover:opacity-100 shrink-0 text-muted-foreground hover:text-red-500"
                onClick={async () => {
                  try {
                    await taskApi.deleteTask(sub.id);
                    onSubtaskDeleted?.(sub.id);
                    onUpdated();
                  } catch (err) {
                    console.error("サブタスク削除失敗:", err);
                  }
                }}
              >
                <Trash2 className="size-3" />
              </Button>
            </div>
          ))}
        </div>
      )}

      {showInput ? (
        <div className="flex items-center gap-2 px-2">
          <Plus className="size-3 text-muted-foreground shrink-0" />
          <input
            ref={inputRef}
            type="text"
            value={addTitle}
            onChange={(e) => setAddTitle(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.nativeEvent.isComposing) {
                e.preventDefault();
                void handleAdd();
              }
              if (e.key === "Escape") {
                setShowInput(false);
                setAddTitle("");
              }
            }}
            onBlur={() => {
              if (keepInputOpenOnBlurRef.current) return;
              if (!addTitle.trim()) setShowInput(false);
            }}
            placeholder="サブタスク名を入力..."
            disabled={adding}
            className="flex-1 bg-transparent text-sm outline-none placeholder:text-muted-foreground"
            autoFocus
          />
        </div>
      ) : (
        <button
          onClick={() => {
            setShowInput(true);
            setTimeout(() => inputRef.current?.focus(), 50);
          }}
          className="flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground transition-colors px-2"
        >
          <Plus className="size-3" />
          サブタスクを追加
        </button>
      )}
    </div>
  );
}
