"use client";

import "@xyflow/react/dist/style.css";

import {
  Background,
  ReactFlow,
  ReactFlowProvider,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import { useVirtualizer } from "@tanstack/react-virtual";
import {
  CalendarRange,
  Check,
  GripVertical,
  Loader2,
  Plus,
  Trash2,
} from "lucide-react";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type DragEvent,
} from "react";
import { toast } from "sonner";

import {
  useTaskViewSelection,
  type TaskHierarchyViewFilter,
} from "@/components/tasks/hooks/use-task-view-selection";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { Project, Task } from "@/lib/task-api";
import { getTaskDisplayStatus, isFutureTask } from "@/lib/tasks-page-utils";
import { cn } from "@/lib/utils";
import {
  clampSchedulePlacement,
  scheduleApi,
  scheduleDateToLocal,
  type SchedulePhase,
  type TaskSchedulePlacement,
} from "@/lib/schedule-api";

const FAR_ZOOM = 0.55;
const DEFAULT_PHASE_DAYS = 14;
const PHASE_ROW_HEIGHT = 168;
const PHASE_HEIGHT = 136;
const TASK_NODE_WIDTH = 212;

type TaskScheduleViewProps = {
  tasks: Task[];
  projects: Project[];
  selectedProjectId: string | null;
  appFilterId: string;
  appTaskIds: ReadonlySet<string>;
  filterState: TaskHierarchyViewFilter;
  loading: boolean;
  loadError: string | null;
  remoteReadOnly: boolean;
  onOpenTask: (task: Task) => void;
};

type PhaseNodeData = {
  phase: SchedulePhase;
  left: number;
  width: number;
  top: number;
  readOnly: boolean;
  onDropTask: (event: DragEvent<HTMLDivElement>, phaseId: string) => void;
};

type TaskNodeData = {
  task: Task;
  readOnly: boolean;
  onOpenTask: (task: Task) => void;
  onDragStart: (event: DragEvent<HTMLDivElement>, taskId: string) => void;
};

type SchedulePhaseNode = Node<PhaseNodeData, "schedule-phase">;
type ScheduleTaskNode = Node<TaskNodeData, "schedule-task">;

function isRemoteProject(project: Project | null | undefined): boolean {
  return Boolean(
    project &&
    (project.source === "remote" ||
      project.remote_server_id ||
      project.id.startsWith("remote:")),
  );
}

function dateDay(value: Date): number {
  return (
    Date.UTC(value.getFullYear(), value.getMonth(), value.getDate()) /
    86_400_000
  );
}

function dateDifference(start: Date, end: Date): number {
  return Math.max(0, dateDay(end) - dateDay(start));
}

function formatDate(value: Date): string {
  return value.toLocaleDateString("ja-JP", {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

function todayWithOffset(days: number): Date {
  const value = new Date();
  value.setHours(0, 0, 0, 0);
  value.setDate(value.getDate() + days);
  return value;
}

function phaseDateRange(phases: readonly SchedulePhase[]): {
  start: Date;
  end: Date;
} {
  const dates = phases
    .flatMap((phase) => [
      scheduleDateToLocal(phase.start_on),
      scheduleDateToLocal(phase.end_on),
    ])
    .filter((date): date is Date => Boolean(date));
  if (dates.length === 0) {
    return {
      start: todayWithOffset(-DEFAULT_PHASE_DAYS),
      end: todayWithOffset(DEFAULT_PHASE_DAYS),
    };
  }
  const start = new Date(Math.min(...dates.map((date) => date.getTime())));
  const end = new Date(Math.max(...dates.map((date) => date.getTime())));
  start.setDate(start.getDate() - DEFAULT_PHASE_DAYS);
  end.setDate(end.getDate() + DEFAULT_PHASE_DAYS);
  return { start, end };
}

function phasePosition(
  phase: SchedulePhase,
  range: { start: Date; end: Date },
  totalWidth: number,
): { left: number; width: number } {
  const start = scheduleDateToLocal(phase.start_on) ?? range.start;
  const end = scheduleDateToLocal(phase.end_on) ?? start;
  const span = Math.max(1, dateDifference(range.start, range.end));
  const left = (dateDifference(range.start, start) / span) * totalWidth;
  const width = Math.max(
    260,
    (Math.max(1, dateDifference(start, end) + 1) / span) * totalWidth,
  );
  return { left, width: Math.min(totalWidth - left, width) };
}

function SchedulePhaseNodeView({ data }: NodeProps<SchedulePhaseNode>) {
  const { phase, left, width, top, readOnly, onDropTask } = data;
  const handleDrop = (event: DragEvent<HTMLDivElement>) => {
    if (readOnly) return;
    event.preventDefault();
    onDropTask(event, phase.id);
  };
  return (
    <div
      data-testid={`schedule-phase-${phase.id}`}
      className="absolute rounded border border-primary/40 bg-card/90 shadow-none"
      style={{ left, top, width, height: PHASE_HEIGHT }}
      onDragOver={(event) => {
        if (!readOnly) event.preventDefault();
      }}
      onDrop={handleDrop}
    >
      <div className="flex items-center justify-between gap-2 border-b border-border px-3 py-2">
        <div className="min-w-0">
          <p className="truncate text-sm font-semibold">{phase.name}</p>
          <p className="text-[11px] text-muted-foreground">
            {phase.start_on} ～ {phase.end_on}
          </p>
        </div>
        <CalendarRange
          className="size-4 shrink-0 text-primary/60"
          aria-hidden="true"
        />
      </div>
      <p className="px-3 pt-3 text-xs text-muted-foreground">
        {readOnly ? "読み取り専用" : "タスクをここへ配置"}
      </p>
    </div>
  );
}

function ScheduleTaskNodeView({ data }: NodeProps<ScheduleTaskNode>) {
  const { task, readOnly, onOpenTask, onDragStart } = data;
  return (
    <div
      data-testid={`schedule-task-${task.id}`}
      draggable={!readOnly && task.source !== "remote"}
      onDragStart={(event) => {
        if (!readOnly) onDragStart(event, task.id);
      }}
      onClick={() => onOpenTask(task)}
      className="group flex min-h-16 cursor-pointer items-start gap-1.5 rounded border border-border bg-card px-2.5 py-2 text-left shadow-none transition hover:border-primary/60 hover:bg-muted/50"
      title={task.title}
    >
      {!readOnly && (
        <GripVertical
          className="mt-0.5 size-3.5 shrink-0 text-muted-foreground/60"
          aria-hidden="true"
        />
      )}
      <div className="min-w-0 flex-1">
        <p className="truncate text-xs font-medium">{task.title}</p>
        <div className="mt-1 flex flex-wrap items-center gap-1 text-[10px] text-muted-foreground">
          <span>{task.status}</span>
          {task.tags.slice(0, 2).map((tag) => (
            <span key={tag.id} className="rounded bg-muted px-1">
              #{tag.name}
            </span>
          ))}
          {task.assignees[0] && (
            <span>
              {task.assignees[0].display_name ?? task.assignees[0].username}
            </span>
          )}
          {task.has_recurrence && <span aria-label="繰り返し">↻</span>}
        </div>
      </div>
    </div>
  );
}

const nodeTypes = {
  "schedule-phase": SchedulePhaseNodeView,
  "schedule-task": ScheduleTaskNodeView,
};

function TaskScheduleCanvas({
  phases,
  placements,
  tasks,
  readOnly,
  onOpenTask,
  onDropTask,
  onDragStop,
}: {
  phases: SchedulePhase[];
  placements: Map<string, TaskSchedulePlacement>;
  tasks: Task[];
  readOnly: boolean;
  onOpenTask: (task: Task) => void;
  onDropTask: (event: DragEvent<HTMLDivElement>, phaseId: string) => void;
  onDragStop: (task: Task, position: { x: number; y: number }) => void;
}) {
  const [lodZoom, setLodZoom] = useState(0.5);
  const range = useMemo(() => phaseDateRange(phases), [phases]);
  const width = Math.max(1000, phases.length * 420, 1280);
  const height = Math.max(360, phases.length * PHASE_ROW_HEIGHT + 80);
  const span = Math.max(1, dateDifference(range.start, range.end));
  const phasePositions = useMemo(
    () =>
      new Map(
        phases.map((phase, index) => [
          phase.id,
          {
            ...phasePosition(phase, range, width),
            top: index * PHASE_ROW_HEIGHT + 44,
          },
        ]),
      ),
    [phases, range, width],
  );
  const phaseNodes = useMemo<SchedulePhaseNode[]>(
    () =>
      phases.map((phase) => {
        const position = phasePositions.get(phase.id)!;
        return {
          id: `phase:${phase.id}`,
          type: "schedule-phase",
          position: { x: 0, y: 0 },
          selectable: false,
          draggable: false,
          data: {
            phase,
            left: position.left,
            width: position.width,
            top: position.top,
            readOnly,
            onDropTask,
          },
        };
      }),
    [onDropTask, phasePositions, phases, readOnly],
  );
  const taskNodes = useMemo<ScheduleTaskNode[]>(() => {
    if (lodZoom < FAR_ZOOM) return [];
    return tasks.flatMap((task) => {
      const placement = placements.get(task.id);
      if (!placement?.phase_id) return [];
      const position = phasePositions.get(placement.phase_id);
      if (!position) return [];
      const x =
        position.left +
        placement.x_ratio * Math.max(0, position.width - TASK_NODE_WIDTH);
      const y =
        position.top + Math.max(12, Math.min(PHASE_HEIGHT - 74, placement.y));
      return [
        {
          id: `task:${task.id}`,
          type: "schedule-task",
          position: { x, y },
          draggable: !readOnly,
          data: {
            task,
            readOnly,
            onOpenTask,
            onDragStart: (event: DragEvent<HTMLDivElement>, taskId: string) => {
              event.dataTransfer.setData("text/task-id", taskId);
              event.dataTransfer.effectAllowed = "move";
            },
          },
        },
      ];
    });
  }, [lodZoom, onOpenTask, phasePositions, placements, readOnly, tasks]);
  const axisTicks = useMemo(() => {
    const ticks: Array<{ left: number; label: string }> = [];
    const cursor = new Date(range.start);
    cursor.setDate(1);
    while (cursor <= range.end && ticks.length < 48) {
      ticks.push({
        left: (dateDifference(range.start, cursor) / span) * width,
        label: formatDate(cursor),
      });
      cursor.setMonth(cursor.getMonth() + 1);
    }
    return ticks;
  }, [range, span, width]);

  return (
    <div
      className="relative min-h-[380px] overflow-auto border border-border bg-background"
      data-testid="task-schedule-canvas"
      onWheel={(event) => {
        const direction = event.deltaY < 0 ? 1 : -1;
        setLodZoom((current) =>
          Math.max(0.25, Math.min(1.25, current + direction * 0.08)),
        );
      }}
    >
      <div className="relative" style={{ width, height }}>
        <div className="pointer-events-none absolute inset-x-0 top-0 z-10 h-11 border-b bg-background/95">
          <div className="relative h-full">
            {axisTicks.map((tick) => (
              <span
                key={`${tick.left}-${tick.label}`}
                className="absolute top-2 whitespace-nowrap text-[10px] text-muted-foreground"
                style={{ left: tick.left + 8 }}
              >
                {tick.label}
              </span>
            ))}
          </div>
        </div>
        <ReactFlow
          nodes={[...phaseNodes, ...taskNodes]}
          edges={[]}
          nodeTypes={nodeTypes}
          onlyRenderVisibleElements
          fitView={false}
          zoomOnScroll={false}
          panOnScroll
          nodesDraggable={!readOnly}
          nodesConnectable={false}
          proOptions={{ hideAttribution: true }}
          onNodeDragStop={(_, node) => {
            const taskId = node.id.startsWith("task:")
              ? node.id.slice(5)
              : null;
            const task = taskId
              ? tasks.find((item) => item.id === taskId)
              : null;
            if (task && !readOnly) onDragStop(task, node.position);
          }}
          className="!bg-transparent"
        >
          <Background gap={24} size={1} color="hsl(var(--border) / 0.4)" />
        </ReactFlow>
        {lodZoom < FAR_ZOOM && (
          <div className="pointer-events-none absolute right-3 top-12 z-20 rounded bg-background/90 px-2 py-1 text-[11px] text-muted-foreground">
            ズームするとタスクを表示します
          </div>
        )}
      </div>
    </div>
  );
}

export function TaskScheduleView(props: TaskScheduleViewProps) {
  return (
    <ReactFlowProvider>
      <TaskScheduleContent {...props} />
    </ReactFlowProvider>
  );
}

function TaskScheduleContent({
  tasks,
  projects,
  selectedProjectId,
  appFilterId,
  appTaskIds,
  filterState,
  loading,
  loadError,
  remoteReadOnly,
  onOpenTask,
}: TaskScheduleViewProps) {
  const [phases, setPhases] = useState<SchedulePhase[]>([]);
  const [placements, setPlacements] = useState<
    Map<string, TaskSchedulePlacement>
  >(new Map());
  const [scheduleLoading, setScheduleLoading] = useState(false);
  const [expandedShelf, setExpandedShelf] = useState(false);
  const [newPhase, setNewPhase] = useState({
    name: "",
    start_on: "",
    end_on: "",
  });
  const [editingPhaseId, setEditingPhaseId] = useState<string | null>(null);
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const shelfRef = useRef<HTMLDivElement>(null);
  const selectedProjectIdRef = useRef<string | null>(selectedProjectId);
  const projectGenerationRef = useRef(0);
  const scheduleGetTokenRef = useRef(0);
  const activeScheduleGetRef = useRef<{
    token: number;
    projectId: string;
    generation: number;
  } | null>(null);
  selectedProjectIdRef.current = selectedProjectId;

  useEffect(() => {
    projectGenerationRef.current += 1;
    activeScheduleGetRef.current = null;
    setPhases([]);
    setPlacements(new Map());
    setScheduleLoading(false);
    setExpandedShelf(false);
    setNewPhase({ name: "", start_on: "", end_on: "" });
    setEditingPhaseId(null);
    setSelectedTaskId(null);
  }, [selectedProjectId]);

  const isCurrentProject = useCallback(
    (projectId: string, generation: number) =>
      selectedProjectIdRef.current === projectId &&
      projectGenerationRef.current === generation,
    [],
  );
  const invalidateCurrentScheduleGet = useCallback(
    (projectId: string, generation: number) => {
      if (!isCurrentProject(projectId, generation)) return;
      const activeGet = activeScheduleGetRef.current;
      if (
        activeGet?.projectId !== projectId ||
        activeGet.generation !== generation
      ) {
        return;
      }
      activeScheduleGetRef.current = null;
      setScheduleLoading(false);
    },
    [isCurrentProject],
  );

  const scheduleProject =
    projects.find((project) => project.id === selectedProjectId) ?? null;
  const scheduleReadOnly =
    remoteReadOnly || scheduleProject?.can_write === false;
  const projectScopedSelection = useTaskViewSelection({
    tasks,
    projects,
    projectTab: selectedProjectId ?? "__no_schedule_project__",
    appFilterId,
    appTaskIds,
    filterState,
    includeFuture: false,
  });
  const visibleTasks = projectScopedSelection.visibleTasks;
  const scheduleTasks = useMemo(
    () =>
      visibleTasks.filter(
        (task) =>
          (filterState.showClosed || getTaskDisplayStatus(task) !== "closed") &&
          (filterState.showFuture || !isFutureTask(task)),
      ),
    [filterState.showClosed, filterState.showFuture, visibleTasks],
  );

  const refreshSchedule = useCallback(async () => {
    const projectId = selectedProjectId;
    const generation = projectGenerationRef.current;
    const token = ++scheduleGetTokenRef.current;
    activeScheduleGetRef.current = {
      token,
      projectId: projectId ?? "",
      generation,
    };
    if (!projectId || isRemoteProject(scheduleProject)) {
      activeScheduleGetRef.current = null;
      return;
    }
    const isCurrentRequest = () => {
      const activeGet = activeScheduleGetRef.current;
      return Boolean(
        activeGet?.token === token &&
        activeGet.projectId === projectId &&
        activeGet.generation === generation &&
        isCurrentProject(projectId, generation),
      );
    };
    setScheduleLoading(true);
    try {
      const data = await scheduleApi.get(projectId);
      if (!isCurrentRequest()) return;
      setPhases(data.phases);
      setPlacements(
        new Map(
          data.placements.map((placement) => [placement.task_id, placement]),
        ),
      );
    } catch (error) {
      if (!isCurrentRequest()) return;
      setPhases([]);
      setPlacements(new Map());
      toast.error(
        error instanceof Error
          ? error.message
          : "スケジュールを取得できませんでした",
      );
    } finally {
      if (isCurrentRequest()) {
        activeScheduleGetRef.current = null;
        setScheduleLoading(false);
      }
    }
  }, [isCurrentProject, scheduleProject, selectedProjectId]);

  useEffect(() => {
    void refreshSchedule();
  }, [refreshSchedule]);

  const setPlacementOptimistically = useCallback(
    async (task: Task, next: TaskSchedulePlacement | null) => {
      const projectId = selectedProjectId;
      const generation = projectGenerationRef.current;
      if (!projectId || scheduleReadOnly || isRemoteProject(scheduleProject))
        return;
      invalidateCurrentScheduleGet(projectId, generation);
      const previous = placements.get(task.id) ?? null;
      setPlacements((current) => {
        const nextMap = new Map(current);
        if (next) nextMap.set(task.id, next);
        else nextMap.delete(task.id);
        return nextMap;
      });
      try {
        if (!next) {
          await scheduleApi.deletePlacement(projectId, task.id);
        } else {
          const saved = await scheduleApi.upsertPlacement(projectId, task.id, {
            phase_id: next.phase_id,
            x_ratio: next.x_ratio,
            y: next.y,
          });
          if (!isCurrentProject(projectId, generation)) return;
          setPlacements((current) => new Map(current).set(task.id, saved));
        }
      } catch (error) {
        if (!isCurrentProject(projectId, generation)) return;
        setPlacements((current) => {
          const nextMap = new Map(current);
          if (previous) nextMap.set(task.id, previous);
          else nextMap.delete(task.id);
          return nextMap;
        });
        toast.error(
          error instanceof Error ? error.message : "配置を保存できませんでした",
        );
      }
    },
    [
      invalidateCurrentScheduleGet,
      isCurrentProject,
      placements,
      scheduleReadOnly,
      scheduleProject,
      selectedProjectId,
    ],
  );

  const phasePositionsForDrop = useMemo(() => {
    const range = phaseDateRange(phases);
    const width = Math.max(1000, phases.length * 420, 1280);
    return new Map(
      phases.map((phase, index) => [
        phase.id,
        {
          ...phasePosition(phase, range, width),
          top: index * PHASE_ROW_HEIGHT + 44,
        },
      ]),
    );
  }, [phases]);

  const dropTask = useCallback(
    (event: DragEvent<HTMLDivElement>, phaseId: string) => {
      const taskId = event.dataTransfer.getData("text/task-id");
      const task = scheduleTasks.find((item) => item.id === taskId);
      if (!task || !selectedProjectId || scheduleReadOnly) return;
      const phasePositionValue = phasePositionsForDrop.get(phaseId);
      const bounds = event.currentTarget.getBoundingClientRect();
      const ratio =
        bounds.width > 0 ? (event.clientX - bounds.left) / bounds.width : 0;
      const y = event.clientY - bounds.top;
      const clamped = clampSchedulePlacement(ratio, y);
      void setPlacementOptimistically(task, {
        task_id: task.id,
        phase_id: phaseId,
        x_ratio: clamped.x_ratio,
        y: phasePositionValue ? Math.max(0, y) : clamped.y,
        created_at: null,
        updated_at: null,
      });
    },
    [
      phasePositionsForDrop,
      scheduleReadOnly,
      scheduleTasks,
      selectedProjectId,
      setPlacementOptimistically,
    ],
  );

  const dragStop = useCallback(
    (task: Task, position: { x: number; y: number }) => {
      const current = placements.get(task.id);
      if (!current || !selectedProjectId || scheduleReadOnly) return;
      let targetPhaseId = current.phase_id;
      for (const phase of phases) {
        const bounds = phasePositionsForDrop.get(phase.id);
        if (!bounds) continue;
        if (
          position.x >= bounds.left &&
          position.x <= bounds.left + bounds.width
        ) {
          targetPhaseId = phase.id;
          const clamped = clampSchedulePlacement(
            (position.x - bounds.left) /
              Math.max(1, bounds.width - TASK_NODE_WIDTH),
            position.y - bounds.top,
          );
          void setPlacementOptimistically(task, {
            ...current,
            phase_id: targetPhaseId,
            x_ratio: clamped.x_ratio,
            y: clamped.y,
          });
          return;
        }
      }
      if (!targetPhaseId) void setPlacementOptimistically(task, null);
    },
    [
      phasePositionsForDrop,
      phases,
      placements,
      scheduleReadOnly,
      selectedProjectId,
      setPlacementOptimistically,
    ],
  );

  const unplacedTasks = scheduleTasks.filter(
    (task) => !placements.get(task.id)?.phase_id,
  );
  const selectedTask =
    scheduleTasks.find((task) => task.id === selectedTaskId) ?? null;
  const handleOpenTask = useCallback(
    (task: Task) => {
      setSelectedTaskId(task.id);
      onOpenTask(task);
    },
    [onOpenTask],
  );
  const shelfVirtualizer = useVirtualizer({
    count: expandedShelf
      ? unplacedTasks.length
      : Math.min(unplacedTasks.length, 8),
    getScrollElement: () => shelfRef.current,
    estimateSize: () => 48,
    overscan: 4,
  });

  const addPhase = async () => {
    const projectId = selectedProjectId;
    const generation = projectGenerationRef.current;
    if (!projectId || scheduleReadOnly || isRemoteProject(scheduleProject))
      return;
    if (!newPhase.name.trim() || !newPhase.start_on || !newPhase.end_on) {
      toast.error("工程名と開始日・終了日を指定してください");
      return;
    }
    invalidateCurrentScheduleGet(projectId, generation);
    try {
      const phase = await scheduleApi.createPhase(projectId, newPhase);
      if (!isCurrentProject(projectId, generation)) return;
      setPhases((current) => [...current, phase]);
      setNewPhase({ name: "", start_on: "", end_on: "" });
    } catch (error) {
      if (!isCurrentProject(projectId, generation)) return;
      toast.error(
        error instanceof Error ? error.message : "工程を作成できませんでした",
      );
    }
  };

  const deletePhase = async (phase: SchedulePhase) => {
    const projectId = selectedProjectId;
    const generation = projectGenerationRef.current;
    if (!projectId || scheduleReadOnly) return;
    invalidateCurrentScheduleGet(projectId, generation);
    try {
      await scheduleApi.deletePhase(projectId, phase.id);
      if (!isCurrentProject(projectId, generation)) return;
      setPhases((current) => current.filter((item) => item.id !== phase.id));
      setPlacements((current) => {
        const next = new Map(current);
        for (const [taskId, placement] of next) {
          if (placement.phase_id === phase.id)
            next.set(taskId, { ...placement, phase_id: null });
        }
        return next;
      });
    } catch (error) {
      if (!isCurrentProject(projectId, generation)) return;
      toast.error(
        error instanceof Error ? error.message : "工程を削除できませんでした",
      );
    }
  };

  const updatePhase = async (
    phase: SchedulePhase,
    patch: Partial<SchedulePhase>,
  ) => {
    const projectId = selectedProjectId;
    const generation = projectGenerationRef.current;
    if (!projectId || scheduleReadOnly) return;
    invalidateCurrentScheduleGet(projectId, generation);
    try {
      const updated = await scheduleApi.updatePhase(projectId, phase.id, patch);
      if (!isCurrentProject(projectId, generation)) return;
      setPhases((current) =>
        current.map((item) => (item.id === phase.id ? updated : item)),
      );
      setEditingPhaseId(null);
    } catch (error) {
      if (!isCurrentProject(projectId, generation)) return;
      toast.error(
        error instanceof Error ? error.message : "工程を更新できませんでした",
      );
    }
  };

  if (!selectedProjectId) {
    return (
      <div
        className="flex h-full min-h-72 items-center justify-center p-6 text-center"
        data-testid="task-schedule-empty"
      >
        <CalendarRange
          className="size-8 text-muted-foreground"
          aria-hidden="true"
        />
      </div>
    );
  }

  if (isRemoteProject(scheduleProject)) {
    return (
      <div className="flex h-full min-h-72 items-center justify-center p-6 text-sm text-muted-foreground">
        リモートプロジェクトのスケジュールは未対応
      </div>
    );
  }

  return (
    <div
      className="flex h-full min-h-0 flex-col bg-background"
      data-testid="task-schedule-view"
    >
      <div className="grid min-h-0 flex-1 grid-cols-1 lg:grid-cols-[224px_minmax(0,1fr)_280px]">
        <aside className="hidden min-h-0 overflow-y-auto border-r border-border bg-card/30 lg:flex lg:flex-col">
          <div className="border-b border-border px-4 py-3">
            <p className="truncate text-sm font-semibold">
              {scheduleProject?.name ?? "プロジェクト"}
            </p>
            <p className="mt-0.5 text-[11px] text-muted-foreground">Schedule</p>
          </div>
          <div className="space-y-4 p-3">
            <section>
              <div className="mb-2 flex items-center justify-between px-1">
                <p className="text-[10px] font-semibold uppercase tracking-[0.14em] text-muted-foreground">
                  Phase management
                </p>
                <span className="text-[10px] tabular-nums text-muted-foreground">
                  {phases.length}
                </span>
              </div>
              <div className="space-y-0.5">
                {phases.map((phase) => (
                  <button
                    key={phase.id}
                    type="button"
                    className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground disabled:cursor-default disabled:opacity-60 disabled:hover:bg-transparent disabled:hover:text-muted-foreground"
                    onClick={() => setEditingPhaseId(phase.id)}
                    disabled={scheduleReadOnly}
                    aria-label={
                      scheduleReadOnly
                        ? `${phase.name}（読み取り専用）`
                        : `${phase.name}を編集`
                    }
                  >
                    <CalendarRange className="size-3.5 shrink-0" />
                    <span className="truncate">{phase.name}</span>
                  </button>
                ))}
              </div>
            </section>
            <section className="min-h-0">
              <div className="mb-2 flex items-center justify-between px-1">
                <p className="text-[10px] font-semibold uppercase tracking-[0.14em] text-muted-foreground">
                  Unplaced tasks
                </p>
                <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] tabular-nums">
                  {unplacedTasks.length}
                </span>
              </div>
              <p className="px-1 text-[11px] leading-4 text-muted-foreground">
                {scheduleReadOnly
                  ? "読み取り専用のスケジュールです。"
                  : "ドラッグして工程へ配置できます。"}
              </p>
            </section>
          </div>
        </aside>
        <section className="flex min-h-0 min-w-0 flex-col">
          <div className="flex min-h-14 flex-wrap items-center justify-between gap-2 border-b border-border px-4 py-2">
            <div>
              <h2 className="text-base font-semibold tracking-tight">
                スケジュール
              </h2>
              <p className="text-[11px] text-muted-foreground">
                工程上の配置 · タスク本体の日付とは独立
              </p>
            </div>
            <div className="flex items-center gap-2 text-xs text-muted-foreground">
              {scheduleReadOnly && (
                <span className="rounded border border-border px-1.5 py-1">
                  読み取り専用
                </span>
              )}
              <Button
                type="button"
                size="sm"
                variant="outline"
                className="h-8 rounded"
                onClick={() => void refreshSchedule()}
                disabled={scheduleLoading}
              >
                {scheduleLoading ? (
                  <Loader2 className="size-3.5 animate-spin" />
                ) : (
                  <Check className="size-3.5" />
                )}
                更新
              </Button>
            </div>
          </div>
          <div className="flex flex-wrap items-end gap-2 border-b border-border bg-card/20 px-4 py-2">
            <Input
              aria-label="工程名"
              placeholder="工程名"
              value={newPhase.name}
              onChange={(event) =>
                setNewPhase((current) => ({
                  ...current,
                  name: event.target.value,
                }))
              }
              className="h-8 w-44 rounded text-xs"
              disabled={scheduleReadOnly}
            />
            <Input
              aria-label="工程開始日"
              type="date"
              value={newPhase.start_on}
              onChange={(event) =>
                setNewPhase((current) => ({
                  ...current,
                  start_on: event.target.value,
                }))
              }
              className="h-8 w-36 rounded text-xs"
              disabled={scheduleReadOnly}
            />
            <Input
              aria-label="工程終了日"
              type="date"
              value={newPhase.end_on}
              onChange={(event) =>
                setNewPhase((current) => ({
                  ...current,
                  end_on: event.target.value,
                }))
              }
              className="h-8 w-36 rounded text-xs"
              disabled={scheduleReadOnly}
            />
            <Button
              type="button"
              size="sm"
              className="h-8 rounded"
              onClick={() => void addPhase()}
              disabled={scheduleReadOnly}
            >
              <Plus className="size-3.5" /> 工程を追加
            </Button>
          </div>
          {loading || scheduleLoading ? (
            <div className="m-4 flex items-center gap-2 rounded border border-border p-4 text-sm text-muted-foreground">
              <Loader2 className="size-4 animate-spin" />
              スケジュールを読み込み中…
            </div>
          ) : loadError ? (
            <div className="m-4 rounded border border-destructive/40 p-4 text-sm text-destructive">
              {loadError}
            </div>
          ) : phases.length === 0 ? (
            <div className="m-4 rounded border border-dashed p-8 text-center text-sm text-muted-foreground">
              工程がありません。上のフォームから工程を追加してください。
            </div>
          ) : (
            <div className="min-h-0 flex-1 p-3">
              <TaskScheduleCanvas
                phases={phases}
                placements={placements}
                tasks={scheduleTasks}
                readOnly={scheduleReadOnly}
                onOpenTask={handleOpenTask}
                onDropTask={dropTask}
                onDragStop={dragStop}
              />
            </div>
          )}
          {phases.length > 0 && (
            <div className="flex flex-wrap gap-2 border-t border-border px-3 py-2 text-xs">
              {phases.map((phase) => (
                <div
                  key={phase.id}
                  className="flex items-center gap-1 rounded border border-border bg-card/50 px-2 py-1"
                >
                  {editingPhaseId === phase.id && !scheduleReadOnly ? (
                    <>
                      <Input
                        aria-label={`${phase.name}工程名`}
                        defaultValue={phase.name}
                        className="h-6 w-28 text-xs"
                        disabled={scheduleReadOnly}
                        onKeyDown={(event) => {
                          if (event.key === "Enter")
                            void updatePhase(phase, {
                              name: event.currentTarget.value,
                            });
                        }}
                      />
                      <Button
                        type="button"
                        size="icon"
                        variant="ghost"
                        className="size-6"
                        onClick={() => setEditingPhaseId(null)}
                        disabled={scheduleReadOnly}
                      >
                        ×
                      </Button>
                    </>
                  ) : (
                    <button
                      type="button"
                      className="max-w-40 truncate hover:underline"
                      onClick={() => setEditingPhaseId(phase.id)}
                      disabled={scheduleReadOnly}
                      aria-label={
                        scheduleReadOnly
                          ? `${phase.name}（読み取り専用）`
                          : `${phase.name}を編集`
                      }
                    >
                      {phase.name}
                    </button>
                  )}
                  <button
                    type="button"
                    aria-label={`${phase.name}を削除`}
                    className="rounded p-0.5 text-muted-foreground hover:text-destructive"
                    onClick={() => void deletePhase(phase)}
                    disabled={scheduleReadOnly}
                  >
                    <Trash2 className="size-3" />
                  </button>
                </div>
              ))}
            </div>
          )}
          <div
            ref={shelfRef}
            data-testid="task-schedule-unplaced"
            className="max-h-40 overflow-auto border-t border-border bg-card/30 p-3"
            onDragOver={(event) => {
              if (!scheduleReadOnly) event.preventDefault();
            }}
            onDrop={(event) => {
              if (scheduleReadOnly) return;
              event.preventDefault();
              const taskId = event.dataTransfer.getData("text/task-id");
              const task = scheduleTasks.find((item) => item.id === taskId);
              if (task) void setPlacementOptimistically(task, null);
            }}
          >
            <button
              type="button"
              className="flex w-full items-center justify-between text-left text-xs font-medium"
              onClick={() => setExpandedShelf((value) => !value)}
            >
              <span>未配置タスク {unplacedTasks.length}件</span>
              <span className="text-muted-foreground">
                {expandedShelf ? "折りたたむ" : "展開"}
              </span>
            </button>
            {unplacedTasks.length > 0 && (
              <div
                className="relative mt-2"
                style={{ height: `${shelfVirtualizer.getTotalSize()}px` }}
              >
                {shelfVirtualizer.getVirtualItems().map((item) => {
                  const task = unplacedTasks[item.index];
                  return (
                    <div
                      key={task.id}
                      className="absolute left-0 right-0 px-1"
                      style={{ transform: `translateY(${item.start}px)` }}
                    >
                      <div
                        draggable={
                          !scheduleReadOnly && task.source !== "remote"
                        }
                        onDragStart={(event) => {
                          if (scheduleReadOnly) return;
                          event.dataTransfer.setData("text/task-id", task.id);
                          event.dataTransfer.effectAllowed = "move";
                        }}
                        onClick={() => handleOpenTask(task)}
                        className={cn(
                          "flex items-center gap-2 rounded border border-border bg-background px-2 py-1.5 text-xs hover:border-primary/50",
                          scheduleReadOnly ? "cursor-pointer" : "cursor-grab",
                        )}
                      >
                        {!scheduleReadOnly && (
                          <GripVertical className="size-3.5 text-muted-foreground" />
                        )}
                        <span className="truncate">{task.title}</span>
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        </section>
        <aside className="hidden min-h-0 overflow-y-auto border-l border-border bg-card/30 lg:block">
          <div className="border-b border-border px-4 py-3">
            <p className="text-sm font-semibold">Task context</p>
            <p className="mt-0.5 text-[11px] text-muted-foreground">
              選択したタスクの実データ
            </p>
          </div>
          {selectedTask ? (
            <div className="space-y-4 p-4">
              <div className="rounded border border-border bg-card/60 p-3">
                <p className="text-[10px] font-semibold uppercase tracking-[0.14em] text-muted-foreground">
                  Selected task
                </p>
                <h3 className="mt-2 text-sm font-semibold leading-5">
                  {selectedTask.title}
                </h3>
                <p className="mt-1 text-xs text-muted-foreground">
                  {scheduleProject?.name ?? "プロジェクト"}
                </p>
              </div>
              <dl className="space-y-2 text-xs">
                <div className="flex items-center justify-between gap-3 border-b border-border/60 pb-2">
                  <dt className="text-muted-foreground">Status</dt>
                  <dd>{selectedTask.status}</dd>
                </div>
                <div className="flex items-center justify-between gap-3 border-b border-border/60 pb-2">
                  <dt className="text-muted-foreground">Priority</dt>
                  <dd>{selectedTask.priority}</dd>
                </div>
                <div className="flex items-center justify-between gap-3 border-b border-border/60 pb-2">
                  <dt className="text-muted-foreground">Dates</dt>
                  <dd className="text-right">
                    {selectedTask.start_at
                      ? new Date(selectedTask.start_at).toLocaleDateString(
                          "ja-JP",
                        )
                      : "—"}
                    {selectedTask.end_at
                      ? ` → ${new Date(selectedTask.end_at).toLocaleDateString("ja-JP")}`
                      : ""}
                  </dd>
                </div>
                <div className="flex items-center justify-between gap-3 border-b border-border/60 pb-2">
                  <dt className="text-muted-foreground">Tracked</dt>
                  <dd className="tabular-nums">
                    {Math.round((selectedTask.total_time_seconds ?? 0) / 60)}{" "}
                    min
                  </dd>
                </div>
              </dl>
              <Button
                type="button"
                size="sm"
                variant="outline"
                className="w-full rounded"
                onClick={() => handleOpenTask(selectedTask)}
              >
                詳細を開く
              </Button>
            </div>
          ) : (
            <div className="p-4 text-xs leading-5 text-muted-foreground">
              工程内のタスクを選択すると、担当・状態・日付を表示します。
            </div>
          )}
        </aside>
      </div>
    </div>
  );
}

export { FAR_ZOOM, phaseDateRange, phasePosition };
