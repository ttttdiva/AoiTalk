import React, { useState, useCallback, useMemo, useEffect, useRef } from "react";
import {
  Alert,
  AppState,
  FlatList,
  Pressable,
  StyleSheet,
  View,
  useWindowDimensions,
} from "react-native";
import type {
  LayoutChangeEvent,
  NativeScrollEvent,
  NativeSyntheticEvent,
} from "react-native";
import {
  Text,
  Surface,
  IconButton,
  Switch,
} from "react-native-paper";
import { CalendarList, LocaleConfig } from "react-native-calendars";
import { useFocusEffect, useRouter } from "expo-router";
import {
  endOfMonth,
  format,
  parseISO,
  startOfMonth,
  startOfWeek,
  endOfWeek,
  addMonths,
  differenceInCalendarMonths,
} from "date-fns";
import { ja } from "date-fns/locale";
import { tasksRepo, occurrencesRepo } from "../../../repositories";
import { useSyncStatusStore } from "../../../stores/sync-status";
import { useAuth } from "../../../contexts/AuthContext";
import { useProject } from "../../../contexts/ProjectContext";
import { taskApi } from "../../../lib/task-api";
import type { Task, TaskOccurrence, Tag } from "../../../types/api";
import {
  createTaskCompletionUndoEntry,
  enqueueTaskCompletionUndoBatch,
  isTaskCompletionTransition,
  useTaskCompletionUndoStore,
} from "../../../stores/task-completion-undo";
import { useRemoteTasks } from "../../../hooks/use-remote-tasks";
import type { RemoteTask } from "../../../lib/remote-tasks";
import {
  RemoteTaskDialog,
  type RemoteTaskDialogTarget,
} from "../../../components/remote-task-dialog";
import { ScreenHeader } from "../../../components/screen-header";
import { ScopeSwitcher } from "../../../components/scope-switcher";
import { scheduleAfterNavigation } from "../../../lib/after-navigation";

LocaleConfig.locales["ja"] = {
  monthNames: [
    "1月",
    "2月",
    "3月",
    "4月",
    "5月",
    "6月",
    "7月",
    "8月",
    "9月",
    "10月",
    "11月",
    "12月",
  ],
  monthNamesShort: [
    "1月",
    "2月",
    "3月",
    "4月",
    "5月",
    "6月",
    "7月",
    "8月",
    "9月",
    "10月",
    "11月",
    "12月",
  ],
  dayNames: [
    "日曜日",
    "月曜日",
    "火曜日",
    "水曜日",
    "木曜日",
    "金曜日",
    "土曜日",
  ],
  dayNamesShort: ["日", "月", "火", "水", "木", "金", "土"],
  today: "今日",
};
LocaleConfig.defaultLocale = "ja";

const STATUS_COLORS: Record<string, string> = {
  open: "#89b4fa",
  in_progress: "#f38ba8",
  closed: "#a6e3a1",
  cancelled: "#a6adc8",
  on_hold: "#f5c2e7",
  review: "#89dceb",
};

const STATUS_LABELS: Record<string, string> = {
  open: "未着手",
  in_progress: "進行中",
  closed: "完了",
  cancelled: "キャンセル",
  on_hold: "保留",
  review: "レビュー待ち",
};

type CalendarItem = {
  id: string;
  taskId: string;
  title: string;
  status: string;
  dateKey: string;
  timeText: string | null;
  allDay: boolean;
  projectColor: string | null;
  projectName: string | null;
  tags: Tag[];
  isOccurrence: boolean;
  isRemote?: boolean;
  remoteServerId?: string;
  remoteServerName?: string;
  remoteServerColor?: string | null;
  startAt?: string | null;
  endAt?: string | null;
};

const CALENDAR_MONTH_SCROLL_RANGE = 3;
const CALENDAR_DAY_MIN_HEIGHT = 62;
const CALENDAR_WEEK_VERTICAL_MARGIN = 14;
const CALENDAR_HEADER_HEIGHT = 72;

// Subset of react-native-calendars' CalendarListImperativeMethods (the type is
// not exported from the package entry point).
type CalendarListHandle = {
  scrollToDay: (date: string, offset: number, animated: boolean) => void;
};

type CalendarScrollPhase = "idle" | "dragging" | "settling" | "arrow";

function monthKey(dateString: string): string {
  return dateString.slice(0, 7);
}

function firstDateOfMonth(month: string): string {
  return `${month}-01`;
}

function addMonthKeys(month: string, count: number): string {
  return format(
    addMonths(parseISO(`${firstDateOfMonth(month)}T00:00:00`), count),
    "yyyy-MM",
  );
}

function buildDateKey(value: string | null | undefined): string | null {
  if (!value) return null;
  return format(parseISO(value), "yyyy-MM-dd");
}

function buildTimeText(
  value: string | null | undefined,
  allDay: boolean,
): string | null {
  if (!value || allDay) return null;
  return format(parseISO(value), "HH:mm");
}

function toTaskItem(task: Task): CalendarItem | null {
  const dateKey = buildDateKey(task.end_at || task.start_at);
  if (!dateKey) return null;
  return {
    id: `task-${task.id}`,
    taskId: task.id,
    title: task.title,
    status: task.status,
    dateKey,
    timeText: buildTimeText(task.end_at || task.start_at, task.all_day),
    allDay: task.all_day,
    projectColor: task.project_color ?? null,
    projectName: task.project_name ?? null,
    tags: task.tags || [],
    isOccurrence: false,
    startAt: task.start_at,
    endAt: task.end_at,
  };
}

function toOccurrenceItem(occurrence: TaskOccurrence): CalendarItem | null {
  const dateKey = buildDateKey(occurrence.start_at || occurrence.end_at);
  if (!dateKey) return null;
  return {
    id: `occ-${occurrence.id}`,
    taskId: occurrence.task_id,
    title: occurrence.title || "Untitled task",
    status: occurrence.status,
    dateKey,
    timeText: buildTimeText(
      occurrence.start_at || occurrence.end_at,
      occurrence.all_day,
    ),
    allDay: occurrence.all_day,
    projectColor: occurrence.project_color ?? null,
    projectName: occurrence.project_name ?? null,
    tags: occurrence.tags || [],
    isOccurrence: true,
    startAt: occurrence.start_at,
    endAt: occurrence.end_at,
  };
}

function toRemoteTaskItem(
  task: RemoteTask,
): CalendarItem | null {
  const dateKey = buildDateKey(task.end_at || task.start_at);
  if (!dateKey) return null;
  return {
    id: `remote-${task.remote_server_id}-${task.id}`,
    taskId: task.id,
    title: task.title,
    status: task.status,
    dateKey,
    timeText: buildTimeText(task.end_at || task.start_at, task.all_day),
    allDay: task.all_day,
    projectColor: task.remote_server_color ?? null,
    projectName: task.remote_server_name,
    tags: task.tags || [],
    isOccurrence: false,
    isRemote: true,
    remoteServerId: task.remote_server_id,
    remoteServerName: task.remote_server_name,
    remoteServerColor: task.remote_server_color,
    startAt: task.start_at,
    endAt: task.end_at,
  };
}

export default function CalendarScreen() {
  const router = useRouter();
  const { fontScale } = useWindowDimensions();
  const { isAuthenticated, isAnonymous, user } = useAuth();
  const authScope = isAuthenticated
    ? `auth:${user?.user_id ?? "unknown"}`
    : isAnonymous ? "anonymous" : "signed_out";
  const { selectedProjectId, selectedSpaceId } = useProject();
  const [tasks, setTasks] = useState<Task[]>([]);
  const [occurrences, setOccurrences] = useState<TaskOccurrence[]>([]);
  const [selectedDate, setSelectedDate] = useState<string>(
    format(new Date(), "yyyy-MM-dd"),
  );
  const [calendarWidth, setCalendarWidth] = useState(0);
  // The CalendarList window is anchored to this month. Page index N of the
  // list is always `anchor + (N - RANGE)` months, so every settled offset maps
  // to an absolute month without remounting the list on ordinary swipes.
  const [calendarAnchorMonth, setCalendarAnchorMonth] = useState(
    monthKey(selectedDate),
  );
  // Explicit jumps are day presses that cross the displayed month. Re-anchoring
  // on such a press can land on the anchor month the window already points at
  // (swipe away, then tap an adjacent-month day of the anchor month), so the
  // anchor value alone cannot express "remount and show that page again". This
  // revision counts only those presses and participates in the list key, which
  // keeps ordinary swipes and in-month taps on the same mounted instance.
  const [calendarJumpRevision, setCalendarJumpRevision] = useState(0);
  const selectedMonthRef = useRef(monthKey(selectedDate));
  const calendarAnchorMonthRef = useRef(calendarAnchorMonth);
  const calendarListRef = useRef<CalendarListHandle | null>(null);
  const calendarWidthRef = useRef(0);
  const calendarWindowEpochRef = useRef(0);
  const activeScrollEpochRef = useRef<number | null>(null);
  const activeArrowEpochRef = useRef<number | null>(null);
  const scrollPhaseRef = useRef<CalendarScrollPhase>("idle");
  const fontScaleRef = useRef(fontScale);
  if (fontScaleRef.current !== fontScale) {
    fontScaleRef.current = fontScale;
    calendarAnchorMonthRef.current = selectedMonthRef.current;
    setCalendarAnchorMonth(selectedMonthRef.current);
    calendarWindowEpochRef.current += 1;
    activeScrollEpochRef.current = null;
    activeArrowEpochRef.current = null;
    scrollPhaseRef.current = "idle";
  }
  const calendarWindowEpoch = calendarWindowEpochRef.current;
  const [showClosed, setShowClosed] = useState(false);
  const [hideRecurring, setHideRecurring] = useState(false);
  const [prefsLoaded, setPrefsLoaded] = useState(false);
  const [calendarRefreshToken, setCalendarRefreshToken] = useState(0);
  const completionRefreshToken = useTaskCompletionUndoStore(
    (state) => state.refreshToken,
  );
  const {
    remoteTasks,
    profiles: remoteProfiles,
    reload: reloadRemote,
  } = useRemoteTasks(isAuthenticated, {
    projectId: selectedProjectId,
    spaceId: selectedSpaceId,
  }, { deferInitialLoad: true });
  const [remoteDialogTarget, setRemoteDialogTarget] =
    useState<RemoteTaskDialogTarget | null>(null);

  useEffect(() => {
    selectedMonthRef.current = monthKey(selectedDate);
  }, [selectedDate]);

  const handleCalendarLayout = useCallback((event: LayoutChangeEvent) => {
    const nextWidth = event.nativeEvent.layout.width;
    if (nextWidth <= 0 || nextWidth === calendarWidthRef.current) return;
    calendarWidthRef.current = nextWidth;
    calendarAnchorMonthRef.current = selectedMonthRef.current;
    setCalendarAnchorMonth(selectedMonthRef.current);
    calendarWindowEpochRef.current += 1;
    activeScrollEpochRef.current = null;
    activeArrowEpochRef.current = null;
    scrollPhaseRef.current = "idle";
    setCalendarWidth(nextWidth);
  }, []);

  const handleCalendarDatePress = useCallback((dateString: string) => {
    if (calendarWindowEpoch !== calendarWindowEpochRef.current) return;
    const nextMonth = monthKey(dateString);
    const previousMonth = selectedMonthRef.current;
    activeScrollEpochRef.current = null;
    activeArrowEpochRef.current = null;
    scrollPhaseRef.current = "idle";
    selectedMonthRef.current = nextMonth;
    setSelectedDate(dateString);
    if (nextMonth !== previousMonth) {
      // Selecting an adjacent-month day outside the visible page is an
      // explicit jump: re-anchor the window on that month.
      calendarAnchorMonthRef.current = nextMonth;
      setCalendarAnchorMonth(nextMonth);
      setCalendarJumpRevision((revision) => revision + 1);
      calendarWindowEpochRef.current += 1;
    }
  }, [calendarWindowEpoch]);

  const handleCalendarScrollBeginDrag = useCallback(
    (_event: NativeSyntheticEvent<NativeScrollEvent>) => {
      if (calendarWindowEpoch !== calendarWindowEpochRef.current) return;
      activeArrowEpochRef.current = null;
      activeScrollEpochRef.current = calendarWindowEpochRef.current;
      scrollPhaseRef.current = "dragging";
    },
    [calendarWindowEpoch],
  );

  const handleCalendarScrollEndDrag = useCallback(
    (_event: NativeSyntheticEvent<NativeScrollEvent>) => {
      if (calendarWindowEpoch !== calendarWindowEpochRef.current) return;
      if (scrollPhaseRef.current === "dragging") {
        scrollPhaseRef.current = "settling";
      }
    },
    [calendarWindowEpoch],
  );

  // CalendarList's own arrow path (addMonth -> scrollToMonth) follows
  // `animateScroll`, which is disabled so that automatic mount/re-anchor
  // scrolls never animate. A non-animated scroll emits no momentum end, so the
  // arrow would never settle. Drive the arrow through the imperative animated
  // scroll instead; its momentum end settles the month like a swipe.
  const handleCalendarArrowStep = useCallback(
    (delta: -1 | 1) => {
      if (calendarWindowEpoch !== calendarWindowEpochRef.current) return;
      // Do not replace an in-flight drag or arrow animation. On Android an
      // old animation-cancel momentum event can arrive after that replacement
      // and would otherwise settle the wrong page.
      if (scrollPhaseRef.current !== "idle") return;
      const list = calendarListRef.current;
      if (!list) return;
      const targetMonth = addMonthKeys(selectedMonthRef.current, delta);
      const pageIndex =
        differenceInCalendarMonths(
          parseISO(`${firstDateOfMonth(targetMonth)}T00:00:00`),
          parseISO(
            `${firstDateOfMonth(calendarAnchorMonthRef.current)}T00:00:00`,
          ),
        ) + CALENDAR_MONTH_SCROLL_RANGE;
      if (pageIndex < 0 || pageIndex > CALENDAR_MONTH_SCROLL_RANGE * 2) return;
      if (pageIndex === 0) {
        // The library skips a scroll to offset 0, which would leave the arrow
        // phase unsettled. Jump by re-anchoring on the target month instead.
        selectedMonthRef.current = targetMonth;
        calendarAnchorMonthRef.current = targetMonth;
        setSelectedDate(firstDateOfMonth(targetMonth));
        setCalendarAnchorMonth(targetMonth);
        calendarWindowEpochRef.current += 1;
        return;
      }
      activeScrollEpochRef.current = null;
      activeArrowEpochRef.current = calendarWindowEpochRef.current;
      scrollPhaseRef.current = "arrow";
      list.scrollToDay(firstDateOfMonth(targetMonth), 0, true);
    },
    [calendarWindowEpoch],
  );
  const handleCalendarArrowLeft = useCallback(
    () => handleCalendarArrowStep(-1),
    [handleCalendarArrowStep],
  );
  const handleCalendarArrowRight = useCallback(
    () => handleCalendarArrowStep(1),
    [handleCalendarArrowStep],
  );

  const handleCalendarMomentumScrollEnd = useCallback(
    (event: NativeSyntheticEvent<NativeScrollEvent>) => {
      if (calendarWindowEpoch !== calendarWindowEpochRef.current) return;
      // React Native may emit onScrollBeginDrag while stopping an earlier
      // momentum animation. Keep the newer gesture armed until its own
      // onScrollEndDrag arrives; the earlier momentum end must be ignored.
      if (scrollPhaseRef.current === "dragging") return;
      const hasCurrentDrag =
        activeScrollEpochRef.current === calendarWindowEpochRef.current &&
        scrollPhaseRef.current === "settling";
      const hasCurrentArrow =
        activeArrowEpochRef.current === calendarWindowEpochRef.current &&
        scrollPhaseRef.current === "arrow";
      if (!hasCurrentDrag && !hasCurrentArrow) return;

      activeScrollEpochRef.current = null;
      activeArrowEpochRef.current = null;
      scrollPhaseRef.current = "idle";
      const width = calendarWidthRef.current;
      const endOffset = event.nativeEvent.contentOffset.x;
      if (width <= 0) return;
      const pageIndex = Math.round(endOffset / width);
      // Page indexes are logical (data order) on every supported platform:
      // react-native-calendars only mirrors offsets for RN < 0.73
      // (`shouldFixRTL`), and this app pins RN 0.81. RTL therefore needs no
      // flip here, matching the unflipped offsets scrollToDay/scrollToMonth use.
      const anchorOffset = pageIndex - CALENDAR_MONTH_SCROLL_RANGE;

      // The offset is absolute within the window, so map it from the anchor
      // month (not from the previously selected month).
      const targetMonth = addMonthKeys(
        calendarAnchorMonthRef.current,
        anchorOffset,
      );

      // A drag that snaps back to its original page must not change the
      // selected day or trigger a settings/query refresh.
      if (targetMonth === selectedMonthRef.current) return;

      selectedMonthRef.current = targetMonth;
      setSelectedDate(firstDateOfMonth(targetMonth));

      // Keep the list mounted for ordinary swipes. Re-anchor (remount) only
      // when the settled page is at the window edge, so the finite window
      // has room to continue in both directions.
      if (
        pageIndex <= 0 ||
        pageIndex >= CALENDAR_MONTH_SCROLL_RANGE * 2
      ) {
        calendarAnchorMonthRef.current = targetMonth;
        setCalendarAnchorMonth(targetMonth);
        calendarWindowEpochRef.current += 1;
      }
    },
    [calendarWindowEpoch],
  );

  useEffect(() => {
    let active = true;

    const loadSettings = async () => {
      if (!isAuthenticated) {
        if (active) setPrefsLoaded(true);
        return;
      }
      try {
        const settings = await taskApi.getUserSettings();
        if (!active) return;
        const view = settings.calendar_view;
        if (typeof view?.show_closed === "boolean")
          setShowClosed(view.show_closed);
        if (typeof view?.hide_recurring === "boolean")
          setHideRecurring(view.hide_recurring);
      } catch (err) {
        console.error("カレンダー表示設定の取得に失敗しました", err);
      } finally {
        if (active) setPrefsLoaded(true);
      }
    };

    void loadSettings();
    return () => {
      active = false;
    };
  }, [isAuthenticated]);

  useEffect(() => {
    if (!prefsLoaded || !isAuthenticated) return;

    void taskApi
      .updateUserSettings({
        calendar_view: {
          selected_date: selectedDate,
          show_closed: showClosed,
          hide_recurring: hideRecurring,
        },
      })
      .catch((err) => {
        console.error("カレンダー表示設定の保存に失敗しました", err);
      });
  }, [isAuthenticated, prefsLoaded, selectedDate, showClosed, hideRecurring]);

  useFocusEffect(
    useCallback(() => {
      let active = true;
      let requestGeneration = 0;
      let loadInFlight = false;
      let appActive = AppState.currentState === "active";
      let needsLoadOnActive = false;
      let reloadAfterInFlight = false;
      let pendingLocalRefresh = false;
      let scheduledLoad: { cancel: () => void; localOnly: boolean } | null = null;
      const performLoad = async (localOnly = false) => {
        const request = ++requestGeneration;
        const isCurrent = () => active && request === requestGeneration;
        // Cover the whole mounted pager window. It is anchored to the list
        // anchor, which stays fixed while ordinary swipes move the selection.
        const anchorMonth = startOfMonth(
          parseISO(`${firstDateOfMonth(calendarAnchorMonth)}T00:00:00`),
        );
        const monthWindowStart = startOfMonth(
          addMonths(anchorMonth, -CALENDAR_MONTH_SCROLL_RANGE),
        );
        const monthWindowEnd = endOfMonth(
          addMonths(anchorMonth, CALENDAR_MONTH_SCROLL_RANGE),
        );
        const monthStart = startOfWeek(monthWindowStart, { weekStartsOn: 0 });
        const monthEnd = endOfWeek(monthWindowEnd, { weekStartsOn: 0 });
        const startIso = monthStart.toISOString();
        const endIso = monthEnd.toISOString();
        let localLoaded = false;

        try {
          const [localTaskList, localOccurrenceList] = await Promise.all([
            selectedProjectId
              ? tasksRepo.listLocal(selectedProjectId)
              : selectedSpaceId
                ? tasksRepo.listLocalBySpace(selectedSpaceId)
                : tasksRepo.listLocal(null),
            occurrencesRepo.listLocal(
              selectedProjectId ?? null,
              selectedSpaceId ?? null,
              startIso,
              endIso,
            ),
          ]);
          localLoaded = true;
          if (!isCurrent()) return;
          setTasks(localTaskList);
          setOccurrences(localOccurrenceList);
        } catch (error) {
          console.error(
            "カレンダーのローカルキャッシュ取得に失敗しました",
            error,
          );
        }

        if (localOnly || !isCurrent()) return;

        try {
          const [taskList, occurrenceList] = await Promise.all([
            selectedProjectId
              ? tasksRepo.list(selectedProjectId)
              : tasksRepo.listByScope(
                  selectedSpaceId ? { space_id: selectedSpaceId } : {},
                ),
            occurrencesRepo.list(
              selectedProjectId ?? null,
              selectedSpaceId ?? null,
              startIso,
              endIso,
            ),
          ]);
          if (!isCurrent()) return;
          setTasks(taskList);
          setOccurrences(occurrenceList);
        } catch (error) {
          console.error("カレンダーの同期データ取得に失敗しました", error);
          if (!isCurrent() || localLoaded) return;
          setTasks([]);
          setOccurrences([]);
        }
      };
      const load = async (localOnly = false) => {
        loadInFlight = true;
        try {
          await performLoad(localOnly);
        } finally {
          loadInFlight = false;
          if (!active || !appActive) return;
          if (reloadAfterInFlight) {
            reloadAfterInFlight = false;
            scheduleLoad();
          } else if (pendingLocalRefresh) {
            pendingLocalRefresh = false;
            scheduleLoad(true);
          }
        }
      };
      const scheduleLoad = (localOnly = false) => {
        if (!appActive) {
          if (localOnly) pendingLocalRefresh = true;
          else needsLoadOnActive = true;
          return;
        }
        if (loadInFlight) {
          if (localOnly) pendingLocalRefresh = true;
          else reloadAfterInFlight = true;
          return;
        }
        if (scheduledLoad) {
          // A pending full load already includes the local refresh and its
          // remote phase. Do not replace it with a local-only revision wake.
          // A full request does supersede a pending local-only wake.
          if (!localOnly && scheduledLoad.localOnly) {
            scheduledLoad.cancel();
            scheduledLoad = null;
          } else {
            return;
          }
        }
        const interaction = scheduleAfterNavigation(() => {
          scheduledLoad = null;
          if (active && appActive) {
            void load(localOnly);
          } else if (localOnly) {
            pendingLocalRefresh = true;
          } else {
            needsLoadOnActive = true;
          }
        });
        scheduledLoad = { cancel: () => interaction.cancel(), localOnly };
      };
      const unsubscribeGeneral = useSyncStatusStore.subscribe((state, previous) => {
        if (state.generalAuthScope !== previous.generalAuthScope &&
            state.generalAuthScope !== authScope) {
          ++requestGeneration;
          return;
        }
        if (state.generalRevision === previous.generalRevision ||
            state.generalAuthScope !== authScope ||
            !appActive) return;
        // A revision refresh must never enter the remote portion of load.
        scheduleLoad(true);
      });
      const appStateSubscription = AppState.addEventListener("change", (state) => {
        appActive = state === "active";
        if (!appActive) {
          ++requestGeneration;
          needsLoadOnActive = true;
          scheduledLoad?.cancel();
          scheduledLoad = null;
          return;
        }
        if (needsLoadOnActive) {
          needsLoadOnActive = false;
          scheduleLoad();
        }
      });
      // The calendar tab is often entered from Docs, whose focus work can
      // still be settling. Defer SQLite/network reads until the tab
      // transition has yielded so the destination becomes interactive first.
      scheduleLoad();
      return () => {
        active = false;
        ++requestGeneration;
        scheduledLoad?.cancel();
        scheduledLoad = null;
        unsubscribeGeneral();
        appStateSubscription.remove();
      };
    }, [
      authScope,
      completionRefreshToken,
      selectedProjectId,
      selectedSpaceId,
      calendarAnchorMonth,
      calendarRefreshToken,
    ]),
  );

  const itemsByDate = useMemo(() => {
    const map: Record<string, CalendarItem[]> = {};
    const push = (item: CalendarItem | null) => {
      if (!item) return;
      if (
        !showClosed &&
        (item.status === "closed" || item.status === "cancelled")
      )
        return;
      if (!map[item.dateKey]) map[item.dateKey] = [];
      map[item.dateKey].push(item);
    };

    // 繰り返しルールを持たないタスクは tasks 本体の予定が正本。
    // バックエンドはそれらにも task_occurrences へ source_kind="task_schedule" の
    // ミラー行を1件作るため、そのまま描画するとタスク本体と二重になる。
    // しかもタスク本体は end_at 基準、オカレンスは start_at 基準で日付が決まるので、
    // 同じタスクが別々の日に1件ずつ並ぶ形の重複になる。
    // モバイルは端末内 SQLite キャッシュを先に描画し、applyRemoteOccurrences は
    // upsert のみで API が返さなくなった古いミラー行を消さないため、
    // サーバー側の修正だけでは重複が残る。よってここで必ずミラー行を落とす。
    // 落とすのはオカレンス側。タスク本体を残さないと、ミラー行を持たない
    // 他の非繰り返しタスク（end_at 基準で表示）と日付の基準がずれてしまう。
    const nonRecurringTaskIds = new Set(
      tasks.filter((task) => !task.has_recurrence).map((task) => task.id),
    );

    // 実際に描画するオカレンス。繰り返し非表示のときは 1 件も出さない。
    const visibleOccurrences = hideRecurring
      ? []
      : occurrences.filter(
          (occurrence) => !nonRecurringTaskIds.has(occurrence.task_id),
        );

    tasks
      .filter((task) => !task.has_recurrence)
      .forEach((task) => push(toTaskItem(task)));

    visibleOccurrences.forEach((occurrence) =>
      push(toOccurrenceItem(occurrence)),
    );

    remoteTasks.forEach((task) => push(toRemoteTaskItem(task)));

    for (const key of Object.keys(map)) {
      map[key].sort((a, b) => {
        if (a.allDay !== b.allDay) return a.allDay ? -1 : 1;
        return (a.timeText || "").localeCompare(b.timeText || "");
      });
    }

    return map;
  }, [tasks, occurrences, remoteTasks, showClosed, hideRecurring]);

  const openRemoteDialog = useCallback(
    (item: CalendarItem) => {
      if (!item.isRemote || !item.remoteServerId) return;
      const profile = remoteProfiles.find((p) => p.id === item.remoteServerId);
      if (!profile) return;
      setRemoteDialogTarget({
        profileId: profile.id,
        profileName: profile.name,
        profileColor: profile.display_color,
        baseUrl: profile.base_url,
        taskId: item.taskId,
        title: item.title,
        status: item.status,
        startAt: item.startAt,
        endAt: item.endAt,
      });
    },
    [remoteProfiles],
  );

  const selectedItems = useMemo(
    () => itemsByDate[selectedDate] || [],
    [itemsByDate, selectedDate],
  );

  const todayKey = format(new Date(), "yyyy-MM-dd");

  const handleStatusToggle = async (item: CalendarItem) => {
    const next = item.status === "closed" ? "open" : "closed";
    try {
      if (item.isOccurrence) {
        await taskApi.updateOccurrence(item.id.replace(/^occ-/, ""), {
          status: next,
        });
      } else {
        await tasksRepo.update(item.taskId, { status: next });
      }
      const taskList = selectedProjectId
        ? await tasksRepo.list(selectedProjectId)
        : await tasksRepo.listByScope(
            selectedSpaceId ? { space_id: selectedSpaceId } : {},
          );
      setTasks(taskList);
      if (isTaskCompletionTransition(item.status, next)) {
        enqueueTaskCompletionUndoBatch({
          entries: [
            createTaskCompletionUndoEntry({
              id: item.taskId,
              title: item.title,
              status: item.status,
              completed_at: null,
            }),
          ],
        });
      }
      setCalendarRefreshToken((value) => value + 1);
    } catch {
      // ignore
    }
  };

  const handleMoveToSelectedDate = useCallback(
    async (item: CalendarItem) => {
      if (item.isRemote || !item.startAt) return;
      const start = parseISO(item.startAt);
      if (Number.isNaN(start.getTime())) return;
      const end = item.endAt ? parseISO(item.endAt) : null;
      const duration = end && !Number.isNaN(end.getTime())
        ? Math.max(0, end.getTime() - start.getTime())
        : 0;
      const nextStartDate = parseISO(
        `${selectedDate}T${format(start, "HH:mm:ss")}`,
      );
      const nextStart = `${selectedDate}T${format(start, "HH:mm:ss")}`;
      const nextEnd = duration
        ? format(
            new Date(nextStartDate.getTime() + duration),
            "yyyy-MM-dd'T'HH:mm:ss",
          )
        : null;
      try {
        if (item.isOccurrence) {
          await taskApi.updateOccurrence(item.id.replace(/^occ-/, ""), {
            start_at: nextStart,
            end_at: nextEnd,
          });
        } else {
          await tasksRepo.update(item.taskId, {
            start_at: nextStart,
            end_at: nextEnd,
          });
        }
        setCalendarRefreshToken((value) => value + 1);
      } catch (error) {
        Alert.alert(
          "移動できませんでした",
          error instanceof Error ? error.message : "サーバーへの保存に失敗しました",
        );
      }
    },
    [selectedDate],
  );

  const handleDeleteOccurrence = useCallback((item: CalendarItem) => {
    if (!item.isOccurrence || item.isRemote) return;
    Alert.alert(
      "予定を削除しますか？",
      "この繰り返し回だけをキャンセルします。繰り返しルール本体は変更されません。",
      [
        { text: "キャンセル", style: "cancel" },
        {
          text: "削除",
          style: "destructive",
          onPress: () => {
            void taskApi
              .deleteOccurrence(item.id.replace(/^occ-/, ""))
              .then(() => setCalendarRefreshToken((value) => value + 1))
              .catch((error) =>
                Alert.alert(
                  "削除できませんでした",
                  error instanceof Error ? error.message : "保存に失敗しました",
                ),
              );
          },
        },
      ],
    );
  }, []);

  const calendarDayMinHeight = Math.max(
    CALENDAR_DAY_MIN_HEIGHT,
    Math.ceil(CALENDAR_DAY_MIN_HEIGHT * fontScale),
  );
  const calendarHeight =
    (calendarDayMinHeight + CALENDAR_WEEK_VERTICAL_MARGIN) * 6 +
    CALENDAR_HEADER_HEIGHT;

  return (
    <View style={styles.container}>
      <ScreenHeader
        title="Calendar"
        right={
          <View style={styles.headerActions}>
            <ScopeSwitcher variant="chip" allLabel="全体" projectNameOnly />
            <IconButton
              icon="plus"
              iconColor="#cdd6f4"
              accessibilityLabel="選択日のタスクを作成"
              onPress={() => {
                const query = selectedProjectId
                  ? `?projectId=${encodeURIComponent(selectedProjectId)}&startDate=${selectedDate}`
                  : selectedSpaceId
                    ? `?spaceId=${encodeURIComponent(selectedSpaceId)}&startDate=${selectedDate}`
                    : `?startDate=${selectedDate}`;
                router.push(`/(tabs)/tasks/create${query}`);
              }}
            />
          </View>
        }
      />
      <FlatList
        data={selectedItems}
        style={styles.taskList}
        ListHeaderComponent={
          <View style={styles.calendarHeader}>
            <Surface style={styles.header} elevation={1}>
              <View style={styles.toggleRow}>
                <View style={styles.toggleItem}>
                  <Text style={styles.toggleLabel}>完了</Text>
                  <Switch value={showClosed} onValueChange={setShowClosed} />
                </View>
                <View style={styles.toggleItem}>
                  <Text style={styles.toggleLabel}>繰り返し非表示</Text>
                  <Switch value={hideRecurring} onValueChange={setHideRecurring} />
                </View>
              </View>
            </Surface>

            <View
              testID="calendar-month-swipe"
              style={styles.calendarViewport}
              onLayout={handleCalendarLayout}
            >
              {calendarWidth > 0 ? (
                <CalendarList
                  ref={calendarListRef}
                  key={`calendar-${calendarWidth}-${fontScale}-${calendarAnchorMonth}-${calendarJumpRevision}`}
                  current={firstDateOfMonth(calendarAnchorMonth)}
                  firstDay={0}
                  horizontal
                  pagingEnabled
                  staticHeader
                  hideArrows={false}
                  animateScroll={false}
                  pastScrollRange={CALENDAR_MONTH_SCROLL_RANGE}
                  futureScrollRange={CALENDAR_MONTH_SCROLL_RANGE}
                  calendarWidth={calendarWidth}
                  calendarHeight={calendarHeight}
                  hideExtraDays={false}
                  showScrollIndicator={false}
                  dayComponent={({ date, state }) => {
                    if (!date)
                      return (
                        <View
                          style={[
                            styles.monthDay,
                            { minHeight: calendarDayMinHeight },
                          ]}
                        />
                      );
                    const dayItems = itemsByDate[date.dateString] || [];
                    const isSelected = selectedDate === date.dateString;
                    const isToday = todayKey === date.dateString;
                    return (
                      <Pressable
                        accessibilityRole="button"
                        accessibilityLabel={
                          date.dateString + " " + dayItems.length + "件"
                        }
                        onPress={() =>
                          handleCalendarDatePress(date.dateString)
                        }
                        style={[
                          styles.monthDay,
                          { minHeight: calendarDayMinHeight },
                          isSelected && styles.monthDaySelected,
                          state === "disabled" && styles.monthDayOutside,
                        ]}
                      >
                        <Text
                          style={[
                            styles.monthDayNumber,
                            state === "disabled" &&
                              styles.monthDayNumberOutside,
                            isToday && styles.monthDayNumberToday,
                            isSelected && styles.monthDayNumberSelected,
                          ]}
                        >
                          {date.day}
                        </Text>
                        <View style={styles.monthTaskStack}>
                          {dayItems.slice(0, 2).map((item) => (
                            <View key={item.id} style={styles.monthTaskLine}>
                              <View
                                style={[
                                  styles.monthTaskDot,
                                  {
                                    backgroundColor:
                                      item.projectColor ||
                                      STATUS_COLORS[item.status] ||
                                      "#585b70",
                                  },
                                ]}
                              />
                              <Text
                                numberOfLines={1}
                                style={[
                                  styles.monthTaskTitle,
                                  item.status === "closed" &&
                                    styles.monthTaskClosed,
                                ]}
                              >
                                {item.title}
                              </Text>
                            </View>
                          ))}
                          {dayItems.length > 2 ? (
                            <Text style={styles.monthTaskMore}>
                              +{dayItems.length - 2}
                            </Text>
                          ) : null}
                        </View>
                      </Pressable>
                    );
                  }}
                  onPressArrowLeft={handleCalendarArrowLeft}
                  onPressArrowRight={handleCalendarArrowRight}
                  onScrollBeginDrag={handleCalendarScrollBeginDrag}
                  onScrollEndDrag={handleCalendarScrollEndDrag}
                  onMomentumScrollEnd={handleCalendarMomentumScrollEnd}
                  theme={{
                    calendarBackground: "#181825",
                    monthTextColor: "#cdd6f4",
                    textMonthFontWeight: "bold",
                    textMonthFontSize: 16,
                    arrowColor: "#7c3aed",
                    todayTextColor: "#7c3aed",
                    todayBackgroundColor: "transparent",
                    dayTextColor: "#cdd6f4",
                    textDisabledColor: "#585b70",
                    selectedDayBackgroundColor: "#4c1d95",
                    selectedDayTextColor: "#cdd6f4",
                    textDayFontSize: 14,
                    textDayHeaderFontSize: 12,
                    textSectionTitleColor: "#a6adc8",
                    stylesheet: {
                      calendar: {
                        header: {
                          dayTextAtIndex0: { color: "#f38ba8" },
                          dayTextAtIndex6: { color: "#89b4fa" },
                        },
                      },
                    },
                  }}
                />
              ) : null}
            </View>

            <Surface style={styles.taskListHeader} elevation={0}>
              <Text style={styles.taskListTitle}>
                {format(parseISO(`${selectedDate}T00:00:00`), "M月d日 (E)", {
                  locale: ja,
                })}{" "}
                • {selectedItems.length}件
              </Text>
            </Surface>
          </View>
        }
        keyExtractor={(item) => item.id}
        renderItem={({ item }) => (
          <Surface style={styles.taskItem} elevation={0}>
            <View
              style={[
                styles.projectStripe,
                {
                  backgroundColor: item.projectColor || "transparent",
                },
              ]}
            />
            <IconButton
              icon={
                item.isRemote
                  ? "server-network"
                  : item.status === "closed"
                    ? "check-circle"
                    : "circle-outline"
              }
              iconColor={STATUS_COLORS[item.status] || "#a6adc8"}
              size={20}
              onPress={() =>
                item.isRemote
                  ? openRemoteDialog(item)
                  : handleStatusToggle(item)
              }
              style={{ margin: 0 }}
            />
            <Pressable
              style={{ flex: 1 }}
              accessibilityRole="button"
              accessibilityLabel={`${item.title}を開く`}
              onPress={() => {
                if (item.isRemote) {
                  openRemoteDialog(item);
                  return;
                }
                router.push(`/(tabs)/tasks/${item.taskId}`);
              }}
              onLongPress={() => handleDeleteOccurrence(item)}
            >
              <Text
                style={[
                  styles.taskTitle,
                  item.status === "closed" && {
                    textDecorationLine: "line-through",
                    color: "#a6adc8",
                  },
                ]}
              >
                {item.title}
              </Text>
              {item.projectName ? (
                <Text style={styles.projectName}>{item.projectName}</Text>
              ) : null}
              <View style={styles.taskMeta}>
                {item.timeText ? (
                  <Text style={styles.taskTime}>{item.timeText}</Text>
                ) : null}
                <Text
                  style={[
                    styles.statusChip,
                    { color: STATUS_COLORS[item.status] || "#a6adc8" },
                  ]}
                >
                  {STATUS_LABELS[item.status] || item.status}
                </Text>
                {item.isOccurrence ? (
                  <Text style={styles.occurrenceBadge}>Recurring</Text>
                ) : null}
                {item.isRemote ? (
                  <Text style={styles.remoteBadge}>外部</Text>
                ) : null}
              </View>
              {item.tags.length > 0 ? (
                <View style={styles.tagRow}>
                  {item.tags.slice(0, 3).map((tag) => (
                    <View
                      key={tag.id}
                      style={[
                        styles.tagDot,
                        { backgroundColor: tag.color || "#45475a" },
                      ]}
                    >
                      <Text style={styles.tagDotText}>{tag.name}</Text>
                    </View>
                  ))}
                </View>
              ) : null}
            </Pressable>
            {!item.isRemote ? (
              <IconButton
                icon="calendar-edit"
                iconColor="#89b4fa"
                size={18}
                accessibilityLabel={`${item.title}を${selectedDate}へ移動`}
                onPress={() => void handleMoveToSelectedDate(item)}
                style={{ margin: 0 }}
              />
            ) : null}
          </Surface>
        )}
        ListEmptyComponent={
          <Text style={styles.emptyText}>この日のタスクはありません</Text>
        }
        contentContainerStyle={{ paddingHorizontal: 8, paddingBottom: 20 }}
      />

      <RemoteTaskDialog
        target={remoteDialogTarget}
        onDismiss={() => setRemoteDialogTarget(null)}
        onUpdated={() => void reloadRemote()}
      />
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: "#11111b" },
  headerActions: { flexDirection: "row", alignItems: "center" },
  header: {
    paddingTop: 4,
    paddingBottom: 8,
    paddingHorizontal: 16,
    backgroundColor: "#1e1e2e",
  },
  toggleRow: { flexDirection: "row", gap: 12 },
  toggleItem: {
    flex: 1,
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
  },
  toggleLabel: { color: "#a6adc8", fontSize: 12 },
  taskList: { flex: 1 },
  calendarHeader: { marginHorizontal: -8 },
  calendarViewport: { width: "100%" },
  monthDay: {
    minHeight: 62,
    paddingHorizontal: 2,
    paddingTop: 2,
    borderRadius: 6,
    justifyContent: "flex-start",
  },
  monthDaySelected: { backgroundColor: "#313244" },
  monthDayOutside: { opacity: 0.45 },
  monthDayNumber: {
    color: "#cdd6f4",
    fontSize: 12,
    fontWeight: "700",
    textAlign: "center",
    marginBottom: 2,
  },
  monthDayNumberOutside: { color: "#6c7086" },
  monthDayNumberToday: { color: "#cba6f7" },
  monthDayNumberSelected: { color: "#ffffff" },
  monthTaskStack: { gap: 1 },
  monthTaskLine: {
    minWidth: 0,
    flexDirection: "row",
    alignItems: "center",
    paddingHorizontal: 1,
  },
  monthTaskDot: {
    width: 4,
    height: 4,
    borderRadius: 2,
    marginRight: 2,
    flexShrink: 0,
  },
  monthTaskTitle: {
    minWidth: 0,
    flexShrink: 1,
    color: "#cdd6f4",
    fontSize: 8,
    lineHeight: 10,
  },
  monthTaskClosed: {
    color: "#7f849c",
    textDecorationLine: "line-through",
  },
  monthTaskMore: {
    color: "#9399b2",
    fontSize: 8,
    lineHeight: 10,
    paddingLeft: 6,
  },
  taskListHeader: {
    backgroundColor: "#11111b",
    paddingHorizontal: 16,
    paddingVertical: 10,
  },
  taskListTitle: { color: "#a6adc8", fontSize: 13, fontWeight: "bold" },
  taskItem: {
    backgroundColor: "#1e1e2e",
    borderRadius: 8,
    padding: 8,
    marginBottom: 4,
    flexDirection: "row",
    alignItems: "center",
    gap: 4,
  },
  projectStripe: { width: 4, alignSelf: "stretch", borderRadius: 999 },
  taskTitle: { color: "#cdd6f4", fontSize: 14 },
  projectName: { color: "#bac2de", fontSize: 11, marginTop: 2 },
  taskMeta: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    marginTop: 2,
    flexWrap: "wrap",
  },
  taskTime: { color: "#a6adc8", fontSize: 11 },
  statusChip: { fontSize: 10 },
  occurrenceBadge: { color: "#f9e2af", fontSize: 10 },
  remoteBadge: { color: "#89b4fa", fontSize: 10, fontWeight: "700" },
  tagRow: { flexDirection: "row", gap: 4, marginTop: 4, flexWrap: "wrap" },
  tagDot: { paddingHorizontal: 6, paddingVertical: 1, borderRadius: 3 },
  tagDotText: { color: "#cdd6f4", fontSize: 9 },
  emptyText: {
    color: "#585b70",
    textAlign: "center",
    paddingVertical: 20,
    fontSize: 13,
  },
});
