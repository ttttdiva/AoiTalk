"use client";

import {
  useState,
  useEffect,
  useCallback,
  useRef,
  useMemo,
} from "react";
import useSWR from "swr";
import { ChevronRight } from "lucide-react";
import { taskApi, type Task } from "@/lib/task-api";
import { useProject } from "@/contexts/project-context";
import { cn } from "@/lib/utils";
import {
  TASK_STATUS_KEY_HINTS as STATUS_KEY_HINTS,
  TASK_STATUS_LABELS as STATUS_LABELS,
} from "@/lib/task-status";
import {
  EMPTY_FILTER,
  applyTaskFilter,
  type FilterConfig,
} from "@/components/tasks/task-filter-builder";
import { formatTaskDateLabel } from "@/lib/task-date-label";
import { TaskDetailModal } from "@/components/tasks/task-detail-modal";
import {
  TaskContextMenu,
  useTaskContextMenu,
} from "@/components/tasks/task-context-menu";
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuItem,
} from "@/components/ui/dropdown-menu";
import {
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
} from "@/components/ui/sidebar";

const TASK_SIDEBAR_VIEW_STATE_KEY = "tasks-sidebar-view-state";

// SWR キャッシュキー。タスクサイドバーは全タスク（スコープ指定なし）を取得するため、
// スペース/プロジェクトで絞り込むタスク一覧ページ（use-tasks-data）とは取得パラメータが
// 異なる。挙動維持のためキャッシュは共有せず専用キーを使う。
const TASK_SIDEBAR_SWR_KEY = "task-sidebar/tasks";

const EMPTY_TASKS: Task[] = [];

type TaskSidebarViewState = {
  filter: "all" | "overdue";
  projectTab: string;
  showClosed: boolean;
  showFuture: boolean;
  customFilter: FilterConfig;
};

const DEFAULT_TASK_SIDEBAR_VIEW_STATE: TaskSidebarViewState = {
  filter: "all",
  projectTab: "all",
  showClosed: false,
  showFuture: false,
  customFilter: EMPTY_FILTER,
};

function isFutureTask(task: Task): boolean {
  if (!task.start_at) return false;
  const start = new Date(task.start_at);
  const tomorrow = new Date();
  tomorrow.setHours(0, 0, 0, 0);
  tomorrow.setDate(tomorrow.getDate() + 1);
  const taskDay = new Date(
    start.getFullYear(),
    start.getMonth(),
    start.getDate(),
  );
  return taskDay >= tomorrow;
}

function isOverdue(task: Task): boolean {
  if (!task.end_at || task.status === "closed") return false;
  const due = new Date(task.end_at);
  if (task.all_day || (due.getHours() === 0 && due.getMinutes() === 0)) {
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const dueDay = new Date(due);
    dueDay.setHours(0, 0, 0, 0);
    return dueDay < today;
  }
  return due < new Date();
}

function readTaskSidebarViewState(): TaskSidebarViewState {
  if (typeof window === "undefined") return DEFAULT_TASK_SIDEBAR_VIEW_STATE;
  try {
    const raw = window.localStorage.getItem(TASK_SIDEBAR_VIEW_STATE_KEY);
    if (!raw) return DEFAULT_TASK_SIDEBAR_VIEW_STATE;
    const parsed = JSON.parse(raw) as Partial<TaskSidebarViewState>;
    return {
      ...DEFAULT_TASK_SIDEBAR_VIEW_STATE,
      ...parsed,
      customFilter: parsed.customFilter ?? EMPTY_FILTER,
    };
  } catch {
    return DEFAULT_TASK_SIDEBAR_VIEW_STATE;
  }
}

// ─── タスク用サイドバー ───
export function TaskSidebar() {
  const { projects } = useProject();
  const [loading, setLoading] = useState(true);
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [openMenuTaskId, setOpenMenuTaskId] = useState<string | null>(null);
  const hasLoadedTasksRef = useRef(false);
  const [viewState, setViewState] = useState<TaskSidebarViewState>(() =>
    readTaskSidebarViewState(),
  );
  const contextMenu = useTaskContextMenu();

  // 取得・キャッシュ・重複排除・競合破棄は SWR に委譲。取得タイミングは従来どおり
  // fetchTasks（= 手動 revalidate）で駆動し、自動 revalidation は全て無効化する。
  const { data, mutate } = useSWR<Task[]>(
    TASK_SIDEBAR_SWR_KEY,
    () => taskApi.listTasks(),
    {
      revalidateOnMount: false,
      revalidateOnFocus: false,
      revalidateOnReconnect: false,
      revalidateIfStale: false,
      keepPreviousData: true,
      dedupingInterval: 0,
    },
  );
  const tasks = data ?? EMPTY_TASKS;

  const fetchTasks = useCallback(
    async (options: { forceLoading?: boolean } = {}) => {
      const shouldShowLoading =
        options.forceLoading ?? !hasLoadedTasksRef.current;
      if (shouldShowLoading) setLoading(true);
      // 失敗時も bound mutate は reject せず、SWR が直前の data を保持する
      // （従来の .catch(()=>{}) と同義）。
      await mutate();
      hasLoadedTasksRef.current = true;
      setLoading(false);
    },
    [mutate],
  );

  useEffect(() => {
    const timer = setTimeout(() => {
      void fetchTasks();
    }, 0);
    return () => clearTimeout(timer);
  }, [fetchTasks]);

  useEffect(() => {
    const handler = () => {
      setViewState(readTaskSidebarViewState());
      fetchTasks();
    };
    window.addEventListener("task-list-refresh", handler);
    window.addEventListener("task-sidebar-refresh", handler);
    return () => {
      window.removeEventListener("task-list-refresh", handler);
      window.removeEventListener("task-sidebar-refresh", handler);
    };
  }, [fetchTasks]);

  // 選択中のスペース/プロジェクトに属するタスクのみ表示
  const projectMap = useMemo(
    () => new Map(projects.map((project) => [project.id, project])),
    [projects],
  );
  const projectNameMap = useMemo(
    () => new Map(projects.map((project) => [project.id, project.name])),
    [projects],
  );
  const filteredTasks = useMemo(() => {
    let result = tasks.filter((task) => !task.parent_task_id);

    if (viewState.projectTab !== "all") {
      result = result.filter(
        (task) => task.project_id === viewState.projectTab,
      );
    } else {
      result = result.filter((task) => projectMap.has(task.project_id));
    }

    if (!viewState.showClosed) {
      result = result.filter((task) => task.status !== "closed");
    }

    if (!viewState.showFuture) {
      result = result.filter((task) => !isFutureTask(task));
    }

    if (viewState.filter === "overdue") {
      result = result.filter(isOverdue);
    }

    if (viewState.customFilter.rules.length > 0) {
      result = applyTaskFilter(result, viewState.customFilter, projectNameMap);
    }

    return result;
  }, [projectMap, projectNameMap, tasks, viewState]);
  const grouped = new Map<string, Task[]>();
  for (const task of filteredTasks) {
    const list = grouped.get(task.project_id) || [];
    list.push(task);
    grouped.set(task.project_id, list);
  }

  const toggleCollapse = (projectId: string) => {
    setCollapsed((prev) => ({ ...prev, [projectId]: !prev[projectId] }));
  };

  const updateTaskStatus = useCallback(
    async (task: Task, status: string) => {
      if (task.status === status) return;
      try {
        await taskApi.updateTask(task.id, { status });
        // 楽観的更新: SWR キャッシュのみ書き換え、再取得はしない。
        void mutate(
          (prev) =>
            (prev ?? EMPTY_TASKS).map((t) =>
              t.id === task.id ? { ...t, status } : t,
            ),
          { revalidate: false },
        );
      } catch {}
    },
    [mutate],
  );

  return (
    <>
      <SidebarGroup>
        <SidebarGroupLabel>タスク ({filteredTasks.length})</SidebarGroupLabel>
        <SidebarGroupContent>
          {loading && (
            <div className="px-4 py-3 text-center text-xs text-muted-foreground">
              読み込み中...
            </div>
          )}
          {!loading && filteredTasks.length === 0 && (
            <div className="px-4 py-3 text-center text-xs text-muted-foreground">
              未完了タスクはありません
            </div>
          )}
          {!loading &&
            Array.from(grouped.entries()).map(([projectId, projectTasks]) => {
              const project = projectMap.get(projectId);
              const isCollapsed = collapsed[projectId] ?? false;
              return (
                <div key={projectId}>
                  <button
                    onClick={() => toggleCollapse(projectId)}
                    className="flex w-full items-center gap-1.5 px-2 py-1.5 text-xs font-medium text-muted-foreground hover:text-foreground transition-colors"
                  >
                    <ChevronRight
                      className={`size-3 shrink-0 transition-transform ${isCollapsed ? "" : "rotate-90"}`}
                    />
                    <span className="truncate">{project?.name || "不明"}</span>
                    <span className="ml-auto text-[10px] tabular-nums">
                      {projectTasks.length}
                    </span>
                  </button>
                  {!isCollapsed && (
                    <SidebarMenu>
                      {projectTasks.map((task) => (
                        <SidebarMenuItem
                          key={task.id}
                          onContextMenu={(e) => contextMenu.open(e, task)}
                        >
                          <div className="flex items-start gap-2 pl-5 py-1">
                            <DropdownMenu
                              open={openMenuTaskId === task.id}
                              onOpenChange={(open) =>
                                setOpenMenuTaskId(open ? task.id : null)
                              }
                            >
                              <DropdownMenuTrigger
                                className={cn(
                                  "size-3.5 mt-1 shrink-0 rounded-full border-2 transition-colors hover:ring-2 hover:ring-offset-1 hover:ring-primary/30 cursor-pointer",
                                  task.status === "open" &&
                                    "border-gray-400 dark:border-gray-500",
                                  task.status === "in_progress" &&
                                    "border-yellow-400 bg-yellow-400/30 dark:border-yellow-500 dark:bg-yellow-500/30",
                                  task.status === "on_hold" &&
                                    "border-pink-400 bg-pink-400/30 dark:border-pink-500 dark:bg-pink-500/30",
                                  task.status === "review" &&
                                    "border-sky-400 bg-sky-400/30 dark:border-sky-500 dark:bg-sky-500/30",
                                  task.status === "closed" &&
                                    "border-green-500 bg-green-500 dark:border-green-400 dark:bg-green-400",
                                )}
                                title={
                                  STATUS_LABELS[task.status] ||
                                  STATUS_LABELS.open
                                }
                              />
                              <DropdownMenuContent
                                align="start"
                                className="min-w-36"
                              >
                                {(
                                  [
                                    "open",
                                    "in_progress",
                                    "on_hold",
                                    "review",
                                    "closed",
                                  ] as const
                                ).map((status) => (
                                  <DropdownMenuItem
                                    key={status}
                                    mnemonic={STATUS_KEY_HINTS[status]}
                                    className={cn(
                                      "flex items-center justify-between gap-2 cursor-pointer",
                                      task.status === status && "font-bold",
                                    )}
                                    onClick={async () => {
                                      setOpenMenuTaskId(null);
                                      await updateTaskStatus(task, status);
                                    }}
                                  >
                                    <span className="flex items-center gap-2">
                                      <span
                                        className={cn(
                                          "inline-block size-2.5 rounded-full border-2",
                                          status === "open" &&
                                            "border-gray-400",
                                          status === "in_progress" &&
                                            "border-yellow-400 bg-yellow-400/30",
                                          status === "on_hold" &&
                                            "border-pink-400 bg-pink-400/30",
                                          status === "review" &&
                                            "border-sky-400 bg-sky-400/30",
                                          status === "closed" &&
                                            "border-green-500 bg-green-500",
                                        )}
                                      />
                                      {STATUS_LABELS[status]}
                                    </span>
                                  </DropdownMenuItem>
                                ))}
                              </DropdownMenuContent>
                            </DropdownMenu>
                            <SidebarMenuButton
                              onClick={() => setSelectedTaskId(task.id)}
                              className="items-start !pl-0 flex-1 min-w-0"
                            >
                              <div className="flex min-w-0 flex-1 flex-col">
                                <span className="truncate text-sm">
                                  {task.title}
                                </span>
                                {task.end_at && (
                                  <span className="text-xs text-muted-foreground">
                                    期限:{" "}
                                    {formatTaskDateLabel(task.end_at, {
                                      allDay: task.all_day,
                                      absoluteStyle: "short",
                                    })}
                                  </span>
                                )}
                              </div>
                            </SidebarMenuButton>
                          </div>
                        </SidebarMenuItem>
                      ))}
                    </SidebarMenu>
                  )}
                </div>
              );
            })}
        </SidebarGroupContent>
      </SidebarGroup>
      <TaskDetailModal
        taskId={selectedTaskId}
        open={!!selectedTaskId}
        onOpenChange={(open) => {
          if (!open) setSelectedTaskId(null);
        }}
        onTaskUpdated={fetchTasks}
      />
      <TaskContextMenu
        menu={contextMenu.menu}
        onClose={contextMenu.close}
        onRefresh={() => {
          fetchTasks();
          window.dispatchEvent(new Event("task-list-refresh"));
        }}
      />
    </>
  );
}
