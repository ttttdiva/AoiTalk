"use client";

import { useState, useEffect, useCallback, useMemo, useRef } from "react";
import useSWR from "swr";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  BarChart3,
  Clock,
  FolderKanban,
  ListChecks,
  Activity,
  ChevronLeft,
  ChevronRight,
  CircleDot,
  TimerReset,
} from "lucide-react";
import {
  taskApi,
  type Task,
  type TimeReport,
  type TimeEntry,
} from "@/lib/task-api";
import {
  getRemoteTimeReport,
  listRemoteTimeEntries,
} from "@/lib/remote-servers";
import { listRemoteTasks, toRemoteTask } from "@/lib/remote-tasks";
import {
  decorateRemoteTimeEntry,
  decorateRemoteTimeReport,
} from "@/lib/remote-resource";
import { useTaskCompletionRefresh } from "@/hooks/use-task-completion-refresh";
import { getUserSettings, patchUserSettings } from "@/lib/user-settings";
import { useProject } from "@/contexts/project-context";
import { useTheme } from "@/contexts/theme-context";
import { useWorkspaceShellRegistration } from "@/components/layout/shell-context";
import { TaskDetailModal } from "@/components/tasks/task-detail-modal";
import {
  formatLocalDateTime,
  formatLocalDateTimeWithMilliseconds,
} from "@/lib/date-time";
import { BucketBar } from "./reports-bucket-bar";
import { ReportsEditDialog } from "./reports-edit-dialog";
import { ReportsContextMenu } from "./reports-context-menu";
import { ReportsTimeline } from "./reports-timeline";
import { useReportsTimeline } from "./use-reports-timeline";
import { useReportsEditEntry } from "./use-reports-edit-entry";
import {
  formatSeconds,
  getWeekRangeFromDate,
  getMonthRange,
  getWeekDays,
  groupEntriesByDay,
  buildEntryColumnLayouts,
  isTaskScheduledInRange,
  type PeriodPreset,
  type ReportsViewMode,
  type ScopeMode,
  type ReportsViewSettings,
  type EntryColumnLayout,
} from "./reports-utils";
import { ReportsWorkspaceNavigation } from "./reports-workspace-navigation";

// SWR キャッシュキー。レポートページで一意なので固定文字列を使う（安定キー）。
// 取得タイミングは従来どおり呼び出し側の fetchReport で駆動する（呼び出し側駆動）。
const REPORTS_SWR_KEY = "reports-page/report";

const EMPTY_ENTRIES: TimeEntry[] = [];
const EMPTY_SCHEDULED_TASKS: Task[] = [];

type ReportData = {
  report: TimeReport | null;
  timeEntries: TimeEntry[];
  scheduledTasks: Task[];
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
  const [activeView, setActiveView] = useState<ReportsViewMode>("summary");
  const remoteContext =
    scope === "project" && selectedProject?.source === "remote"
      ? selectedProject
      : scope === "space" && selectedSpace?.source === "remote"
        ? selectedSpace
        : scope === "all" && selectedProject?.source === "remote"
          ? selectedProject
          : null;
  const remoteReadOnly = Boolean(remoteContext);
  const projectsById = useMemo(
    () => new Map(allProjects.map((project) => [project.id, project] as const)),
    [allProjects],
  );
  const isProjectReadOnly = useCallback(
    (projectId: string | null | undefined) => {
      if (!projectId) return false;
      const project = projectsById.get(projectId);
      return project?.source === "remote" || project?.can_write === false;
    },
    [projectsById],
  );
  const isTimeEntryReadOnly = useCallback(
    (entry: TimeEntry) =>
      entry.source === "remote" || isProjectReadOnly(entry.project_id),
    [isProjectReadOnly],
  );
  const isReportBucketReadOnly = useCallback(
    (bucket: TimeReport["by_task"][number]) =>
      bucket.source === "remote" || isProjectReadOnly(bucket.project_id),
    [isProjectReadOnly],
  );
  const reportsReadOnly =
    remoteReadOnly ||
    selectedProject?.can_write === false;
  // レポート集計（report / timeEntries / scheduledTasks）のサーバー状態を SWR で保持する。
  // 取得は複雑にパラメータ化され早期 return・リモート分岐・相関する 3 出力を伴うため、
  // 自動 revalidation は全て無効化し、fetchReport が計算結果を mutate で書き込む
  // （書き込み経由の楽観的更新）。これにより従来の setState と表示挙動を完全一致させる。
  const { data: reportData, mutate: mutateReport } = useSWR<ReportData>(
    REPORTS_SWR_KEY,
    null,
    {
      revalidateOnFocus: false,
      revalidateOnReconnect: false,
      revalidateIfStale: false,
      keepPreviousData: true,
    },
  );
  const report = reportData?.report ?? null;
  const timeEntries = reportData?.timeEntries ?? EMPTY_ENTRIES;
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [selectedEntry, setSelectedEntry] = useState<TimeEntry | null>(null);
  const hasReadOnlyData = useMemo(
    () =>
      timeEntries.some(isTimeEntryReadOnly) ||
      Boolean(report?.by_project.some(isReportBucketReadOnly)),
    [isReportBucketReadOnly, isTimeEntryReadOnly, report, timeEntries],
  );
  const selectedTaskBucket = useMemo(
    () => report?.by_task.find((bucket) => bucket.key === selectedTaskId) ?? null,
    [report?.by_task, selectedTaskId],
  );
  const selectedTaskReadOnly = Boolean(
    selectedEntry
      ? isTimeEntryReadOnly(selectedEntry)
      : selectedTaskBucket && isReportBucketReadOnly(selectedTaskBucket),
  );
  const [loading, setLoading] = useState(false);
  const [reportError, setReportError] = useState<string | null>(null);
  const [period, setPeriod] = useState<PeriodPreset>("this_week");
  const [customFrom, setCustomFrom] = useState("");
  const [customTo, setCustomTo] = useState("");
  const [weekOffset, setWeekOffset] = useState(0);
  const [showScheduleFrames, setShowScheduleFrames] = useState(false);
  const [prefsLoaded, setPrefsLoaded] = useState(false);
  const scheduledTasks = reportData?.scheduledTasks ?? EMPTY_SCHEDULED_TASKS;
  const reportRequestGenerationRef = useRef(0);
  const activeReportRequestRef = useRef<{ generation: number; queryKey: string } | null>(null);

  const [now, setNow] = useState(() => new Date());

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
  const reportQueryKey = useMemo(
    () =>
      JSON.stringify({
        scope,
        selectedProjectId,
        selectedSpaceId,
        period,
        customFrom,
        customTo,
        showScheduleFrames,
        weekFrom: weekRange.monday.toISOString(),
        weekTo: weekRange.sunday.toISOString(),
        remoteProfileId: remoteContext?.remote_server_id ?? null,
        remoteResourceId: remoteContext?.resource_id ?? null,
        remoteSource: remoteContext?.source ?? null,
      }),
    [
      customFrom,
      customTo,
      period,
      remoteContext?.remote_server_id,
      remoteContext?.resource_id,
      remoteContext?.source,
      scope,
      selectedProjectId,
      selectedSpaceId,
      showScheduleFrames,
      weekRange.monday,
      weekRange.sunday,
    ],
  );
  const currentReportQueryKeyRef = useRef(reportQueryKey);
  currentReportQueryKeyRef.current = reportQueryKey;

  useEffect(() => {
    let active = true;

    void getUserSettings()
      .then((settings) => {
        if (!active) return;
        const view = settings.reports_view as ReportsViewSettings | undefined;
        if (!view) return;

        if (view.active_view === "summary" || view.active_view === "timeline") {
          setActiveView(view.active_view);
        }

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
        active_view: activeView,
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
    activeView,
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

  const fetchReport = useCallback(async () => {
    if (reportQueryKey !== currentReportQueryKeyRef.current) return;
    const generation = ++reportRequestGenerationRef.current;
    activeReportRequestRef.current = { generation, queryKey: reportQueryKey };
    const isCurrentRequest = () => {
      const active = activeReportRequestRef.current;
      return (
        active?.generation === generation &&
        active.queryKey === reportQueryKey &&
        currentReportQueryKeyRef.current === reportQueryKey
      );
    };
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
    if (!scopeArg) {
      if (isCurrentRequest()) {
        setLoading(false);
        setReportError(null);
      }
      return;
    }

    setLoading(true);
    setReportError(null);

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
      const remoteProfileId = remoteContext?.remote_server_id;
      const remoteScope =
        remoteContext?.source === "remote"
          ? remoteContext === selectedProject || scope === "project"
            ? { project_id: remoteContext.resource_id }
            : { space_id: remoteContext.resource_id }
          : null;
      const scheduledTasksPromise = shouldLoadScheduledTasks
        ? remoteProfileId && remoteScope
          ? listRemoteTasks(remoteProfileId, remoteScope).then((items) =>
              items.map((task) =>
                toRemoteTask(
                  {
                    id: remoteProfileId,
                    name: remoteContext?.remote_server_name ?? "Remote",
                    display_color: remoteContext?.remote_server_color,
                    base_url: remoteContext?.remote_server_base_url,
                  },
                  task,
                ),
              ),
            )
          : taskApi.listTasks(selectedProjectId!).catch((err) => {
              console.error("予定枠用タスク取得に失敗しました", err);
              return [] as Task[];
            })
        : Promise.resolve([] as Task[]);
      const [r, entries, tasks] =
        remoteProfileId && remoteScope
          ? await Promise.all([
              getRemoteTimeReport(remoteProfileId, {
                ...remoteScope,
                date_from: dateFrom,
                date_to: dateTo,
              }),
              listRemoteTimeEntries(remoteProfileId, {
                ...remoteScope,
                date_from: dateFrom,
                date_to: dateTo,
              }),
              scheduledTasksPromise,
            ])
          : await Promise.all([
              taskApi.getTimeReport(scopeArg, dateFrom, dateTo),
              taskApi.listTimeEntries(scopeArg, dateFrom, dateTo),
              scheduledTasksPromise,
            ]);

      const nextReport =
        remoteProfileId && remoteContext?.source === "remote"
          ? decorateRemoteTimeReport(remoteProfileId, r)
          : r;
      const nextEntries =
        remoteProfileId && remoteContext?.source === "remote"
          ? entries.map((entry) =>
              decorateRemoteTimeEntry(
                remoteProfileId,
                remoteContext.remote_server_name ?? "Remote",
                remoteContext.remote_server_color,
                remoteContext.remote_server_base_url,
                entry,
              ),
            )
          : entries;
      if (!isCurrentRequest()) return;
      // 3 出力を一度に SWR キャッシュへ書き込む（再取得はしない）。
      void mutateReport(
        {
          report: nextReport,
          timeEntries: nextEntries,
          scheduledTasks: shouldLoadScheduledTasks
            ? tasks.filter((task) =>
                isTaskScheduledInRange(
                  task,
                  weekRange.monday,
                  weekRange.sunday,
                ),
              )
            : [],
        },
        { revalidate: false },
      );
    } catch (err) {
      if (!isCurrentRequest()) return;
      console.error("レポート取得失敗:", err);
      setReportError("レポートを取得できませんでした。再試行してください。");
      void mutateReport(
        {
          report: null,
          timeEntries: EMPTY_ENTRIES,
          scheduledTasks: EMPTY_SCHEDULED_TASKS,
        },
        { revalidate: false },
      );
    } finally {
      if (isCurrentRequest()) setLoading(false);
    }
  }, [
    scope,
    selectedProjectId,
    selectedProject,
    selectedSpaceId,
    period,
    customFrom,
    customTo,
    showScheduleFrames,
    weekRange,
    remoteContext,
    reportQueryKey,
    mutateReport,
  ]);

  useEffect(() => () => {
    reportRequestGenerationRef.current += 1;
    activeReportRequestRef.current = null;
  }, []);

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

  const {
    editingEntry,
    editDate,
    editStart,
    editEnd,
    editDuration,
    editNote,
    editSaving,
    setEditDate,
    setEditStart,
    setEditEnd,
    setEditDuration,
    setEditNote,
    currentEditingProject,
    currentEditingSpace,
    projectsForEditingSpace,
    isEditingRunning,
    openEditDialog,
    closeEditDialog,
    handleEditStartBlur,
    handleEditEndBlur,
    handleEditDurationBlur,
    handleEditSave,
    handleEditInputEnter,
    handleEditDelete,
    handleEditDuplicate,
    handleEditRestartTimer,
    handleEditStopTimer,
    handleEditRevertToOriginal,
    handleOpenTaskDetail,
    handleEditMoveTaskProject,
    handleEditMoveTaskSpace,
  } = useReportsEditEntry({
    remoteReadOnly,
    isEntryReadOnly: isTimeEntryReadOnly,
    isProjectReadOnly,
    allProjects,
    spaces,
    fetchReport,
    setSelectedEntry,
    setSelectedTaskId,
  });

  const {
    dragState,
    dragForm,
    dragTaskName,
    setDragTaskName,
    dragSelectedTaskId,
    setDragSelectedTaskId,
    dragTaskLoading,
    dragCreating,
    dragFormInputRef,
    isDraggingRef,
    resizeState,
    isResizingRef,
    dayColRefs,
    moveState,
    isMovingRef,
    ctxMenu,
    ctxMenuRef,
    ctxMenuStyle,
    matchingDragTasks,
    selectedDragTask,
    handleDragMouseDown,
    handleDragMouseMove,
    handleDragMouseUp,
    handleDragFormSubmit,
    handleDragFormCancel,
    handleResizeMouseDown,
    handleEntryMouseDown,
    handleEntryContextMenu,
    handleCtxOpenDetail,
    handleCtxEdit,
    handleCtxDuplicate,
    handleCtxDelete,
  } = useReportsTimeline({
    remoteReadOnly,
    createReadOnly: reportsReadOnly,
    isEntryReadOnly: isTimeEntryReadOnly,
    selectedProjectId,
    weekDays,
    timeEntries,
    fetchReport,
    openEditDialog,
    setSelectedEntry,
    setSelectedTaskId,
    editingEntry,
    period,
    setWeekOffset,
  });

  const hasScope =
    scope === "all"
      ? true
      : scope === "space"
        ? !!selectedSpaceId
        : !!selectedProjectId;

  const reportsScopeLabel =
    scope === "all"
      ? "全プロジェクト横断"
      : scope === "space"
        ? selectedSpace
          ? `スペース: ${selectedSpace.name}`
          : "スペース未選択"
        : selectedProject
          ? `プロジェクト: ${selectedProject.name}`
          : "プロジェクト未選択";

  useWorkspaceShellRegistration({
    id: "reports-workspace",
    workspaceNavigation: (
      <ReportsWorkspaceNavigation
        scope={scope}
        activeView={activeView}
        period={period}
        customFrom={customFrom}
        customTo={customTo}
        scopeLabel={reportsScopeLabel}
        readOnly={reportsReadOnly || hasReadOnlyData}
        weekOffset={weekOffset}
        showScheduleFrames={showScheduleFrames}
        canShowScheduleFrames={canShowScheduleFrames}
        onScopeChange={setScope}
        onActiveViewChange={setActiveView}
        onPeriodChange={setPeriod}
        onCustomFromChange={setCustomFrom}
        onCustomToChange={setCustomTo}
        onWeekOffsetChange={setWeekOffset}
        onShowScheduleFramesChange={setShowScheduleFrames}
      />
    ),
  });

  return (
    <div
      className="flex min-h-full flex-col gap-4 bg-background p-4 pb-16 md:p-6"
      data-shell-workspace="reports"
      data-shell-region="reports-canvas"
    >
      <header className="hidden shrink-0 items-center justify-between gap-4 border-b border-border/80 pb-4 md:flex">
        <div className="flex min-w-0 items-center gap-3">
          <div className="grid size-9 shrink-0 place-items-center rounded-md bg-primary/10 text-primary">
            <BarChart3 className="size-5" aria-hidden="true" />
          </div>
          <div className="min-w-0">
            <p className="text-[10px] font-semibold uppercase tracking-[0.16em] text-muted-foreground">
              Reports
            </p>
            <h1 className="truncate text-xl font-semibold tracking-tight">
          {activeView === "timeline" ? "週間タイムライン" : "サマリー"}
            </h1>
          </div>
        </div>
        <div className="flex flex-wrap items-center justify-end gap-2">
          {(reportsReadOnly || hasReadOnlyData) && (
            <span className="inline-flex items-center rounded-full border border-primary/35 bg-primary/10 px-2.5 py-1 text-xs text-primary">
              {remoteReadOnly ? "リモート・読み取り専用" : "一部読み取り専用"}
            </span>
          )}
          <div className="inline-flex items-center gap-1 rounded-md border border-border bg-card p-1 text-xs">
            {(["this_week", "this_month", "custom"] as const).map((value) => (
              <button
                key={value}
                type="button"
                onClick={() => setPeriod(value)}
                aria-label={
                  value === "this_week"
                    ? "今週"
                    : value === "this_month"
                      ? "今月"
                      : "カスタム"
                }
                className={`rounded px-2.5 py-1.5 transition-colors ${
                  period === value
                    ? "bg-primary text-primary-foreground"
                    : "text-muted-foreground hover:bg-muted hover:text-foreground"
                }`}
              >
                {value === "this_week" ? "W" : value === "this_month" ? "M" : "…"}
              </button>
            ))}
          </div>
          {period === "custom" && (
            <div className="flex items-center gap-1.5">
              <Input
                type="date"
                value={customFrom}
                onChange={(e) => setCustomFrom(e.target.value)}
                className="h-8 w-32 bg-card text-xs"
                aria-label="レポート開始日"
              />
              <span className="text-xs text-muted-foreground">–</span>
              <Input
                type="date"
                value={customTo}
                onChange={(e) => setCustomTo(e.target.value)}
                className="h-8 w-32 bg-card text-xs"
                aria-label="レポート終了日"
              />
            </div>
          )}
          {activeView === "timeline" && period === "this_week" && (
            <div className="inline-flex items-center gap-1 rounded-md border border-border bg-card p-1 text-xs">
              <Button
                variant="ghost"
                size="icon"
                className="size-7"
                onClick={() => setWeekOffset((offset) => offset - 1)}
                aria-label="前の週"
              >
                <ChevronLeft className="size-3.5" />
              </Button>
              <span className="min-w-[96px] text-center text-[11px] text-muted-foreground">
                {weekLabel}
              </span>
              <Button
                variant="ghost"
                size="icon"
                className="size-7"
                onClick={() => setWeekOffset((offset) => Math.min(offset + 1, 0))}
                disabled={weekOffset >= 0}
                aria-label="次の週"
              >
                <ChevronRight className="size-3.5" />
              </Button>
            </div>
          )}
        </div>
      </header>
      {/* ヘッダー */}
      <div className="flex flex-wrap items-center gap-3 md:hidden">
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
      ) : reportError ? (
        <div
          role="alert"
          className="flex flex-col items-center justify-center gap-3 rounded-lg border border-destructive/35 bg-destructive/5 px-6 py-16 text-center"
        >
          <p className="text-sm text-foreground">{reportError}</p>
          <Button type="button" variant="outline" size="sm" onClick={() => void fetchReport()}>
            再試行
          </Button>
        </div>
      ) : !report ? (
        <div className="flex items-center justify-center py-16 text-muted-foreground">
          データがありません
        </div>
      ) : (
        <>
          {/* サマリーカード */}
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            <Card size="sm" className="border-border/80 bg-card/80">
              <CardHeader className="flex-row items-center justify-between space-y-0 pb-2">
                <CardTitle className="text-[11px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">
                  合計作業時間
                </CardTitle>
                <Clock className="size-4 text-primary" aria-hidden="true" />
              </CardHeader>
              <CardContent>
                <p className="text-3xl font-semibold tracking-tight">
                  {formatSeconds(report.summary.total_seconds)}
                </p>
                <p className="mt-1 text-xs text-muted-foreground">
                  {period === "this_month" ? "今月" : period === "custom" ? "カスタム期間" : "今週"}の集計
                </p>
              </CardContent>
            </Card>
            <Card size="sm" className="border-border/80 bg-card/80">
              <CardHeader className="flex-row items-center justify-between space-y-0 pb-2">
                <CardTitle className="text-[11px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">
                  エントリ数
                </CardTitle>
                <ListChecks className="size-4 text-primary" aria-hidden="true" />
              </CardHeader>
              <CardContent>
                <p className="text-3xl font-semibold tracking-tight">
                  {report.summary.entry_count}
                </p>
                <p className="mt-1 text-xs text-muted-foreground">記録済みのタイムエントリ</p>
              </CardContent>
            </Card>
            <Card size="sm" className="border-border/80 bg-card/80">
              <CardHeader className="flex-row items-center justify-between space-y-0 pb-2">
                <CardTitle className="text-[11px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">
                  計測中
                </CardTitle>
                <Activity className="size-4 text-primary" aria-hidden="true" />
              </CardHeader>
              <CardContent>
                <p className="text-3xl font-semibold tracking-tight">
                  {report.summary.active_entries}
                </p>
                <p className="mt-1 text-xs text-muted-foreground">現在進行中のエントリ</p>
              </CardContent>
            </Card>
          </div>

          {/* 週間タイムライン */}
          {activeView === "timeline" && period === "this_week" && (
            <ReportsTimeline
              weekDays={weekDays}
              entriesByDay={entriesByDay}
              now={now}
              entryLayoutsByDay={entryLayoutsByDay}
              moveState={moveState}
              timeEntries={timeEntries}
              dragState={dragState}
              dragForm={dragForm}
              visibleScheduledTasks={visibleScheduledTasks}
              readOnly={remoteReadOnly}
              createReadOnly={reportsReadOnly}
              isEntryReadOnly={isTimeEntryReadOnly}
              dayColRefs={dayColRefs}
              handleDragMouseDown={handleDragMouseDown}
              handleDragMouseMove={handleDragMouseMove}
              handleDragMouseUp={handleDragMouseUp}
              isDraggingRef={isDraggingRef}
              isResizingRef={isResizingRef}
              isMovingRef={isMovingRef}
              dragFormInputRef={dragFormInputRef}
              dragTaskName={dragTaskName}
              setDragTaskName={setDragTaskName}
              dragSelectedTaskId={dragSelectedTaskId}
              setDragSelectedTaskId={setDragSelectedTaskId}
              dragCreating={dragCreating}
              handleDragFormSubmit={handleDragFormSubmit}
              handleDragFormCancel={handleDragFormCancel}
              selectedDragTask={selectedDragTask}
              dragTaskLoading={dragTaskLoading}
              matchingDragTasks={matchingDragTasks}
              resizeState={resizeState}
              resolvedTheme={resolvedTheme}
              handleEntryMouseDown={handleEntryMouseDown}
              openEditDialog={openEditDialog}
              handleEntryContextMenu={handleEntryContextMenu}
              handleResizeMouseDown={handleResizeMouseDown}
            />
          )}

          {activeView === "timeline" && period !== "this_week" && (
            <div className="rounded-lg border border-border bg-card/70 px-4 py-8 text-center text-sm text-muted-foreground">
              タイムラインは「今週」期間で利用できます。期間を切り替えてください。
            </div>
          )}

          {activeView === "summary" && (
            <div className="grid gap-4 xl:grid-cols-[minmax(0,0.92fr)_minmax(0,1.55fr)]">
              <Card size="sm" className="border-border/80 bg-card/80">
                <CardHeader className="flex-row items-center justify-between space-y-0">
                  <CardTitle className="flex items-center gap-2 text-base">
                    <FolderKanban className="size-4 text-primary" aria-hidden="true" />
                    プロジェクト別
                  </CardTitle>
                  <span className="text-[11px] text-muted-foreground">{report.by_project?.length ?? 0}件</span>
                </CardHeader>
                <CardContent>
                  {report.by_project && report.by_project.length > 0 ? (
                    <div className="space-y-3">
                      {report.by_project.slice(0, 6).map((bucket) => (
                        <BucketBar
                          key={bucket.key}
                          bucket={bucket}
                          maxSeconds={maxProjectSeconds}
                        />
                      ))}
                    </div>
                  ) : (
                    <p className="py-8 text-center text-sm text-muted-foreground">プロジェクト別の記録はありません。</p>
                  )}
                </CardContent>
              </Card>

              <Card size="sm" className="border-border/80 bg-card/80">
                <CardHeader className="flex-row items-center justify-between space-y-0">
                  <CardTitle className="flex items-center gap-2 text-base">
                    <BarChart3 className="size-4 text-primary" aria-hidden="true" />
                    日別の作業時間
                  </CardTitle>
                  <span className="text-[11px] text-muted-foreground">実績</span>
                </CardHeader>
                <CardContent>
                  {report.by_day.length > 0 ? (
                    <div className="flex h-52 items-end gap-2 border-b border-border/80 px-1 pt-4">
                      {report.by_day.map((bucket) => {
                        const ratio = maxDaySeconds > 0 ? bucket.seconds / maxDaySeconds : 0;
                        return (
                          <div key={bucket.key} className="group flex min-w-0 flex-1 flex-col items-center justify-end gap-2">
                            <span className="max-w-full truncate text-[10px] text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100">
                              {formatSeconds(bucket.seconds)}
                            </span>
                            <div
                              className="w-full rounded-t bg-primary/80 transition-all group-hover:bg-primary"
                              style={{ height: `${Math.max(8, Math.round(ratio * 136))}px` }}
                              title={`${bucket.label}: ${formatSeconds(bucket.seconds)}`}
                            />
                            <span className="max-w-full truncate text-[10px] text-muted-foreground">
                              {bucket.label}
                            </span>
                          </div>
                        );
                      })}
                    </div>
                  ) : (
                    <p className="py-8 text-center text-sm text-muted-foreground">日別の記録はありません。</p>
                  )}
                </CardContent>
              </Card>

              <Card size="sm" className="border-border/80 bg-card/80 xl:col-span-2">
                <CardHeader className="flex-row items-center justify-between space-y-0">
                  <CardTitle className="flex items-center gap-2 text-base">
                    <TimerReset className="size-4 text-primary" aria-hidden="true" />
                    アクティブタイマー
                  </CardTitle>
                  <span className="inline-flex items-center gap-1.5 rounded-full bg-primary/10 px-2 py-1 text-[11px] text-primary">
                    <CircleDot className="size-3" aria-hidden="true" />
                    {report.summary.active_entries} 実行中
                  </span>
                </CardHeader>
                <CardContent className="divide-y divide-border/80 p-0">
                  {timeEntries.filter((entry) => !entry.ended_at).length > 0 ? (
                    timeEntries
                      .filter((entry) => !entry.ended_at)
                      .map((entry) => {
                        const entryReadOnly = isTimeEntryReadOnly(entry);
                        return (
                          <button
                            key={entry.id}
                            type="button"
                            className={`flex w-full items-center justify-between gap-3 px-5 py-4 text-left transition-colors ${
                              entryReadOnly
                                ? "cursor-default opacity-80"
                                : "hover:bg-muted/30"
                            }`}
                            disabled={entryReadOnly}
                            onClick={
                              entryReadOnly
                                ? undefined
                                : () => openEditDialog(entry)
                            }
                          >
                            <span className="min-w-0">
                              <span className="block truncate text-sm font-medium">
                                {entry.task_title || entry.note || "タスク"}
                              </span>
                              <span className="mt-1 block truncate text-xs text-muted-foreground">
                                {entry.project_name || "プロジェクト未設定"}
                              </span>
                            </span>
                            <span className="shrink-0 font-mono text-sm text-primary">
                              計測中
                            </span>
                          </button>
                        );
                      })
                  ) : (
                    <p className="px-5 py-8 text-center text-sm text-muted-foreground">現在計測中のエントリはありません。</p>
                  )}
                </CardContent>
              </Card>
            </div>
          )}

          {activeView === "summary" && (
            <>
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
                    onClick={
                      remoteReadOnly || b.source === "remote"
                        ? undefined
                        : () => {
                            setSelectedEntry(null);
                            setSelectedTaskId(b.key);
                          }
                    }
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
        </>
      )}

      {!remoteReadOnly && (
        <TaskDetailModal
          taskId={selectedTaskId}
          entryFocus={selectedEntry}
          readOnly={selectedTaskReadOnly}
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
      )}

      {/* 実績編集ダイアログ (Toggl風) */}
      {!remoteReadOnly &&
        (!editingEntry || !isTimeEntryReadOnly(editingEntry)) && (
          <ReportsEditDialog
        editingEntry={editingEntry}
        closeEditDialog={closeEditDialog}
        isEditingRunning={isEditingRunning}
        editSaving={editSaving}
        spaces={spaces}
        allProjects={allProjects}
        currentEditingSpace={currentEditingSpace}
        currentEditingProject={currentEditingProject}
        projectsForEditingSpace={projectsForEditingSpace}
        isProjectReadOnly={isProjectReadOnly}
        editStart={editStart}
        editEnd={editEnd}
        editDate={editDate}
        editDuration={editDuration}
        editNote={editNote}
        setEditStart={setEditStart}
        setEditEnd={setEditEnd}
        setEditDate={setEditDate}
        setEditDuration={setEditDuration}
        setEditNote={setEditNote}
        handleEditStartBlur={handleEditStartBlur}
        handleEditEndBlur={handleEditEndBlur}
        handleEditDurationBlur={handleEditDurationBlur}
        handleEditInputEnter={handleEditInputEnter}
        handleEditSave={handleEditSave}
        handleEditStopTimer={handleEditStopTimer}
        handleEditRestartTimer={handleEditRestartTimer}
        handleEditDuplicate={handleEditDuplicate}
        handleOpenTaskDetail={handleOpenTaskDetail}
        handleEditRevertToOriginal={handleEditRevertToOriginal}
        handleEditDelete={handleEditDelete}
        handleEditMoveTaskSpace={handleEditMoveTaskSpace}
        handleEditMoveTaskProject={handleEditMoveTaskProject}
          />
        )}

      {/* 右クリックメニュー */}
      {!remoteReadOnly &&
        (!ctxMenu || !isTimeEntryReadOnly(ctxMenu.entry)) && (
        <ReportsContextMenu
          ctxMenu={ctxMenu}
          ctxMenuRef={ctxMenuRef}
          ctxMenuStyle={ctxMenuStyle}
          handleCtxEdit={handleCtxEdit}
          handleCtxOpenDetail={handleCtxOpenDetail}
          handleCtxDuplicate={handleCtxDuplicate}
          handleCtxDelete={handleCtxDelete}
        />
        )}
    </div>
  );
}
