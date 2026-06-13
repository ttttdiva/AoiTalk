"use client";

/* eslint-disable @next/next/no-img-element */

import { useRouter, useSearchParams } from "next/navigation";
import {
  useState,
  useEffect,
  useCallback,
  useRef,
  useMemo,
  Suspense,
  type MouseEvent,
} from "react";
import {
  Plus,
  MessageSquare,
  CheckSquare,
  CheckCheck,
  FolderOpen,
  MoreHorizontal,
  Trash2,
  ChevronRight,
  Bell,
  X,
  Folder,
  FileIcon,
  Music,
  Film,
  ArrowUp,
  Home,
  Upload,
  Layers,
  Table2,
} from "lucide-react";
import {
  chatApi,
  type ScenarioLogResponse,
} from "@/lib/chat-api";
import { taskApi, type Task } from "@/lib/task-api";
import {
  explorerCopy,
  explorerDelete,
  explorerList,
  ExplorerUploadError,
  explorerMove,
  explorerUpload,
  filerBrowse,
  type ExplorerDirectory,
  type ExplorerListResponse,
  type ExplorerFile,
} from "@/lib/explorer-api";
import { getDroppedExplorerFiles } from "@/lib/file-drop";
import {
  deleteProjectRecordTable,
  isRecordTableFile,
  listProjectRecordTables,
  recordTableToExplorerFile,
} from "@/lib/record-tables-api";
import { TaskDetailModal } from "@/components/tasks/task-detail-modal";
import {
  TaskContextMenu,
  useTaskContextMenu,
} from "@/components/tasks/task-context-menu";
import { useProject } from "@/contexts/project-context";
import { useAudioPlayer } from "@/contexts/audio-player-context";
import { cn } from "@/lib/utils";
import { toast } from "sonner";
import { useChatSessions } from "@/contexts/chat-session-context";
import {
  TASK_STATUS_LABELS as STATUS_LABELS,
  TASK_STATUS_SHORTCUT_KEYS as STATUS_SHORTCUT_KEYS,
} from "@/lib/task-status";
import {
  EMPTY_FILTER,
  applyTaskFilter,
  type FilterConfig,
} from "@/components/tasks/task-filter-builder";
import { formatTaskDateLabel } from "@/lib/task-date-label";
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuItem,
} from "@/components/ui/dropdown-menu";
import {
  Sidebar,
  SidebarContent,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
} from "@/components/ui/sidebar";
import {
  CHAT_SESSION_NAVIGATION_EVENT,
  navigateChatSessionInPlace,
  readChatSessionIdFromLocation,
} from "@/lib/chat-navigation";

/** 相対時間を返す */
function formatRelativeTime(dateStr: string | null | undefined): string {
  if (!dateStr) return "";
  const date = new Date(dateStr);
  const now = new Date();
  const diffMs = now.getTime() - date.getTime();
  const diffSec = Math.floor(diffMs / 1000);
  const diffMin = Math.floor(diffSec / 60);
  const diffHour = Math.floor(diffMin / 60);
  const diffDay = Math.floor(diffHour / 24);

  if (diffSec < 60) return "たった今";
  if (diffMin < 60) return `${diffMin}分前`;
  if (diffHour < 24) return `${diffHour}時間前`;
  if (diffDay < 7) return `${diffDay}日前`;
  if (diffDay < 30) return `${Math.floor(diffDay / 7)}週間前`;
  return date.toLocaleDateString("ja-JP");
}

const TASK_SIDEBAR_VIEW_STATE_KEY = "tasks-sidebar-view-state";

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

function handleSidebarAnchorNavigation(
  event: MouseEvent<HTMLAnchorElement | HTMLButtonElement>,
  href: string,
) {
  if (
    event.defaultPrevented ||
    event.button !== 0 ||
    event.metaKey ||
    event.ctrlKey ||
    event.shiftKey ||
    event.altKey
  ) {
    return;
  }

  event.preventDefault();
  if (!navigateChatSessionInPlace(href)) {
    window.location.href = href;
  }
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
// ─── チャット用サイドバー ───
function ChatSidebar() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const searchParamSessionId = searchParams.get("s") || null;
  const [activeSessionId, setActiveSessionId] = useState(searchParamSessionId);
  const { selectedProjectId } = useProject();
  const { sessions, sessionsError, fetchSessions, addSession, removeSession } =
    useChatSessions();
  const [isCreatingSession, setIsCreatingSession] = useState(false);
  const [scenarioLogContextState, setScenarioLogContextState] = useState<{
    sessionId: string;
    data: ScenarioLogResponse | null;
  } | null>(null);

  useEffect(() => {
    fetchSessions();
  }, [fetchSessions]);

  useEffect(() => {
    setActiveSessionId(searchParamSessionId);
  }, [searchParamSessionId]);

  useEffect(() => {
    const syncActiveSessionId = () => {
      setActiveSessionId(readChatSessionIdFromLocation());
    };

    window.addEventListener(
      CHAT_SESSION_NAVIGATION_EVENT,
      syncActiveSessionId,
    );
    window.addEventListener("popstate", syncActiveSessionId);
    return () => {
      window.removeEventListener(
        CHAT_SESSION_NAVIGATION_EVENT,
        syncActiveSessionId,
      );
      window.removeEventListener("popstate", syncActiveSessionId);
    };
  }, []);

  useEffect(() => {
    let cancelled = false;

    if (!activeSessionId) return;

    (async () => {
      try {
        const data =
          await chatApi.getScenarioLogContextByConversation(activeSessionId);
        if (!cancelled) {
          setScenarioLogContextState({ sessionId: activeSessionId, data });
        }
      } catch {
        if (!cancelled) {
          setScenarioLogContextState({ sessionId: activeSessionId, data: null });
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [activeSessionId]);

  const handleCreateSession = useCallback(async () => {
    if (isCreatingSession) return;

    setIsCreatingSession(true);
    try {
      let characterName = "aoi";
      try {
        const res = await fetch("/api/python-proxy/characters", {
          credentials: "include",
          signal: AbortSignal.timeout(3000),
        });
        if (res.ok) {
          const data = await res.json();
          if (typeof data.current === "string" && data.current.trim()) {
            characterName = data.current;
          }
        }
      } catch (err) {
        console.error("現在キャラクター取得エラー:", err);
      }

      const data = await chatApi.createSession(
        characterName,
        selectedProjectId ?? undefined,
      );
      addSession(data.session);
      localStorage.setItem("aoitalk_last_session_id", data.session.id);

      const href = `/chat?s=${encodeURIComponent(data.session.id)}`;
      if (!navigateChatSessionInPlace(href)) {
        router.push(href);
      }
    } catch (err) {
      console.error("新規会話作成エラー:", err);
      localStorage.removeItem("aoitalk_last_session_id");
      if (!navigateChatSessionInPlace("/chat")) {
        router.push("/chat");
      }
    } finally {
      setIsCreatingSession(false);
    }
  }, [addSession, isCreatingSession, router, selectedProjectId]);

  const handleDeleteSession = useCallback(
    async (id: string) => {
      try {
        await chatApi.deleteSession(id);
        removeSession(id);
        if (activeSessionId === id) {
          localStorage.removeItem("aoitalk_last_session_id");
          router.push("/chat");
        }
      } catch (err) {
        console.error("セッション削除エラー:", err);
      }
    },
    [activeSessionId, router, removeSession],
  );

  const sortedSessions = [...sessions].sort((a, b) => {
    const dateA = new Date(a.last_activity ?? a.session_start ?? 0).getTime();
    const dateB = new Date(b.last_activity ?? b.session_start ?? 0).getTime();
    return dateB - dateA;
  });

  const scenarioLogContext =
    activeSessionId && scenarioLogContextState?.sessionId === activeSessionId
      ? scenarioLogContextState.data
      : null;
  const scenarioTitle = scenarioLogContext?.scenario?.title;
  const scenarioLogs = scenarioLogContext?.logs ?? [];

  return (
    <>
      {scenarioTitle && (
        <SidebarGroup>
          <div className="flex items-center justify-between px-2">
            <div className="min-w-0">
              <SidebarGroupLabel>ログ</SidebarGroupLabel>
              <div className="truncate px-2 text-xs text-muted-foreground">
                {scenarioTitle}
              </div>
            </div>
            <button
              onClick={handleCreateSession}
              disabled={isCreatingSession}
              className="p-1 rounded hover:bg-accent"
              aria-label="新規通常会話"
              title="新規通常会話"
            >
              <Plus className="size-4" />
              <span className="sr-only">新規通常会話</span>
            </button>
          </div>
          <SidebarGroupContent>
            <SidebarMenu>
              {scenarioLogs.length === 0 && (
                <li className="px-4 py-6 text-center text-xs text-muted-foreground">
                  ログがありません
                </li>
              )}
              {scenarioLogs.map((log) => {
                const isActive =
                  !!activeSessionId &&
                  log.conversation_session_id === activeSessionId;
                const href = log.href;
                return (
                  <SidebarMenuItem key={`${log.type}:${log.id}`}>
                    <SidebarMenuButton
                      isActive={isActive}
                      render={
                        href ? (
                          <button
                            type="button"
                            onClick={(event) =>
                              handleSidebarAnchorNavigation(event, href)
                            }
                          />
                        ) : (
                          <button type="button" disabled />
                        )
                      }
                      className="group/session-item"
                    >
                      <MessageSquare className="size-4 shrink-0" />
                      <div className="flex min-w-0 flex-1 flex-col">
                        <span className="truncate text-sm">
                          {log.target_label || log.title}
                        </span>
                        <span className="truncate text-xs text-muted-foreground">
                          {log.type_label}
                          {log.updated_at && (
                            <> &middot; {formatRelativeTime(log.updated_at)}</>
                          )}
                          {log.count > 0 && <> &middot; {log.count}件</>}
                        </span>
                      </div>
                    </SidebarMenuButton>
                  </SidebarMenuItem>
                );
              })}
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>
      )}

      <SidebarGroup>
        <div className="flex items-center justify-between px-2">
          <SidebarGroupLabel>会話履歴</SidebarGroupLabel>
          <button
            onClick={handleCreateSession}
            disabled={isCreatingSession}
            className="flex items-center gap-1 rounded px-1.5 py-1 text-xs hover:bg-accent disabled:opacity-50"
            aria-label="新規会話"
            title="新規会話"
          >
            <Plus className="size-4" />
            <span>新規会話</span>
          </button>
        </div>
        <SidebarGroupContent>
          <SidebarMenu>
            {sessionsError && (
              <li className="px-4 py-6 text-center text-xs text-destructive">
                {sessionsError}
              </li>
            )}
            {!sessionsError && sortedSessions.length === 0 && (
              <li className="px-4 py-6 text-center text-xs text-muted-foreground">
                会話がありません
              </li>
            )}
            {!sessionsError &&
              sortedSessions.map((s) => {
                const href = `/chat?s=${encodeURIComponent(s.id)}`;
                return (
                  <SidebarMenuItem key={s.id}>
                    <SidebarMenuButton
                      isActive={activeSessionId === s.id}
                      render={
                        <button
                          type="button"
                          onClick={(event) =>
                            handleSidebarAnchorNavigation(event, href)
                          }
                        />
                      }
                      className="group/session-item"
                    >
                      <MessageSquare className="size-4 shrink-0" />
                      <div className="flex min-w-0 flex-1 flex-col">
                        <span className="truncate text-sm">
                          {s.title || "無題の会話"}
                        </span>
                        <span className="text-xs text-muted-foreground">
                          {s.character_name}
                          {s.last_activity && (
                            <> &middot; {formatRelativeTime(s.last_activity)}</>
                          )}
                        </span>
                      </div>
                    </SidebarMenuButton>
                    <DropdownMenu>
                      <DropdownMenuTrigger
                        className="absolute right-1 top-1.5 rounded p-0.5 opacity-0 transition-opacity hover:bg-accent group-hover/menu-item:opacity-100 data-[state=open]:opacity-100"
                        onClick={(e) => e.stopPropagation()}
                      >
                        <MoreHorizontal className="size-4" />
                      </DropdownMenuTrigger>
                      <DropdownMenuContent side="right" align="start">
                        <DropdownMenuItem
                          className="text-destructive focus:text-destructive"
                          onClick={(e) => {
                            e.stopPropagation();
                            handleDeleteSession(s.id);
                          }}
                        >
                          <Trash2 className="mr-2 size-4" />
                          削除
                        </DropdownMenuItem>
                      </DropdownMenuContent>
                    </DropdownMenu>
                  </SidebarMenuItem>
                );
              })}
          </SidebarMenu>
        </SidebarGroupContent>
      </SidebarGroup>
    </>
  );
}

// ─── タスク用サイドバー ───
function TaskSidebar() {
  const { projects } = useProject();
  const [tasks, setTasks] = useState<Task[]>([]);
  const [loading, setLoading] = useState(true);
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [openMenuTaskId, setOpenMenuTaskId] = useState<string | null>(null);
  const hasLoadedTasksRef = useRef(false);
  const [viewState, setViewState] = useState<TaskSidebarViewState>(() =>
    readTaskSidebarViewState(),
  );
  const contextMenu = useTaskContextMenu();

  const fetchTasks = useCallback((options: { forceLoading?: boolean } = {}) => {
    const shouldShowLoading =
      options.forceLoading ?? !hasLoadedTasksRef.current;
    if (shouldShowLoading) setLoading(true);
    taskApi
      .listTasks()
      .then((data) => {
        setTasks(data);
      })
      .catch(() => {})
      .finally(() => {
        hasLoadedTasksRef.current = true;
        setLoading(false);
      });
  }, []);

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

  const updateTaskStatus = useCallback(async (task: Task, status: string) => {
    if (task.status === status) return;
    try {
      await taskApi.updateTask(task.id, { status });
      setTasks((prev) =>
        prev.map((t) => (t.id === task.id ? { ...t, status } : t)),
      );
    } catch {}
  }, []);

  useEffect(() => {
    if (!openMenuTaskId) return;

    const handleKeyDown = (e: KeyboardEvent) => {
      const target = STATUS_SHORTCUT_KEYS[e.key.toLowerCase()];
      if (!target) return;

      e.preventDefault();
      e.stopPropagation();
      e.stopImmediatePropagation();

      const task = tasks.find((item) => item.id === openMenuTaskId);
      setOpenMenuTaskId(null);
      if (task) {
        void updateTaskStatus(task, target);
      }
    };

    window.addEventListener("keydown", handleKeyDown, true);
    return () => window.removeEventListener("keydown", handleKeyDown, true);
  }, [openMenuTaskId, tasks, updateTaskStatus]);

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
                                onKeyDown={async (e) => {
                                  const target =
                                    STATUS_SHORTCUT_KEYS[e.key.toLowerCase()];
                                  if (target) {
                                    e.preventDefault();
                                    setOpenMenuTaskId(null);
                                    await updateTaskStatus(task, target);
                                  }
                                }}
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
                                    <kbd className="text-[10px] text-muted-foreground opacity-60">
                                      {
                                        {
                                          closed: "C",
                                          in_progress: "S",
                                          review: "R",
                                          on_hold: "H",
                                          open: "X",
                                        }[status]
                                      }
                                    </kbd>
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

// ─── ファイラー用サイドバー ───

// ファイルタイプ判定
const AUDIO_EXTS = ["mp3", "wav", "ogg", "flac", "aac", "m4a", "opus", "wma"];
const IMAGE_EXTS = ["jpg", "jpeg", "png", "gif", "webp", "svg", "bmp"];
const VIDEO_EXTS = ["mp4", "webm", "mov", "avi", "mkv"];
function getFileExt(name: string): string {
  return (name.split(".").pop() || "").toLowerCase();
}

// ファイル配信URL（絶対パス→ファイラーAPI、相対パス→エクスプローラーAPI）
function getFilerFileUrl(filePath: string) {
  if (isAbsolutePath(filePath)) {
    return `/api/python-proxy/filer/file?path=${encodeURIComponent(filePath)}`;
  }
  return `/api/python-proxy/explorer/serve?path=${encodeURIComponent(filePath)}`;
}

// 絶対パス判定
function isAbsolutePath(p: string): boolean {
  if (!p) return false;
  if (/^[A-Za-z]:[\\/]/.test(p)) return true;
  if (p.startsWith("/")) return true;
  return false;
}

type FilerTab = "workspace" | "user";

function FilerSidebar() {
  const { selectedProjectId } = useProject();
  const audioPlayer = useAudioPlayer();
  const router = useRouter();

  const [filerTab, setFilerTab] = useState<FilerTab>("workspace");
  const [currentPath, setCurrentPath] = useState("");
  const [browseData, setBrowseData] = useState<ExplorerListResponse | null>(
    null,
  );
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [userId, setUserId] = useState<string | null>(null);
  const [isAbsoluteFilerPath, setIsAbsoluteFilerPath] = useState(false);
  const [isDragging, setIsDragging] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [viewerFile, setViewerFile] = useState<ExplorerFile | null>(null);
  const [selectedItems, setSelectedItems] = useState<Set<string>>(new Set());
  const [clipboard, setClipboard] = useState<{
    paths: string[];
    operation: "copy" | "cut";
  } | null>(null);

  const initDoneRef = useRef(false);
  const containerRef = useRef<HTMLDivElement>(null);

  // コンテキストルートパス
  const contextRootPath = useMemo(() => {
    if (isAbsoluteFilerPath) return "";
    if (filerTab === "workspace" && selectedProjectId) {
      return `_projects/project_${selectedProjectId}`;
    }
    if (filerTab === "user" && userId) {
      return `_users/user_${userId}`;
    }
    return "";
  }, [filerTab, selectedProjectId, userId, isAbsoluteFilerPath]);
  const itemByPath = useMemo(() => {
    const entries = [
      ...(browseData?.directories ?? []),
      ...(browseData?.files ?? []),
    ] as Array<ExplorerDirectory | ExplorerFile>;
    return new Map(entries.map((entry) => [entry.path, entry]));
  }, [browseData]);
  const selectedPaths = useMemo(
    () => Array.from(selectedItems).filter((path) => itemByPath.has(path)),
    [itemByPath, selectedItems],
  );
  const selectedRegularPaths = useMemo(
    () =>
      selectedPaths.filter((path) => {
        const item = itemByPath.get(path);
        return !item || !("type" in item) || !isRecordTableFile(item);
      }),
    [itemByPath, selectedPaths],
  );
  const canUseFileShortcuts = !isAbsoluteFilerPath;

  // ディレクトリ読み込み
  const fetchDirectory = useCallback(
    async (path: string) => {
      setLoading(true);
      setError(null);
      const useAbsoluteFilerPath = isAbsolutePath(path);
      try {
        if (useAbsoluteFilerPath) {
          const data = await filerBrowse(path);
          setIsAbsoluteFilerPath(true);
          setBrowseData({
            success: true,
            current_path: data.current_path,
            parent_path: data.parent_path,
            can_go_up: data.can_go_up,
            directories: data.folders.map((f) => ({
              name: f.name,
              path: f.path,
              item_count: f.item_count,
            })),
            files: data.files.map((f) => ({
              name: f.name,
              path: f.path,
              type: f.type,
              size: f.size,
            })),
            total_items: data.folders.length + data.files.length,
          });
          setCurrentPath(data.current_path);
        } else {
          const data = await explorerList(path);
          let nextData = data;
          if (
            selectedProjectId &&
            data.current_path === `_projects/project_${selectedProjectId}`
          ) {
            const records = await listProjectRecordTables(selectedProjectId);
            const recordFiles = records.tables.map((table) =>
              recordTableToExplorerFile(selectedProjectId, table),
            );
            nextData = {
              ...data,
              files: [...recordFiles, ...data.files],
              total_items:
                data.directories.length +
                recordFiles.length +
                data.files.length,
            };
          }
          setIsAbsoluteFilerPath(false);
          setBrowseData(nextData);
          setCurrentPath(data.current_path);
        }
        setSelectedItems(new Set());
      } catch {
        setError("読み込みに失敗しました");
      } finally {
        setLoading(false);
      }
    },
    [selectedProjectId],
  );

  const navigate = useCallback(
    (path: string) => {
      fetchDirectory(path);
    },
    [fetchDirectory],
  );

  const goUp = useCallback(() => {
    if (!isAbsoluteFilerPath && contextRootPath && currentPath === contextRootPath)
      return;
    if (browseData?.parent_path != null) navigate(browseData.parent_path);
  }, [browseData, navigate, currentPath, contextRootPath, isAbsoluteFilerPath]);

  const goHome = useCallback(() => {
    navigate(contextRootPath || "");
  }, [navigate, contextRootPath]);

  // タブ切り替え
  const handleSetFilerTab = useCallback(
    (tab: FilerTab) => {
      setFilerTab(tab);
      setIsAbsoluteFilerPath(false);
      if (tab === "workspace" && selectedProjectId) {
        fetchDirectory(`_projects/project_${selectedProjectId}`);
      } else if (tab === "user" && userId) {
        fetchDirectory(`_users/user_${userId}`);
      }
    },
    [fetchDirectory, selectedProjectId, userId],
  );

  const toggleSelect = useCallback((path: string) => {
    setSelectedItems((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  }, []);

  const selectAll = useCallback(() => {
    if (!browseData) return;
    setSelectedItems(
      new Set([
        ...browseData.directories.map((dir) => dir.path),
        ...browseData.files.map((file) => file.path),
      ]),
    );
  }, [browseData]);

  const handleDirectoryClick = useCallback(
    (e: MouseEvent<HTMLButtonElement>, dir: ExplorerDirectory) => {
      if (e.ctrlKey || e.metaKey) {
        toggleSelect(dir.path);
        return;
      }
      navigate(dir.path);
    },
    [navigate, toggleSelect],
  );

  // ファイルクリック
  const handleFileClick = useCallback(
    (file: ExplorerFile) => {
      if (isRecordTableFile(file) && file.project_id && file.record_table_id) {
        const params = new URLSearchParams({
          recordProject: file.project_id,
          recordTable: file.record_table_id,
          recordName: file.name,
        });
        router.push(`/filer?${params.toString()}`);
        return;
      }
      const ext = getFileExt(file.name);
      if (AUDIO_EXTS.includes(ext)) {
        const audioFiles = (browseData?.files ?? [])
          .filter((f) => AUDIO_EXTS.includes(getFileExt(f.name)))
          .map((f) => ({
            name: f.name,
            path: f.path,
            type: f.type || "audio",
            rootPath: isAbsoluteFilerPath ? currentPath : contextRootPath || "",
            sourceKind: isAbsoluteFilerPath ? "filer" as const : "explorer" as const,
          }));
        audioPlayer.play(
          {
            name: file.name,
            path: file.path,
            type: file.type || "audio",
            rootPath: isAbsoluteFilerPath ? currentPath : contextRootPath || "",
            sourceKind: isAbsoluteFilerPath ? "filer" : "explorer",
          },
          audioFiles,
        );
      } else if (IMAGE_EXTS.includes(ext) || VIDEO_EXTS.includes(ext)) {
        setViewerFile(file);
      }
    },
    [audioPlayer, browseData, contextRootPath, currentPath, isAbsoluteFilerPath, router],
  );

  // D&D アップロード
  const handleFileButtonClick = useCallback(
    (e: MouseEvent<HTMLButtonElement>, file: ExplorerFile) => {
      if (e.ctrlKey || e.metaKey) {
        toggleSelect(file.path);
        return;
      }
      handleFileClick(file);
    },
    [handleFileClick, toggleSelect],
  );

  const copySelectedItems = useCallback(
    (operation: "copy" | "cut") => {
      if (selectedRegularPaths.length === 0) return;
      setClipboard({ paths: selectedRegularPaths, operation });
    },
    [selectedRegularPaths],
  );

  const pasteClipboardItems = useCallback(async () => {
    if (!clipboard || !canUseFileShortcuts) return;
    for (const src of clipboard.paths) {
      if (clipboard.operation === "cut") {
        await explorerMove(src, currentPath);
      } else {
        await explorerCopy(src, currentPath);
      }
    }
    if (clipboard.operation === "cut") setClipboard(null);
    setSelectedItems(new Set());
    await fetchDirectory(currentPath);
  }, [canUseFileShortcuts, clipboard, currentPath, fetchDirectory]);

  const deleteSelectedItems = useCallback(async () => {
    if (!canUseFileShortcuts || selectedPaths.length === 0) return;
    for (const path of selectedPaths) {
      const item = itemByPath.get(path);
      if (item && "type" in item && isRecordTableFile(item)) {
        if (item.project_id && item.record_table_id) {
          await deleteProjectRecordTable(item.project_id, item.record_table_id);
        }
      } else {
        await explorerDelete(path);
      }
    }
    setSelectedItems(new Set());
    await fetchDirectory(currentPath);
  }, [
    canUseFileShortcuts,
    currentPath,
    fetchDirectory,
    itemByPath,
    selectedPaths,
  ]);

  useEffect(() => {
    const isTextInput = (target: EventTarget | null) => {
      if (!(target instanceof HTMLElement)) return false;
      return (
        target.tagName === "INPUT" ||
        target.tagName === "TEXTAREA" ||
        target.isContentEditable
      );
    };
    const handleKeyDown = (e: KeyboardEvent) => {
      if (!containerRef.current?.contains(document.activeElement)) return;
      if (viewerFile || isTextInput(e.target)) return;
      const key = e.key.toLowerCase();
      const primaryModifier = e.ctrlKey || e.metaKey;

      if (primaryModifier && key === "a") {
        e.preventDefault();
        selectAll();
        return;
      }
      if (primaryModifier && key === "c") {
        e.preventDefault();
        if (canUseFileShortcuts) copySelectedItems("copy");
        return;
      }
      if (primaryModifier && key === "x") {
        e.preventDefault();
        if (canUseFileShortcuts) copySelectedItems("cut");
        return;
      }
      if (primaryModifier && key === "v") {
        e.preventDefault();
        void pasteClipboardItems();
        return;
      }
      if (
        canUseFileShortcuts &&
        selectedPaths.length > 0 &&
        (e.key === "Delete" || e.key === "Backspace")
      ) {
        e.preventDefault();
        void deleteSelectedItems();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [
    canUseFileShortcuts,
    copySelectedItems,
    deleteSelectedItems,
    pasteClipboardItems,
    selectAll,
    selectedPaths.length,
    viewerFile,
  ]);

  const handleDragOver = useCallback((e: React.DragEvent) => {
    if (e.dataTransfer.types.includes("application/x-explorer-paths")) return;
    e.preventDefault();
    e.stopPropagation();
    setIsDragging(true);
  }, []);
  const handleDragLeave = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragging(false);
  }, []);
  const handleDrop = useCallback(
    async (e: React.DragEvent) => {
      if (e.dataTransfer.types.includes("application/x-explorer-paths")) return;
      e.preventDefault();
      e.stopPropagation();
      setIsDragging(false);
      const files = await getDroppedExplorerFiles(e.dataTransfer);
      if (!files || files.length === 0) return;
      setUploading(true);
      try {
        const result = await explorerUpload(currentPath, files);
        toast.success(`${result.successCount}件アップロードしました`);
        await fetchDirectory(currentPath);
      } catch (error) {
        if (error instanceof ExplorerUploadError) {
          const { successCount, failureCount } = error.batchResult;
          if (successCount > 0) await fetchDirectory(currentPath);
          toast.error(
            successCount > 0
              ? `${successCount}件アップロード、${failureCount}件失敗しました`
              : error.message,
          );
        } else {
          toast.error("アップロードに失敗しました");
        }
      } finally {
        setUploading(false);
      }
    },
    [currentPath, fetchDirectory],
  );

  // 初期化
  useEffect(() => {
    (async () => {
      try {
        const res = await fetch("/api/auth/status", { credentials: "include" });
        if (res.ok) {
          const data = await res.json();
          if (data.authenticated && data.user) {
            setUserId(data.user.id);
          }
        }
      } catch {
        /* ignore */
      }
    })();
  }, []);

  // ユーザーIDまたはプロジェクトIDが揃ったら初期表示
  useEffect(() => {
    if (initDoneRef.current) return;
    if (filerTab === "workspace" && selectedProjectId) {
      fetchDirectory(`_projects/project_${selectedProjectId}`);
      initDoneRef.current = true;
    } else if (filerTab === "user" && userId) {
      fetchDirectory(`_users/user_${userId}`);
      initDoneRef.current = true;
    }
  }, [selectedProjectId, userId, filerTab, fetchDirectory]);

  // プロジェクト切り替え時
  useEffect(() => {
    if (!initDoneRef.current || !selectedProjectId) return;
    if (filerTab === "workspace" && !isAbsoluteFilerPath) {
      fetchDirectory(`_projects/project_${selectedProjectId}`);
    }
  }, [selectedProjectId]); // eslint-disable-line react-hooks/exhaustive-deps

  // パンくず
  const breadcrumbs = useMemo(() => {
    if (!currentPath) return [];
    if (isAbsoluteFilerPath) return currentPath.split(/[/\\]/).filter(Boolean);
    if (contextRootPath && currentPath.startsWith(contextRootPath)) {
      const rel = currentPath
        .slice(contextRootPath.length)
        .replace(/^[/\\]/, "");
      return rel ? rel.split(/[/\\]/).filter(Boolean) : [];
    }
    return currentPath.split(/[/\\]/).filter(Boolean);
  }, [currentPath, contextRootPath, isAbsoluteFilerPath]);

  // ファイルアイコン
  const fileIcon = (file: ExplorerFile) => {
    if (isRecordTableFile(file)) {
      return <Table2 className="size-4 shrink-0 text-emerald-500" />;
    }
    const ext = getFileExt(file.name);
    if (VIDEO_EXTS.includes(ext))
      return <Film className="size-4 shrink-0 text-purple-500" />;
    if (AUDIO_EXTS.includes(ext))
      return <Music className="size-4 shrink-0 text-orange-500" />;
    if (IMAGE_EXTS.includes(ext))
      return <FileIcon className="size-4 shrink-0 text-blue-500" />;
    return <FileIcon className="size-4 shrink-0 text-muted-foreground" />;
  };

  return (
    <>
      <SidebarGroup ref={containerRef}>
        {/* タブ切り替え */}
        <div className="flex items-center gap-1 px-2 pb-1">
          <button
            onClick={() => handleSetFilerTab("workspace")}
            className={cn(
              "flex-1 rounded px-2 py-1 text-xs font-medium transition-colors",
              filerTab === "workspace" && !isAbsoluteFilerPath
                ? "bg-primary text-primary-foreground"
                : "hover:bg-accent",
            )}
          >
            ワークスペース
          </button>
          <button
            onClick={() => handleSetFilerTab("user")}
            className={cn(
              "flex-1 rounded px-2 py-1 text-xs font-medium transition-colors",
              filerTab === "user" && !isAbsoluteFilerPath
                ? "bg-primary text-primary-foreground"
                : "hover:bg-accent",
            )}
          >
            ユーザー
          </button>
        </div>

        {/* パンくず + ナビゲーション */}
        <div className="flex items-center gap-0.5 px-2 pb-1 text-xs text-muted-foreground flex-wrap">
          <button
            onClick={goHome}
            className="p-0.5 rounded hover:bg-accent"
            title="ホーム"
          >
            <Home className="size-3" />
          </button>
          {browseData?.can_go_up && browseData.parent_path !== null && (
            <button
              onClick={goUp}
              className="p-0.5 rounded hover:bg-accent"
              title="上のフォルダへ"
            >
              <ArrowUp className="size-3" />
            </button>
          )}
          {breadcrumbs.slice(-3).map((segment, i) => (
            <span key={i} className="flex items-center gap-0.5">
              <ChevronRight className="size-2.5" />
              <span className="truncate max-w-[80px]">{segment}</span>
            </span>
          ))}
        </div>

        <SidebarGroupContent>
          {/* D&D ゾーン */}
          <div
            className="relative"
            onDragOver={handleDragOver}
            onDragLeave={handleDragLeave}
            onDrop={handleDrop}
          >
            {/* ローディング */}
            {loading && (
              <div className="px-4 py-6 text-center text-xs text-muted-foreground">
                読み込み中...
              </div>
            )}

            {/* エラー */}
            {error && !loading && (
              <div className="px-4 py-3 text-center text-xs text-destructive">
                {error}
              </div>
            )}

            {/* フォルダ・ファイル一覧 */}
            {!loading && browseData && (
              <SidebarMenu>
                {/* フォルダ */}
                {browseData.directories.map((dir) => (
                  <SidebarMenuItem key={dir.path}>
                    <SidebarMenuButton
                      className={cn(
                        selectedItems.has(dir.path) &&
                          "bg-accent text-accent-foreground",
                      )}
                      onClick={(e) => handleDirectoryClick(e, dir)}
                    >
                      <Folder className="size-4 shrink-0 text-yellow-500" />
                      <span className="truncate text-sm">{dir.name}</span>
                      {dir.item_count !== undefined && (
                        <span className="ml-auto text-[10px] text-muted-foreground tabular-nums">
                          {dir.item_count}
                        </span>
                      )}
                    </SidebarMenuButton>
                  </SidebarMenuItem>
                ))}
                {/* ファイル */}
                {browseData.files.map((file) => (
                  <SidebarMenuItem key={file.path}>
                    <SidebarMenuButton
                      className={cn(
                        selectedItems.has(file.path) &&
                          "bg-accent text-accent-foreground",
                      )}
                      onClick={(e) => handleFileButtonClick(e, file)}
                    >
                      {fileIcon(file)}
                      <span className="truncate text-sm">{file.name}</span>
                    </SidebarMenuButton>
                  </SidebarMenuItem>
                ))}
                {/* 空フォルダ */}
                {browseData.directories.length === 0 &&
                  browseData.files.length === 0 && (
                    <li className="px-4 py-6 text-center text-xs text-muted-foreground">
                      空のフォルダです。ファイルをドラッグ&ドロップでアップロードできます。
                    </li>
                  )}
              </SidebarMenu>
            )}

            {/* D&Dオーバーレイ */}
            {isDragging && (
              <div className="absolute inset-0 z-40 flex items-center justify-center rounded border-2 border-dashed border-blue-400 bg-blue-500/10">
                <div className="flex flex-col items-center gap-1 text-blue-500">
                  <Upload className="size-5" />
                  <span className="text-xs font-medium">
                    ドロップしてアップロード
                  </span>
                </div>
              </div>
            )}
            {uploading && (
              <div className="absolute inset-0 z-40 flex items-center justify-center bg-background/60">
                <div className="flex items-center gap-2 text-xs text-muted-foreground">
                  <div className="size-3 animate-spin rounded-full border-2 border-primary border-t-transparent" />
                  アップロード中...
                </div>
              </div>
            )}
          </div>
        </SidebarGroupContent>
      </SidebarGroup>

      {/* 画像/動画ビューア */}
      {viewerFile && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/90"
          onClick={() => setViewerFile(null)}
        >
          <button
            className="absolute top-4 right-4 z-50 text-white p-2 rounded hover:bg-white/20"
            onClick={() => setViewerFile(null)}
          >
            <X className="size-6" />
          </button>
          <div className="absolute top-4 left-4 z-50 text-white text-sm bg-black/50 px-3 py-1.5 rounded">
            {viewerFile.name}
          </div>
          <div
            className="max-w-[98vw] max-h-[96vh] flex items-center justify-center"
            onClick={(e) => e.stopPropagation()}
          >
            {IMAGE_EXTS.includes(getFileExt(viewerFile.name)) && (
              <img
                src={getFilerFileUrl(viewerFile.path)}
                alt={viewerFile.name}
                className="max-w-[98vw] max-h-[96vh] object-contain"
              />
            )}
            {VIDEO_EXTS.includes(getFileExt(viewerFile.name)) && (
              <video
                src={getFilerFileUrl(viewerFile.path)}
                controls
                autoPlay
                className="max-w-[98vw] max-h-[96vh]"
              >
                <track kind="captions" />
              </video>
            )}
          </div>
        </div>
      )}
    </>
  );
}

// ─── 通知 ───
const OS_NOTIFICATION_SEEN_KEY = "aoitalk-os-notification-seen";
const OS_NOTIFICATION_SEEN_LIMIT = 200;
const OS_NOTIFICATION_STALE_MS = 24 * 60 * 60 * 1000;
const NOTIFICATION_SERVICE_WORKER_URL = "/aoitalk-notifications-sw.js";

type OsNotificationPermission = NotificationPermission | "unsupported";

interface InAppNotification {
  id: string;
  type: string;
  title: string;
  message?: string | null;
  task_id?: string | null;
  is_read: boolean;
  created_at: string;
  delivered_at?: string | null;
}

function notificationTimestamp(notification: InAppNotification): number {
  const raw = notification.delivered_at || notification.created_at;
  const time = new Date(raw).getTime();
  return Number.isFinite(time) ? time : Date.now();
}

async function getNotificationServiceWorkerRegistration() {
  if (typeof navigator === "undefined" || !("serviceWorker" in navigator)) {
    return null;
  }
  try {
    return await navigator.serviceWorker.register(
      NOTIFICATION_SERVICE_WORKER_URL,
      { scope: "/" },
    );
  } catch {
    return null;
  }
}

function NotificationPanel() {
  const router = useRouter();
  const [notifications, setNotifications] = useState<InAppNotification[]>([]);
  const [loading, setLoading] = useState(false);
  const [open, setOpen] = useState(false);
  const [osNotificationPermission, setOsNotificationPermission] =
    useState<OsNotificationPermission>("unsupported");
  const osNotificationSeenIdsRef = useRef<Set<string>>(new Set());
  const osNotificationInitializedRef = useRef(false);

  const notificationTypeLabel = useCallback((type: string) => {
    switch (type) {
      case "reminder":
        return "リマインダー";
      case "due_soon":
        return "期限間近";
      case "overdue":
        return "期限超過";
      case "assigned":
        return "アサイン";
      case "comment":
        return "コメント";
      default:
        return type;
    }
  }, []);

  const persistOsNotificationSeenIds = useCallback(() => {
    if (typeof window === "undefined") return;
    const ids = Array.from(osNotificationSeenIdsRef.current).slice(
      -OS_NOTIFICATION_SEEN_LIMIT,
    );
    osNotificationSeenIdsRef.current = new Set(ids);
    window.localStorage.setItem(OS_NOTIFICATION_SEEN_KEY, JSON.stringify(ids));
  }, []);

  const markAsRead = useCallback(async (id: string) => {
    try {
      await fetch(`/api/notifications/${id}/read`, {
        method: "POST",
        credentials: "include",
      });
      setNotifications((prev) =>
        prev.map((n) => (n.id === id ? { ...n, is_read: true } : n)),
      );
    } catch {
      // エラー時は何もしない
    }
  }, []);

  const markAllAsRead = useCallback(async () => {
    const unreadIds = notifications.filter((n) => !n.is_read).map((n) => n.id);
    if (unreadIds.length === 0) return;

    setNotifications((prev) => prev.map((n) => ({ ...n, is_read: true })));
    try {
      const res = await fetch("/api/notifications/read-all", {
        method: "POST",
        credentials: "include",
      });
      if (!res.ok) {
        setNotifications((prev) =>
          prev.map((n) =>
            unreadIds.includes(n.id) ? { ...n, is_read: false } : n,
          ),
        );
      }
    } catch {
      setNotifications((prev) =>
        prev.map((n) =>
          unreadIds.includes(n.id) ? { ...n, is_read: false } : n,
        ),
      );
    }
  }, [notifications]);

  const handleNotificationClick = useCallback(
    async (notification: InAppNotification) => {
      if (!notification.is_read) {
        await markAsRead(notification.id);
      }
      if (notification.task_id) {
        setOpen(false);
        router.push(`/tasks/${notification.task_id}`);
      }
    },
    [markAsRead, router],
  );

  const showOsNotification = useCallback(
    async (notification: InAppNotification) => {
      if (
        typeof window === "undefined" ||
        !("Notification" in window) ||
        window.Notification.permission !== "granted"
      ) {
        return;
      }

      const url = notification.task_id
        ? `/tasks/${notification.task_id}`
        : "/";
      const options: NotificationOptions = {
        body: notification.message || notificationTypeLabel(notification.type),
        tag: `aoitalk-${notification.id}`,
        data: {
          url,
          notificationId: notification.id,
        },
        icon: "/favicon.ico",
        requireInteraction: true,
      };

      const registration = await getNotificationServiceWorkerRegistration();
      if (registration) {
        try {
          await registration.showNotification(notification.title, options);
          return;
        } catch {
          // Fall back to the page-level Notification API below.
        }
      }

      const osNotification = new window.Notification(
        notification.title,
        options,
      );
      osNotification.onclick = () => {
        window.focus();
        void markAsRead(notification.id);
        if (notification.task_id) {
          setOpen(false);
          router.push(`/tasks/${notification.task_id}`);
        }
      };
    },
    [markAsRead, notificationTypeLabel, router],
  );

  const syncOsNotifications = useCallback(
    (nextNotifications: InAppNotification[]) => {
      if (typeof window === "undefined") return;

      if (!osNotificationInitializedRef.current) {
        const staleBefore = Date.now() - OS_NOTIFICATION_STALE_MS;
        nextNotifications.forEach((notification) => {
          if (notificationTimestamp(notification) < staleBefore) {
            osNotificationSeenIdsRef.current.add(notification.id);
          }
        });
        osNotificationInitializedRef.current = true;
        persistOsNotificationSeenIds();
      }

      if (
        !("Notification" in window) ||
        window.Notification.permission !== "granted"
      ) {
        return;
      }

      const unreadNewNotifications = nextNotifications
        .filter(
          (notification) =>
            !notification.is_read &&
            !osNotificationSeenIdsRef.current.has(notification.id),
        )
        .sort(
          (a, b) =>
            new Date(a.created_at).getTime() - new Date(b.created_at).getTime(),
        );

      unreadNewNotifications.forEach((notification) => {
        osNotificationSeenIdsRef.current.add(notification.id);
      });
      if (unreadNewNotifications.length === 0) return;
      persistOsNotificationSeenIds();

      unreadNewNotifications.slice(-3).forEach((notification) => {
        void showOsNotification(notification);
      });
    },
    [persistOsNotificationSeenIds, showOsNotification],
  );

  const requestOsNotificationPermission = useCallback(async () => {
    if (typeof window === "undefined" || !("Notification" in window)) {
      setOsNotificationPermission("unsupported");
      return;
    }
    const permission = await window.Notification.requestPermission();
    setOsNotificationPermission(permission);
    if (permission === "granted") {
      await getNotificationServiceWorkerRegistration();
      syncOsNotifications(notifications);
    }
  }, [notifications, syncOsNotifications]);

  const sendTestOsNotification = useCallback(async () => {
    if (typeof window === "undefined" || !("Notification" in window)) return;
    if (window.Notification.permission !== "granted") {
      await requestOsNotificationPermission();
      return;
    }
    await showOsNotification({
      id: `test-${Date.now()}`,
      type: "reminder",
      title: "AoiTalk notification test",
      message: "ブラウザのOS通知は有効です。",
      task_id: null,
      is_read: false,
      created_at: new Date().toISOString(),
    });
  }, [requestOsNotificationPermission, showOsNotification]);

  useEffect(() => {
    if (typeof window === "undefined" || !("Notification" in window)) {
      setOsNotificationPermission("unsupported");
      return;
    }

    setOsNotificationPermission(window.Notification.permission);
    void getNotificationServiceWorkerRegistration();
    try {
      const stored = window.localStorage.getItem(OS_NOTIFICATION_SEEN_KEY);
      const parsed = stored ? JSON.parse(stored) : [];
      if (Array.isArray(parsed)) {
        osNotificationSeenIdsRef.current = new Set(
          parsed.filter((id): id is string => typeof id === "string"),
        );
      }
    } catch {
      osNotificationSeenIdsRef.current = new Set();
    }
  }, []);

  const fetchNotifications = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch("/api/notifications", {
        credentials: "include",
        signal: AbortSignal.timeout(5000),
      });
      if (res.ok) {
        const data = await res.json();
        const nextNotifications = Array.isArray(data)
          ? data
          : (data.notifications ?? []);
        setNotifications(nextNotifications);
        syncOsNotifications(nextNotifications);
      }
    } catch {
      // エラー時は何もしない
    } finally {
      setLoading(false);
    }
  }, [syncOsNotifications]);

  // 初回取得 + 30秒ポーリング
  useEffect(() => {
    fetchNotifications();
    const interval = setInterval(fetchNotifications, 30000);
    return () => clearInterval(interval);
  }, [fetchNotifications]);

  const unreadCount = notifications.filter((n) => !n.is_read).length;

  return (
    <SidebarGroup>
      <div className="flex items-center justify-between px-2">
        <button
          onClick={() => setOpen(!open)}
          className="flex items-center gap-1.5 px-1 py-1 rounded hover:bg-accent transition-colors"
          title="通知"
        >
          <div className="relative">
            <Bell className="size-4" />
            {unreadCount > 0 && (
              <span className="absolute -top-1.5 -right-1.5 flex size-4 items-center justify-center rounded-full bg-red-500 text-[10px] font-bold text-white">
                {unreadCount > 9 ? "9+" : unreadCount}
              </span>
            )}
          </div>
          <SidebarGroupLabel className="p-0">通知</SidebarGroupLabel>
        </button>
        {open && (
          <div className="flex items-center gap-0.5">
            <button
              type="button"
              onClick={() => void markAllAsRead()}
              disabled={unreadCount === 0}
              className="p-1 rounded hover:bg-accent disabled:cursor-not-allowed disabled:opacity-40"
              title="すべて確認済みにする"
            >
              <CheckCheck className="size-3.5" />
            </button>
            {osNotificationPermission !== "unsupported" &&
              osNotificationPermission !== "denied" && (
                <button
                  type="button"
                  onClick={() =>
                    void (osNotificationPermission === "granted"
                      ? sendTestOsNotification()
                      : requestOsNotificationPermission())
                  }
                  className="p-1 rounded hover:bg-accent"
                  title={
                    osNotificationPermission === "granted"
                      ? "OS通知をテスト"
                      : "OS通知を許可"
                  }
                >
                  <Bell className="size-3.5" />
                </button>
              )}
            <button
              onClick={() => setOpen(false)}
              className="p-1 rounded hover:bg-accent"
              title="閉じる"
            >
              <X className="size-3.5" />
            </button>
          </div>
        )}
      </div>
      {open && (
        <SidebarGroupContent>
          {osNotificationPermission === "denied" && (
            <div className="px-3 py-2 text-xs text-muted-foreground">
              ブラウザでOS通知がブロックされています
            </div>
          )}
          {loading && notifications.length === 0 && (
            <div className="px-4 py-3 text-center text-xs text-muted-foreground">
              読み込み中...
            </div>
          )}
          {!loading && notifications.length === 0 && (
            <div className="px-4 py-3 text-center text-xs text-muted-foreground">
              通知はありません
            </div>
          )}
          <div className="max-h-[300px] overflow-y-auto">
            {notifications.map((n) => (
              <button
                key={n.id}
                onClick={() => void handleNotificationClick(n)}
                className={`w-full text-left px-3 py-2 border-b border-border/50 hover:bg-accent/50 transition-colors ${
                  n.is_read ? "opacity-60" : ""
                }`}
              >
                <div className="flex items-start gap-2">
                  {!n.is_read && (
                    <span className="mt-1.5 inline-block size-2 shrink-0 rounded-full bg-blue-500" />
                  )}
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-1.5">
                      <span className="text-[10px] font-medium text-muted-foreground uppercase">
                        {notificationTypeLabel(n.type)}
                      </span>
                    </div>
                    <p className="truncate text-sm font-medium">{n.title}</p>
                    {n.message && (
                      <p className="truncate text-xs text-muted-foreground">
                        {n.message}
                      </p>
                    )}
                    <span className="text-[10px] text-muted-foreground">
                      {formatRelativeTime(n.created_at)}
                    </span>
                  </div>
                </div>
              </button>
            ))}
          </div>
        </SidebarGroupContent>
      )}
    </SidebarGroup>
  );
}

// ─── モバイル用スペース/プロジェクト選択 ───
const mobileSelectClassName =
  "h-8 w-full rounded-lg border border-input bg-white/45 px-2 text-sm text-foreground outline-none backdrop-blur-xl focus-visible:border-ring dark:bg-input/30";

function MobileContextSwitcher() {
  const {
    spaces,
    selectedSpaceId,
    setSelectedSpaceId,
    projects,
    selectedProjectId,
    setSelectedProjectId,
  } = useProject();

  if (spaces.length === 0 && projects.length === 0) return null;

  return (
    <div className="flex flex-col gap-2 px-2 pb-2 md:hidden">
      {spaces.length > 0 && (
        <div className="flex items-center gap-2">
          <Layers className="size-4 shrink-0 text-muted-foreground" />
          <select
            value={selectedSpaceId ?? ""}
            onChange={(e) => setSelectedSpaceId(e.target.value)}
            className={mobileSelectClassName}
            aria-label="スペース選択"
          >
            {spaces.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </select>
        </div>
      )}
      {projects.length > 0 && (
        <div className="flex items-center gap-2">
          <FolderOpen className="size-4 shrink-0 text-muted-foreground" />
          <select
            value={selectedProjectId ?? ""}
            onChange={(e) => setSelectedProjectId(e.target.value)}
            className={mobileSelectClassName}
            aria-label="プロジェクト選択"
          >
            {projects.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        </div>
      )}
    </div>
  );
}

// ─── メインサイドバー ───
function AppSidebarInner() {
  // サイドバーのタブ状態をlocalStorageで永続化（メイン画面のルートとは独立）
  const [sidebarTab, setSidebarTab] = useState<"chat" | "tasks" | "filer">(
    () => {
      if (typeof window !== "undefined") {
        const saved = localStorage.getItem("aoitalk-sidebar-tab");
        if (saved === "chat" || saved === "tasks" || saved === "filer")
          return saved;
      }
      return "chat";
    },
  );

  const handleSetSidebarTab = useCallback((tab: "chat" | "tasks" | "filer") => {
    setSidebarTab(tab);
    localStorage.setItem("aoitalk-sidebar-tab", tab);
  }, []);

  const handleBrandClick = useCallback(() => {
    setSidebarTab("chat");
    localStorage.setItem("aoitalk-sidebar-tab", "chat");
    localStorage.removeItem("aoitalk_last_session_id");
    if (!navigateChatSessionInPlace("/chat")) {
      window.location.href = "/chat";
    }
  }, []);

  return (
    <Sidebar>
      <SidebarHeader className="ao-sidebar-hero justify-center">
        <button
          type="button"
          onClick={handleBrandClick}
          className="flex min-w-0 items-center gap-2 rounded-md px-2 py-2 text-left transition-colors hover:bg-white/48 focus-visible:ring-2 focus-visible:ring-sidebar-ring focus-visible:outline-none dark:hover:bg-white/8"
          title="新規チャットを開く"
        >
          <img
            src="/images/ui/brand-orb.png"
            alt=""
            className="size-9 shrink-0 rounded-xl object-cover shadow-[0_14px_30px_-22px_rgba(5,90,115,0.9)] ring-1 ring-white/80"
          />
          <div className="min-w-0">
            <span className="block text-lg font-semibold leading-5 tracking-tight">
              AoiTalk
            </span>
            <span className="block truncate text-[11px] font-semibold leading-4 text-sidebar-foreground/58">
              Crystal workspace
            </span>
          </div>
        </button>
        <MobileContextSwitcher />
      </SidebarHeader>
      <SidebarContent>
        {/* 通知パネル */}
        <NotificationPanel />

        {/* サイドバー内タブ切り替え（メイン画面は遷移しない） */}
        <SidebarGroup>
          <SidebarGroupContent>
            <SidebarMenu>
              <SidebarMenuItem>
                <SidebarMenuButton
                  isActive={sidebarTab === "chat"}
                  onClick={() => handleSetSidebarTab("chat")}
                >
                  <MessageSquare className="size-4" />
                  <span>チャット</span>
                </SidebarMenuButton>
              </SidebarMenuItem>
              <SidebarMenuItem>
                <SidebarMenuButton
                  isActive={sidebarTab === "tasks"}
                  onClick={() => handleSetSidebarTab("tasks")}
                >
                  <CheckSquare className="size-4" />
                  <span>タスク</span>
                </SidebarMenuButton>
              </SidebarMenuItem>
              <SidebarMenuItem>
                <SidebarMenuButton
                  isActive={sidebarTab === "filer"}
                  onClick={() => handleSetSidebarTab("filer")}
                >
                  <FolderOpen className="size-4" />
                  <span>ファイラー</span>
                </SidebarMenuButton>
              </SidebarMenuItem>
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>

        {/* タブに応じたサイドバーコンテンツ */}
        {sidebarTab === "chat" && <ChatSidebar />}
        {sidebarTab === "tasks" && <TaskSidebar />}
        {sidebarTab === "filer" && <FilerSidebar />}
      </SidebarContent>
    </Sidebar>
  );
}

export function AppSidebar() {
  return (
    <Suspense>
      <AppSidebarInner />
    </Suspense>
  );
}
