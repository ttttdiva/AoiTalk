"use client";

import React, {
  useState,
  useEffect,
  useCallback,
  useMemo,
  useRef,
} from "react";

import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Checkbox } from "@/components/ui/checkbox";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Search,
  Play,
  Square,
  Timer,
  ChevronRight,
  ChevronDown,
  ChevronUp,
  Clock,
  Repeat,
} from "lucide-react";
import { toast } from "sonner";
import {
  taskApi,
  type RecurringOccurrenceContext,
  type Task,
} from "@/lib/task-api";
import { TaskDetailModal } from "@/components/tasks/task-detail-modal";
import { RecurringDeleteDialog } from "@/components/tasks/task-detail/recurring-delete-dialog";
import { TagPill } from "@/components/tasks/tag-pill";
import {
  applyTaskFilter,
  EMPTY_FILTER,
  type FilterConfig,
} from "@/components/tasks/task-filter-builder";
import { TaskRowDatePicker } from "@/components/tasks/task-row-date-picker";
import { toLocalDateTimeInputValue } from "@/lib/date-time";
import { cn } from "@/lib/utils";
import { useProject } from "@/contexts/project-context";
import {
  getTaskDisplayAllDay,
  getTaskDisplayEndAt,
  getTaskDisplayStartAt,
} from "@/lib/task-effective-date";
import { getTaskNotificationsDefaultEnabled } from "@/lib/user-settings";
import {
  buildTaskDateUpdate,
  dateButtonColor,
  formatDuration,
  formatElapsed,
  getStatusShortcutTarget,
  getTaskDateView,
  getTaskOccurrenceContext,
  handleStatusShortcutCapture,
  isFutureTask,
  isOverdue,
  PRIORITY_COLORS,
  PRIORITY_LABELS,
  STATUS_DOT_COLORS,
  STATUS_LABELS,
  type FilterTab,
} from "@/lib/tasks-page-utils";
import { useTasksData } from "@/components/tasks/hooks/use-tasks-data";
import { useTaskUndo } from "@/components/tasks/hooks/use-task-undo";
import { useTaskDnd } from "@/components/tasks/hooks/use-task-dnd";
import { useTaskContextMenu } from "@/components/tasks/hooks/use-task-context-menu";
import { useTaskSelection } from "@/components/tasks/hooks/use-task-selection";
import { useTaskClipboard } from "@/components/tasks/hooks/use-task-clipboard";
import { useBulkTaskActions } from "@/components/tasks/hooks/use-bulk-task-actions";
import { useProjectTabs } from "@/components/tasks/hooks/use-project-tabs";
import { useTaskCommandDialog } from "@/components/tasks/hooks/use-task-command-dialog";
import { useTaskListKeyboard } from "@/components/tasks/hooks/use-task-list-keyboard";
import { TaskStatusMenuItems } from "@/components/tasks/task-status-menu-items";
import { TaskListContextMenu } from "@/components/tasks/task-list-context-menu";
import { TaskCommandDialog } from "@/components/tasks/task-command-dialog";
import { TaskListToolbar } from "@/components/tasks/task-list-toolbar";
import { TaskProjectTabs } from "@/components/tasks/task-project-tabs";
import {
  QuickAddRow,
  SubtaskAddRow,
  SubtaskRow,
} from "@/components/tasks/task-list-rows";

export default function TasksPage() {
  const { projects, selectedProjectId, setSelectedProjectId, selectedSpaceId } =
    useProject();
  const {
    tasks,
    setTasks,
    tags,
    setTags,
    loading,
    fetchData,
    hasLoadedTasksRef,
    upsertTaskLocally,
    removeTaskLocally,
    applyTaskPatchLocally,
    applyTaskPatchesLocally,
    applyTopLevelReorderLocally,
    applyAllTopLevelReorderLocally,
  } = useTasksData(selectedProjectId);
  const [filter, setFilter] = useState<FilterTab>("all");
  const [showClosed, setShowClosed] = useState(false);
  const [customFilter, setCustomFilter] = useState<FilterConfig>(EMPTY_FILTER);
  const [filterOpen, setFilterOpen] = useState(false);

  // customFilter を localStorage に保存/復元
  useEffect(() => {
    try {
      const saved = localStorage.getItem("tasks-custom-filter");
      if (saved) setCustomFilter(JSON.parse(saved));
    } catch {
      // ignore
    }
  }, []);
  useEffect(() => {
    try {
      localStorage.setItem("tasks-custom-filter", JSON.stringify(customFilter));
    } catch {
      // ignore
    }
  }, [customFilter]);
  const [showFuture, setShowFuture] = useState(false);
  const [showPriority, setShowPriority] = useState(false);
  const [showOnlyMine, setShowOnlyMine] = useState(false);
  const [currentUserId, setCurrentUserId] = useState<string | null>(null);
  const [taskNotificationsDefaultEnabled, setTaskNotificationsDefaultEnabled] =
    useState(true);
  const [search, setSearch] = useState("");
  const [timerLoading, setTimerLoading] = useState<string | null>(null);
  const [bulkStatusMenuOpen, setBulkStatusMenuOpen] = useState(false);
  const [rowStatusMenuTaskId, setRowStatusMenuTaskId] = useState<string | null>(
    null,
  );
  const [bulkLoading, setBulkLoading] = useState(false);
  const searchInputRef = useRef<HTMLInputElement>(null);
  const taskRowRefs = useRef<Record<string, HTMLTableRowElement | null>>({});
  const pendingRowStatusFocusTaskIdRef = useRef<string | null>(null);
  const [focusedTaskId, setFocusedTaskId] = useState<string | null>(null);
  const projectIds = useMemo(
    () => new Set(projects.map((project) => project.id)),
    [projects],
  );

  // filteredTasks を ref で保持（Shift+クリック範囲選択・DnD 用）
  const filteredTasksRef = useRef<Task[]>([]);

  const {
    selectedIds,
    setSelectedIds,
    selectedIdsRef,
    lastClickedIndexRef,
    prevShiftRangeRef,
    handleCheckboxClick,
    clearSelection,
    toggleSelectAll,
  } = useTaskSelection({ filteredTasksRef });

  const { pushUndo, snapshotTask, queueTaskCompletionUndo } = useTaskUndo({
    tasks,
    fetchData,
  });

  // 「自分担当のみ表示」用に現在ユーザーIDを取得
  useEffect(() => {
    fetch("/api/auth/status", { credentials: "include" })
      .then((r) => r.json())
      .then((d) => {
        if (d.authenticated && d.user?.id) {
          setCurrentUserId(d.user.id);
          setTaskNotificationsDefaultEnabled(
            getTaskNotificationsDefaultEnabled(d.user.user_settings),
          );
        }
      })
      .catch(() => {});
  }, []);

  // タスク詳細モーダル（URLクエリパラメータ ?detail= にも対応）
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [selectedOccurrenceContext, setSelectedOccurrenceContext] =
    useState<RecurringOccurrenceContext | null>(null);
  const [pendingRecurringDelete, setPendingRecurringDelete] = useState<{
    task: Task;
    occurrenceContext: RecurringOccurrenceContext;
  } | null>(null);
  const [draftTask, setDraftTask] = useState<Partial<Task> | null>(null);
  const requestRecurringDelete = useCallback((task: Task): boolean => {
    if (!task.has_recurrence) return false;
    const occurrenceContext = getTaskOccurrenceContext(task);
    if (!occurrenceContext?.start_at) {
      toast.error("繰り返しタスクの発生日を取得できません", {
        description: "タスク詳細を開き直してから削除してください。",
      });
      return true;
    }
    setPendingRecurringDelete({ task, occurrenceContext });
    return true;
  }, []);
  const handleRecurringDelete = useCallback(
    async (mode: "single" | "future") => {
      if (!pendingRecurringDelete) return;
      const { task, occurrenceContext } = pendingRecurringDelete;
      try {
        await taskApi.deleteOccurrence(task.id, {
          mode,
          occurrence_id: occurrenceContext.occurrence_id ?? null,
          occurrence_start_at: occurrenceContext.start_at,
          occurrence_end_at: occurrenceContext.end_at ?? null,
          original_start_at: occurrenceContext.original_start_at ?? null,
        });
        setPendingRecurringDelete(null);
        clearSelection();
        await fetchData();
      } catch (err) {
        console.error("繰り返しタスク削除失敗:", err);
      }
    },
    [clearSelection, fetchData, pendingRecurringDelete],
  );
  const openTaskById = useCallback(
    (
      taskId: string,
      occurrenceContext: RecurringOccurrenceContext | null = null,
    ) => {
      setDraftTask(null);
      setSelectedOccurrenceContext(occurrenceContext);
      setSelectedTaskId(taskId);
    },
    [],
  );
  const openTask = useCallback((task: Task) => {
    setDraftTask(null);
    setSelectedOccurrenceContext(getTaskOccurrenceContext(task));
    setSelectedTaskId(task.id);
  }, []);

  // プロジェクトごとのタスク数（フィルタ前、親タスクのみ）
  const projectTaskCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const t of tasks) {
      if (!t.parent_task_id) {
        counts.set(t.project_id, (counts.get(t.project_id) || 0) + 1);
      }
    }
    return counts;
  }, [tasks]);

  // タスクがあるプロジェクトのみタブに表示
  const activeProjects = useMemo(
    () => projects.filter((p) => projectTaskCounts.has(p.id)),
    [projects, projectTaskCounts],
  );

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
    setSelectedProjectId,
    hasLoadedTasksRef,
    filter,
    showClosed,
    showFuture,
    customFilter,
  });

  // タスク作成
  const handleCreateNewTask = useCallback(async () => {
    if (!selectedProjectId) return;
    setSelectedTaskId(null);
    setSelectedOccurrenceContext(null);
    setDraftTask({
      project_id: projectTab !== "all" ? projectTab : selectedProjectId,
      title: "",
      notifications_enabled: taskNotificationsDefaultEnabled,
    });
  }, [selectedProjectId, projectTab, taskNotificationsDefaultEnabled]);

  // コマンドパレットからの ?detail= / ?new=1 パラメータで各ダイアログを開く
  useEffect(() => {
    const checkParams = (event?: Event) => {
      const eventTaskId =
        event instanceof CustomEvent && typeof event.detail?.taskId === "string"
          ? event.detail.taskId
          : null;
      const params = new URLSearchParams(window.location.search);
      const detailId = eventTaskId ?? params.get("detail");
      const isNew = params.get("new");
      if (detailId) {
        setDraftTask(null);
        setSelectedOccurrenceContext(null);
        setSelectedTaskId(detailId);
      }
      if (isNew) {
        handleCreateNewTask();
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
  }, [handleCreateNewTask]);

  // 親タスクグループ（親行 + サブ行 + サブタスク追加行）の hover 管理
  const [hoveredGroupId, setHoveredGroupId] = useState<string | null>(null);

  // サブタスク展開状態
  const [expandedTasks, setExpandedTasks] = useState<Set<string>>(new Set());
  const toggleExpand = useCallback((taskId: string) => {
    setExpandedTasks((prev) => {
      const next = new Set(prev);
      if (next.has(taskId)) next.delete(taskId);
      else next.add(taskId);
      return next;
    });
  }, []);

  const handleTaskDateChange = useCallback(
    async (
      task: Task,
      changes: { start_at?: string | null; end_at?: string | null },
    ) => {
      const dateView = getTaskDateView(task);
      const updates = buildTaskDateUpdate(dateView, changes);
      if (task.has_recurrence && task.effective_occurrence_start_at) {
        const hasStartUpdate = Object.prototype.hasOwnProperty.call(
          updates,
          "start_at",
        );
        const hasEndUpdate = Object.prototype.hasOwnProperty.call(
          updates,
          "end_at",
        );
        const nextStartAt =
          hasStartUpdate && updates.start_at
            ? updates.start_at
            : task.effective_occurrence_start_at;
        if (!nextStartAt) return;
        const nextEndAt =
          hasEndUpdate ? updates.end_at : (task.effective_occurrence_end_at ?? null);
        applyTaskPatchLocally(task.id, {
          effective_start_at: nextStartAt,
          effective_end_at: nextEndAt,
          effective_all_day: updates.all_day,
        });
        try {
          await taskApi.moveOccurrence(task.id, {
            occurrence_id: task.effective_occurrence_id ?? null,
            occurrence_start_at: task.effective_occurrence_start_at,
            occurrence_end_at: task.effective_occurrence_end_at ?? null,
            original_start_at:
              task.effective_occurrence_original_start_at ??
              task.effective_occurrence_start_at,
            next_start_at: nextStartAt,
            next_end_at: nextEndAt,
            status: task.effective_occurrence_status ?? task.status,
            all_day: updates.all_day,
          });
          await fetchData({ forceLoading: false });
        } catch (err) {
          console.error("繰り返し発生日時の更新に失敗:", err);
          await fetchData();
        }
        return;
      }

      pushUndo({
        type: "update",
        taskId: task.id,
        previous: snapshotTask(task, ["start_at", "end_at", "all_day"]),
      });
      applyTaskPatchLocally(task.id, updates);
      try {
        const updatedTask = await taskApi.updateTask(task.id, updates);
        applyTaskPatchLocally(task.id, updatedTask);
      } catch (err) {
        console.error("日付更新失敗:", err);
        await fetchData();
      }
    },
    [applyTaskPatchLocally, fetchData, pushUndo, snapshotTask],
  );

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  // グローバルタスク作成からのリフレッシュ通知
  useEffect(() => {
    const handler = () => fetchData({ notifySidebar: false });
    window.addEventListener("task-list-refresh", handler);
    return () => window.removeEventListener("task-list-refresh", handler);
  }, [fetchData]);

  // タイマー操作
  const handleTimer = useCallback(
    async (task: Task, e: React.MouseEvent) => {
      e.stopPropagation();
      setTimerLoading(task.id);
      try {
        if (task.active_time_entry) {
          await taskApi.stopTimer(task.active_time_entry.id);
          setTasks((prev) =>
            prev.map((item) =>
              item.id === task.id ? { ...item, active_time_entry: null } : item,
            ),
          );
          window.dispatchEvent(
            new CustomEvent("timer-changed", {
              detail: { activeEntry: null },
            }),
          );
        } else {
          const started = await taskApi.startTimer(task.id);
          setTasks((prev) =>
            prev.map((item) =>
              item.id === task.id
                ? { ...item, active_time_entry: started }
                : item,
            ),
          );
          window.dispatchEvent(
            new CustomEvent("timer-changed", {
              detail: { activeEntry: started },
            }),
          );
        }
        await fetchData();
      } catch (err) {
        console.error("タイマー操作失敗:", err);
      } finally {
        setTimerLoading(null);
      }
    },
    [fetchData, setTasks],
  );

  // 他画面（ヘッダー/モーダル）でタイマーが変わったら一覧も再取得
  useEffect(() => {
    const onTimerChanged = () => {
      fetchData();
    };
    window.addEventListener("timer-changed", onTimerChanged);
    return () => window.removeEventListener("timer-changed", onTimerChanged);
  }, [fetchData]);

  // フォーカス中タスクのタイマー開始
  const handleFocusedTaskTimerStart = useCallback(async () => {
    if (!focusedTaskId) return;
    const task = tasks.find((item) => item.id === focusedTaskId);
    if (!task || task.active_time_entry) return;

    setTimerLoading(task.id);
    try {
      const started = await taskApi.startTimer(task.id);
      setTasks((prev) =>
        prev.map((item) =>
          item.id === task.id ? { ...item, active_time_entry: started } : item,
        ),
      );
      window.dispatchEvent(
        new CustomEvent("timer-changed", {
          detail: { activeEntry: started },
        }),
      );
      await fetchData();
    } catch (err) {
      console.error("Focused task timer start failed:", err);
    } finally {
      setTimerLoading(null);
    }
  }, [fetchData, focusedTaskId, setTasks, tasks]);

  // 経過時間リアルタイム表示
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const hasActive = tasks.some((t) => t.active_time_entry);
    if (!hasActive) return;
    const interval = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(interval);
  }, [tasks]);

  const {
    draggingIds,
    dropTargetId,
    dropMode,
    handleDragStart,
    handleDragOver,
    handleDragLeave,
    handleDrop,
    handleDragEnd,
  } = useTaskDnd({
    tasks,
    projectIds,
    projectTab,
    fetchData,
    applyTaskPatchesLocally,
    applyTopLevelReorderLocally,
    applyAllTopLevelReorderLocally,
    setExpandedTasks,
    filteredTasksRef,
    selectedIdsRef,
  });

  // 右クリックコンテキストメニュー
  const {
    contextMenu,
    contextMenuRef,
    contextMenuStyle,
    contextSubmenuClassName,
    statusSubmenuOpen,
    setStatusSubmenuOpen,
    prioritySubmenuOpen,
    setPrioritySubmenuOpen,
    handleContextMenu,
    handleContextStatusChange,
    handleContextPriorityChange,
    handleContextTimer,
    handleDuplicate,
    handleCopyTaskId,
    handleContextDelete,
  } = useTaskContextMenu({
    fetchData,
    pushUndo,
    queueTaskCompletionUndo,
    applyTaskPatchLocally,
    upsertTaskLocally,
    removeTaskLocally,
    setSelectedIds,
    requestRecurringDelete,
  });

  // プロジェクト名マップ
  const projectMap = useMemo(
    () => new Map(projects.map((p) => [p.id, p.name])),
    [projects],
  );

  const focusTaskById = useCallback((taskId: string | null) => {
    setFocusedTaskId(taskId);
    if (!taskId) return;
    requestAnimationFrame(() => {
      const row = taskRowRefs.current[taskId];
      row?.focus();
      row?.scrollIntoView({ block: "nearest" });
    });
  }, []);

  const refocusPendingRowStatusTask = useCallback(
    (taskId: string, clearPending = false) => {
      if (pendingRowStatusFocusTaskIdRef.current !== taskId) return;
      focusTaskById(taskId);
      if (clearPending) {
        pendingRowStatusFocusTaskIdRef.current = null;
      }
    },
    [focusTaskById],
  );

  const resolveRowStatusMenuFinalFocus = useCallback((taskId: string) => {
    if (pendingRowStatusFocusTaskIdRef.current !== taskId) return true;
    return false;
  }, []);

  const closeRowStatusMenuAndRefocusTask = useCallback(
    (taskId: string) => {
      pendingRowStatusFocusTaskIdRef.current = taskId;
      setFocusedTaskId(taskId);
      setRowStatusMenuTaskId(null);
      window.setTimeout(() => refocusPendingRowStatusTask(taskId), 0);
      window.setTimeout(() => refocusPendingRowStatusTask(taskId), 80);
      window.setTimeout(() => refocusPendingRowStatusTask(taskId, true), 200);
    },
    [refocusPendingRowStatusTask],
  );

  const handleRowStatusMenuOpenChange = useCallback(
    (taskId: string, open: boolean) => {
      if (open) {
        pendingRowStatusFocusTaskIdRef.current = null;
      }
      setRowStatusMenuTaskId(open ? taskId : null);
    },
    [],
  );

  const handleRowStatusMenuOpenChangeComplete = useCallback(
    (taskId: string, open: boolean) => {
      if (!open) {
        refocusPendingRowStatusTask(taskId);
      }
    },
    [refocusPendingRowStatusTask],
  );

  // タスクコマンドダイアログ（`/` ショートカット）
  const {
    taskCommandOpen,
    taskCommandTaskId,
    taskCommandValue,
    setTaskCommandValue,
    taskCommandError,
    setTaskCommandError,
    taskCommandLoading,
    taskCommandCandidates,
    closeTaskCommandDialog,
    openTaskCommandDialog,
    handleTaskCommandSubmit,
  } = useTaskCommandDialog({
    tasks,
    tags,
    setTags,
    projects,
    selectedProjectId,
    fetchData,
    focusTaskById,
    pushUndo,
    queueTaskCompletionUndo,
    applyTaskPatchLocally,
    upsertTaskLocally,
  });

  // サブタスクインライン追加
  const [subtaskAddParentId, setSubtaskAddParentId] = useState<string | null>(
    null,
  );
  const [subtaskAddTitle, setSubtaskAddTitle] = useState("");
  const [subtaskAddCreating, setSubtaskAddCreating] = useState(false);
  const subtaskAddRef = useRef<HTMLInputElement>(null);

  const handleSubtaskAdd = useCallback(
    async (parentTask: Task) => {
      if (!subtaskAddTitle.trim()) return;
      setSubtaskAddCreating(true);
      try {
        const created = await taskApi.createTask({
          project_id: parentTask.project_id,
          title: subtaskAddTitle.trim(),
          parent_task_id: parentTask.id,
        });
        upsertTaskLocally(created);
        setSubtaskAddTitle("");
        void fetchData();
      } catch (err) {
        console.error("サブタスク作成失敗:", err);
      } finally {
        setSubtaskAddCreating(false);
      }
    },
    [subtaskAddTitle, fetchData, upsertTaskLocally],
  );

  // サブタスクマップ（parent_task_id → サブタスク配列）
  const subtaskMap = useMemo(() => {
    const map = new Map<string, Task[]>();
    for (const t of tasks) {
      if (t.parent_task_id) {
        const list = map.get(t.parent_task_id) || [];
        list.push(t);
        map.set(t.parent_task_id, list);
      }
    }
    return map;
  }, [tasks]);

  // フィルタリング（親タスクのみ表示、サブタスクは除外）
  const filteredTasks = useMemo(() => {
    let result = tasks.filter((t) => !t.parent_task_id);

    // プロジェクトタブフィルタ
    if (projectTab !== "all") {
      result = result.filter((t) => t.project_id === projectTab);
    } else {
      result = result.filter((t) => projectIds.has(t.project_id));
    }

    // トグル: 完了済みを非表示（デフォルト）
    if (!showClosed) {
      result = result.filter((t) => t.status !== "closed");
    }

    // トグル: 未来のタスクを非表示（デフォルト）
    if (!showFuture) {
      result = result.filter((t) => !isFutureTask(t));
    }

    // トグル: 自分が担当のタスクのみ
    if (showOnlyMine && currentUserId) {
      result = result.filter((t) =>
        (t.assignees || []).some((a) => a.user_id === currentUserId),
      );
    }

    // フィルタタブ
    switch (filter) {
      case "overdue":
        result = result.filter(isOverdue);
        break;
    }

    if (search.trim()) {
      const q = search.toLowerCase();
      result = result.filter(
        (t) =>
          t.title.toLowerCase().includes(q) ||
          t.description?.toLowerCase().includes(q) ||
          t.tags.some((tag) => tag.name.toLowerCase().includes(q)),
      );
    }

    // ClickUp 風カスタムフィルタ
    if (customFilter.rules.length > 0) {
      result = applyTaskFilter(result, customFilter, projectMap);
    }

    return result;
  }, [
    tasks,
    filter,
    search,
    projectTab,
    projectIds,
    showClosed,
    showFuture,
    showOnlyMine,
    currentUserId,
    customFilter,
    projectMap,
  ]);

  filteredTasksRef.current = filteredTasks;

  const getKeyboardSelectionTasks = useCallback(() => {
    if (selectedIds.size > 0) {
      return filteredTasks
        .filter((task) => selectedIds.has(task.id))
        .map((task) => tasks.find((item) => item.id === task.id) || task);
    }

    if (!focusedTaskId) return [];
    const focusedTask = tasks.find((task) => task.id === focusedTaskId);
    return focusedTask ? [focusedTask] : [];
  }, [filteredTasks, focusedTaskId, selectedIds, tasks]);

  // クリップボード（Ctrl+C / X / V）
  const {
    clipboardRef,
    cutTaskIds,
    setCutTaskIds,
    handleClipboardStore,
    handleClipboardPaste,
  } = useTaskClipboard({
    tasks,
    focusedTaskId,
    fetchData,
    focusTaskById,
    getKeyboardSelectionTasks,
    setSelectedIds,
    setBulkLoading,
    lastClickedIndexRef,
    prevShiftRangeRef,
  });

  // 一括操作
  const {
    handleRowStatusChange,
    handleBulkStatusChange,
    handleBulkDelete,
    handleBulkDuplicate,
    handleBulkMove,
    handleDeleteTasks,
  } = useBulkTaskActions({
    tasks,
    setTasks,
    selectedIds,
    setSelectedIds,
    clearSelection,
    fetchData,
    pushUndo,
    queueTaskCompletionUndo,
    applyTaskPatchLocally,
    upsertTaskLocally,
    setBulkLoading,
    setCutTaskIds,
    focusedTaskId,
    focusTaskById,
    filteredTasksRef,
    requestRecurringDelete,
  });

  // ステータスメニュー表示中のショートカットキー
  useEffect(() => {
    if (!bulkStatusMenuOpen && !rowStatusMenuTaskId) return;

    const handleKeyDown = (e: KeyboardEvent) => {
      const target = getStatusShortcutTarget(e.key);
      if (!target) return;

      e.preventDefault();
      e.stopPropagation();
      e.stopImmediatePropagation();

      if (rowStatusMenuTaskId) {
        const task = tasks.find((item) => item.id === rowStatusMenuTaskId);
        closeRowStatusMenuAndRefocusTask(rowStatusMenuTaskId);
        if (task) {
          void handleRowStatusChange(task, target);
        }
        return;
      }

      if (bulkStatusMenuOpen) {
        setBulkStatusMenuOpen(false);
        void handleBulkStatusChange(target);
      }
    };

    window.addEventListener("keydown", handleKeyDown, true);
    return () => window.removeEventListener("keydown", handleKeyDown, true);
  }, [
    bulkStatusMenuOpen,
    closeRowStatusMenuAndRefocusTask,
    handleBulkStatusChange,
    handleRowStatusChange,
    rowStatusMenuTaskId,
    tasks,
  ]);

  useEffect(() => {
    if (filteredTasks.length === 0) {
      setFocusedTaskId(null);
      return;
    }

    if (
      focusedTaskId &&
      filteredTasks.some((task) => task.id === focusedTaskId)
    ) {
      return;
    }

    setFocusedTaskId(filteredTasks[0].id);
  }, [filteredTasks, focusedTaskId]);

  // グローバルキーボードショートカット
  useTaskListKeyboard({
    tasks,
    filteredTasks,
    focusedTaskId,
    focusTaskById,
    selectedTaskId,
    draftTask,
    selectedIds,
    clearSelection,
    cycleProjectTab,
    openTaskCommandDialog,
    openTask,
    openTaskById,
    handleFocusedTaskTimerStart,
    handleCheckboxClick,
    getKeyboardSelectionTasks,
    handleDeleteTasks,
    handleClipboardStore,
    handleClipboardPaste,
    clipboardRef,
    searchInputRef,
  });

  return (
    <div className="flex h-full min-h-0 flex-col gap-3 p-4">
      {/* ヘッダー部 */}
      <div className="flex flex-wrap items-center gap-3">
        <div className="relative flex-1 min-w-48">
          <Search className="absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            ref={searchInputRef}
            placeholder="タスクを検索..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="pl-8"
          />
        </div>
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
          className="inline-flex size-9 shrink-0 items-center justify-center rounded-md border bg-background text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
        >
          {projectTabsCollapsed ? (
            <ChevronDown className="size-4" />
          ) : (
            <ChevronUp className="size-4" />
          )}
        </button>
      </div>

      {/* プロジェクト横タブ */}
      {!projectTabsCollapsed && (
        <TaskProjectTabs
          projectTab={projectTab}
          activeProjects={activeProjects}
          allCount={
            tasks.filter(
              (t) => !t.parent_task_id && projectIds.has(t.project_id),
            ).length
          }
          projectTaskCounts={projectTaskCounts}
          projectTabRefs={projectTabRefs}
          onSelectTab={setProjectTabAndSelection}
        />
      )}

      {/* フィルタ + トグル */}
      <TaskListToolbar
        selectedIds={selectedIds}
        bulkLoading={bulkLoading}
        bulkStatusMenuOpen={bulkStatusMenuOpen}
        setBulkStatusMenuOpen={setBulkStatusMenuOpen}
        onBulkStatusChange={handleBulkStatusChange}
        onBulkDuplicate={handleBulkDuplicate}
        onBulkMove={handleBulkMove}
        onBulkDelete={handleBulkDelete}
        clearSelection={clearSelection}
        projects={projects}
        tags={tags}
        filter={filter}
        setFilter={setFilter}
        showClosed={showClosed}
        setShowClosed={setShowClosed}
        showFuture={showFuture}
        setShowFuture={setShowFuture}
        showPriority={showPriority}
        setShowPriority={setShowPriority}
        showOnlyMine={showOnlyMine}
        setShowOnlyMine={setShowOnlyMine}
        filterOpen={filterOpen}
        setFilterOpen={setFilterOpen}
        customFilter={customFilter}
        setCustomFilter={setCustomFilter}
      />

      {/* タスクテーブル */}
      <ScrollArea className="min-h-0 flex-1">
        {loading ? (
          <div className="space-y-2">
            {Array.from({ length: 5 }).map((_, i) => (
              <Skeleton key={i} className="h-10 w-full rounded" />
            ))}
          </div>
        ) : filteredTasks.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-16 text-muted-foreground">
            <p className="text-sm">タスクがありません</p>
          </div>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b text-left text-xs text-muted-foreground">
                <th className="w-8 py-2 pl-2">
                  <Checkbox
                    checked={
                      filteredTasks.length > 0 &&
                      selectedIds.size === filteredTasks.length
                    }
                    onCheckedChange={toggleSelectAll}
                    className="size-3.5"
                    title="全選択"
                  />
                </th>
                <th className="w-8 py-2 pl-0"></th>
                <th className="py-2 pl-2 font-medium">
                  {filteredTasks.length} Tasks
                </th>
                {projectTab === "all" && (
                  <th className="py-2 px-2 font-medium w-32">Project</th>
                )}
                <th className="py-2 px-2 font-medium w-44">Start Date</th>
                <th className="py-2 px-2 font-medium w-44">Due Date</th>
                <th className="py-2 px-2 font-medium w-28">Time tracked</th>
                <th className="w-8"></th>
              </tr>
            </thead>
            <tbody onMouseLeave={() => setHoveredGroupId(null)}>
              {filteredTasks.map((task, taskIndex) => {
                const subtasks = subtaskMap.get(task.id) || [];
                const hasSubtasks = subtasks.length > 0;
                const visibleSubtasks = showClosed
                  ? subtasks
                  : subtasks.filter((s) => s.status !== "closed");
                const isExpanded = expandedTasks.has(task.id);
                const wbsMetadata =
                  task.metadata && typeof task.metadata.wbs === "object"
                    ? (task.metadata.wbs as Record<string, unknown>)
                    : null;

                return (
                  <React.Fragment key={task.id}>
                    <tr
                      ref={(node) => {
                        taskRowRefs.current[task.id] = node;
                      }}
                      data-testid={`task-row-${task.id}`}
                      draggable
                      tabIndex={focusedTaskId === task.id ? 0 : -1}
                      onFocus={() => setFocusedTaskId(task.id)}
                      onDragStart={(e) => handleDragStart(e, task.id)}
                      onDragOver={(e) => handleDragOver(e, task.id)}
                      onDragLeave={handleDragLeave}
                      onDrop={(e) => handleDrop(e, task.id)}
                      onDragEnd={handleDragEnd}
                      onContextMenu={(e) => handleContextMenu(e, task)}
                      onMouseEnter={() => setHoveredGroupId(task.id)}
                      onClick={() => {
                        focusTaskById(task.id);
                        openTask(task);
                      }}
                      title="ドラッグで並び替え / 行の右側に落とすとサブタスク化"
                      className={cn(
                        "group relative border-b border-border/50 cursor-pointer transition-colors hover:bg-accent/70 hover:shadow-sm focus:outline-none",
                        draggingIds.includes(task.id) && "opacity-40",
                        selectedIds.has(task.id) && "bg-primary/5",
                        focusedTaskId === task.id &&
                          "bg-primary/10 outline outline-1 -outline-offset-1 outline-primary/60",
                        cutTaskIds.has(task.id) && "opacity-60",
                      )}
                    >
                      {/* 選択チェックボックス（Shift+クリック範囲選択対応） */}
                      <td
                        className="py-2 pl-2"
                        data-no-drag="true"
                        onClick={(e) => e.stopPropagation()}
                      >
                        <Checkbox
                          checked={selectedIds.has(task.id)}
                          onClick={(e) => {
                            e.stopPropagation();
                            handleCheckboxClick(
                              task.id,
                              taskIndex,
                              e.shiftKey,
                            );
                          }}
                          onCheckedChange={() => {
                            /* tdのonClickで処理 */
                          }}
                          className="size-3.5"
                        />
                        {dropTargetId === task.id &&
                          !draggingIds.includes(task.id) && (
                            <div
                              className={cn(
                                "pointer-events-none absolute z-20 h-[3px] rounded-full bg-blue-500 shadow-[0_0_6px_rgba(59,130,246,0.7)]",
                                dropMode === "reorder-before" &&
                                  "-top-[2px] inset-x-0",
                                dropMode === "reorder-after" &&
                                  "-bottom-[2px] inset-x-0",
                                dropMode === "subtask-before" &&
                                  "-top-[2px] left-16 right-0",
                                dropMode === "subtask-after" &&
                                  "-bottom-[2px] left-16 right-0",
                              )}
                            />
                          )}
                      </td>

                      {/* 展開ボタン + ステータスドット */}
                      <td className="py-2 pl-0">
                        <div className="flex items-center gap-1">
                          {hasSubtasks ? (
                            <button
                              onClick={(e) => {
                                e.stopPropagation();
                                toggleExpand(task.id);
                              }}
                              className="shrink-0 size-5 flex items-center justify-center rounded text-muted-foreground hover:text-foreground hover:bg-muted"
                            >
                              {isExpanded ? (
                                <ChevronDown className="size-3.5" />
                              ) : (
                                <ChevronRight className="size-3.5" />
                              )}
                            </button>
                          ) : (
                            <span className="w-5" />
                          )}
                          <DropdownMenu
                            open={rowStatusMenuTaskId === task.id}
                            onOpenChange={(open) =>
                              handleRowStatusMenuOpenChange(task.id, open)
                            }
                            onOpenChangeComplete={(open) =>
                              handleRowStatusMenuOpenChangeComplete(
                                task.id,
                                open,
                              )
                            }
                          >
                            <DropdownMenuTrigger
                              onClick={(e) => e.stopPropagation()}
                              className={cn(
                                "size-4 shrink-0 rounded-full border-2 transition-colors hover:ring-2 hover:ring-offset-1 hover:ring-primary/30 cursor-pointer",
                                STATUS_DOT_COLORS[task.status] ||
                                  STATUS_DOT_COLORS.open,
                              )}
                              title={STATUS_LABELS[task.status]}
                            />
                            <DropdownMenuContent
                              align="start"
                              className="min-w-36"
                              finalFocus={() =>
                                resolveRowStatusMenuFinalFocus(task.id)
                              }
                              onKeyDownCapture={(e) =>
                                handleStatusShortcutCapture(e, (target) => {
                                  closeRowStatusMenuAndRefocusTask(task.id);
                                  void handleRowStatusChange(task, target);
                                })
                              }
                            >
                              <TaskStatusMenuItems
                                currentStatus={task.status}
                                onSelect={async (status, e) => {
                                  e.stopPropagation();
                                  closeRowStatusMenuAndRefocusTask(task.id);
                                  await handleRowStatusChange(task, status);
                                }}
                              />
                            </DropdownMenuContent>
                          </DropdownMenu>
                        </div>
                      </td>

                      {/* タイトル + タグ + ステータス/優先度バッジ */}
                      <td className="py-2 pl-2">
                        <div className="flex items-center gap-1.5 min-w-0">
                          <span
                            className={cn(
                              "truncate font-medium",
                              task.status === "closed" &&
                                "line-through text-muted-foreground",
                            )}
                          >
                            {task.title}
                          </span>
                          {(task.tags || []).map((tag) => (
                            <TagPill
                              key={tag.id}
                              tag={tag}
                              size="sm"
                              onUpdated={fetchData}
                              onFilter={() => setSearch(tag.name)}
                            />
                          ))}
                          {wbsMetadata && (
                            <Badge
                              variant="outline"
                              className="h-4 shrink-0 px-1.5 text-[10px]"
                              title={`${String(wbsMetadata.sheet_name ?? "WBS")} ${String(wbsMetadata.row_number ?? "")}`}
                            >
                              WBS
                              {wbsMetadata.wbs_id
                                ? ` ${String(wbsMetadata.wbs_id)}`
                                : ""}
                            </Badge>
                          )}
                          {hasSubtasks && (
                            <span className="text-[10px] text-muted-foreground shrink-0">
                              {
                                subtasks.filter((s) => s.status === "closed")
                                  .length
                              }
                              /{subtasks.length}
                            </span>
                          )}
                          {showPriority &&
                            task.priority !== "none" &&
                            task.priority !== "medium" && (
                              <Badge
                                variant="secondary"
                                className={cn(
                                  "text-[10px] px-1.5 h-4 shrink-0",
                                  PRIORITY_COLORS[task.priority],
                                )}
                              >
                                {PRIORITY_LABELS[task.priority] ||
                                  task.priority}
                              </Badge>
                            )}
                          {task.active_time_entry?.started_at && (
                            <div className="flex shrink-0 items-center gap-1 text-xs text-green-600 dark:text-green-400 font-mono">
                              <Timer className="size-3" />
                              <span>
                                {formatElapsed(
                                  task.active_time_entry.started_at,
                                  now,
                                )}
                              </span>
                            </div>
                          )}
                        </div>
                      </td>

                      {/* プロジェクト名（全体表示時のみ）— クリックでインライン編集 */}
                      {projectTab === "all" && (
                        <td
                          className="py-2 px-2"
                          onClick={(e) => e.stopPropagation()}
                        >
                          <select
                            value={task.project_id}
                            onChange={async (e) => {
                              const newProjectId = e.target.value;
                              if (newProjectId === task.project_id) return;
                              try {
                                pushUndo({
                                  type: "update",
                                  taskId: task.id,
                                  previous: { project_id: task.project_id },
                                });
                                const updatedTask = await taskApi.moveTask(
                                  task.id,
                                  {
                                    project_id: newProjectId,
                                  },
                                );
                                applyTaskPatchLocally(task.id, updatedTask);
                              } catch (err) {
                                console.error("プロジェクト変更失敗:", err);
                              }
                            }}
                            className="text-xs text-muted-foreground bg-transparent border-none outline-none cursor-pointer max-w-28 truncate hover:text-foreground"
                          >
                            {projects.map((p) => (
                              <option key={p.id} value={p.id}>
                                {p.name}
                              </option>
                            ))}
                          </select>
                        </td>
                      )}

                      {/* 日程 — 開始日と期限を同じポップオーバーで編集 */}
                      <td
                        className="py-2 px-2 text-xs whitespace-nowrap"
                        colSpan={2}
                        onClick={(e) => e.stopPropagation()}
                      >
                        <div className="flex items-center gap-1">
                          {task.has_recurrence && (
                            <Repeat
                              className="size-3 shrink-0 text-muted-foreground"
                              aria-label="繰り返しタスク"
                            />
                          )}
                          <TaskRowDatePicker
                            taskId={task.id}
                            startAt={toLocalDateTimeInputValue(
                              getTaskDisplayStartAt(task),
                              { allDay: getTaskDisplayAllDay(task) },
                            )}
                            endAt={toLocalDateTimeInputValue(
                              getTaskDisplayEndAt(task),
                              {
                                allDay: getTaskDisplayAllDay(task),
                              },
                            )}
                            onRangeChange={({ startAt, endAt }) =>
                              handleTaskDateChange(task, {
                                start_at: startAt,
                                end_at: endAt,
                              })
                            }
                            onRecurrenceChange={(hasRecurrence) =>
                              applyTaskPatchLocally(task.id, {
                                has_recurrence: hasRecurrence,
                              })
                            }
                            allDay={getTaskDisplayAllDay(task)}
                            startPlaceholder="Start Date"
                            endPlaceholder="Due Date"
                            startButtonClassName={dateButtonColor(
                              getTaskDisplayStartAt(task),
                              task,
                              "start",
                            )}
                            endButtonClassName={dateButtonColor(
                              getTaskDisplayEndAt(task),
                              task,
                              "end",
                            )}
                          />
                        </div>
                      </td>

                      {/* 記録時間 */}
                      <td className="py-2 px-2 text-xs text-muted-foreground whitespace-nowrap">
                        {(task.total_time_seconds ?? 0) > 0 && (
                          <div className="flex items-center gap-1">
                            <Clock className="size-3" />
                            <span>
                              {formatDuration(task.total_time_seconds ?? 0)}
                            </span>
                          </div>
                        )}
                      </td>

                      {/* タイマーボタン */}
                      <td className="py-2 pr-2">
                        <Button
                          variant="ghost"
                          size="icon-xs"
                          onClick={(e) => handleTimer(task, e)}
                          disabled={timerLoading === task.id}
                          className={cn(
                            "shrink-0",
                            task.active_time_entry && "text-green-600",
                          )}
                        >
                          {task.active_time_entry ? (
                            <Square className="size-3" />
                          ) : (
                            <Play className="size-3" />
                          )}
                        </Button>
                      </td>
                    </tr>

                    {/* サブタスク行 */}
                    {isExpanded &&
                      visibleSubtasks.map((sub) => (
                        <SubtaskRow
                          key={sub.id}
                          sub={sub}
                          parentTask={task}
                          projectTab={projectTab}
                          draggingIds={draggingIds}
                          dropTargetId={dropTargetId}
                          dropMode={dropMode}
                          onDragStart={handleDragStart}
                          onDragOver={handleDragOver}
                          onDragLeave={handleDragLeave}
                          onDrop={handleDrop}
                          onDragEnd={handleDragEnd}
                          setHoveredGroupId={setHoveredGroupId}
                          openTask={openTask}
                          onContextMenu={handleContextMenu}
                          pushUndo={pushUndo}
                          fetchData={fetchData}
                          handleTaskDateChange={handleTaskDateChange}
                          applyTaskPatchLocally={applyTaskPatchLocally}
                          requestRecurringDelete={requestRecurringDelete}
                        />
                      ))}

                    {/* サブタスク追加行 — 既にサブタスクがあり、かつグループに hover 中 or 入力中のみ表示 */}
                    {isExpanded &&
                      hasSubtasks &&
                      (hoveredGroupId === task.id ||
                        subtaskAddParentId === task.id) && (
                        <SubtaskAddRow
                          task={task}
                          colSpan={projectTab === "all" ? 7 : 6}
                          setHoveredGroupId={setHoveredGroupId}
                          subtaskAddParentId={subtaskAddParentId}
                          setSubtaskAddParentId={setSubtaskAddParentId}
                          subtaskAddTitle={subtaskAddTitle}
                          setSubtaskAddTitle={setSubtaskAddTitle}
                          subtaskAddCreating={subtaskAddCreating}
                          subtaskAddRef={subtaskAddRef}
                          onSubmit={handleSubtaskAdd}
                        />
                      )}
                  </React.Fragment>
                );
              })}
              {/* インラインQuickAdd行 */}
              <QuickAddRow
                colSpan={projectTab === "all" ? 7 : 6}
                projectTab={projectTab}
                selectedProjectId={selectedProjectId}
                upsertTaskLocally={upsertTaskLocally}
                fetchData={fetchData}
              />
            </tbody>
          </table>
        )}
      </ScrollArea>

      <TaskCommandDialog
        open={taskCommandOpen}
        onClose={closeTaskCommandDialog}
        taskCommandTaskId={taskCommandTaskId}
        tasks={tasks}
        value={taskCommandValue}
        onValueChange={setTaskCommandValue}
        error={taskCommandError}
        onErrorClear={() => setTaskCommandError(null)}
        loading={taskCommandLoading}
        commandCandidates={taskCommandCandidates}
        onSubmit={handleTaskCommandSubmit}
      />

      {/* タスク詳細モーダル */}
      <TaskDetailModal
        taskId={selectedTaskId}
        draftTask={draftTask}
        open={!!selectedTaskId || !!draftTask}
        onOpenChange={(open) => {
          if (open) return;
          setSelectedTaskId(null);
          setSelectedOccurrenceContext(null);
          setDraftTask(null);
        }}
        onTaskUpdated={() => fetchData({ forceLoading: false })}
        occurrenceContext={selectedOccurrenceContext}
      />
      <RecurringDeleteDialog
        open={!!pendingRecurringDelete}
        onOpenChange={(open) => {
          if (!open) setPendingRecurringDelete(null);
        }}
        onDeleteSingle={() => void handleRecurringDelete("single")}
        onDeleteFuture={() => void handleRecurringDelete("future")}
      />

      {/* 右クリックコンテキストメニュー */}
      <TaskListContextMenu
        contextMenu={contextMenu}
        contextMenuRef={contextMenuRef}
        contextMenuStyle={contextMenuStyle}
        contextSubmenuClassName={contextSubmenuClassName}
        statusSubmenuOpen={statusSubmenuOpen}
        setStatusSubmenuOpen={setStatusSubmenuOpen}
        prioritySubmenuOpen={prioritySubmenuOpen}
        setPrioritySubmenuOpen={setPrioritySubmenuOpen}
        onStatusChange={handleContextStatusChange}
        onPriorityChange={handleContextPriorityChange}
        onTimer={handleContextTimer}
        onDuplicate={handleDuplicate}
        onCopyTaskId={handleCopyTaskId}
        onDelete={handleContextDelete}
      />
    </div>
  );
}
