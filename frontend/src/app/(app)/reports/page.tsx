"use client";

import {
  useState,
  useEffect,
  useCallback,
  useMemo,
  useRef,
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
} from "react";
import { createPortal } from "react-dom";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Dialog, DialogContent } from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
} from "@/components/ui/dropdown-menu";
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectTrigger,
} from "@/components/ui/select";
import {
  Clock,
  ListChecks,
  Activity,
  ChevronLeft,
  ChevronRight,
  Trash2,
  ExternalLink,
  Undo2,
  PlayCircle,
  StopCircle,
  Copy,
  MoreVertical,
  X as XIcon,
  FolderKanban,
  Folder,
} from "lucide-react";
import {
  taskApi,
  type Task,
  type TimeReport,
  type TimeReportBucket,
  type TimeEntry,
} from "@/lib/task-api";
import { useTaskCompletionRefresh } from "@/hooks/use-task-completion-refresh";
import { useContextMenuPosition } from "@/hooks/use-context-menu-position";
import { getUserSettings, patchUserSettings } from "@/lib/user-settings";
import { useProject } from "@/contexts/project-context";
import { useTheme } from "@/contexts/theme-context";
import { TaskDetailModal } from "@/components/tasks/task-detail-modal";
import {
  formatLocalDateTime,
  formatLocalDateTimeWithMilliseconds,
} from "@/lib/date-time";
import {
  resolveProjectColorTokens,
  type ProjectColorTokens,
} from "@/lib/project-colors";

function formatSeconds(seconds: number): string {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (h > 0) return `${h}時間${m}分`;
  return `${m}分`;
}

function formatHours(seconds: number): string {
  const h = seconds / 3600;
  return h.toFixed(1) + "h";
}

type PeriodPreset = "this_week" | "this_month" | "custom";
type ScopeMode = "project" | "space" | "all";

type ReportsViewSettings = {
  scope?: ScopeMode;
  period?: PeriodPreset;
  custom_from?: string;
  custom_to?: string;
  week_offset?: number;
  show_schedule_frames?: boolean;
};

function getWeekRangeFromDate(base: Date): { monday: Date; sunday: Date } {
  const day = base.getDay();
  const diff = base.getDate() - day + (day === 0 ? -6 : 1);
  const monday = new Date(base);
  monday.setDate(diff);
  monday.setHours(0, 0, 0, 0);
  const sunday = new Date(monday);
  sunday.setDate(monday.getDate() + 6);
  sunday.setHours(23, 59, 59, 999);
  return { monday, sunday };
}

function getMonthRange(): { start: Date; end: Date } {
  const now = new Date();
  const start = new Date(now.getFullYear(), now.getMonth(), 1, 0, 0, 0, 0);
  const end = new Date(
    now.getFullYear(),
    now.getMonth() + 1,
    0,
    23,
    59,
    59,
    999,
  );
  return { start, end };
}

const DAY_LABELS = ["月", "火", "水", "木", "金", "土", "日"];
const HOUR_START = 7;
const HOUR_END = 22;
const TOTAL_HOURS = HOUR_END - HOUR_START;

const DEFAULT_ENTRY_COLOR = "#94a3b8";

function timelineBlockStyle(tokens: ProjectColorTokens): CSSProperties {
  return {
    background: tokens.surfaceGradient,
    borderColor: tokens.border,
    color: tokens.text,
    boxShadow: `inset 3px 0 ${tokens.stripe}, inset 0 1px rgba(255,255,255,0.42), 0 12px 28px -24px rgba(0,0,0,0.42)`,
    backdropFilter: "blur(12px) saturate(1.18)",
  };
}

function formatTimeWindow(entry: TimeEntry): string {
  if (!entry.started_at) return "-";
  const start = new Date(entry.started_at);
  const startText = start.toLocaleTimeString("ja-JP", {
    hour: "2-digit",
    minute: "2-digit",
  });
  if (!entry.ended_at) return `${startText} - 計測中`;
  const end = new Date(entry.ended_at);
  return `${startText} - ${end.toLocaleTimeString("ja-JP", {
    hour: "2-digit",
    minute: "2-digit",
  })}`;
}

function toLocalHM(date: Date): string {
  return `${String(date.getHours()).padStart(2, "0")}:${String(
    date.getMinutes(),
  ).padStart(2, "0")}`;
}

function toLocalYMD(date: Date): string {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(
    2,
    "0",
  )}-${String(date.getDate()).padStart(2, "0")}`;
}

function parseDateValue(value?: string | null): Date | null {
  if (!value) return null;
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

function parseTimeInput(input: string): string | null {
  const trimmed = input.trim();
  if (!trimmed) return null;
  let m = trimmed.match(/^(\d{1,2}):(\d{2})$/);
  if (!m) m = trimmed.match(/^(\d{1,2})(\d{2})$/);
  if (!m) return null;
  const h = parseInt(m[1], 10);
  const mm = parseInt(m[2], 10);
  if (isNaN(h) || isNaN(mm) || h > 23 || mm > 59) return null;
  return `${String(h).padStart(2, "0")}:${String(mm).padStart(2, "0")}`;
}

function parseDurationInput(input: string): number | null {
  const trimmed = input.trim();
  if (!trimmed) return null;
  const parts = trimmed.split(":");
  if (parts.length === 3) {
    const h = parseInt(parts[0], 10);
    const m = parseInt(parts[1], 10);
    const s = parseInt(parts[2], 10);
    if ([h, m, s].some(isNaN) || m > 59 || s > 59) return null;
    return h * 3600 + m * 60 + s;
  }
  if (parts.length === 2) {
    const a = parseInt(parts[0], 10);
    const b = parseInt(parts[1], 10);
    if ([a, b].some(isNaN) || b > 59) return null;
    return a * 3600 + b * 60;
  }
  if (parts.length === 1) {
    const n = parseInt(parts[0], 10);
    if (isNaN(n)) return null;
    return n * 60;
  }
  return null;
}

function formatDurationInput(seconds: number): string {
  const sec = Math.max(0, Math.floor(seconds));
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

function combineDateTime(ymd: string, hm: string): Date {
  return new Date(`${ymd}T${hm}:00`);
}

function BucketBar({
  bucket,
  maxSeconds,
  onClick,
}: {
  bucket: TimeReportBucket;
  maxSeconds: number;
  onClick?: () => void;
}) {
  const pct = maxSeconds > 0 ? (bucket.seconds / maxSeconds) * 100 : 0;
  const content = (
    <div className="space-y-1 rounded-md border border-white/55 bg-white/42 p-2.5 shadow-[inset_0_1px_rgba(255,255,255,0.68),0_12px_30px_-26px_rgba(6,81,110,0.55)] backdrop-blur-xl dark:border-white/10 dark:bg-white/8 dark:shadow-[inset_0_1px_rgba(255,255,255,0.1)]">
      <div className="flex items-center justify-between text-sm">
        <div className="min-w-0">
          <span className="block truncate">{bucket.label}</span>
          {bucket.project_name && bucket.project_name !== bucket.label && (
            <span className="block truncate text-xs text-muted-foreground">
              {bucket.project_name}
            </span>
          )}
        </div>
        <span className="shrink-0 text-muted-foreground">
          {formatSeconds(bucket.seconds)} ({bucket.entries}件)
        </span>
      </div>
      <div className="h-2 rounded-full bg-muted/55 overflow-hidden">
        <div
          className="h-full rounded-full bg-primary/75 transition-all"
          style={{ width: `${Math.max(pct, 1)}%` }}
        />
      </div>
    </div>
  );
  if (!onClick) return content;
  return (
    <button
      type="button"
      className="w-full rounded-md text-left transition-colors hover:bg-accent/30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      onClick={onClick}
    >
      {content}
    </button>
  );
}

function getWeekDays(monday: Date): Date[] {
  return Array.from({ length: 7 }, (_, i) => {
    const d = new Date(monday);
    d.setDate(monday.getDate() + i);
    return d;
  });
}

function groupEntriesByDay(
  entries: TimeEntry[],
  weekDays: Date[],
): Map<string, TimeEntry[]> {
  const map = new Map<string, TimeEntry[]>();
  for (const d of weekDays) {
    map.set(toLocalYMD(d), []);
  }
  for (const entry of entries) {
    if (!entry.started_at) continue;
    const start = new Date(entry.started_at);
    const dateKey = toLocalYMD(start);
    if (map.has(dateKey)) {
      map.get(dateKey)!.push(entry);
    }
  }
  return map;
}

function getEntryDurationSeconds(entry: TimeEntry, now: Date): number {
  if (!entry.started_at) return 0;
  const start = new Date(entry.started_at);
  const end = entry.ended_at ? new Date(entry.ended_at) : now;
  if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) return 0;
  return Math.max(0, Math.floor((end.getTime() - start.getTime()) / 1000));
}

function getEntryHourRange(
  entry: TimeEntry,
  now: Date,
): { startHour: number; endHour: number } | null {
  if (!entry.started_at) return null;
  const start = new Date(entry.started_at);
  const end = entry.ended_at ? new Date(entry.ended_at) : now;
  if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) {
    return null;
  }
  return {
    startHour: start.getHours() + start.getMinutes() / 60,
    endHour: end.getHours() + end.getMinutes() / 60,
  };
}

type EntryColumnLayout = {
  columnIndex: number;
  columnCount: number;
};

function buildEntryColumnLayouts(
  entries: TimeEntry[],
  now: Date,
): Map<string, EntryColumnLayout> {
  const ranges = entries
    .map((entry) => {
      if (!entry.started_at) return null;
      const start = new Date(entry.started_at).getTime();
      const rawEnd = entry.ended_at
        ? new Date(entry.ended_at).getTime()
        : now.getTime();
      if (Number.isNaN(start) || Number.isNaN(rawEnd)) return null;
      return {
        entry,
        start,
        end: Math.max(rawEnd, start + 60 * 1000),
      };
    })
    .filter((range): range is NonNullable<typeof range> => range !== null)
    .sort((a, b) => a.start - b.start || a.end - b.end);

  const layouts = new Map<string, EntryColumnLayout>();
  let group: typeof ranges = [];
  let groupEnd = -Infinity;

  const flushGroup = () => {
    if (group.length === 0) return;
    const columns: number[] = [];
    const assigned = new Map<string, number>();
    for (const range of group) {
      let columnIndex = columns.findIndex((end) => end <= range.start);
      if (columnIndex === -1) {
        columnIndex = columns.length;
        columns.push(range.end);
      } else {
        columns[columnIndex] = range.end;
      }
      assigned.set(range.entry.id, columnIndex);
    }
    const columnCount = Math.max(columns.length, 1);
    for (const range of group) {
      layouts.set(range.entry.id, {
        columnIndex: assigned.get(range.entry.id) ?? 0,
        columnCount,
      });
    }
  };

  for (const range of ranges) {
    if (group.length > 0 && range.start >= groupEnd) {
      flushGroup();
      group = [];
      groupEnd = -Infinity;
    }
    group.push(range);
    groupEnd = Math.max(groupEnd, range.end);
  }
  flushGroup();

  return layouts;
}

function isTaskScheduledInRange(task: Task, rangeStart: Date, rangeEnd: Date) {
  const start = parseDateValue(task.start_at);
  const end = parseDateValue(task.end_at);
  if (!start || !end || end <= start || task.all_day) return false;
  return end >= rangeStart && start <= rangeEnd;
}

function getTaskScheduleSegmentForDay(task: Task, day: Date) {
  const start = parseDateValue(task.start_at);
  const end = parseDateValue(task.end_at);
  if (!start || !end || end <= start || task.all_day) return null;

  const dayStart = new Date(day);
  dayStart.setHours(0, 0, 0, 0);
  const dayEnd = new Date(dayStart);
  dayEnd.setDate(dayEnd.getDate() + 1);

  if (end <= dayStart || start >= dayEnd) return null;

  const segmentStart = start > dayStart ? start : dayStart;
  const segmentEnd = end < dayEnd ? end : dayEnd;

  return {
    startHour:
      segmentStart.getHours() +
      segmentStart.getMinutes() / 60 +
      segmentStart.getSeconds() / 3600,
    endHour:
      segmentEnd.getHours() +
      segmentEnd.getMinutes() / 60 +
      segmentEnd.getSeconds() / 3600,
  };
}

function formatTaskScheduleLabel(task: Task): string {
  const start = parseDateValue(task.start_at);
  const end = parseDateValue(task.end_at);
  if (!start || !end) return task.title;
  const startText = start.toLocaleString("ja-JP", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
  const endText = end.toLocaleString("ja-JP", {
    hour: "2-digit",
    minute: "2-digit",
  });
  return `${task.title} ${startText} - ${endText}`;
}

function getDayTotalSeconds(entries: TimeEntry[], now: Date): number {
  return entries.reduce((sum, e) => sum + getEntryDurationSeconds(e, now), 0);
}

type ResizeState = {
  entryId: string;
  edge: "top" | "bottom";
  dayIndex: number;
  originalStartHour: number;
  originalEndHour: number;
  currentHour: number;
};

type MoveState = {
  entryId: string;
  originalStartedAt: string;
  originalEndedAt: string;
  originalDayIndex: number;
  originalStartHour: number;
  durationHours: number;
  pointerOffsetHours: number;
  mouseStartX: number;
  mouseStartY: number;
  currentDayIndex: number;
  currentStartHour: number;
  moving: boolean;
};

type CtxMenuState = {
  entry: TimeEntry;
  x: number;
  y: number;
};

export default function ReportsPage() {
  const {
    selectedProjectId,
    selectedSpaceId,
    selectedProject,
    selectedSpace,
    allProjects,
    spaces,
  } = useProject();
  const { resolvedTheme } = useTheme();
  const [scope, setScope] = useState<ScopeMode>("project");
  const [report, setReport] = useState<TimeReport | null>(null);
  const [timeEntries, setTimeEntries] = useState<TimeEntry[]>([]);
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [selectedEntry, setSelectedEntry] = useState<TimeEntry | null>(null);
  const [loading, setLoading] = useState(false);
  const [period, setPeriod] = useState<PeriodPreset>("this_week");
  const [customFrom, setCustomFrom] = useState("");
  const [customTo, setCustomTo] = useState("");
  const [weekOffset, setWeekOffset] = useState(0);
  const [showScheduleFrames, setShowScheduleFrames] = useState(false);
  const [prefsLoaded, setPrefsLoaded] = useState(false);
  const [scheduledTasks, setScheduledTasks] = useState<Task[]>([]);

  // 既存エントリ編集ダイアログ
  const [editingEntry, setEditingEntry] = useState<TimeEntry | null>(null);
  const [editDate, setEditDate] = useState("");
  const [editStart, setEditStart] = useState("");
  const [editEnd, setEditEnd] = useState("");
  const [editDuration, setEditDuration] = useState("0:00:00");
  const [editNote, setEditNote] = useState("");
  const [editSaving, setEditSaving] = useState(false);

  const currentEditingProject = useMemo(
    () =>
      editingEntry?.project_id
        ? (allProjects.find(
            (project) => project.id === editingEntry.project_id,
          ) ?? null)
        : null,
    [allProjects, editingEntry?.project_id],
  );

  const currentEditingSpace = useMemo(() => {
    const spaceId = currentEditingProject?.space_id ?? editingEntry?.space_id;
    return spaceId
      ? (spaces.find((space) => space.id === spaceId) ?? null)
      : null;
  }, [currentEditingProject?.space_id, editingEntry?.space_id, spaces]);

  const projectsForEditingSpace = useMemo(() => {
    if (currentEditingSpace?.id) {
      return allProjects.filter(
        (project) => project.space_id === currentEditingSpace.id,
      );
    }
    if (editingEntry?.space_id) {
      return allProjects.filter(
        (project) => project.space_id === editingEntry.space_id,
      );
    }
    return allProjects.filter((project) => !project.space_id);
  }, [allProjects, currentEditingSpace?.id, editingEntry?.space_id]);

  // 新規作成ドラッグ
  type DragState = {
    dayIndex: number;
    startHour: number;
    currentHour: number;
  };
  type DragForm = {
    dayIndex: number;
    startHour: number;
    endHour: number;
    topPct: number;
  };
  const [dragState, setDragState] = useState<DragState | null>(null);
  const [dragForm, setDragForm] = useState<DragForm | null>(null);
  const [dragTaskName, setDragTaskName] = useState("");
  const [dragTaskOptions, setDragTaskOptions] = useState<Task[]>([]);
  const [dragSelectedTaskId, setDragSelectedTaskId] = useState<string | null>(
    null,
  );
  const [dragTaskLoading, setDragTaskLoading] = useState(false);
  const [dragCreating, setDragCreating] = useState(false);
  const dragFormInputRef = useRef<HTMLInputElement>(null);
  const isDraggingRef = useRef(false);
  const [now, setNow] = useState(() => new Date());

  // リサイズD&D
  const [resizeState, setResizeState] = useState<ResizeState | null>(null);
  const isResizingRef = useRef(false);
  const dayColRefs = useRef<Array<HTMLDivElement | null>>([]);

  // 移動D&D
  const [moveState, setMoveState] = useState<MoveState | null>(null);
  const moveStateRef = useRef<MoveState | null>(null);
  const isMovingRef = useRef(false);

  // 右クリックコンテキストメニュー
  const [ctxMenu, setCtxMenu] = useState<CtxMenuState | null>(null);
  const { ref: ctxMenuRef, style: ctxMenuStyle } = useContextMenuPosition(
    ctxMenu ? { x: ctxMenu.x, y: ctxMenu.y } : null,
    { fallbackWidth: 180, fallbackHeight: 150 },
  );

  const currentWeekMonday = useMemo(() => {
    const now = new Date();
    const day = now.getDay();
    const diff = now.getDate() - day + (day === 0 ? -6 : 1);
    const monday = new Date(now);
    monday.setDate(diff + weekOffset * 7);
    monday.setHours(0, 0, 0, 0);
    return monday;
  }, [weekOffset]);

  const weekRange = useMemo(
    () => getWeekRangeFromDate(currentWeekMonday),
    [currentWeekMonday],
  );

  const weekDays = useMemo(() => getWeekDays(weekRange.monday), [weekRange]);

  useEffect(() => {
    let active = true;

    void getUserSettings()
      .then((settings) => {
        if (!active) return;
        const view = settings.reports_view as ReportsViewSettings | undefined;
        if (!view) return;

        if (
          view.scope === "project" ||
          view.scope === "space" ||
          view.scope === "all"
        ) {
          setScope(view.scope);
        }
        if (
          view.period === "this_week" ||
          view.period === "this_month" ||
          view.period === "custom"
        ) {
          setPeriod(view.period);
        }
        if (typeof view.custom_from === "string") {
          setCustomFrom(view.custom_from);
        }
        if (typeof view.custom_to === "string") {
          setCustomTo(view.custom_to);
        }
        if (typeof view.week_offset === "number") {
          setWeekOffset(view.week_offset);
        }
        if (typeof view.show_schedule_frames === "boolean") {
          setShowScheduleFrames(view.show_schedule_frames);
        }
      })
      .catch((err) => {
        console.error("レポート表示設定の取得に失敗しました", err);
      })
      .finally(() => {
        if (active) setPrefsLoaded(true);
      });

    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    if (!prefsLoaded) return;

    void patchUserSettings({
      reports_view: {
        scope,
        period,
        custom_from: customFrom,
        custom_to: customTo,
        week_offset: weekOffset,
        show_schedule_frames: showScheduleFrames,
      } satisfies ReportsViewSettings,
    }).catch((err) => {
      console.error("レポート表示設定の保存に失敗しました", err);
    });
  }, [
    prefsLoaded,
    scope,
    period,
    customFrom,
    customTo,
    weekOffset,
    showScheduleFrames,
  ]);

  useEffect(() => {
    const tick = () => setNow(new Date());
    tick();
    const timer = window.setInterval(tick, 60 * 1000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    if (!dragForm || !selectedProjectId) {
      setDragTaskOptions([]);
      setDragSelectedTaskId(null);
      return;
    }

    let cancelled = false;
    setDragTaskLoading(true);
    void taskApi
      .listTasks(selectedProjectId)
      .then((list) => {
        if (cancelled) return;
        setDragTaskOptions(list.filter((task) => !task.parent_task_id));
      })
      .catch((err) => {
        if (!cancelled) {
          console.error("既存タスク取得失敗", err);
          setDragTaskOptions([]);
        }
      })
      .finally(() => {
        if (!cancelled) setDragTaskLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [dragForm, selectedProjectId]);

  const fetchReport = useCallback(async () => {
    const scopeArg: { project_id?: string; space_id?: string } | null =
      scope === "all"
        ? {}
        : scope === "space"
          ? selectedSpaceId
            ? { space_id: selectedSpaceId }
            : null
          : selectedProjectId
            ? { project_id: selectedProjectId }
            : null;
    if (!scopeArg) return;

    setLoading(true);

    let dateFrom: string | undefined;
    let dateTo: string | undefined;

    switch (period) {
      case "this_week": {
        dateFrom = formatLocalDateTime(weekRange.monday);
        dateTo = formatLocalDateTimeWithMilliseconds(weekRange.sunday);
        break;
      }
      case "this_month": {
        const { start, end } = getMonthRange();
        dateFrom = formatLocalDateTime(start);
        dateTo = formatLocalDateTimeWithMilliseconds(end);
        break;
      }
      case "custom":
        dateFrom = customFrom ? `${customFrom}T00:00:00` : undefined;
        dateTo = customTo ? `${customTo}T23:59:59.999` : undefined;
        break;
    }

    const shouldLoadScheduledTasks =
      showScheduleFrames &&
      scope === "project" &&
      period === "this_week" &&
      !!selectedProjectId;

    try {
      const scheduledTasksPromise = shouldLoadScheduledTasks
        ? taskApi.listTasks(selectedProjectId!).catch((err) => {
            console.error("予定枠用タスク取得に失敗しました", err);
            return [] as Task[];
          })
        : Promise.resolve([] as Task[]);
      const [r, entries, tasks] = await Promise.all([
        taskApi.getTimeReport(scopeArg, dateFrom, dateTo),
        taskApi.listTimeEntries(scopeArg, dateFrom, dateTo),
        scheduledTasksPromise,
      ]);

      if (scope !== "all" && entries.length === 0) {
        const [allReport, allEntries] = await Promise.all([
          taskApi.getTimeReport({}, dateFrom, dateTo),
          taskApi.listTimeEntries({}, dateFrom, dateTo),
        ]);
        if (allEntries.length > 0) {
          setScope("all");
          setReport(allReport);
          setTimeEntries(allEntries);
          setScheduledTasks([]);
          return;
        }
      }

      setReport(r);
      setTimeEntries(entries);
      setScheduledTasks(
        shouldLoadScheduledTasks
          ? tasks.filter((task) =>
              isTaskScheduledInRange(task, weekRange.monday, weekRange.sunday),
            )
          : [],
      );
    } catch (err) {
      console.error("レポート取得失敗:", err);
    } finally {
      setLoading(false);
    }
  }, [
    scope,
    selectedProjectId,
    selectedSpaceId,
    period,
    customFrom,
    customTo,
    showScheduleFrames,
    weekRange,
  ]);

  useTaskCompletionRefresh(fetchReport);

  useEffect(() => {
    if (!prefsLoaded) return;
    fetchReport();
  }, [fetchReport, prefsLoaded]);

  useEffect(() => {
    if (!prefsLoaded || !report?.summary.active_entries) return;
    const timer = window.setInterval(() => {
      fetchReport();
    }, 60 * 1000);
    return () => window.clearInterval(timer);
  }, [fetchReport, prefsLoaded, report?.summary.active_entries]);

  useEffect(() => {
    setWeekOffset(0);
  }, [period]);

  // 左右キーで週移動
  useEffect(() => {
    if (period !== "this_week") return;
    const handleKeyDown = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
      if (dragForm || editingEntry || resizeState || moveState || ctxMenu)
        return;
      if (e.key === "ArrowLeft") {
        e.preventDefault();
        setWeekOffset((o) => o - 1);
      } else if (e.key === "ArrowRight") {
        e.preventDefault();
        setWeekOffset((o) => Math.min(o + 1, 0));
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [period, dragForm, editingEntry, resizeState, moveState, ctxMenu]);

  const maxTaskSeconds =
    report?.by_task.reduce((max, b) => Math.max(max, b.seconds), 0) || 0;
  const maxDaySeconds =
    report?.by_day.reduce((max, b) => Math.max(max, b.seconds), 0) || 0;
  const maxUserSeconds =
    report?.by_user.reduce((max, b) => Math.max(max, b.seconds), 0) || 0;
  const maxProjectSeconds =
    report?.by_project?.reduce((max, b) => Math.max(max, b.seconds), 0) || 0;

  const entriesByDay = useMemo(
    () => groupEntriesByDay(timeEntries, weekDays),
    [timeEntries, weekDays],
  );
  const entryLayoutsByDay = useMemo(() => {
    const map = new Map<string, Map<string, EntryColumnLayout>>();
    for (const [dateKey, entries] of entriesByDay) {
      map.set(dateKey, buildEntryColumnLayouts(entries, now));
    }
    return map;
  }, [entriesByDay, now]);

  const canShowScheduleFrames =
    scope === "project" && period === "this_week" && !!selectedProjectId;
  const visibleScheduledTasks = canShowScheduleFrames ? scheduledTasks : [];

  const weekLabel = useMemo(() => {
    const mon = weekDays[0];
    const sun = weekDays[6];
    const fmt = (d: Date) => `${d.getMonth() + 1}/${d.getDate()}`;
    return `${fmt(mon)} ~ ${fmt(sun)}`;
  }, [weekDays]);

  const matchingDragTasks = useMemo(() => {
    const keyword = dragTaskName.trim().toLowerCase();
    const candidates = dragTaskOptions.filter((task) => !task.parent_task_id);
    if (!keyword) return candidates.slice(0, 6);
    return candidates
      .filter((task) => task.title.toLowerCase().includes(keyword))
      .slice(0, 6);
  }, [dragTaskName, dragTaskOptions]);

  const selectedDragTask = useMemo(
    () =>
      dragTaskOptions.find((task) => task.id === dragSelectedTaskId) || null,
    [dragSelectedTaskId, dragTaskOptions],
  );

  // マウスY座標 → 時間(15分単位)
  const calcHourFromMouseY = useCallback(
    (clientY: number, columnEl: HTMLElement): number => {
      const rect = columnEl.getBoundingClientRect();
      const y = clientY - rect.top;
      const pct = Math.max(0, Math.min(1, y / rect.height));
      const rawHour = HOUR_START + pct * TOTAL_HOURS;
      return Math.round(rawHour * 4) / 4;
    },
    [],
  );

  const handleDragMouseDown = useCallback(
    (e: React.MouseEvent<HTMLDivElement>, dayIndex: number) => {
      if (e.button !== 0) return;
      if ((e.target as HTMLElement).closest("[data-entry]")) return;
      if ((e.target as HTMLElement).closest("[data-drag-form]")) return;
      const hour = calcHourFromMouseY(
        e.clientY,
        e.currentTarget as HTMLDivElement,
      );
      isDraggingRef.current = true;
      setDragState({ dayIndex, startHour: hour, currentHour: hour });
      setDragForm(null);
    },
    [calcHourFromMouseY],
  );

  const handleDragMouseMove = useCallback(
    (e: React.MouseEvent<HTMLDivElement>, dayIndex: number) => {
      if (
        !isDraggingRef.current ||
        !dragState ||
        dragState.dayIndex !== dayIndex
      )
        return;
      const hour = calcHourFromMouseY(
        e.clientY,
        e.currentTarget as HTMLDivElement,
      );
      setDragState((prev) => (prev ? { ...prev, currentHour: hour } : null));
    },
    [dragState, calcHourFromMouseY],
  );

  const handleDragMouseUp = useCallback(() => {
    if (!isDraggingRef.current || !dragState) return;
    isDraggingRef.current = false;

    const startH = Math.min(dragState.startHour, dragState.currentHour);
    const endH = Math.max(dragState.startHour, dragState.currentHour);

    if (endH - startH < 0.25) {
      setDragState(null);
      return;
    }

    const topPct = ((startH - HOUR_START) / TOTAL_HOURS) * 100;

    setDragForm({
      dayIndex: dragState.dayIndex,
      startHour: startH,
      endHour: endH,
      topPct,
    });
    setDragState(null);
    setDragTaskName("");
    setDragSelectedTaskId(null);
    setTimeout(() => dragFormInputRef.current?.focus(), 50);
  }, [dragState]);

  const handleDragFormSubmit = useCallback(async () => {
    if (!dragForm) return;
    const targetProjectId = selectedProjectId;
    if (!targetProjectId) {
      alert("プロジェクトを選択してください。");
      return;
    }
    setDragCreating(true);
    try {
      const trimmedName = dragTaskName.trim();
      let taskId: string;

      if (dragSelectedTaskId) {
        taskId = dragSelectedTaskId;
      } else {
        if (!trimmedName) return;
        const newTask = await taskApi.createTask({
          project_id: targetProjectId,
          title: trimmedName,
          status: "open",
          priority: "normal",
        });
        taskId = newTask.id;
      }

      const day = weekDays[dragForm.dayIndex];
      const startDate = new Date(day);
      const startHourInt = Math.floor(dragForm.startHour);
      const startMin = Math.round((dragForm.startHour - startHourInt) * 60);
      startDate.setHours(startHourInt, startMin, 0, 0);

      const endDate = new Date(day);
      const endHourInt = Math.floor(dragForm.endHour);
      const endMin = Math.round((dragForm.endHour - endHourInt) * 60);
      endDate.setHours(endHourInt, endMin, 0, 0);

      await taskApi.createTimeEntry({
        task_id: taskId,
        started_at: formatLocalDateTime(startDate),
        ended_at: formatLocalDateTime(endDate),
      });

      setDragForm(null);
      setDragTaskName("");
      setDragSelectedTaskId(null);
      fetchReport();
    } catch (err) {
      console.error("タイムエントリ作成失敗:", err);
    } finally {
      setDragCreating(false);
    }
  }, [
    dragForm,
    dragSelectedTaskId,
    dragTaskName,
    selectedProjectId,
    weekDays,
    fetchReport,
  ]);

  const handleDragFormCancel = useCallback(() => {
    setDragForm(null);
    setDragTaskName("");
    setDragSelectedTaskId(null);
  }, []);

  // --- エントリクリック編集 ---
  const openEditDialog = useCallback((entry: TimeEntry) => {
    if (!entry.started_at) return;
    const start = new Date(entry.started_at);
    const end = entry.ended_at ? new Date(entry.ended_at) : new Date();
    const durationSec = Math.max(
      0,
      Math.floor((end.getTime() - start.getTime()) / 1000),
    );
    setEditingEntry(entry);
    setEditDate(toLocalYMD(start));
    setEditStart(toLocalHM(start));
    setEditEnd(toLocalHM(end));
    setEditDuration(formatDurationInput(durationSec));
    setEditNote(entry.note || "");
  }, []);

  const closeEditDialog = useCallback(() => {
    setEditingEntry(null);
    setEditSaving(false);
  }, []);

  const isEditingRunning = !!editingEntry && !editingEntry.ended_at;

  const handleEditStartBlur = useCallback(() => {
    const parsed = parseTimeInput(editStart);
    if (!parsed) {
      // 不正値は元の状態へ戻す
      if (editingEntry?.started_at) {
        setEditStart(toLocalHM(new Date(editingEntry.started_at)));
      }
      return;
    }
    setEditStart(parsed);
    const startDt = combineDateTime(editDate, parsed);
    const endDt = combineDateTime(editDate, editEnd);
    const diffSec = Math.floor((endDt.getTime() - startDt.getTime()) / 1000);
    if (diffSec >= 0) setEditDuration(formatDurationInput(diffSec));
  }, [editStart, editDate, editEnd, editingEntry]);

  const handleEditEndBlur = useCallback(() => {
    const parsed = parseTimeInput(editEnd);
    if (!parsed) {
      if (editingEntry?.ended_at) {
        setEditEnd(toLocalHM(new Date(editingEntry.ended_at)));
      }
      return;
    }
    setEditEnd(parsed);
    const startDt = combineDateTime(editDate, editStart);
    const endDt = combineDateTime(editDate, parsed);
    const diffSec = Math.floor((endDt.getTime() - startDt.getTime()) / 1000);
    if (diffSec >= 0) setEditDuration(formatDurationInput(diffSec));
  }, [editEnd, editDate, editStart, editingEntry]);

  const handleEditDurationBlur = useCallback(() => {
    const sec = parseDurationInput(editDuration);
    if (sec === null || sec < 0) {
      // 復元
      const startDt = combineDateTime(editDate, editStart);
      const endDt = combineDateTime(editDate, editEnd);
      const diffSec = Math.floor((endDt.getTime() - startDt.getTime()) / 1000);
      setEditDuration(formatDurationInput(Math.max(0, diffSec)));
      return;
    }
    setEditDuration(formatDurationInput(sec));
    if (isEditingRunning) {
      // 計測中: 開始時刻をずらす（end は現在時刻として扱う）
      const endDt = combineDateTime(editDate, editEnd);
      const newStart = new Date(endDt.getTime() - sec * 1000);
      setEditDate(toLocalYMD(newStart));
      setEditStart(toLocalHM(newStart));
    } else {
      // 停止中: 終了時刻を伸ばす
      const startDt = combineDateTime(editDate, editStart);
      const newEnd = new Date(startDt.getTime() + sec * 1000);
      setEditEnd(toLocalHM(newEnd));
    }
  }, [editDuration, editDate, editStart, editEnd, isEditingRunning]);

  const saveEditEntry = useCallback(
    async (keepOpen: boolean) => {
      if (!editingEntry) return;
      if (!editDate || !editStart) return;
      const newStart = combineDateTime(editDate, editStart);
      setEditSaving(true);
      try {
        if (isEditingRunning) {
          await taskApi.updateTimeEntry(editingEntry.id, {
            started_at: formatLocalDateTime(newStart),
            note: editNote,
          });
        } else {
          if (!editEnd) return;
          const newEnd = combineDateTime(editDate, editEnd);
          if (newEnd <= newStart) {
            alert("終了時刻は開始時刻より後にしてください");
            setEditSaving(false);
            return;
          }
          await taskApi.updateTimeEntry(editingEntry.id, {
            started_at: formatLocalDateTime(newStart),
            ended_at: formatLocalDateTime(newEnd),
            note: editNote,
          });
        }
        if (!keepOpen) closeEditDialog();
        fetchReport();
      } catch (err) {
        console.error("タイムエントリ更新失敗:", err);
        alert("更新に失敗しました");
      } finally {
        setEditSaving(false);
      }
    },
    [
      editingEntry,
      editDate,
      editStart,
      editEnd,
      editNote,
      isEditingRunning,
      closeEditDialog,
      fetchReport,
    ],
  );

  const handleEditSave = useCallback(
    () => saveEditEntry(false),
    [saveEditEntry],
  );

  const saveEditEntryRef = useRef(saveEditEntry);
  useEffect(() => {
    saveEditEntryRef.current = saveEditEntry;
  }, [saveEditEntry]);

  const handleEditInputEnter = useCallback(
    (e: ReactKeyboardEvent<HTMLInputElement>) => {
      if (e.key !== "Enter" || e.nativeEvent.isComposing) return;
      e.preventDefault();
      e.currentTarget.blur();
      // blur → onBlur で state が正規化された後に保存するため次tickへ
      setTimeout(() => {
        void saveEditEntryRef.current(true);
      }, 0);
    },
    [],
  );

  const handleEditDelete = useCallback(async () => {
    if (!editingEntry) return;
    setEditSaving(true);
    try {
      await taskApi.deleteTimeEntry(editingEntry.id);
      closeEditDialog();
      fetchReport();
    } catch (err) {
      console.error("タイムエントリ削除失敗:", err);
      alert("削除に失敗しました");
    } finally {
      setEditSaving(false);
    }
  }, [editingEntry, closeEditDialog, fetchReport]);

  const handleEditDuplicate = useCallback(async () => {
    if (!editingEntry) return;
    if (!editingEntry.started_at || !editingEntry.ended_at) {
      alert("計測中のエントリは複製できません");
      return;
    }
    setEditSaving(true);
    try {
      await taskApi.createTimeEntry({
        task_id: editingEntry.task_id,
        started_at: editingEntry.started_at,
        ended_at: editingEntry.ended_at,
        note: editingEntry.note || undefined,
      });
      closeEditDialog();
      fetchReport();
    } catch (err) {
      console.error("タイムエントリ複製失敗:", err);
      alert("複製に失敗しました");
    } finally {
      setEditSaving(false);
    }
  }, [editingEntry, closeEditDialog, fetchReport]);

  const handleEditRestartTimer = useCallback(async () => {
    if (!editingEntry) return;
    setEditSaving(true);
    try {
      await taskApi.startTimer(editingEntry.task_id);
      closeEditDialog();
      fetchReport();
      window.dispatchEvent(new Event("task-list-refresh"));
    } catch (err) {
      console.error("タイマー開始失敗:", err);
      alert("タイマー開始に失敗しました");
    } finally {
      setEditSaving(false);
    }
  }, [editingEntry, closeEditDialog, fetchReport]);

  const handleEditStopTimer = useCallback(async () => {
    if (!editingEntry) return;
    setEditSaving(true);
    try {
      await taskApi.stopTimer(editingEntry.id);
      closeEditDialog();
      fetchReport();
      window.dispatchEvent(new Event("task-list-refresh"));
    } catch (err) {
      console.error("タイマー停止失敗:", err);
      alert("タイマー停止に失敗しました");
    } finally {
      setEditSaving(false);
    }
  }, [editingEntry, closeEditDialog, fetchReport]);

  const handleEditRevertToOriginal = useCallback(() => {
    if (!editingEntry?.original_started_at || !editingEntry?.original_ended_at)
      return;
    const origStart = new Date(editingEntry.original_started_at);
    const origEnd = new Date(editingEntry.original_ended_at);
    const durationSec = Math.max(
      0,
      Math.floor((origEnd.getTime() - origStart.getTime()) / 1000),
    );
    setEditDate(toLocalYMD(origStart));
    setEditStart(toLocalHM(origStart));
    setEditEnd(toLocalHM(origEnd));
    setEditDuration(formatDurationInput(durationSec));
  }, [editingEntry]);

  const handleOpenTaskDetail = useCallback(() => {
    if (!editingEntry) return;
    setSelectedEntry(editingEntry);
    setSelectedTaskId(editingEntry.task_id);
    setEditingEntry(null);
  }, [editingEntry]);

  const handleEditMoveTaskProject = useCallback(
    async (projectId: string) => {
      if (
        !editingEntry ||
        !projectId ||
        projectId === editingEntry.project_id
      ) {
        return;
      }
      setEditSaving(true);
      try {
        await taskApi.moveTask(editingEntry.task_id, {
          project_id: projectId,
        });
        const nextProject =
          allProjects.find((project) => project.id === projectId) ?? null;
        const nextSpace = nextProject?.space_id
          ? (spaces.find((space) => space.id === nextProject.space_id) ?? null)
          : null;
        setEditingEntry((prev) =>
          prev
            ? {
                ...prev,
                project_id: projectId,
                project_name: nextProject?.name ?? prev.project_name,
                space_id: nextProject?.space_id ?? null,
                space_name: nextSpace?.name ?? null,
              }
            : prev,
        );
        fetchReport();
        window.dispatchEvent(new Event("task-list-refresh"));
      } catch (err) {
        console.error("タスクのプロジェクト移動に失敗", err);
        alert("プロジェクトの変更に失敗しました");
      } finally {
        setEditSaving(false);
      }
    },
    [allProjects, editingEntry, fetchReport, spaces],
  );

  const handleEditMoveTaskSpace = useCallback(
    async (spaceId: string) => {
      if (!spaceId) return;
      const currentSpaceId =
        currentEditingSpace?.id ?? editingEntry?.space_id ?? null;
      if (spaceId === currentSpaceId) return;
      const targetProject = allProjects.find(
        (project) => project.space_id === spaceId,
      );
      if (!targetProject) {
        alert("このスペースに移動できるプロジェクトがありません");
        return;
      }
      await handleEditMoveTaskProject(targetProject.id);
    },
    [
      allProjects,
      currentEditingSpace?.id,
      editingEntry?.space_id,
      handleEditMoveTaskProject,
    ],
  );

  // --- リサイズD&D ---
  const handleResizeMouseDown = useCallback(
    (
      e: React.MouseEvent<HTMLDivElement>,
      entry: TimeEntry,
      edge: "top" | "bottom",
      dayIndex: number,
    ) => {
      e.stopPropagation();
      e.preventDefault();
      if (!entry.started_at || !entry.ended_at) return;
      const start = new Date(entry.started_at);
      const end = new Date(entry.ended_at);
      const startHour = start.getHours() + start.getMinutes() / 60;
      const endHour = end.getHours() + end.getMinutes() / 60;
      isResizingRef.current = true;
      setResizeState({
        entryId: entry.id,
        edge,
        dayIndex,
        originalStartHour: startHour,
        originalEndHour: endHour,
        currentHour: edge === "top" ? startHour : endHour,
      });
    },
    [],
  );

  useEffect(() => {
    if (!resizeState) return;
    const handleMove = (e: MouseEvent) => {
      const col = dayColRefs.current[resizeState.dayIndex];
      if (!col) return;
      const hour = calcHourFromMouseY(e.clientY, col);
      setResizeState((prev) => (prev ? { ...prev, currentHour: hour } : null));
    };
    const handleUp = async () => {
      const state = resizeState;
      if (!state) return;
      isResizingRef.current = false;

      const entry = timeEntries.find((x) => x.id === state.entryId);
      if (!entry || !entry.started_at || !entry.ended_at) {
        setResizeState(null);
        return;
      }

      let newStartHour = state.originalStartHour;
      let newEndHour = state.originalEndHour;
      if (state.edge === "top") {
        newStartHour = Math.min(
          state.currentHour,
          state.originalEndHour - 0.25,
        );
      } else {
        newEndHour = Math.max(
          state.currentHour,
          state.originalStartHour + 0.25,
        );
      }

      setResizeState(null);

      // 変化なしならAPIを呼ばない
      if (
        (state.edge === "top" && newStartHour === state.originalStartHour) ||
        (state.edge === "bottom" && newEndHour === state.originalEndHour)
      ) {
        openEditDialog(entry);
        return;
      }

      const baseDate = new Date(
        state.edge === "top" ? entry.started_at : entry.ended_at,
      );
      baseDate.setHours(0, 0, 0, 0);

      const applyHour = (d: Date, h: number) => {
        const intH = Math.floor(h);
        const m = Math.round((h - intH) * 60);
        d.setHours(intH, m, 0, 0);
        return d;
      };

      const payload: { started_at?: string; ended_at?: string } = {};
      if (state.edge === "top") {
        const d = new Date(baseDate);
        applyHour(d, newStartHour);
        payload.started_at = formatLocalDateTime(d);
      } else {
        const d = new Date(baseDate);
        applyHour(d, newEndHour);
        payload.ended_at = formatLocalDateTime(d);
      }

      try {
        await taskApi.updateTimeEntry(entry.id, payload);
        fetchReport();
      } catch (err) {
        console.error("タイムエントリ更新失敗:", err);
      }
    };
    window.addEventListener("mousemove", handleMove);
    window.addEventListener("mouseup", handleUp);
    return () => {
      window.removeEventListener("mousemove", handleMove);
      window.removeEventListener("mouseup", handleUp);
    };
  }, [
    resizeState,
    timeEntries,
    calcHourFromMouseY,
    fetchReport,
    openEditDialog,
  ]);

  // --- 移動D&D ---
  const handleEntryMouseDown = useCallback(
    (
      e: React.MouseEvent<HTMLDivElement>,
      entry: TimeEntry,
      dayIndex: number,
    ) => {
      if (e.button !== 0) return;
      if ((e.target as HTMLElement).closest("[data-resize-handle]")) return;
      if (!entry.started_at || !entry.ended_at) return;
      e.stopPropagation();
      e.preventDefault();
      const start = new Date(entry.started_at);
      const end = new Date(entry.ended_at);
      const startHour = start.getHours() + start.getMinutes() / 60;
      const endHour = end.getHours() + end.getMinutes() / 60;
      const durationHours = endHour - startHour;
      const col = dayColRefs.current[dayIndex];
      const cursorHour = col ? calcHourFromMouseY(e.clientY, col) : startHour;
      isMovingRef.current = true;
      const initial: MoveState = {
        entryId: entry.id,
        originalStartedAt: entry.started_at,
        originalEndedAt: entry.ended_at,
        originalDayIndex: dayIndex,
        originalStartHour: startHour,
        durationHours,
        pointerOffsetHours: cursorHour - startHour,
        mouseStartX: e.clientX,
        mouseStartY: e.clientY,
        currentDayIndex: dayIndex,
        currentStartHour: startHour,
        moving: false,
      };
      moveStateRef.current = initial;
      setMoveState(initial);
    },
    [calcHourFromMouseY],
  );

  const moveActive = !!moveState;
  useEffect(() => {
    if (!moveActive) return;
    const THRESHOLD_PX = 4;

    const handleMove = (e: MouseEvent) => {
      const prev = moveStateRef.current;
      if (!prev) return;
      const next = { ...prev };
      if (!next.moving) {
        const dx = e.clientX - next.mouseStartX;
        const dy = e.clientY - next.mouseStartY;
        if (Math.hypot(dx, dy) < THRESHOLD_PX) return;
        next.moving = true;
      }
      let targetDay = next.currentDayIndex;
      for (let i = 0; i < dayColRefs.current.length; i++) {
        const c = dayColRefs.current[i];
        if (!c) continue;
        const r = c.getBoundingClientRect();
        if (e.clientX >= r.left && e.clientX <= r.right) {
          targetDay = i;
          break;
        }
      }
      const col = dayColRefs.current[targetDay];
      if (!col) {
        moveStateRef.current = next;
        setMoveState(next);
        return;
      }
      const cursorHour = calcHourFromMouseY(e.clientY, col);
      let newStart = cursorHour - next.pointerOffsetHours;
      newStart = Math.max(
        HOUR_START,
        Math.min(HOUR_END - next.durationHours, newStart),
      );
      newStart = Math.round(newStart * 4) / 4;
      next.currentDayIndex = targetDay;
      next.currentStartHour = newStart;
      moveStateRef.current = next;
      setMoveState(next);
    };

    const handleUp = async () => {
      const state = moveStateRef.current;
      moveStateRef.current = null;
      isMovingRef.current = false;
      setMoveState(null);
      if (!state) return;

      const entry = timeEntries.find((x) => x.id === state.entryId);
      if (!entry) return;

      if (!state.moving) {
        // 実質クリック: 記録編集を開く
        openEditDialog(entry);
        return;
      }

      // 変化がなければ何もしない
      if (
        state.currentDayIndex === state.originalDayIndex &&
        state.currentStartHour === state.originalStartHour
      ) {
        return;
      }

      const day = weekDays[state.currentDayIndex];
      if (!day) return;
      const sH = Math.floor(state.currentStartHour);
      const sM = Math.round((state.currentStartHour - sH) * 60);
      const newStart = new Date(day);
      newStart.setHours(sH, sM, 0, 0);
      const newEnd = new Date(
        newStart.getTime() + state.durationHours * 3600 * 1000,
      );

      try {
        await taskApi.updateTimeEntry(entry.id, {
          started_at: formatLocalDateTime(newStart),
          ended_at: formatLocalDateTime(newEnd),
        });
        fetchReport();
      } catch (err) {
        console.error("タイムエントリ移動失敗:", err);
      }
    };

    window.addEventListener("mousemove", handleMove);
    window.addEventListener("mouseup", handleUp);
    return () => {
      window.removeEventListener("mousemove", handleMove);
      window.removeEventListener("mouseup", handleUp);
    };
  }, [
    moveActive,
    timeEntries,
    weekDays,
    calcHourFromMouseY,
    openEditDialog,
    fetchReport,
  ]);

  // --- 右クリックメニュー ---
  const handleEntryContextMenu = useCallback(
    (e: React.MouseEvent<HTMLDivElement>, entry: TimeEntry) => {
      e.preventDefault();
      e.stopPropagation();
      setCtxMenu({ entry, x: e.clientX, y: e.clientY });
    },
    [],
  );

  const handleCtxOpenDetail = useCallback(() => {
    if (!ctxMenu) return;
    setSelectedEntry(ctxMenu.entry);
    setSelectedTaskId(ctxMenu.entry.task_id);
    setCtxMenu(null);
  }, [ctxMenu]);

  const handleCtxEdit = useCallback(() => {
    if (!ctxMenu) return;
    const entry = ctxMenu.entry;
    setCtxMenu(null);
    if (entry.ended_at) openEditDialog(entry);
  }, [ctxMenu, openEditDialog]);

  const handleCtxDuplicate = useCallback(async () => {
    if (!ctxMenu) return;
    const entry = ctxMenu.entry;
    setCtxMenu(null);
    if (!entry.started_at || !entry.ended_at) {
      alert("計測中のエントリは複製できません");
      return;
    }
    try {
      await taskApi.createTimeEntry({
        task_id: entry.task_id,
        started_at: entry.started_at,
        ended_at: entry.ended_at,
        note: entry.note || undefined,
      });
      fetchReport();
    } catch (err) {
      console.error("タイムエントリ複製失敗:", err);
      alert("複製に失敗しました");
    }
  }, [ctxMenu, fetchReport]);

  const handleCtxDelete = useCallback(async () => {
    if (!ctxMenu) return;
    const entry = ctxMenu.entry;
    setCtxMenu(null);
    try {
      await taskApi.deleteTimeEntry(entry.id);
      fetchReport();
    } catch (err) {
      console.error("タイムエントリ削除失敗:", err);
      alert("削除に失敗しました");
    }
  }, [ctxMenu, fetchReport]);

  // メニュー外クリック / Esc で閉じる
  useEffect(() => {
    if (!ctxMenu) return;
    const onDown = (e: MouseEvent) => {
      if (!(e.target as HTMLElement | null)?.closest("[data-ctx-menu]")) {
        setCtxMenu(null);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setCtxMenu(null);
    };
    const onScroll = () => setCtxMenu(null);
    // contextmenu イベント発火直後の同じ mousedown で閉じないよう次tick
    const t = window.setTimeout(() => {
      window.addEventListener("mousedown", onDown);
      window.addEventListener("keydown", onKey);
      window.addEventListener("scroll", onScroll, true);
    }, 0);
    return () => {
      window.clearTimeout(t);
      window.removeEventListener("mousedown", onDown);
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("scroll", onScroll, true);
    };
  }, [ctxMenu]);

  const hasScope =
    scope === "all"
      ? true
      : scope === "space"
        ? !!selectedSpaceId
        : !!selectedProjectId;

  return (
    <div className="flex flex-col gap-4 p-4 pb-16">
      {/* ヘッダー */}
      <div className="flex flex-wrap items-center gap-3">
        <Tabs value={scope} onValueChange={(v) => setScope(v as ScopeMode)}>
          <TabsList>
            <TabsTrigger value="project">プロジェクト単位</TabsTrigger>
            <TabsTrigger value="space">スペース単位</TabsTrigger>
            <TabsTrigger value="all">全表示</TabsTrigger>
          </TabsList>
        </Tabs>

        <div className="text-xs text-muted-foreground">
          {scope === "all"
            ? "全プロジェクト横断"
            : scope === "space"
              ? selectedSpace
                ? `スペース: ${selectedSpace.name}`
                : "スペース未選択"
              : selectedProject
                ? `プロジェクト: ${selectedProject.name}`
                : "プロジェクト未選択"}
        </div>

        <div className="ml-auto flex flex-wrap items-center gap-3">
          <Tabs
            value={period}
            onValueChange={(v) => setPeriod(v as PeriodPreset)}
          >
            <TabsList>
              <TabsTrigger value="this_week">今週</TabsTrigger>
              <TabsTrigger value="this_month">今月</TabsTrigger>
              <TabsTrigger value="custom">カスタム</TabsTrigger>
            </TabsList>
          </Tabs>

          {period === "custom" && (
            <div className="flex items-center gap-2">
              <Input
                type="date"
                value={customFrom}
                onChange={(e) => setCustomFrom(e.target.value)}
                className="w-36"
              />
              <span className="text-sm text-muted-foreground">~</span>
              <Input
                type="date"
                value={customTo}
                onChange={(e) => setCustomTo(e.target.value)}
                className="w-36"
              />
            </div>
          )}

          {period === "this_week" && (
            <div className="flex items-center gap-2">
              <Button
                variant="outline"
                size="sm"
                onClick={() => setWeekOffset((o) => o - 1)}
              >
                <ChevronLeft className="size-4" />
                前の週
              </Button>
              <span className="text-sm font-medium min-w-[120px] text-center">
                {weekLabel}
              </span>
              <Button
                variant="outline"
                size="sm"
                onClick={() => setWeekOffset((o) => o + 1)}
                disabled={weekOffset >= 0}
              >
                次の週
                <ChevronRight className="size-4" />
              </Button>
              {weekOffset !== 0 && (
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => setWeekOffset(0)}
                >
                  今週
                </Button>
              )}
            </div>
          )}
          {period === "this_week" && (
            <label
              className={`flex items-center gap-2 rounded-md border px-3 py-2 text-sm ${
                canShowScheduleFrames
                  ? "border-border text-foreground"
                  : "border-border/60 text-muted-foreground"
              }`}
            >
              <Checkbox
                checked={showScheduleFrames}
                disabled={!canShowScheduleFrames}
                onCheckedChange={(checked) =>
                  setShowScheduleFrames(checked === true)
                }
              />
              <span>予定時間の枠を表示</span>
            </label>
          )}
        </div>
      </div>

      {loading ? (
        <div className="space-y-4">
          <div className="grid grid-cols-3 gap-4">
            {Array.from({ length: 3 }).map((_, i) => (
              <Skeleton key={i} className="h-24 rounded-xl" />
            ))}
          </div>
          <Skeleton className="h-48 rounded-xl" />
        </div>
      ) : !hasScope ? (
        <div className="flex items-center justify-center py-16 text-muted-foreground">
          {scope === "space"
            ? "スペースを選択してください"
            : "プロジェクトを選択してください"}
        </div>
      ) : !report ? (
        <div className="flex items-center justify-center py-16 text-muted-foreground">
          データがありません
        </div>
      ) : (
        <>
          {/* サマリーカード */}
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
            <Card size="sm">
              <CardHeader>
                <CardTitle className="flex items-center gap-2 text-sm font-medium text-muted-foreground">
                  <Clock className="size-4" />
                  合計作業時間
                </CardTitle>
              </CardHeader>
              <CardContent>
                <p className="text-2xl font-bold">
                  {formatSeconds(report.summary.total_seconds)}
                </p>
              </CardContent>
            </Card>
            <Card size="sm">
              <CardHeader>
                <CardTitle className="flex items-center gap-2 text-sm font-medium text-muted-foreground">
                  <ListChecks className="size-4" />
                  エントリ数
                </CardTitle>
              </CardHeader>
              <CardContent>
                <p className="text-2xl font-bold">
                  {report.summary.entry_count}
                </p>
              </CardContent>
            </Card>
            <Card size="sm">
              <CardHeader>
                <CardTitle className="flex items-center gap-2 text-sm font-medium text-muted-foreground">
                  <Activity className="size-4" />
                  計測中
                </CardTitle>
              </CardHeader>
              <CardContent>
                <p className="text-2xl font-bold">
                  {report.summary.active_entries}
                </p>
              </CardContent>
            </Card>
          </div>

          {/* 週間タイムライン */}
          {period === "this_week" && (
            <Card size="sm" className="overflow-visible">
              <CardHeader>
                <CardTitle className="text-sm">
                  週間タイムライン
                  <span className="ml-2 text-xs font-normal text-muted-foreground">
                    (クリック:編集 / ドラッグ:移動 / 上下端:リサイズ /
                    右クリック:メニュー)
                  </span>
                </CardTitle>
              </CardHeader>
              <CardContent className="p-0">
                <div className="min-w-[800px]">
                  {/* 曜日ヘッダー */}
                  <div className="grid grid-cols-[60px_repeat(7,1fr)] border-b border-border">
                    <div className="p-2" />
                    {weekDays.map((day, i) => {
                      const dateKey = toLocalYMD(day);
                      const dayEntries = entriesByDay.get(dateKey) || [];
                      const totalSec = getDayTotalSeconds(dayEntries, now);
                      const isToday = dateKey === toLocalYMD(new Date());
                      return (
                        <div
                          key={dateKey}
                          className={`p-2 text-center border-l border-border ${
                            isToday ? "bg-primary/10" : ""
                          }`}
                        >
                          <div
                            className={`text-xs font-semibold ${
                              isToday
                                ? "text-primary"
                                : i >= 5
                                  ? "text-muted-foreground"
                                  : ""
                            }`}
                          >
                            {DAY_LABELS[i]}
                          </div>
                          <div className="text-xs text-muted-foreground">
                            {day.getMonth() + 1}/{day.getDate()}
                          </div>
                          {totalSec > 0 && (
                            <div className="text-xs font-medium text-primary mt-0.5">
                              {formatHours(totalSec)}
                            </div>
                          )}
                        </div>
                      );
                    })}
                  </div>

                  {/* タイムグリッド */}
                  <div
                    className="grid grid-cols-[60px_repeat(7,1fr)] relative"
                    style={{ height: `${TOTAL_HOURS * 48}px` }}
                  >
                    <div className="relative">
                      {Array.from({ length: TOTAL_HOURS + 1 }, (_, i) => (
                        <div
                          key={i}
                          className="absolute right-2 text-[10px] text-muted-foreground -translate-y-1/2"
                          style={{ top: `${(i / TOTAL_HOURS) * 100}%` }}
                        >
                          {HOUR_START + i}:00
                        </div>
                      ))}
                    </div>

                    {weekDays.map((day, dayIndex) => {
                      const dateKey = toLocalYMD(day);
                      let dayEntries = entriesByDay.get(dateKey) || [];
                      const entryLayouts =
                        entryLayoutsByDay.get(dateKey) ?? new Map();
                      if (moveState?.moving) {
                        dayEntries = dayEntries.filter(
                          (e) => e.id !== moveState.entryId,
                        );
                        if (
                          moveState.currentDayIndex === dayIndex &&
                          moveState.originalDayIndex !== dayIndex
                        ) {
                          const movingEntry = timeEntries.find(
                            (e) => e.id === moveState.entryId,
                          );
                          if (movingEntry)
                            dayEntries = [...dayEntries, movingEntry];
                        }
                      }
                      const isToday = dateKey === toLocalYMD(new Date());

                      const showDragPreview =
                        dragState && dragState.dayIndex === dayIndex;
                      let dragPreviewTop = 0;
                      let dragPreviewHeight = 0;
                      if (showDragPreview && dragState) {
                        const s = Math.min(
                          dragState.startHour,
                          dragState.currentHour,
                        );
                        const e = Math.max(
                          dragState.startHour,
                          dragState.currentHour,
                        );
                        dragPreviewTop = ((s - HOUR_START) / TOTAL_HOURS) * 100;
                        dragPreviewHeight = ((e - s) / TOTAL_HOURS) * 100;
                      }

                      const showDragForm =
                        dragForm && dragForm.dayIndex === dayIndex;
                      let dragFormTop = 0;
                      let dragFormHeight = 0;
                      if (showDragForm && dragForm) {
                        dragFormTop =
                          ((dragForm.startHour - HOUR_START) / TOTAL_HOURS) *
                          100;
                        dragFormHeight =
                          ((dragForm.endHour - dragForm.startHour) /
                            TOTAL_HOURS) *
                          100;
                      }

                      const nowDateKey = toLocalYMD(now);
                      const nowHour = now.getHours() + now.getMinutes() / 60;
                      const showNowLine =
                        dateKey === nowDateKey &&
                        nowHour >= HOUR_START &&
                        nowHour <= HOUR_END;
                      const nowLineTop =
                        ((nowHour - HOUR_START) / TOTAL_HOURS) * 100;
                      const dayScheduleFrames = visibleScheduledTasks
                        .map((task) => {
                          const segment = getTaskScheduleSegmentForDay(
                            task,
                            day,
                          );
                          if (!segment) return null;
                          return { task, ...segment };
                        })
                        .filter(
                          (
                            value,
                          ): value is {
                            task: Task;
                            startHour: number;
                            endHour: number;
                          } => value !== null,
                        );

                      return (
                        <div
                          key={dateKey}
                          ref={(el) => {
                            dayColRefs.current[dayIndex] = el;
                          }}
                          data-day-col={dayIndex}
                          className={`relative border-l border-border select-none ${
                            isToday ? "bg-primary/5" : ""
                          }`}
                          onMouseDown={(e) => handleDragMouseDown(e, dayIndex)}
                          onMouseMove={(e) => handleDragMouseMove(e, dayIndex)}
                          onMouseUp={handleDragMouseUp}
                          onMouseLeave={() => {
                            if (isDraggingRef.current) handleDragMouseUp();
                          }}
                          style={{
                            cursor: isResizingRef.current
                              ? "ns-resize"
                              : isMovingRef.current
                                ? "grabbing"
                                : isDraggingRef.current
                                  ? "ns-resize"
                                  : "crosshair",
                          }}
                        >
                          {/* 時間区切り */}
                          {Array.from({ length: TOTAL_HOURS + 1 }, (_, i) => (
                            <div
                              key={i}
                              className="absolute left-0 right-0 border-t border-border/40"
                              style={{
                                top: `${(i / TOTAL_HOURS) * 100}%`,
                              }}
                            />
                          ))}

                          {showNowLine && (
                            <div
                              className="absolute left-0 right-0 z-10 border-t-2 border-red-500/80 pointer-events-none"
                              style={{ top: `${nowLineTop}%` }}
                            >
                              <div className="absolute -left-1 -top-1 size-2 rounded-full bg-red-500" />
                            </div>
                          )}

                          {/* 新規作成プレビュー */}
                          {dayScheduleFrames.map(
                            ({ task, startHour, endHour }) => {
                              const clampedStart = Math.max(
                                startHour,
                                HOUR_START,
                              );
                              const clampedEnd = Math.min(endHour, HOUR_END);
                              if (
                                clampedEnd <= HOUR_START ||
                                clampedStart >= HOUR_END
                              ) {
                                return null;
                              }
                              const endPct =
                                ((clampedEnd - HOUR_START) / TOTAL_HOURS) * 100;
                              const heightPct =
                                ((clampedEnd - clampedStart) / TOTAL_HOURS) *
                                100;
                              return (
                                <div
                                  key={`schedule-${task.id}`}
                                  className="absolute left-1 right-1 z-[1] overflow-hidden rounded-md border border-dashed border-primary/55 bg-primary/8 pointer-events-none shadow-[inset_0_1px_rgba(255,255,255,0.42)] backdrop-blur-sm"
                                  style={{
                                    top: `${endPct}%`,
                                    height: `${heightPct}%`,
                                    minHeight: "18px",
                                    transform: "translateY(-100%)",
                                  }}
                                  title={formatTaskScheduleLabel(task)}
                                >
                                  {heightPct > 4 && (
                                    <div className="px-1 py-0.5 text-[9px] font-medium leading-tight text-primary/90 truncate">
                                      {task.title}
                                    </div>
                                  )}
                                </div>
                              );
                            },
                          )}

                          {showDragPreview && dragPreviewHeight > 0 && (
                            <div
                              className="absolute left-0.5 right-0.5 rounded bg-primary/30 border-2 border-primary/50 z-20 pointer-events-none"
                              style={{
                                top: `${dragPreviewTop}%`,
                                height: `${Math.max(dragPreviewHeight, 1)}%`,
                                minHeight: "12px",
                              }}
                            />
                          )}

                          {/* 新規作成フォーム */}
                          {showDragForm && dragForm && (
                            <>
                              <div
                                className="absolute left-0.5 right-0.5 rounded bg-primary/20 border-2 border-primary/60 z-20 pointer-events-none"
                                style={{
                                  top: `${dragFormTop}%`,
                                  height: `${Math.max(dragFormHeight, 1)}%`,
                                  minHeight: "18px",
                                }}
                              >
                                <div className="text-[9px] text-primary px-1 pt-0.5 font-medium">
                                  {Math.floor(dragForm.startHour)}:
                                  {String(
                                    Math.round((dragForm.startHour % 1) * 60),
                                  ).padStart(2, "0")}
                                  {" ~ "}
                                  {Math.floor(dragForm.endHour)}:
                                  {String(
                                    Math.round((dragForm.endHour % 1) * 60),
                                  ).padStart(2, "0")}
                                </div>
                              </div>
                              <div
                                data-drag-form
                                className="absolute left-0 right-0 z-30"
                                style={{
                                  top: `calc(${dragFormTop + dragFormHeight}% + 4px)`,
                                }}
                              >
                                <div className="mx-0.5 bg-popover border border-border rounded-md shadow-lg p-2">
                                  <input
                                    ref={dragFormInputRef}
                                    type="text"
                                    className="w-full text-xs bg-transparent border border-input rounded px-2 py-1 focus:outline-none focus:ring-1 focus:ring-primary"
                                    placeholder="タスク名を入力..."
                                    value={dragTaskName}
                                    onChange={(e) => {
                                      setDragTaskName(e.target.value);
                                      if (dragSelectedTaskId) {
                                        setDragSelectedTaskId(null);
                                      }
                                    }}
                                    onKeyDown={(e) => {
                                      if (e.key === "Enter") {
                                        e.preventDefault();
                                        handleDragFormSubmit();
                                      } else if (e.key === "Escape") {
                                        handleDragFormCancel();
                                      }
                                    }}
                                    disabled={dragCreating}
                                  />
                                  {selectedDragTask && (
                                    <div className="mt-1 text-[10px] text-primary">
                                      選択中: {selectedDragTask.title}
                                    </div>
                                  )}
                                  <div className="mt-1 space-y-1">
                                    <div className="text-[10px] text-muted-foreground">
                                      既存タスクを選ぶか、タスク名を入力して新規作成します。
                                    </div>
                                    {dragTaskLoading ? (
                                      <div className="text-[10px] text-muted-foreground">
                                        読み込み中...
                                      </div>
                                    ) : matchingDragTasks.length > 0 ? (
                                      <div className="flex flex-wrap gap-1">
                                        {matchingDragTasks.map((task) => (
                                          <button
                                            key={task.id}
                                            type="button"
                                            className={`rounded border px-2 py-0.5 text-[10px] ${
                                              dragSelectedTaskId === task.id
                                                ? "border-primary bg-primary/15 text-primary"
                                                : "border-border bg-muted/40 text-muted-foreground"
                                            }`}
                                            onClick={() => {
                                              setDragSelectedTaskId(task.id);
                                              setDragTaskName(task.title);
                                            }}
                                            disabled={dragCreating}
                                          >
                                            {task.title}
                                          </button>
                                        ))}
                                      </div>
                                    ) : (
                                      <div className="text-[10px] text-muted-foreground">
                                        一致する既存タスクは見つかりません
                                      </div>
                                    )}
                                  </div>
                                  <div className="flex gap-1 mt-1">
                                    <button
                                      className="flex-1 text-[10px] bg-primary text-primary-foreground rounded px-2 py-0.5 hover:bg-primary/90 disabled:opacity-50"
                                      onClick={handleDragFormSubmit}
                                      disabled={
                                        dragCreating ||
                                        (!dragSelectedTaskId &&
                                          !dragTaskName.trim())
                                      }
                                    >
                                      {dragCreating ? "作成中..." : "作成"}
                                    </button>
                                    <button
                                      className="flex-1 text-[10px] bg-muted text-muted-foreground rounded px-2 py-0.5 hover:bg-muted/80"
                                      onClick={handleDragFormCancel}
                                      disabled={dragCreating}
                                    >
                                      キャンセル
                                    </button>
                                  </div>
                                </div>
                              </div>
                            </>
                          )}

                          {/* タイムエントリ */}
                          {dayEntries.map((entry) => {
                            if (!entry.started_at) return null;
                            const range = getEntryHourRange(entry, now);
                            if (!range) return null;
                            let { startHour, endHour } = range;

                            // リサイズ中の見た目
                            if (
                              resizeState &&
                              resizeState.entryId === entry.id
                            ) {
                              if (resizeState.edge === "top") {
                                startHour = Math.min(
                                  resizeState.currentHour,
                                  resizeState.originalEndHour - 0.25,
                                );
                              } else {
                                endHour = Math.max(
                                  resizeState.currentHour,
                                  resizeState.originalStartHour + 0.25,
                                );
                              }
                            }

                            // 移動中の見た目（位置オーバーライド）
                            const isMovingThis =
                              moveState?.moving &&
                              moveState.entryId === entry.id &&
                              moveState.currentDayIndex === dayIndex;
                            if (isMovingThis && moveState) {
                              startHour = moveState.currentStartHour;
                              endHour = startHour + moveState.durationHours;
                            }

                            const clampedStart = Math.max(
                              startHour,
                              HOUR_START,
                            );
                            const clampedEnd = Math.min(endHour, HOUR_END);
                            if (
                              clampedEnd <= HOUR_START ||
                              clampedStart >= HOUR_END
                            )
                              return null;

                            const endPct =
                              ((clampedEnd - HOUR_START) / TOTAL_HOURS) * 100;
                            const heightPct =
                              ((clampedEnd - clampedStart) / TOTAL_HOURS) * 100;

                            const colorTokens = resolveProjectColorTokens(
                              entry.project_color,
                              resolvedTheme,
                              DEFAULT_ENTRY_COLOR,
                            )!;
                            const title =
                              entry.task_title || entry.note || "タスク";
                            const durSec = getEntryDurationSeconds(entry, now);
                            const durText = formatSeconds(durSec);
                            const hoverText = [
                              `プロジェクト: ${entry.project_name || "未設定"}`,
                              `タスク: ${title}`,
                              `時間: ${formatTimeWindow(entry)}`,
                              `経過: ${durText}`,
                              entry.original_started_at
                                ? "(編集済み — クリックで詳細)"
                                : "",
                            ]
                              .filter(Boolean)
                              .join("\n");
                            const isEdited = !!entry.original_started_at;
                            const isActive = !entry.ended_at;
                            const layout = entryLayouts.get(entry.id) ?? {
                              columnIndex: 0,
                              columnCount: 1,
                            };
                            const columnWidthPct = 100 / layout.columnCount;
                            const cursorCls = isActive
                              ? "cursor-pointer"
                              : isMovingThis
                                ? "cursor-grabbing"
                                : "cursor-grab";
                            return (
                              <div
                                key={entry.id}
                                data-entry
                                className={`absolute z-10 ${cursorCls} overflow-hidden rounded border text-foreground transition-all hover:brightness-[0.98] dark:hover:brightness-110 ${
                                  isEdited ? "ring-1 ring-yellow-300/70" : ""
                                } ${isMovingThis ? "opacity-80 ring-2 ring-primary/60" : ""}`}
                                style={{
                                  left: `calc(${layout.columnIndex * columnWidthPct}% + 2px)`,
                                  width: `calc(${columnWidthPct}% - 4px)`,
                                  top: `${endPct}%`,
                                  height: `${heightPct}%`,
                                  minHeight: isActive ? "2px" : "18px",
                                  transform: "translateY(-100%)",
                                  ...timelineBlockStyle(colorTokens),
                                }}
                                title={hoverText}
                                onMouseDown={(e) => {
                                  if (isActive) return;
                                  handleEntryMouseDown(e, entry, dayIndex);
                                }}
                                onClick={(e) => {
                                  if (isActive) {
                                    e.stopPropagation();
                                    openEditDialog(entry);
                                  }
                                }}
                                onContextMenu={(e) =>
                                  handleEntryContextMenu(e, entry)
                                }
                              >
                                {/* 上端リサイズハンドル */}
                                {!isActive && (
                                  <div
                                    data-resize-handle
                                    className="absolute left-0 right-0 top-0 h-1.5 cursor-ns-resize hover:bg-white/40 z-10"
                                    onMouseDown={(e) =>
                                      handleResizeMouseDown(
                                        e,
                                        entry,
                                        "top",
                                        dayIndex,
                                      )
                                    }
                                  />
                                )}

                                <div className="px-1 py-0.5 pointer-events-none">
                                  <div className="text-[10px] leading-tight font-medium truncate">
                                    {title}
                                  </div>
                                  {entry.project_name && heightPct > 4.5 && (
                                    <div
                                      className="text-[9px] leading-tight truncate"
                                      style={{ color: colorTokens.mutedText }}
                                    >
                                      {entry.project_name}
                                    </div>
                                  )}
                                  {heightPct > 3 && (
                                    <div
                                      className="text-[9px] leading-tight truncate"
                                      style={{ color: colorTokens.mutedText }}
                                    >
                                      {durText}
                                    </div>
                                  )}
                                </div>

                                {/* 下端リサイズハンドル */}
                                {!isActive && (
                                  <div
                                    data-resize-handle
                                    className="absolute left-0 right-0 bottom-0 h-1.5 cursor-ns-resize hover:bg-white/40 z-10"
                                    onMouseDown={(e) =>
                                      handleResizeMouseDown(
                                        e,
                                        entry,
                                        "bottom",
                                        dayIndex,
                                      )
                                    }
                                  />
                                )}
                              </div>
                            );
                          })}
                        </div>
                      );
                    })}
                  </div>
                </div>
              </CardContent>
            </Card>
          )}

          {/* プロジェクト別工数（スペース単位・全表示時） */}
          {scope !== "project" &&
            report.by_project &&
            report.by_project.length > 0 && (
              <Card size="sm">
                <CardHeader>
                  <CardTitle className="text-sm">プロジェクト別工数</CardTitle>
                </CardHeader>
                <CardContent className="space-y-3">
                  {report.by_project.map((b) => (
                    <BucketBar
                      key={b.key}
                      bucket={b}
                      maxSeconds={maxProjectSeconds}
                    />
                  ))}
                </CardContent>
              </Card>
            )}

          {/* タスク別 */}
          {report.by_task.length > 0 && (
            <Card size="sm">
              <CardHeader>
                <CardTitle className="text-sm">タスク別工数</CardTitle>
              </CardHeader>
              <CardContent className="space-y-3">
                {report.by_task.map((b) => (
                  <BucketBar
                    key={b.key}
                    bucket={b}
                    maxSeconds={maxTaskSeconds}
                    onClick={() => {
                      setSelectedEntry(null);
                      setSelectedTaskId(b.key);
                    }}
                  />
                ))}
              </CardContent>
            </Card>
          )}

          {/* 日別 */}
          {report.by_day.length > 0 && (
            <Card size="sm">
              <CardHeader>
                <CardTitle className="text-sm">日別工数</CardTitle>
              </CardHeader>
              <CardContent className="space-y-3">
                {report.by_day.map((b) => (
                  <BucketBar
                    key={b.key}
                    bucket={b}
                    maxSeconds={maxDaySeconds}
                  />
                ))}
              </CardContent>
            </Card>
          )}

          {/* ユーザー別 */}
          {report.by_user.length > 0 && (
            <Card size="sm">
              <CardHeader>
                <CardTitle className="text-sm">ユーザー別工数</CardTitle>
              </CardHeader>
              <CardContent className="space-y-3">
                {report.by_user.map((b) => (
                  <BucketBar
                    key={b.key}
                    bucket={b}
                    maxSeconds={maxUserSeconds}
                  />
                ))}
              </CardContent>
            </Card>
          )}
        </>
      )}

      <TaskDetailModal
        taskId={selectedTaskId}
        entryFocus={selectedEntry}
        open={!!selectedTaskId}
        onOpenChange={(open) => {
          if (!open) {
            setSelectedTaskId(null);
            setSelectedEntry(null);
          }
        }}
        onTaskUpdated={() => {
          fetchReport();
          window.dispatchEvent(new Event("task-list-refresh"));
        }}
      />

      {/* 実績編集ダイアログ (Toggl風) */}
      <Dialog
        open={!!editingEntry}
        onOpenChange={(open) => {
          if (!open) closeEditDialog();
        }}
      >
        <DialogContent
          className="sm:max-w-lg p-4 gap-3"
          showCloseButton={false}
        >
          {/* ヘッダー: アクション群 */}
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-0.5">
              {isEditingRunning ? (
                <Button
                  variant="ghost"
                  size="icon-sm"
                  onClick={handleEditStopTimer}
                  disabled={editSaving}
                  title="タイマー停止"
                  className="text-destructive hover:text-destructive"
                >
                  <StopCircle className="size-5" />
                </Button>
              ) : (
                <Button
                  variant="ghost"
                  size="icon-sm"
                  onClick={handleEditRestartTimer}
                  disabled={editSaving}
                  title="このタスクでタイマー再開"
                  className="text-primary hover:text-primary"
                >
                  <PlayCircle className="size-5" />
                </Button>
              )}
              <Button
                variant="ghost"
                size="icon-sm"
                onClick={handleEditDuplicate}
                disabled={editSaving || isEditingRunning}
                title="複製"
              >
                <Copy className="size-4" />
              </Button>
              <DropdownMenu>
                <DropdownMenuTrigger
                  disabled={editSaving}
                  className="inline-flex size-8 items-center justify-center rounded-md hover:bg-accent disabled:opacity-50"
                  title="その他"
                >
                  <MoreVertical className="size-4" />
                </DropdownMenuTrigger>
                <DropdownMenuContent align="start">
                  <DropdownMenuItem onClick={handleOpenTaskDetail}>
                    <ExternalLink className="mr-2 size-3.5" />
                    タスク詳細を開く
                  </DropdownMenuItem>
                  {editingEntry?.original_started_at &&
                    editingEntry?.original_ended_at && (
                      <DropdownMenuItem onClick={handleEditRevertToOriginal}>
                        <Undo2 className="mr-2 size-3.5" />
                        タイマー記録値に戻す
                      </DropdownMenuItem>
                    )}
                  <DropdownMenuSeparator />
                  <DropdownMenuItem
                    onClick={handleEditDelete}
                    className="text-destructive focus:text-destructive"
                  >
                    <Trash2 className="mr-2 size-3.5" />
                    削除
                  </DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
            </div>
            <Button
              variant="ghost"
              size="icon-sm"
              onClick={closeEditDialog}
              disabled={editSaving}
            >
              <XIcon className="size-4" />
            </Button>
          </div>

          {/* タイトル */}
          <div>
            <button
              type="button"
              onClick={handleOpenTaskDetail}
              className="text-left text-base font-medium leading-tight hover:underline"
            >
              {editingEntry?.task_title || "タスク"}
            </button>
            <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
              {spaces.length > 0 &&
                editingEntry?.project_id &&
                allProjects.length > 1 && (
                  <Select
                    value={
                      currentEditingSpace?.id ?? editingEntry.space_id ?? ""
                    }
                    onValueChange={(value) => {
                      if (value) void handleEditMoveTaskSpace(value);
                    }}
                    disabled={editSaving}
                  >
                    <SelectTrigger className="h-7 w-auto border-none px-0 text-xs text-muted-foreground shadow-none hover:text-foreground">
                      <span className="inline-flex items-center gap-1">
                        <FolderKanban className="size-3" />
                        {currentEditingSpace?.name ||
                          editingEntry.space_name ||
                          "スペース未設定"}
                      </span>
                    </SelectTrigger>
                    <SelectContent>
                      {spaces
                        .filter((space) =>
                          allProjects.some(
                            (project) => project.space_id === space.id,
                          ),
                        )
                        .map((space) => (
                          <SelectItem key={space.id} value={space.id}>
                            {space.name}
                          </SelectItem>
                        ))}
                    </SelectContent>
                  </Select>
                )}
              {editingEntry?.project_id && allProjects.length > 1 ? (
                <Select
                  value={editingEntry.project_id}
                  onValueChange={(value) => {
                    if (value) void handleEditMoveTaskProject(value);
                  }}
                  disabled={editSaving}
                >
                  <SelectTrigger className="h-7 w-auto border-none px-0 text-xs text-muted-foreground shadow-none hover:text-foreground">
                    <span className="inline-flex items-center gap-1">
                      <Folder className="size-3" />
                      {currentEditingProject?.name ||
                        editingEntry.project_name ||
                        "プロジェクト未設定"}
                    </span>
                  </SelectTrigger>
                  <SelectContent>
                    {projectsForEditingSpace.length > 0 ? (
                      <SelectGroup>
                        <SelectLabel>
                          {currentEditingSpace?.name ||
                            editingEntry.space_name ||
                            "スペースなし"}
                        </SelectLabel>
                        {projectsForEditingSpace.map((project) => (
                          <SelectItem key={project.id} value={project.id}>
                            {project.name}
                          </SelectItem>
                        ))}
                      </SelectGroup>
                    ) : (
                      <SelectGroup>
                        <SelectLabel>{"プロジェクト"}</SelectLabel>
                        {allProjects.map((project) => (
                          <SelectItem key={project.id} value={project.id}>
                            {project.name}
                          </SelectItem>
                        ))}
                      </SelectGroup>
                    )}
                  </SelectContent>
                </Select>
              ) : (
                editingEntry?.project_name && (
                  <span className="inline-flex items-center gap-1">
                    <Folder className="size-3" />
                    {editingEntry.project_name}
                  </span>
                )
              )}
              {isEditingRunning && (
                <span className="inline-flex items-center gap-1 text-primary">
                  <Clock className="size-3" />
                  {"計測中"}
                </span>
              )}
            </div>
          </div>

          {/* Time row */}
          <div className="flex flex-wrap items-center gap-2">
            <Input
              type="text"
              inputMode="numeric"
              value={editStart}
              onChange={(e) => setEditStart(e.target.value)}
              onBlur={handleEditStartBlur}
              onKeyDown={handleEditInputEnter}
              placeholder="10:00"
              className="w-20 text-center font-mono tabular-nums"
              aria-label="開始時刻"
            />
            <span className="text-muted-foreground">→</span>
            <Input
              type="text"
              inputMode="numeric"
              value={isEditingRunning ? "計測中" : editEnd}
              onChange={(e) => setEditEnd(e.target.value)}
              onBlur={handleEditEndBlur}
              onKeyDown={handleEditInputEnter}
              placeholder="11:00"
              className="w-20 text-center font-mono tabular-nums"
              aria-label="終了時刻"
              disabled={isEditingRunning}
            />
            <Input
              type="date"
              value={editDate}
              onChange={(e) => setEditDate(e.target.value)}
              onKeyDown={handleEditInputEnter}
              className="w-40"
              aria-label="日付"
            />
            <Input
              type="text"
              inputMode="numeric"
              value={editDuration}
              onChange={(e) => setEditDuration(e.target.value)}
              onBlur={handleEditDurationBlur}
              onKeyDown={handleEditInputEnter}
              placeholder="0:00:00"
              className="w-24 text-center font-mono tabular-nums ml-auto"
              aria-label="経過時間"
            />
          </div>

          {/* メモ + 保存 */}
          <div className="flex items-center gap-2">
            <Input
              value={editNote}
              onChange={(e) => setEditNote(e.target.value)}
              onKeyDown={handleEditInputEnter}
              placeholder="メモ (任意)"
              className="flex-1"
            />
            <Button size="sm" onClick={handleEditSave} disabled={editSaving}>
              {editSaving ? "保存中..." : "Save"}
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      {/* 右クリックメニュー */}
      {ctxMenu &&
        typeof document !== "undefined" &&
        createPortal(
          <div
            ref={ctxMenuRef}
            data-ctx-menu
            role="menu"
            className="fixed z-[100] min-w-[180px] rounded-md bg-popover p-1 text-sm text-popover-foreground shadow-md ring-1 ring-foreground/10"
            style={ctxMenuStyle}
            onContextMenu={(e) => e.preventDefault()}
          >
            <button
              type="button"
              className="flex w-full items-center gap-2 rounded px-2 py-1 text-left hover:bg-accent hover:text-accent-foreground"
              onClick={handleCtxEdit}
              disabled={!ctxMenu.entry.ended_at}
            >
              <Clock className="size-3.5" />
              編集
            </button>
            <button
              type="button"
              className="flex w-full items-center gap-2 rounded px-2 py-1 text-left hover:bg-accent hover:text-accent-foreground"
              onClick={handleCtxOpenDetail}
            >
              <ExternalLink className="size-3.5" />
              タスク詳細を開く
            </button>
            <button
              type="button"
              className="flex w-full items-center gap-2 rounded px-2 py-1 text-left hover:bg-accent hover:text-accent-foreground disabled:opacity-50 disabled:pointer-events-none"
              onClick={handleCtxDuplicate}
              disabled={!ctxMenu.entry.ended_at}
            >
              <Copy className="size-3.5" />
              複製
            </button>
            <div className="-mx-1 my-1 h-px bg-border" />
            <button
              type="button"
              className="flex w-full items-center gap-2 rounded px-2 py-1 text-left text-destructive hover:bg-destructive/10"
              onClick={handleCtxDelete}
            >
              <Trash2 className="size-3.5" />
              削除
            </button>
          </div>,
          document.body,
        )}
    </div>
  );
}
