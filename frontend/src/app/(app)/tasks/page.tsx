"use client";

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useSearchParams } from "next/navigation";
import useSWR from "swr";

import { useProject } from "@/contexts/project-context";
import { appsApi } from "@/lib/apps-api";
import {
  taskApi,
  type RecurringOccurrenceContext,
  type Task,
} from "@/lib/task-api";
import {
  applyTaskTimerStart,
  getTaskOccurrenceContext,
} from "@/lib/tasks-page-utils";
import type { FilterTab } from "@/lib/tasks-page-utils";
import {
  EMPTY_FILTER,
  type FilterConfig,
} from "@/components/tasks/task-filter-builder";
import { getTaskNotificationsDefaultEnabled } from "@/lib/user-settings";
import {
  isRemoteTasksCacheScope,
  shouldApplyTaskMutationToCurrentCache,
  useTasksData,
} from "@/components/tasks/hooks/use-tasks-data";
import { useProjectTabs } from "@/components/tasks/hooks/use-project-tabs";
import { useTaskViewPreferences } from "@/components/tasks/hooks/use-task-view-preferences";
import { TaskViewSwitcher } from "@/components/tasks/task-view-switcher";
import { TaskListView } from "@/components/tasks/views/task-list-view";
import { TaskScheduleView } from "@/components/tasks/views/task-schedule-view";
import { TaskDetailModal } from "@/components/tasks/task-detail-modal";
import {
  RemoteTaskDialog,
  type RemoteTaskDialogTarget,
} from "@/components/tasks/remote-task-dialog";
import { TaskProjectTabs } from "@/components/tasks/task-project-tabs";
import { TasksWorkspaceNavigation } from "@/components/tasks/tasks-workspace-navigation";
import { useWorkspaceShellRegistration } from "@/components/layout/shell-context";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import {
  ChevronDown,
  ChevronUp,
  Plus,
  Search,
  SlidersHorizontal,
} from "lucide-react";

export default function TasksPage() {
  const searchParams = useSearchParams();
  const appFilterId = searchParams.get("app_id") || "";
  const appFilterProjectId = searchParams.get("project_id") || "";
  const projectContext = useProject();
  const {
    projects,
    allProjects,
    spaces,
    participatingProjects,
    selectedProjectId,
    selectedProject,
    selectedSpaceId,
    selectedSpace,
    setSelectedSpaceId,
    setSelectedProjectId,
    refreshSpaces,
    refreshProjects,
    initialLoadComplete,
  } = projectContext;

  useEffect(() => {
    if (appFilterProjectId && appFilterProjectId !== selectedProjectId) {
      setSelectedProjectId(appFilterProjectId);
    }
  }, [appFilterProjectId, selectedProjectId, setSelectedProjectId]);

  const taskData = useTasksData(
    selectedProjectId,
    selectedProject,
    selectedSpaceId,
    selectedSpace,
  );
  // useTasksData は毎レンダー新しい戻り値オブジェクトを返すため、これを
  // TaskDetailModal へ渡す callback の依存配列に置くと、一覧更新のたびに
  // モーダル側の取得 effect（onTaskLoaded 依存）が再実行されてしまう。
  // callback は安定させつつ、イベント発生時には最新の一覧操作を参照する。
  const taskDataRef = useRef(taskData);
  useLayoutEffect(() => {
    taskDataRef.current = taskData;
  }, [taskData]);
  const { fetchData, setTasks } = taskData;
  const [filter, setFilter] = useState<FilterTab>("all");
  const [showClosed, setShowClosed] = useState(false);
  const [showFuture, setShowFuture] = useState(false);
  const [showOnlyMine, setShowOnlyMine] = useState(false);
  const [customFilter, setCustomFilter] = useState<FilterConfig>(EMPTY_FILTER);
  const [filterOpen, setFilterOpen] = useState(false);
  const [search, setSearch] = useState("");
  const [currentUserId, setCurrentUserId] = useState<string | null>(null);
  const [taskNotificationsDefaultEnabled, setTaskNotificationsDefaultEnabled] =
    useState(true);
  const searchInputRef = useRef<HTMLInputElement>(null);
  const projectTaskCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const task of taskData.tasks) {
      if (!task.parent_task_id) {
        counts.set(task.project_id, (counts.get(task.project_id) ?? 0) + 1);
      }
    }
    return counts;
  }, [taskData.tasks]);
  const activeProjects = useMemo(
    () => projects.filter((project) => projectTaskCounts.has(project.id)),
    [projectTaskCounts, projects],
  );
  // The workspace tree is an operational view.  Completed Projects remain
  // available to other project-level surfaces, but should not disappear from
  // the task scope merely because they have no tasks; only completion filters
  // them out here.
  const activeParticipatingProjects = useMemo(
    () => participatingProjects.filter((project) => !project.is_completed),
    [participatingProjects],
  );
  const projectIds = useMemo(
    () => new Set(projects.map((project) => project.id)),
    [projects],
  );
  const taskSpaceSelectionSourceRef = useRef<
    "task-space-navigation" | "task-space-wide-navigation" | null
  >(null);
  const {
    projectTab,
    projectTabsCollapsed,
    projectTabRefs,
    projectTabsToggleRef,
    setProjectTabAndSelection,
    cycleProjectTab,
    toggleProjectTabsCollapsed,
  } = useProjectTabs({
    projects,
    activeProjects,
    selectedSpaceId,
    selectedProjectId,
    setSelectedProjectId,
    spaceSelectionSourceRef: taskSpaceSelectionSourceRef,
    hasLoadedTasksRef: taskData.hasLoadedTasksRef,
    filter,
    showClosed,
    showFuture,
    customFilter,
  });

  const handleTaskSpaceChange = useCallback(
    (nextSpaceId: string) => {
      // This is one user intent, not two independent state changes.  The
      // marker prevents useProjectTabs from restoring a remembered Project
      // tab after Context switches to the new Space.
      setSelectedSpaceId(nextSpaceId);
      setProjectTabAndSelection("all", nextSpaceId, { source: "space-wide" });
    },
    [setProjectTabAndSelection, setSelectedSpaceId],
  );

  useEffect(() => {
    function handleSwitchSpace(e: Event) {
      const index = (e as CustomEvent<number>).detail;
      if (typeof index !== "number") return;
      const nextSpace = spaces[index];
      if (!nextSpace) return;
      handleTaskSpaceChange(nextSpace.id);
    }
    window.addEventListener("global-switch-space", handleSwitchSpace);
    return () => window.removeEventListener("global-switch-space", handleSwitchSpace);
  }, [spaces, handleTaskSpaceChange]);

  const handleSpaceColorChange = useCallback(
    async (spaceId: string, color: string) => {
      const space = spaces.find((item) => item.id === spaceId);
      if (!space || space.source === "remote" || space.can_write !== true) {
        throw new Error("このスペースは編集できません");
      }
      await taskApi.updateSpace(spaceId, { color: color || null });
      await Promise.all([refreshSpaces(), refreshProjects()]);
    },
    [refreshProjects, refreshSpaces, spaces],
  );

  const handleProjectColorChange = useCallback(
    async (projectId: string, color: string) => {
      const project = participatingProjects.find((item) => item.id === projectId);
      if (
        !project ||
        project.source === "remote" ||
        project.can_manage_settings !== true
      ) {
        throw new Error("このプロジェクトは編集できません");
      }
      await taskApi.updateProject(projectId, { color: color || null });
      await Promise.all([refreshSpaces(), refreshProjects()]);
    },
    [participatingProjects, refreshProjects, refreshSpaces],
  );

  useEffect(() => {
    let active = true;
    try {
      const saved = window.localStorage.getItem("tasks-custom-filter");
      if (saved) {
        const parsed = JSON.parse(saved) as FilterConfig;
        window.queueMicrotask(() => {
          if (active) setCustomFilter(parsed);
        });
      }
    } catch {
      // 不正な保存値は既定filterのまま扱う。
    }
    return () => {
      active = false;
    };
  }, []);
  useEffect(() => {
    try {
      window.localStorage.setItem(
        "tasks-custom-filter",
        JSON.stringify(customFilter),
      );
    } catch {
      // localStorageが無効でも表示中のfilterは維持する。
    }
  }, [customFilter]);
  useEffect(() => {
    fetch("/api/auth/status", { credentials: "include" })
      .then((response) => response.json())
      .then((data) => {
        if (data.authenticated && data.user?.id) {
          setCurrentUserId(data.user.id);
          setTaskNotificationsDefaultEnabled(
            getTaskNotificationsDefaultEnabled(data.user.user_settings),
          );
        }
      })
      .catch(() => {});
  }, []);
  useEffect(() => {
    if (!initialLoadComplete) return;
    void fetchData({ forceLoading: true });
  }, [fetchData, initialLoadComplete]);
  useEffect(() => {
    const handleRefresh = () => fetchData({ notifySidebar: false });
    window.addEventListener("task-list-refresh", handleRefresh);
    return () => window.removeEventListener("task-list-refresh", handleRefresh);
  }, [fetchData]);
  useEffect(() => {
    const handleTimerChanged = (event: Event) => {
      const detail = (
        event as CustomEvent<{
          activeEntry?: Task["active_time_entry"];
          taskId?: string | null;
        }>
      ).detail;
      const activeEntry = detail?.activeEntry ?? null;
      const taskId =
        detail?.taskId ??
        (activeEntry as { task_id?: string | null } | null)?.task_id ??
        null;
      if (!taskId) return;
      setTasks((current) =>
        current.map((task) => {
          if (task.id === taskId && activeEntry) {
            return applyTaskTimerStart(task, activeEntry);
          }
          return {
            ...task,
            active_time_entry:
              task.id === taskId
                ? activeEntry
                : activeEntry
                  ? null
                  : task.active_time_entry,
          };
        }),
      );
    };
    window.addEventListener("timer-changed", handleTimerChanged);
    return () =>
      window.removeEventListener("timer-changed", handleTimerChanged);
  }, [setTasks]);
  const appTasksKey =
    appFilterId &&
    appFilterProjectId &&
    selectedProjectId === appFilterProjectId
      ? `/apps/${appFilterId}/tasks?project_id=${encodeURIComponent(
          appFilterProjectId,
        )}`
      : null;
  const { data: appTasksData } = useSWR<{
    tasks: Array<{ task_id: string }>;
  }>(appTasksKey, () => appsApi.listAppTasks(appFilterId, appFilterProjectId));
  const appTaskIds = useMemo(() => {
    const linkedIds = new Set(
      (appTasksData?.tasks || []).map((link) => link.task_id),
    );
    if (!appFilterId || linkedIds.size === 0) return linkedIds;
    const taskById = new Map(taskData.tasks.map((task) => [task.id, task]));
    const topLevelIds = new Set<string>();
    for (const linkedId of linkedIds) {
      let task = taskById.get(linkedId);
      const visited = new Set<string>();
      while (task?.parent_task_id && !visited.has(task.id)) {
        visited.add(task.id);
        const parent = taskById.get(task.parent_task_id);
        if (!parent) break;
        task = parent;
      }
      if (task) topLevelIds.add(task.id);
    }
    return topLevelIds.size > 0 ? topLevelIds : linkedIds;
  }, [appFilterId, appTasksData?.tasks, taskData.tasks]);

  const {
    preferences,
    storageReady,
    setViewMode,
    columnVisibility,
    setColumnVisibility,
    columnWidths,
    setColumnWidth,
  } = useTaskViewPreferences();
  const viewMode = preferences.viewMode;
  const remoteReadOnly =
    selectedProject?.source === "remote" ||
    selectedProject?.can_write === false;
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const selectedTaskIdRef = useRef<string | null>(null);
  const [loadedDetailTask, setLoadedDetailTask] = useState<Task | null>(null);
  const [selectedOccurrenceContext, setSelectedOccurrenceContext] =
    useState<RecurringOccurrenceContext | null>(null);
  const [draftTask, setDraftTask] = useState<Partial<Task> | null>(null);
  const selectedSpaceIdRef = useRef(selectedSpaceId);
  const selectedSpaceRef = useRef(selectedSpace);
  const selectedProjectRef = useRef(selectedProject);
  const projectsRef = useRef(allProjects?.length ? allProjects : projects);
  const draftTaskRef = useRef(draftTask);
  useLayoutEffect(() => {
    selectedSpaceIdRef.current = selectedSpaceId;
    selectedSpaceRef.current = selectedSpace;
    selectedProjectRef.current = selectedProject;
    projectsRef.current = allProjects?.length ? allProjects : projects;
    draftTaskRef.current = draftTask;
  }, [
    allProjects,
    draftTask,
    projects,
    selectedProject,
    selectedSpace,
    selectedSpaceId,
  ]);

  const openTaskById = useCallback(
    (
      taskId: string,
      occurrenceContext: RecurringOccurrenceContext | null = null,
    ) => {
      setDraftTask(null);
      selectedTaskIdRef.current = taskId;
      setLoadedDetailTask(null);
      setSelectedOccurrenceContext(occurrenceContext);
      setSelectedTaskId(taskId);
    },
    [],
  );
  const openTask = useCallback((task: Task) => {
    setDraftTask(null);
    selectedTaskIdRef.current = task.id;
    setLoadedDetailTask(null);
    setSelectedOccurrenceContext(getTaskOccurrenceContext(task));
    setSelectedTaskId(task.id);
  }, []);
  const startDraft = useCallback((nextDraft: Partial<Task>) => {
    selectedTaskIdRef.current = null;
    setSelectedTaskId(null);
    setLoadedDetailTask(null);
    setSelectedOccurrenceContext(null);
    setDraftTask(nextDraft);
  }, []);
  const handleCreateNewTask = useCallback(() => {
    if (!selectedProjectId || remoteReadOnly) return;
    startDraft({
      project_id: projectTab !== "all" ? projectTab : selectedProjectId,
      title: "",
      notifications_enabled: taskNotificationsDefaultEnabled,
    });
  }, [
    projectTab,
    remoteReadOnly,
    selectedProjectId,
    startDraft,
    taskNotificationsDefaultEnabled,
  ]);

  // SharedAppShell の route-local navigation。データ・選択状態はこのページが
  // 引き続き所有し、ナビはイベントを転送するだけにして既存の API/DnD/shortcut
  // 挙動を変更しない。モバイルでは従来の canvas 内 controls を残す。
  useWorkspaceShellRegistration({
    id: "tasks-workspace",
    desktopPersistent: true,
    workspaceNavigation: (
      <TasksWorkspaceNavigation
        viewMode={viewMode}
        onViewModeChange={setViewMode}
        spaces={spaces}
        projects={activeParticipatingProjects}
        selectedSpaceId={selectedSpaceId}
        onSpaceChange={handleTaskSpaceChange}
        projectTab={projectTab}
        activeProjects={activeProjects}
        projectTaskCounts={projectTaskCounts}
        onProjectChange={setProjectTabAndSelection}
        onSpaceColorChange={handleSpaceColorChange}
        onProjectColorChange={handleProjectColorChange}
      />
    ),
  });
  const closeTaskDetail = useCallback(() => {
    selectedTaskIdRef.current = null;
    setSelectedTaskId(null);
    setLoadedDetailTask(null);
    setSelectedOccurrenceContext(null);
    setDraftTask(null);
  }, []);
  useEffect(() => {
    const checkParams = (event?: Event) => {
      const eventTaskId =
        event instanceof CustomEvent && typeof event.detail?.taskId === "string"
          ? event.detail.taskId
          : null;
      const params = new URLSearchParams(window.location.search);
      const detailId = eventTaskId ?? params.get("detail");
      const isNew = params.get("new");
      if (detailId) openTaskById(detailId, null);
      if (isNew && selectedProjectId && !remoteReadOnly) {
        startDraft({
          project_id: selectedProjectId,
          title: "",
          notifications_enabled: taskNotificationsDefaultEnabled,
        });
      }
      if (detailId || isNew) {
        const url = new URL(window.location.href);
        url.searchParams.delete("detail");
        url.searchParams.delete("new");
        window.history.replaceState({}, "", url.pathname + url.search);
      }
    };
    checkParams();
    window.addEventListener("task-detail-open", checkParams);
    return () => window.removeEventListener("task-detail-open", checkParams);
  }, [
    openTaskById,
    remoteReadOnly,
    selectedProjectId,
    startDraft,
    taskNotificationsDefaultEnabled,
  ]);
  const handleDetailTaskUpdated = useCallback(
    (updated?: Task | null, options?: { removedTaskId?: string }) => {
      const currentTaskId = selectedTaskIdRef.current;
      const isStaleCallback = currentTaskId !== selectedTaskId;
      const currentTaskData = taskDataRef.current;
      const selectedSpaceIdNow = selectedSpaceIdRef.current;
      const cacheScope = {
        selectedSpaceId: selectedSpaceIdNow,
        projects: projectsRef.current,
        cachedTasks: currentTaskData.tasks,
        draftTask: draftTaskRef.current,
        isRemoteCache: isRemoteTasksCacheScope(
          selectedSpaceIdNow,
          selectedSpaceRef.current,
          selectedProjectRef.current,
        ),
      };
      if (options?.removedTaskId) {
        if (
          shouldApplyTaskMutationToCurrentCache({
            ...cacheScope,
            removedTaskId: options.removedTaskId,
          })
        ) {
          currentTaskData.removeTaskLocally(options.removedTaskId);
        }
        return;
      }
      if (updated === null) {
        if (isStaleCallback) return;
        setLoadedDetailTask(null);
        if (currentTaskId) currentTaskData.removeTaskLocally(currentTaskId);
        return;
      }
      if (updated) {
        if (currentTaskId && updated.id === currentTaskId) {
          if (isStaleCallback) return;
          setLoadedDetailTask(updated);
          if (currentTaskData.tasks.some((task) => task.id === updated.id)) {
            currentTaskData.applyTaskPatchLocally(updated.id, updated);
          }
          if (
            updated.has_recurrence &&
            updated.effective_occurrence_status !== undefined
          ) {
            void currentTaskData.fetchData({ forceLoading: false });
          }
        } else if (
          shouldApplyTaskMutationToCurrentCache({
            ...cacheScope,
            task: updated,
          })
        ) {
          currentTaskData.upsertTaskLocally(updated);
        }
        return;
      }
      if (isStaleCallback) return;
      if (!currentTaskId) return;
      void taskApi
        .getTask(currentTaskId)
        .then((loaded) => {
          if (selectedTaskIdRef.current !== loaded.id) return;
          setLoadedDetailTask(loaded);
          const latestTaskData = taskDataRef.current;
          if (latestTaskData.tasks.some((task) => task.id === loaded.id)) {
            latestTaskData.upsertTaskLocally(loaded);
          }
        })
        .catch((error) => {
          console.warn("更新済みタスクの局所再取得に失敗:", error);
        });
    },
    [selectedTaskId],
  );
  const handleDetailTaskLoaded = useCallback((loaded: Task) => {
    if (selectedTaskIdRef.current !== loaded.id) return;
    setLoadedDetailTask(loaded);
    const currentTaskData = taskDataRef.current;
    if (currentTaskData.tasks.some((task) => task.id === loaded.id)) {
      currentTaskData.applyTaskPatchLocally(loaded.id, loaded);
    }
  }, []);
  const selectedTask = selectedTaskId
    ? loadedDetailTask?.id === selectedTaskId
      ? loadedDetailTask
      : taskData.tasks.find((task) => task.id === selectedTaskId)
    : null;
  const availableProjects = allProjects?.length ? allProjects : projects;
  const selectedTaskProject = selectedTask
    ? availableProjects.find(
        (project) => project.id === selectedTask.project_id,
      )
    : null;
  const selectedRemoteTask =
    selectedTask?.source === "remote" ? selectedTask : null;
  const selectedTaskReadOnly = selectedTaskId
    ? !selectedTask ||
      !selectedTaskProject ||
      selectedTask.source === "remote" ||
      selectedTaskProject.source === "remote" ||
      selectedTaskProject.can_write === false
    : remoteReadOnly;
  const remoteDialogTarget: RemoteTaskDialogTarget | null =
    selectedRemoteTask?.remote_server_id && selectedRemoteTask.resource_id
      ? {
          profileId: selectedRemoteTask.remote_server_id,
          profileName: selectedRemoteTask.remote_server_name ?? "Remote",
          profileColor: selectedRemoteTask.remote_server_color,
          baseUrl: selectedRemoteTask.remote_server_base_url ?? "",
          taskId: selectedRemoteTask.resource_id,
          title: selectedRemoteTask.title,
          status: selectedRemoteTask.status,
          priority: selectedRemoteTask.priority,
          startAt: selectedRemoteTask.start_at,
          endAt: selectedRemoteTask.end_at,
        }
      : null;
  const currentScopeLabel =
    selectedProject?.name ??
    (selectedSpaceId
      ? (spaces.find((space) => space.id === selectedSpaceId)?.name ??
        "スペース")
      : "すべてのスペース");

  return (
    <div
      className="ao-tasks-canvas flex h-full min-h-0 flex-col bg-card dark:bg-background"
      data-shell-workspace="tasks"
      data-shell-region="tasks-canvas"
    >
      <div className="ao-tasks-mobile-header shrink-0 space-y-3 border-b border-border bg-card dark:bg-background px-3 py-3 md:hidden md:px-4 md:py-3">
        <div className="flex items-end justify-between gap-3 md:hidden">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight">タスク</h1>
            <p className="mt-0.5 text-sm text-muted-foreground">
              {currentScopeLabel}
            </p>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2 md:gap-3">
          <div className="relative min-w-0 flex-1 md:min-w-48">
            <Search className="absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              ref={searchInputRef}
              placeholder="タスク・タグ・プロジェクトを検索"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              aria-label="タスク・タグ・プロジェクトを検索"
              className="ao-task-toolbar-control ao-task-search-input h-10 rounded border border-border bg-card/40 pl-10 text-sm shadow-none md:h-9 md:pl-8"
            />
          </div>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={handleCreateNewTask}
            disabled={remoteReadOnly}
            className="hidden"
          >
            <Plus className="size-4" />
            新規タスク
          </Button>
          <button
            ref={projectTabsToggleRef}
            type="button"
            data-testid="task-project-tabs-toggle"
            aria-label={
              projectTabsCollapsed
                ? "プロジェクトタブを表示"
                : "プロジェクトタブを隠す"
            }
            title={
              projectTabsCollapsed
                ? "プロジェクトタブを表示"
                : "プロジェクトタブを隠す"
            }
            onClick={toggleProjectTabsCollapsed}
            className="inline-flex h-12 shrink-0 items-center justify-center gap-1.5 rounded-xl border bg-card px-3 text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground md:size-9 md:rounded-md md:px-0 md:hidden"
          >
            <SlidersHorizontal className="size-4 md:hidden" />
            <span className="max-w-28 truncate md:hidden">
              {selectedProject?.name ?? "全体"}
            </span>
            {projectTabsCollapsed ? (
              <ChevronDown className="hidden size-4 md:block" />
            ) : (
              <ChevronUp className="hidden size-4 md:block" />
            )}
          </button>
        </div>
        {!projectTabsCollapsed && (
          <div className="md:hidden">
            <TaskProjectTabs
              projectTab={projectTab}
              activeProjects={activeProjects}
              allCount={
                taskData.tasks.filter(
                  (task) =>
                    !task.parent_task_id && projectIds.has(task.project_id),
                ).length
              }
              projectTaskCounts={projectTaskCounts}
              projectTabRefs={projectTabRefs}
              onSelectTab={setProjectTabAndSelection}
            />
          </div>
        )}
        <div className="md:hidden">
          <TaskViewSwitcher value={viewMode} onChange={setViewMode} />
        </div>
      </div>
      <div className="min-h-0 flex-1">
        {storageReady && viewMode === "list" ? (
          <TaskListView
            projectContext={projectContext}
            taskData={taskData}
            appFilterId={appFilterId}
            appTaskIds={appTaskIds}
            filterState={{
              filter,
              setFilter,
              showClosed,
              setShowClosed,
              showFuture,
              setShowFuture,
              showOnlyMine,
              setShowOnlyMine,
              customFilter,
              setCustomFilter,
              filterOpen,
              setFilterOpen,
              search,
              setSearch,
              currentUserId,
            }}
            projectTab={projectTab}
            cycleProjectTab={cycleProjectTab}
            searchInputRef={searchInputRef}
            handleCreateNewTask={handleCreateNewTask}
            columnVisibility={columnVisibility}
            onColumnVisibilityChange={setColumnVisibility}
            columnWidths={columnWidths}
            onColumnWidthChange={setColumnWidth}
            selectedTaskId={selectedTaskId}
            draftTask={draftTask}
            openTask={openTask}
            openTaskById={openTaskById}
          />
        ) : null}
        {storageReady && viewMode === "schedule" ? (
          <TaskScheduleView
            tasks={taskData.tasks}
            projects={allProjects?.length ? allProjects : projects}
            selectedProjectId={selectedProjectId}
            appFilterId={appFilterId}
            appTaskIds={appTaskIds}
            filterState={{
              filter,
              showClosed,
              showFuture,
              showOnlyMine,
              customFilter,
              search,
              currentUserId,
            }}
            loading={taskData.loading}
            loadError={taskData.loadError}
            remoteReadOnly={remoteReadOnly}
            onOpenTask={openTask}
          />
        ) : null}
      </div>
      {selectedRemoteTask ? (
        <RemoteTaskDialog
          target={remoteDialogTarget}
          onClose={closeTaskDetail}
          onUpdated={() => taskData.fetchData({ forceLoading: false })}
        />
      ) : (
        <TaskDetailModal
          taskId={selectedTaskId}
          draftTask={draftTask}
          open={Boolean(selectedTaskId || draftTask)}
          readOnly={selectedTaskReadOnly}
          onOpenChange={(open) => {
            if (!open) closeTaskDetail();
          }}
          onTaskUpdated={handleDetailTaskUpdated}
          onTaskLoaded={handleDetailTaskLoaded}
          onOpenTask={(taskId) => openTaskById(taskId, null)}
          occurrenceContext={selectedOccurrenceContext}
        />
      )}
    </div>
  );
}
