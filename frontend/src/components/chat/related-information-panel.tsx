"use client";

import { useCallback, useEffect, useState } from "react";
import { CheckSquare, FolderOpen, Flag, RefreshCcw } from "lucide-react";
import { Button } from "@/components/ui/button";

export type RelatedTaskSummary = {
  id: string;
  title: string;
  status: string;
  priority: string;
  project_id: string;
  project_name?: string | null;
  updated_at?: string | null;
};

export function RelatedInformationPanel({
  sessionId,
  onTaskClick,
  onTasksChange,
}: {
  sessionId: string | null;
  onTaskClick: (taskId: string) => void;
  onTasksChange?: (tasks: RelatedTaskSummary[]) => void;
}) {
  const [tasks, setTasks] = useState<RelatedTaskSummary[]>([]);
  const [loading, setLoading] = useState(false);

  const refresh = useCallback(async () => {
    if (!sessionId) {
      setTasks([]);
      return;
    }
    setLoading(true);
    try {
      const response = await fetch(`/api/conversations/${encodeURIComponent(sessionId)}/related-tasks`, {
        credentials: "include",
        cache: "no-store",
      });
      if (!response.ok) throw new Error(`related tasks: ${response.status}`);
      const data = (await response.json()) as { tasks?: RelatedTaskSummary[] };
      const next = Array.isArray(data.tasks) ? data.tasks : [];
      setTasks(next);
      onTasksChange?.(next);
    } catch (error) {
      console.warn("関連タスク取得に失敗しました", error);
      setTasks([]);
      onTasksChange?.([]);
    } finally {
      setLoading(false);
    }
  }, [onTasksChange, sessionId]);

  useEffect(() => {
    void refresh();
    const interval = window.setInterval(() => void refresh(), 5000);
    const handleTaskEvent = () => void refresh();
    window.addEventListener("aoitalk-task-updated", handleTaskEvent);
    window.addEventListener("aoitalk-task-created", handleTaskEvent);
    return () => {
      window.clearInterval(interval);
      window.removeEventListener("aoitalk-task-updated", handleTaskEvent);
      window.removeEventListener("aoitalk-task-created", handleTaskEvent);
    };
  }, [refresh]);

  return (
    <section className="flex min-h-0 flex-1 flex-col border-b border-border">
      <div className="flex items-center justify-between gap-2 border-b px-3 py-2">
        <h2 className="text-sm font-medium">関連タスク <span className="text-xs text-muted-foreground">({tasks.length})</span></h2>
        <Button type="button" size="icon" variant="ghost" onClick={() => void refresh()} disabled={loading} title="再取得">
          <RefreshCcw className={loading ? "size-3.5 animate-spin" : "size-3.5"} />
        </Button>
      </div>
      <div className="min-h-0 flex-1 overflow-auto p-2">
        {tasks.length === 0 ? <p className="px-2 py-6 text-center text-xs text-muted-foreground">このチャットに関連するタスクはありません</p> : <div className="space-y-1">
          {tasks.map((task) => <button key={task.id} type="button" className="w-full rounded-md border p-2 text-left hover:bg-accent" onClick={() => onTaskClick(task.id)}>
            <div className="flex items-start gap-2"><CheckSquare className="mt-0.5 size-4 shrink-0 text-muted-foreground" /><span className="min-w-0 flex-1 truncate text-sm font-medium">{task.title}</span></div>
            <div className="mt-1 flex flex-wrap gap-x-2 gap-y-1 text-[11px] text-muted-foreground"><span>{task.status}</span><span><Flag className="mr-0.5 inline size-3" />{task.priority}</span>{task.project_name && <span><FolderOpen className="mr-0.5 inline size-3" />{task.project_name}</span>}</div>
          </button>)}
        </div>}
      </div>
    </section>
  );
}
