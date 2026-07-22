"use client";

import { useState, useEffect, useCallback, useMemo } from "react";
import useSWR from "swr";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Clock,
  ListChecks,
  Activity,
  ChevronLeft,
  ChevronRight,
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
  type ScopeMode,
  type ReportsViewSettings,
  type EntryColumnLayout,
} from "./reports-utils";

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
  const remoteContext =
    scope === "project" && selectedProject?.source === "remote"
      ? selectedProject
      : scope === "space" && selectedSpace?.source === "remote"
        ? selectedSpace
        : scope === "all" && selectedProject?.source === "remote"
          ? selectedProject
          : null;
  const remoteReadOnly = Boolean(remoteContext);
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
  const [loading, setLoading] = useState(false);
  const [period, setPeriod] = useState<PeriodPreset>("this_week");
  const [customFrom, setCustomFrom] = useState("");
  const [customTo, setCustomTo] = useState("");
  const [weekOffset, setWeekOffset] = useState(0);
  const [showScheduleFrames, setShowScheduleFrames] = useState(false);
  const [prefsLoaded, setPrefsLoaded] = useState(false);
  const scheduledTasks = reportData?.scheduledTasks ?? EMPTY_SCHEDULED_TASKS;

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
      const [r, entries, tasks] = remoteProfileId && remoteScope
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

      const nextReport = remoteProfileId && remoteContext?.source === "remote"
        ? decorateRemoteTimeReport(remoteProfileId, r)
        : r;
      const nextEntries = remoteProfileId && remoteContext?.source === "remote"
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
      console.error("レポート取得失敗:", err);
    } finally {
      setLoading(false);
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
    mutateReport,
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
                      if (remoteReadOnly) return;
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

      {!remoteReadOnly && <TaskDetailModal
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
      />}

      {/* 実績編集ダイアログ (Toggl風) */}
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

      {/* 右クリックメニュー */}
      <ReportsContextMenu
        ctxMenu={ctxMenu}
        ctxMenuRef={ctxMenuRef}
        ctxMenuStyle={ctxMenuStyle}
        handleCtxEdit={handleCtxEdit}
        handleCtxOpenDetail={handleCtxOpenDetail}
        handleCtxDuplicate={handleCtxDuplicate}
        handleCtxDelete={handleCtxDelete}
      />
    </div>
  );
}
