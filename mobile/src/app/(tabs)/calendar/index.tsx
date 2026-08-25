import React, { useState, useCallback, useMemo, useEffect } from "react";
import { Alert, View, FlatList, StyleSheet, Pressable } from "react-native";
import {
  Text,
  Surface,
  IconButton,
  Switch,
} from "react-native-paper";
import { Calendar, LocaleConfig } from "react-native-calendars";
import { useFocusEffect, useRouter } from "expo-router";
import {
  endOfMonth,
  format,
  parseISO,
  startOfMonth,
  subDays,
  addDays,
} from "date-fns";
import { ja } from "date-fns/locale";
import { tasksRepo, occurrencesRepo } from "../../../repositories";
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

const UNCOLORED_PROJECT_DOT = "#585b70";

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
  const { isAuthenticated } = useAuth();
  const {
    selectedProjectId,
    selectedSpaceId,
    selectedSpace,
    selectedProject,
  } = useProject();
  const [tasks, setTasks] = useState<Task[]>([]);
  const [occurrences, setOccurrences] = useState<TaskOccurrence[]>([]);
  const [selectedDate, setSelectedDate] = useState<string>(
    format(new Date(), "yyyy-MM-dd"),
  );
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
  } = useRemoteTasks(isAuthenticated);
  const [remoteDialogTarget, setRemoteDialogTarget] =
    useState<RemoteTaskDialogTarget | null>(null);

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
        if (view?.selected_date) setSelectedDate(view.selected_date);
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
      const load = async () => {
        const monthStart = subDays(
          startOfMonth(parseISO(`${selectedDate}T00:00:00`)),
          31,
        );
        const monthEnd = addDays(
          endOfMonth(parseISO(`${selectedDate}T00:00:00`)),
          90,
        );
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
          if (!active) return;
          setTasks(localTaskList);
          setOccurrences(localOccurrenceList);
        } catch (error) {
          console.error(
            "カレンダーのローカルキャッシュ取得に失敗しました",
            error,
          );
        }

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
          if (!active) return;
          setTasks(taskList);
          setOccurrences(occurrenceList);
        } catch (error) {
          console.error("カレンダーの同期データ取得に失敗しました", error);
          if (!active || localLoaded) return;
          setTasks([]);
          setOccurrences([]);
        }
      };
      void load();
      return () => {
        active = false;
      };
    }, [
      completionRefreshToken,
      selectedProjectId,
      selectedSpaceId,
      selectedDate,
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

  const markedDates = useMemo(() => {
    const marks: Record<
      string,
      {
        marked: boolean;
        dots: { key: string; color: string }[];
        selected?: boolean;
        selectedColor?: string;
      }
    > = {};

    for (const [date, items] of Object.entries(itemsByDate)) {
      const dots = items.slice(0, 3).map((item, index) => ({
        key: `${item.id}-${index}`,
        color: item.projectColor || UNCOLORED_PROJECT_DOT,
      }));
      marks[date] = { marked: true, dots };
    }

    if (marks[selectedDate]) {
      marks[selectedDate] = {
        ...marks[selectedDate],
        selected: true,
        selectedColor: "#4c1d95",
      };
    } else {
      marks[selectedDate] = {
        marked: false,
        dots: [],
        selected: true,
        selectedColor: "#4c1d95",
      };
    }

    return marks;
  }, [itemsByDate, selectedDate]);

  const selectedItems = useMemo(
    () => itemsByDate[selectedDate] || [],
    [itemsByDate, selectedDate],
  );

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

  return (
    <View style={styles.container}>
      <ScreenHeader
        title="カレンダー"
        subtitle={
          selectedProject?.name ||
          selectedSpace?.name ||
          "すべてのプロジェクト"
        }
        right={
          <View style={styles.headerActions}>
            <ScopeSwitcher />
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
      <Surface style={styles.header} elevation={1}>
        <View style={styles.toggleRow}>
          <View style={styles.toggleItem}>
            <Text style={styles.toggleLabel}>完了を表示</Text>
            <Switch value={showClosed} onValueChange={setShowClosed} />
          </View>
          <View style={styles.toggleItem}>
            <Text style={styles.toggleLabel}>繰り返しを非表示</Text>
            <Switch value={hideRecurring} onValueChange={setHideRecurring} />
          </View>
        </View>
      </Surface>

      <Calendar
        markingType="multi-dot"
        markedDates={markedDates}
        onDayPress={(day: { dateString: string }) =>
          setSelectedDate(day.dateString)
        }
        firstDay={0}
        enableSwipeMonths
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

      <Surface style={styles.taskListHeader} elevation={0}>
        <Text style={styles.taskListTitle}>
          {format(parseISO(`${selectedDate}T00:00:00`), "M月d日 (E)", {
            locale: ja,
          })}{" "}
          • {selectedItems.length}件
        </Text>
      </Surface>

      <FlatList
        data={selectedItems}
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
  toggleRow: { gap: 8 },
  toggleItem: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
  },
  toggleLabel: { color: "#a6adc8", fontSize: 12 },
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
