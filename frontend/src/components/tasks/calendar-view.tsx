"use client";

import { useState, useEffect, useCallback, useMemo, useRef } from "react";
import { useRouter } from "next/navigation";
import FullCalendar from "@fullcalendar/react";
import dayGridPlugin from "@fullcalendar/daygrid";
import timeGridPlugin from "@fullcalendar/timegrid";
import interactionPlugin from "@fullcalendar/interaction";
import listPlugin from "@fullcalendar/list";
import type {
  EventClickArg,
  DatesSetArg,
  EventDropArg,
} from "@fullcalendar/core";
import type {
  DateClickArg,
  EventResizeDoneArg,
} from "@fullcalendar/interaction";
import {
  AlertCircle,
  FileText,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  taskApi,
  type RecurringOccurrenceContext,
  type Scope,
  type Task,
  type TaskOccurrence,
} from "@/lib/task-api";
import { listRemoteTaskOccurrences } from "@/lib/remote-servers";
import { listRemoteTasks, toRemoteTask } from "@/lib/remote-tasks";
import { decorateRemoteOccurrence } from "@/lib/remote-resource";
import {
  getTaskNotificationsDefaultEnabled,
  getUserSettings,
  patchUserSettings,
} from "@/lib/user-settings";
import { useTaskCompletionRefresh } from "@/hooks/use-task-completion-refresh";
import { formatDateTimeLocal } from "@/components/tasks/task-form-utils";
import { TaskDetailModal } from "@/components/tasks/task-detail-modal";
import {
  TaskContextMenu,
  useTaskContextMenu,
} from "@/components/tasks/task-context-menu";
import { useProject } from "@/contexts/project-context";
import { useTheme } from "@/contexts/theme-context";
import { parseLocalDateTime } from "@/lib/date-time";
import {
  fallbackColorFromId,
  resolveProjectColorTokens,
} from "@/lib/project-colors";
import { isClosedTaskStatus } from "@/lib/task-status";
import {
  RemoteTaskDialog,
  type RemoteTaskDialogTarget,
} from "@/components/tasks/remote-task-dialog";
import { CalendarWorkspaceNavigation } from "@/components/tasks/calendar-workspace-navigation";
import {
  useWorkspaceShellRegistration,
} from "@/components/layout/shell-context";

type ScopeMode = "project" | "space" | "all";
type CalendarViewName = "dayGridMonth" | "timeGridWeek" | "listWeek";

type CalendarViewSettings = {
  scope?: ScopeMode;
  show_closed?: boolean;
  hide_recurring?: boolean;
  expanded_week_key?: string | null;
  current_view?: CalendarViewName;
  current_date?: string | null;
};

type DocsCalendarItem = {
  id: string;
  node: {
    id: string;
    title: string;
    project_id: string | null;
  };
  field: {
    name: string;
  };
  start: string;
  end: string | null;
  all_day: boolean;
};

interface CalendarEvent {
  id: string;
  title: string;
  start: string;
  end?: string;
  allDay: boolean;
  backgroundColor?: string;
  borderColor?: string;
  textColor?: string;
  classNames?: string[];
  editable?: boolean;
  extendedProps: {
    taskId: string;
    projectId: string | null;
    status: string;
    tags: Task["tags"];
    projectColor: string | null;
    projectName?: string | null;
    occurrenceId?: string | null;
    occurrenceStartAt?: string | null;
    occurrenceEndAt?: string | null;
    occurrenceOriginalStartAt?: string | null;
    occurrenceSourceKind?: string | null;
    isRemote?: boolean;
    isReadOnly?: boolean;
    remoteServerId?: string | null;
    remoteServerName?: string | null;
    remoteBaseUrl?: string | null;
    isDocs?: boolean;
    docsNodeId?: string | null;
    docsFieldName?: string | null;
  };
}

const FC_PLUGINS = [
  dayGridPlugin,
  timeGridPlugin,
  interactionPlugin,
  listPlugin,
];

const FC_HEADER_TOOLBAR = {
  left: "today prev,next",
  center: "title",
  right: "dayGridMonth,timeGridWeek,listWeek",
};

const FC_BUTTON_TEXT = {
  today: "Today",
  month: "Month",
  week: "Week",
  list: "List",
};

function shouldShowEventTime(start: Date | null, allDay: boolean): boolean {
  return Boolean(start && !allDay);
}

function parseTaskCalendarDate(value: string | null | undefined): Date | null {
  if (!value) return null;
  const date = parseLocalDateTime(value) ?? new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

function addLocalDays(date: Date, days: number): Date {
  const next = new Date(date);
  next.setDate(next.getDate() + days);
  return next;
}

function formatTaskDatePayload(
  date: Date | null | undefined,
  options?: { allDay?: boolean; allDayEnd?: boolean },
): string | null {
  if (!date) return null;
  const payloadDate = options?.allDayEnd ? addLocalDays(date, -1) : date;
  if (options?.allDay) return formatLocalDateKey(payloadDate);
  return `${formatDateTimeLocal(payloadDate)}:00`;
}

function getCalendarEventEnd(
  endAt: string | null | undefined,
  allDay: boolean,
): string | undefined {
  if (!endAt) return undefined;
  if (!allDay) return endAt;

  const inclusiveEnd = parseTaskCalendarDate(endAt);
  if (!inclusiveEnd) return endAt;
  return `${formatDateTimeLocal(addLocalDays(inclusiveEnd, 1))}:00`;
}

function formatLocalDateKey(date: Date): string {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function shiftCalendarMonth(
  value: string | null | undefined,
  offset: number,
): string {
  const anchor = parseTaskCalendarDate(value) ?? new Date();
  const day = anchor.getDate();
  const target = new Date(anchor);
  target.setDate(1);
  target.setMonth(target.getMonth() + offset);
  const lastDay = new Date(
    target.getFullYear(),
    target.getMonth() + 1,
    0,
  ).getDate();
  target.setDate(Math.min(day, lastDay));
  return formatLocalDateKey(target);
}

function getEventStartDateKey(event: CalendarEvent): string | null {
  const date = new Date(event.start);
  if (Number.isNaN(date.getTime())) return null;
  return formatLocalDateKey(date);
}

// タスク本体イベントとオカレンスイベントの突き合わせキー。
// 文字列表現の揺れ（秒やタイムゾーン表記）で取りこぼさないよう時刻値で比較する。
function eventOccurrenceKey(event: CalendarEvent): string {
  const taskId = (event.extendedProps.taskId as string | undefined) ?? "";
  const time = new Date(event.start).getTime();
  return `${taskId}:${Number.isNaN(time) ? event.start : time}`;
}

export default function CalendarView() {
  const {
    selectedProjectId,
    selectedSpaceId,
    selectedProject,
    selectedSpace,
    allProjects,
  } = useProject();
  const { resolvedTheme } = useTheme();
  const calendarRef = useRef<FullCalendar>(null);
  const calendarContainerRef = useRef<HTMLDivElement>(null);
  const [calendarMirrorParent, setCalendarMirrorParent] =
    useState<HTMLElement | null>(null);
  const router = useRouter();
  const [scope, setScope] = useState<ScopeMode>("project");
  const [tasks, setTasks] = useState<Task[]>([]);
  const [occurrences, setOccurrences] = useState<TaskOccurrence[]>([]);
  const [docsCalendarItems, setDocsCalendarItems] = useState<
    DocsCalendarItem[]
  >([]);
  const [showDocsLayer, setShowDocsLayer] = useState(false);
  const [loading, setLoading] = useState(false);
  const [calendarError, setCalendarError] = useState<string | null>(null);
  const dateRangeRef = useRef<{ start: string; end: string } | null>(null);
  const fetchGenerationRef = useRef(0);
  const settingsSaveTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(
    null,
  );
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [detailTaskId, setDetailTaskId] = useState<string | null>(null);
  const [selectedOccurrenceContext, setSelectedOccurrenceContext] =
    useState<RecurringOccurrenceContext | null>(null);
  const [draftTask, setDraftTask] = useState<Partial<Task> | null>(null);
  const [taskNotificationsDefaultEnabled, setTaskNotificationsDefaultEnabled] =
    useState(true);
  const [showClosed, setShowClosed] = useState(false);
  const [hideRecurring, setHideRecurring] = useState(false);
  const [expandedWeekKey, setExpandedWeekKey] = useState<string | null>(null);
  const [currentView, setCurrentView] =
    useState<CalendarViewName>("dayGridMonth");
  const [currentDate, setCurrentDate] = useState<string | null>(null);
  const [prefsLoaded, setPrefsLoaded] = useState(false);
  const contextMenu = useTaskContextMenu();
  const lastKeyboardNavigationRef = useRef(0);
  const [remoteDialogTarget, setRemoteDialogTarget] =
    useState<RemoteTaskDialogTarget | null>(null);
  const closeTaskContextMenu = contextMenu.close;

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

  const calendarRemoteContext =
    selectedProject?.source === "remote"
      ? selectedProject
      : selectedSpace?.source === "remote"
        ? selectedSpace
        : null;
  const calendarReadOnly = Boolean(
    calendarRemoteContext || selectedProject?.can_write === false,
  );

  const detailTask = useMemo(
    () =>
      (detailTaskId ? tasks.find((task) => task.id === detailTaskId) : null) ??
      null,
    [detailTaskId, tasks],
  );
  const detailReadOnly = Boolean(
    calendarReadOnly ||
      detailTask?.source === "remote" ||
      isProjectReadOnly(detailTask?.project_id),
  );

  const calendarScopeLabel =
    scope === "all"
      ? "All projects"
      : scope === "space"
        ? selectedSpace
          ? `Space: ${selectedSpace.name}`
          : "No space selected"
        : selectedProject
          ? `Project: ${selectedProject.name}`
          : "No project selected";

  const navigateMiniMonth = useCallback(
    (offset: number) => {
      const api = calendarRef.current?.getApi();
      const baseDate = api ? formatLocalDateKey(api.getDate()) : currentDate;
      const nextDate = shiftCalendarMonth(baseDate, offset);
      api?.gotoDate(nextDate);
    },
    [currentDate],
  );

  useWorkspaceShellRegistration({
    id: "calendar-workspace",
    workspaceNavigation: (
      <CalendarWorkspaceNavigation
        scope={scope}
        scopeLabel={calendarScopeLabel}
        readOnly={calendarReadOnly}
        showDocsLayer={showDocsLayer}
        hideRecurring={hideRecurring}
        showClosed={showClosed}
        onScopeChange={setScope}
        onShowDocsLayerChange={setShowDocsLayer}
        onHideRecurringChange={setHideRecurring}
        onShowClosedChange={setShowClosed}
        currentDate={currentDate}
        onDateChange={(date) => calendarRef.current?.getApi().gotoDate(date)}
        onPreviousMonth={() => navigateMiniMonth(-1)}
        onNextMonth={() => navigateMiniMonth(1)}
      />
    ),
  });

  useEffect(() => {
    if (calendarReadOnly) closeTaskContextMenu();
  }, [calendarReadOnly, closeTaskContextMenu]);

  useEffect(() => {
    setCalendarMirrorParent(document.body);
  }, []);

  const getWeekKey = useCallback((date: Date) => {
    const local = new Date(date);
    local.setHours(0, 0, 0, 0);
    local.setDate(local.getDate() - local.getDay());
    return formatLocalDateKey(local);
  }, []);

  useEffect(() => {
    let active = true;

    void getUserSettings()
      .then((settings) => {
        if (!active) return;
        setTaskNotificationsDefaultEnabled(
          getTaskNotificationsDefaultEnabled(settings),
        );
        const view = settings.calendar_view as CalendarViewSettings | undefined;
        if (!view) return;

        if (
          view.scope === "project" ||
          view.scope === "space" ||
          view.scope === "all"
        ) {
          setScope(view.scope);
        }
        if (typeof view.show_closed === "boolean") {
          setShowClosed(view.show_closed);
        }
        if (typeof view.hide_recurring === "boolean") {
          setHideRecurring(view.hide_recurring);
        }
        if (typeof view.expanded_week_key === "string") {
          setExpandedWeekKey(view.expanded_week_key);
        }
        if (
          view.current_view === "dayGridMonth" ||
          view.current_view === "timeGridWeek" ||
          view.current_view === "listWeek"
        ) {
          setCurrentView(view.current_view);
        }
        if (typeof view.current_date === "string" && view.current_date) {
          setCurrentDate(view.current_date.slice(0, 10));
        }
      })
      .catch((err) => {
        console.error("カレンダー表示設定の取得に失敗しました", err);
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

    if (settingsSaveTimeoutRef.current) {
      clearTimeout(settingsSaveTimeoutRef.current);
    }

    settingsSaveTimeoutRef.current = setTimeout(() => {
      settingsSaveTimeoutRef.current = null;
      void patchUserSettings({
        calendar_view: {
          scope,
          show_closed: showClosed,
          hide_recurring: hideRecurring,
          expanded_week_key: expandedWeekKey,
          current_view: currentView,
          current_date: currentDate,
        } satisfies CalendarViewSettings,
      }).catch((err) => {
        console.error("カレンダー表示設定の保存に失敗しました", err);
      });
    }, 300);

    return () => {
      if (settingsSaveTimeoutRef.current) {
        clearTimeout(settingsSaveTimeoutRef.current);
        settingsSaveTimeoutRef.current = null;
      }
    };
  }, [
    prefsLoaded,
    scope,
    showClosed,
    hideRecurring,
    expandedWeekKey,
    currentView,
    currentDate,
  ]);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;

      const target = e.target instanceof HTMLElement ? e.target : null;
      const tag = target?.tagName;
      if (
        tag === "INPUT" ||
        tag === "TEXTAREA" ||
        tag === "SELECT" ||
        target?.isContentEditable ||
        target?.closest('[role="dialog"]')
      ) {
        return;
      }
      if (selectedTaskId || draftTask) return;

      const api = calendarRef.current?.getApi();
      if (!api) return;

      if (e.repeat) {
        e.preventDefault();
        return;
      }

      const now = performance.now();
      if (now - lastKeyboardNavigationRef.current < 250) {
        e.preventDefault();
        return;
      }
      lastKeyboardNavigationRef.current = now;

      if (e.key === "ArrowLeft") {
        e.preventDefault();
        api.prev();
      } else if (e.key === "ArrowRight") {
        e.preventDefault();
        api.next();
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [selectedTaskId, draftTask]);

  const scopeArg = useMemo<Scope | null>(() => {
    if (scope === "all") return {};
    if (scope === "space")
      return selectedSpaceId ? { space_id: selectedSpaceId } : null;
    return selectedProjectId ? { project_id: selectedProjectId } : null;
  }, [scope, selectedProjectId, selectedSpaceId]);
  const canRenderCalendar = prefsLoaded && scopeArg !== null;

  const fetchData = useCallback(
    async (range?: { start: string; end: string }) => {
      const dr = range || dateRangeRef.current;
      if (!scopeArg || !dr) return;

      const generation = fetchGenerationRef.current + 1;
      fetchGenerationRef.current = generation;
      setLoading(true);
      setCalendarError(null);
      try {
        const remoteContext =
          selectedProject?.source === "remote"
            ? selectedProject
            : selectedSpace?.source === "remote"
              ? selectedSpace
              : null;
        const remoteProfileId = remoteContext?.remote_server_id;
        const remoteScope =
          scope === "project" && selectedProject?.source === "remote"
            ? { project_id: selectedProject.resource_id }
            : scope === "space" && selectedSpace?.source === "remote"
              ? { space_id: selectedSpace.resource_id }
              : selectedProject?.source === "remote"
                ? { project_id: selectedProject.resource_id }
                : selectedSpace?.source === "remote"
                  ? { space_id: selectedSpace.resource_id }
                  : {};
        const remoteTasksPromise = remoteProfileId
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
          : Promise.resolve([] as Task[]);
        const remoteOccurrencesPromise = remoteProfileId
          ? listRemoteTaskOccurrences(remoteProfileId, {
              ...remoteScope,
              start_from: dr.start,
              end_to: dr.end,
            }).then((items) =>
              items.map((item) =>
                decorateRemoteOccurrence(remoteProfileId, item),
              ),
            )
          : Promise.resolve([] as TaskOccurrence[]);
        const localDocsPromise = remoteProfileId
          ? Promise.resolve([] as DocsCalendarItem[])
          : fetch(
              `/api/docs/calendar-items?${new URLSearchParams({
                start: dr.start,
                end: dr.end,
                ...(scopeArg.project_id
                  ? { project_id: scopeArg.project_id }
                  : {}),
                ...(scopeArg.space_id ? { space_id: scopeArg.space_id } : {}),
              }).toString()}`,
              {
                credentials: "include",
              },
            )
              .then((res) => (res.ok ? res.json() : { items: [] }))
              .then((data) =>
                Array.isArray(data.items)
                  ? (data.items as DocsCalendarItem[])
                  : [],
              )
              .catch((err) => {
                console.error("Docsカレンダー取得失敗:", err);
                return [] as DocsCalendarItem[];
              });
        const [taskList, occList, docsItems] = remoteProfileId
          ? await Promise.all([
              remoteTasksPromise,
              remoteOccurrencesPromise,
              localDocsPromise,
            ])
          : await Promise.all([
              taskApi.listTasks(scopeArg),
              taskApi
                .listOccurrences(scopeArg, dr.start, dr.end)
                .catch((err) => {
                  console.error("カレンダー繰り返し予定取得失敗:", err);
                  return [] as TaskOccurrence[];
                }),
              localDocsPromise,
            ]);
        if (fetchGenerationRef.current !== generation) {
          return;
        }
        setTasks(taskList);
        setOccurrences(occList);
        setDocsCalendarItems(docsItems);
        setCalendarError(null);
      } catch (err) {
        if (fetchGenerationRef.current !== generation) {
          return;
        }
        console.error("カレンダーデータ取得失敗:", err);
        setTasks([]);
        setOccurrences([]);
        setDocsCalendarItems([]);
        setCalendarError(
          "Calendar data could not be loaded. Try again.",
        );
      } finally {
        if (fetchGenerationRef.current === generation) {
          setLoading(false);
        }
      }
    },
    [scopeArg, scope, selectedProject, selectedSpace],
  );

  useTaskCompletionRefresh(fetchData);

  useEffect(() => {
    return () => {
      fetchGenerationRef.current += 1;
    };
  }, []);

  useEffect(() => {
    if (scopeArg && dateRangeRef.current) {
      fetchData();
    } else if (!scopeArg) {
      fetchGenerationRef.current += 1;
      setLoading(false);
      setTasks([]);
      setOccurrences([]);
      setDocsCalendarItems([]);
      setCalendarError(null);
    }
  }, [fetchData, scopeArg]);

  const handleDatesSet = useCallback(
    (arg: DatesSetArg) => {
      const nextView = arg.view.type as CalendarViewName;
      const nextDate = formatLocalDateKey(arg.view.calendar.getDate());
      setCurrentView((prev) => (prev === nextView ? prev : nextView));
      setCurrentDate((prev) => (prev === nextDate ? prev : nextDate));
      const prev = dateRangeRef.current;
      if (arg.view.type !== "dayGridMonth") {
        setExpandedWeekKey((value) => (value === null ? value : null));
      }
      if (prev && prev.start === arg.startStr && prev.end === arg.endStr) {
        return;
      }
      dateRangeRef.current = { start: arg.startStr, end: arg.endStr };
      fetchData({ start: arg.startStr, end: arg.endStr });
    },
    [fetchData],
  );

  const handleCreateTask = useCallback(
    (startAt?: string, allDay?: boolean, endAt?: string) => {
      if (!selectedProjectId) {
        alert("Select a project before creating a task.");
        return;
      }
      if (calendarReadOnly) {
        alert("Enterprise reference is read-only.");
        return;
      }
      setSelectedTaskId(null);
      setDetailTaskId(null);
      setSelectedOccurrenceContext(null);
      setRemoteDialogTarget(null);
      closeTaskContextMenu();
      setDraftTask({
        project_id: selectedProjectId,
        title: "",
        notifications_enabled: taskNotificationsDefaultEnabled,
        ...(startAt && { start_at: startAt }),
        ...(endAt && { end_at: endAt }),
        ...(allDay !== undefined && { all_day: allDay }),
      });
    },
    [
      selectedProjectId,
      calendarReadOnly,
      closeTaskContextMenu,
      taskNotificationsDefaultEnabled,
    ],
  );

  const handleDateClick = useCallback(
    (arg: DateClickArg) => {
      const target =
        arg.jsEvent.target instanceof Element ? arg.jsEvent.target : null;
      if (
        target?.closest(
          ".ao-calendar-toggle, .fc-daygrid-more-link, .fc-more-popover, .fc-popover",
        )
      ) {
        return;
      }
      if (arg.allDay || !arg.dateStr.includes("T")) {
        const d = new Date(arg.date);
        d.setHours(0, 0, 0, 0);
        const dateOnlyStart = formatDateTimeLocal(d);
        handleCreateTask(dateOnlyStart, true, dateOnlyStart);
      } else {
        handleCreateTask(formatDateTimeLocal(arg.date), false);
      }
    },
    [handleCreateTask],
  );

  const events: CalendarEvent[] = useMemo(() => {
    const taskEvents: CalendarEvent[] = tasks
      .filter(
        (task) =>
          (task.start_at || task.end_at) &&
          !task.parent_task_id &&
          !task.has_recurrence &&
          (showClosed || !isClosedTaskStatus(task.status)),
      )
      .map((task) => {
        const colorTokens = resolveProjectColorTokens(
          task.project_color,
          resolvedTheme,
          fallbackColorFromId(task.project_id),
        );
        return {
          id: `task-${task.id}`,
          title: task.title,
          start: task.start_at || task.end_at || "",
          end: getCalendarEventEnd(task.end_at, task.all_day),
          allDay: task.all_day,
          backgroundColor: "transparent",
          borderColor: "transparent",
          textColor: colorTokens?.text ?? "var(--foreground)",
          classNames: [
            `event-${task.status}`,
            ...(task.source === "remote" ? ["event-remote"] : []),
          ],
          editable:
            task.source !== "remote" && !isProjectReadOnly(task.project_id),
          extendedProps: {
            taskId: task.id,
            projectId: task.project_id,
            status: task.status,
            priority: task.priority,
            tags: task.tags || [],
            projectColor: colorTokens?.accent ?? null,
            projectName: task.project_name ?? null,
            occurrenceId: null,
            occurrenceStartAt: null,
            occurrenceEndAt: null,
            occurrenceOriginalStartAt: null,
            occurrenceSourceKind: null,
            isRemote: task.source === "remote",
            isReadOnly:
              task.source === "remote" || isProjectReadOnly(task.project_id),
            remoteServerId: task.remote_server_id,
            remoteServerName: task.remote_server_name,
            remoteBaseUrl: task.remote_server_base_url,
            remoteTaskId: task.resource_id,
          },
        };
      });

    const occurrenceEvents: CalendarEvent[] = (hideRecurring ? [] : occurrences)
      .filter(
        (occurrence) => showClosed || !isClosedTaskStatus(occurrence.status),
      )
      .map((occurrence) => {
        const colorTokens = resolveProjectColorTokens(
          occurrence.project_color,
          resolvedTheme,
          fallbackColorFromId(occurrence.project_id),
        );
        return {
          id: `occ-${occurrence.id}`,
          title: occurrence.title || "(無題)",
          start: occurrence.start_at || "",
          end: getCalendarEventEnd(occurrence.end_at, occurrence.all_day),
          allDay: occurrence.all_day,
          backgroundColor: "transparent",
          borderColor: "transparent",
          textColor: colorTokens?.text ?? "var(--foreground)",
          classNames: [
            `event-${occurrence.status}`,
            ...(occurrence.source === "remote" ? ["event-remote"] : []),
          ],
          editable:
            occurrence.source !== "remote" &&
            !isProjectReadOnly(occurrence.project_id),
          extendedProps: {
            taskId: occurrence.task_id,
            projectId: occurrence.project_id ?? null,
            status: occurrence.status,
            priority: "none",
            tags: occurrence.tags || [],
            projectColor: colorTokens?.accent ?? null,
            projectName: occurrence.project_name ?? null,
            occurrenceId:
              occurrence.id.startsWith("generated-") ||
              occurrence.id.startsWith("base-")
                ? null
                : occurrence.id,
            occurrenceStartAt: occurrence.start_at ?? null,
            occurrenceEndAt: occurrence.end_at ?? null,
            occurrenceOriginalStartAt:
              occurrence.original_start_at ?? occurrence.start_at ?? null,
            occurrenceSourceKind: occurrence.source_kind,
            isRemote: occurrence.source === "remote",
            isReadOnly:
              occurrence.source === "remote" ||
              isProjectReadOnly(occurrence.project_id),
            remoteServerId: occurrence.remote_server_id,
            remoteTaskId: occurrence.task_id.replace(/^remote:[^:]+:/, ""),
          },
        };
      });

    const docsEvents: CalendarEvent[] = showDocsLayer
      ? docsCalendarItems.map((item) => ({
          id: `docs-${item.id}`,
          // 1ノードが複数のdateフィールドを持つ場合に見分けが付くよう、フィールド名を併記する
          title: item.node.title
            ? `${item.node.title}（${item.field.name}）`
            : item.field.name,
          start: item.start,
          end: item.end ?? item.start,
          allDay: item.all_day,
          backgroundColor: "color-mix(in srgb, #0ea5e9 16%, transparent)",
          borderColor: "#0ea5e9",
          textColor: "var(--foreground)",
          classNames: ["event-docs"],
          editable: false,
          extendedProps: {
            taskId: "",
            projectId: item.node.project_id,
            status: "docs",
            tags: [],
            projectColor: "#0ea5e9",
            projectName: "Docs",
            isDocs: true,
            docsNodeId: item.node.id,
            docsFieldName: item.field.name,
          },
        }))
      : [];

    // 同じタスクの同じ開始時刻がタスク本体とオカレンスの両方から来たらオカレンスを正とし、
    // タスク本体側を落とす。通常は API 側で除外済みだが、remote サーバー由来の
    // オカレンス（listRemoteTaskOccurrences）は相手側の実装に依存するため保険を残す。
    const occurrenceKeys = new Set(occurrenceEvents.map(eventOccurrenceKey));
    const dedupedTaskEvents = taskEvents.filter(
      (event) => !occurrenceKeys.has(eventOccurrenceKey(event)),
    );

    return [...dedupedTaskEvents, ...occurrenceEvents, ...docsEvents];
  }, [
    tasks,
    occurrences,
    docsCalendarItems,
    showDocsLayer,
    showClosed,
    hideRecurring,
    resolvedTheme,
    isProjectReadOnly,
  ]);

  const calendarEvents = useMemo(() => {
    if (currentView !== "dayGridMonth") return events;

    const visibleCountsByDate = new Map<string, number>();
    return events.filter((event) => {
      const dateKey = getEventStartDateKey(event);
      if (!dateKey) return true;

      const weekKey = getWeekKey(new Date(`${dateKey}T00:00:00`));
      if (expandedWeekKey === weekKey) return true;

      const nextCount = (visibleCountsByDate.get(dateKey) ?? 0) + 1;
      visibleCountsByDate.set(dateKey, nextCount);
      return nextCount <= 3;
    });
  }, [events, currentView, expandedWeekKey, getWeekKey]);

  const hiddenEventCountsByDate = useMemo(() => {
    if (currentView !== "dayGridMonth") return new Map<string, number>();

    const counts = new Map<string, number>();
    events.forEach((event) => {
      const dateKey = getEventStartDateKey(event);
      if (!dateKey) return;
      counts.set(dateKey, (counts.get(dateKey) ?? 0) + 1);
    });

    const hiddenCounts = new Map<string, number>();
    counts.forEach((count, dateKey) => {
      const hiddenCount = count - 3;
      if (hiddenCount > 0) {
        hiddenCounts.set(dateKey, hiddenCount);
      }
    });
    return hiddenCounts;
  }, [events, currentView]);

  const handleEventClick = useCallback(
    (info: EventClickArg) => {
      const props = info.event.extendedProps;
      if (props.isDocs && props.docsNodeId) {
        closeTaskContextMenu();
        setSelectedTaskId(null);
        setDetailTaskId(null);
        setSelectedOccurrenceContext(null);
        setDraftTask(null);
        setRemoteDialogTarget(null);
        router.push(`/docs/${encodeURIComponent(props.docsNodeId as string)}`);
        return;
      }
      if (props.isRemote && props.remoteServerId) {
        closeTaskContextMenu();
        setSelectedTaskId(null);
        setDetailTaskId(null);
        setSelectedOccurrenceContext(null);
        setDraftTask(null);
        setRemoteDialogTarget(null);
        setRemoteDialogTarget({
          profileId: props.remoteServerId as string,
          profileName: (props.remoteServerName as string) ?? "remote",
          profileColor: (props.projectColor as string | null) ?? null,
          baseUrl: (props.remoteBaseUrl as string | null) ?? "",
          taskId: (props.remoteTaskId as string) ?? (props.taskId as string),
          title: info.event.title.replace(/^\[[^\]]*\]\s*/, ""),
          status: (props.status as string) ?? "open",
          priority: (props.priority as string) ?? "none",
          startAt: info.event.start
            ? formatDateTimeLocal(info.event.start)
            : null,
          endAt: info.event.end ? formatDateTimeLocal(info.event.end) : null,
        });
        return;
      }
      const taskId = info.event.extendedProps.taskId;
      if (taskId) {
        closeTaskContextMenu();
        setDraftTask(null);
        setDetailTaskId(null);
        setRemoteDialogTarget(null);
        const occurrenceStartAt = info.event.extendedProps.occurrenceStartAt as
          | string
          | null
          | undefined;
        setSelectedOccurrenceContext(
          occurrenceStartAt
            ? {
                occurrence_id:
                  (info.event.extendedProps.occurrenceId as string | null) ??
                  null,
                start_at: occurrenceStartAt,
                end_at:
                  (info.event.extendedProps.occurrenceEndAt as string | null) ??
                  null,
                original_start_at:
                  (info.event.extendedProps.occurrenceOriginalStartAt as
                    | string
                    | null) ?? null,
                source_kind:
                  (info.event.extendedProps.occurrenceSourceKind as
                    | string
                    | null) ?? "task_schedule",
                status:
                  (info.event.extendedProps.status as string | null) ?? null,
              }
            : null,
        );
        setSelectedTaskId(taskId);
        setDetailTaskId(taskId);
      }
    },
    [closeTaskContextMenu, router],
  );

  const handleEventContextMenu = useCallback(
    (
      ev: React.MouseEvent,
      taskId: string,
      eventTitle: string,
      eventProjectId: string | null,
      eventStatus: string | null,
      eventTags: Task["tags"],
      eventAllDay: boolean,
      occurrenceContext?: RecurringOccurrenceContext | null,
    ) => {
      if (calendarReadOnly) return;
      const task = tasks.find((t) => t.id === taskId);
      const menuTask: Task =
        task ??
        ({
          id: taskId,
          project_id: eventProjectId ?? "",
          title: eventTitle || "(無題)",
          description: null,
          status: eventStatus ?? occurrenceContext?.status ?? "open",
          priority: "medium",
          start_at: occurrenceContext?.start_at ?? null,
          end_at: occurrenceContext?.end_at ?? null,
          all_day: eventAllDay,
          reminder_offsets: [],
          notifications_enabled: true,
          source: "calendar",
          metadata: {},
          assignees: [],
          tags: eventTags || [],
          active_time_entry: null,
          has_recurrence: !!occurrenceContext?.start_at,
        } as Task);
      contextMenu.open(
        ev,
        occurrenceContext?.status
          ? { ...menuTask, status: occurrenceContext.status }
          : menuTask,
        occurrenceContext,
      );
    },
    [calendarReadOnly, contextMenu, tasks],
  );

  const handleEventDrop = useCallback(
    async (info: EventDropArg) => {
      if (
        info.event.extendedProps.isRemote ||
        info.event.extendedProps.isReadOnly
      ) {
        info.revert();
        return;
      }
      const taskId = info.event.extendedProps.taskId;
      if (!taskId) return;

      try {
        const nextStart = info.event.start;
        const nextEnd =
          info.event.end ??
          (() => {
            const prevStart = info.oldEvent.start;
            const prevEnd = info.oldEvent.end;
            if (!nextStart || !prevStart || !prevEnd) return null;
            return new Date(
              nextStart.getTime() + (prevEnd.getTime() - prevStart.getTime()),
            );
          })();

        const occurrenceStartAt = info.event.extendedProps.occurrenceStartAt as
          | string
          | null
          | undefined;

        if (occurrenceStartAt && nextStart) {
          await taskApi.moveOccurrence(taskId, {
            occurrence_id:
              (info.event.extendedProps.occurrenceId as string | null) ?? null,
            occurrence_start_at: occurrenceStartAt,
            occurrence_end_at:
              (info.event.extendedProps.occurrenceEndAt as string | null) ??
              null,
            original_start_at:
              (info.event.extendedProps.occurrenceOriginalStartAt as
                | string
                | null) ?? null,
            next_start_at:
              formatTaskDatePayload(nextStart, {
                allDay: info.event.allDay,
              }) ?? "",
            next_end_at: formatTaskDatePayload(nextEnd, {
              allDay: info.event.allDay,
              allDayEnd: info.event.allDay,
            }),
            status: (info.event.extendedProps.status as string | null) ?? null,
            all_day: info.event.allDay,
          });
        } else {
          await taskApi.updateTask(taskId, {
            start_at:
              formatTaskDatePayload(nextStart, {
                allDay: info.event.allDay,
              }) ?? undefined,
            end_at:
              formatTaskDatePayload(nextEnd, {
                allDay: info.event.allDay,
                allDayEnd: info.event.allDay,
              }) || undefined,
            all_day: info.event.allDay,
          });
        }
        fetchData();
      } catch {
        info.revert();
      }
    },
    [fetchData],
  );

  const handleEventResize = useCallback(
    async (info: EventResizeDoneArg) => {
      if (
        info.event.extendedProps.isRemote ||
        info.event.extendedProps.isReadOnly
      ) {
        info.revert();
        return;
      }
      const taskId = info.event.extendedProps.taskId;
      if (!taskId) return;
      try {
        const occurrenceStartAt = info.event.extendedProps.occurrenceStartAt as
          | string
          | null
          | undefined;
        if (occurrenceStartAt && info.event.start) {
          await taskApi.moveOccurrence(taskId, {
            occurrence_id:
              (info.event.extendedProps.occurrenceId as string | null) ?? null,
            occurrence_start_at: occurrenceStartAt,
            occurrence_end_at:
              (info.event.extendedProps.occurrenceEndAt as string | null) ??
              null,
            original_start_at:
              (info.event.extendedProps.occurrenceOriginalStartAt as
                | string
                | null) ?? null,
            next_start_at:
              formatTaskDatePayload(info.event.start, {
                allDay: info.event.allDay,
              }) ?? "",
            next_end_at: formatTaskDatePayload(info.event.end, {
              allDay: info.event.allDay,
              allDayEnd: info.event.allDay,
            }),
            status: (info.event.extendedProps.status as string | null) ?? null,
            all_day: info.event.allDay,
          });
        } else {
          await taskApi.updateTask(taskId, {
            start_at:
              formatTaskDatePayload(info.event.start, {
                allDay: info.event.allDay,
              }) ?? undefined,
            end_at:
              formatTaskDatePayload(info.event.end, {
                allDay: info.event.allDay,
                allDayEnd: info.event.allDay,
              }) || undefined,
            all_day: info.event.allDay,
          });
        }
        fetchData();
      } catch {
        info.revert();
      }
    },
    [fetchData],
  );

  useEffect(() => {
    const root = calendarContainerRef.current;
    const viewType = calendarRef.current?.getApi().view.type;
    if (!root || viewType !== "dayGridMonth") {
      root
        ?.querySelectorAll(".ao-calendar-toggle")
        .forEach((el) => el.remove());
      return;
    }

    root.querySelectorAll(".ao-calendar-toggle").forEach((el) => el.remove());

    const collapseRenderedWeeks = new Set<string>();
    const cells = root.querySelectorAll<HTMLElement>(
      ".fc-daygrid-day[data-date]",
    );

    cells.forEach((cell) => {
      const dateStr = cell.dataset.date;
      const bottom = cell.querySelector<HTMLElement>(".fc-daygrid-day-bottom");
      if (!dateStr || !bottom) return;

      const hiddenCount = hiddenEventCountsByDate.get(dateStr) ?? 0;
      if (hiddenCount === 0) return;

      const weekKey = getWeekKey(new Date(`${dateStr}T00:00:00`));
      const isExpanded = expandedWeekKey === weekKey;
      if (isExpanded && collapseRenderedWeeks.has(weekKey)) return;

      const button = document.createElement("button");
      button.type = "button";
      button.className = "ao-calendar-toggle";
      button.textContent = isExpanded ? "Collapse" : `+${hiddenCount} more`;
      button.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        setExpandedWeekKey((current) => (current === weekKey ? null : weekKey));
      });
      bottom.appendChild(button);

      if (isExpanded) {
        collapseRenderedWeeks.add(weekKey);
      }
    });
  }, [hiddenEventCountsByDate, expandedWeekKey, getWeekKey]);

  return (
    <div
      className="ao-calendar-canvas flex h-full min-h-0 flex-col bg-background"
      data-shell-workspace="calendar"
      data-shell-region="calendar-canvas"
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-lg border border-border bg-card px-3 py-2 md:hidden">
        <Tabs value={scope} onValueChange={(v) => setScope(v as ScopeMode)}>
          <TabsList className="h-8">
            <TabsTrigger value="project" className="text-xs">
              Project
            </TabsTrigger>
            <TabsTrigger value="space" className="text-xs">
              Space
            </TabsTrigger>
            <TabsTrigger value="all" className="text-xs">
              All
            </TabsTrigger>
          </TabsList>
        </Tabs>

        <div className="text-xs text-muted-foreground">
          {scope === "all"
            ? "All projects"
            : scope === "space"
              ? selectedSpace
                ? `Space: ${selectedSpace.name}`
                : "No space selected"
              : selectedProject
                ? `Project: ${selectedProject.name}`
                : "No project selected"}
        </div>

        <div className="ml-auto flex items-center gap-3">
          <label className="flex cursor-pointer select-none items-center gap-1.5 text-xs text-muted-foreground">
            <Checkbox
              checked={showDocsLayer}
              onCheckedChange={(checked) => setShowDocsLayer(!!checked)}
            />
            Docs Items
          </label>
          <label className="flex cursor-pointer select-none items-center gap-1.5 text-xs text-muted-foreground">
            <Checkbox
              checked={hideRecurring}
              onCheckedChange={(checked) => setHideRecurring(!!checked)}
            />
            Hide Recurring
          </label>
          <label className="flex cursor-pointer select-none items-center gap-1.5 text-xs text-muted-foreground">
            <Checkbox
              checked={showClosed}
              onCheckedChange={(checked) => setShowClosed(!!checked)}
            />
            Show Completed
          </label>
        </div>
      </div>

      <div
        ref={calendarContainerRef}
        className="ao-calendar-frame relative min-h-0 flex-1 overflow-hidden border border-border bg-card"
        style={{ minHeight: "500px" }}
      >
        {canRenderCalendar && calendarError ? (
          <div
            role="alert"
            className="flex h-full flex-col items-center justify-center gap-3 rounded-lg border border-destructive/35 bg-destructive/5 px-6 text-center"
          >
            <AlertCircle className="size-6 text-destructive" aria-hidden="true" />
            <p className="text-sm text-foreground">{calendarError}</p>
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => void fetchData()}
            >
              Retry
            </Button>
          </div>
        ) : canRenderCalendar ? (
          <FullCalendar
            ref={calendarRef}
            plugins={FC_PLUGINS}
            initialView={currentView}
            initialDate={currentDate ?? undefined}
            locale="en"
            headerToolbar={FC_HEADER_TOOLBAR}
            buttonText={FC_BUTTON_TEXT}
            events={calendarEvents}
            eventDisplay="block"
            editable
            eventClick={handleEventClick}
            eventDrop={handleEventDrop}
            eventResize={handleEventResize}
            fixedMirrorParent={calendarMirrorParent ?? undefined}
            eventContent={(info) => {
              const tags =
                (info.event.extendedProps.tags as Task["tags"]) || [];
              const projectName = info.event.extendedProps.projectName as
                | string
                | null
                | undefined;
              const projectAccent =
                (info.event.extendedProps.projectColor as string | null) ??
                undefined;
              const isDocs = Boolean(info.event.extendedProps.isDocs);
              const tagTokens = tags.length === 1
                ? resolveProjectColorTokens(tags[0].color, resolvedTheme)
                : null;
              const tagSurface = tagTokens?.surface;
              const tagAccent = tagTokens?.accent;
              const singleTagTint =
                resolvedTheme === "light" && tagSurface && tagAccent
                  ? `color-mix(in srgb, ${tagSurface} 72%, ${tagAccent} 28%)`
                  : tagSurface;
              const eventTint = isDocs
                ? "color-mix(in srgb, #0ea5e9 16%, var(--surface-slate))"
                : tags.length === 1
                  ? singleTagTint ?? "var(--surface-slate)"
                  : "var(--surface-slate)";
              const eventBorder = tags.length === 1 ? tagTokens?.border : undefined;
              const eventCardClasses = [
                "ao-cal-event",
                isDocs ? "ao-cal-event-docs" : "",
                tags.length === 1
                  ? "ao-cal-event-tag-single"
                  : tags.length > 1
                    ? "ao-cal-event-tag-multi"
                    : "ao-cal-event-tag-none",
              ]
                .filter(Boolean)
                .join(" ");
              const timeText = shouldShowEventTime(
                info.event.start,
                info.event.allDay,
              )
                ? info.timeText
                : "";

              // 月表示: 高さ一定のコンパクト1行チップ
              if (info.view.type === "dayGridMonth") {
                const tooltip = [
                  info.event.title,
                  projectName || null,
                  tags.length > 0
                    ? tags.map((tag) => tag.name).join(", ")
                    : null,
                ]
                  .filter(Boolean)
                  .join(" / ");
                return (
                  <div
                    className={`${eventCardClasses} rounded-r px-1.5 py-1 transition-colors hover:bg-foreground/[0.09]`}
                    style={{
                      backgroundColor: eventTint,
                      borderColor: eventBorder,
                    }}
                    title={tooltip}
                  >
                    <span
                      className="ao-cal-event-bar"
                      style={{ backgroundColor: projectAccent ?? "var(--border)" }}
                    />
                    {isDocs && (
                      <FileText className="ao-cal-event-doc-icon" aria-hidden="true" />
                    )}
                    <span className="ao-cal-event-content">
                      <span className="ao-cal-event-title">
                        {isDocs && <span className="ao-cal-event-doc-label">Docs</span>}
                        {info.event.title}
                      </span>
                      {timeText && (
                        <span className="ao-cal-event-time">{timeText}</span>
                      )}
                    </span>
                    {tags.length > 1 && (
                      <span className="ao-cal-event-tags" aria-label={`${tags.length} tags`}>
                        {tags.slice(0, 3).map((tag) => {
                          const tokens = resolveProjectColorTokens(
                            tag.color,
                            resolvedTheme,
                          );
                          return (
                            <span
                              key={tag.id}
                              className="ao-cal-event-tag-dot"
                              style={{ backgroundColor: tokens?.accent ?? "var(--primary)" }}
                            />
                          );
                        })}
                        {tags.length > 3 && (
                          <span className="ao-cal-event-tag-count">+{tags.length - 3}</span>
                        )}
                      </span>
                    )}
                  </div>
                );
              }

              // 週表示・リスト表示: プロジェクト名・タグを表示
              const visibleTags = tags.length > 1 ? tags.slice(0, 2) : [];
              const hiddenTagCount = tags.length > 2 ? tags.length - visibleTags.length : 0;
              return (
                <div
                  className={`${eventCardClasses.replace("ao-cal-event", "ao-cal-event-list")} min-w-0 overflow-hidden rounded px-1.5 py-1 transition-colors hover:bg-foreground/[0.09]`}
                  style={{
                    backgroundColor: eventTint,
                    borderColor: eventBorder,
                    borderLeftColor: projectAccent ?? "var(--border)",
                  }}
                >
                  {isDocs && (
                    <FileText className="ao-cal-event-doc-icon" aria-hidden="true" />
                  )}
                  <div className="ao-cal-event-list-body">
                    <div className="flex min-w-0 items-baseline gap-1">
                      <div className="truncate font-medium">
                        {isDocs && <span className="ao-cal-event-doc-label">Docs</span>}
                        {info.event.title}
                      </div>
                      {timeText && (
                        <span className="shrink-0 text-[10px] font-medium text-muted-foreground">
                          {timeText}
                        </span>
                      )}
                    </div>
                    {projectName && (
                      <div className="truncate text-[10px] text-muted-foreground">
                        {projectName}
                      </div>
                    )}
                    {visibleTags.length > 0 && (
                      <div className="mt-0.5 flex gap-1 overflow-hidden">
                        {visibleTags.map((tag) =>
                          (() => {
                            const tagTokens = resolveProjectColorTokens(
                              tag.color,
                              resolvedTheme,
                            );
                            const chipSurface =
                              resolvedTheme === "light" &&
                              tagTokens?.surface &&
                              tagTokens.accent
                                ? `color-mix(in srgb, ${tagTokens.surface} 72%, ${tagTokens.accent} 28%)`
                                : tagTokens?.surface ?? "var(--muted)";
                            return (
                              <span
                                key={tag.id}
                                className="truncate rounded border px-1 py-0 text-[9px] font-medium"
                                style={{
                                  backgroundColor: chipSurface,
                                  borderColor:
                                    tagTokens?.border ?? "var(--border)",
                                  color: tagTokens?.text ?? "var(--foreground)",
                                }}
                              >
                                {tag.name}
                              </span>
                            );
                          })(),
                        )}
                        {hiddenTagCount > 0 && (
                          <span className="shrink-0 rounded bg-muted px-1 py-0 text-[9px] font-medium text-muted-foreground">
                            +{hiddenTagCount}
                          </span>
                        )}
                      </div>
                    )}
                  </div>
                </div>
              );
            }}
            eventDidMount={(info) => {
              const taskId = info.event.extendedProps.taskId as
                | string
                | undefined;
              if (
                !taskId ||
                info.event.extendedProps.isRemote ||
                info.event.extendedProps.isReadOnly
              )
                return;
              const occurrenceStartAt = info.event.extendedProps
                .occurrenceStartAt as string | null | undefined;
              info.el.addEventListener("contextmenu", (e) => {
                e.preventDefault();
                e.stopPropagation();
                handleEventContextMenu(
                  e as unknown as React.MouseEvent,
                  taskId,
                  info.event.title,
                  (info.event.extendedProps.projectId as string | null) ?? null,
                  (info.event.extendedProps.status as string | null) ?? null,
                  (info.event.extendedProps.tags as Task["tags"]) ?? [],
                  info.event.allDay,
                  occurrenceStartAt
                    ? {
                        occurrence_id:
                          (info.event.extendedProps.occurrenceId as
                            | string
                            | null) ?? null,
                        start_at: occurrenceStartAt,
                        end_at:
                          (info.event.extendedProps.occurrenceEndAt as
                            | string
                            | null) ?? null,
                        original_start_at:
                          (info.event.extendedProps
                            .occurrenceOriginalStartAt as string | null) ??
                          null,
                        source_kind:
                          (info.event.extendedProps.occurrenceSourceKind as
                            | string
                            | null) ?? "task_schedule",
                        status:
                          (info.event.extendedProps.status as string | null) ??
                          null,
                      }
                    : null,
                );
              });
            }}
            dateClick={handleDateClick}
            datesSet={handleDatesSet}
            dayCellClassNames={(arg) =>
              arg.view.type === "dayGridMonth"
                ? expandedWeekKey === getWeekKey(new Date(arg.date))
                  ? ["ao-calendar-week-expanded"]
                  : ["ao-calendar-week-collapsed"]
                : []
            }
            height="100%"
            dayMaxEvents={false}
            nowIndicator
          />
        ) : (
          <div className="flex h-full items-center justify-center rounded-lg border border-dashed border-border text-sm text-muted-foreground">
            {prefsLoaded && scopeArg === null
              ? scope === "space"
                ? "Select a space to view this calendar"
                : "Select a project to view this calendar"
              : "Preparing calendar..."}
          </div>
        )}
        {loading && prefsLoaded && (
          <div className="pointer-events-none absolute right-3 top-3 rounded-md border border-border bg-card/90 px-2 py-1 text-xs text-muted-foreground shadow-sm">
            Loading...
          </div>
        )}
        {!prefsLoaded && (
          <div className="absolute inset-0 flex items-center justify-center rounded-xl bg-background/50">
            <div className="size-6 animate-spin rounded-full border-2 border-primary border-t-transparent" />
          </div>
        )}
      </div>

      <TaskDetailModal
        taskId={detailTaskId}
        draftTask={draftTask}
        readOnly={detailReadOnly}
        open={!!detailTaskId || !!draftTask}
        onOpenChange={(open) => {
          if (open) return;
          setSelectedTaskId(null);
          setDetailTaskId(null);
          setSelectedOccurrenceContext(null);
          setDraftTask(null);
        }}
        onTaskUpdated={() => {
          fetchData();
          window.dispatchEvent(new Event("task-list-refresh"));
        }}
        occurrenceContext={selectedOccurrenceContext}
      />
      <TaskContextMenu
        menu={contextMenu.menu}
        onClose={contextMenu.close}
        onRefresh={() => {
          fetchData();
          window.dispatchEvent(new Event("task-list-refresh"));
        }}
      />
      <RemoteTaskDialog
        target={remoteDialogTarget}
        onClose={() => setRemoteDialogTarget(null)}
        onUpdated={() => fetchData()}
      />
    </div>
  );
}
