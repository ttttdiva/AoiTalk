"use client";

import { AppSelect } from "@/components/ui/app-select";
import React, {
  useState,
  useEffect,
  useCallback,
  useMemo,
  useRef,
} from "react";

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
  Play,
  Square,
  Timer,
  ChevronRight,
  ChevronDown,
  Clock,
  Plus,
  Repeat,
} from "lucide-react";
import { toast } from "sonner";
import {
  taskApi,
  type Project,
  type RecurringOccurrenceContext,
  type Task,
} from "@/lib/task-api";
import { RecurringDeleteDialog } from "@/components/tasks/task-detail/recurring-delete-dialog";
import { TagPill } from "@/components/tasks/tag-pill";
import {
  applyTaskFilter,
  type FilterConfig,
} from "@/components/tasks/task-filter-builder";
import { TaskRowDatePicker } from "@/components/tasks/task-row-date-picker";
import { toLocalDateTimeInputValue } from "@/lib/date-time";
import { formatTaskDateLabel } from "@/lib/task-date-label";
import { cn } from "@/lib/utils";
import { useProject } from "@/contexts/project-context";
import {
  getTaskDisplayAllDay,
  getTaskDisplayEndAt,
  getTaskDisplayStartAt,
} from "@/lib/task-effective-date";
import {
  buildTaskDateUpdate,
  dateButtonColor,
  formatDuration,
  formatElapsed,
  getTaskDateView,
  getTaskDisplayStatus,
  getTaskOccurrenceContext,
  applyTaskTimerStart,
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
import { useTaskCommandDialog } from "@/components/tasks/hooks/use-task-command-dialog";
import { useTaskListKeyboard } from "@/components/tasks/hooks/use-task-list-keyboard";
import {
  TaskStatusMenuItems,
  type TaskStatusOption,
} from "@/components/tasks/task-status-menu-items";
import { TaskListContextMenu } from "@/components/tasks/task-list-context-menu";
import { TaskCommandDialog } from "@/components/tasks/task-command-dialog";
import { TaskListToolbar } from "@/components/tasks/task-list-toolbar";
import {
  SubtaskAddRow,
  SubtaskRow,
} from "@/components/tasks/task-list-rows";
import {
  DEFAULT_TASK_COLUMN_WIDTHS,
  DEFAULT_TASK_COLUMN_VISIBILITY,
  TASK_LIST_COLUMN_MIN_WIDTHS,
  TASK_LIST_COLUMN_MAX_WIDTHS,
  type TaskListColumnWidths,
  type TaskListResizableColumn,
  type TaskListColumn,
  type TaskListColumnVisibility,
} from "@/components/tasks/hooks/use-task-view-preferences";

function compactDurationLabel(seconds: number): string {
  const label = formatDuration(seconds).replace(/\s+/g, "");
  return label || "--";
}

function compactElapsedLabel(startedAt: string, now: number): string {
  const [hours, minutes, seconds] = formatElapsed(startedAt, now)
    .split(":")
    .map((value) => Number(value));
  if (hours > 0) return `${hours}h${minutes}m`;
  if (minutes > 0) return `${minutes}m`;
  return `${seconds}s`;
}

// The first three columns are fixed controls (selection, hierarchy, status).
// Keep their total width stable so adding the hierarchy toggle never widens
// the table, while leaving enough room for the checkbox's left padding.
export const TASK_LIST_SELECTION_COLUMN_WIDTH = 40;
export const TASK_LIST_EXPAND_COLUMN_WIDTH = 24;
export const TASK_LIST_STATUS_COLUMN_WIDTH = 24;
export const TASK_LIST_STATIC_COLUMN_WIDTH =
  TASK_LIST_SELECTION_COLUMN_WIDTH +
  TASK_LIST_EXPAND_COLUMN_WIDTH +
  TASK_LIST_STATUS_COLUMN_WIDTH;

export type TaskListTableLayoutInput = {
  columnWidths: TaskListColumnWidths;
  showProjectColumn: boolean;
  showStartColumn: boolean;
  showDueColumn: boolean;
  showPriorityColumn: boolean;
  showAssigneeColumn: boolean;
  showTimeColumn: boolean;
  wrapperWidth?: number | null;
};

export type TaskListTableLayout = {
  baseWidth: number;
  tableWidth: number;
  renderedTaskNameWidth: number;
};

/**
 * Keep the table's saved widths stable while allowing only Task Name to fill
 * spare desktop space.  A zero/unknown wrapper width deliberately falls back
 * to the intrinsic base width so SSR and jsdom renders remain deterministic.
 */
export function calculateTaskListTableLayout({
  columnWidths,
  showProjectColumn,
  showStartColumn,
  showDueColumn,
  showPriorityColumn,
  showAssigneeColumn,
  showTimeColumn,
  wrapperWidth,
}: TaskListTableLayoutInput): TaskListTableLayout {
  const baseWidth =
    TASK_LIST_STATIC_COLUMN_WIDTH +
    columnWidths.taskName +
    (showProjectColumn ? columnWidths.project : 0) +
    (showStartColumn ? columnWidths.start : 0) +
    (showDueColumn ? columnWidths.due : 0) +
    (showPriorityColumn ? columnWidths.priority : 0) +
    (showAssigneeColumn ? columnWidths.assignee : 0) +
    (showTimeColumn ? columnWidths.time : 0);
  const measuredWidth =
    typeof wrapperWidth === "number" && Number.isFinite(wrapperWidth)
      ? Math.max(0, wrapperWidth)
      : 0;
  const tableWidth = Math.max(baseWidth, measuredWidth);
  return {
    baseWidth,
    tableWidth,
    renderedTaskNameWidth:
      columnWidths.taskName + Math.max(0, measuredWidth - baseWidth),
  };
}

type MobileTaskCardProps = {
  task: Task;
  taskIndex: number;
  subtasks: Task[];
  displayStatus: string;
  wbsMetadata: Record<string, unknown> | null;
  selected: boolean;
  focused: boolean;
  showPriority: boolean;
  readOnly: boolean;
  now: number;
  timerLoading: string | null;
  statusMenuOpen: boolean;
  onOpen: () => void;
  onFocus: () => void;
  onSelect: (event: React.MouseEvent) => void;
  onStatusMenuOpenChange: (open: boolean) => void;
  onStatusMenuClose: () => void;
  onStatusChange: (status: TaskStatusOption) => Promise<void>;
  onTagUpdated: () => void;
  onTagFilter: (tagName: string) => void;
  onTimer: (event: React.MouseEvent) => void;
};

function MobileTaskCard({
  task,
  subtasks,
  displayStatus,
  wbsMetadata,
  selected,
  focused,
  showPriority,
  readOnly,
  now,
  timerLoading,
  statusMenuOpen,
  onOpen,
  onFocus,
  onSelect,
  onStatusMenuOpenChange,
  onStatusMenuClose,
  onStatusChange,
  onTagUpdated,
  onTagFilter,
  onTimer,
}: MobileTaskCardProps) {
  const startAt = getTaskDisplayStartAt(task);
  const endAt = getTaskDisplayEndAt(task);
  const dateLabel =
    startAt && endAt ? `${startAt} / ${endAt}` : (startAt ?? endAt ?? null);
  const closedSubtasks = subtasks.filter(
    (subtask) => getTaskDisplayStatus(subtask) === "closed",
  ).length;

  const handleKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    onOpen();
  };

  return (
    <article
      data-testid={`mobile-task-card-${task.id}`}
      className={cn(
        "ao-task-card group rounded border border-transparent bg-card/40 px-3 py-3 transition-colors",
        "hover:border-border hover:bg-muted/60",
        focused && "is-focused ring-1 ring-primary/60",
        selected && "is-selected border-primary/60 bg-primary/5",
        displayStatus === "closed" && "opacity-80",
      )}
    >
      <div className="flex items-start gap-2.5">
        <div
          className="flex shrink-0 flex-col items-center gap-2 pt-0.5"
          onClick={(event) => event.stopPropagation()}
        >
          {readOnly ? (
            <span
              className={cn(
                "size-5 shrink-0 rounded-full border-2",
                STATUS_DOT_COLORS[displayStatus] || STATUS_DOT_COLORS.open,
              )}
              title={STATUS_LABELS[displayStatus]}
            />
          ) : (
            <DropdownMenu
              open={statusMenuOpen}
              onOpenChange={onStatusMenuOpenChange}
            >
              <DropdownMenuTrigger
                className={cn(
                  "size-5 shrink-0 rounded-full border-2 transition-colors hover:ring-2 hover:ring-primary/30",
                  STATUS_DOT_COLORS[displayStatus] || STATUS_DOT_COLORS.open,
                )}
                title={STATUS_LABELS[displayStatus]}
                aria-label={`${task.title}のステータスを変更`}
              />
              <DropdownMenuContent align="start" className="min-w-36">
                <TaskStatusMenuItems
                  currentStatus={displayStatus}
                  onSelect={(status, event) => {
                    event.stopPropagation();
                    onStatusMenuClose();
                    void onStatusChange(status);
                  }}
                />
              </DropdownMenuContent>
            </DropdownMenu>
          )}
          <Checkbox
            disabled={readOnly}
            checked={selected}
            onClick={(event) => {
              event.stopPropagation();
              onSelect(event);
            }}
            onCheckedChange={() => {
              /* onClickで範囲選択を処理する */
            }}
            className="size-4"
            aria-label={`${task.title}を選択`}
          />
        </div>

        <div
          role="button"
          tabIndex={0}
          onFocus={onFocus}
          onClick={onOpen}
          onKeyDown={handleKeyDown}
          className="min-w-0 flex-1 cursor-pointer outline-none"
          aria-label={`${task.title}を開く`}
        >
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0 flex-1">
              <p
                className={cn(
                  "line-clamp-2 text-sm font-semibold leading-5 text-foreground",
                  displayStatus === "closed" &&
                    "text-muted-foreground line-through",
                )}
              >
                {task.title}
              </p>
              {(dateLabel || task.project_name) && (
                <div className="mt-1 flex min-w-0 items-center gap-2 text-xs text-muted-foreground">
                  {dateLabel && (
                    <span className="inline-flex min-w-0 items-center gap-1 truncate">
                      <Clock className="size-3 shrink-0" />
                      {dateLabel}
                    </span>
                  )}
                  {task.project_name && (
                    <span className="truncate">{task.project_name}</span>
                  )}
                </div>
              )}
            </div>

            {(task.tags?.length ?? 0) > 0 && (
              <div
                className="flex max-w-[48%] shrink-0 flex-wrap justify-end gap-1"
                onClick={(event) => event.stopPropagation()}
              >
                {task.tags.slice(0, 2).map((tag) =>
                  readOnly ? (
                    <span
                      key={tag.id}
                      className="h-[18px] rounded px-1.5 text-[10px] font-medium text-white"
                      style={{ backgroundColor: tag.color || "#6B7280" }}
                    >
                      {tag.name}
                    </span>
                  ) : (
                    <TagPill
                      key={tag.id}
                      tag={tag}
                      size="sm"
                      onUpdated={onTagUpdated}
                      onFilter={() => onTagFilter(tag.name)}
                    />
                  ),
                )}
                {task.tags.length > 2 && (
                  <span className="self-center text-[10px] font-semibold text-muted-foreground">
                    +{task.tags.length - 2}
                  </span>
                )}
              </div>
            )}
          </div>

          <div className="mt-2 flex min-h-5 items-center gap-1.5 text-[10px] text-muted-foreground">
            {subtasks.length > 0 && (
              <Badge variant="secondary" className="h-5 px-1.5 text-[10px]">
                {closedSubtasks}/{subtasks.length} 子タスク
              </Badge>
            )}
            {wbsMetadata && (
              <Badge variant="outline" className="h-5 px-1.5 text-[10px]">
                WBS
                {wbsMetadata.wbs_id ? ` ${String(wbsMetadata.wbs_id)}` : ""}
              </Badge>
            )}
            {showPriority &&
              task.priority !== "none" &&
              task.priority !== "medium" && (
                <Badge
                  variant="secondary"
                  className={cn(
                    "h-5 px-1.5 text-[10px]",
                    PRIORITY_COLORS[task.priority],
                  )}
                >
                  {PRIORITY_LABELS[task.priority] || task.priority}
                </Badge>
              )}
            {task.active_time_entry?.started_at && (
              <span className="inline-flex items-center gap-1 font-mono text-green-600 dark:text-green-400">
                <Timer className="size-3" />
                {formatElapsed(task.active_time_entry.started_at, now)}
              </span>
            )}
            {!readOnly && (
              <Button
                variant="ghost"
                size="icon-xs"
                onClick={onTimer}
                disabled={timerLoading === task.id}
                className={cn(
                  "ml-auto shrink-0 text-muted-foreground",
                  task.active_time_entry &&
                    "text-green-600 dark:text-green-400",
                )}
                aria-label={
                  task.active_time_entry ? "タイマー停止" : "タイマー開始"
                }
              >
                {task.active_time_entry ? (
                  <Square className="size-3" />
                ) : (
                  <Play className="size-3" />
                )}
              </Button>
            )}
          </div>
        </div>
      </div>
    </article>
  );
}

export function isTaskListTaskReadOnly(
  task: Task,
  projectById: ReadonlyMap<string, Pick<Project, "source" | "can_write">>,
  selectedProjectReadOnly = false,
): boolean {
  const project = projectById.get(task.project_id);
  return (
    selectedProjectReadOnly ||
    !project ||
    task.source === "remote" ||
    project?.source === "remote" ||
    project?.can_write === false
  );
}

type TaskListViewProps = {
  projectContext: ReturnType<typeof useProject>;
  taskData: ReturnType<typeof useTasksData>;
  appFilterId: string;
  appTaskIds: ReadonlySet<string>;
  filterState: TaskListFilterState;
  projectTab: string;
  cycleProjectTab: (direction: 1 | -1) => void;
  searchInputRef: React.RefObject<HTMLInputElement | null>;
  handleCreateNewTask: () => void;
  columnVisibility?: TaskListColumnVisibility;
  onColumnVisibilityChange?: (column: TaskListColumn, visible: boolean) => void;
  columnWidths?: TaskListColumnWidths;
  onColumnWidthChange?: (
    column: TaskListResizableColumn,
    width: number,
  ) => void;
  selectedTaskId: string | null;
  draftTask: Partial<Task> | null;
  openTask: (task: Task) => void;
  openTaskById: (
    taskId: string,
    occurrenceContext?: RecurringOccurrenceContext | null,
  ) => void;
};

type TaskListFilterState = {
  filter: FilterTab;
  setFilter: React.Dispatch<React.SetStateAction<FilterTab>>;
  showClosed: boolean;
  setShowClosed: React.Dispatch<React.SetStateAction<boolean>>;
  showFuture: boolean;
  setShowFuture: React.Dispatch<React.SetStateAction<boolean>>;
  showOnlyMine: boolean;
  setShowOnlyMine: React.Dispatch<React.SetStateAction<boolean>>;
  customFilter: FilterConfig;
  setCustomFilter: React.Dispatch<React.SetStateAction<FilterConfig>>;
  filterOpen: boolean;
  setFilterOpen: React.Dispatch<React.SetStateAction<boolean>>;
  search: string;
  setSearch: React.Dispatch<React.SetStateAction<string>>;
  currentUserId: string | null;
};

export function TaskListView({
  projectContext,
  taskData,
  appFilterId,
  appTaskIds,
  filterState,
  projectTab,
  cycleProjectTab,
  searchInputRef,
  handleCreateNewTask,
  columnVisibility = DEFAULT_TASK_COLUMN_VISIBILITY,
  onColumnVisibilityChange = () => undefined,
  columnWidths = DEFAULT_TASK_COLUMN_WIDTHS,
  onColumnWidthChange = () => undefined,
  selectedTaskId,
  draftTask,
  openTask,
  openTaskById,
}: TaskListViewProps) {
  // Search has two viewport-specific controls. Keep the refs separate so the
  // desktop toolbar cannot overwrite the visible mobile input (Ctrl/Cmd+F
  // resolves the active viewport at keydown time).
  const desktopSearchInputRef = useRef<HTMLInputElement>(null);
  const keyboardSearchInputRef = useMemo<
    React.RefObject<HTMLInputElement | null>
  >(() => {
    const ref = {} as React.RefObject<HTMLInputElement | null>;
    Object.defineProperty(ref, "current", {
      enumerable: true,
      get: () => {
        const desktop =
          typeof window !== "undefined" &&
          typeof window.matchMedia === "function" &&
          window.matchMedia("(min-width: 768px)").matches;
        return desktop
          ? desktopSearchInputRef.current
          : searchInputRef.current;
      },
    });
    return ref;
  }, [searchInputRef]);
  const {
    projects,
    allProjects,
    spaces,
    selectedProjectId,
    selectedProject,
    refreshProjects,
    projectsLoading,
    projectsLoadError,
  } = projectContext;
  const remoteReadOnly =
    selectedProject?.source === "remote" ||
    selectedProject?.can_write === false;
  const scopeReadOnly = projectTab !== "all" && remoteReadOnly;
  const projectAccessById = useMemo(
    () =>
      new Map(
        [...allProjects, ...projects].map((project) => [project.id, project]),
      ),
    [allProjects, projects],
  );
  const isTaskReadOnly = useCallback(
    (task: Task) =>
      isTaskListTaskReadOnly(task, projectAccessById, scopeReadOnly),
    [projectAccessById, scopeReadOnly],
  );
  const {
    tasks,
    setTasks,
    tags,
    setTags,
    loading,
    loadError,
    fetchData,
    upsertTaskLocally,
    removeTaskLocally,
    applyTaskPatchLocally,
    applyTaskPatchesLocally,
    applyTopLevelReorderLocally,
    applyAllTopLevelReorderLocally,
  } = taskData;
  const writableTasks = useMemo(
    () => tasks.filter((task) => !isTaskReadOnly(task)),
    [isTaskReadOnly, tasks],
  );
  const writableProjectIds = useMemo(
    () =>
      new Set(
        projects
          .filter(
            (project) =>
              project.source !== "remote" && project.can_write !== false,
          )
          .map((project) => project.id),
      ),
    [projects],
  );
  const {
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
  } = filterState;
  // Priority used to be an inline toolbar toggle. Keep the state for the
  // mobile compatibility path while desktop column visibility is persisted in
  // tasks-view-preferences.
  const [showPriority, setShowPriority] = useState(false);
  const [timerLoading, setTimerLoading] = useState<string | null>(null);
  const [bulkStatusMenuOpen, setBulkStatusMenuOpen] = useState(false);
  // モバイルカードとデスクトップ行はCSSで同時にmountされるため、状態を共有すると
  // 非表示側のPortalまで開いて左上にメニューが表示される。開閉状態を分離する。
  const [rowStatusMenuTaskId, setRowStatusMenuTaskId] = useState<string | null>(
    null,
  );
  const [mobileRowStatusMenuTaskId, setMobileRowStatusMenuTaskId] = useState<
    string | null
  >(null);
  const [bulkLoading, setBulkLoading] = useState(false);
  const taskRowRefs = useRef<Record<string, HTMLTableRowElement | null>>({});
  const pendingRowStatusFocusTaskIdRef = useRef<string | null>(null);
  const [focusedTaskId, setFocusedTaskId] = useState<string | null>(null);
  const projectIds = useMemo(
    () => new Set(projects.map((project) => project.id)),
    [projects],
  );
  const retryInitialData = useCallback(() => {
    void Promise.all([fetchData({ forceLoading: true }), refreshProjects()]);
  }, [fetchData, refreshProjects]);

  // filteredTasks を ref で保持（Shift+クリック範囲選択・DnD 用）
  const filteredTasksRef = useRef<Task[]>([]);
  const selectableTasksRef = useRef<Task[]>([]);

  const {
    selectedIds,
    setSelectedIds,
    selectedIdsRef,
    lastClickedIndexRef,
    prevShiftRangeRef,
    handleCheckboxClick: handleSelectableCheckboxClick,
    clearSelection,
    toggleSelectAll: toggleSelectAllWritable,
  } = useTaskSelection({ filteredTasksRef: selectableTasksRef });

  const { pushUndo, snapshotTask, queueTaskCompletionUndo } = useTaskUndo({
    tasks,
    fetchData,
  });

  const [pendingRecurringDelete, setPendingRecurringDelete] = useState<{
    task: Task;
    occurrenceContext: RecurringOccurrenceContext;
  } | null>(null);
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
    async (mode: "single" | "future" | "series") => {
      if (!pendingRecurringDelete) return;
      if (isTaskReadOnly(pendingRecurringDelete.task)) {
        toast.error("Enterprise参照は読み取り専用です");
        return;
      }
      const { task, occurrenceContext } = pendingRecurringDelete;
      try {
        if (mode === "series") {
          // 繰り返しタスク本体ごと削除する（全発生回が消える）
          await taskApi.deleteTask(task.id);
        } else {
          await taskApi.deleteOccurrence(task.id, {
            mode,
            occurrence_id: occurrenceContext.occurrence_id ?? null,
            occurrence_start_at: occurrenceContext.start_at,
            occurrence_end_at: occurrenceContext.end_at ?? null,
            original_start_at: occurrenceContext.original_start_at ?? null,
          });
        }
        setPendingRecurringDelete(null);
        clearSelection();
        await fetchData();
      } catch (err) {
        console.error("繰り返しタスク削除失敗:", err);
        toast.error("繰り返しタスクの削除に失敗しました", {
          description: err instanceof Error ? err.message : undefined,
        });
      }
    },
    [clearSelection, fetchData, isTaskReadOnly, pendingRecurringDelete],
  );
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

  // キーボード操作からはトグルではなく、展開状態を明示的に更新する。
  // 展開・折りたたみはトップレベルの親行だけが対象で、サブタスク行からは
  // 親グループの状態を暗黙に変更しない。
  const setTaskExpanded = useCallback(
    (taskId: string, expanded: boolean) => {
      const task = tasks.find((item) => item.id === taskId);
      if (task?.parent_task_id) return;
      setExpandedTasks((prev) => {
        const alreadyExpanded = prev.has(taskId);
        if (alreadyExpanded === expanded) return prev;
        const next = new Set(prev);
        if (expanded) next.add(taskId);
        else next.delete(taskId);
        return next;
      });
    },
    [tasks],
  );

  const handleTaskDateChange = useCallback(
    async (
      task: Task,
      changes: { start_at?: string | null; end_at?: string | null },
    ) => {
      if (isTaskReadOnly(task)) {
        toast.error("Enterprise参照は読み取り専用です");
        return;
      }
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
        const nextEndAt = hasEndUpdate
          ? updates.end_at
          : (task.effective_occurrence_end_at ?? null);
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
    [applyTaskPatchLocally, fetchData, isTaskReadOnly, pushUndo, snapshotTask],
  );

  // タイマー操作
  const handleTimer = useCallback(
    async (task: Task, e: React.MouseEvent) => {
      e.stopPropagation();
      if (isTaskReadOnly(task)) {
        toast.error("Enterprise参照ではタイマーを操作できません");
        return;
      }
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
              detail: { activeEntry: null, taskId: task.id },
            }),
          );
        } else {
          const started = await taskApi.startTimer(
            task.id,
            task.effective_occurrence_id ?? undefined,
          );
          setTasks((prev) =>
            prev.map((item) =>
              item.id === task.id ? applyTaskTimerStart(item, started) : item,
            ),
          );
          window.dispatchEvent(
            new CustomEvent("timer-changed", {
              detail: { activeEntry: started },
            }),
          );
        }
      } catch (err) {
        console.error("タイマー操作失敗:", err);
      } finally {
        setTimerLoading(null);
      }
    },
    [isTaskReadOnly, setTasks],
  );

  // フォーカス中タスクのタイマー開始
  const handleFocusedTaskTimerStart = useCallback(async () => {
    if (!focusedTaskId) return;
    const task = tasks.find((item) => item.id === focusedTaskId);
    if (!task || task.active_time_entry) return;
    if (isTaskReadOnly(task)) {
      toast.error("Enterprise参照ではタイマーを操作できません");
      return;
    }

    setTimerLoading(task.id);
    try {
      const started = await taskApi.startTimer(
        task.id,
        task.effective_occurrence_id ?? undefined,
      );
      setTasks((prev) =>
        prev.map((item) =>
          item.id === task.id ? applyTaskTimerStart(item, started) : item,
        ),
      );
      window.dispatchEvent(
        new CustomEvent("timer-changed", {
          detail: { activeEntry: started, taskId: task.id },
        }),
      );
    } catch (err) {
      console.error("Focused task timer start failed:", err);
    } finally {
      setTimerLoading(null);
    }
  }, [focusedTaskId, isTaskReadOnly, setTasks, tasks]);

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
    handleDragStart: handleWritableDragStart,
    handleDragOver: handleWritableDragOver,
    handleDragLeave,
    handleDrop: handleWritableDrop,
    handleDragEnd: handleWritableDragEnd,
  } = useTaskDnd({
    tasks: writableTasks,
    projectIds: writableProjectIds,
    projectTab,
    fetchData,
    applyTaskPatchesLocally,
    applyTopLevelReorderLocally,
    applyAllTopLevelReorderLocally,
    setExpandedTasks,
    filteredTasksRef: selectableTasksRef,
    selectedIdsRef,
  });
  const activeDragTaskIdsRef = useRef<string[]>([]);
  const handleDragStart = useCallback(
    (event: React.DragEvent, taskId: string) => {
      const candidateIds = selectedIdsRef.current.has(taskId)
        ? filteredTasksRef.current
            .filter((task) => selectedIdsRef.current.has(task.id))
            .map((task) => task.id)
        : [taskId];
      const candidateTasks = candidateIds
        .map((id) => tasks.find((task) => task.id === id))
        .filter((task): task is Task => !!task);
      if (
        candidateTasks.length !== candidateIds.length ||
        candidateTasks.some(isTaskReadOnly)
      ) {
        event.preventDefault();
        toast.error("読み取り専用タスクは移動できません");
        activeDragTaskIdsRef.current = [];
        return;
      }
      activeDragTaskIdsRef.current = candidateIds;
      handleWritableDragStart(event, taskId);
    },
    [handleWritableDragStart, isTaskReadOnly, selectedIdsRef, tasks],
  );
  const handleDragOver = useCallback(
    (event: React.DragEvent, taskId: string) => {
      const target = tasks.find((task) => task.id === taskId);
      if (
        activeDragTaskIdsRef.current.length === 0 ||
        !target ||
        isTaskReadOnly(target)
      ) {
        event.dataTransfer.dropEffect = "none";
        return;
      }
      handleWritableDragOver(event, taskId);
    },
    [handleWritableDragOver, isTaskReadOnly, tasks],
  );
  const handleDrop = useCallback(
    async (event: React.DragEvent, taskId: string) => {
      const target = tasks.find((task) => task.id === taskId);
      const draggedTasks = activeDragTaskIdsRef.current
        .map((id) => tasks.find((task) => task.id === id))
        .filter((task): task is Task => !!task);
      if (
        activeDragTaskIdsRef.current.length === 0 ||
        !target ||
        isTaskReadOnly(target) ||
        draggedTasks.length !== activeDragTaskIdsRef.current.length ||
        draggedTasks.some(isTaskReadOnly)
      ) {
        event.preventDefault();
        toast.error("読み取り専用タスクは移動できません");
        activeDragTaskIdsRef.current = [];
        handleWritableDragEnd();
        return;
      }
      try {
        await handleWritableDrop(event, taskId);
      } finally {
        activeDragTaskIdsRef.current = [];
      }
    },
    [handleWritableDragEnd, handleWritableDrop, isTaskReadOnly, tasks],
  );
  const handleDragEnd = useCallback(() => {
    activeDragTaskIdsRef.current = [];
    handleWritableDragEnd();
  }, [handleWritableDragEnd]);

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
    handleContextMenu: handleWritableContextMenu,
    handleContextStatusChange: handleWritableContextStatusChange,
    handleContextPriorityChange: handleWritableContextPriorityChange,
    handleContextTimer: handleWritableContextTimer,
    handleDuplicate: handleWritableDuplicate,
    handleCopyTaskId,
    handleContextDelete: handleWritableContextDelete,
  } = useTaskContextMenu({
    pushUndo,
    queueTaskCompletionUndo,
    applyTaskPatchLocally,
    upsertTaskLocally,
    removeTaskLocally,
    setSelectedIds,
    refreshTasks: fetchData,
    requestRecurringDelete,
  });
  const handleContextMenu = useCallback(
    (event: React.MouseEvent, task: Task) => {
      if (isTaskReadOnly(task)) {
        event.preventDefault();
        return;
      }
      handleWritableContextMenu(event, task);
    },
    [handleWritableContextMenu, isTaskReadOnly],
  );
  const contextTaskIsReadOnly =
    !!contextMenu && isTaskReadOnly(contextMenu.task);
  const handleContextStatusChange = useCallback(
    async (status: string) => {
      if (contextTaskIsReadOnly) return;
      await handleWritableContextStatusChange(status);
    },
    [contextTaskIsReadOnly, handleWritableContextStatusChange],
  );
  const handleContextPriorityChange = useCallback(
    async (priority: string) => {
      if (contextTaskIsReadOnly) return;
      await handleWritableContextPriorityChange(priority);
    },
    [contextTaskIsReadOnly, handleWritableContextPriorityChange],
  );
  const handleContextTimer = useCallback(async () => {
    if (contextTaskIsReadOnly) return;
    await handleWritableContextTimer();
  }, [contextTaskIsReadOnly, handleWritableContextTimer]);
  const handleDuplicate = useCallback(async () => {
    if (contextTaskIsReadOnly) return;
    await handleWritableDuplicate();
  }, [contextTaskIsReadOnly, handleWritableDuplicate]);
  const handleContextDelete = useCallback(async () => {
    if (contextTaskIsReadOnly) return;
    await handleWritableContextDelete();
  }, [contextTaskIsReadOnly, handleWritableContextDelete]);

  // プロジェクト名マップ
  const projectMap = useMemo(
    () => new Map(allProjects.map((p) => [p.id, p.name])),
    [allProjects],
  );
  const showProjectColumn = projectTab === "all" || columnVisibility.project;
  const showStartColumn = columnVisibility.start;
  const showDueColumn = columnVisibility.due;
  const showTimeColumn = columnVisibility.time;
  const showPriorityColumn = columnVisibility.priority;
  const showAssigneeColumn = columnVisibility.assignee;
  const desktopColumnCount =
    4 +
    Number(showProjectColumn) +
    Number(showStartColumn) +
    Number(showDueColumn) +
    Number(showPriorityColumn) +
    Number(showAssigneeColumn) +
    Number(showTimeColumn);

  const [localColumnWidths, setLocalColumnWidths] =
    useState<TaskListColumnWidths>(columnWidths);
  const localColumnWidthsRef = useRef(localColumnWidths);
  const resizeCleanupRef = useRef<(() => void) | null>(null);
  const desktopTableWrapperRef = useRef<HTMLDivElement | null>(null);
  const [desktopTableWrapperNode, setDesktopTableWrapperNode] =
    useState<HTMLDivElement | null>(null);
  const setDesktopTableWrapperRef = useCallback((node: HTMLDivElement | null) => {
    desktopTableWrapperRef.current = node;
    setDesktopTableWrapperNode(node);
  }, []);
  const [desktopWrapperWidth, setDesktopWrapperWidth] = useState(0);
  const [resizingColumn, setResizingColumn] =
    useState<TaskListResizableColumn | null>(null);

  useEffect(() => {
    localColumnWidthsRef.current = localColumnWidths;
  }, [localColumnWidths]);
  useEffect(() => {
    setLocalColumnWidths(columnWidths);
    localColumnWidthsRef.current = columnWidths;
  }, [columnWidths]);
  useEffect(() => {
    const wrapper = desktopTableWrapperNode;
    if (!wrapper) {
      setDesktopWrapperWidth((current) => (current === 0 ? current : 0));
      return;
    }
    let active = true;

    const measure = (contentWidth?: number) => {
      if (!active) return;
      const clientWidth = wrapper.clientWidth;
      const nextWidth =
        clientWidth > 0
          ? clientWidth
          : typeof contentWidth === "number" &&
              Number.isFinite(contentWidth) &&
              contentWidth > 0
            ? contentWidth
            : 0;
      setDesktopWrapperWidth((current) =>
        current === nextWidth ? current : nextWidth,
      );
    };

    measure();
    const handleWindowResize = () => measure();
    window.addEventListener("resize", handleWindowResize);

    let observer: ResizeObserver | null = null;
    if (typeof ResizeObserver !== "undefined") {
      observer = new ResizeObserver((entries) => {
        const contentWidth = entries[0]?.contentRect?.width;
        measure(contentWidth);
      });
      observer.observe(wrapper);
    }

    return () => {
      active = false;
      observer?.disconnect();
      window.removeEventListener("resize", handleWindowResize);
    };
  }, [
    showAssigneeColumn,
    showDueColumn,
    showPriorityColumn,
    showProjectColumn,
    showStartColumn,
    showTimeColumn,
    desktopTableWrapperNode,
  ]);
  useEffect(
    () => () => {
      resizeCleanupRef.current?.();
      resizeCleanupRef.current = null;
    },
    [],
  );

  // Keep user-selected pixel widths as actual CSS widths.  `w-full` on a
  // table-fixed table distributes spare viewport space across every <col>,
  // turning a 104px Time Tracked default into 150px+ on wide screens.  The
  // wrapper owns horizontal scrolling when this exact table is wider than
  // the viewport instead.
  const desktopTableLayout = calculateTaskListTableLayout({
    columnWidths: localColumnWidths,
    showProjectColumn,
    showStartColumn,
    showDueColumn,
    showPriorityColumn,
    showAssigneeColumn,
    showTimeColumn,
    wrapperWidth: desktopWrapperWidth,
  });
  const desktopTableWidth = desktopTableLayout.tableWidth;
  const renderedTaskNameWidth = desktopTableLayout.renderedTaskNameWidth;

  const beginColumnResize = useCallback(
    (column: TaskListResizableColumn, event: React.PointerEvent) => {
      event.preventDefault();
      event.stopPropagation();
      resizeCleanupRef.current?.();

      const startX = event.clientX;
      const startWidth = localColumnWidthsRef.current[column];
      setResizingColumn(column);

      const handlePointerMove = (moveEvent: PointerEvent) => {
        const nextWidth = Math.min(
          TASK_LIST_COLUMN_MAX_WIDTHS[column],
          Math.max(
            TASK_LIST_COLUMN_MIN_WIDTHS[column],
            Math.round(startWidth + moveEvent.clientX - startX),
          ),
        );
        setLocalColumnWidths((current) => ({
          ...current,
          [column]: nextWidth,
        }));
        localColumnWidthsRef.current = {
          ...localColumnWidthsRef.current,
          [column]: nextWidth,
        };
      };
      let finished = false;
      const finishResize = (commit: boolean, revert = true) => {
        if (finished) return;
        finished = true;
        window.removeEventListener("pointermove", handlePointerMove);
        window.removeEventListener("pointerup", handlePointerUp);
        window.removeEventListener("pointercancel", handlePointerCancel);
        window.removeEventListener("blur", handleWindowBlur);
        resizeCleanupRef.current = null;
        setResizingColumn(null);
        if (commit) {
          onColumnWidthChange(column, localColumnWidthsRef.current[column]);
        } else if (revert) {
          setLocalColumnWidths((current) => ({
            ...current,
            [column]: startWidth,
          }));
          localColumnWidthsRef.current = {
            ...localColumnWidthsRef.current,
            [column]: startWidth,
          };
        }
      };
      const handlePointerUp = () => finishResize(true);
      const handlePointerCancel = () => finishResize(false);
      const handleWindowBlur = () => finishResize(false);
      // Unmount cleanup only removes listeners; pointercancel/blur also
      // restores the pre-drag width so a cancelled gesture is not persisted.
      resizeCleanupRef.current = () => finishResize(false, false);
      window.addEventListener("pointermove", handlePointerMove);
      window.addEventListener("pointerup", handlePointerUp, { once: true });
      window.addEventListener("pointercancel", handlePointerCancel);
      window.addEventListener("blur", handleWindowBlur);
    },
    [onColumnWidthChange],
  );

  const renderResizableHeader = useCallback(
    (
      column: TaskListResizableColumn,
      label: string,
      className = "",
    ) => (
      <th
        key={column}
        className={cn(
          "relative whitespace-nowrap px-2 py-2 font-medium",
          className,
        )}
      >
        {label}
        <span
          role="separator"
          aria-orientation="vertical"
          aria-label={`${label}列幅を変更`}
          data-column-resizer={column}
          onPointerDown={(event) => beginColumnResize(column, event)}
          className={cn(
            "absolute inset-y-0 right-0 z-20 w-2 cursor-col-resize touch-none select-none",
            "after:absolute after:inset-y-1.5 after:left-1/2 after:w-px after:-translate-x-1/2 after:bg-border/70",
            "hover:bg-primary/15 hover:after:bg-primary dark:after:bg-border",
            resizingColumn === column && "bg-primary/20 after:bg-primary",
          )}
        />
      </th>
    ),
    [beginColumnResize, resizingColumn],
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
        setMobileRowStatusMenuTaskId(null);
      }
      setRowStatusMenuTaskId(open ? taskId : null);
    },
    [],
  );

  const handleMobileRowStatusMenuOpenChange = useCallback(
    (taskId: string, open: boolean) => {
      if (open) {
        pendingRowStatusFocusTaskIdRef.current = null;
        setRowStatusMenuTaskId(null);
      }
      setMobileRowStatusMenuTaskId(open ? taskId : null);
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
    openTaskCommandDialog: openWritableTaskCommandDialog,
    handleTaskCommandSubmit: handleWritableTaskCommandSubmit,
  } = useTaskCommandDialog({
    tasks,
    tags,
    setTags,
    projects: allProjects.filter(
      (project) =>
        !project.is_completed &&
        project.source !== "remote" &&
        project.can_write !== false,
    ),
    spaces,
    selectedProjectId,
    fetchData,
    focusTaskById,
    pushUndo,
    queueTaskCompletionUndo,
    applyTaskPatchLocally,
    upsertTaskLocally,
  });
  const openTaskCommandDialog = useCallback(
    (taskId: string) => {
      const task = tasks.find((item) => item.id === taskId);
      if (!task || isTaskReadOnly(task)) return;
      openWritableTaskCommandDialog(taskId);
    },
    [isTaskReadOnly, openWritableTaskCommandDialog, tasks],
  );
  const handleTaskCommandSubmit = useCallback(
    async (raw: string, selectedTargetProjectId?: string) => {
      const task = tasks.find((item) => item.id === taskCommandTaskId);
      if (!task || isTaskReadOnly(task)) {
        toast.error("読み取り専用タスクは変更できません");
        return raw;
      }
      if (selectedTargetProjectId) {
        const targetProject = projectAccessById.get(selectedTargetProjectId);
        if (
          !targetProject ||
          targetProject.source === "remote" ||
          targetProject.can_write === false
        ) {
          toast.error("読み取り専用プロジェクトへは移動できません");
          return raw;
        }
      }
      return handleWritableTaskCommandSubmit(raw, selectedTargetProjectId);
    },
    [
      handleWritableTaskCommandSubmit,
      isTaskReadOnly,
      projectAccessById,
      taskCommandTaskId,
      tasks,
    ],
  );

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
      if (isTaskReadOnly(parentTask)) {
        toast.error("Enterprise参照は読み取り専用です");
        return;
      }
      setSubtaskAddCreating(true);
      try {
        const created = await taskApi.createTask({
          project_id: parentTask.project_id,
          title: subtaskAddTitle.trim(),
          parent_task_id: parentTask.id,
        });
        // 作成レスポンスをキャッシュへ反映（楽観的更新）。全量再取得はしない。
        upsertTaskLocally(created);
        setSubtaskAddTitle("");
      } catch (err) {
        console.error("サブタスク作成失敗:", err);
      } finally {
        setSubtaskAddCreating(false);
      }
    },
    [isTaskReadOnly, subtaskAddTitle, upsertTaskLocally],
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

    // App詳細から開いた場合は、正式な task_app_links の結果だけを表示
    // する。取得失敗時に全タスクへフォールバックしない。
    if (appFilterId) {
      result = result.filter((task) => appTaskIds.has(task.id));
    }

    // プロジェクトタブフィルタ
    if (projectTab !== "all") {
      result = result.filter((t) => t.project_id === projectTab);
    } else {
      result = result.filter((t) => projectIds.has(t.project_id));
    }

    // トグル: 完了済みを非表示（デフォルト）
    if (!showClosed) {
      result = result.filter((t) => getTaskDisplayStatus(t) !== "closed");
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
    appFilterId,
    appTaskIds,
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

  // キーボード移動は、展開された親の直後に表示されるサブタスクも
  // 1 行ずつ辿れるよう、デスクトップの表示順と同じフラットな配列を使う。
  // フィルタ済みの親だけを起点にするため、非表示の親配下のサブタスクや
  // showClosed=false で隠れているサブタスクは移動対象に含めない。
  const keyboardTasks = useMemo(() => {
    const rows: Task[] = [];
    for (const task of filteredTasks) {
      rows.push(task);
      if (!expandedTasks.has(task.id)) continue;
      const subtasks = subtaskMap.get(task.id) || [];
      rows.push(
        ...(showClosed
          ? subtasks
          : subtasks.filter(
              (subtask) => getTaskDisplayStatus(subtask) !== "closed",
            )),
      );
    }
    return rows;
  }, [expandedTasks, filteredTasks, showClosed, subtaskMap]);

  filteredTasksRef.current = filteredTasks;
  const writableFilteredTasks = useMemo(
    () => filteredTasks.filter((task) => !isTaskReadOnly(task)),
    [filteredTasks, isTaskReadOnly],
  );
  selectableTasksRef.current = writableFilteredTasks;
  const writableTaskIndexById = useMemo(
    () => new Map(writableFilteredTasks.map((task, index) => [task.id, index])),
    [writableFilteredTasks],
  );
  const getTaskExpansionState = useCallback(
    (taskId: string) => {
      const task = tasks.find((item) => item.id === taskId);
      if (task?.parent_task_id) {
        return { hasSubtasks: false, expanded: false };
      }
      return {
        hasSubtasks: (subtaskMap.get(taskId)?.length ?? 0) > 0,
        expanded: expandedTasks.has(taskId),
      };
    },
    [expandedTasks, subtaskMap, tasks],
  );
  const handleKeyboardRangeSelection = useCallback(
    (taskIds: string[]) => {
      const eligibleIds = new Set(writableFilteredTasks.map((task) => task.id));
      const rangeIds = [...new Set(taskIds)].filter((taskId) =>
        eligibleIds.has(taskId),
      );
      if (rangeIds.length === 0) return;

      // 範囲選択は一度の state 更新で行い、mouse Shift+クリック用の
      // lastClickedIndex/prevShiftRange refs は意図的に変更しない。
      setSelectedIds((current) => {
        const allSelected = rangeIds.every((taskId) => current.has(taskId));
        const next = new Set(current);
        for (const taskId of rangeIds) {
          if (allSelected) next.delete(taskId);
          else next.add(taskId);
        }
        return next;
      });
    },
    [setSelectedIds, writableFilteredTasks],
  );
  const writableSelectedIds = useMemo(
    () =>
      new Set(
        [...selectedIds].filter((taskId) => {
          const task = tasks.find((item) => item.id === taskId);
          return !!task && !isTaskReadOnly(task);
        }),
      ),
    [isTaskReadOnly, selectedIds, tasks],
  );
  useEffect(() => {
    setSelectedIds((current) => {
      const next = new Set(
        [...current].filter((taskId) => writableSelectedIds.has(taskId)),
      );
      return next.size === current.size ? current : next;
    });
  }, [setSelectedIds, writableSelectedIds]);
  const handleCheckboxClick = useCallback(
    (taskId: string, _taskIndex: number, shiftKey: boolean) => {
      const task = tasks.find((item) => item.id === taskId);
      if (!task) return;
      if (isTaskReadOnly(task)) return;
      const taskIndex = writableTaskIndexById.get(task.id);
      if (taskIndex === undefined) return;
      handleSelectableCheckboxClick(task.id, taskIndex, shiftKey);
    },
    [
      handleSelectableCheckboxClick,
      isTaskReadOnly,
      tasks,
      writableTaskIndexById,
    ],
  );

  const getKeyboardSelectionTasks = useCallback(() => {
    if (selectedIds.size > 0) {
      return filteredTasks
        .filter((task) => selectedIds.has(task.id) && !isTaskReadOnly(task))
        .map((task) => tasks.find((item) => item.id === task.id) || task);
    }

    if (!focusedTaskId) return [];
    const focusedTask = tasks.find((task) => task.id === focusedTaskId);
    return focusedTask && !isTaskReadOnly(focusedTask) ? [focusedTask] : [];
  }, [filteredTasks, focusedTaskId, isTaskReadOnly, selectedIds, tasks]);

  // クリップボード（Ctrl+C / X / V）
  const {
    clipboardRef,
    cutTaskIds,
    setCutTaskIds,
    handleClipboardStore: handleWritableClipboardStore,
    handleClipboardPaste: handleWritableClipboardPaste,
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
    readOnly: scopeReadOnly,
  });
  const handleClipboardStore = useCallback(
    (mode: Parameters<typeof handleWritableClipboardStore>[0]) => {
      handleWritableClipboardStore(mode);
    },
    [handleWritableClipboardStore],
  );
  const handleClipboardPaste = useCallback(async () => {
    const focusedTask = tasks.find((task) => task.id === focusedTaskId);
    if (!focusedTask || isTaskReadOnly(focusedTask)) return;
    await handleWritableClipboardPaste();
  }, [focusedTaskId, handleWritableClipboardPaste, isTaskReadOnly, tasks]);

  // 一括操作
  const {
    handleRowStatusChange: handleWritableRowStatusChange,
    handleBulkStatusChange,
    handleBulkDelete,
    handleBulkDuplicate,
    handleBulkMove: handleWritableBulkMove,
    handleDeleteTasks: handleWritableDeleteTasks,
  } = useBulkTaskActions({
    tasks,
    setTasks,
    selectedIds: writableSelectedIds,
    setSelectedIds,
    clearSelection,
    pushUndo,
    queueTaskCompletionUndo,
    applyTaskPatchLocally,
    upsertTaskLocally,
    setBulkLoading,
    setCutTaskIds,
    focusedTaskId,
    focusTaskById,
    filteredTasksRef,
    refreshTasks: fetchData,
    requestRecurringDelete,
  });
  const handleRowStatusChange = useCallback(
    async (task: Task, status: string) => {
      if (isTaskReadOnly(task)) return;
      await handleWritableRowStatusChange(task, status);
    },
    [handleWritableRowStatusChange, isTaskReadOnly],
  );
  const handleBulkMove = useCallback(
    async (targetProjectId: string) => {
      const targetProject = projectAccessById.get(targetProjectId);
      if (
        !targetProject ||
        targetProject.source === "remote" ||
        targetProject.can_write === false
      ) {
        toast.error("読み取り専用プロジェクトへは移動できません");
        return;
      }
      await handleWritableBulkMove(targetProjectId);
    },
    [handleWritableBulkMove, projectAccessById],
  );
  const handleDeleteTasks = useCallback(
    async (taskList: Task[]) => {
      if (taskList.some(isTaskReadOnly)) return;
      await handleWritableDeleteTasks(taskList);
    },
    [handleWritableDeleteTasks, isTaskReadOnly],
  );

  useEffect(() => {
    if (keyboardTasks.length === 0) {
      setFocusedTaskId(null);
      return;
    }

    if (
      focusedTaskId &&
      keyboardTasks.some((task) => task.id === focusedTaskId)
    ) {
      return;
    }

    setFocusedTaskId(keyboardTasks[0].id);
  }, [focusedTaskId, keyboardTasks]);

  // グローバルキーボードショートカット
  const { rangeFocusedIds } = useTaskListKeyboard({
    tasks,
    filteredTasks,
    keyboardTasks,
    rangeTasks: writableFilteredTasks,
    getTaskExpansionState,
    setTaskExpanded,
    handleKeyboardRangeSelection,
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
    searchInputRef: keyboardSearchInputRef,
    readOnly: scopeReadOnly,
  });

  return (
    <div className="ao-task-list-view relative flex h-full min-h-0 flex-col gap-0 bg-card dark:bg-background">
      {/* フィルタ + トグル */}
      <TaskListToolbar
        selectedIds={writableSelectedIds}
        readOnly={scopeReadOnly}
        bulkLoading={bulkLoading}
        bulkStatusMenuOpen={bulkStatusMenuOpen}
        setBulkStatusMenuOpen={setBulkStatusMenuOpen}
        onBulkStatusChange={handleBulkStatusChange}
        onBulkDuplicate={handleBulkDuplicate}
        onBulkMove={handleBulkMove}
        onBulkDelete={handleBulkDelete}
        clearSelection={clearSelection}
        // Bulk Move must remain writable-only, while Advanced Filter needs to
        // list every readable project (including remote/read-only entries).
        projects={projects.filter(
          (project) =>
            project.source !== "remote" && project.can_write !== false,
        )}
        filterProjects={allProjects}
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
         searchInputRef={desktopSearchInputRef}
         search={search}
         setSearch={setSearch}
         onCreateTask={handleCreateNewTask}
         createDisabled={remoteReadOnly}
         columnVisibility={columnVisibility}
         onColumnVisibilityChange={onColumnVisibilityChange}
         projectScopeAll={projectTab === "all"}
       />
      <p className="px-3 py-2 text-xs text-muted-foreground md:hidden">
        タップで開く / チェックで複数選択
      </p>

      {/* タスクテーブル */}
      <ScrollArea className="min-h-0 flex-1">
        {(loadError || projectsLoadError) &&
        tasks.length > 0 &&
        projects.length > 0 ? (
          <div
            className="mb-3 flex items-center justify-between rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:border-amber-800 dark:bg-amber-950/40 dark:text-amber-100"
            role="alert"
          >
            <span>
              最新の一覧を取得できませんでした。前回のデータを表示しています。
            </span>
            <Button variant="outline" size="sm" onClick={retryInitialData}>
              再試行
            </Button>
          </div>
        ) : null}
        {loading || (projectsLoading && projects.length === 0) ? (
          <div className="space-y-1 p-3">
            {Array.from({ length: 5 }).map((_, i) => (
              <Skeleton
                key={i}
                className="h-14 w-full rounded md:h-10 md:rounded"
              />
            ))}
          </div>
        ) : (loadError || projectsLoadError) &&
          (tasks.length === 0 || projects.length === 0) ? (
          <div
            className="flex flex-col items-center justify-center gap-3 py-16 text-muted-foreground"
            role="alert"
          >
            <p className="text-sm">タスクを取得できませんでした</p>
            <p className="text-xs">
              通信状態を確認して、もう一度お試しください。
            </p>
            <Button variant="outline" size="sm" onClick={retryInitialData}>
              再試行
            </Button>
          </div>
        ) : filteredTasks.length === 0 ? (
          <div className="flex flex-col items-center justify-center p-8 text-muted-foreground">
            <div className="w-full max-w-md rounded border border-dashed border-border px-8 py-10 text-center">
              <p className="text-sm text-foreground">タスクがありません</p>
              <p className="mt-1 text-xs">
                検索条件やフィルタを調整してください。
              </p>
            </div>
          </div>
        ) : (
          <>
            <div className="space-y-1.5 p-3 pb-20 md:hidden">
              {filteredTasks.map((task, taskIndex) => {
                const subtasks = subtaskMap.get(task.id) || [];
                const taskReadOnly = isTaskReadOnly(task);
                const wbsMetadata =
                  task.metadata && typeof task.metadata.wbs === "object"
                    ? (task.metadata.wbs as Record<string, unknown>)
                    : null;

                return (
                  <MobileTaskCard
                    key={task.id}
                    task={task}
                    taskIndex={taskIndex}
                    subtasks={subtasks}
                    displayStatus={getTaskDisplayStatus(task)}
                    wbsMetadata={wbsMetadata}
                    selected={selectedIds.has(task.id)}
                    focused={focusedTaskId === task.id}
                    showPriority={showPriority}
                    readOnly={taskReadOnly}
                    now={now}
                    timerLoading={timerLoading}
                    statusMenuOpen={mobileRowStatusMenuTaskId === task.id}
                    onOpen={() => {
                      focusTaskById(task.id);
                      openTask(task);
                    }}
                    onFocus={() => setFocusedTaskId(task.id)}
                    onSelect={(event) =>
                      handleCheckboxClick(task.id, taskIndex, event.shiftKey)
                    }
                    onStatusMenuOpenChange={(open) =>
                      handleMobileRowStatusMenuOpenChange(task.id, open)
                    }
                    onStatusMenuClose={() => setMobileRowStatusMenuTaskId(null)}
                    onStatusChange={(status) =>
                      handleRowStatusChange(task, status)
                    }
                    onTagUpdated={fetchData}
                    onTagFilter={setSearch}
                    onTimer={(event) => {
                      void handleTimer(task, event);
                    }}
                  />
                );
              })}
            </div>
            {/* table-fixed: タスク名が長くても後続列の幅を動かさない。
                データ列はヘッダー境界のドラッグで変更でき、min-w より狭いときは
                列を潰さず横スクロールへ逃がす。 */}
            <div
              ref={setDesktopTableWrapperRef}
              className="hidden overflow-x-auto md:block"
              data-testid="task-list-table-wrapper"
            >
              <table
                className={cn(
                  "ao-task-list-table table-fixed text-[13px]",
                  resizingColumn && "select-none",
                )}
                style={{
                  width: `${desktopTableWidth}px`,
                  minWidth: `${desktopTableWidth}px`,
                }}
                data-testid="task-list-table"
              >
                <colgroup>
                  <col
                    style={{
                      width: `${TASK_LIST_SELECTION_COLUMN_WIDTH}px`,
                      minWidth: `${TASK_LIST_SELECTION_COLUMN_WIDTH}px`,
                    }}
                  />
                  <col
                    style={{
                      width: `${TASK_LIST_EXPAND_COLUMN_WIDTH}px`,
                      minWidth: `${TASK_LIST_EXPAND_COLUMN_WIDTH}px`,
                    }}
                  />
                  <col
                    style={{
                      width: `${TASK_LIST_STATUS_COLUMN_WIDTH}px`,
                      minWidth: `${TASK_LIST_STATUS_COLUMN_WIDTH}px`,
                    }}
                  />
                  <col
                    style={{
                      width: `${renderedTaskNameWidth}px`,
                      minWidth: `${renderedTaskNameWidth}px`,
                    }}
                    data-column-width={localColumnWidths.taskName}
                  />
                  {showProjectColumn && (
                    <col
                      style={{
                        width: `${localColumnWidths.project}px`,
                        minWidth: `${localColumnWidths.project}px`,
                      }}
                    />
                  )}
                  {showStartColumn && (
                    <col
                      style={{
                        width: `${localColumnWidths.start}px`,
                        minWidth: `${localColumnWidths.start}px`,
                      }}
                    />
                  )}
                  {showDueColumn && (
                    <col
                      style={{
                        width: `${localColumnWidths.due}px`,
                        minWidth: `${localColumnWidths.due}px`,
                      }}
                    />
                  )}
                  {showPriorityColumn && (
                    <col
                      style={{
                        width: `${localColumnWidths.priority}px`,
                        minWidth: `${localColumnWidths.priority}px`,
                      }}
                    />
                  )}
                  {showAssigneeColumn && (
                    <col
                      style={{
                        width: `${localColumnWidths.assignee}px`,
                        minWidth: `${localColumnWidths.assignee}px`,
                      }}
                    />
                  )}
                  {showTimeColumn && (
                    <col
                      style={{
                        width: `${localColumnWidths.time}px`,
                        minWidth: `${localColumnWidths.time}px`,
                      }}
                    />
                  )}
                </colgroup>
                <thead>
                  <tr className="sticky top-0 z-10 h-9 border-b border-border bg-card text-left text-xs uppercase tracking-[0.06em] text-muted-foreground">
                    <th className="py-2 pl-6">
                      <Checkbox
                        checked={
                          writableFilteredTasks.length > 0 &&
                          writableSelectedIds.size ===
                            writableFilteredTasks.length
                        }
                        onCheckedChange={toggleSelectAllWritable}
                        disabled={
                          scopeReadOnly || writableFilteredTasks.length === 0
                        }
                        className="size-3.5"
                        title="全選択"
                      />
                    </th>
                    <th className="py-2 text-center font-medium"></th>
                    <th className="py-2 text-center font-medium"></th>
                    {renderResizableHeader("taskName", "Task Name", "pl-0")}
                    {showProjectColumn &&
                      renderResizableHeader("project", "Project")}
                    {showStartColumn &&
                      renderResizableHeader("start", "Start Date")}
                    {showDueColumn && renderResizableHeader("due", "Due Date")}
                    {showPriorityColumn &&
                      renderResizableHeader("priority", "Priority")}
                    {showAssigneeColumn &&
                      renderResizableHeader("assignee", "Assignee")}
                    {showTimeColumn &&
                      renderResizableHeader("time", "Time Tracked", "text-left")}
                  </tr>
                </thead>
                <tbody onMouseLeave={() => setHoveredGroupId(null)}>
                  {filteredTasks.map((task, taskIndex) => {
                    const subtasks = subtaskMap.get(task.id) || [];
                    const taskReadOnly = isTaskReadOnly(task);
                    const hasSubtasks = subtasks.length > 0;
                    const visibleSubtasks = showClosed
                      ? subtasks
                      : subtasks.filter(
                          (s) => getTaskDisplayStatus(s) !== "closed",
                        );
                    const isExpanded = expandedTasks.has(task.id);
                    const displayStatus = getTaskDisplayStatus(task);
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
                          draggable={!taskReadOnly}
                          tabIndex={focusedTaskId === task.id ? 0 : -1}
                          onFocus={() => setFocusedTaskId(task.id)}
                          onDragStart={(e) => handleDragStart(e, task.id)}
                          onDragOver={(e) => handleDragOver(e, task.id)}
                          onDragLeave={handleDragLeave}
                          onDrop={(e) => handleDrop(e, task.id)}
                          onDragEnd={handleDragEnd}
                          onContextMenu={(e) => {
                            if (!taskReadOnly) handleContextMenu(e, task);
                          }}
                          onMouseEnter={() => setHoveredGroupId(task.id)}
                          onClick={() => {
                            focusTaskById(task.id);
                            openTask(task);
                          }}
                          title="ドラッグで並び替え / 行の右側に落とすとサブタスク化"
                          className={cn(
                             "ao-task-row group relative h-11 cursor-pointer border-b border-border/60 transition-colors hover:bg-card/70 focus:outline-none",
                             draggingIds.includes(task.id) && "opacity-40",
                             taskReadOnly && "opacity-60",
                            selectedIds.has(task.id) && "is-selected bg-primary/5",
                            rangeFocusedIds.has(task.id) &&
                              "is-range-focused bg-primary/5 outline outline-1 -outline-offset-1 outline-primary/30",
                            focusedTaskId === task.id &&
                              "is-focused bg-primary/10 outline outline-1 -outline-offset-1 outline-primary/60",
                            cutTaskIds.has(task.id) && "opacity-60",
                          )}
                        >
                          {/* 選択チェックボックス（Shift+クリック範囲選択対応） */}
                            <td
                              className="h-11 py-0 pl-6"
                            data-no-drag="true"
                            onClick={(e) => e.stopPropagation()}
                          >
                            <Checkbox
                              disabled={taskReadOnly}
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

                           {/* 展開ボタン */}
                            <td className="h-11 py-0 text-center">
                             {hasSubtasks ? (
                                 <button
                                  type="button"
                                  aria-label={`${isExpanded ? "サブタスクを折りたたむ" : "サブタスクを展開"}`}
                                  aria-expanded={isExpanded}
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    toggleExpand(task.id);
                                  }}
                                  className="relative z-10 shrink-0 size-5 flex items-center justify-center rounded text-muted-foreground hover:text-foreground hover:bg-muted"
                                >
                                  {isExpanded ? (
                                    <ChevronDown className="size-3.5" />
                                  ) : (
                                    <ChevronRight className="size-3.5" />
                                  )}
                                </button>
                             ) : (
                               <span className="inline-block w-5" />
                             )}
                           </td>

                           {/* ステータスドット */}
                            <td className="h-11 py-0 text-center">
                             <div className="flex items-center justify-center">
                               {taskReadOnly ? (
                                <span
                                  className={cn(
                                    "size-4 shrink-0 rounded-full border-2",
                                    STATUS_DOT_COLORS[displayStatus] ||
                                      STATUS_DOT_COLORS.open,
                                  )}
                                  title={STATUS_LABELS[displayStatus]}
                                />
                              ) : (
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
                                      STATUS_DOT_COLORS[displayStatus] ||
                                        STATUS_DOT_COLORS.open,
                                    )}
                                    title={STATUS_LABELS[displayStatus]}
                                  />
                                  <DropdownMenuContent
                                    align="start"
                                    className="min-w-36"
                                    finalFocus={() =>
                                      resolveRowStatusMenuFinalFocus(task.id)
                                    }
                                  >
                                    <TaskStatusMenuItems
                                      currentStatus={displayStatus}
                                      onSelect={async (status, e) => {
                                        e.stopPropagation();
                                        closeRowStatusMenuAndRefocusTask(
                                          task.id,
                                        );
                                        await handleRowStatusChange(
                                          task,
                                          status,
                                        );
                                      }}
                                    />
                                  </DropdownMenuContent>
                                </DropdownMenu>
                               )}
                             </div>
                           </td>

                           {/* タイトル + タグ + ステータス/優先度バッジ */}
                            <td className="h-11 overflow-hidden py-0 pl-0 pr-4">
                            <div className="flex items-center gap-1.5 min-w-0">
                              <span
                                className={cn(
                                  "truncate text-sm font-medium leading-5",
                                  displayStatus === "closed" &&
                                    "line-through text-muted-foreground",
                                )}
                                title={task.title}
                              >
                                {task.title}
                              </span>
                              {task.source === "remote" && (
                                <span
                                  className="shrink-0 text-[11px] text-muted-foreground"
                                  title="Remote task (read only)"
                                >
                                  ☁
                                </span>
                              )}
                              <span className="flex min-w-0 max-w-[38%] items-center gap-1 overflow-hidden">
                                {(task.tags || []).slice(0, 3).map((tag) =>
                                  taskReadOnly ? (
                                    <span
                                      key={tag.id}
                                      className="h-[18px] max-w-24 truncate shrink-0 rounded px-1.5 text-[10px] font-medium text-white"
                                      style={{
                                        backgroundColor: tag.color || "#6B7280",
                                      }}
                                    >
                                      {tag.name}
                                    </span>
                                  ) : (
                                    <TagPill
                                      key={tag.id}
                                      tag={tag}
                                      size="sm"
                                      onUpdated={fetchData}
                                      onFilter={() => setSearch(tag.name)}
                                    />
                                  ),
                                )}
                                {(task.tags?.length ?? 0) > 3 && (
                                  <span className="shrink-0 text-[10px] text-muted-foreground">
                                    +{task.tags.length - 3}
                                  </span>
                                )}
                              </span>
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
                                    subtasks.filter(
                                      (s) => s.status === "closed",
                                    ).length
                                  }
                                  /{subtasks.length}
                                </span>
                              )}
                              {showPriority &&
                                !showPriorityColumn &&
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
                            </div>
                          </td>

                          {/* プロジェクト名（全体表示時のみ）— クリックでインライン編集 */}
                           {showProjectColumn && (
                             <td
                               className="h-11 truncate px-2 py-0"
                              onClick={(e) => e.stopPropagation()}
                            >
                              <AppSelect
                                value={task.project_id}
                                disabled={taskReadOnly}
                                onChange={async (e) => {
                                  const newProjectId = e.target.value;
                                  if (newProjectId === task.project_id) return;
                                  if (taskReadOnly) {
                                    toast.error(
                                      "Enterprise参照は読み取り専用です",
                                    );
                                    return;
                                  }
                                  const targetProject =
                                    projectAccessById.get(newProjectId);
                                  if (
                                    !targetProject ||
                                    targetProject.source === "remote" ||
                                    targetProject.can_write === false
                                  ) {
                                    toast.error(
                                      "読み取り専用プロジェクトへは移動できません",
                                    );
                                    return;
                                  }
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
                                 className="ao-task-project-select max-w-28 cursor-pointer truncate border-none bg-transparent text-[13px] leading-5 text-muted-foreground outline-none hover:text-foreground"
                              >
                                {projects.map((p) => (
                                  <option
                                    key={p.id}
                                    value={p.id}
                                    disabled={
                                      p.source === "remote" ||
                                      p.can_write === false
                                    }
                                  >
                                    {p.name}
                                  </option>
                                ))}
                              </AppSelect>
                            </td>
                          )}

                           {/* 日程 — Start / Due を独立列で表示し、各セルから同じ範囲を編集 */}
                           {showStartColumn && (
                             <td
                               className="h-11 whitespace-nowrap px-2 py-1 text-[13px] leading-5"
                               onClick={(e) => e.stopPropagation()}
                             >
                               <div className="flex min-w-0 items-center gap-1">
                                  {task.has_recurrence && (
                                    <Repeat
                                      className="size-3 shrink-0 text-muted-foreground"
                                      aria-label="繰り返しタスク"
                                    />
                                  )}
                                  {taskReadOnly ? (
                                    <span
                                      className="min-w-0 flex-1 whitespace-nowrap text-[13px] leading-5 text-muted-foreground"
                                     title={
                                       formatTaskDateLabel(
                                         getTaskDisplayStartAt(task),
                                         { allDay: getTaskDisplayAllDay(task) },
                                       ) || "-"
                                     }
                                   >
                                     {formatTaskDateLabel(
                                       getTaskDisplayStartAt(task),
                                       { allDay: getTaskDisplayAllDay(task) },
                                     ) || "-"}
                                   </span>
                                  ) : (
                                    <div className="min-w-0 flex-1">
                                      <TaskRowDatePicker
                                        taskId={task.id}
                                        startAt={toLocalDateTimeInputValue(
                                          getTaskDisplayStartAt(task),
                                          { allDay: getTaskDisplayAllDay(task) },
                                        )}
                                        endAt={toLocalDateTimeInputValue(
                                          getTaskDisplayEndAt(task),
                                          { allDay: getTaskDisplayAllDay(task) },
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
                                        startButtonClassName={cn(
                                          "ao-task-date-button h-8 w-full min-w-0 px-1.5 text-[13px] leading-5",
                                          dateButtonColor(
                                            getTaskDisplayStartAt(task),
                                            task,
                                            "start",
                                          ),
                                        )}
                                        endButtonClassName={cn(
                                          "ao-task-date-button h-8 w-full min-w-0 px-1.5 text-[13px] leading-5",
                                          dateButtonColor(
                                            getTaskDisplayEndAt(task),
                                            task,
                                            "end",
                                          ),
                                        )}
                                        showStartDate
                                        showEndDate={false}
                                      />
                                    </div>
                                  )}
                               </div>
                             </td>
                           )}
                           {showDueColumn && (
                             <td
                             className="h-11 whitespace-nowrap px-2 py-1 text-[13px] leading-5"
                               onClick={(e) => e.stopPropagation()}
                             >
                               <div className="flex min-w-0 items-center gap-1">
                                 {task.has_recurrence && !showStartColumn && (
                                   <Repeat
                                     className="size-3 shrink-0 text-muted-foreground"
                                     aria-label="繰り返しタスク"
                                   />
                                 )}
                                  {taskReadOnly ? (
                                    <span
                                      className="min-w-0 flex-1 block whitespace-nowrap text-[13px] leading-5 text-muted-foreground"
                                     title={
                                       formatTaskDateLabel(
                                         getTaskDisplayEndAt(task),
                                         { allDay: getTaskDisplayAllDay(task) },
                                       ) || "-"
                                     }
                                   >
                                     {formatTaskDateLabel(
                                       getTaskDisplayEndAt(task),
                                       { allDay: getTaskDisplayAllDay(task) },
                                     ) || "-"}
                                   </span>
                                  ) : (
                                    <div className="min-w-0 flex-1">
                                      <TaskRowDatePicker
                                        taskId={task.id}
                                        startAt={toLocalDateTimeInputValue(
                                          getTaskDisplayStartAt(task),
                                          { allDay: getTaskDisplayAllDay(task) },
                                        )}
                                        endAt={toLocalDateTimeInputValue(
                                          getTaskDisplayEndAt(task),
                                          { allDay: getTaskDisplayAllDay(task) },
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
                                        startButtonClassName={cn(
                                          "ao-task-date-button h-8 w-full min-w-0 px-1.5 text-[13px] leading-5",
                                          dateButtonColor(
                                            getTaskDisplayStartAt(task),
                                            task,
                                            "start",
                                          ),
                                        )}
                                        endButtonClassName={cn(
                                          "ao-task-date-button h-8 w-full min-w-0 px-1.5 text-[13px] leading-5",
                                          dateButtonColor(
                                            getTaskDisplayEndAt(task),
                                            task,
                                            "end",
                                          ),
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
                               {task.priority !== "none" ? (
                                 <Badge
                                   variant="secondary"
                                   className={cn(
                                     "h-5 px-1.5 text-[13px] leading-5",
                                     PRIORITY_COLORS[task.priority],
                                   )}
                                 >
                                   {PRIORITY_LABELS[task.priority] || task.priority}
                                 </Badge>
                               ) : (
                                 "-"
                               )}
                             </td>
                           )}
                           {showAssigneeColumn && (
                              <td className="h-11 truncate px-2 py-0 text-[13px] leading-5 text-muted-foreground">
                               {task.assignees?.length
                                 ? task.assignees
                                     .map(
                                       (assignee) =>
                                         assignee.display_name ||
                                         assignee.username ||
                                         assignee.user_id,
                                     )
                                     .join(", ")
                                 : "-"}
                             </td>
                           )}
                            {/* タイマー操作 → 実績時間。通常導線として常時表示する。 */}
                            {showTimeColumn && (
                               <td className="h-11 whitespace-nowrap px-1 py-0 text-left text-[13px] leading-5 text-muted-foreground">
                                <div className="flex min-w-0 items-center gap-1 overflow-hidden">
                                  {!taskReadOnly && (
                                    <Button
                                      variant="ghost"
                                      size="icon-xs"
                                      onClick={(e) => handleTimer(task, e)}
                                      disabled={timerLoading === task.id}
                                      className={cn(
                                        "shrink-0 text-muted-foreground hover:bg-muted hover:text-foreground",
                                        task.active_time_entry &&
                                          "text-green-600 dark:text-green-400",
                                      )}
                                      aria-label={
                                        task.active_time_entry
                                          ? "Time Tracked のタイマー停止"
                                          : "Time Tracked のタイマー開始"
                                      }
                                      title={
                                        task.active_time_entry
                                          ? "タイマー停止"
                                          : "タイマー開始"
                                      }
                                    >
                                      {task.active_time_entry ? (
                                        <Square className="size-3" />
                                      ) : (
                                        <Play className="size-3" />
                                      )}
                                    </Button>
                                  )}
                                  <span
                                    className="min-w-0 truncate font-mono text-[11px] leading-5 text-foreground/80"
                                    title={
                                      (task.total_time_seconds ?? 0) > 0
                                        ? `実績時間 ${formatDuration(task.total_time_seconds ?? 0)}`
                                        : "実績時間なし"
                                    }
                                  >
                                    {compactDurationLabel(
                                      task.total_time_seconds ?? 0,
                                    )}
                                  </span>
                                  {task.active_time_entry?.started_at && (
                                    <span
                                      className="inline-flex min-w-0 shrink items-center gap-0.5 truncate font-mono text-[10px] leading-5 text-green-600 dark:text-green-400"
                                      title={`現在の経過時間 ${formatElapsed(task.active_time_entry.started_at, now)}`}
                                    >
                                      <span className="size-1.5 rounded-full bg-green-500 shadow-[0_0_6px_rgba(0,191,165,0.7)]" />
                                      +{compactElapsedLabel(
                                        task.active_time_entry.started_at,
                                        now,
                                      )}
                                    </span>
                                  )}
                                </div>
                              </td>
                            )}
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
                              handleTaskDateChange={handleTaskDateChange}
                              applyTaskPatchLocally={applyTaskPatchLocally}
                              removeTaskLocally={removeTaskLocally}
                               requestRecurringDelete={requestRecurringDelete}
                               readOnly={isTaskReadOnly(sub)}
                               showProjectColumn={showProjectColumn}
                               showStartColumn={showStartColumn}
                               showDueColumn={showDueColumn}
                               showPriorityColumn={showPriorityColumn}
                               showAssigneeColumn={showAssigneeColumn}
                               showTimeColumn={showTimeColumn}
                               onStatusChange={handleRowStatusChange}
                               rowRef={(node) => {
                                 taskRowRefs.current[sub.id] = node;
                               }}
                               tabIndex={focusedTaskId === sub.id ? 0 : -1}
                               onFocus={() => setFocusedTaskId(sub.id)}
                               focusRow={() => focusTaskById(sub.id)}
                               focused={focusedTaskId === sub.id}
                             />
                          ))}

                        {/* サブタスク追加行 — 既にサブタスクがあり、かつグループに hover 中 or 入力中のみ表示 */}
                        {isExpanded &&
                          !taskReadOnly &&
                          hasSubtasks &&
                          (hoveredGroupId === task.id ||
                            subtaskAddParentId === task.id) && (
                            <SubtaskAddRow
                              task={task}
                               colSpan={desktopColumnCount}
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
                </tbody>
              </table>
            </div>
          </>
        )}
      </ScrollArea>

      {!remoteReadOnly && (
        <Button
          type="button"
          size="icon"
          onClick={handleCreateNewTask}
          className="absolute bottom-4 right-4 z-10 size-14 rounded-full shadow-lg md:hidden"
          aria-label="新規タスクを作成"
        >
          <Plus className="size-6" />
        </Button>
      )}

      {!scopeReadOnly && (
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
      )}

      {!scopeReadOnly && (
        <RecurringDeleteDialog
          open={!!pendingRecurringDelete}
          onOpenChange={(open) => {
            if (!open) setPendingRecurringDelete(null);
          }}
          onDeleteSingle={() => void handleRecurringDelete("single")}
          onDeleteFuture={() => void handleRecurringDelete("future")}
          onDeleteSeries={() => void handleRecurringDelete("series")}
        />
      )}

      {/* 右クリックコンテキストメニュー */}
      {!scopeReadOnly && (
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
      )}
    </div>
  );
}
